from __future__ import annotations

import base64
import io
import json
import shutil
import subprocess
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

    md_bytes = b"### Executive Summary\n- Abstraction separates interface from implementation.\n\n### Key Topics\n- Strategy pattern vs inheritance.\n"
    up_resp = prof.post(
        f"/api/sessions/{sid}/lectures/summarize",
        data={"media": (io.BytesIO(md_bytes), "lecture1_summary.md")},
        content_type="multipart/form-data",
    )
    assert up_resp.status_code == 201
    art = up_resp.get_json()
    assert art["status"] == "COMPLETED"
    assert art["is_published"] is False
    assert art["course_id"] == cid
    assert art["course_name"] == "Summary Course"
    assert "Abstraction separates interface" in art["summary_markdown"]

    # Draft is hidden from students until Lecturer explicitly clicks Publish to Students
    pre_pub = [e for e in sio.get_received() if e["name"] == "lecture:summary_ready"]
    assert pre_pub == []
    assert stu.get(f"/api/courses/{cid}/lectures").get_json()["artifacts"] == []
    assert stu.get(f"/api/lectures/{art['id']}/download/summary.md").status_code == 403

    pub_resp = prof.post(f"/api/lectures/{art['id']}/publish", json={"publish": True})
    assert pub_resp.status_code == 200
    assert pub_resp.get_json()["is_published"] is True

    events = [e for e in sio.get_received() if e["name"] == "lecture:summary_ready"]
    assert len(events) >= 1
    assert events[0]["args"][0]["course_id"] == cid
    sio.disconnect()

    assert len(stu.get(f"/api/courses/{cid}/lectures").get_json()["artifacts"]) == 1
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


