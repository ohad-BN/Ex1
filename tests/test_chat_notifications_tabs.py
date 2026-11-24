from __future__ import annotations

from pathlib import Path
import sqlite3

import pytest

from app import create_app, socketio
from app.config import Config
from app.services.face_counter import BoundingBox, FaceDetector
from app.services.qr_service import RollingQRService


class StubDetector(FaceDetector):
    def detect(self, image) -> list[BoundingBox]:
        return []


@pytest.fixture()
def notify_app(tmp_path: Path):
    db_file = tmp_path / "test_chat_notify.db"
    cfg = Config(
        SECRET_KEY="chat-notify-test-secret-32chars-min",
        DATABASE_URL=f"sqlite:///{db_file.as_posix()}",
    )
    app = create_app(cfg, face_detector=StubDetector())
    app.config["TESTING"] = True
    yield app
    app.extensions["task_manager"].shutdown(wait=True)
    app.extensions["db_engine"].dispose()


def test_legacy_synced_to_zoom_column_dropped_on_startup(tmp_path: Path) -> None:
    """Verify legacy chat_messages.synced_to_zoom NOT NULL column is dropped so chat inserts succeed (201)."""
    db_file = tmp_path / "legacy_zoom.db"
    conn = sqlite3.connect(str(db_file))
    conn.execute(
        """
        CREATE TABLE class_sessions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            course_id INTEGER,
            session_code VARCHAR(16) NOT NULL UNIQUE,
            title VARCHAR(200) NOT NULL,
            started_at DATETIME NOT NULL,
            attendance_open BOOLEAN NOT NULL DEFAULT 1,
            late_after_minutes INTEGER NOT NULL DEFAULT 15,
            headcount_tolerance INTEGER NOT NULL DEFAULT 2,
            zoom_meeting_id VARCHAR(64),
            zoom_auto_sync BOOLEAN NOT NULL DEFAULT 0
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE chat_messages (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            session_id INTEGER NOT NULL,
            student_id INTEGER,
            author_name VARCHAR(120) NOT NULL,
            category VARCHAR(32) NOT NULL DEFAULT 'Question',
            body TEXT NOT NULL,
            offset_seconds INTEGER NOT NULL DEFAULT 0,
            upvotes INTEGER NOT NULL DEFAULT 0,
            is_pinned BOOLEAN NOT NULL DEFAULT 0,
            synced_to_zoom BOOLEAN NOT NULL,
            created_at DATETIME NOT NULL
        )
        """
    )
    conn.execute(
        "INSERT INTO class_sessions (session_code, title, started_at) VALUES ('LEGACY1', 'Old Session', '2026-11-15 10:00:00')"
    )
    conn.commit()
    conn.close()

    cfg = Config(
        SECRET_KEY="legacy-migration-secret-32chars!",
        DATABASE_URL=f"sqlite:///{db_file.as_posix()}",
    )
    app = create_app(cfg, face_detector=StubDetector())
    app.config["TESTING"] = True
    try:
        client = app.test_client()
        resp = client.post(
            "/api/sessions/1/chat",
            json={"author_name": "Ohad", "category": "Question", "body": "Legacy DB fixed!"},
        )
        assert resp.status_code == 201
        assert resp.get_json()["body"] == "Legacy DB fixed!"
    finally:
        app.extensions["task_manager"].shutdown(wait=True)
        app.extensions["db_engine"].dispose()


