from __future__ import annotations

from typing import Any

from flask import current_app, request
from flask_socketio import emit

from app import socketio
from app.models import ClassSession, Student
from app.services.chat_service import ChatError, ChatService
from app.services.qr_service import DeviceBindingManager


def register_chat_events(sio) -> None:
    sio.on_event("chat:send", handle_socket_chat_send)
    sio.on_event("chat:upvote", handle_socket_chat_upvote)


@socketio.on("chat:send")
def handle_socket_chat_send(data: dict[str, Any] | None) -> None:
    payload = data or {}
    session_code = str(payload.get("session_code", "")).strip().upper()
    body = str(payload.get("body", "")).strip()
    category = str(payload.get("category", "Question")).strip()
    if not session_code or not body:
        return

    db = current_app.extensions["db_session"]
    device_mgr: DeviceBindingManager = current_app.extensions["device_mgr"]

    session = db.query(ClassSession).filter_by(session_code=session_code).one_or_none()
    if session is None:
        return

    student = None
    parsed = device_mgr.parse_device_cookie(
        request.cookies.get(DeviceBindingManager.COOKIE_NAME)
    )
    if parsed is not None:
        student = db.get(Student, parsed[1])
    elif payload.get("student_id") is not None:
        student = db.get(Student, int(payload["student_id"]))

    author_name = (
        student.full_name
        if student is not None
        else str(payload.get("author_name", "Student")).strip()
    )

    svc = ChatService(db=db)
    try:
        msg = svc.post_message(
            session_id=session.id,
            author_name=author_name,
            body=body,
            category=category,
            student_id=student.id if student else None,
        )
    except ChatError as exc:
        emit("chat:error", {"error": exc.message})
        return

    serialized = svc.serialize_message(msg)
    socketio.emit("chat:new", serialized, to=f"session_{session.session_code}")


@socketio.on("chat:upvote")
def handle_socket_chat_upvote(data: dict[str, Any] | None) -> None:
    payload = data or {}
    message_id = payload.get("message_id")
    if message_id is None:
        return

    db = current_app.extensions["db_session"]
    svc = ChatService(db=db)
    try:
        msg = svc.upvote_message(int(message_id))
    except ChatError:
        return

    serialized = svc.serialize_message(msg)
    session = db.get(ClassSession, msg.session_id)
    if session is not None:
        socketio.emit("chat:updated", serialized, to=f"session_{session.session_code}")