def test_submission_composite_routing_grading_calibration_and_course_isolation(tmp_path: Path) -> None:
    """Verify composite-key submission routing, non-uniform AI+class grading, course isolation, and UI/rubric cleanup."""
    db_file = tmp_path / "hw_verify.db"
    cfg = Config(
        SECRET_KEY="hw-composite-grading-secret",
        DATABASE_URL=f"sqlite:///{db_file.as_posix()}",
        UPLOAD_DIR=tmp_path / "uploads",
        GEMINI_API_KEY="",
    )
    app = create_app(cfg, face_detector=StubDetector())
    app.config["TESTING"] = True
    try:
        prof = app.test_client()
        stu_strong = app.test_client()
        stu_weak = app.test_client()

        _reg_lecturer(prof, "Prof. Grading", "grading@ariel.ac.il")
        c1 = prof.post(
            "/api/courses",
            json={"course_code": "GRD101", "name": "OOP Grading", "initial_lecture_title": "L1"},
        ).get_json()
        c2 = prof.post(
            "/api/courses",
            json={"course_code": "GRD202", "name": "Databases", "initial_lecture_title": "L1"},
        ).get_json()
        c1_id, c2_id = c1["id"], c2["id"]

        a1 = prof.post(
            f"/api/courses/{c1_id}/assignments",
            json={"title": "Lab 1 — Stack", "rubric_text": "Implement bounded stack with error handling and tests."},
        ).get_json()
        a2 = prof.post(
            f"/api/courses/{c1_id}/assignments",
            json={"title": "Lab 2 — Queue", "rubric_text": "Implement FIFO queue."},
        ).get_json()
        b1 = prof.post(
            f"/api/courses/{c2_id}/assignments",
            json={"title": "SQL 1 — Joins", "rubric_text": "Normalize schema."},
        ).get_json()

        assert stu_strong.post("/api/student/login", json={"full_name": "Strong Dev", "student_id_number": "311111111"}).status_code == 200
        assert stu_weak.post("/api/student/login", json={"full_name": "Weak Dev", "student_id_number": "322222222"}).status_code == 200
        for sc in (stu_strong, stu_weak):
            assert sc.post("/api/student/enroll", json={"course_id": c1_id}).status_code == 200
            assert sc.post("/api/student/enroll", json={"course_id": c2_id}).status_code == 200

        # Mismatched course_id is rejected cleanly instead of misrouting
        bad_route = stu_strong.post(
            f"/api/assignments/{a1['id']}/submissions",
            data={
                "course_id": str(c2_id),
                "assignment_number": str(a1["assignment_number"]),
                "assignment_title": a1["title"],
                "file": (io.BytesIO(b"print('ok')"), "wrong_course.py"),
            },
            content_type="multipart/form-data",
        )
        assert bad_route.status_code == 400

        strong_code = (
            'class BoundedStack:\n'
            '    """Bounded stack supporting push/pop with error handling."""\n'
            '    def __init__(self, capacity: int = 10):\n'
            '        if capacity <= 0:\n'
            '            raise ValueError("Capacity must be positive")\n'
            '        self._items = []\n'
            '        self._cap = capacity\n'
            '    def push(self, item):\n'
            '        try:\n'
            '            if len(self._items) >= self._cap:\n'
            '                raise OverflowError("Stack full")\n'
            '            self._items.append(item)\n'
            '        except Exception:\n'
            '            raise\n'
            '    def pop(self):\n'
            '        if not self._items:\n'
            '            raise IndexError("Empty stack")\n'
            '        return self._items.pop()\n\n'
            'def test_bounded_stack():\n'
            '    st = BoundedStack(2)\n'
            '    st.push(1)\n'
            '    assert st.pop() == 1\n'
        ).encode("utf-8")
        weak_code = b"# todo\npass\n"

        r_s = stu_strong.post(
            f"/api/assignments/{a1['id']}/submissions",
            data={
                "course_id": str(c1_id),
                "assignment_number": str(a1["assignment_number"]),
                "assignment_title": a1["title"],
                "file": (io.BytesIO(strong_code), "stack_solution.py"),
            },
            content_type="multipart/form-data",
        )
        assert r_s.status_code == 201, r_s.get_data(as_text=True)
        sub_s = r_s.get_json()
        assert sub_s["course_id"] == c1_id
        assert sub_s["assignment_id"] == a1["id"]
        assert sub_s["assignment_number"] == a1["assignment_number"]
        assert sub_s["assignment_title"] == a1["title"]
        assert f"course_{c1_id}" in sub_s["source_ref"] and f"assignment_{a1['id']}_num_{a1['assignment_number']}" in sub_s["source_ref"]

        r_w = stu_weak.post(
            f"/api/assignments/{a1['id']}/submissions",
            data={
                "course_id": str(c1_id),
                "assignment_number": str(a1["assignment_number"]),
                "assignment_title": a1["title"],
                "file": (io.BytesIO(weak_code), "stub.py"),
            },
            content_type="multipart/form-data",
        )
        assert r_w.status_code == 201
        sub_w = r_w.get_json()

        # Calibration: strong vs weak must differ substantially in AI rubric score & ordinal class ranks
        assert sub_s["ai_rubric_grade"] >= sub_w["ai_rubric_grade"] + 25.0
        strong_detail = stu_strong.get(f"/api/assignments/{a1['id']}?course_id={c1_id}").get_json()
        weak_detail = stu_weak.get(f"/api/assignments/{a1['id']}?course_id={c1_id}").get_json()
        strong_comp = strong_detail["my_comparative_submission"]
        weak_comp = weak_detail["my_comparative_submission"]
        assert strong_comp["submission_type"] == "comparative"
        assert strong_comp["class_standing"]["rank"] == 1
        assert strong_comp["class_standing"]["rank_label"] == "1st Place"
        assert weak_comp["class_standing"]["rank"] == 2
        assert weak_comp["class_standing"]["rank_label"] == "2nd Place"
        assert strong_comp["class_standing"]["class_score"] > weak_comp["class_standing"]["class_score"]

        # Assignment A submission does NOT appear under Assignment A2 or Course C2
        c1_list = stu_strong.get(f"/api/courses/{c1_id}/assignments").get_json()["assignments"]
        c2_list = stu_strong.get(f"/api/courses/{c2_id}/assignments").get_json()["assignments"]
        assert {x["id"] for x in c1_list} == {a1["id"], a2["id"]}
        assert {x["id"] for x in c2_list} == {b1["id"]}
        a2_item = next(x for x in c1_list if x["id"] == a2["id"])
        assert a2_item["my_submission"] is None
        assert a2_item["my_final_submission"] is None

        # STRICT SEPARATION: Lecturer sees ZERO comparative submissions and zero comparative cohort metrics!
        lec_c1 = prof.get(f"/api/courses/{c1_id}/assignments?include_submissions=1").get_json()["assignments"]
        lec_a1 = next(x for x in lec_c1 if x["id"] == a1["id"])
        assert lec_a1["submissions"] == []
        assert lec_a1["final_submission_count"] == 0
        assert lec_a1["cohort_stats"]["submission_count"] == 0
        assert lec_a1["cohort_stats"]["mean_overall_score"] == 0.0
        assert lec_a1["cohort_stats"]["category_averages"] == {}

        # Now student_strong submits FINAL submission to lecturer
        final_code = strong_code + b"\n# Official final submission\n"
        r_final = stu_strong.post(
            f"/api/assignments/{a1['id']}/submissions",
            data={
                "course_id": str(c1_id),
                "assignment_number": str(a1["assignment_number"]),
                "assignment_title": a1["title"],
                "submission_type": "final",
                "file": (io.BytesIO(final_code), "final_stack.py"),
            },
            content_type="multipart/form-data",
        )
        assert r_final.status_code == 201, r_final.get_data(as_text=True)
        final_sub = r_final.get_json()
        assert final_sub["submission_type"] == "final"
        assert final_sub["file_name"].endswith("final_stack.py")
        assert final_sub["download_url"]
        assert final_sub["class_standing"] is None

        # Lecturer sees ONLY final submission, with standalone AI Score + downloadable file, and ZERO class ranking
        lec_c1_final = prof.get(f"/api/courses/{c1_id}/assignments?include_submissions=1").get_json()["assignments"]
        lec_a1_final = next(x for x in lec_c1_final if x["id"] == a1["id"])
        assert lec_a1_final["final_submission_count"] == 1
        assert len(lec_a1_final["submissions"]) == 1
        shown = lec_a1_final["submissions"][0]
        assert shown["submission_type"] == "final"
        assert shown["class_standing"] is None
        assert shown["category_breakdown"] == []
        assert shown["telemetry_comparison"] == {}
        assert shown["ai_rubric_grade"] >= 40.0
        dl_resp = prof.get(shown["download_url"])
        assert dl_resp.status_code == 200
        assert b"Official final submission" in dl_resp.data

        # Seeded assignment & templates contain neither irrelevant OOP blurb nor sample button nor lecturer Class Score
        prof.post(f"/api/courses/{c1_id}/assignments/seed-sample")
        lec_c1_after = prof.get(f"/api/courses/{c1_id}/assignments").get_json()["assignments"]
        assert not any("Evaluate OOP encapsulation" in (x.get("rubric_text") or "") for x in lec_c1_after)
        teacher_html = prof.get(f"/teacher?course_id={c1_id}").get_data(as_text=True)
        student_html = stu_strong.get("/student").get_data(as_text=True)
        assert "Use Ariel Classroom Sample" not in teacher_html
        assert "Evaluate OOP encapsulation" not in teacher_html
        assert "Class Score (1-10)" not in teacher_html
        assert "Final Student Submissions" in teacher_html
        assert "Available School Courses" not in student_html
        assert "join-course-code-input" in student_html
        assert "Final Submit to Lecturer" in student_html
        assert "lecturer-greeting-prefix" in teacher_html
        assert "student-greeting-prefix" in student_html
        assert "btn-webcam-headcount" in teacher_html
    finally:
        app.extensions["task_manager"].shutdown(wait=True)
        app.extensions["db_engine"].dispose()


