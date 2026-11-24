from __future__ import annotations

import io
from pathlib import Path

import cv2
import numpy as np
import pytest

from app import create_app, socketio
from app.config import Config
from app.models import ClassSession, Course, Student
from app.services.face_counter import BoundingBox, FaceDetector, TiledSCRFDDetector
from app.services.qr_service import RollingQRService


class StubDetector(FaceDetector):
    def detect(self, image) -> list[BoundingBox]:
        return []


@pytest.fixture()
def overhaul_app(tmp_path: Path):
    db_file = tmp_path / "test_overhaul.db"
    cfg = Config(
        SECRET_KEY="overhaul-isolation-and-ux-secret-key",
        DATABASE_URL=f"sqlite:///{db_file.as_posix()}",
        GEMINI_API_KEY="",
    )
    app = create_app(cfg, face_detector=StubDetector())
    app.config["TESTING"] = True
    yield app
    app.extensions["task_manager"].shutdown(wait=True)
    app.extensions["db_engine"].dispose()


def _reg_lecturer(client, full_name: str, email: str, password: str = "Password1!"):
    resp = client.post(
        "/api/auth/lecturer/register",
        json={"full_name": full_name, "email": email, "password": password},
    )
    assert resp.status_code == 201, resp.get_data(as_text=True)
    return resp.get_json()["lecturer"]


def test_complete_multi_tenant_isolation_and_assignment_numbering(overhaul_app) -> None:
    """Lecturers and enrolled students have strictly isolated assignments, submissions, summaries, polls, and Q&A."""
    prof_a = overhaul_app.test_client()
    prof_b = overhaul_app.test_client()
    stu_a = overhaul_app.test_client()
    stu_b = overhaul_app.test_client()

    # Streamlined registration WITHOUT explicit username/department works
    lec_a = _reg_lecturer(prof_a, "Prof. Alice Owner", "alice.owner@ariel.ac.il")
    lec_b = _reg_lecturer(prof_b, "Prof. Bob Other", "bob.other@ariel.ac.il")
    assert lec_a["username"]
    assert lec_b["username"] != lec_a["username"]

    c_a = prof_a.post(
        "/api/courses",
        json={"course_code": "A101", "name": "Course Alpha", "initial_lecture_title": "Alpha L1"},
    ).get_json()
    c_b = prof_b.post(
        "/api/courses",
        json={"course_code": "B202", "name": "Course Beta", "initial_lecture_title": "Beta L1"},
    ).get_json()

    cid_a, sid_a = c_a["id"], c_a["sessions"][0]["id"]
    cid_b, sid_b = c_b["id"], c_b["sessions"][0]["id"]

    # Sequential assignment_number per course + course_name returned cleanly
    a1 = prof_a.post(
        f"/api/courses/{cid_a}/assignments",
        json={"title": "Linked Lists", "rubric_text": "Implement singly linked list"},
    ).get_json()
    a2 = prof_a.post(
        f"/api/courses/{cid_a}/assignments",
        json={"title": "Binary Trees", "rubric_text": "Implement BST traversal"},
    ).get_json()
    b1 = prof_b.post(
        f"/api/courses/{cid_b}/assignments",
        json={"title": "Graph Theory", "rubric_text": "Implement BFS and DFS"},
    ).get_json()
    assert a1["assignment_number"] == 1
    assert a1["course_name"] == "Course Alpha"
    assert a2["assignment_number"] == 2
    assert b1["assignment_number"] == 1
    assert b1["course_name"] == "Course Beta"

    # Student A signs in remotely (Full Name + ID Number only, no email, no QR) & enrolls only in Course A
    assert (
        stu_a.post(
            "/api/student/login",
            json={"full_name": "Student Alpha", "student_id_number": "211111111"},
        ).status_code
        == 200
    )
    assert stu_a.post("/api/student/enroll", json={"course_id": cid_a}).status_code == 200

    # Student B signs in & enrolls only in Course B
    assert (
        stu_b.post(
            "/api/student/login",
            json={"full_name": "Student Beta", "student_id_number": "222222222"},
        ).status_code
        == 200
    )
    assert stu_b.post("/api/student/enroll", json={"course_id": cid_b}).status_code == 200

    # Student A submits to Assignment 1 of Course A
    sub_a = stu_a.post(
        f"/api/assignments/{a1['id']}/submissions",
        json={"content_text": "class Node:\n    pass\n"},
    )
    assert sub_a.status_code == 201

    # Lecturer B is blocked (403) from Course A assignments, submissions, summaries, polls, and Q&A
    assert prof_b.get(f"/api/courses/{cid_a}/assignments?include_submissions=1").status_code == 403
    assert prof_b.get(f"/api/assignments/{a1['id']}").status_code == 403
    assert prof_b.get(f"/api/courses/{cid_a}/lectures").status_code == 403
    assert prof_b.get(f"/api/sessions/{sid_a}/lectures").status_code == 403
    assert prof_b.get(f"/api/sessions/{sid_a}/polls").status_code == 403
    assert prof_b.get(f"/api/sessions/{sid_a}/chat").status_code == 403
    assert prof_b.get(f"/api/sessions/{sid_a}/overview").status_code == 403

    # Enrolled Student A NEVER sees peer submissions even if passing ?include_submissions=1
    a_list = stu_a.get(f"/api/courses/{cid_a}/assignments?include_submissions=1").get_json()
    assert len(a_list["assignments"]) == 2
    assert "submissions" not in a_list["assignments"][0]
    a_detail = stu_a.get(f"/api/assignments/{a1['id']}").get_json()
    assert "submissions" not in a_detail
    assert a_detail["my_submission"] is not None

    # Unenrolled Student B CANNOT read Course A assignments, lectures, or submit to Course A assignment
    assert stu_b.get(f"/api/courses/{cid_a}/assignments").status_code == 403
    assert stu_b.get(f"/api/assignments/{a1['id']}").status_code == 403
    assert stu_b.get(f"/api/courses/{cid_a}/lectures").status_code == 403
    assert (
        stu_b.post(
            f"/api/assignments/{a1['id']}/submissions",
            json={"content_text": "unauthorized submission"},
        ).status_code
        == 403
    )

    # Dashboard catalog (all_courses) leaks ZERO assignments/summaries/sessions for unenrolled Course A
    dash_b = stu_b.get("/api/student/me").get_json()
    course_a_entry = next(c for c in dash_b["all_courses"] if c["id"] == cid_a)
    assert course_a_entry["is_enrolled"] is False
    assert course_a_entry["assignments"] == []
    assert course_a_entry["lecture_summaries"] == []
    assert course_a_entry["sessions"] == []


