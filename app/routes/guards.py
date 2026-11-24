from __future__ import annotations

from typing import Any, Optional

from flask import current_app, jsonify, request

from app.models import (
    AttendanceRecord,
    ClassSession,
    Course,
    CourseEnrollment,
    CourseStaff,
    Lecturer,
    Student,
)
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


def is_student_enrolled_in_course(db: Any, course_id: int, student_id: int) -> bool:
    """Return True if student has an approved CourseEnrollment or session AttendanceRecord."""
    if (
        db.query(CourseEnrollment)
        .filter_by(course_id=course_id, student_id=student_id, status="approved")
        .first()
        is not None
    ):
        return True
    return (
        db.query(AttendanceRecord)
        .join(ClassSession, AttendanceRecord.session_id == ClassSession.id)
        .filter(
            ClassSession.course_id == course_id,
            AttendanceRecord.student_id == student_id,
        )
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


def verify_course_read_access(
    course_id: int,
    *,
    explicit_student_id: Optional[int] = None,
) -> tuple[
    Optional[Course],
    Optional[Lecturer],
    Optional[Student],
    bool,
    Optional[tuple[Any, int]],
]:
    """Validate course read access for either an owning lecturer or an enrolled student.

    Returns (course, lecturer, student, can_view_all_submissions, error_response).
    """
    db = current_app.extensions["db_session"]
    course = db.get(Course, course_id)
    if course is None:
        return None, None, None, False, (jsonify({"error": "Course not found."}), 404)

    lecturer = get_current_lecturer()
    cookie_student = get_current_student()
    student = cookie_student
    if student is None and explicit_student_id is not None:
        student = db.get(Student, int(explicit_student_id))

    if course.lecturer_id is None:
        if lecturer is not None:
            return (
                None,
                lecturer,
                None,
                False,
                (jsonify({"error": "Access denied: course is not owned by your account."}), 403),
            )
        return course, None, student, True, None

    if lecturer is not None:
        if is_lecturer_authorized_for_course(db, course, lecturer.id):
            return course, lecturer, student, True, None
        # Lecturer may not read another lecturer's course
        return (
            None,
            lecturer,
            None,
            False,
            (jsonify({"error": "Access denied: this course belongs to another lecturer."}), 403),
        )

    if student is not None and is_student_enrolled_in_course(db, course.id, student.id):
        return course, None, student, False, None

    if student is not None:
        return (
            None,
            None,
            student,
            False,
            (jsonify({"error": "Enroll in this course to view its materials."}), 403),
        )
    return None, None, None, False, (jsonify({"error": "Sign-in required to access this course."}), 401)


def verify_session_read_access(
    session_id: int,
    *,
    allow_open_attendance: bool = False,
) -> tuple[Optional[ClassSession], Optional[Lecturer], Optional[Student], Optional[tuple[Any, int]]]:
    """Validate session read access: blocks cross-lecturer snooping while allowing enrolled students."""
    db = current_app.extensions["db_session"]
    sess = db.get(ClassSession, session_id)
    if sess is None:
        return None, None, None, (jsonify({"error": "Lecture session not found."}), 404)

    lecturer = get_current_lecturer()
    student = get_current_student()
    if sess.course_id is None:
        return sess, lecturer, student, None

    course = db.get(Course, sess.course_id)
    if course is None or course.lecturer_id is None:
        if lecturer is not None and course is not None and course.lecturer_id is None:
            return (
                None,
                lecturer,
                None,
                (jsonify({"error": "Access denied: this lecture's course is not in your account."}), 403),
            )
        return sess, lecturer, student, None

    if lecturer is not None:
        if is_lecturer_authorized_for_course(db, course, lecturer.id):
            return sess, lecturer, student, None
        return (
            None,
            lecturer,
            None,
            (jsonify({"error": "Access denied: this lecture belongs to another lecturer."}), 403),
        )

    if student is not None and is_student_enrolled_in_course(db, course.id, student.id):
        return sess, None, student, None

    if allow_open_attendance and sess.attendance_open:
        return sess, None, student, None

    if student is not None:
        return None, None, student, (jsonify({"error": "Enroll in this course to access its lectures."}), 403)
    return None, None, None, (jsonify({"error": "Sign-in required to access this lecture."}), 401)
