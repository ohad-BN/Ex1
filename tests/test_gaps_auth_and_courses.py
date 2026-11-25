from __future__ import annotations

import io
from pathlib import Path

from flask import Flask
import numpy as np
import cv2

from app.services.identity_helpers import (
    is_synthetic_student_email,
    make_synthetic_student_email,
    normalize_student_id,
    validate_image_magic_bytes,
    validate_student_id_format,
)
from app.services.rate_limiter import SlidingWindowRateLimiter
from tests.conftest import login_student, register_lecturer


def test_lecturer_profile_patch_and_staff_listing_security(app: Flask) -> None:
    """Cover PATCH /api/auth/lecturer/me, blank-field preservation, 401 guard, and GET /api/courses/<id>/staff RBAC."""
    prof = app.test_client()
    ta = app.test_client()
    outsider = app.test_client()
    anon = app.test_client()

    # Unauthenticated PATCH -> 401
    assert anon.patch("/api/auth/lecturer/me", json={"full_name": "Hacker"}).status_code == 401

    register_lecturer(prof, "Prof. Original Name", "orig_prof@ariel.ac.il", username="orig.prof")
    register_lecturer(ta, "TA Member", "ta_mem@ariel.ac.il", username="ta.mem")
    register_lecturer(outsider, "Prof. Outsider", "out_prof@ariel.ac.il", username="out.prof")

    # Blank full_name preserves existing name cleanly (never stores empty name)
    blank_res = prof.patch("/api/auth/lecturer/me", json={"full_name": "   "})
    assert blank_res.status_code == 200
    assert blank_res.get_json()["lecturer"]["full_name"] == "Prof. Original Name"

    # Valid update persists on subsequent GET /api/auth/lecturer/me
    upd = prof.patch(
        "/api/auth/lecturer/me",
        json={"full_name": "Prof. Updated Name", "department": "Software Engineering"},
    )
    assert upd.status_code == 200
    assert upd.get_json()["lecturer"]["full_name"] == "Prof. Updated Name"
    assert upd.get_json()["lecturer"]["department"] == "Software Engineering"

    me = prof.get("/api/auth/lecturer/me").get_json()
    assert me["lecturer"]["full_name"] == "Prof. Updated Name"
    assert me["lecturer"]["department"] == "Software Engineering"

    # GET /api/courses/<id>/staff lists owner/TA and blocks outsider (403)
    cid = prof.post("/api/courses", json={"course_code": "STF202", "name": "Staff Listing"}).get_json()["id"]
    assert prof.post(f"/api/courses/{cid}/staff", json={"identifier": "ta.mem", "role": "ta"}).status_code == 201

    staff_res = prof.get(f"/api/courses/{cid}/staff")
    assert staff_res.status_code == 200
    staff_usernames = {m["username"] for m in staff_res.get_json()["staff"]}
    assert "ta.mem" in staff_usernames
    assert outsider.get(f"/api/courses/{cid}/staff").status_code == 403


def test_change_password_validation_and_session_invalidation(app: Flask) -> None:
    """Cover /api/auth/lecturer/change-password validation (unauth 401, weak pw 400, mismatch 400) & session rotation."""
    assert (
        app.test_client()
        .post("/api/auth/lecturer/change-password", json={"current_password": "x", "new_password": "NewPassword1!"})
        .status_code
        == 401
    )

    c1 = app.test_client()
    c2 = app.test_client()
    register_lecturer(c1, "Prof. PW", "pw_rot@ariel.ac.il", password="OldPassword1!")
    assert (
        c2.post("/api/auth/lecturer/login", json={"identifier": "pw_rot@ariel.ac.il", "password": "OldPassword1!"}).status_code
        == 200
    )

    assert (
        c1.post(
            "/api/auth/lecturer/change-password",
            json={"current_password": "OldPassword1!", "new_password": "short"},
        ).status_code
        == 400
    )
    assert (
        c1.post(
            "/api/auth/lecturer/change-password",
            json={
                "current_password": "OldPassword1!",
                "new_password": "NewPassword2!",
                "confirm_password": "MismatchPassword2!",
            },
        ).status_code
        == 400
    )

    ok = c1.post(
        "/api/auth/lecturer/change-password",
        json={
            "current_password": "OldPassword1!",
            "new_password": "NewPassword2!",
            "confirm_password": "NewPassword2!",
        },
    )
    assert ok.status_code == 200
    assert c1.get("/api/auth/lecturer/me").status_code == 200
    assert c2.get("/api/auth/lecturer/me").status_code == 401


