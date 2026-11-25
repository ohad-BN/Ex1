from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

import pytest

from app import create_app
from app.config import Config
from app.models import ClassSession, Course, CourseEnrollment, Student
from app.services.face_counter import BoundingBox, FaceDetector


class StubDetector(FaceDetector):
    def detect(self, image) -> list[BoundingBox]:
        return []


@pytest.fixture()
def mobile_app(tmp_path: Path):
    db_file = tmp_path / "test_mobile_fix.db"
    cfg = Config(
        SECRET_KEY="mobile-fix-secret",
        DATABASE_URL=f"sqlite:///{db_file.as_posix()}",
        UPLOAD_DIR=tmp_path / "uploads",
        REPORT_DIR=tmp_path / "reports",
    )
    app = create_app(cfg, face_detector=StubDetector())
    app.config["TESTING"] = True
    yield app
    app.extensions["task_manager"].shutdown(wait=True)
    app.extensions["db_engine"].dispose()


def _sign_in_student(client, *, name="Ohad Ben Nun", email="ohad@ariel.ac.il"):
    resp = client.post(
        "/api/student/login",
        json={"full_name": name, "student_id_number": "208123456", "email": email},
    )
    assert resp.status_code == 200
    return resp.get_json()["student"]["id"]


def test_courses_sorted_by_most_recent_lecture_first(mobile_app) -> None:
    """Courses in /api/student/me must sort by their most recent lecture date descending."""
    client = mobile_app.test_client()
    student_id = _sign_in_student(client)

    db = mobile_app.extensions["db_session"]
    try:
        c_old = Course(course_code="OLD101", name="Older Course", lecturer_name="Prof. A")
        c_mid = Course(course_code="MID102", name="Most Recent Course", lecturer_name="Prof. B")
        c_new = Course(course_code="NEW103", name="Second Recent Course", lecturer_name="Prof. C")
        db.add_all([c_old, c_mid, c_new])
        db.flush()

        db.add_all(
            [
                CourseEnrollment(course_id=c_old.id, student_id=student_id),
                CourseEnrollment(course_id=c_mid.id, student_id=student_id),
                CourseEnrollment(course_id=c_new.id, student_id=student_id),
                ClassSession(
                    course_id=c_old.id,
                    title="Old Lecture",
                    session_code="OLDL1",
                    started_at=datetime(2026, 11, 10, 9, 0, tzinfo=timezone.utc),
                ),
                ClassSession(
                    course_id=c_mid.id,
                    title="Latest Lecture Today",
                    session_code="MIDL1",
                    started_at=datetime(2026, 11, 22, 14, 0, tzinfo=timezone.utc),
                ),
                ClassSession(
                    course_id=c_new.id,
                    title="Midweek Lecture",
                    session_code="NEWL1",
                    started_at=datetime(2026, 11, 18, 11, 0, tzinfo=timezone.utc),
                ),
            ]
        )
        db.commit()
    finally:
        db.remove()

    data = client.get("/api/student/me").get_json()
    my_codes = [c["course_code"] for c in data["my_courses"]]
    assert my_codes[:3] == ["MID102", "NEW103", "OLD101"]
    assert data["my_courses"][0]["latest_lecture_at"].startswith("2026-11-22")


def test_courses_with_no_lectures_sort_after_courses_with_lectures(mobile_app) -> None:
    """Courses with zero lectures must never crash and must sort after courses that have lectures."""
    client = mobile_app.test_client()
    student_id = _sign_in_student(client, email="empty@ariel.ac.il")

    db = mobile_app.extensions["db_session"]
    try:
        c_empty = Course(course_code="EMPTY104", name="No Lectures Yet", lecturer_name="Prof. Z")
        c_has = Course(course_code="HAS105", name="Has Lecture", lecturer_name="Prof. Y")
        db.add_all([c_empty, c_has])
        db.flush()

        db.add_all(
            [
                CourseEnrollment(course_id=c_empty.id, student_id=student_id),
                CourseEnrollment(course_id=c_has.id, student_id=student_id),
                ClassSession(
                    course_id=c_has.id,
                    title="Only Lecture",
                    session_code="HASL1",
                    started_at=datetime(2026, 11, 1, 8, 0, tzinfo=timezone.utc),
                ),
            ]
        )
        db.commit()
    finally:
        db.remove()

    data = client.get("/api/student/me").get_json()
    my_codes = [c["course_code"] for c in data["my_courses"]]
    all_codes = [c["course_code"] for c in data["all_courses"]]
    assert my_codes.index("HAS105") < my_codes.index("EMPTY104")
    assert all_codes.index("HAS105") < all_codes.index("EMPTY104")

    empty_card = next(c for c in data["my_courses"] if c["course_code"] == "EMPTY104")
    assert empty_card["latest_lecture_at"] == ""


