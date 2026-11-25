from __future__ import annotations

from datetime import datetime, timedelta, timezone
import io
from pathlib import Path

import cv2
import numpy as np
from flask import Flask

from app import socketio
from app.models import HeadcountSnapshot
from app.services.reconciler import AttendanceReconciler
from tests.conftest import login_student, register_lecturer


def _make_jpeg_bytes(width: int = 120, height: int = 90) -> bytes:
    arr = np.full((height, width, 3), 200, dtype=np.uint8)
    ok, buf = cv2.imencode(".jpg", arr)
    assert ok
    return buf.tobytes()


def test_live_qr_endpoint_late_checkin_closed_session_and_expired_token(app: Flask) -> None:
    """Cover /api/sessions/<code>/qr, LATE status past late_after_minutes, closed session 403, and expired QR 410."""
    prof = app.test_client()
    stu_on_time = app.test_client()
    stu_late = app.test_client()
    qr = app.extensions["qr_service"]
    db = app.extensions["db_session"]

    register_lecturer(prof, "Prof. QR Timing", "qr_timing@ariel.ac.il")
    course = prof.post(
        "/api/courses",
        json={"course_code": "QRT101", "name": "Timing Course", "initial_lecture_title": "Lecture 1"},
    ).get_json()
    cid = course["id"]
    sid = course["sessions"][0]["id"]
    scode = course["sessions"][0]["session_code"]

    # Unknown session code -> 404
    assert prof.get("/api/sessions/UNKNOWN99/qr").status_code == 404

    # Live QR payload endpoint returns valid bucket, signature & attendance_open flag
    qr_res = prof.get(f"/api/sessions/{scode}/qr")
    assert qr_res.status_code == 200
    qr_data = qr_res.get_json()
    assert qr_data["session_code"] == scode
    assert qr_data["attendance_open"] is True
    assert qr_data["sig"] and qr_data["bucket"] is not None

    # Expired / tampered QR token rejected (410 / 403)
    bad_tok = stu_on_time.post(
        "/api/attendance/checkin",
        json={
            "session_code": scode,
            "t": 1,
            "sig": "tampered",
            "full_name": "Bad Token",
            "student_id_number": "301000111",
        },
    )
    assert bad_tok.status_code in (401, 403, 410)

    # Fresh session -> student marked PRESENT
    ok_ck = stu_on_time.post(
        "/api/attendance/checkin",
        json={
            "session_code": scode,
            "t": qr_data["bucket"],
            "sig": qr_data["sig"],
            "full_name": "On Time Stu",
            "student_id_number": "301000222",
            "device_id": "dev-ontime",
        },
    )
    assert ok_ck.status_code == 200
    assert ok_ck.get_json()["is_late"] is False

    # Backdate session.started_at by 30 minutes (late_after_minutes default is 15) -> LATE
    from app.models import ClassSession

    sess_row = db.get(ClassSession, sid)
    sess_row.started_at = datetime.now(timezone.utc) - timedelta(minutes=30)
    db.commit()

    b_now = qr.compute_bucket()
    sig_now = qr.sign_token(scode, b_now)
    late_ck = stu_late.post(
        "/api/attendance/checkin",
        json={
            "session_code": scode,
            "t": b_now,
            "sig": sig_now,
            "full_name": "Late Stu",
            "student_id_number": "301000333",
            "device_id": "dev-late",
        },
    )
    assert late_ck.status_code == 200
    assert late_ck.get_json()["is_late"] is True

    # Closed attendance blocks new check-ins
    db.remove()
    sess_row = db.get(ClassSession, sid)
    sess_row.attendance_open = False
    db.commit()
    closed_ck = app.test_client().post(
        "/api/attendance/checkin",
        json={
            "session_code": scode,
            "t": b_now,
            "sig": sig_now,
            "full_name": "Too Late",
            "student_id_number": "301000444",
            "device_id": "dev-closed",
        },
    )
    assert closed_ck.status_code in (403, 409, 410)

    # Suppress unused variable warning for cid
    assert cid > 0