def test_identity_helpers_and_rate_limiter_unit_boundaries() -> None:
    """Unit-test identity normalization, magic bytes, and SlidingWindowRateLimiter expiry/prefix resets."""
    assert normalize_student_id(" 208-123 456 ") == "208123456"
    assert normalize_student_id("ab-cd") == "ABCD"
    assert normalize_student_id("   ") is None
    assert validate_student_id_format("123") is False
    assert validate_student_id_format("1234") is True
    assert validate_student_id_format("208_123") is False

    synth = make_synthetic_student_email("208123456")
    assert is_synthetic_student_email(synth) is True
    assert is_synthetic_student_email("ohad@ariel.ac.il") is False

    # Magic bytes checks
    arr = np.zeros((20, 20, 3), dtype=np.uint8)
    _, jpg_buf = cv2.imencode(".jpg", arr)
    _, png_buf = cv2.imencode(".png", arr)
    assert validate_image_magic_bytes(jpg_buf.tobytes()) is True
    assert validate_image_magic_bytes(png_buf.tobytes()) is True
    assert validate_image_magic_bytes(b"GIF89a" + b"\x00" * 16) is True
    assert validate_image_magic_bytes(b"RIFF\x00\x00\x00\x00WEBP" + b"\x00" * 8) is True
    assert validate_image_magic_bytes(b"BM" + b"\x00" * 20) is True
    assert validate_image_magic_bytes(b"plain text file") is False
    assert validate_image_magic_bytes(b"short") is False

    # Rate limiter window expiry, independent keys, limit<=0 bypass, prefix reset
    rl = SlidingWindowRateLimiter()
    assert rl.allow("auth:ip1", limit=1, window_seconds=10.0, now_ts=100.0) == (True, 0)
    blocked, retry = rl.allow("auth:ip1", limit=1, window_seconds=10.0, now_ts=102.0)
    assert blocked is False and retry >= 1
    assert rl.allow("auth:ip2", limit=1, window_seconds=10.0, now_ts=102.0) == (True, 0)
    # After 10s window passes, auth:ip1 is allowed again
    assert rl.allow("auth:ip1", limit=1, window_seconds=10.0, now_ts=110.5) == (True, 0)
    assert rl.allow("bypass", limit=0, window_seconds=10.0, now_ts=100.0) == (True, 0)
    rl.allow("other:ip1", limit=1, window_seconds=10.0, now_ts=100.0)
    rl.reset(prefix="auth:")
    assert rl.allow("auth:ip2", limit=1, window_seconds=10.0, now_ts=103.0) == (True, 0)
    assert rl.allow("other:ip1", limit=1, window_seconds=10.0, now_ts=103.0)[0] is False


def test_student_remote_login_validation_and_logout_cleanup(app: Flask) -> None:
    """Cover /api/student/login negative inputs (blank name, short ID) and /api/student/logout session clear."""
    stu = app.test_client()

    # Blank full name for unregistered student -> 400
    assert stu.post("/api/student/login", json={"full_name": "   ", "student_id_number": "312111222"}).status_code == 400
    # 3-char ID -> 400
    assert stu.post("/api/student/login", json={"full_name": "Short ID", "student_id_number": "123"}).status_code == 400

    # Valid remote login -> logout -> /api/student/me shows authenticated=False
    login_student(stu, "Valid Student", "312111222")
    assert stu.get("/api/student/me").get_json()["authenticated"] is True
    assert stu.post("/api/student/logout").status_code == 200
    me_after = stu.get("/api/student/me").get_json()
    assert me_after["authenticated"] is False
    assert me_after["my_courses"] == []