def test_sessions_sorted_newest_first_with_tie_breaker(mobile_app) -> None:
    """Lectures inside a course must sort by started_at DESC, breaking ties by id DESC."""
    client = mobile_app.test_client()
    student_id = _sign_in_student(client, email="ties@ariel.ac.il")

    db = mobile_app.extensions["db_session"]
    try:
        c = Course(course_code="DATE200", name="Date Ordering Course", lecturer_name="Prof. D")
        db.add(c)
        db.flush()
        db.add(CourseEnrollment(course_id=c.id, student_id=student_id))

        # Insert newer-id older-date session AFTER older-id newer-date sessions to prove started_at dominates id.
        s_tie_a = ClassSession(
            course_id=c.id,
            title="Tie Lecture A",
            session_code="S_TIE_A",
            started_at=datetime(2026, 11, 20, 10, 0, tzinfo=timezone.utc),
        )
        db.add(s_tie_a)
        db.flush()

        s_tie_b = ClassSession(
            course_id=c.id,
            title="Tie Lecture B",
            session_code="S_TIE_B",
            started_at=datetime(2026, 11, 20, 10, 0, tzinfo=timezone.utc),
        )
        db.add(s_tie_b)
        db.flush()

        s_old = ClassSession(
            course_id=c.id,
            title="Older Backfilled Lecture",
            session_code="S_OLD",
            started_at=datetime(2026, 11, 12, 10, 0, tzinfo=timezone.utc),
        )
        db.add(s_old)
        db.commit()
    finally:
        db.remove()

    data = client.get("/api/student/me").get_json()
    course = next(x for x in data["my_courses"] if x["course_code"] == "DATE200")
    assert [s["session_code"] for s in course["sessions"]] == ["S_TIE_B", "S_TIE_A", "S_OLD"]
    assert all(s["started_at_iso"] for s in course["sessions"])
    assert [log["session_code"] for log in course["my_attendance_logs"]] == [
        "S_TIE_B",
        "S_TIE_A",
        "S_OLD",
    ]


def test_student_app_renders_mobile_back_button_and_history_guard(mobile_app) -> None:
    """Student App renders persistent #student-back-btn (Web + Mobile) and popstate sign-in guard."""
    client = mobile_app.test_client()
    resp = client.get("/student")
    assert resp.status_code == 200
    html = resp.data.decode("utf-8")

    assert 'id="student-back-btn"' in html
    assert 'onclick="handleStudentBack()"' in html
    assert "cp-m-back-btn" in html
    assert 'addEventListener("popstate"' in html
    assert "initStudentHistoryGuard" in html

    css_path = Path(__file__).resolve().parents[1] / "app" / "static" / "css" / "classpulse.css"
    css_text = css_path.read_text(encoding="utf-8")
    assert ".cp-m-back-btn" in css_text
    assert "@media (min-width: 769px)" not in css_text


def test_student_app_removes_join_button_and_loading_text(mobile_app) -> None:
    """Mobile Student App omits the legacy auto-join bar & 'Loading ...' placeholders while keeping manual course-number join."""
    from app.services.course_service import CourseService

    CourseService(mobile_app.extensions["db_session"]).ensure_default_school_courses()
    client = mobile_app.test_client()
    _sign_in_student(client, email="cleanui@ariel.ac.il")

    resp = client.get("/student")
    assert resp.status_code == 200
    html = resp.data.decode("utf-8")

    assert 'id="join-code-input"' not in html
    assert "joinCourseByCode" not in html
    assert "Join course by code" not in html
    assert 'id="join-course-code-input"' in html
    assert "Loading course assignments..." not in html
    assert "Loading lecture summaries..." not in html

    # Backend /api/student/enroll remains available for API compatibility.
    enroll_resp = client.post("/api/student/enroll", json={"course_code": "CS204"})
    assert enroll_resp.status_code == 200


def test_student_app_uses_unified_date_grouped_lecture_list(mobile_app) -> None:
    """Student App must render card-based progressive disclosure screens & date-grouped lectures."""
    client = mobile_app.test_client()
    resp = client.get("/student")
    assert resp.status_code == 200
    html = resp.data.decode("utf-8")

    for screen_id in (
        'id="screen-courses"',
        'id="screen-course-hub"',
        'id="screen-course-lectures"',
        'id="screen-course-attendance"',
        'id="screen-course-assignments"',
        'id="screen-assignment-detail"',
        'id="screen-course-summaries"',
        'id="screen-course-info"',
        'id="screen-lecture"',
    ):
        assert screen_id in html

    assert "sortCoursesByRecentLecture" in html
    assert "sortSessionsNewestFirst" in html
    assert "cp-m-date-group" in html
    assert "cp-m-lecture-row" in html
    # The old second duplicate log list loop must be gone.
    assert "c.my_attendance_logs" not in html


def test_student_app_decluttered_offline_question_and_upload_ui(mobile_app) -> None:
    """Student App omits LIVE clutter, supports Offline Questions & prominent assignment upload UI."""
    client = mobile_app.test_client()
    _sign_in_student(client, email="decluttered@ariel.ac.il")
    html = client.get("/student").data.decode("utf-8")

    for forbidden in (
        "LIVE SESSION",
        "LIVE CLASS",
        "Live Class",
        "Vote Recorded • Live",
        "LIVE — Tap to Vote",
        "LIVE &mdash; Tap to Vote",
    ):
        assert forbidden not in html

    assert "sendOfflineQuestion" in html
    assert "Offline Question" in html
    assert "renderAssignmentDetailScreen" in html
    assert "cp-upload-dropzone" in html
    assert 'id="course-info-number"' in html