def test_remote_student_id_only_login_offline_q_and_qr_id_only_checkin(overhaul_app) -> None:
    """Students can log in from home (Name + ID only, no QR, no attendance) AND check in via QR (Name + ID only)."""
    prof = overhaul_app.test_client()
    remote_stu = overhaul_app.test_client()
    qr_stu = overhaul_app.test_client()
    qr: RollingQRService = overhaul_app.extensions["qr_service"]

    _reg_lecturer(prof, "Prof. Remote", "remote.prof@ariel.ac.il")
    course = prof.post(
        "/api/courses",
        json={"course_code": "RMT101", "name": "Remote Access Course", "initial_lecture_title": "Lecture 1"},
    ).get_json()
    cid = course["id"]
    sid = course["sessions"][0]["id"]
    scode = course["sessions"][0]["session_code"]

    # 1. Remote login using Full Name + ID Number ONLY (no email, no course_code, no QR)
    login_resp = remote_stu.post(
        "/api/student/login",
        json={"full_name": "Noam Israeli", "student_id_number": "305556667"},
    )
    assert login_resp.status_code == 200
    me = login_resp.get_json()
    assert me["authenticated"] is True
    assert me["student"]["full_name"] == "Noam Israeli"
    assert me["student"]["student_id_number"] == "305556667"
    assert me["student"]["email"] == ""  # synthetic placeholder hidden cleanly

    # Enroll via course_id without triggers attendance
    enr = remote_stu.post("/api/student/enroll", json={"course_id": cid})
    assert enr.status_code == 200
    my_course = enr.get_json()["dashboard"]["my_courses"][0]
    assert my_course["attended_count"] == 0

    # Post an Offline Question remotely
    oq = remote_stu.post(
        f"/api/sessions/{sid}/chat",
        json={"category": "Offline Question", "body": "Can we review recursion complexity?"},
    )
    assert oq.status_code == 201
    assert oq.get_json()["category"] == "Offline Question"

    # 2. Check in via QR using Name + ID Number ONLY (no email field on form)
    b = qr.compute_bucket()
    sig = qr.sign_token(scode, b)
    checkin_form_page = qr_stu.get(f"/s/{scode}?t={b}&sig={sig}").data.decode("utf-8")
    assert 'name="full_name"' in checkin_form_page
    assert 'name="student_id_number"' in checkin_form_page
    assert 'name="email"' not in checkin_form_page

    checkin_res = qr_stu.post(
        "/api/attendance/checkin",
        json={
            "session_code": scode,
            "t": b,
            "sig": sig,
            "full_name": "Yael Cohen",
            "student_id_number": "312223334",
            "device_id": "yael-iphone",
        },
    )
    assert checkin_res.status_code == 200
    assert checkin_res.get_json()["full_name"] == "Yael Cohen"
    assert checkin_res.get_json()["student_id_number"] == "312223334"