def test_roster_malformed_and_deduplication_and_student_removal_revocation(app: Flask) -> None:
    """Cover corrupt .xlsx/empty CSV 400s, 2-col ID+Name CSV dedup, and immediate student access revocation on removal."""
    prof_a = app.test_client()
    prof_b = app.test_client()
    stu = app.test_client()

    register_lecturer(prof_a, "Prof. Roster A", "roster_a@ariel.ac.il")
    register_lecturer(prof_b, "Prof. Roster B", "roster_b@ariel.ac.il")
    course = prof_a.post("/api/courses", json={"course_code": "RST20", "name": "Roster Ops"}).get_json()
    cid = course["id"]
    prof_a.post(f"/api/courses/{cid}/assignments", json={"title": "Lab 1", "rubric_text": ""})

    # Empty CSV -> 400; Corrupt xlsx -> 400
    assert prof_a.post(f"/api/courses/{cid}/roster", json={"csv_text": "   "}).status_code == 400
    assert (
        prof_a.post(
            f"/api/courses/{cid}/roster",
            data={"file": (io.BytesIO(b"PK\x03\x04corrupt_xlsx"), "broken.xlsx")},
            content_type="multipart/form-data",
        ).status_code
        == 400
    )

    # 2-column (Student ID, Full Name) CSV with duplicate rows deduplicates cleanly
    csv_2col = "Student ID,Full Name\n319000111,Noa Rimon\n319000111,Noa Rimon\n319000222,Gal Shaked\n"
    imp = prof_a.post(f"/api/courses/{cid}/roster", json={"csv_text": csv_2col})
    assert imp.status_code == 200
    assert len(imp.get_json()["course"]["students"]) == 2

    # Sign in Noa Rimon -> sees assignments; outsider Prof B cannot remove Noa (403); Prof A removes Noa -> 403 immediately
    noa = login_student(stu, "Noa Rimon", "319000111")
    assert stu.get(f"/api/courses/{cid}/assignments").status_code == 200
    assert prof_b.delete(f"/api/courses/{cid}/students/{noa['id']}").status_code == 403
    assert prof_a.delete(f"/api/courses/{cid}/students/{noa['id']}").status_code == 200
    assert stu.get(f"/api/courses/{cid}/assignments").status_code == 403


def test_enrollment_request_edge_cases_and_cross_tenant_approval_block(app: Flask) -> None:
    """Cover idempotent repeat enroll-request, re-request after rejection, 404 bad student, and cross-lecturer approve/reject 403."""
    prof_a = app.test_client()
    prof_b = app.test_client()
    stu = app.test_client()

    register_lecturer(prof_a, "Prof. Gate A", "gate_a@ariel.ac.il")
    register_lecturer(prof_b, "Prof. Gate B", "gate_b@ariel.ac.il")
    cid = prof_a.post("/api/courses", json={"course_code": "GT101", "name": "Gate Edge"}).get_json()["id"]
    st = login_student(stu, "Gate Student", "321000888")

    # Duplicate pending requests don't create duplicate roster requests
    r1 = stu.post("/api/student/enroll-request", json={"course_id": cid})
    r2 = stu.post("/api/student/enroll-request", json={"course_id": cid})
    assert r1.status_code == 202 and r2.status_code == 202
    assert len(prof_a.get(f"/api/courses/{cid}/enrollment-requests").get_json()["pending_requests"]) == 1

    # Outsider Prof B cannot approve or reject (403)
    assert prof_b.post(f"/api/courses/{cid}/enrollment-requests/{st['id']}/approve").status_code == 403
    assert prof_b.post(f"/api/courses/{cid}/enrollment-requests/{st['id']}/reject").status_code == 403

    # Unknown student approve returns 404
    assert prof_a.post(f"/api/courses/{cid}/enrollment-requests/99999/approve").status_code == 404

    # Reject -> student can re-request and transitions back to pending -> approve works
    assert prof_a.post(f"/api/courses/{cid}/enrollment-requests/{st['id']}/reject").status_code == 200
    r3 = stu.post("/api/student/enroll-request", json={"course_id": cid})
    assert r3.status_code == 202 and r3.get_json()["status"] == "pending"
    assert prof_a.post(f"/api/courses/{cid}/enrollment-requests/{st['id']}/approve").status_code == 200
    assert stu.get("/api/student/me").get_json()["my_courses"][0]["id"] == cid