def test_cross_lecture_chat_notifications_and_lecturer_attribution(notify_app) -> None:
    """Verify student & lecturer receive chat:notify payloads across lectures with real attribution & export."""
    prof = notify_app.test_client()
    student = notify_app.test_client()
    qr: RollingQRService = notify_app.extensions["qr_service"]

    assert (
        prof.post(
            "/api/auth/lecturer/register",
            json={
                "full_name": "Prof. Boaz Ben-Moshe",
                "username": "boaz.chat",
                "email": "boaz_chat@ariel.ac.il",
                "password": "Password1!",
            },
        ).status_code
        == 201
    )

    course = prof.post(
        "/api/courses",
        json={
            "course_code": "CS500",
            "name": "Chat Notifications Course",
            "initial_lecture_title": "Lecture 1 — Intro",
        },
    ).get_json()
    cid = course["id"]
    l1_code = course["sessions"][0]["session_code"]

    l2 = prof.post(
        f"/api/courses/{cid}/sessions",
        json={"title": "Lecture 2 — Sockets", "session_code": "C500L2"},
    ).get_json()
    l2_id = l2["id"]

    # Student checks into Lecture 2 via QR -> enrolled in course CS500
    b = qr.compute_bucket()
    sig = qr.sign_token("C500L2", b)
    ck = student.post(
        "/api/attendance/checkin",
        json={
            "session_code": "C500L2",
            "t": b,
            "sig": sig,
            "full_name": "Alice Cohen",
            "student_id_number": "209111222",
            "email": "alice_chat@ariel.ac.il",
            "device_id": "alice-phone-chat",
        },
    )
    assert ck.status_code == 200

    # Lecturer watches Lecture 1 (not Lecture 2!), student watches Lecture 2
    prof_sio = socketio.test_client(notify_app, flask_test_client=prof)
    prof_sio.emit("join_session", {"session_code": l1_code})
    prof_sio.emit("join_notifications", {})
    prof_sio.get_received()

    st_sio = socketio.test_client(notify_app, flask_test_client=student)
    st_sio.emit("join_session", {"session_code": "C500L2", "as_student": True})
    st_sio.emit("join_notifications", {})
    st_sio.get_received()

    # Student sends message in Lecture 2 -> Lecturer on Lecture 1 still gets chat:notify!
    post_st = student.post(
        f"/api/sessions/{l2_id}/chat",
        json={"category": "Question", "body": "Will notification jump to Lecture 2?"},
    )
    assert post_st.status_code == 201
    msg_data = post_st.get_json()
    assert msg_data["sender_role"] == "student"

    prof_events = prof_sio.get_received()
    notify_ev = next(e for e in prof_events if e["name"] == "chat:notify")
    payload = notify_ev["args"][0]
    assert payload["course_id"] == cid
    assert payload["course_code"] == "CS500"
    assert payload["session_code"] == "C500L2"
    assert payload["lecture_title"] == "Lecture 2 — Sockets"
    assert payload["message_id"] == msg_data["id"]

    # Lecturer posts reply in Lecture 2 -> attribution uses Lecturer's real name & student receives chat:notify!
    post_prof = prof.post(
        f"/api/sessions/{l2_id}/chat",
        json={"as_lecturer": True, "category": "Comment", "body": "Yes, clicking opens Lecture 2!"},
    )
    assert post_prof.status_code == 201
    prof_msg = post_prof.get_json()
    assert prof_msg["author_name"] == "Prof. Boaz Ben-Moshe"
    assert prof_msg["sender_role"] == "lecturer"

    st_events = st_sio.get_received()
    st_notifies = [e["args"][0] for e in st_events if e["name"] == "chat:notify"]
    prof_notify = next(p for p in st_notifies if p["message_id"] == prof_msg["id"])
    assert prof_notify["sender_role"] == "lecturer"
    assert prof_notify["author_name"] == "Prof. Boaz Ben-Moshe"

    # Export URL without .txt returns 200
    assert prof.get(f"/api/sessions/{l2_id}/chat/export").status_code == 200

    prof_sio.disconnect()
    st_sio.disconnect()


def test_teacher_portal_splits_tab5_and_includes_notifications(notify_app) -> None:
    """Verify Tab 5 was split into #tab-ai-lectures (Tab 5) and #tab-homework (Tab 6) with notification assets."""
    prof = notify_app.test_client()
    prof.post(
        "/api/auth/lecturer/register",
        json={
            "full_name": "Prof. Split",
            "username": "prof.split",
            "email": "split@ariel.ac.il",
            "password": "Password1!",
        },
    )
    prof.post("/api/courses", json={"course_code": "UI600", "name": "Workspace Split"})
    html = prof.get("/teacher")
    assert html.status_code == 200
    assert b'id="tab-ai-lectures"' in html.data
    assert b'id="tab-homework"' in html.data
    assert b'id="tab-chat-badge"' in html.data
    assert b"/static/js/notifications.js" in html.data
    assert b'id="tab-ai-hub"' not in html.data
    assert b"/chat/export.txt" not in html.data


def test_offline_question_category_and_lecturer_sidebar_redesign(notify_app) -> None:
    """Phase 5: Verify Offline Question category, Lecturer sidebar, Course Details isolation & clean copy."""
    prof = notify_app.test_client()
    prof.post(
        "/api/auth/lecturer/register",
        json={
            "full_name": "Prof. Sidebar",
            "username": "prof.sidebar",
            "email": "sidebar@ariel.ac.il",
            "password": "Password1!",
        },
    )
    c = prof.post(
        "/api/courses",
        json={"course_code": "COURSE77", "name": "Software Testing Course"},
    ).get_json()
    sid = c["sessions"][0]["id"]

    # Offline Question category is accepted and serialized cleanly
    oq_resp = prof.post(
        f"/api/sessions/{sid}/chat",
        json={
            "author_name": "Ohad Ben Nun",
            "category": "Offline Question",
            "body": "Can you clarify slide 14 before next week?",
        },
    )
    assert oq_resp.status_code == 201
    oq = oq_resp.get_json()
    assert oq["category"] == "Offline Question"
    assert oq["course_name"] == "Software Testing Course"

    portal_html = prof.get(f"/teacher?course_id={c['id']}").data.decode("utf-8")
    for expected in (
        'id="lecturer-back-btn"',
        'id="tab-overview"',
        'id="tab-lectures"',
        'id="tab-course-details"',
        "Offline Questions",
        "COURSE77",
    ):
        assert expected in portal_html

    for forbidden in ("LIVE SESSION", "LIVE ON STUDENT PHONES"):
        assert forbidden not in portal_html

    import re

    # Course number appears inside #tab-course-details, NOT visibly in the topbar header
    topbar_segment = portal_html.split('<header class="cp-topbar', 1)[1].split("</header>", 1)[0]
    topbar_visible = re.sub(r"<[^>]+>", " ", topbar_segment)
    assert "COURSE77" not in topbar_visible
    assert "cp-code-pill" not in topbar_segment
