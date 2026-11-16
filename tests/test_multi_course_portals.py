from __future__ import annotations

from pathlib import Path

import pytest

from app import create_app
from app.config import Config
from app.services.face_counter import BoundingBox, FaceDetector
from app.services.qr_service import RollingQRService


class StubDetector(FaceDetector):
    def detect(self, image) -> list[BoundingBox]:
        return []


@pytest.fixture()
def school_app(tmp_path: Path):
    db_file = tmp_path / "test_school_wide.db"
    cfg = Config(
        SECRET_KEY="phase10-school-secret",
        DATABASE_URL=f"sqlite:///{db_file.as_posix()}",
    )
    app = create_app(cfg, face_detector=StubDetector())
    app.config["TESTING"] = True
    yield app
    app.extensions["task_manager"].shutdown(wait=True)
    app.extensions["db_engine"].dispose()


def test_multi_course_creation_and_teacher_portal_rendering(school_app) -> None:
    """Phase 10A/10B: Verify multi-course creation, course lecture sessions, and /teacher portal."""
    client = school_app.test_client()

    # 0. Register as a Lecturer first
    reg_resp = client.post(
        "/api/auth/lecturer/register",
        json={
            "full_name": "Prof. Boaz Ben-Moshe",
            "username": "boaz.bm",
            "email": "boaz@ariel.ac.il",
            "password": "Password123!",
            "department": "Computer Science",
        },
    )
    assert reg_resp.status_code == 201

    # 1. Create a new school course via REST API
    create_resp = client.post(
        "/api/courses",
        json={
            "course_code": "SE305",
            "name": "Deep Learning & Computer Vision",
            "department": "Computer Science",
            "semester": "Fall 2026",
            "lecturer_name": "Prof. Boaz Ben-Moshe",
        },
    )
    assert create_resp.status_code == 201
    course_data = create_resp.get_json()
    assert course_data["course_code"] == "SE305"
    assert course_data["session_count"] == 1
    course_id = course_data["id"]

    # 2. Add a second lecture session under SE305
    sess_resp = client.post(
        f"/api/courses/{course_id}/sessions",
        json={"title": "Lecture 2 — Object Detection & NMS", "session_code": "SE305L2"},
    )
    assert sess_resp.status_code == 201

    # 3. Verify Teacher Portal renders with SE305, its lectures, and Tab 5 AI Hub
    portal_resp = client.get(f"/teacher?course_id={course_id}&session=SE305L2")
    assert portal_resp.status_code == 200
    assert b"SE305" in portal_resp.data
    assert b"Lecture 2" in portal_resp.data
    assert b"AI Lecture Summarizer" in portal_resp.data
    assert b"Homework Cohort Analytics" in portal_resp.data


def test_qr_checkin_auto_enrolls_student_in_course_and_populates_student_app(
    school_app,
) -> None:
    """Phase 10A/10C: Scanning a lecture QR automatically enrolls the student in the parent Course and shows it in /student."""
    client = school_app.test_client()
    qr: RollingQRService = school_app.extensions["qr_service"]

    # Ensure default courses (CS204 -> ARIEL1) exist in public catalog
    courses_resp = client.get("/api/student/courses/catalog").get_json()
    assert len(courses_resp["courses"]) >= 2

    # Student checks into ARIEL1 via QR
    b = qr.compute_bucket()
    sig = qr.sign_token("ARIEL1", b)
    checkin_resp = client.post(
        "/api/attendance/checkin",
        json={
            "session_code": "ARIEL1",
            "t": b,
            "sig": sig,
            "full_name": "Ohad Ben Nun",
            "email": "ohad@ariel.ac.il",
            "device_id": "ohad-mobile-dev",
        },
    )
    assert checkin_resp.status_code == 200

    # Student opens /api/student/me (holding the persistent cp_device cookie)
    me_resp = client.get("/api/student/me")
    assert me_resp.status_code == 200
    me_data = me_resp.get_json()
    assert me_data["authenticated"] is True
    assert me_data["student"]["email"] == "ohad@ariel.ac.il"

    enrolled_codes = [c["course_code"] for c in me_data["my_courses"]]
    assert "CS204" in enrolled_codes

    # Student joins a second course (CS101) by course code from the Student App
    join_resp = client.post("/api/student/enroll", json={"course_code": "CS101"})
    assert join_resp.status_code == 200
    updated_codes = [
        c["course_code"] for c in join_resp.get_json()["dashboard"]["my_courses"]
    ]
    assert "CS204" in updated_codes
    assert "CS101" in updated_codes

    # Render /student HTML app (with 3-mode Slido/Study Hub tabs)
    student_html = client.get("/student")
    assert student_html.status_code == 200
    assert b"ClassPulse Student App" in student_html.data
    assert b"Ohad Ben Nun" in student_html.data
    assert b"AI Study Hub" in student_html.data