def test_lecturer_summary_upload_blocks_empty_and_uploads_markdown(overhaul_app) -> None:
    """Clicking Upload Summary without a file/notes returns 400 cleanly; valid .md upload publishes & emits notification."""
    prof = overhaul_app.test_client()
    stu = overhaul_app.test_client()
    _reg_lecturer(prof, "Prof. Summary", "sum.prof@ariel.ac.il")
    course = prof.post(
        "/api/courses",
        json={"course_code": "SUM101", "name": "Summary Course", "initial_lecture_title": "Lecture 1 — Intro"},
    ).get_json()
    cid = course["id"]
    sid = course["sessions"][0]["id"]

    # Empty upload MUST fail with 400 (never silently generate sample or corrupt download)
    empty_resp = prof.post(f"/api/sessions/{sid}/lectures/summarize", json={})
    assert empty_resp.status_code == 400
    assert "select" in empty_resp.get_json()["error"].lower()

    # Empty file MUST also fail with 400
    empty_file_resp = prof.post(
        f"/api/sessions/{sid}/lectures/summarize",
        data={"media": (io.BytesIO(b""), "empty.md")},
        content_type="multipart/form-data",
    )
    assert empty_file_resp.status_code == 400

    # Connect enrolled student Socket.IO client before upload -> verifies hierarchical course_notify broadcast
    assert (
        stu.post(
            "/api/student/login",
            json={"full_name": "Stu Summary", "student_id_number": "399888777"},
        ).status_code
        == 200
    )
    assert stu.post("/api/student/enroll", json={"course_id": cid}).status_code == 200
    sio = socketio.test_client(overhaul_app, flask_test_client=stu)
    sio.emit("join_notifications", {})
    sio.get_received()

    md_bytes = b"### Lecture 1 Notes\n- Abstraction separates interface from implementation.\n"
    up_resp = prof.post(
        f"/api/sessions/{sid}/lectures/summarize",
        data={"media": (io.BytesIO(md_bytes), "lecture1_summary.md")},
        content_type="multipart/form-data",
    )
    assert up_resp.status_code == 201
    art = up_resp.get_json()
    assert art["status"] == "COMPLETED"
    assert art["course_id"] == cid
    assert art["course_name"] == "Summary Course"
    assert "Abstraction separates interface" in art["summary_markdown"]

    events = [e for e in sio.get_received() if e["name"] == "lecture:summary_ready"]
    assert len(events) >= 1
    assert events[0]["args"][0]["course_id"] == cid
    sio.disconnect()

    dl = stu.get(f"/api/lectures/{art['id']}/download/summary.md")
    assert dl.status_code == 200
    assert b"Abstraction separates interface" in dl.data


def test_real_scrfd_closeup_single_face_detection() -> None:
    """Verify SCRFD detects faces accurately even in a tight single-person crop without hanging."""
    sample_path = Path("data/samples/classroom_ariel.jpg")
    if not sample_path.exists():
        pytest.skip("Sample photo not present")
    img = cv2.imread(str(sample_path))
    detector = TiledSCRFDDetector()
    full_boxes = detector.detect(img)
    assert len(full_boxes) >= 3

    # Tightly crop around a single person + shoulders so face fills ~55% of frame (close-up selfie case)
    b = max(full_boxes, key=lambda x: (x.x2 - x.x1) * (x.y2 - x.y1))
    bw = max(10.0, b.x2 - b.x1)
    bh = max(10.0, b.y2 - b.y1)
    x1 = max(0, int(b.x1 - 0.35 * bw))
    y1 = max(0, int(b.y1 - 0.35 * bh))
    x2 = min(img.shape[1], int(b.x2 + 0.35 * bw))
    y2 = min(img.shape[0], int(b.y2 + 0.35 * bh))
    closeup = img[y1:y2, x1:x2]
    closeup_boxes = detector.detect(closeup)
    assert len(closeup_boxes) >= 1