def test_video_upload_transcription_summary_and_camera_headcount_discrepancy(tmp_path: Path) -> None:
    """Verify video (.mp4) audio extraction, transcription/summary, file+camera headcount, and discrepancy alert/approval."""
    import importlib.util
    from types import SimpleNamespace
    from app.services.llm_client import GeminiStudioClient

    ffmpeg_bin = shutil.which("ffmpeg")
    if not ffmpeg_bin:
        pytest.skip("ffmpeg binary not on host PATH (verified inside Docker container)")
    sample_photo = Path("data/samples/classroom_ariel.jpg")
    if not sample_photo.exists():
        pytest.skip("Sample photo not present")

    has_fw = importlib.util.find_spec("faster_whisper") is not None
    uploaded_audio_suffixes: list[str] = []

    class FakeGenAI:
        class files:
            @staticmethod
            def upload(file: str, **_kwargs):
                p = Path(file)
                uploaded_audio_suffixes.append(p.suffix.lower())
                assert p.exists() and p.stat().st_size > 0
                return SimpleNamespace(name="files/mock_vid_mp3", state=SimpleNamespace(name="ACTIVE"))

            @staticmethod
            def get(name: str):
                return SimpleNamespace(name=name, state=SimpleNamespace(name="ACTIVE"))

            @staticmethod
            def delete(name: str):
                return None

        class models:
            @staticmethod
            def generate_content(**kwargs):
                contents = kwargs.get("contents")
                if isinstance(contents, list):
                    return SimpleNamespace(text="[00:00] Video audio track extracted cleanly.\n[00:10] Lecture topic overview.")
                return SimpleNamespace(text="### Executive Summary (TL;DR)\nVideo recording transcribed cleanly into a structured study guide.")

    db_file = tmp_path / "video_cam_disc.db"
    cfg = Config(
        SECRET_KEY="video-cam-discrepancy-secret",
        DATABASE_URL=f"sqlite:///{db_file.as_posix()}",
        UPLOAD_DIR=tmp_path / "uploads",
        GEMINI_API_KEY="" if has_fw else "test-video-key",
    )
    llm_override = None if has_fw else GeminiStudioClient(
        api_key="test-video-key",
        min_interval_sec=0.0,
        client_override=FakeGenAI(),
    )
    app = create_app(cfg, llm_client=llm_override)
    app.config["TESTING"] = True
    try:
        prof = app.test_client()
        _reg_lecturer(prof, "Prof. Media", "media@ariel.ac.il")
        course = prof.post(
            "/api/courses",
            json={"course_code": "VID101", "name": "Media & Headcount", "initial_lecture_title": "Lecture 1"},
        ).get_json()
        sid = course["sessions"][0]["id"]
        scode = course["sessions"][0]["session_code"]

        # 1. Generate a small valid .mp4 video with audio track and upload with Hebrew filename
        vid_path = tmp_path / "lecture_clip.mp4"
        proc = subprocess.run(
            [
                ffmpeg_bin,
                "-y",
                "-f",
                "lavfi",
                "-i",
                "color=c=black:s=320x240:d=1",
                "-f",
                "lavfi",
                "-i",
                "sine=frequency=440:duration=1",
                "-c:v",
                "libx264",
                "-c:a",
                "aac",
                "-strict",
                "-2",
                "-shortest",
                str(vid_path),
            ],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=60,
        )
        assert proc.returncode == 0 and vid_path.exists()
        up_vid = prof.post(
            f"/api/sessions/{sid}/lectures/summarize",
            data={"media": (io.BytesIO(vid_path.read_bytes()), "הרצאה_1.mp4")},
            content_type="multipart/form-data",
        )
        assert up_vid.status_code == 201, up_vid.get_data(as_text=True)
        art = up_vid.get_json()
        assert art["status"] == "COMPLETED"
        assert art["media_path"].endswith(".mp4")
        assert len(art["summary_markdown"].strip()) > 30
        if not has_fw:
            assert uploaded_audio_suffixes == [".mp3"]

        # 2. Headcount via uploaded file + direct base64 camera capture
        raw_bytes = sample_photo.read_bytes()
        r_file = prof.post(
            f"/api/sessions/{sid}/headcount",
            data={"photo": (io.BytesIO(raw_bytes), "classroom.jpg")},
            content_type="multipart/form-data",
        )
        assert r_file.status_code == 200
        assert r_file.get_json()["auto_face_count"] >= 5

        b64_uri = "data:image/jpeg;base64," + base64.b64encode(raw_bytes).decode("ascii")
        r_cam = prof.post(
            f"/api/sessions/{sid}/headcount",
            json={"image_data": b64_uri, "source": "camera"},
        )
        assert r_cam.status_code == 200
        cam_data = r_cam.get_json()
        assert cam_data["source"] == "camera"
        assert cam_data["auto_face_count"] >= 5

        # 3. Registered students <= detected heads -> NO discrepancy alert
        qr_svc: RollingQRService = app.extensions["qr_service"]
        for i in range(1, 3):
            c_stu = app.test_client()
            qr_p = qr_svc.build_qr_payload("http://localhost", scode)
            chk = c_stu.post(
                "/api/attendance/checkin",
                json={
                    "session_code": scode,
                    "t": qr_p["bucket"],
                    "sig": qr_p["sig"],
                    "full_name": f"Present Stu {i}",
                    "student_id_number": f"20900000{i}",
                },
            )
            assert chk.status_code in (200, 201)

        ov_ok = prof.get(f"/api/sessions/{sid}/overview").get_json()
        assert ov_ok["verdict"]["discrepancy_requires_approval"] is False

        # 4. Patch headcount to 1 while 2 students checked in -> discrepancy alert & approval flow
        patched = prof.patch(f"/api/sessions/{sid}/headcount", json={"verified_face_count": 1}).get_json()
        assert patched["verdict"]["discrepancy_requires_approval"] is True
        assert patched["verdict"]["discrepancy_excess"] == 1

        approved = prof.post(f"/api/sessions/{sid}/headcount/approve-discrepancy", json={}).get_json()
        assert approved["ok"] is True
        assert approved["verdict"]["discrepancy_approved"] is True
        assert approved["verdict"]["discrepancy_requires_approval"] is False
    finally:
        app.extensions["task_manager"].shutdown(wait=True)
        app.extensions["db_engine"].dispose()