def test_lecturer_signin_and_class_lecture_creation_workflow(school_app) -> None:
    """Phase 11A/11B/11C: Lecturer signs in -> creates class -> creates lecture -> student QR scan syncs personal logs."""
    teacher_client = school_app.test_client()
    student_client = school_app.test_client()
    qr: RollingQRService = school_app.extensions["qr_service"]

    # 1. Unauthenticated /teacher redirects to /teacher/login
    unauth_resp = teacher_client.get("/teacher")
    assert unauth_resp.status_code == 302
    assert "/teacher/login" in unauth_resp.headers["Location"]

    # 2. Lecturer registers & creates a new Class (Course) + Lecture
    reg_resp = teacher_client.post(
        "/api/auth/lecturer/register",
        json={
            "full_name": "Prof. Boaz Ben-Moshe",
            "username": "boaz_ariel",
            "email": "boaz@ariel.ac.il",
            "password": "StrongPass1!",
            "department": "Computer Science",
        },
    )
    assert reg_resp.status_code == 201
    assert reg_resp.get_json()["lecturer"]["email"] == "boaz@ariel.ac.il"

    # 3. Signed-in Lecturer creates a new Class (Course) + Lecture
    c_resp = teacher_client.post(
        "/api/courses",
        json={
            "course_code": "OOP2026",
            "name": "Advanced Object-Oriented Design",
            "initial_lecture_title": "Lecture 1 — SOLID Principles",
        },
    )
    assert c_resp.status_code == 201
    c_data = c_resp.get_json()
    course_id = c_data["id"]

    l2_resp = teacher_client.post(
        f"/api/courses/{course_id}/sessions",
        json={"title": "Lecture 2 — Factory & Adapter Patterns", "session_code": "OOP26L2"},
    )
    assert l2_resp.status_code == 201

    # 4. Student scans QR for Lecture 2 (OOP26L2) for the first time
    b = qr.compute_bucket()
    sig = qr.sign_token("OOP26L2", b)
    checkin_resp = student_client.post(
        "/api/attendance/checkin",
        json={
            "session_code": "OOP26L2",
            "t": b,
            "sig": sig,
            "full_name": "Yael Levi",
            "email": "yael@ariel.ac.il",
            "device_id": "yael-phone-01",
        },
    )
    assert checkin_resp.status_code == 200

    # 5. Student's personal database view (/api/student/me) automatically has OOP2026, both lectures, and attendance logs!
    me_data = student_client.get("/api/student/me").get_json()
    oop_course = next(c for c in me_data["my_courses"] if c["course_code"] == "OOP2026")
    assert oop_course["is_enrolled"] is True
    assert oop_course["attended_count"] == 1
    assert oop_course["total_sessions"] == 2

    logs_by_code = {log["session_code"]: log for log in oop_course["my_attendance_logs"]}
    assert logs_by_code["OOP26L2"]["status"] == "PRESENT"
    assert logs_by_code["OOP26L2"]["checked_in_at"] != "Not checked in"