def test_enrollment_request_lecturer_roster_approval_and_rejection(overhaul_app) -> None:
    """Student Request to Join creates pending enrollment; lecturer Roster Approve unlocks access, Reject blocks."""
    prof = overhaul_app.test_client()
    stu_a = overhaul_app.test_client()
    stu_b = overhaul_app.test_client()

    _reg_lecturer(prof, "Prof. Roster Gate", "roster.gate@ariel.ac.il")
    course = prof.post(
        "/api/courses",
        json={"course_code": "RST301", "name": "Distributed Systems", "initial_lecture_title": "Lecture 1"},
    ).get_json()
    cid = course["id"]
    prof.post(f"/api/courses/{cid}/assignments", json={"title": "Consensus Lab", "rubric_text": ""})

    assert stu_a.post("/api/student/login", json={"full_name": "Approved Stu", "student_id_number": "311222333"}).status_code == 200
    assert stu_b.post("/api/student/login", json={"full_name": "Rejected Stu", "student_id_number": "344555666"}).status_code == 200

    # 1. Both students request to join
    req_a = stu_a.post("/api/student/enroll-request", json={"course_id": cid})
    req_b = stu_b.post("/api/student/enroll-request", json={"course_id": cid})
    assert req_a.status_code == 202 and req_a.get_json()["status"] == "pending"
    assert req_b.status_code == 202 and req_b.get_json()["status"] == "pending"

    # Pending requests show up in Lecturer Roster API, while students remain blocked (403) until approved
    pending = prof.get(f"/api/courses/{cid}/enrollment-requests").get_json()["pending_requests"]
    assert len(pending) == 2
    assert stu_a.get(f"/api/courses/{cid}/assignments").status_code == 403
    assert stu_b.get(f"/api/courses/{cid}/assignments").status_code == 403

    stu_a_id = stu_a.get("/api/student/me").get_json()["student"]["id"]
    stu_b_id = stu_b.get("/api/student/me").get_json()["student"]["id"]

    # 2. Lecturer approves Stu A -> immediate 200 course access + appears in enrolled list
    app_res = prof.post(f"/api/courses/{cid}/enrollment-requests/{stu_a_id}/approve")
    assert app_res.status_code == 200
    assert app_res.get_json()["status"] == "approved"
    assert stu_a.get(f"/api/courses/{cid}/assignments").status_code == 200
    dash_a = stu_a.get("/api/student/me").get_json()
    assert len(dash_a["my_courses"]) == 1 and dash_a["my_courses"][0]["id"] == cid

    # 3. Lecturer rejects Stu B -> still 403 and catalog shows rejected
    rej_res = prof.post(f"/api/courses/{cid}/enrollment-requests/{stu_b_id}/reject")
    assert rej_res.status_code == 200
    assert rej_res.get_json()["status"] == "rejected"
    assert stu_b.get(f"/api/courses/{cid}/assignments").status_code == 403
    dash_b = stu_b.get("/api/student/me").get_json()
    cat_b = next(c for c in dash_b["all_courses"] if c["id"] == cid)
    assert cat_b["enrollment_status"] == "rejected"


