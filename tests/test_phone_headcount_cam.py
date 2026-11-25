from __future__ import annotations

import io
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import cv2
import numpy as np
import pytest

from app import create_app
from app.config import Config
from app.services.face_counter import (
    BoundingBox,
    FaceDetector,
    read_image_unicode,
    write_image_unicode,
)


class StubDetector(FaceDetector):
    """Deterministic fast detector stub for testing phone camera headcount flow."""

    def __init__(self, count: int = 3) -> None:
        self.count = count

    def detect(self, image: np.ndarray) -> list[BoundingBox]:
        return [
            BoundingBox(
                label_id=i + 1,
                x1=20.0 + i * 35.0,
                y1=20.0,
                x2=50.0 + i * 35.0,
                y2=60.0,
                confidence=0.95,
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


def _make_jpeg_bytes(width: int = 160, height: int = 120) -> bytes:
    arr = np.full((height, width, 3), 210, dtype=np.uint8)
    ok, buf = cv2.imencode(".jpg", arr)
    assert ok
    return buf.tobytes()


def _register_lecturer_and_session(client, username: str, email: str, code: str) -> tuple[int, str]:
    reg = client.post(
        "/api/auth/lecturer/register",
        json={
            "full_name": f"Prof. {username}",
            "username": username,
            "email": email,
            "password": "StrongPassword123!",
            "confirm_password": "StrongPassword123!",
            "department": "Computer Science",
        },
    )
    assert reg.status_code in (200, 201), reg.get_data(as_text=True)

    c_resp = client.post(
        "/api/courses",
        json={
            "course_code": code,
            "name": f"Course {code}",
            "department": "Computer Science",
        },
    )
    assert c_resp.status_code in (200, 201), c_resp.get_data(as_text=True)
    course_id = c_resp.get_json()["id"]

    s_resp = client.post(
        f"/api/courses/{course_id}/sessions",
        json={"title": "Lecture Phone Test", "session_code": f"{code}L1"},
    )
    assert s_resp.status_code in (200, 201), s_resp.get_data(as_text=True)
    s_data = s_resp.get_json()
    return int(s_data["id"]), str(s_data["session_code"])


@pytest.fixture()
def phone_cam_app(tmp_path: Path):
    cfg = Config()
    cfg.TESTING = True
    cfg.DATABASE_URL = f"sqlite:///{tmp_path / 'phone_cam.db'}"
    cfg.UPLOAD_DIR = tmp_path / "uploads"
    cfg.REPORT_DIR = tmp_path / "reports"
    cfg.PUBLIC_BASE_URL = "https://demo-classpulse.trycloudflare.com"
    app = create_app(cfg, face_detector=StubDetector(count=3))
    yield app
    app.extensions["db_session"].close()
    app.extensions["task_manager"].shutdown(wait=True)
    app.extensions["db_engine"].dispose()


def test_phone_pairing_auto_connect_upload_and_stepper_adjust(phone_cam_app) -> None:
    """Full flow: laptop pairs QR -> phone connects (flips status) -> auto-uploads JPEG -> +1/-1 adjusts count."""
    laptop = phone_cam_app.test_client()
    phone = phone_cam_app.test_client()

    session_id, session_code = _register_lecturer_and_session(
        laptop, "prof_cam1", "cam1@ariel.ac.il", "CAM101"
    )

    # 1. Laptop generates phone camera pairing QR
    pair_resp = laptop.post(f"/api/sessions/{session_id}/headcount-cam/pair", json={})
    assert pair_resp.status_code == 200
    pair_data = pair_resp.get_json()
    assert pair_data["nonce"]
    assert pair_data["svg_data_uri"].startswith("data:image/svg+xml")
    assert pair_data["url"].startswith(
        f"https://demo-classpulse.trycloudflare.com/headcount-cam/{session_code}?ctok="
    )

    parsed_url = urlparse(pair_data["url"])
    ctok = parse_qs(parsed_url.query)["ctok"][0]
    nonce = pair_data["nonce"]

    # 2. Before phone opens page, pairing status is connected=False
    st_before = laptop.get(f"/api/sessions/{session_id}/headcount-cam/pair/{nonce}/status")
    assert st_before.status_code == 200
    assert st_before.get_json()["connected"] is False

    # 3. Phone opens the paired URL (unauthenticated browser, authorized purely by ctok)
    cam_page = phone.get(f"/headcount-cam/{session_code}?ctok={ctok}")
    assert cam_page.status_code == 200
    assert b"Take Classroom Photo" in cam_page.data
    assert b'capture="environment"' in cam_page.data
    assert b"/static/js/image_compress.js" in cam_page.data
    assert b"fetchWithTimeout" in cam_page.data
    phone_csrf = phone.get_cookie("cp_csrf")
    assert phone_csrf is not None
    phone_headers = {"X-Headcount-Token": ctok, "X-CSRF-Token": phone_csrf.value}

    # 4. Pairing status flips immediately to connected=True (drives desktop QR auto-close)
    st_after = laptop.get(f"/api/sessions/{session_id}/headcount-cam/pair/{nonce}/status")
    assert st_after.status_code == 200
    assert st_after.get_json()["connected"] is True
    assert st_after.get_json()["connected_at"] is not None

    # 5. Phone uploads captured JPEG automatically
    jpeg_bytes = _make_jpeg_bytes()
    up_resp = phone.post(
        f"/api/headcount-cam/{session_code}",
        headers=phone_headers,
        data={"photo": (io.BytesIO(jpeg_bytes), "classroom_phone.jpg")},
        content_type="multipart/form-data",
    )
    assert up_resp.status_code == 200, up_resp.get_data(as_text=True)
    up_data = up_resp.get_json()
    assert up_data["auto_count"] == 3
    assert up_data["verified_count"] == 3
    assert up_data["source"] == "phone"
    snap_id = up_data["snapshot_id"]

    # 6. Phone can stream the annotated preview JPEG via its scoped ctok
    img_resp = phone.get(f"/api/headcount-cam/{session_code}/image/{snap_id}?ctok={ctok}")
    assert img_resp.status_code == 200
    assert img_resp.mimetype == "image/jpeg"
    assert len(img_resp.data) > 100

    # 7. Professor glances at preview & taps +1 twice, then -1 once (3 -> 4 -> 5 -> 4)
    p1 = phone.patch(
        f"/api/headcount-cam/{session_code}",
        headers=phone_headers,
        json={"snapshot_id": snap_id, "delta": 1},
    )
    assert p1.status_code == 200
    assert p1.get_json()["verified_count"] == 4
    assert p1.get_json()["auto_count"] == 3

    p2 = phone.patch(
        f"/api/headcount-cam/{session_code}",
        headers=phone_headers,
        json={"snapshot_id": snap_id, "delta": 1},
    )
    assert p2.status_code == 200
    assert p2.get_json()["verified_count"] == 5

    m1 = phone.patch(
        f"/api/headcount-cam/{session_code}",
        headers=phone_headers,
        json={"snapshot_id": snap_id, "delta": -1},
    )
    assert m1.status_code == 200
    assert m1.get_json()["verified_count"] == 4

    # 8. Desktop overview reflects the phone snapshot and verified_face_count=4
    ov = laptop.get(f"/api/sessions/{session_id}/overview").get_json()
    assert ov["snapshot"]["id"] == snap_id
    assert ov["snapshot"]["auto_face_count"] == 3
    assert ov["snapshot"]["verified_face_count"] == 4
    assert ov["verdict"]["visual_count"] == 4

    # 9. Floor-at-0 check and invalid delta rejection
    bad_delta = phone.patch(
        f"/api/headcount-cam/{session_code}",
        headers=phone_headers,
        json={"snapshot_id": snap_id, "delta": 5},
    )
    assert bad_delta.status_code == 400

    zero_set = phone.patch(
        f"/api/headcount-cam/{session_code}",
        headers=phone_headers,
        json={"snapshot_id": snap_id, "verified_face_count": 0},
    )
    assert zero_set.status_code == 200
    assert zero_set.get_json()["verified_count"] == 0

    floor_check = phone.patch(
        f"/api/headcount-cam/{session_code}",
        headers=phone_headers,
        json={"snapshot_id": snap_id, "delta": -1},
    )
    assert floor_check.status_code == 200
    assert floor_check.get_json()["verified_count"] == 0


def test_phone_cam_security_isolation_revocation_and_validation(phone_cam_app) -> None:
    """Verify cross-lecturer isolation, tampered/revoked ctok rejection, invalid uploads, and CSRF."""
    prof_a = phone_cam_app.test_client()
    prof_b = phone_cam_app.test_client()
    anon = phone_cam_app.test_client()

    sid_a, code_a = _register_lecturer_and_session(
        prof_a, "prof_iso_a", "iso_a@ariel.ac.il", "ISOA1"
    )
    sid_b, code_b = _register_lecturer_and_session(
        prof_b, "prof_iso_b", "iso_b@ariel.ac.il", "ISOB1"
    )

    # Cross-lecturer & anonymous cannot create pairing QR for Prof A's lecture
    assert anon.post(f"/api/sessions/{sid_a}/headcount-cam/pair", json={}).status_code == 401
    assert prof_b.post(f"/api/sessions/{sid_a}/headcount-cam/pair", json={}).status_code == 403

    # Generate pairing for Session A
    pair_a = prof_a.post(f"/api/sessions/{sid_a}/headcount-cam/pair", json={}).get_json()
    ctok_a = parse_qs(urlparse(pair_a["url"]).query)["ctok"][0]
    nonce_a = pair_a["nonce"]

    # Prof A's ctok cannot access Session B
    assert anon.get(f"/headcount-cam/{code_b}?ctok={ctok_a}").status_code == 403
    assert (
        anon.post(
            f"/api/headcount-cam/{code_b}",
            headers={"X-Headcount-Token": ctok_a},
            data={"photo": (io.BytesIO(_make_jpeg_bytes()), "bad.jpg")},
            content_type="multipart/form-data",
        ).status_code
        == 403
    )

    # Invalid magic bytes rejected with 400 (with valid CSRF header)
    anon_csrf = anon.get_cookie("cp_csrf")
    bad_headers = {"X-Headcount-Token": ctok_a}
    if anon_csrf is not None:
        bad_headers["X-CSRF-Token"] = anon_csrf.value
    bad_file = anon.post(
        f"/api/headcount-cam/{code_a}",
        headers=bad_headers,
        data={"photo": (io.BytesIO(b"not-a-real-image-file"), "evil.jpg")},
        content_type="multipart/form-data",
    )
    assert bad_file.status_code == 400

    # Rotating QR revokes previous nonce immediately
    pair_a2 = prof_a.post(
        f"/api/sessions/{sid_a}/headcount-cam/pair",
        json={"revoke_nonce": nonce_a},
    ).get_json()
    ctok_a2 = parse_qs(urlparse(pair_a2["url"]).query)["ctok"][0]
    assert anon.get(f"/headcount-cam/{code_a}?ctok={ctok_a}").status_code == 403
    assert anon.get(f"/headcount-cam/{code_a}?ctok={ctok_a2}").status_code == 200

    # Verify CSRF protection works properly on phone endpoints when enforced
    phone_cam_app.config["TEST_ENFORCE_CSRF"] = True
    try:
        csrf_phone = phone_cam_app.test_client()
        page = csrf_phone.get(f"/headcount-cam/{code_a}?ctok={ctok_a2}")
        assert page.status_code == 200
        csrf_cookie = csrf_phone.get_cookie("cp_csrf")
        assert csrf_cookie is not None

        # Without X-CSRF-Token -> 403
        blocked = csrf_phone.post(
            f"/api/headcount-cam/{code_a}",
            headers={"X-Headcount-Token": ctok_a2},
            data={"photo": (io.BytesIO(_make_jpeg_bytes()), "classroom.jpg")},
            content_type="multipart/form-data",
        )
        assert blocked.status_code == 403

        # With X-CSRF-Token -> 200
        allowed = csrf_phone.post(
            f"/api/headcount-cam/{code_a}",
            headers={
                "X-Headcount-Token": ctok_a2,
                "X-CSRF-Token": csrf_cookie.value,
            },
            data={"photo": (io.BytesIO(_make_jpeg_bytes()), "classroom.jpg")},
            content_type="multipart/form-data",
        )
        assert allowed.status_code == 200
    finally:
        phone_cam_app.config["TEST_ENFORCE_CSRF"] = False
