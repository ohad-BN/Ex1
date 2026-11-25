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
def isolated_app(tmp_path: Path):
    db_file = tmp_path / "test_lecturer_isolation.db"
    cfg = Config(
        SECRET_KEY="lecturer-isolation-secret",
        DATABASE_URL=f"sqlite:///{db_file.as_posix()}",
        UPLOAD_DIR=tmp_path / "uploads",
        REPORT_DIR=tmp_path / "reports",
    )
    app = create_app(cfg, face_detector=StubDetector())
    app.config["TESTING"] = True
    yield app
    app.extensions["task_manager"].shutdown(wait=True)
    app.extensions["db_engine"].dispose()


def test_two_lecturers_have_completely_isolated_dashboards_and_courses(isolated_app) -> None:
    """Boas and Daniel must each get their own isolated dashboard, courses, and lectures with 403/409 cross-access blocks."""
    boas = isolated_app.test_client()
    daniel = isolated_app.test_client()
    anon = isolated_app.test_client()
    student = isolated_app.test_client()
    qr: RollingQRService = isolated_app.extensions["qr_service"]

    # 0. Unauthenticated access to /teacher?course_id=1 and POST /api/courses must be blocked
    unauth_teacher = anon.get("/teacher?course_id=1")
    assert unauth_teacher.status_code == 302
    assert "/teacher/login" in unauth_teacher.headers["Location"]

    unauth_create = anon.post(
        "/api/courses", json={"course_code": "ANON1", "name": "Unauthorized Course"}
    )
    assert unauth_create.status_code == 401

    # 1. Lecturer 1 (Boas) registers -> starts with empty personal course list -> creates BOAS101 + Lecture 2
    boas_login = boas.post(
        "/api/auth/lecturer/register",
        json={
            "full_name": "Prof. Boaz Ben-Moshe",
            "username": "boas_prof",
            "email": "boaz@ariel.ac.il",
            "password": "BoasPassword1!",
            "department": "Computer Science",
        },
    )
    assert boas_login.status_code == 201
    assert boas_login.get_json()["courses"] == []

    # Before Boas creates a course, /teacher redirects to /teacher/login?setup=1 (never leaks global courses)
    boas_setup = boas.get("/teacher")
    assert boas_setup.status_code == 302
    assert "setup=1" in boas_setup.headers["Location"]

    boas_course = boas.post(
        "/api/courses",
        json={
            "course_code": "BOAS101",
            "name": "Object-Oriented Architecture",
            "initial_lecture_title": "Lecture 1 — SOLID",
        },
    )
    assert boas_course.status_code == 201
    boas_data = boas_course.get_json()
    boas_cid = boas_data["id"]
    boas_sid = boas_data["sessions"][0]["id"]
    boas_scode = boas_data["sessions"][0]["session_code"]

    # 2. Lecturer 2 (Daniel) registers -> gets an empty dashboard, ZERO overlap with Boas!
    dan_login = daniel.post(
        "/api/auth/lecturer/register",
        json={
            "full_name": "Dr. Daniel Levi",
            "username": "daniel_prof",
            "email": "daniel@ariel.ac.il",
            "password": "DanielPassword1!",
            "department": "Software Engineering",
        },
    )
    assert dan_login.status_code == 201
    assert dan_login.get_json()["courses"] == []

    assert daniel.get("/api/courses").get_json()["courses"] == []

    dan_setup = daniel.get("/teacher")
    assert dan_setup.status_code == 302
    assert "setup=1" in dan_setup.headers["Location"]

    # 3. Daniel CANNOT hijack BOAS101 by recreating the same course_code or hitting Boas's URLs/APIs
    dup_resp = daniel.post(
        "/api/courses",
        json={"course_code": "BOAS101", "name": "Attempted Takeover"},
    )
    assert dup_resp.status_code == 409

    assert daniel.get(f"/teacher?course_id={boas_cid}").status_code == 403
    assert daniel.get(f"/teacher?session={boas_scode}").status_code == 403
    assert daniel.get(f"/api/courses/{boas_cid}/overview").status_code == 403
    assert (
        daniel.post(
            f"/api/courses/{boas_cid}/sessions",
            json={"title": "Hijacked Lecture"},
        ).status_code
        == 403
    )
    assert (
        daniel.post(
            f"/api/sessions/{boas_sid}/polls",
            json={"question": "Hijacked Poll?", "options": ["A", "B"]},
        ).status_code
        == 403
    )
    assert (
        daniel.post(
            f"/api/courses/{boas_cid}/assignments",
            json={"title": "Hijacked HW", "rubric_text": "test", "topics": ["OOP"]},
        ).status_code
        == 403
    )
    assert (
        daniel.post(
            f"/api/sessions/{boas_sid}/lectures/summarize",
            json={"use_sample": True},
        ).status_code
        == 403
    )

    # 4. Daniel creates his own course DAN202 & lecture DANL2
    dan_course = daniel.post(
        "/api/courses",
        json={
            "course_code": "DAN202",
            "name": "Distributed Systems",
            "initial_lecture_title": "Lecture 1 — Consensus",
        },
    )
    assert dan_course.status_code == 201
    dan_data = dan_course.get_json()
    dan_cid = dan_data["id"]

    dan_l2 = daniel.post(
        f"/api/courses/{dan_cid}/sessions",
        json={"title": "Lecture 2 — Raft", "session_code": "DANL2"},
    )
    assert dan_l2.status_code == 201

    # 5. Each lecturer sees ONLY their own courses & lectures in /api/courses and /teacher
    boas_list = [c["course_code"] for c in boas.get("/api/courses").get_json()["courses"]]
    dan_list = [c["course_code"] for c in daniel.get("/api/courses").get_json()["courses"]]
    assert boas_list == ["BOAS101"]
    assert dan_list == ["DAN202"]

    boas_html = boas.get("/teacher")
    assert boas_html.status_code == 200
    assert b"BOAS101" in boas_html.data
    assert b"DAN202" not in boas_html.data

    dan_html = daniel.get("/teacher")
    assert dan_html.status_code == 200
    assert b"DAN202" in dan_html.data
    assert b"BOAS101" not in dan_html.data

    # Boas is also blocked from Daniel's course
    assert boas.get(f"/teacher?course_id={dan_cid}").status_code == 403
    assert boas.get(f"/api/courses/{dan_cid}/overview").status_code == 403

    # 6. Student checks into both lectures via QR -> student sees both courses, lecturers still isolated
    for scode in (boas_scode, "DANL2"):
        b = qr.compute_bucket()
        sig = qr.sign_token(scode, b)
        res = student.post(
            "/api/attendance/checkin",
            json={
                "session_code": scode,
                "t": b,
                "sig": sig,
                "full_name": "Ohad Student",
                "email": "ohad@ariel.ac.il",
                "device_id": "ohad-phone-dev",
            },
        )
        assert res.status_code == 200

    st_courses = {
        c["course_code"]
        for c in student.get("/api/student/me").get_json()["my_courses"]
    }
    assert {"BOAS101", "DAN202"}.issubset(st_courses)

    # Confirm logging out resets lecturer session cleanly
    assert daniel.post("/api/teacher/logout").status_code == 200
    assert daniel.get("/teacher").status_code == 302


