from __future__ import annotations

from typing import Any

from flask import current_app, request
from flask_socketio import emit

from app import socketio
from app.models import ClassSession, Poll
from app.services.poll_service import PollError, PollService
from app.services.qr_service import DeviceBindingManager


def register_poll_events(sio) -> None:
    sio.on_event("poll:request_sync", handle_poll_request_sync)
    sio.on_event("poll:vote", handle_socket_poll_vote)


@socketio.on("poll:request_sync")
def handle_poll_request_sync(data: dict[str, Any] | None) -> None:
    payload = data or {}
    session_code = str(payload.get("session_code", "")).strip().upper()
    if not session_code:
        return

    db = current_app.extensions["db_session"]
    device_mgr: DeviceBindingManager = current_app.extensions["device_mgr"]

    session = db.query(ClassSession).filter_by(session_code=session_code).one_or_none()
    if session is None:
        return

    parsed = device_mgr.parse_device_cookie(
        request.cookies.get(DeviceBindingManager.COOKIE_NAME)
    )
    if parsed is not None:
        student_id = parsed[1]
    elif session.course_id is None:
        student_id = payload.get("student_id")
    else:
        student_id = None

    svc = PollService(db)
    sync_data = svc.build_sync_payload(
        session.id, student_id=int(student_id) if student_id is not None else None
    )
    emit("poll:sync", sync_data)


@socketio.on("poll:vote")
def handle_socket_poll_vote(data: dict[str, Any] | None) -> None:
    payload = data or {}
    poll_id = payload.get("poll_id")
    option_index = payload.get("option_index")
    if poll_id is None or option_index is None:
        return

    db = current_app.extensions["db_session"]
    device_mgr: DeviceBindingManager = current_app.extensions["device_mgr"]

    poll_target = db.get(Poll, int(poll_id))
    poll_sess = db.get(ClassSession, poll_target.session_id) if poll_target else None
    parsed = device_mgr.parse_device_cookie(
        request.cookies.get(DeviceBindingManager.COOKIE_NAME)
    )
    if parsed is not None:
        student_id = parsed[1]
    elif poll_sess is None or poll_sess.course_id is None:
        student_id = payload.get("student_id")
    else:
        student_id = None

    if student_id is None:
        emit("poll:error", {"error": "Student check-in required before voting."})
        return

    svc = PollService(db)
    try:
        serialized, is_new = svc.cast_vote(
            poll_id=int(poll_id),
            student_id=int(student_id),
            option_index=int(option_index),
        )
    except PollError as exc:
        emit("poll:error", {"error": exc.message})
        return

    # Send personal confirmation back to voter
    serialized["is_new_vote"] = is_new
    emit("poll:voted_ack", serialized)

    # Broadcast updated counts to the entire classroom room
    poll = db.get(Poll, int(poll_id))
    session = db.get(ClassSession, poll.session_id) if poll else None
    if session is not None:
        public_tally = svc.serialize_poll(poll)
        socketio.emit(
            "poll:results_update",
            public_tally,
            to=f"session_{session.session_code}",
        )
