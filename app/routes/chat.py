from __future__ import annotations

from typing import Optional

from flask import Blueprint, Response, current_app, jsonify, redirect, render_template, request, url_for

from app import socketio
from app.models import ChatMessage, ClassSession, Course, Student
from app.routes.guards import (
    get_current_lecturer,
    is_lecturer_authorized_for_course,
    require_session_access,
    verify_session_read_access,
)
from app.services.chat_service import ChatError, ChatService
from app.services.qr_service import DeviceBindingManager

chat_bp = Blueprint("chat", __name__)


def _emit_chat_new_and_notify(
    svc: ChatService,
    session: ClassSession,
    serialized: dict,
    sender_lecturer_id: Optional[int] = None,
) -> None:
    """Broadcast chat:new to the lecture room and chat:notify to lecturer & course notification rooms."""
    notify_payload = {
        **serialized,
        "message_id": serialized["id"],
        "sender_lecturer_id": sender_lecturer_id,
        "sender_student_id": serialized.get("student_id"),
    }
    socketio.emit("chat:new", serialized, to=f"session_{session.session_code}")
    for lec_id in svc.get_course_lecturer_recipient_ids(session.id):
        socketio.emit("chat:notify", notify_payload, to=f"lecturer_notify_{lec_id}")
    if session.course_id is not None:
        socketio.emit("chat:notify", notify_payload, to=f"course_notify_{session.course_id}")


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
    _sess, _lec, _stu, err = verify_session_read_access(session_id, allow_open_attendance=True)
    if err is not None:
        return err
    svc = _get_chat_service()
    return jsonify({"messages": svc.list_messages(session_id)})


@chat_bp.post("/api/sessions/<int:session_id>/chat")
def post_chat_message(session_id: int):
    db = current_app.extensions["db_session"]
    svc = _get_chat_service()
    data = request.get_json(silent=True) or request.form.to_dict()

    session = db.get(ClassSession, session_id)
    if session is None:
        return jsonify({"error": "Session not found."}), 404

    lecturer = get_current_lecturer()
    course = db.get(Course, session.course_id) if session.course_id else None
    as_lecturer = str(data.get("as_lecturer", "")).lower() in ("1", "true", "yes")
    sender_lecturer_id: Optional[int] = None
    student: Optional[Student] = None

    if lecturer is not None and (
        as_lecturer
        or (
            _resolve_student_from_cookie() is None
            and data.get("student_id") is None
            and (course is None or course.lecturer_id is None or is_lecturer_authorized_for_course(db, course, lecturer.id))
        )
    ):
        author_name = lecturer.full_name
        sender_lecturer_id = lecturer.id
    else:
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
    if sender_lecturer_id is not None:
        serialized["sender_role"] = "lecturer"
    _emit_chat_new_and_notify(
        svc, session, serialized, sender_lecturer_id=sender_lecturer_id
    )

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
    target_msg = db.get(ChatMessage, message_id)
    if target_msg is None:
        return jsonify({"error": "Message not found."}), 404
    _sess, _lec, err = require_session_access(target_msg.session_id)
    if err is not None:
        return err

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
    session, _lec, _stu, err = verify_session_read_access(session_id, allow_open_attendance=True)
    if err is not None:
        return err
    assert session is not None

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
    _sess, _lec, err = require_session_access(session.id)
    if err is not None:
        if err[1] == 401:
            return redirect(url_for("courses.teacher_login_page"))
        return "Access denied: this lecture belongs to another lecturer.", 403
    svc = _get_chat_service()
    messages = svc.list_messages(session.id)
    return render_template(
        "chat_popout.html",
        session=session,
        messages=messages,
    )