def test_new_lecturer_does_not_inherit_seeded_demo_courses(isolated_app) -> None:
    """When default demo courses exist (owned by Boas), a newly signed-in lecturer like Daniel gets 0 courses."""
    with isolated_app.app_context():
        from app.services.course_service import CourseService

        CourseService(isolated_app.extensions["db_session"]).ensure_default_school_courses()

    daniel = isolated_app.test_client()
    login_resp = daniel.post(
        "/api/auth/lecturer/register",
        json={
            "full_name": "Daniel",
            "username": "daniel_new",
            "email": "daniel.new@ariel.ac.il",
            "password": "DanielPassword1!",
            "department": "Computer Science",
        },
    )
    assert login_resp.status_code == 201
    assert login_resp.get_json()["courses"] == []
    assert daniel.get("/api/courses").get_json()["courses"] == []
    assert daniel.get("/teacher?session=ARIEL1").status_code == 403

    boas = isolated_app.test_client()
    boas_login = boas.post(
        "/api/auth/lecturer/login",
        json={
            "identifier": "boaz",
            "password": "ClassPulse2026!",
        },
    )
    assert boas_login.status_code == 200
    boas_codes = {c["course_code"] for c in boas_login.get_json()["courses"]}
    assert {"CS204", "CS101"}.issubset(boas_codes)

