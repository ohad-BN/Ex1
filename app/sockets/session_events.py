from __future__ import annotations

from typing import Any

from flask import current_app, request
from flask_socketio import emit, join_room

from app import socketio
from app.models import AttendanceRecord, ClassSession
from app.services.chat_service import ChatService
from app.services.poll_service import PollService
from app.services.qr_service import DeviceBindingManager, resolve_effective_base_url


def register_session_events(sio) -> None:
    sio.on_event("join_session", handle_join_session)
    sio.on_event("qr:request_refresh", handle_qr_refresh)


@socketio.on("join_session")
def handle_join_session(data: dict[str, Any] | None) -> None:
    payload = data or {}
    session_code = str(payload.get("session_code", "")).strip().upper()
    if not session_code:
        return

    room = f"session_{session_code}"
    join_room(room)

    db = current_app.extensions["db_session"]
    qr_service = current_app.extensions["qr_service"]
    device_mgr: DeviceBindingManager = current_app.extensions["device_mgr"]
    cfg = current_app.config["CLASSPULSE_CONFIG"]

    session = db.query(ClassSession).filter_by(session_code=session_code).one_or_none()
    if session is None:
        return

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
    student_id = payload.get("student_id")
    if student_id is None:
        parsed = device_mgr.parse_device_cookie(
            request.cookies.get(DeviceBindingManager.COOKIE_NAME)
        )
        if parsed is not None:
            student_id = parsed[1]

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

    base_url = resolve_effective_base_url(cfg.PUBLIC_BASE_URL, request.host_url)
    qr_payload = qr_service.build_qr_payload(base_url, session_code)
    qr_payload["attendance_open"] = session.attendance_open
    emit("qr:rotate", qr_payload)
