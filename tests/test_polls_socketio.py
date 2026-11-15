from __future__ import annotations

from pathlib import Path

import pytest

from app import create_app, socketio
from app.config import Config
from app.models import ClassSession, Student
from app.services.face_counter import BoundingBox, FaceDetector


class StubDetector(FaceDetector):
    def detect(self, image) -> list[BoundingBox]:
        return []


@pytest.fixture()
def poll_app(tmp_path: Path):
    db_file = tmp_path / "test_phase5_polls.db"
    cfg = Config(
        SECRET_KEY="phase5-poll-secret",
        DATABASE_URL=f"sqlite:///{db_file.as_posix()}",
    )
    app = create_app(cfg, face_detector=StubDetector())
    app.config["TESTING"] = True
    yield app
    app.extensions["task_manager"].shutdown(wait=True)
    app.extensions["db_engine"].dispose()


def test_preclass_poll_bank_lifecycle_and_quick_launch(poll_app) -> None:
    """Suite 4: Create pre-class DRAFT polls, launch one, and verify auto-close on next launch."""
    client = poll_app.test_client()
    s_resp = client.post(
        "/api/sessions", json={"title": "Polls Lecture", "session_code": "POLL1"}
    ).get_json()
    sid = s_resp["id"]

    # 1. Create a pre-class DRAFT poll in Poll Bank
    p1 = client.post(
        f"/api/sessions/{sid}/polls",
        json={
            "question": "Which pattern decouples abstraction from implementation?",
            "options": ["Bridge", "Singleton", "Memento"],
            "correct_index": 0,
            "time_limit_sec": 45,
        },
    )
    assert p1.status_code == 201
    p1_data = p1.get_json()
    assert p1_data["status"] == "DRAFT"

    # 2. Launch Poll #1
    l1 = client.post(f"/api/polls/{p1_data['id']}/launch")
    assert l1.status_code == 200
    assert l1.get_json()["status"] == "ACTIVE"

    # 3. Launch a 1-click Quick Poll (launch_now=True) -> automatically closes Poll #1
    p2 = client.post(
        f"/api/sessions/{sid}/polls",
        json={
            "question": "Is the Bridge pattern clear?",
            "options": ["Yes", "Need another example"],
            "launch_now": True,
        },
    )
    assert p2.status_code == 201
    p2_data = p2.get_json()
    assert p2_data["status"] == "ACTIVE"

    bank = client.get(f"/api/sessions/{sid}/polls").get_json()["polls"]
    statuses = {item["id"]: item["status"] for item in bank}
    assert statuses[p1_data["id"]] == "CLOSED"
    assert statuses[p2_data["id"]] == "ACTIVE"


def test_single_vote_per_student_idempotency(poll_app) -> None:
    """Suite 4: Verify duplicate taps by the same student only count once."""
    client = poll_app.test_client()
    db = poll_app.extensions["db_session"]

    cs = ClassSession(session_code="VOTE1", title="Voting Test")
    st1 = Student(full_name="Alice", email="alice@ariel.ac.il", primary_device_id="d1")
    st2 = Student(full_name="Bob", email="bob@ariel.ac.il", primary_device_id="d2")
    db.add_all([cs, st1, st2])
    db.commit()

    poll_resp = client.post(
        f"/api/sessions/{cs.id}/polls",
        json={
            "question": "Pick an option",
            "options": ["Opt A", "Opt B", "Opt C"],
            "launch_now": True,
        },
    ).get_json()
    pid = poll_resp["id"]

    # Alice votes for Option 1 (Opt B)
    v1 = client.post(
        f"/api/polls/{pid}/vote",
        json={"student_id": st1.id, "option_index": 1},
    ).get_json()
    assert v1["is_new_vote"] is True
    assert v1["counts"] == [0, 1, 0]
    assert v1["already_voted"] is True
    assert v1["my_vote_index"] == 1

    # Alice taps again on Option 2 -> ignored idempotently!
    v_dup = client.post(
        f"/api/polls/{pid}/vote",
        json={"student_id": st1.id, "option_index": 2},
    ).get_json()
    assert v_dup["is_new_vote"] is False
    assert v_dup["counts"] == [0, 1, 0]
    assert v_dup["my_vote_index"] == 1

    # Bob votes for Option 0 (Opt A)
    v2 = client.post(
        f"/api/polls/{pid}/vote",
        json={"student_id": st2.id, "option_index": 0},
    ).get_json()
    assert v2["is_new_vote"] is True
    assert v2["counts"] == [1, 1, 0]
    assert v2["percentages"] == [50.0, 50.0, 0.0]