def test_headcount_image_endpoint_and_expired_photo_retention_purge(app: Flask) -> None:
    """Cover /headcount/image/<kind> RBAC/404 and /api/maintenance/purge-expired-photos deleting aged files."""
    prof_a = app.test_client()
    prof_b = app.test_client()
    db = app.extensions["db_session"]

    register_lecturer(prof_a, "Prof. Media Owner", "media_owner@ariel.ac.il")
    register_lecturer(prof_b, "Prof. Outsider", "media_outsider@ariel.ac.il")

    c_a = prof_a.post(
        "/api/courses",
        json={"course_code": "IMG101", "name": "Image Retention"},
    ).get_json()
    sid_a = c_a["sessions"][0]["id"]

    # Before any snapshot exists -> 404
    assert prof_a.get(f"/api/sessions/{sid_a}/headcount/image/raw").status_code == 404

    jpeg_bytes = _make_jpeg_bytes()
    r1 = prof_a.post(
        f"/api/sessions/{sid_a}/headcount",
        data={"photo": (io.BytesIO(jpeg_bytes), "snap1.jpg")},
        content_type="multipart/form-data",
    )
    assert r1.status_code == 200
    snap1_id = r1.get_json()["snapshot_id"]

    r2 = prof_a.post(
        f"/api/sessions/{sid_a}/headcount",
        data={"photo": (io.BytesIO(jpeg_bytes), "snap2.jpg")},
        content_type="multipart/form-data",
    )
    assert r2.status_code == 200
    snap2_id = r2.get_json()["snapshot_id"]

    # Image streaming endpoints work for owner & block Prof B (403)
    raw_resp = prof_a.get(f"/api/sessions/{sid_a}/headcount/image/raw?snap_id={snap1_id}")
    ann_resp = prof_a.get(f"/api/sessions/{sid_a}/headcount/image/annotated?snap_id={snap2_id}")
    assert raw_resp.status_code == 200 and raw_resp.mimetype == "image/jpeg"
    assert ann_resp.status_code == 200 and ann_resp.mimetype == "image/jpeg"
    assert prof_b.get(f"/api/sessions/{sid_a}/headcount/image/raw").status_code == 403

    # Backdate snap1 beyond 24h while keeping snap2 recent
    snap1 = db.get(HeadcountSnapshot, snap1_id)
    snap2 = db.get(HeadcountSnapshot, snap2_id)
    assert snap1 is not None and snap2 is not None
    snap1_raw = Path(snap1.raw_image_path)
    snap1_ann = Path(snap1.annotated_image_path)
    snap2_raw = Path(snap2.raw_image_path)
    assert snap1_raw.exists() and snap1_ann.exists() and snap2_raw.exists()

    snap1.captured_at = datetime.now(timezone.utc) - timedelta(hours=36)
    db.commit()

    # Unauthenticated caller blocked; Prof A purges expired photos only
    assert app.test_client().post("/api/maintenance/purge-expired-photos", json={"retention_hours": 24}).status_code == 401
    purge_res = prof_a.post("/api/maintenance/purge-expired-photos", json={"retention_hours": 24})
    assert purge_res.status_code == 200
    assert purge_res.get_json()["deleted_files"] == 2
    assert not snap1_raw.exists() and not snap1_ann.exists()
    assert snap2_raw.exists()


def test_headcount_corrupt_inputs_zero_faces_and_reconciler_boundaries(app: Flask) -> None:
    """Cover malformed base64/truncated JPEG headcount rejection, 0-face photos, and AttendanceReconciler boundaries."""
    prof = app.test_client()
    register_lecturer(prof, "Prof. CV Edges", "cv_edges@ariel.ac.il")
    sid = prof.post(
        "/api/courses", json={"course_code": "CVE101", "name": "CV Edges"}
    ).get_json()["sessions"][0]["id"]

    # Malformed base64 camera payload -> 400
    bad_b64 = prof.post(
        f"/api/sessions/{sid}/headcount",
        json={"image_data": "data:image/jpeg;base64,!!!not-valid-base64!!!", "source": "camera"},
    )
    assert bad_b64.status_code == 400

    # JPEG SOI magic bytes followed by corrupted garbage -> cv2/PIL decode fails -> 400
    corrupt_jpg = b"\xff\xd8\xff\xe0" + b"\x00" * 32 + b"CORRUPTED_DATA"
    bad_decode = prof.post(
        f"/api/sessions/{sid}/headcount",
        data={"photo": (io.BytesIO(corrupt_jpg), "corrupt.jpg")},
        content_type="multipart/form-data",
    )
    assert bad_decode.status_code == 400

    # Valid blank JPEG with StubDetector returns 0 faces cleanly without error
    valid_blank = _make_jpeg_bytes()
    zero_ok = prof.post(
        f"/api/sessions/{sid}/headcount",
        data={"photo": (io.BytesIO(valid_blank), "empty_room.jpg")},
        content_type="multipart/form-data",
    )
    assert zero_ok.status_code == 200
    assert zero_ok.get_json()["auto_face_count"] == 0

    # Negative verified_face_count clamps safely or rejects cleanly (never stores negative count)
    neg_patch = prof.patch(f"/api/sessions/{sid}/headcount", json={"verified_face_count": -5})
    assert neg_patch.status_code in (200, 400)
    if neg_patch.status_code == 200:
        assert neg_patch.get_json()["verified_face_count"] >= 0

    # AttendanceReconciler boundary checks
    rec = AttendanceReconciler()
    awaiting = rec.reconcile(roster_total=10, qr_count=2, visual_count=0, has_snapshot=False)
    assert awaiting.status == "AWAITING_PHOTO"

    exact_pos = rec.reconcile(roster_total=10, qr_count=12, visual_count=10, tolerance=2)
    assert exact_pos.status == "VERIFIED" and exact_pos.discrepancy_requires_approval is True

    exact_neg = rec.reconcile(roster_total=10, qr_count=8, visual_count=10, tolerance=2)
    assert exact_neg.status == "VERIFIED" and exact_neg.discrepancy_requires_approval is False

    approved = rec.reconcile(
        roster_total=10, qr_count=5, visual_count=0, tolerance=2, discrepancy_approved=True
    )
    assert approved.status == "DISCREPANCY_ALERT"
    assert approved.discrepancy_requires_approval is False
    assert approved.discrepancy_approved is True


