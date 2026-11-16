from __future__ import annotations

import secrets
from typing import Any

from flask import (
    Blueprint,
    current_app,
    jsonify,
    make_response,
    redirect,
    render_template,
    request,
    url_for,
)

from app.models import ClassSession, Course, HeadcountSnapshot, Lecturer, Student
from app.services.attendance_service import AttendanceService
from app.services.course_service import CourseError, CourseService
from app.services.qr_service import DeviceBindingManager, resolve_effective_base_url

courses_bp = Blueprint("courses", __name__)


def _get_course_service() -> CourseService:
    db = current_app.extensions["db_session"]
    return CourseService(db=db)


def _get_attendance_service() -> AttendanceService:
    db = current_app.extensions["db_session"]
    qr_service = current_app.extensions["qr_service"]
    device_mgr = current_app.extensions["device_mgr"]
    return AttendanceService(db=db, qr_service=qr_service, device_mgr=device_mgr)


def _get_current_lecturer() -> Lecturer | None:
    db = current_app.extensions["db_session"]
    device_mgr: DeviceBindingManager = current_app.extensions["device_mgr"]
    cookie_val = request.cookies.get(DeviceBindingManager.LECTURER_COOKIE_NAME)
    lecturer_id = device_mgr.parse_lecturer_cookie(cookie_val)
    if lecturer_id is None:
        return None
    return db.get(Lecturer, lecturer_id)


@courses_bp.get("/portal")
def portal_selector():
    """School-wide landing hub allowing users to enter Teacher Portal or Student App."""
    svc = _get_course_service()
    courses = svc.ensure_default_school_courses()
    lecturer = _get_current_lecturer()
    return render_template("portal_home.html", courses=courses, lecturer=lecturer)


@courses_bp.get("/teacher/login")
def teacher_login_page():
    """Dedicated Lecturer Sign-In & Registration Page."""
    lecturer = _get_current_lecturer()
    return render_template("teacher_login.html", lecturer=lecturer)


@courses_bp.post("/api/teacher/login")
def api_teacher_login():
    """Authenticate or register a Lecturer and issue signed cp_lecturer cookie."""
    payload: dict[str, Any] = request.get_json(silent=True) or request.form.to_dict()
    full_name = str(payload.get("full_name", "")).strip()
    email = str(payload.get("email", "")).strip()
    department = str(payload.get("department", "Computer Science")).strip()

    svc = _get_course_service()
    device_mgr: DeviceBindingManager = current_app.extensions["device_mgr"]
    try:
        lecturer = svc.get_or_create_lecturer(
            full_name=full_name,
            email=email,
            department=department,
        )
    except CourseError as exc:
        return jsonify({"error": exc.message}), exc.status_code

    resp = make_response(
        jsonify(
            {
                "lecturer": {
                    "id": lecturer.id,
                    "full_name": lecturer.full_name,
                    "email": lecturer.email,
                    "department": lecturer.department,
                }
            }
        ),
        200,
    )
    resp.set_cookie(
        DeviceBindingManager.LECTURER_COOKIE_NAME,
        device_mgr.create_lecturer_cookie(lecturer.id),
        max_age=365 * 86400,
        httponly=True,
        samesite="Lax",
        secure=request.is_secure,
    )
    return resp


@courses_bp.post("/api/teacher/logout")
def api_teacher_logout():
    resp = make_response(jsonify({"status": "logged_out"}), 200)
    resp.delete_cookie(DeviceBindingManager.LECTURER_COOKIE_NAME)
    return resp


