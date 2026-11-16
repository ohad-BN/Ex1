from __future__ import annotations

from typing import Optional

from flask import Blueprint, Response, current_app, jsonify, render_template, request

from app import socketio
from app.models import ClassSession, Student
from app.services.chat_service import ChatError, ChatService
from app.services.qr_service import DeviceBindingManager

chat_bp = Blueprint("chat", __name__)


def _get_chat_service() -> ChatService:
    db = current_app.extensions["db_session"]
    return ChatService(db=db)


def _resolve_student_from_cookie() -> Optional[Student]:
    db = current_app.extensions["db_session"]
    device_mgr: DeviceBindingManager = current_app.extensions["device_mgr"]
    cookie_raw = request.cookies.get(DeviceBindingManager.COOKIE_NAME)
    parsed = device_mgr.parse_device_cookie(cookie_raw)
    if parsed is None:
        return None
    return db.get(Student, parsed[1])


@chat_bp.get("/api/sessions/<int:session_id>/chat")
def list_chat_messages(session_id: int):
    svc = _get_chat_service()
    return jsonify({"messages": svc.list_messages(session_id)})


@chat_bp.post("/api/sessions/<int:session_id>/chat")
def post_chat_message(session_id: int):
    db = current_app.extensions["db_session"]
    svc = _get_chat_service()
    data = request.get_json(silent=True) or request.form.to_dict()

    student = _resolve_student_from_cookie()
    if student is None and data.get("student_id"):
        student = db.get(Student, int(data["student_id"]))

    author_name = (
        student.full_name
        if student is not None
        else str(data.get("author_name", "Student")).strip()
    )
    category = str(data.get("category", "Question")).strip()
    body = str(data.get("body", "")).strip()

    try:
        msg = svc.post_message(
            session_id=session_id,
            author_name=author_name,
            body=body,
            category=category,
            student_id=student.id if student else None,
        )
    except ChatError as exc:
        return jsonify({"error": exc.message}), exc.status_code

    serialized = svc.serialize_message(msg)
    session = db.get(ClassSession, session_id)
    if session is not None:
        socketio.emit("chat:new", serialized, to=f"session_{session.session_code}")

    return jsonify(serialized), 201


@chat_bp.post("/api/chat/<int:message_id>/upvote")
def upvote_chat_message(message_id: int):
    db = current_app.extensions["db_session"]
    svc = _get_chat_service()
    try:
        msg = svc.upvote_message(message_id)
    except ChatError as exc:
        return jsonify({"error": exc.message}), exc.status_code

    serialized = svc.serialize_message(msg)
    session = db.get(ClassSession, msg.session_id)
    if session is not None:
        socketio.emit("chat:updated", serialized, to=f"session_{session.session_code}")
    return jsonify(serialized)


@chat_bp.post("/api/chat/<int:message_id>/pin")
def pin_chat_message(message_id: int):
    db = current_app.extensions["db_session"]
    svc = _get_chat_service()
    try:
        msg = svc.toggle_pin(message_id)
    except ChatError as exc:
        return jsonify({"error": exc.message}), exc.status_code

    serialized = svc.serialize_message(msg)
    session = db.get(ClassSession, msg.session_id)
    if session is not None:
        socketio.emit("chat:updated", serialized, to=f"session_{session.session_code}")
    return jsonify(serialized)


@chat_bp.get("/api/sessions/<int:session_id>/chat/export")
def export_chat_timeline(session_id: int):
    db = current_app.extensions["db_session"]
    session = db.get(ClassSession, session_id)
    if session is None:
        return jsonify({"error": "Session not found"}), 404

    svc = _get_chat_service()
    text_out = svc.export_timeline_text(session_id)
    filename = f"ClassPulse_{session.session_code}_Chat_Timeline.txt"
    return Response(
        text_out.encode("utf-8"),
        mimetype="text/plain; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@chat_bp.get("/lecturer/chat-popout/<session_code>")
def chat_popout_view(session_code: str):
    db = current_app.extensions["db_session"]
    code = session_code.strip().upper()
    session = db.query(ClassSession).filter_by(session_code=code).one_or_none()
    if session is None:
        return "Session not found", 404
    svc = _get_chat_service()
    messages = svc.list_messages(session.id)
    return render_template(
        "chat_popout.html",
        session=session,
        messages=messages,
    )