def test_poll_explicit_close_vote_guards_and_cross_tenant_rbac(app: Flask) -> None:
    """Cover POST /api/polls/<id>/close + poll:closed event, DRAFT/CLOSED vote blocks, bad option_index, and cross-lecturer 403."""
    prof_a = app.test_client()
    prof_b = app.test_client()
    stu = app.test_client()

    register_lecturer(prof_a, "Prof. Polls A", "polls_a@ariel.ac.il")
    register_lecturer(prof_b, "Prof. Polls B", "polls_b@ariel.ac.il")
    course = prof_a.post("/api/courses", json={"course_code": "PL101", "name": "Polls QA"}).get_json()
    cid = course["id"]
    sid = course["sessions"][0]["id"]
    scode = course["sessions"][0]["session_code"]

    st_data = login_student(stu, "Poll Voter", "305111222")
    assert stu.post("/api/student/enroll", json={"course_id": cid}).status_code == 200

    # Create DRAFT poll
    draft_poll = prof_a.post(
        f"/api/sessions/{sid}/polls",
        json={"question": "Ready?", "options": ["Yes", "No"]},
    ).get_json()
    pid = draft_poll["id"]
    assert draft_poll["status"] == "DRAFT"

    # Voting on DRAFT poll fails (409)
    assert (
        stu.post(
            f"/api/polls/{pid}/vote", json={"student_id": st_data["id"], "option_index": 0}
        ).status_code
        == 409
    )

    # Lecturer B cannot launch or close Lecturer A's poll (403)
    assert prof_b.post(f"/api/polls/{pid}/launch").status_code == 403
    assert prof_b.post(f"/api/polls/{pid}/close").status_code == 403

    # Launch -> out-of-range option_index rejected (400)
    assert prof_a.post(f"/api/polls/{pid}/launch").status_code == 200
    assert (
        stu.post(
            f"/api/polls/{pid}/vote", json={"student_id": st_data["id"], "option_index": -1}
        ).status_code
        == 400
    )
    assert (
        stu.post(
            f"/api/polls/{pid}/vote", json={"student_id": st_data["id"], "option_index": 99}
        ).status_code
        == 400
    )

    # Close poll via endpoint and verify Socket.IO poll:closed emission + further voting blocked
    sio = socketio.test_client(app, flask_test_client=stu)
    sio.emit("join_session", {"session_code": scode, "student_id": st_data["id"], "as_student": True})
    sio.get_received()

    close_res = prof_a.post(f"/api/polls/{pid}/close")
    assert close_res.status_code == 200
    assert close_res.get_json()["status"] == "CLOSED"
    closed_evs = [e for e in sio.get_received() if e["name"] == "poll:closed"]
    assert len(closed_evs) == 1 and closed_evs[0]["args"][0]["id"] == pid
    sio.disconnect()

    assert (
        stu.post(
            f"/api/polls/{pid}/vote", json={"student_id": st_data["id"], "option_index": 0}
        ).status_code
        == 409
    )


def test_chat_pin_rbac_and_xss_payload_safety(app: Flask) -> None:
    """Cover /api/chat/<id>/pin RBAC, toggle off, XSS text preservation without error, and 404 session."""
    prof_a = app.test_client()
    prof_b = app.test_client()
    stu = app.test_client()

    register_lecturer(prof_a, "Prof. Chat A", "chat_a@ariel.ac.il")
    register_lecturer(prof_b, "Prof. Chat B", "chat_b@ariel.ac.il")
    c_a = prof_a.post("/api/courses", json={"course_code": "CH101", "name": "Chat Sec"}).get_json()
    sid = c_a["sessions"][0]["id"]

    login_student(stu, "Chat Student", "309888777")
    assert stu.post("/api/student/enroll", json={"course_id": c_a["id"]}).status_code == 200

    # Non-existent session -> 404
    assert prof_a.post("/api/sessions/99999/chat", json={"body": "hello"}).status_code == 404

    xss_body = '<script>alert("xss")</script> How does OOP work?'
    posted = stu.post(f"/api/sessions/{sid}/chat", json={"category": "Question", "body": xss_body})
    assert posted.status_code == 201
    mid = posted.get_json()["id"]
    assert posted.get_json()["body"] == xss_body

    # Student lacks lecturer session (401); Outsider Lecturer B is blocked (403)
    assert stu.post(f"/api/chat/{mid}/pin").status_code == 401
    assert prof_b.post(f"/api/chat/{mid}/pin").status_code == 403

    # Owner toggles pin True -> False
    p1 = prof_a.post(f"/api/chat/{mid}/pin")
    assert p1.status_code == 200 and p1.get_json()["is_pinned"] is True
    p2 = prof_a.post(f"/api/chat/{mid}/pin")
    assert p2.status_code == 200 and p2.get_json()["is_pinned"] is False
