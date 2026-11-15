from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from app import create_app, socketio
from app.config import Config
from app.models import ChatMessage, ClassSession, Student
from app.services.chat_service import ChatService
from app.services.face_counter import BoundingBox, FaceDetector
from app.services.zoom_bridge import ZoomBridgeAdapter, ZoomOAuthRestBridge


class StubDetector(FaceDetector):
    def detect(self, image) -> list[BoundingBox]:
        return []


class SpyZoomBridge(ZoomBridgeAdapter):
    """Spy adapter recording dispatched chat messages."""

    def __init__(self, should_succeed: bool = True) -> None:
        self.should_succeed = should_succeed
        self.sent: list[tuple[str, str, str]] = []

    def is_configured(self) -> bool:
        return True

    def dispatch_chat_message(
        self, msg: ChatMessage, offset_mmss: str, session_code: str
    ) -> bool:
        if not self.should_succeed:
            return False
        self.sent.append((session_code, offset_mmss, msg.body))
        return True

    def fetch_cloud_recording(self, meeting_id: str, dest_dir: str | Path) -> str | None:
        return None


@pytest.fixture()
def chat_app(tmp_path: Path):
    db_file = tmp_path / "test_phase6_chat.db"
    cfg = Config(
        SECRET_KEY="phase6-chat-secret",
        DATABASE_URL=f"sqlite:///{db_file.as_posix()}",
    )
    spy_zoom = SpyZoomBridge(should_succeed=True)
    app = create_app(cfg, face_detector=StubDetector(), zoom_bridge=spy_zoom)
    app.config["TESTING"] = True
    yield app, spy_zoom
    app.extensions["task_manager"].shutdown(wait=True)
    app.extensions["db_engine"].dispose()


def test_chat_lecture_relative_timestamp_upvotes_pin_and_export(chat_app) -> None:
    """Suite 5: Verify +MM:SS lecture offset calculation, upvotes, pinning, and .txt export."""
    app, spy_zoom = chat_app
    db = app.extensions["db_session"]
    client = app.test_client()

    # Create a session that started 15 minutes and 30 seconds ago (930s)
    t_start = datetime(2026, 11, 15, 10, 0, 0, tzinfo=timezone.utc)
    t_msg = t_start + timedelta(minutes=15, seconds=30)

    cs = ClassSession(session_code="CHAT1", title="Chat & Zoom Lecture", started_at=t_start)
    st = Student(full_name="Ohad", email="ohad@ariel.ac.il", primary_device_id="d-ohad")
    db.add_all([cs, st])
    db.commit()

    svc = ChatService(db=db, zoom_bridge=spy_zoom)
    msg = svc.post_message(
        session_id=cs.id,
        author_name=st.full_name,
        body="Can we use Dependency Injection for the Zoom adapter?",
        category="Question",
        student_id=st.id,
        now_ts=t_msg.timestamp(),
    )
    assert msg.offset_seconds == 930
    assert msg.synced_to_zoom is True
    assert spy_zoom.sent == [
        ("CHAT1", "+15:30", "Can we use Dependency Injection for the Zoom adapter?")
    ]

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

    # Verify Zoom Companion Popout view renders cleanly
    pop_resp = client.get("/lecturer/chat-popout/CHAT1")
    assert pop_resp.status_code == 200
    assert b"Zoom Companion Feed" in pop_resp.data


def test_zoom_api_failure_does_not_break_classroom_chat(tmp_path: Path) -> None:
    """Suite 5: Verify a broken/unreachable Zoom OAuth endpoint never breaks classroom chat."""
    db_file = tmp_path / "test_zoom_fail.db"
    cfg = Config(DATABASE_URL=f"sqlite:///{db_file.as_posix()}")

    # Configure ZoomOAuthRestBridge with unreachable/invalid credentials
    broken_zoom = ZoomOAuthRestBridge(
        account_id="invalid",
        client_id="invalid",
        client_secret="invalid",
        team_channel_id="ch-123",
        timeout_sec=0.2,
    )
    app = create_app(cfg, face_detector=StubDetector(), zoom_bridge=broken_zoom)
    app.config["TESTING"] = True
    client = app.test_client()

    s_resp = client.post(
        "/api/sessions", json={"title": "Resilient Chat", "session_code": "RES1"}
    ).get_json()
    sid = s_resp["id"]

    post_resp = client.post(
        f"/api/sessions/{sid}/chat",
        json={
            "author_name": "Einav",
            "category": "Suggestion",
            "body": "Chat works even when Zoom API is unreachable!",
        },
    )
    assert post_resp.status_code == 201
    payload = post_resp.get_json()
    assert payload["synced_to_zoom"] is False
    assert payload["body"] == "Chat works even when Zoom API is unreachable!"

    app.extensions["task_manager"].shutdown(wait=True)
    app.extensions["db_engine"].dispose()


def test_socketio_chat_send_and_upvote_broadcast(chat_app) -> None:
    """Suite 5: Verify real-time Socket.IO chat:send and chat:upvote events."""
    app, _spy = chat_app
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