def test_manual_course_number_join_request_and_lecturer_approval(overhaul_app) -> None:
    """Verify students join courses manually via course number (case-insensitive) and trigger lecturer approval flow."""
    prof = overhaul_app.test_client()
    _reg_lecturer(prof, "Prof Join", "prof_join@uni.edu")
    c = prof.post("/api/courses", json={"name": "Networks 101", "course_code": "NET-402", "lecturer_name": "Prof Join"}).get_json()

    stu = overhaul_app.test_client()
    stu.post("/api/student/login", json={"full_name": "Dana Levi", "student_id_number": "301999111"})

    # Bad course number returns 404
    r_bad = stu.post("/api/student/enroll-request", json={"course_number": "DOES-NOT-EXIST"})
    assert r_bad.status_code == 404

    # Case-insensitive course_number sends pending request
    r_req = stu.post("/api/student/enroll-request", json={"course_number": "net-402"})
    assert r_req.status_code == 202, r_req.get_data(as_text=True)
    payload = r_req.get_json()
    assert payload["status"] == "pending"
    assert payload["course_id"] == c["id"]
    assert payload["course_code"] == "NET-402"
    assert any(p["id"] == c["id"] for p in payload["dashboard"]["pending_courses"])
    assert all(m["id"] != c["id"] for m in payload["dashboard"]["my_courses"])

    # Lecturer pending list shows Dana Levi
    pending = prof.get(f"/api/courses/{c['id']}/enrollment-requests").get_json()["pending_requests"]
    assert len(pending) == 1 and pending[0]["student_id_number"] == "301999111"

    # Approve grants access
    stu_id = payload["dashboard"]["student"]["id"]
    r_app = prof.post(f"/api/courses/{c['id']}/enrollment-requests/{stu_id}/approve", json={})
    assert r_app.status_code == 200
    me = stu.get("/api/student/me").get_json()
    assert any(m["id"] == c["id"] for m in me["my_courses"])
    assert all(p["id"] != c["id"] for p in me["pending_courses"])


