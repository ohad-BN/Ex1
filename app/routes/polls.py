from __future__ import annotations

from typing import Optional

from flask import Blueprint, current_app, jsonify, request

from app import socketio
from app.models import ClassSession, Poll, Student
from app.routes.guards import require_session_access, verify_session_read_access
from app.services.poll_service import PollError, PollService
from app.services.qr_service import DeviceBindingManager

polls_bp = Blueprint("polls", __name__)


def _resolve_student_id_from_request() -> Optional[int]:
    device_mgr: DeviceBindingManager = current_app.extensions["device_mgr"]
    cookie_raw = request.cookies.get(DeviceBindingManager.COOKIE_NAME)
    parsed = device_mgr.parse_device_cookie(cookie_raw)
    if parsed is not None:
        return parsed[1]
    data = request.get_json(silent=True) or {}
    if data.get("student_id") is not None:
        return int(data["student_id"])
    return request.args.get("student_id", type=int)


@polls_bp.get("/api/sessions/<int:session_id>/polls")
def list_polls(session_id: int):
    sess, _lec, stu, err = verify_session_read_access(session_id, allow_open_attendance=True)
    if err is not None:
        return err
    assert sess is not None
    db = current_app.extensions["db_session"]
    svc = PollService(db)
    student_id = stu.id if stu is not None else _resolve_student_id_from_request()
    return jsonify(
        {
            "polls": svc.list_session_polls(session_id, student_id=student_id),
            "sync": svc.build_sync_payload(session_id, student_id=student_id),
        }
    )


@polls_bp.post("/api/sessions/<int:session_id>/polls")
def create_poll(session_id: int):
    _sess, _lec, err = require_session_access(session_id)
    if err is not None:
        return err
    db = current_app.extensions["db_session"]
    svc = PollService(db)
    data = request.get_json(silent=True) or request.form.to_dict()

    question = str(data.get("question", "")).strip()
    raw_opts = data.get("options")
    if isinstance(raw_opts, list):
        options = [str(o).strip() for o in raw_opts if str(o).strip()]
    else:
        raw_str = str(raw_opts or "")
        options = [o.strip() for o in raw_str.split(",") if o.strip()]

    time_limit = int(data.get("time_limit_sec", 60))
    correct_raw = data.get("correct_index")
    correct_idx = (
        int(correct_raw)
        if (correct_raw is not None and str(correct_raw).strip() != "")
        else None
    )
    launch_now = str(data.get("launch_now", "")).lower() in ("1", "true", "yes")

    try:
        poll = svc.create_poll(
            session_id=session_id,
            question=question,
            options=options,
            time_limit_sec=time_limit,
            correct_index=correct_idx,
        )
        session = db.get(ClassSession, session_id)

        if launch_now:
            poll = svc.launch_poll(poll.id)
            serialized = svc.serialize_poll(poll)
            if session is not None:
                room = f"session_{session.session_code}"
                socketio.emit("poll:activated", serialized, to=room)
                socketio.emit("poll:opened", serialized, to=room)
                if session.course_id is not None:
                    socketio.emit(
                        "poll:notify",
                        {
                            "poll_id": serialized["id"],
                            "question": serialized["question"],
                            "course_id": session.course_id,
                            "session_id": session.id,
                            "session_code": session.session_code,
                            "lecture_number": session.lecture_number,
                            "lecture_title": session.title,
                        },
                        to=f"course_notify_{session.course_id}",
                    )
            return jsonify(serialized), 201

        serialized = svc.serialize_poll(poll)
        return jsonify(serialized), 201
    except PollError as exc:
        return jsonify({"error": exc.message}), exc.status_code


@polls_bp.post("/api/polls/<int:poll_id>/launch")
def launch_poll(poll_id: int):
    db = current_app.extensions["db_session"]
    target_poll = db.get(Poll, poll_id)
    if target_poll is None:
        return jsonify({"error": "Poll not found."}), 404
    _sess, _lec, err = require_session_access(target_poll.session_id)
    if err is not None:
        return err

    svc = PollService(db)
    try:
        poll = svc.launch_poll(poll_id)
    except PollError as exc:
        return jsonify({"error": exc.message}), exc.status_code

    session = db.get(ClassSession, poll.session_id)
    serialized = svc.serialize_poll(poll)
    if session is not None:
        room = f"session_{session.session_code}"
        socketio.emit("poll:activated", serialized, to=room)
        socketio.emit("poll:opened", serialized, to=room)
        if session.course_id is not None:
            socketio.emit(
                "poll:notify",
                {
                    "poll_id": serialized["id"],
                    "question": serialized["question"],
                    "course_id": session.course_id,
                    "session_id": session.id,
                    "session_code": session.session_code,
                    "lecture_number": session.lecture_number,
                    "lecture_title": session.title,
                },
                to=f"course_notify_{session.course_id}",
            )
    return jsonify(serialized)


@polls_bp.post("/api/polls/<int:poll_id>/close")
def close_poll(poll_id: int):
    db = current_app.extensions["db_session"]
    target_poll = db.get(Poll, poll_id)
    if target_poll is None:
        return jsonify({"error": "Poll not found."}), 404
    _sess, _lec, err = require_session_access(target_poll.session_id)
    if err is not None:
        return err

    svc = PollService(db)
    try:
        poll = svc.close_poll(poll_id)
    except PollError as exc:
        return jsonify({"error": exc.message}), exc.status_code

    session = db.get(ClassSession, poll.session_id)
    serialized = svc.serialize_poll(poll)
    if session is not None:
        socketio.emit(
            "poll:closed",
            serialized,
            to=f"session_{session.session_code}",
        )
    return jsonify(serialized)


@polls_bp.post("/api/polls/<int:poll_id>/vote")
def vote_on_poll(poll_id: int):
    db = current_app.extensions["db_session"]
    svc = PollService(db)
    data = request.get_json(silent=True) or request.form.to_dict()

    student_id = _resolve_student_id_from_request()
    if student_id is None:
        return jsonify({"error": "Student check-in required before voting."}), 401

    student = db.get(Student, student_id)
    if student is None:
        return jsonify({"error": "Student record not found."}), 404

    if "option_index" not in data:
        return jsonify({"error": "option_index is required."}), 400

    try:
        serialized, is_new = svc.cast_vote(
            poll_id=poll_id,
            student_id=student.id,
            option_index=int(data["option_index"]),
        )
    except PollError as exc:
        return jsonify({"error": exc.message}), exc.status_code

    poll = db.get(Poll, poll_id)
    session = db.get(ClassSession, poll.session_id) if poll else None
    if session is not None:
        # Broadcast neutral tally update (without student-specific my_vote_index)
        public_tally = svc.serialize_poll(poll)
        socketio.emit(
            "poll:results_update",
            public_tally,
            to=f"session_{session.session_code}",
        )

    serialized["is_new_vote"] = is_new
    return jsonify(serialized)