@courses_bp.get("/teacher")
def teacher_portal():
    """Modular Multi-Course Teacher Portal with Lecturer Sign-In & Guided Course/Lecture Creation."""
    svc = _get_course_service()
    att_svc = _get_attendance_service()
    qr_service = current_app.extensions["qr_service"]
    cfg = current_app.config["CLASSPULSE_CONFIG"]
    db = current_app.extensions["db_session"]

    lecturer = _get_current_lecturer()
    req_course_id = request.args.get("course_id", type=int)
    req_session_code = request.args.get("session", "").strip().upper()

    # If no lecturer signed in and no explicit course/session query param, show Lecturer Sign-In page
    if lecturer is None and not req_course_id and not req_session_code:
        return redirect(url_for("courses.teacher_login_page"))

    courses = (
        svc.list_lecturer_courses(lecturer.id)
        if lecturer is not None
        else svc.ensure_default_school_courses()
    )
    if not courses:
        courses = svc.ensure_default_school_courses()

    selected_session = None
    selected_course = None

    if req_session_code:
        selected_session = (
            db.query(ClassSession).filter_by(session_code=req_session_code).one_or_none()
        )
        if selected_session and selected_session.course_id:
            selected_course = db.get(Course, selected_session.course_id)

    if selected_course is None and req_course_id:
        selected_course = db.get(Course, req_course_id)

    if selected_course is None:
        selected_course = courses[0]

    course_sessions = (
        db.query(ClassSession)
        .filter_by(course_id=selected_course.id)
        .order_by(ClassSession.id.desc())
        .all()
    )
    if selected_session is None:
        if course_sessions:
            selected_session = course_sessions[0]
        else:
            selected_session = att_svc.create_session(
                title=f"Lecture 1 — {selected_course.name}",
                course_id=selected_course.id,
            )
            course_sessions = [selected_session]

    base_url = resolve_effective_base_url(cfg.PUBLIC_BASE_URL, request.host_url)
    qr_payload = qr_service.build_qr_payload(base_url, selected_session.session_code)
    verdict = att_svc.get_audit_summary(selected_session.id)
    rows = att_svc.get_attendance_rows(selected_session.id)
    latest_snap = (
        db.query(HeadcountSnapshot)
        .filter_by(session_id=selected_session.id)
        .order_by(HeadcountSnapshot.id.desc())
        .first()
    )
    course_overview = svc.get_course_overview(selected_course.id)

    llm_client = current_app.extensions.get("llm_client")

    return render_template(
        "teacher_portal.html",
        lecturer=lecturer,
        courses=[svc.serialize_course_summary(c) for c in courses],
        selected_course=course_overview,
        course_sessions=course_sessions,
        session=selected_session,
        qr_payload=qr_payload,
        verdict=verdict,
        rows=rows,
        latest_snap=latest_snap,
        base_url=base_url,
        gemini_configured=bool(llm_client and llm_client.is_configured()),
    )


@courses_bp.get("/student")
def student_app():
    """Dedicated Multi-Course Student Mobile/Web App."""
    svc = _get_course_service()
    svc.ensure_default_school_courses()
    device_mgr: DeviceBindingManager = current_app.extensions["device_mgr"]

    cookie_val = request.cookies.get(DeviceBindingManager.COOKIE_NAME)
    parsed = device_mgr.parse_device_cookie(cookie_val)
    student_id = parsed[1] if parsed else request.args.get("student_id", type=int)

    dashboard = svc.build_student_dashboard_payload(student_id=student_id)
    active_session_code = request.args.get("session", "").strip().upper()

    return render_template(
        "student_app.html",
        dashboard=dashboard,
        initial_session_code=active_session_code,
    )


@courses_bp.get("/api/courses")
def api_list_courses():
    svc = _get_course_service()
    courses = svc.ensure_default_school_courses()
    return jsonify(
        {"courses": [svc.serialize_course_summary(c) for c in courses]}
    )


@courses_bp.post("/api/courses")
def api_create_course():
    payload: dict[str, Any] = request.get_json(silent=True) or {}
    svc = _get_course_service()
    lecturer = _get_current_lecturer()
    lecturer_name = str(
        payload.get("lecturer_name")
        or (lecturer.full_name if lecturer else "Prof. Boaz Ben-Moshe")
    )
    department = str(
        payload.get("department")
        or (lecturer.department if lecturer else "Computer Science")
    )
    try:
        course = svc.create_course(
            course_code=str(payload.get("course_code", "")),
            name=str(payload.get("name", "")),
            department=department,
            semester=str(payload.get("semester", "Fall 2026")),
            lecturer_name=lecturer_name,
            lecturer_id=lecturer.id if lecturer else None,
            initial_lecture_title=payload.get("initial_lecture_title"),
        )
    except CourseError as exc:
        return jsonify({"error": exc.message}), exc.status_code

    return jsonify(svc.serialize_course_summary(course)), 201


@courses_bp.get("/api/courses/<int:course_id>/overview")
def api_course_overview(course_id: int):
    svc = _get_course_service()
    try:
        data = svc.get_course_overview(course_id)
    except CourseError as exc:
        return jsonify({"error": exc.message}), exc.status_code
    return jsonify(data)


@courses_bp.post("/api/courses/<int:course_id>/sessions")
def api_create_course_session(course_id: int):
    payload: dict[str, Any] = request.get_json(silent=True) or {}
    att_svc = _get_attendance_service()
    title = str(payload.get("title", "New Lecture")).strip()
    session_code = payload.get("session_code")
    sess = att_svc.create_session(
        title=title,
        session_code=str(session_code) if session_code else None,
        course_id=course_id,
    )
    return (
        jsonify(
            {
                "id": sess.id,
                "course_id": sess.course_id,
                "session_code": sess.session_code,
                "title": sess.title,
                "started_at": sess.started_at.isoformat(),
                "attendance_open": sess.attendance_open,
            }
        ),
        201,
    )


