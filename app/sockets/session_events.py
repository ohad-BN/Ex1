from __future__ import annotations

from typing import Any

from flask import current_app, request
from flask_socketio import emit, join_room, leave_room

from app import socketio
from app.models import AttendanceRecord, ClassSession, Course, CourseEnrollment
from app.routes.guards import get_current_lecturer, is_lecturer_authorized_for_course
from app.services.chat_service import ChatService
from app.services.poll_service import PollService
from app.services.qr_service import DeviceBindingManager, resolve_effective_base_url


def register_session_events(sio) -> None:
    sio.on_event("connect", handle_socket_connect)
    sio.on_event("join_notifications", handle_join_notifications)
    sio.on_event("join_session", handle_join_session)
    sio.on_event("leave_session", handle_leave_session)
    sio.on_event("qr:request_refresh", handle_qr_refresh)


def _subscribe_user_notification_rooms() -> None:
    """Join personal lecturer or enrolled-course Socket.IO notification rooms based on cookies."""
    db = current_app.extensions["db_session"]
    device_mgr: DeviceBindingManager = current_app.extensions["device_mgr"]

    lecturer = get_current_lecturer()
    if lecturer is not None:
        join_room(f"lecturer_notify_{lecturer.id}")

    parsed_student = device_mgr.parse_device_cookie(
        request.cookies.get(DeviceBindingManager.COOKIE_NAME)
    )
    if parsed_student is not None:
        sid = parsed_student[1]
        room_course_ids: set[int] = set()
        enrs = db.query(CourseEnrollment).filter_by(student_id=sid, status="approved").all()
        for enr in enrs:
            room_course_ids.add(enr.course_id)
        att_rows = db.query(AttendanceRecord).filter_by(student_id=sid).all()
        for rec in att_rows:
            sess_obj = db.get(ClassSession, rec.session_id)
            if sess_obj and sess_obj.course_id is not None:
                room_course_ids.add(sess_obj.course_id)
        for cid_val in room_course_ids:
            join_room(f"course_notify_{cid_val}")


@socketio.on("connect")
def handle_socket_connect() -> None:
    _subscribe_user_notification_rooms()


@socketio.on("join_notifications")
def handle_join_notifications(_data: dict[str, Any] | None = None) -> None:
    _subscribe_user_notification_rooms()


@socketio.on("leave_session")
def handle_leave_session(data: dict[str, Any] | None) -> None:
    payload = data or {}
    session_code = str(payload.get("session_code", "")).strip().upper()
    if session_code:
        leave_room(f"session_{session_code}")


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

    # Deny another lecturer snooping when acting as lecturer (allow if also enrolled/open preview on student view)
    as_student_preview = bool(payload.get("as_student"))
    if lecturer is not None and not as_student_preview:
        if not is_lecturer_authorized_for_course(db, course, lecturer.id):
            return False, None
        return True, cookie_student_id

    # Allow valid classroom projector token
    ptok = str(payload.get("projector_token", "")).strip()
    if ptok and device_mgr.verify_projector_token(ptok, session.session_code):
        return True, cookie_student_id

    # Allow enrolled/checked-in student OR student browsing an open active session
    if session.attendance_open:
        return True, cookie_student_id or payload.get("student_id")

    if cookie_student_id is not None:
        enrolled = (
            db.query(CourseEnrollment)
            .filter_by(course_id=course.id, student_id=cookie_student_id, status="approved")
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
    if session.course_id is not None:
        join_room(f"course_notify_{session.course_id}")
    _subscribe_user_notification_rooms()

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
