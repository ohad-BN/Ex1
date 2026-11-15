from __future__ import annotations

import io
from pathlib import Path

import numpy as np
from openpyxl import load_workbook
import pytest

from app import create_app
from app.config import Config
from app.services.face_counter import (
    BoundingBox,
    FaceDetector,
    TiledSCRFDDetector,
    read_image_unicode,
    write_image_unicode,
)
from app.services.reconciler import AttendanceReconciler


class FakeFaceDetector(FaceDetector):
    """Deterministic fast detector stub for unit-testing routes and canvas edits."""

    def __init__(self, count: int = 4) -> None:
        self.count = count

    def detect(self, image: np.ndarray) -> list[BoundingBox]:
        return [
            BoundingBox(
                label_id=i + 1,
                x1=10.0 * (i + 1),
                y1=10.0,
                x2=40.0 * (i + 1),
                y2=50.0,
                confidence=0.92,
                source="auto",
            )
            for i in range(self.count)
        ]

    @staticmethod
    def render_annotated_image(
        image_path: str | Path,
        boxes: list[BoundingBox],
        out_path: str | Path,
    ) -> str:
        img = read_image_unicode(image_path)
        return write_image_unicode(out_path, img)


@pytest.fixture()
def app_with_fake_cv(tmp_path: Path):
    db_file = tmp_path / "test_phase4.db"
    cfg = Config(
        SECRET_KEY="phase4-test-secret",
        DATABASE_URL=f"sqlite:///{db_file.as_posix()}",
        UPLOAD_DIR=tmp_path / "uploads",
        REPORT_DIR=tmp_path / "reports",
    )
    app = create_app(cfg, face_detector=FakeFaceDetector(count=4))
    app.config["TESTING"] = True
    yield app
    app.extensions["task_manager"].shutdown(wait=True)
    app.extensions["db_engine"].dispose()


def test_bounding_box_iou_and_nms_deduplication() -> None:
    """Suite 3: Verify overlapping tile seam boxes collapse to 1 via IoU NMS."""
    det = TiledSCRFDDetector(nms_iou_threshold=0.40)
    b1 = BoundingBox(label_id=0, x1=100, y1=100, x2=150, y2=150, confidence=0.95)
    b2 = BoundingBox(label_id=0, x1=104, y1=102, x2=152, y2=149, confidence=0.81)  # High overlap with b1
    b3 = BoundingBox(label_id=0, x1=300, y1=200, x2=340, y2=240, confidence=0.88)  # Distinct face

    assert b1.iou(b2) > 0.70
    assert b1.iou(b3) == 0.0

    merged = det._apply_nms([b2, b3, b1])
    assert len(merged) == 2
    assert merged[0].confidence == 0.95
    assert merged[0].label_id == 1
    assert merged[1].label_id == 2


def test_reconciler_verdicts() -> None:
    """Suite 3: Verify VERIFIED, DISCREPANCY_ALERT, and UNDER_SCANNED thresholds."""
    rec = AttendanceReconciler()

    v_ok = rec.reconcile(roster_total=35, qr_count=33, visual_count=34, tolerance=2)
    assert v_ok.status == "VERIFIED"
    assert v_ok.delta == -1

    v_proxy = rec.reconcile(roster_total=35, qr_count=39, visual_count=34, tolerance=2)
    assert v_proxy.status == "DISCREPANCY_ALERT"
    assert v_proxy.delta == 5

    v_under = rec.reconcile(roster_total=35, qr_count=28, visual_count=34, tolerance=2)
    assert v_under.status == "UNDER_SCANNED"
    assert v_under.delta == -6