def test_lecture_summary_draft_publish_and_grouped_assignments_templates(overhaul_app) -> None:
    """Verify draft-only summaries, structured section parsing, publish/unpublish toggle, and course-grouped global assignments."""
    prof = overhaul_app.test_client()
    _reg_lecturer(prof, "Prof. Publisher", "publisher@uni.edu")
    c = prof.post(
        "/api/courses",
        json={"name": "Algorithms & Structures", "course_code": "ALG301", "initial_lecture_title": "Asymptotic Bounds"},
    ).get_json()
    cid = c["id"]
    sid = c["sessions"][0]["id"]

    stu = overhaul_app.test_client()
    assert stu.post("/api/student/login", json={"full_name": "Noa Katz", "student_id_number": "322111444"}).status_code == 200
    assert stu.post("/api/student/enroll", json={"course_id": cid}).status_code == 200

    res = prof.post(
        f"/api/sessions/{sid}/lectures/summarize",
        json={"transcript_text": "[00:05] Asymptotic notation compares growth rates.\n[02:10] Merge sort runs in O(n log n) time."},
    )
    assert res.status_code == 201
    art = res.get_json()
    assert art["is_published"] is False
    assert art["course_name"] == "Algorithms & Structures"
    assert art["structured_sections"]["executive_summary"]
    assert art["structured_sections"]["key_topics"]
    assert art["structured_sections"]["action_items"]

    # Draft hidden in student dashboard & course lectures API
    dash_before = stu.get("/api/student/me").get_json()
    my_c_before = next(x for x in dash_before["my_courses"] if x["id"] == cid)
    assert my_c_before["lecture_summaries"] == []
    assert my_c_before["sessions"][0]["has_summary"] is False

    # Publish unlocks student access -> Unpublish hides it immediately everywhere while keeping Draft intact for Lecturer
    sio_u = socketio.test_client(overhaul_app, flask_test_client=stu)
    sio_u.emit("join_notifications", {})
    sio_u.get_received()

    assert prof.post(f"/api/lectures/{art['id']}/publish", json={"publish": True}).status_code == 200
    dash_after = stu.get("/api/student/me").get_json()
    my_c_after = next(x for x in dash_after["my_courses"] if x["id"] == cid)
    assert len(my_c_after["lecture_summaries"]) == 1
    assert my_c_after["sessions"][0]["has_summary"] is True

    # Unpublish (accepting both bool False and string "false")
    sio_u.get_received()
    unpub_res = prof.post(f"/api/lectures/{art['id']}/publish", json={"publish": False})
    assert unpub_res.status_code == 200
    assert unpub_res.get_json()["is_published"] is False

    unpub_events = [e for e in sio_u.get_received() if e["name"] == "lecture:unpublished"]
    assert len(unpub_events) >= 1 and unpub_events[0]["args"][0]["id"] == art["id"]
    sio_u.disconnect()

    assert stu.get(f"/api/courses/{cid}/lectures").get_json()["artifacts"] == []
    assert stu.get(f"/api/courses/{cid}/lectures?published_only=1").get_json()["artifacts"] == []
    assert stu.get(f"/api/lectures/{art['id']}/download/summary.md?published_only=1").status_code == 403
    # Dual-cookie browser simulation (lecturer + student cookies together): published_only=1 still hides unpublished
    assert prof.get(f"/api/courses/{cid}/lectures?published_only=1").get_json()["artifacts"] == []
    # Lecturer draft still remains intact in session artifacts and can be re-published
    lec_arts = prof.get(f"/api/sessions/{sid}/lectures").get_json()["artifacts"]
    assert len(lec_arts) == 1 and lec_arts[0]["is_published"] is False
    dash_unpub = stu.get("/api/student/me").get_json()
    my_c_unpub = next(x for x in dash_unpub["my_courses"] if x["id"] == cid)
    assert my_c_unpub["lecture_summaries"] == []
    assert my_c_unpub["sessions"][0]["has_summary"] is False

    # Re-publish unlocks again cleanly
    assert prof.post(f"/api/lectures/{art['id']}/publish", json={"publish": True}).status_code == 200
    assert len(stu.get(f"/api/courses/{cid}/lectures?published_only=1").get_json()["artifacts"]) == 1

    # Verify UI templates include grouped global assignments, Publish to Students, and published_only=1 polling
    teacher_html = prof.get(f"/teacher?course_id={cid}").get_data(as_text=True)
    student_html = stu.get("/student").get_data(as_text=True)
    assert "Publish to Students" in teacher_html
    assert "publishLectureArtifact" in teacher_html
    assert "data-assignment-course-group" in student_html
    assert "published_only=1" in student_html