@courses_bp.post("/api/courses/<int:course_id>/roster")
def api_import_course_roster(course_id: int):
    svc = _get_course_service()
    csv_text = ""
    if "file" in request.files:
        csv_text = request.files["file"].read().decode("utf-8-sig", errors="replace")
    else:
        payload = request.get_json(silent=True) or {}
        csv_text = str(payload.get("csv_text", ""))

    if not csv_text.strip():
        return jsonify({"error": "CSV content is empty."}), 400

    try:
        imported = svc.import_course_roster_csv(course_id, csv_text)
        overview = svc.get_course_overview(course_id)
    except CourseError as exc:
        return jsonify({"error": exc.message}), exc.status_code

    return jsonify({"imported": imported, "course": overview})


@courses_bp.get("/api/student/me")
def api_student_me():
    svc = _get_course_service()
    device_mgr: DeviceBindingManager = current_app.extensions["device_mgr"]

    cookie_val = request.cookies.get(DeviceBindingManager.COOKIE_NAME)
    parsed = device_mgr.parse_device_cookie(cookie_val)
    student_id = parsed[1] if parsed else request.args.get("student_id", type=int)

    return jsonify(svc.build_student_dashboard_payload(student_id=student_id))


@courses_bp.post("/api/student/login")
def api_student_login():
    """Sign in or register a student into the Student App and set persistent cp_device cookie."""
    payload: dict[str, Any] = request.get_json(silent=True) or {}
    full_name = str(payload.get("full_name", "")).strip()
    email = str(payload.get("email", "")).strip().lower()
    course_code = str(payload.get("course_code", "")).strip().upper()

    db = current_app.extensions["db_session"]
    device_mgr: DeviceBindingManager = current_app.extensions["device_mgr"]
    svc = _get_course_service()
    svc.ensure_default_school_courses()

    cookie_val = request.cookies.get(DeviceBindingManager.COOKIE_NAME)
    parsed = device_mgr.parse_device_cookie(cookie_val)
    device_id = (
        parsed[0]
        if parsed
        else str(payload.get("device_id") or secrets.token_hex(12))
    )

    if course_code:
        try:
            _course, student = svc.enroll_student_by_code(
                course_code=course_code,
                full_name=full_name,
                email=email,
                device_id=device_id,
            )
        except CourseError as exc:
            return jsonify({"error": exc.message}), exc.status_code
    else:
        if not email:
            return jsonify({"error": "Email is required."}), 400
        student = db.query(Student).filter_by(email=email).one_or_none()
        if student is None:
            if not full_name:
                return (
                    jsonify({"error": "Full name is required for first-time sign in."}),
                    400,
                )
            student = Student(
                full_name=full_name,
                email=email,
                primary_device_id=device_id,
            )
            db.add(student)
            db.commit()
        elif full_name:
            student.full_name = full_name
            db.commit()

    dashboard = svc.build_student_dashboard_payload(student_id=student.id)
    resp = make_response(jsonify(dashboard), 200)
    signed_cookie = device_mgr.create_device_cookie(device_id, student.id)
    resp.set_cookie(
        DeviceBindingManager.COOKIE_NAME,
        signed_cookie,
        max_age=365 * 86400,
        httponly=True,
        samesite="Lax",
        secure=request.is_secure,
    )
    return resp


@courses_bp.post("/api/student/enroll")
def api_student_enroll():
    """Allow an authenticated student to join a school course by course_code or lecture code."""
    payload: dict[str, Any] = request.get_json(silent=True) or {}
    course_code = str(payload.get("course_code", "")).strip().upper()

    db = current_app.extensions["db_session"]
    device_mgr: DeviceBindingManager = current_app.extensions["device_mgr"]
    svc = _get_course_service()

    cookie_val = request.cookies.get(DeviceBindingManager.COOKIE_NAME)
    parsed = device_mgr.parse_device_cookie(cookie_val)
    student_id = parsed[1] if parsed else payload.get("student_id")

    student = db.get(Student, int(student_id)) if student_id else None
    if student is None:
        return jsonify({"error": "Please sign in first."}), 401

    try:
        course, _st = svc.enroll_student_by_code(
            course_code=course_code,
            full_name=student.full_name,
            email=student.email,
            device_id=student.primary_device_id,
        )
    except CourseError as exc:
        return jsonify({"error": exc.message}), exc.status_code

    return jsonify(
        {
            "enrolled_course": svc.serialize_course_summary(course),
            "dashboard": svc.build_student_dashboard_payload(student_id=student.id),
        }
    )