def test_socketio_poll_vote_and_reconnect_hydration(poll_app) -> None:
    """Suite 4: Verify Socket.IO poll:vote and poll:sync reconnect state hydration."""
    db = poll_app.extensions["db_session"]
    cs = ClassSession(session_code="SYNC1", title="Reconnect Hydration")
    st = Student(full_name="Dana", email="dana@ariel.ac.il", primary_device_id="dev-dana")
    db.add_all([cs, st])
    db.commit()

    http_client = poll_app.test_client()
    poll_data = http_client.post(
        f"/api/sessions/{cs.id}/polls",
        json={
            "question": "Does Socket.IO reconnect hydrate state?",
            "options": ["Yes", "No"],
            "launch_now": True,
        },
    ).get_json()
    pid = poll_data["id"]

    # Connect Socket.IO client, join room, and cast vote via WebSocket
    sio = socketio.test_client(poll_app)
    sio.emit("join_session", {"session_code": "SYNC1", "student_id": st.id})
    sio.get_received()  # clear initial join events

    sio.emit(
        "poll:vote",
        {"poll_id": pid, "option_index": 0, "student_id": st.id},
    )
    events = sio.get_received()
    event_names = [e["name"] for e in events]
    assert "poll:voted_ack" in event_names
    assert "poll:results_update" in event_names
    sio.disconnect()

    # Simulate mobile screen wake-up & Socket.IO reconnection
    sio_reconnected = socketio.test_client(poll_app)
    sio_reconnected.emit("join_session", {"session_code": "SYNC1", "student_id": st.id})
    reconnect_events = sio_reconnected.get_received()
    sync_ev = next(e for e in reconnect_events if e["name"] == "poll:sync")
    sync_payload = sync_ev["args"][0]

    assert sync_payload["has_active_poll"] is True
    assert sync_payload["poll"]["id"] == pid
    assert sync_payload["poll"]["already_voted"] is True
    assert sync_payload["poll"]["my_vote_index"] == 0
    assert sync_payload["poll"]["counts"] == [1, 0]
    sio_reconnected.disconnect()


def test_live_poll_broadcast_and_overview_sync(poll_app) -> None:
    """Phase 12A/12B: Verify dual Socket.IO poll events (poll:activated + poll:opened), saved lecture polls, and /api/sessions/<id>/overview live sync."""
    client = poll_app.test_client()
    db = poll_app.extensions["db_session"]

    cs = ClassSession(session_code="LIVE01", title="Live Poll & Overview Session")
    db.add(cs)
    db.commit()

    # Launch a poll and verify both poll:activated and poll:opened are emitted
    sio = socketio.test_client(poll_app)
    sio.emit("join_session", {"session_code": "LIVE01"})
    sio.get_received()

    p_resp = client.post(
        f"/api/sessions/{cs.id}/polls",
        json={
            "question": "Is the live poll saved in this lecture?",
            "options": ["Yes", "No"],
            "launch_now": True,
        },
    ).get_json()
    pid = p_resp["id"]

    launch_events = [e["name"] for e in sio.get_received()]
    assert "poll:activated" in launch_events
    assert "poll:opened" in launch_events

    # Verify the poll is saved in the lecture's poll list so 'See Results' can display it anytime
    saved_polls = client.get(f"/api/sessions/{cs.id}/polls").get_json()["polls"]
    assert any(p["id"] == pid for p in saved_polls)

    # Verify /api/sessions/<id>/overview returns live attendance and audit verdict fields
    ov_resp = client.get(f"/api/sessions/{cs.id}/overview")
    assert ov_resp.status_code == 200
    ov_data = ov_resp.get_json()
    assert "roster_count" in ov_data["verdict"]
    assert "explanation" in ov_data["verdict"]
    sio.disconnect()