def test_video_summary_regression_fix_cascade_delete_and_sidebar_tree(tmp_path: Path) -> None:
    """Verify fast daily-quota failover, content-grounded local fallback, sequential lecture sorting, cascade delete, and sidebar tree."""
    from types import SimpleNamespace
    from app.services.llm_client import GeminiStudioClient

    attempts: list[str] = []

    class DailyExhaustedFakeModels:
        def generate_content(self, **kwargs):
            m = kwargs.get("model", "")
            attempts.append(m)
            if m == "gemini-2.5-flash-lite":
                raise RuntimeError("429 RESOURCE_EXHAUSTED GenerateRequestsPerDayPerProjectPerModel-FreeTier retryDelay 79458s")
            raise RuntimeError("404 NOT_FOUND")

    fake_llm = GeminiStudioClient(
        api_key="fake-key",
        min_interval_sec=0.0,
        backoff_base_sec=5.0,
        max_retries=3,
        client_override=SimpleNamespace(models=DailyExhaustedFakeModels()),
    )

    db_file = tmp_path / "test_reg_cascade.db"
    cfg = Config(
        SECRET_KEY="regression-cascade-secret",
        DATABASE_URL=f"sqlite:///{db_file.as_posix()}",
        UPLOAD_DIR=tmp_path / "uploads",
        GEMINI_API_KEY="fake-key",
    )
    app = create_app(cfg, face_detector=StubDetector(), llm_client=fake_llm)
    app.config["TESTING"] = True
    try:
        prof = app.test_client()
        stu = app.test_client()
        _reg_lecturer(prof, "Prof. Cascade", "cascade@uni.edu")
        c = prof.post(
            "/api/courses",
            json={"name": "Algorithms II", "course_code": "ALG202", "initial_lecture_title": "Lecture 1 — Divide & Conquer"},
        ).get_json()
        cid = c["id"]
        sid1 = c["sessions"][0]["id"]
        sid2 = prof.post(f"/api/courses/{cid}/sessions", json={"title": "Dynamic Programming"}).get_json()["id"]
        sid3 = prof.post(f"/api/courses/{cid}/sessions", json={"title": "Graph Flows"}).get_json()["id"]

        assert stu.post("/api/student/login", json={"full_name": "Student Sync", "student_id_number": "388111222"}).status_code == 200
        assert stu.post("/api/student/enroll", json={"course_id": cid}).status_code == 200

        # 1. Generate summary on Lecture 3 first -> daily-quota error on 2.5-flash-lite must NOT retry 3x with 5s sleeps,
        #    and content-grounded fallback must reflect spoken lecture content (NEVER Automated Synthesis prompt dump!)
        s3 = prof.post(
            f"/api/sessions/{sid3}/lectures/summarize",
            json={"transcript_text": "[00:12] Ford-Fulkerson augments paths along residual graphs.\n[03:40] Max-flow min-cut theorem proves optimality."},
        ).get_json()
        assert attempts.count("gemini-2.5-flash-lite") == 2  # Exactly 1 attempt in Pass-1 + 1 in Pass-2 (zero retries)
        assert "Automated Synthesis" not in s3["summary_markdown"]
        assert "Generated locally" not in s3["summary_markdown"]
        assert "Ford-Fulkerson" in s3["summary_markdown"]
        assert "Max-flow min-cut theorem" in s3["summary_markdown"]
        assert prof.post(f"/api/lectures/{s3['id']}/publish", json={"publish": True}).status_code == 200

        s1 = prof.post(
            f"/api/sessions/{sid1}/lectures/summarize",
            json={"transcript_text": "[00:05] Master theorem solves divide and conquer recurrences."},
        ).get_json()
        assert prof.post(f"/api/lectures/{s1['id']}/publish", json={"publish": True}).status_code == 200

        s2 = prof.post(
            f"/api/sessions/{sid2}/lectures/summarize",
            json={"transcript_text": "[00:10] Memoization caches overlapping subproblems in DP."},
        ).get_json()
        assert prof.post(f"/api/lectures/{s2['id']}/publish", json={"publish": True}).status_code == 200

        # 2. Sequential Lecture Number order (Lecture 1 -> Lecture 2 -> Lecture 3) regardless of creation/publish order
        stu_lecs = stu.get(f"/api/courses/{cid}/lectures").get_json()["artifacts"]
        assert [a["lecture_number"] for a in stu_lecs] == [1, 2, 3]

        # 3. Cascade Delete: Lecturer deleting s2 emits lecture:deleted & removes immediately from Student APIs
        sio = socketio.test_client(app, flask_test_client=stu)
        sio.emit("join_notifications", {})
        sio.get_received()

        del_res = prof.delete(f"/api/lectures/{s2['id']}")
        assert del_res.status_code == 200
        del_events = [e for e in sio.get_received() if e["name"] == "lecture:deleted"]
        assert len(del_events) >= 1 and del_events[0]["args"][0]["id"] == s2["id"]
        sio.disconnect()

        stu_after_del = stu.get(f"/api/courses/{cid}/lectures").get_json()["artifacts"]
        assert [a["lecture_number"] for a in stu_after_del] == [1, 3]
        dash = stu.get("/api/student/me").get_json()
        my_c = next(x for x in dash["my_courses"] if x["id"] == cid)
        assert [a["lecture_number"] for a in my_c["lecture_summaries"]] == [1, 3]

        # 4. Student HTML renders 3-section hierarchical indented sidebar tree + cascade listener
        student_html = stu.get("/student").get_data(as_text=True)
        for token in ("nav-tree-courses", "nav-tree-assignments", "nav-tree-summaries", "cp-nav-tree--l2", "lecture:deleted"):
            assert token in student_html
    finally:
        app.extensions["task_manager"].shutdown(wait=True)
        app.extensions["db_engine"].dispose()
