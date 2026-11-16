from __future__ import annotations

from typing import Any, Optional

from flask import current_app, jsonify, request

from app.models import ClassSession, Course, CourseStaff, Lecturer, Student
from app.services.identity_helpers import record_audit_event
from app.services.qr_service import DeviceBindingManager


def is_lecturer_authorized_for_course(db: Any, course: Course, lecturer_id: int) -> bool:
    """Return True if lecturer_id is the primary course owner or registered TA/co-lecturer."""
    if course.lecturer_id == lecturer_id:
        return True
    return (
        db.query(CourseStaff)
        .filter_by(course_id=course.id, lecturer_id=lecturer_id)
        .first()
        is not None
    )


def get_client_ip() -> str:
    forwarded = request.headers.get("X-Forwarded-For", "").split(",")[0].strip()
    return forwarded or (request.remote_addr or "unknown")


def get_current_lecturer() -> Optional[Lecturer]:
    """Verify timed cp_lecturer cookie and active token_version, returning Lecturer or None."""
    db = current_app.extensions["db_session"]
    device_mgr: DeviceBindingManager = current_app.extensions["device_mgr"]
    cookie_val = request.cookies.get(DeviceBindingManager.LECTURER_COOKIE_NAME)
    parsed = device_mgr.parse_lecturer_cookie(cookie_val)
    if parsed is None:
        return None
    lecturer_id, token_version = parsed
    lecturer = db.get(Lecturer, lecturer_id)
    if lecturer is None or not lecturer.is_active:
        return None
    if int(lecturer.token_version or 1) != token_version:
        return None
    return lecturer


def get_current_student() -> Optional[Student]:
    """Verify timed cp_device cookie and return registered Student or None."""
    db = current_app.extensions["db_session"]
    device_mgr: DeviceBindingManager = current_app.extensions["device_mgr"]
    cookie_val = request.cookies.get(DeviceBindingManager.COOKIE_NAME)
    parsed = device_mgr.parse_device_cookie(cookie_val)
    if parsed is None:
        return None
    return db.get(Student, parsed[1])


def require_course_access(
    course_id: int,
) -> tuple[Optional[Course], Optional[Lecturer], Optional[tuple[Any, int]]]:
    """Verify the active lecturer owns the target course when course has an owner or lecturer is logged in."""
    db = current_app.extensions["db_session"]
    course = db.get(Course, course_id)
    if course is None:
        return None, None, (jsonify({"error": "Course not found."}), 404)

    lecturer = get_current_lecturer()
    if course.lecturer_id is not None:
        if lecturer is None:
            return None, None, (jsonify({"error": "Lecturer sign-in required."}), 401)
        if not is_lecturer_authorized_for_course(db, course, lecturer.id):
            record_audit_event(
                db,
                actor_type="lecturer",
                actor_id=lecturer.id,
                action="course.access_denied_403",
                target_type="course",
                target_id=course.id,
                ip_address=get_client_ip(),
            )
            db.commit()
            return (
                None,
                lecturer,
                (jsonify({"error": "Access denied: this course belongs to another lecturer."}), 403),
            )
    elif lecturer is not None:
        return (
            None,
            lecturer,
            (jsonify({"error": "Access denied: course is not owned by your account."}), 403),
        )

    return course, lecturer, None


def require_session_access(
    session_id: int,
) -> tuple[Optional[ClassSession], Optional[Lecturer], Optional[tuple[Any, int]]]:
    """Verify the active lecturer owns the parent course for a session."""
    db = current_app.extensions["db_session"]
    sess = db.get(ClassSession, session_id)
    if sess is None:
        return None, None, (jsonify({"error": "Lecture session not found."}), 404)

    lecturer = get_current_lecturer()
    if sess.course_id is None:
        return sess, lecturer, None

    course = db.get(Course, sess.course_id)
    if course is None:
        return sess, lecturer, None

    if course.lecturer_id is not None:
        if lecturer is None:
            return None, None, (jsonify({"error": "Lecturer sign-in required."}), 401)
        if not is_lecturer_authorized_for_course(db, course, lecturer.id):
            record_audit_event(
                db,
                actor_type="lecturer",
                actor_id=lecturer.id,
                action="session.access_denied_403",
                target_type="session",
                target_id=sess.id,
                ip_address=get_client_ip(),
            )
            db.commit()
            return (
                None,
                lecturer,
                (jsonify({"error": "Access denied: this lecture belongs to another lecturer."}), 403),
            )
    elif lecturer is not None:
        return (
            None,
            lecturer,
            (jsonify({"error": "Access denied: this lecture's course is not in your account."}), 403),
        )

    return sess, lecturer, None