def test_cross_lecture_chat_switch_lecture_and_blank_rubric(overhaul_app) -> None:
    """Course-wide Q&A endpoint, Switch Lecture query param, and clean blank rubric assignment creation work."""
    prof_a = overhaul_app.test_client()
    prof_b = overhaul_app.test_client()
    stu = overhaul_app.test_client()

    _reg_lecturer(prof_a, "Prof. Switch", "switch.prof@ariel.ac.il")
    _reg_lecturer(prof_b, "Prof. Outsider", "outsider.prof@ariel.ac.il")

    course = prof_a.post(
        "/api/courses",
        json={"course_code": "SW101", "name": "Switch Lecture Course", "initial_lecture_title": "Lecture 1"},
    ).get_json()
    cid = course["id"]
    sess1 = course["sessions"][0]
    sess2 = prof_a.post(f"/api/courses/{cid}/sessions", json={"title": "Lecture 2 — Switching"}).get_json()

    # Blank rubric assignment succeeds without injecting legacy OOP modularity text
    asgn = prof_a.post(
        f"/api/courses/{cid}/assignments",
        json={"title": "Clean Lab 1", "rubric_text": ""},
    )
    assert asgn.status_code == 201
    assert "OOP modularity" not in (asgn.get_json().get("rubric_text") or "")

    # Student posts note in Lecture 1 while lecturer views Lecture 2 -> GET /api/courses/<id>/chat surfaces it
    assert stu.post("/api/student/login", json={"full_name": "Chat Stu", "student_id_number": "377888999"}).status_code == 200
    assert stu.post("/api/student/enroll", json={"course_id": cid}).status_code == 200
    assert stu.post(f"/api/sessions/{sess1['id']}/chat", json={"category": "Question", "body": "Cross-lecture question visible!"}).status_code == 201

    course_msgs = prof_a.get(f"/api/courses/{cid}/chat")
    assert course_msgs.status_code == 200
    bodies = [m["body"] for m in course_msgs.get_json()["messages"]]
    assert "Cross-lecture question visible!" in bodies
    assert prof_b.get(f"/api/courses/{cid}/chat").status_code == 403

    # /teacher?course_id=...&session_id=... and ?session=... both switch active lecture cleanly
    html_by_code = prof_a.get(f"/teacher?course_id={cid}&session={sess1['session_code']}").get_data(as_text=True)
    assert f'SESSION_CODE = "{sess1["session_code"]}"' in html_by_code
    html_by_id = prof_a.get(f"/teacher?course_id={cid}&session_id={sess2['id']}").get_data(as_text=True)
    assert f'SESSION_CODE = "{sess2["session_code"]}"' in html_by_id


def test_phone_headcount_png_csrf_and_real_count(tmp_path: Path) -> None:
    """Phone camera PNG upload with CSRF header & valid ctok normalizes to JPEG and runs real SCRFD."""
    sample_path = Path("data/samples/classroom_ariel.jpg")
    if not sample_path.exists():
        pytest.skip("Sample photo not present")

    db_file = tmp_path / "csrf_cam.db"
    cfg = Config(
        SECRET_KEY="phone-cam-csrf-real-scrfd-secret",
        DATABASE_URL=f"sqlite:///{db_file.as_posix()}",
        UPLOAD_DIR=tmp_path / "uploads",
        GEMINI_API_KEY="",
        ENABLE_CSRF=True,
    )
    app = create_app(cfg)
    app.config["TESTING"] = True
    app.config["TEST_ENFORCE_CSRF"] = True
    try:
        prof = app.test_client()
        phone = app.test_client()
        _reg_lecturer(prof, "Prof. Cam", "cam.prof@ariel.ac.il")
        course = prof.post(
            "/api/courses",
            json={"course_code": "CAM101", "name": "Camera Course", "initial_lecture_title": "Lecture 1"},
        ).get_json()
        sid = course["sessions"][0]["id"]
        scode = course["sessions"][0]["session_code"]

        pair_res = prof.post(f"/api/sessions/{sid}/headcount-cam/pair", json={}).get_json()
        ctok = pair_res["url"].split("ctok=")[1]

        # Seed a browser CSRF cookie on phone and send X-CSRF-Token as csrf.js does
        assert phone.get(f"/headcount-cam/{scode}?ctok={ctok}").status_code == 200
        csrf_cookie = phone.get_cookie("cp_csrf")
        assert csrf_cookie is not None
        img = cv2.imread(str(sample_path))
        ok, png_buf = cv2.imencode(".png", img)
        assert ok

        up = phone.post(
            f"/api/headcount-cam/{scode}",
            headers={"X-Headcount-Token": ctok, "X-CSRF-Token": csrf_cookie.value},
            data={"photo": (io.BytesIO(png_buf.tobytes()), "classroom.png"), "ctok": ctok},
            content_type="multipart/form-data",
        )
        assert up.status_code == 200, up.get_data(as_text=True)
        payload = up.get_json()
        assert payload["auto_count"] >= 5
    finally:
        app.extensions["task_manager"].shutdown(wait=True)
        app.extensions["db_engine"].dispose()