def test_headcount_upload_and_interactive_box_patch_and_excel_export(
    app_with_fake_cv, tmp_path: Path
) -> None:
    """Suite 3: Upload classroom photo, patch manual +1 box, and verify .xlsx/.csv export."""
    client = app_with_fake_cv.test_client()
    qr = app_with_fake_cv.extensions["qr_service"]

    s_resp = client.post(
        "/api/sessions",
        json={"title": "CV Audit Demo", "session_code": "CV101", "headcount_tolerance": 1},
    ).get_json()
    sid = s_resp["id"]

    # Import 5 roster students and check in 5 via QR
    csv_roster = "\n".join(
        [f"Student {i},s{i}@ariel.ac.il" for i in range(1, 6)]
    )
    client.post(f"/api/sessions/{sid}/roster", json={"csv_text": csv_roster})

    b = qr.compute_bucket()
    sig = qr.sign_token("CV101", b)
    for i in range(1, 6):
        client.post(
            "/api/attendance/checkin",
            json={
                "session_code": "CV101",
                "t": b,
                "sig": sig,
                "full_name": f"Student {i}",
                "email": f"s{i}@ariel.ac.il",
                "device_id": f"dev-{i}",
            },
        )

    # Create a synthetic valid JPEG image and upload to /headcount
    synth_img = np.zeros((200, 300, 3), dtype=np.uint8)
    img_path = tmp_path / "synth_class.jpg"
    write_image_unicode(img_path, synth_img)

    with img_path.open("rb") as fh:
        hc_resp = client.post(
            f"/api/sessions/{sid}/headcount",
            data={"photo": (fh, "synth_class.jpg")},
            content_type="multipart/form-data",
        )
    assert hc_resp.status_code == 200
    hc_data = hc_resp.get_json()
    assert hc_data["auto_face_count"] == 4
    # 5 QR check-ins vs 4 auto faces with tolerance=1 -> delta=1 -> VERIFIED
    assert hc_data["verdict"]["status"] == "VERIFIED"

    # Lecturer clicks on canvas to add 1 occluded student -> 5 verified faces (delta=0)
    boxes = hc_data["boxes"]
    boxes.append(
        {
            "label_id": 5,
            "x1": 210,
            "y1": 80,
            "x2": 250,
            "y2": 120,
            "confidence": 1.0,
            "source": "manual_added",
        }
    )
    patch_resp = client.patch(
        f"/api/sessions/{sid}/headcount",
        json={"boxes": boxes, "lecturer_note": "Added 1 student behind laptop"},
    )
    assert patch_resp.status_code == 200
    patch_data = patch_resp.get_json()
    assert patch_data["verified_face_count"] == 5
    assert patch_data["verdict"]["delta"] == 0

    # Export and inspect .xlsx report
    xlsx_resp = client.get(f"/api/sessions/{sid}/report?format=xlsx")
    assert xlsx_resp.status_code == 200
    wb = load_workbook(io.BytesIO(xlsx_resp.data))
    assert "Audit Summary" in wb.sheetnames
    assert "Student Attendance" in wb.sheetnames
    ws_att = wb["Student Attendance"]
    assert ws_att.max_row == 6  # 1 header + 5 student rows

    # Export and inspect .csv report
    csv_resp = client.get(f"/api/sessions/{sid}/report?format=csv")
    assert csv_resp.status_code == 200
    csv_text = csv_resp.data.decode("utf-8-sig")
    assert "CV101" in csv_text
    assert "Student 1" in csv_text


def test_real_scrfd_detector_on_ariel_classroom_photo() -> None:
    """Suite 3 (Real-Life Integration): Run TiledSCRFDDetector on the Ariel classroom photo."""
    root_dir = Path(__file__).resolve().parent.parent
    sample_photo = root_dir / "data" / "samples" / "classroom_ariel.jpg"
    if not sample_photo.exists():
        pytest.skip("Sample classroom photo not present")

    img = read_image_unicode(sample_photo)
    detector = TiledSCRFDDetector()
    boxes = detector.detect(img)
    # Tiled SCRFD detects 34 faces on the Ariel lecture hall photo
    assert len(boxes) >= 30
    h, w = img.shape[:2]
    for box in boxes:
        assert 0.0 <= box.x1 < box.x2 <= float(w)
        assert 0.0 <= box.y1 < box.y2 <= float(h)
