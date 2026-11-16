from __future__ import annotations

from typing import Any

from flask import current_app, request
from flask_socketio import emit, join_room

from app import socketio
from app.models import AttendanceRecord, ClassSession, Course, CourseEnrollment
from app.routes.guards import get_current_lecturer, is_lecturer_authorized_for_course
from app.services.chat_service import ChatService
from app.services.poll_service import PollService
from app.services.qr_service import DeviceBindingManager, resolve_effective_base_url


def _is_socket_authorized_for_session(
    session: ClassSession, payload: dict[str, Any]
) -> tuple[bool, int | None]:
    """Verify lecturer, student, or projector access for owned course sessions.

    Returns (allowed, verified_student_id).
    """
    db = current_app.extensions["db_session"]
    device_mgr: DeviceBindingManager = current_app.extensions["device_mgr"]

    lecturer = get_current_lecturer()
    parsed_student = device_mgr.parse_device_cookie(
        request.cookies.get(DeviceBindingManager.COOKIE_NAME)
    )
    cookie_student_id = parsed_student[1] if parsed_student is not None else None

    course = db.get(Course, session.course_id) if session.course_id else None
    if course is None or course.lecturer_id is None:
        return True, cookie_student_id or payload.get("student_id")

    # Deny any lecturer trying to spy on another lecturer's course room
    if lecturer is not None:
        if not is_lecturer_authorized_for_course(db, course, lecturer.id):
            return False, None
        return True, cookie_student_id

    # Allow valid classroom projector token
    ptok = str(payload.get("projector_token", "")).strip()
    if ptok and device_mgr.verify_projector_token(ptok, session.session_code):
        return True, cookie_student_id

    # Allow authenticated student enrolled or checked into this course/session
    if cookie_student_id is not None:
        enrolled = (
            db.query(CourseEnrollment)
            .filter_by(course_id=course.id, student_id=cookie_student_id)
            .first()
        )
        checked = (
            db.query(AttendanceRecord)
            .filter_by(session_id=session.id, student_id=cookie_student_id)
            .first()
        )
        if enrolled is not None or checked is not None:
            return True, cookie_student_id

    return False, None


def register_session_events(sio) -> None:
    sio.on_event("join_session", handle_join_session)
    sio.on_event("qr:request_refresh", handle_qr_refresh)


@socketio.on("join_session")
def handle_join_session(data: dict[str, Any] | None) -> None:
    payload = data or {}
    session_code = str(payload.get("session_code", "")).strip().upper()
    if not session_code:
        return

    db = current_app.extensions["db_session"]
    qr_service = current_app.extensions["qr_service"]
    cfg = current_app.config["CLASSPULSE_CONFIG"]

    session = db.query(ClassSession).filter_by(session_code=session_code).one_or_none()
    if session is None:
        return

    allowed, verified_student_id = _is_socket_authorized_for_session(session, payload)
    if not allowed:
        emit("session:error", {"error": "Access denied for this classroom session."})
        return

    room = f"session_{session_code}"
    join_room(room)

    base_url = resolve_effective_base_url(cfg.PUBLIC_BASE_URL, request.host_url)
    qr_payload = qr_service.build_qr_payload(base_url, session_code)
    qr_payload["attendance_open"] = session.attendance_open
    emit("qr:rotate", qr_payload)

    qr_count = db.query(AttendanceRecord).filter_by(session_id=session.id).count()
    emit(
        "attendance:count_update",
        {"session_code": session_code, "qr_count": qr_count},
    )

    # Hydrate active poll state on connect/reconnect (Feature 3)
    student_id = verified_student_id

    poll_svc = PollService(db)
    emit(
        "poll:sync",
        poll_svc.build_sync_payload(
            session.id, student_id=int(student_id) if student_id is not None else None
        ),
    )

    # Hydrate chat feed on connect/reconnect (Feature 2)
    chat_svc = ChatService(db=db)
    emit("chat:init", {"messages": chat_svc.list_messages(session.id)})


@socketio.on("qr:request_refresh")
def handle_qr_refresh(data: dict[str, Any] | None) -> None:
    payload = data or {}
    session_code = str(payload.get("session_code", "")).strip().upper()
    if not session_code:
        return

    db = current_app.extensions["db_session"]
    qr_service = current_app.extensions["qr_service"]
    cfg = current_app.config["CLASSPULSE_CONFIG"]

    session = db.query(ClassSession).filter_by(session_code=session_code).one_or_none()
    if session is None:
        return

    allowed, _sid = _is_socket_authorized_for_session(session, payload)
    if not allowed:
        emit("session:error", {"error": "Access denied for QR refresh."})
        return

    base_url = resolve_effective_base_url(cfg.PUBLIC_BASE_URL, request.host_url)
    qr_payload = qr_service.build_qr_payload(base_url, session_code)
    qr_payload["attendance_open"] = session.attendance_open
    emit("qr:rotate", qr_payload)
