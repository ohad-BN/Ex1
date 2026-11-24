from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from app import create_app, socketio
from app.config import Config
from app.models import ClassSession, Student
from app.services.chat_service import ChatService
from app.services.face_counter import BoundingBox, FaceDetector


class StubDetector(FaceDetector):
    def detect(self, image) -> list[BoundingBox]:
        return []


@pytest.fixture()
def chat_app(tmp_path: Path):
    db_file = tmp_path / "test_phase6_chat.db"
    cfg = Config(
        SECRET_KEY="phase6-chat-secret",
        DATABASE_URL=f"sqlite:///{db_file.as_posix()}",
        GEMINI_API_KEY="",
    )
    app = create_app(cfg, face_detector=StubDetector())
    app.config["TESTING"] = True
    yield app
    app.extensions["task_manager"].shutdown(wait=True)
    app.extensions["db_engine"].dispose()


def test_chat_lecture_relative_timestamp_upvotes_pin_and_export(chat_app) -> None:
    """Suite 5: Verify +MM:SS lecture offset calculation, upvotes, pinning, and .txt export."""
    app = chat_app
    db = app.extensions["db_session"]
    client = app.test_client()

    t_start = datetime(2026, 11, 15, 10, 0, 0, tzinfo=timezone.utc)
    t_msg = t_start + timedelta(minutes=15, seconds=30)

    cs = ClassSession(session_code="CHAT1", title="Classroom Chat Lecture", started_at=t_start)
    st = Student(full_name="Ohad", email="ohad@ariel.ac.il", primary_device_id="d-ohad")
    db.add_all([cs, st])
    db.commit()

    svc = ChatService(db=db)
    msg = svc.post_message(
        session_id=cs.id,
        author_name=st.full_name,
        body="Can we use Dependency Injection for the AI client adapter?",
        category="Question",
        student_id=st.id,
        now_ts=t_msg.timestamp(),
    )
    assert msg.offset_seconds == 930

    # Upvote twice & pin via HTTP routes
    client.post(f"/api/chat/{msg.id}/upvote")
    r_up = client.post(f"/api/chat/{msg.id}/upvote").get_json()
    assert r_up["upvotes"] == 2
    assert r_up["offset_mmss"] == "+15:30"

    r_pin = client.post(f"/api/chat/{msg.id}/pin").get_json()
    assert r_pin["is_pinned"] is True

    # Verify timeline export (.txt) for Feature 4 lecture summary consumption
    exp_resp = client.get(f"/api/sessions/{cs.id}/chat/export")
    assert exp_resp.status_code == 200
    txt = exp_resp.data.decode("utf-8")
    assert "[+15:30] [Question] [PINNED] Ohad:" in txt
    assert "(+2 upvotes)" in txt

    # Verify Classroom Q&A Popout view renders cleanly
    pop_resp = client.get("/lecturer/chat-popout/CHAT1")
    assert pop_resp.status_code == 200
    assert b"Classroom Q&amp;A Feed" in pop_resp.data


def test_rest_chat_post_and_validation(chat_app) -> None:
    """Suite 5: Verify REST chat submission and empty-message rejection."""
    client = chat_app.test_client()
    s_resp = client.post(
        "/api/sessions", json={"title": "Native Chat", "session_code": "RES1"}
    ).get_json()
    sid = s_resp["id"]

    bad_resp = client.post(f"/api/sessions/{sid}/chat", json={"body": "   "})
    assert bad_resp.status_code == 400

    post_resp = client.post(
        f"/api/sessions/{sid}/chat",
        json={
            "author_name": "Einav",
            "category": "Suggestion",
            "body": "Native classroom chat works seamlessly!",
        },
    )
    assert post_resp.status_code == 201
    payload = post_resp.get_json()
    assert payload["body"] == "Native classroom chat works seamlessly!"


def test_socketio_chat_send_and_upvote_broadcast(chat_app) -> None:
    """Suite 5: Verify real-time Socket.IO chat:send and chat:upvote events."""
    app = chat_app
    db = app.extensions["db_session"]
    cs = ClassSession(session_code="LIVECHAT", title="Socket Chat")
    db.add(cs)
    db.commit()

    sio = socketio.test_client(app)
    sio.emit("join_session", {"session_code": "LIVECHAT"})
    sio.get_received()

    sio.emit(
        "chat:send",
        {
            "session_code": "LIVECHAT",
            "author_name": "Student X",
            "category": "Question",
            "body": "How does WAL mode handle concurrency?",
        },
    )
    events = sio.get_received()
    new_ev = next(e for e in events if e["name"] == "chat:new")
    msg_data = new_ev["args"][0]
    assert msg_data["author_name"] == "Student X"
    assert msg_data["upvotes"] == 0

    sio.emit("chat:upvote", {"message_id": msg_data["id"]})
    up_events = sio.get_received()
    upd_ev = next(e for e in up_events if e["name"] == "chat:updated")
    assert upd_ev["args"][0]["upvotes"] == 1
    sio.disconnect()
