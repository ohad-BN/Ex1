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
from sqlalchemy import func

from app import socketio
from app.models import AttendanceRecord, ClassSession, Course, CourseStaff, HeadcountSnapshot, Lecturer, Student
from app.routes.guards import (
    get_client_ip,
    get_current_lecturer,
    is_lecturer_authorized_for_course,
    require_course_access,
)
from app.schemas import (
    CourseCreateSchema,
    CourseStaffAddSchema,
    parse_schema,
)
from app.services.attendance_service import AttendanceService
from app.services.course_service import CourseError, CourseService
from app.services.identity_helpers import (
    normalize_student_id,
    parse_roster_upload,
    resolve_or_upsert_student,
)
from app.services.qr_service import DeviceBindingManager, resolve_effective_base_url
from app.services.rate_limiter import SlidingWindowRateLimiter

courses_bp = Blueprint("courses", __name__)


def _get_course_service() -> CourseService:
    db = current_app.extensions["db_session"]
    return CourseService(db=db)


def _get_attendance_service() -> AttendanceService:
    db = current_app.extensions["db_session"]
    qr_service = current_app.extensions["qr_service"]
    device_mgr = current_app.extensions["device_mgr"]
    return AttendanceService(db=db, qr_service=qr_service, device_mgr=device_mgr)


@courses_bp.get("/portal")
def portal_selector():
    """School-wide landing hub allowing users to enter Teacher Portal or Student App."""
    svc = _get_course_service()
    courses = svc.ensure_default_school_courses()
    lecturer = get_current_lecturer()
    return render_template("portal_home.html", courses=courses, lecturer=lecturer)


def _serialize_lecturer(lecturer: Lecturer) -> dict[str, Any]:
    return {
        "id": lecturer.id,
        "username": lecturer.username,
        "full_name": lecturer.full_name,
        "email": lecturer.email,
        "department": lecturer.department,
        "role": lecturer.role,
    }


def _build_lecturer_auth_response(
    lecturer: Lecturer,
    svc: CourseService,
    *,
    status_code: int = 200,
    remember_me: bool = True,
):
    device_mgr: DeviceBindingManager = current_app.extensions["device_mgr"]
    owned_courses = [
        svc.serialize_course_summary(c) for c in svc.list_lecturer_courses(lecturer.id)
    ]
    resp = make_response(
        jsonify(
            {
                "lecturer": _serialize_lecturer(lecturer),
                "courses": owned_courses,
            }
        ),
        status_code,
    )
    resp.set_cookie(
        DeviceBindingManager.LECTURER_COOKIE_NAME,
        device_mgr.create_lecturer_cookie(lecturer.id, int(lecturer.token_version or 1)),
        max_age=(7 * 86400) if remember_me else None,
        httponly=True,
        samesite="Lax",
        secure=request.is_secure,
    )
    return resp


def _check_auth_rate_limit(bucket: str, limit: int = 15, window_sec: float = 60.0):
    limiter: SlidingWindowRateLimiter | None = current_app.extensions.get("rate_limiter")
    if limiter is None or current_app.config.get("TESTING"):
        return None
    allowed, retry_after = limiter.allow(f"{bucket}:{get_client_ip()}", limit, window_sec)
    if not allowed:
        return (
            jsonify(
                {
                    "error": f"Too many authentication attempts. Please retry in {retry_after}s."
                }
            ),
            429,
        )
    return None


def _maybe_seed_demo_if_empty(svc: CourseService) -> None:
    cfg = current_app.config["CLASSPULSE_CONFIG"]
    if not cfg.ENABLE_DEMO_SEED or current_app.config.get("TESTING"):
        return
    db = current_app.extensions["db_session"]
    if db.query(Lecturer).count() == 0:
        svc.ensure_default_school_courses()


@courses_bp.get("/teacher/login")
def teacher_login_page():
    """Dedicated Lecturer Sign-In & Registration Page."""
    svc = _get_course_service()
    _maybe_seed_demo_if_empty(svc)
    force_setup = bool(request.args.get("setup"))
    switch_mode = bool(request.args.get("switch"))
    has_cookie = DeviceBindingManager.LECTURER_COOKIE_NAME in request.cookies
    lecturer = get_current_lecturer()
    lecturer_courses = (
        [svc.serialize_course_summary(c) for c in svc.list_lecturer_courses(lecturer.id)]
        if lecturer is not None
        else []
    )
    if lecturer is not None and lecturer_courses and not force_setup and not switch_mode:
        return redirect(url_for("courses.teacher_portal"))

    resp = make_response(
        render_template(
            "teacher_login.html",
            lecturer=lecturer,
            lecturer_courses=lecturer_courses,
            force_setup=force_setup,
        )
    )
    if lecturer is None and has_cookie:
        resp.delete_cookie(
            DeviceBindingManager.LECTURER_COOKIE_NAME,
            path="/",
            httponly=True,
            samesite="Lax",
            secure=request.is_secure,
        )
    return resp


@courses_bp.post("/api/auth/lecturer/register")
def api_lecturer_register():
    """Register a new Lecturer account with unique username, email, and hashed password."""
    rate_err = _check_auth_rate_limit("auth_reg", limit=10, window_sec=60.0)
    if rate_err is not None:
        return rate_err

    payload: dict[str, Any] = request.get_json(silent=True) or request.form.to_dict()
    svc = _get_course_service()
    try:
        lecturer = svc.register_lecturer(
            full_name=str(payload.get("full_name", "")),
            username=str(payload.get("username", "")),
            email=str(payload.get("email", "")),
            password=str(payload.get("password", "")),
            confirm_password=(
                str(payload["confirm_password"])
                if "confirm_password" in payload
                else None
            ),
            department=str(payload.get("department", "Computer Science")),
            ip_address=get_client_ip(),
        )
    except CourseError as exc:
        return jsonify({"error": exc.message}), exc.status_code

    return _build_lecturer_auth_response(lecturer, svc, status_code=201)


@courses_bp.post("/api/auth/lecturer/login")
@courses_bp.post("/api/teacher/login")
def api_teacher_login():
    """Authenticate an existing Lecturer via username or email + password."""
    rate_err = _check_auth_rate_limit("auth_login", limit=15, window_sec=60.0)
    if rate_err is not None:
        return rate_err

    payload: dict[str, Any] = request.get_json(silent=True) or request.form.to_dict()
    identifier = str(
        payload.get("identifier")
        or payload.get("username")
        or payload.get("email")
        or ""
    ).strip()
    password = str(payload.get("password", ""))
    remember_raw = str(payload.get("remember_me", "true")).lower()
    remember_me = remember_raw not in ("0", "false", "no")

    svc = _get_course_service()
    _maybe_seed_demo_if_empty(svc)
    try:
        lecturer = svc.authenticate_lecturer(
            identifier=identifier,
            password=password,
            ip_address=get_client_ip(),
        )
    except CourseError as exc:
        return jsonify({"error": exc.message}), exc.status_code

    return _build_lecturer_auth_response(lecturer, svc, status_code=200, remember_me=remember_me)


@courses_bp.post("/api/auth/lecturer/logout")
@courses_bp.post("/api/teacher/logout")
def api_teacher_logout():
    resp = make_response(jsonify({"status": "logged_out"}), 200)
    resp.delete_cookie(
        DeviceBindingManager.LECTURER_COOKIE_NAME,
        path="/",
        httponly=True,
        samesite="Lax",
        secure=request.is_secure,
    )
    return resp


@courses_bp.post("/api/auth/lecturer/logout-all")
def api_lecturer_logout_all():
    lecturer = get_current_lecturer()
    if lecturer is None:
        return jsonify({"error": "Lecturer sign-in required."}), 401
    svc = _get_course_service()
    svc.invalidate_all_lecturer_sessions(lecturer.id, ip_address=get_client_ip())
    resp = make_response(jsonify({"status": "all_sessions_revoked"}), 200)
    resp.delete_cookie(
        DeviceBindingManager.LECTURER_COOKIE_NAME,
        path="/",
        httponly=True,
        samesite="Lax",
        secure=request.is_secure,
    )
    return resp


@courses_bp.get("/api/auth/lecturer/me")
@courses_bp.patch("/api/auth/lecturer/me")
def api_lecturer_me():
    lecturer = get_current_lecturer()
    if lecturer is None:
        return jsonify({"authenticated": False, "lecturer": None}), 401
    svc = _get_course_service()
    if request.method == "PATCH":
        payload: dict[str, Any] = request.get_json(silent=True) or {}
        try:
            lecturer = svc.update_lecturer_profile(
                lecturer.id,
                full_name=payload.get("full_name"),
                department=payload.get("department"),
            )
        except CourseError as exc:
            return jsonify({"error": exc.message}), exc.status_code
    return jsonify({"authenticated": True, "lecturer": _serialize_lecturer(lecturer)})


@courses_bp.post("/api/auth/lecturer/change-password")
def api_lecturer_change_password():
    lecturer = get_current_lecturer()
    if lecturer is None:
        return jsonify({"error": "Lecturer sign-in required."}), 401
    payload: dict[str, Any] = request.get_json(silent=True) or {}
    svc = _get_course_service()
    try:
        updated = svc.change_lecturer_password(
            lecturer.id,
            current_password=str(payload.get("current_password", "")),
            new_password=str(payload.get("new_password", "")),
            confirm_password=(
                str(payload["confirm_password"])
                if "confirm_password" in payload
                else None
            ),
            ip_address=get_client_ip(),
        )
    except CourseError as exc:
        return jsonify({"error": exc.message}), exc.status_code
    return _build_lecturer_auth_response(updated, svc, status_code=200)


@courses_bp.post("/api/auth/lecturer/forgot-password")
def api_lecturer_forgot_password():
    rate_err = _check_auth_rate_limit("auth_forgot", limit=5, window_sec=60.0)
    if rate_err is not None:
        return rate_err

    payload: dict[str, Any] = request.get_json(silent=True) or {}
    identifier = str(
        payload.get("identifier")
        or payload.get("email")
        or payload.get("username")
        or ""
    ).strip()
    svc = _get_course_service()
    raw_token = svc.create_password_reset_token(identifier, ip_address=get_client_ip())

    body: dict[str, Any] = {
        "message": "If an account matches that email or username, a password reset link has been generated."
    }
    if raw_token and current_app.config.get("TESTING"):
        body["reset_token"] = raw_token
    return jsonify(body), 200


@courses_bp.post("/api/auth/lecturer/reset-password")
def api_lecturer_reset_password():
    payload: dict[str, Any] = request.get_json(silent=True) or {}
    svc = _get_course_service()
    try:
        svc.reset_password_with_token(
            raw_token=str(payload.get("token", "")),
            new_password=str(payload.get("new_password", "")),
            confirm_password=(
                str(payload["confirm_password"])
                if "confirm_password" in payload
                else None
            ),
            ip_address=get_client_ip(),
        )
    except CourseError as exc:
        return jsonify({"error": exc.message}), exc.status_code
    return jsonify({"status": "password_reset"}), 200


@courses_bp.get("/api/auth/lecturer/audit-logs")
def api_lecturer_audit_logs():
    lecturer = get_current_lecturer()
    if lecturer is None:
        return jsonify({"error": "Lecturer sign-in required."}), 401
    svc = _get_course_service()
    limit = request.args.get("limit", default=30, type=int)
    return jsonify({"logs": svc.list_lecturer_audit_logs(lecturer.id, limit=limit)})


@courses_bp.get("/teacher")
def teacher_portal():
    """Modular Multi-Course Teacher Portal strictly isolated per signed-in Lecturer."""
    svc = _get_course_service()
    att_svc = _get_attendance_service()
    qr_service = current_app.extensions["qr_service"]
    cfg = current_app.config["CLASSPULSE_CONFIG"]
    db = current_app.extensions["db_session"]

    lecturer = get_current_lecturer()
    if lecturer is None:
        return redirect(url_for("courses.teacher_login_page"))

    req_course_id = request.args.get("course_id", type=int)
    req_session_id = request.args.get("session_id", type=int)
    req_session_code = request.args.get("session", "").strip().upper()

    courses = svc.list_lecturer_courses(lecturer.id)

    selected_session = None
    selected_course = None

    if req_session_code or req_session_id:
        selected_session = (
            db.query(ClassSession).filter_by(session_code=req_session_code).one_or_none()
            if req_session_code
            else db.get(ClassSession, int(req_session_id))
        )
        if selected_session is None or selected_session.course_id is None:
            return (
                make_response(
                    f"Access denied: lecture '{req_session_code or req_session_id}' does not belong to your lecturer account. "
                    f"<a href='/teacher'>Return to your dashboard</a>",
                    403,
                )
            )
        selected_course = db.get(Course, selected_session.course_id)
        if selected_course is None or not is_lecturer_authorized_for_course(
            db, selected_course, lecturer.id
        ):
            return make_response(
                "Access denied: this lecture belongs to another lecturer. "
                "<a href='/teacher'>Return to your dashboard</a>",
                403,
            )

    if selected_course is None and req_course_id:
        selected_course = db.get(Course, req_course_id)
        if selected_course is None or not is_lecturer_authorized_for_course(
            db, selected_course, lecturer.id
        ):
            return make_response(
                "Access denied: this course belongs to another lecturer. "
                "<a href='/teacher'>Return to your dashboard</a>",
                403,
            )

    if selected_course is None:
        if not courses:
            return redirect(url_for("courses.teacher_login_page", setup=1))
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
    device_mgr: DeviceBindingManager = current_app.extensions["device_mgr"]

    return render_template(
        "teacher_portal.html",
        lecturer=lecturer,
        courses=[svc.serialize_course_summary(c) for c in courses],
        selected_course=course_overview,
        course_sessions=course_sessions,
        session=selected_session,
        qr_payload=qr_payload,
        projector_token=device_mgr.create_projector_token(selected_session.session_code),
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
    req_course_id = request.args.get("course_id", type=int)
    checked_in_param = request.args.get("checked_in", "").strip().lower() in ("1", "true", "yes")

    checked_in_banner = None
    initial_session_id = None
    initial_course_id = req_course_id
    if active_session_code:
        db = current_app.extensions["db_session"]
        sess = db.query(ClassSession).filter_by(session_code=active_session_code).one_or_none()
        if sess is not None:
            initial_session_id = sess.id
            if initial_course_id is None:
                initial_course_id = sess.course_id
            if checked_in_param and student_id is not None:
                st = db.get(Student, int(student_id))
                rec = (
                    db.query(AttendanceRecord)
                    .filter_by(session_id=sess.id, student_id=int(student_id))
                    .one_or_none()
                )
                if rec is not None and st is not None:
                    course = db.get(Course, sess.course_id) if sess.course_id else None
                    lecture_label = sess.title or (
                        f"Lecture {sess.lecture_number}" if sess.lecture_number else "Lecture"
                    )
                    checked_in_banner = {
                        "student_name": st.full_name,
                        "course_name": course.name if course else "",
                        "lecture_number": sess.lecture_number,
                        "lecture_label": lecture_label,
                        "session_code": sess.session_code,
                        "session_title": sess.title,
                        "attendance_recorded": True,
                    }

    return render_template(
        "student_app.html",
        dashboard=dashboard,
        initial_session_code=active_session_code,
        initial_course_id=initial_course_id,
        initial_session_id=initial_session_id,
        checked_in_banner=checked_in_banner,
        from_qr_route=checked_in_param,
    )


@courses_bp.get("/api/courses")
def api_list_courses():
    svc = _get_course_service()
    lecturer = get_current_lecturer()
    if lecturer is None:
        return jsonify({"error": "Lecturer sign-in required."}), 401
    courses = svc.list_lecturer_courses(lecturer.id)
    return jsonify(
        {"courses": [svc.serialize_course_summary(c) for c in courses]}
    )


@courses_bp.get("/api/student/courses/catalog")
def api_student_courses_catalog():
    """Public non-PII course catalog for student discovery/enrollment."""
    svc = _get_course_service()
    courses = svc.ensure_default_school_courses()
    return jsonify(
        {
            "courses": [
                {
                    "id": c.id,
                    "course_code": c.course_code,
                    "name": c.name,
                    "department": c.department,
                    "semester": c.semester,
                    "lecturer_name": c.lecturer_name,
                }
                for c in courses
            ]
        }
    )


@courses_bp.post("/api/courses")
def api_create_course():
    payload: dict[str, Any] = request.get_json(silent=True) or {}
    svc = _get_course_service()
    lecturer = get_current_lecturer()
    if lecturer is None:
        return jsonify({"error": "Lecturer sign-in required to create a course."}), 401

    schema_in = {
        "course_code": str(payload.get("course_code") or payload.get("course_number") or "").strip(),
        "name": str(payload.get("name") or "").strip(),
        "department": str(payload.get("department") or lecturer.department).strip() or None,
        "semester": str(payload.get("semester") or "Fall 2026").strip() or "Fall 2026",
        "lecturer_name": str(payload.get("lecturer_name") or lecturer.full_name).strip() or None,
        "initial_lecture_title": payload.get("initial_lecture_title"),
    }
    parsed, schema_err = parse_schema(CourseCreateSchema, schema_in)
    if schema_err is not None or parsed is None:
        return jsonify({"error": schema_err or "Invalid course details."}), 400

    try:
        course = svc.create_course(
            course_code=parsed.course_code,  # type: ignore[attr-defined]
            name=parsed.name,  # type: ignore[attr-defined]
            department=parsed.department or lecturer.department,  # type: ignore[attr-defined]
            semester=parsed.semester,  # type: ignore[attr-defined]
            lecturer_name=parsed.lecturer_name or lecturer.full_name,  # type: ignore[attr-defined]
            lecturer_id=lecturer.id,
            initial_lecture_title=parsed.initial_lecture_title,  # type: ignore[attr-defined]
        )
    except CourseError as exc:
        return jsonify({"error": exc.message}), exc.status_code

    return jsonify(svc.serialize_course_summary(course)), 201


@courses_bp.get("/api/courses/<int:course_id>/overview")
def api_course_overview(course_id: int):
    _course, _lec, err = require_course_access(course_id)
    if err is not None:
        return err

    svc = _get_course_service()
    try:
        data = svc.get_course_overview(course_id)
    except CourseError as exc:
        return jsonify({"error": exc.message}), exc.status_code
    return jsonify(data)


@courses_bp.post("/api/courses/<int:course_id>/sessions")
def api_create_course_session(course_id: int):
    _course, _lec, err = require_course_access(course_id)
    if err is not None:
        return err

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
                "lecture_number": sess.lecture_number,
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
    _course, _lec, err = require_course_access(course_id)
    if err is not None:
        return err

    svc = _get_course_service()
    if "file" in request.files and request.files["file"].filename:
        fs = request.files["file"]
        raw_bytes = fs.read()
        if not raw_bytes:
            return jsonify({"error": "Uploaded roster file is empty."}), 400
        try:
            rows = parse_roster_upload(fs.filename or "", raw_bytes)
        except Exception as exc:
            return jsonify({"error": f"Could not read roster file: {exc}"}), 400
        if not rows:
            return jsonify(
                {"error": "No valid rows found. Include columns for Full Name and Email (and optional Student ID)."}
            ), 400
        try:
            imported = svc.import_course_roster_rows(course_id, rows)
            overview = svc.get_course_overview(course_id)
        except CourseError as exc:
            return jsonify({"error": exc.message}), exc.status_code
        return jsonify({"imported": imported, "course": overview})

    payload = request.get_json(silent=True) or {}
    csv_text = str(payload.get("csv_text", ""))
    if not csv_text.strip():
        return jsonify({"error": "Provide a CSV/Excel file or csv_text content."}), 400

    try:
        imported = svc.import_course_roster_csv(course_id, csv_text)
        overview = svc.get_course_overview(course_id)
    except CourseError as exc:
        return jsonify({"error": exc.message}), exc.status_code

    return jsonify({"imported": imported, "course": overview})


@courses_bp.delete("/api/courses/<int:course_id>/students/<int:student_id>")
def api_remove_course_student(course_id: int, student_id: int):
    _course, lecturer, err = require_course_access(course_id)
    if err is not None:
        return err
    assert lecturer is not None
    svc = _get_course_service()
    try:
        st = svc.remove_student_from_course(
            course_id=course_id,
            student_id=student_id,
            lecturer_id=lecturer.id,
            ip_address=get_client_ip(),
        )
        overview = svc.get_course_overview(course_id)
    except CourseError as exc:
        return jsonify({"error": exc.message}), exc.status_code
    return jsonify({"status": "removed", "student_id": st.id, "course": overview})


@courses_bp.post("/api/courses/<int:course_id>/students/<int:student_id>/reset-device")
def api_reset_student_device(course_id: int, student_id: int):
    _course, lecturer, err = require_course_access(course_id)
    if err is not None:
        return err
    assert lecturer is not None
    svc = _get_course_service()
    try:
        st = svc.reset_student_device(
            course_id=course_id,
            student_id=student_id,
            lecturer_id=lecturer.id,
            ip_address=get_client_ip(),
        )
    except CourseError as exc:
        return jsonify({"error": exc.message}), exc.status_code
    return jsonify({"status": "device_reset", "student_id": st.id})


@courses_bp.get("/api/courses/<int:course_id>/staff")
@courses_bp.post("/api/courses/<int:course_id>/staff")
def api_course_staff(course_id: int):
    _course, lecturer, err = require_course_access(course_id)
    if err is not None:
        return err
    assert lecturer is not None
    svc = _get_course_service()

    if request.method == "GET":
        return jsonify({"staff": svc.list_course_staff(course_id)})

    payload: dict[str, Any] = request.get_json(silent=True) or {}
    parsed, schema_err = parse_schema(CourseStaffAddSchema, payload)
    if schema_err is not None:
        return jsonify({"error": schema_err}), 400
    assert parsed is not None
    try:
        member = svc.add_course_staff(
            course_id=course_id,
            identifier=parsed.identifier,  # type: ignore[attr-defined]
            role=parsed.role,  # type: ignore[attr-defined]
            actor_lecturer_id=lecturer.id,
            ip_address=get_client_ip(),
        )
    except CourseError as exc:
        return jsonify({"error": exc.message}), exc.status_code
    return jsonify({"member": member, "staff": svc.list_course_staff(course_id)}), 201


@courses_bp.delete("/api/courses/<int:course_id>/staff/<int:staff_lecturer_id>")
def api_remove_course_staff(course_id: int, staff_lecturer_id: int):
    _course, lecturer, err = require_course_access(course_id)
    if err is not None:
        return err
    assert lecturer is not None
    svc = _get_course_service()
    try:
        svc.remove_course_staff(
            course_id=course_id,
            staff_lecturer_id=staff_lecturer_id,
            actor_lecturer_id=lecturer.id,
            ip_address=get_client_ip(),
        )
    except CourseError as exc:
        return jsonify({"error": exc.message}), exc.status_code
    return jsonify({"status": "removed", "staff": svc.list_course_staff(course_id)})


@courses_bp.get("/api/student/me")
def api_student_me():
    svc = _get_course_service()
    device_mgr: DeviceBindingManager = current_app.extensions["device_mgr"]

    cookie_val = request.cookies.get(DeviceBindingManager.COOKIE_NAME)
    parsed = device_mgr.parse_device_cookie(cookie_val)
    student_id = parsed[1] if parsed else request.args.get("student_id", type=int)

    return jsonify(svc.build_student_dashboard_payload(student_id=student_id))


@courses_bp.post("/api/student/logout")
def api_student_logout():
    """Clear the student device cookie so another student or fresh session can sign in."""
    resp = make_response(jsonify({"status": "logged_out"}), 200)
    resp.delete_cookie(
        DeviceBindingManager.COOKIE_NAME,
        path="/",
        httponly=True,
        samesite="Lax",
        secure=request.is_secure,
    )
    return resp


@courses_bp.post("/api/student/login")
def api_student_login():
    """Sign in or register a student with Full Name + ID Number (or Email) without QR."""
    payload: dict[str, Any] = request.get_json(silent=True) or {}
    full_name = str(payload.get("full_name", "")).strip()
    email = str(payload.get("email", "")).strip().lower()
    student_id_number = str(payload.get("student_id_number", "")).strip() or None
    norm_sid = normalize_student_id(student_id_number)
    course_code = str(payload.get("course_code", "")).strip().upper()
    raw_course_id = payload.get("course_id")

    db = current_app.extensions["db_session"]
    device_mgr: DeviceBindingManager = current_app.extensions["device_mgr"]
    svc = _get_course_service()
    svc.ensure_default_school_courses()

    by_email = (
        db.query(Student)
        .filter(func.lower(Student.email) == email)
        .order_by(Student.id.asc())
        .first()
        if email
        else None
    )
    by_sid = (
        db.query(Student)
        .filter_by(student_id_number=norm_sid)
        .order_by(Student.id.asc())
        .first()
        if norm_sid
        else None
    )
    if by_email is not None and by_sid is not None and by_email.id != by_sid.id:
        return jsonify({"error": "Student ID and Email belong to different registered accounts."}), 409

    existing_student = by_email or by_sid
    resolved_name = full_name or (existing_student.full_name if existing_student else "")
    resolved_email = email or (existing_student.email if existing_student else "")

    cookie_val = request.cookies.get(DeviceBindingManager.COOKIE_NAME)
    parsed = device_mgr.parse_device_cookie(cookie_val)
    explicit_device = str(payload.get("device_id") or "").strip()
    if explicit_device:
        device_id = explicit_device
    elif parsed and existing_student is not None and int(existing_student.id) == int(parsed[1]):
        device_id = parsed[0]
    else:
        device_id = secrets.token_hex(12)

    if raw_course_id is not None or course_code:
        try:
            _course, student = svc.enroll_student_by_code(
                course_code=course_code,
                course_id=int(raw_course_id) if raw_course_id is not None else None,
                full_name=resolved_name,
                email=resolved_email,
                student_id_number=student_id_number,
                device_id=device_id,
            )
        except CourseError as exc:
            return jsonify({"error": exc.message}), exc.status_code
    else:
        student, err = resolve_or_upsert_student(
            db,
            full_name=resolved_name,
            email=resolved_email,
            student_id_number=student_id_number,
            device_id=device_id,
        )
        if err is not None:
            return jsonify({"error": err[0]}), err[1]
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
    """Allow an authenticated student to join a school course by course_id or course_code."""
    payload: dict[str, Any] = request.get_json(silent=True) or {}
    course_code = str(payload.get("course_code", "")).strip().upper()
    raw_course_id = payload.get("course_id")
    course_id_val: int | None = None
    if raw_course_id is not None and str(raw_course_id).strip() != "":
        try:
            course_id_val = int(raw_course_id)
        except (TypeError, ValueError):
            return jsonify({"error": "Invalid course_id."}), 400

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
            course_id=course_id_val,
            full_name=student.full_name,
            email=student.email,
            student_id_number=student.student_id_number,
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


def _emit_lecturer_roster_notifications(db: Any, course: Course, event: str, payload: dict[str, Any]) -> None:
    ids: list[int] = []
    if course.lecturer_id is not None:
        ids.append(course.lecturer_id)
    for row in db.query(CourseStaff).filter_by(course_id=course.id).all():
        if row.lecturer_id not in ids:
            ids.append(row.lecturer_id)
    for lid in ids:
        socketio.emit(event, payload, to=f"lecturer_notify_{lid}")


@courses_bp.post("/api/student/enroll-request")
def api_student_enroll_request():
    """Submit a pending join request by manual course_number or course_id, notifying the lecturer."""
    payload: dict[str, Any] = request.get_json(silent=True) or {}
    raw_course_num = str(payload.get("course_number") or payload.get("course_code") or "").strip()
    raw_course_id = payload.get("course_id")
    course_id_val: int | None = None
    if not raw_course_num:
        try:
            course_id_val = int(raw_course_id)
        except (TypeError, ValueError):
            return jsonify({"error": "Enter a valid Course Number."}), 400

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
        course, student_obj, status_val = svc.request_course_enrollment(
            course_id=course_id_val,
            course_number=raw_course_num or None,
            student_id=student.id,
        )
    except CourseError as exc:
        return jsonify({"error": exc.message}), exc.status_code

    if status_val == "pending":
        _emit_lecturer_roster_notifications(
            db,
            course,
            "roster:enrollment_request",
            {
                "course_id": course.id,
                "course_code": course.course_code,
                "course_name": course.name,
                "student_id": student_obj.id,
                "full_name": student_obj.full_name,
                "student_id_number": student_obj.student_id_number or "-",
            },
        )

    return (
        jsonify(
            {
                "status": status_val,
                "course_id": course.id,
                "course_code": course.course_code,
                "course_name": course.name,
                "dashboard": svc.build_student_dashboard_payload(student_id=student_obj.id),
            }
        ),
        202 if status_val == "pending" else 200,
    )


@courses_bp.get("/api/courses/<int:course_id>/enrollment-requests")
def api_list_enrollment_requests(course_id: int):
    _course, _lec, err = require_course_access(course_id)
    if err is not None:
        return err
    svc = _get_course_service()
    overview = svc.get_course_overview(course_id)
    return jsonify(
        {
            "course_id": course_id,
            "pending_requests": overview.get("pending_requests", []),
            "students": overview.get("students", []),
        }
    )


@courses_bp.post("/api/courses/<int:course_id>/enrollment-requests/<int:student_id>/approve")
def api_approve_enrollment_request(course_id: int, student_id: int):
    _course, lecturer, err = require_course_access(course_id)
    if err is not None:
        return err
    assert lecturer is not None
    svc = _get_course_service()
    try:
        course, st = svc.approve_enrollment_request(
            course_id=course_id,
            student_id=student_id,
            lecturer_id=lecturer.id,
            ip_address=get_client_ip(),
        )
        overview = svc.get_course_overview(course_id)
    except CourseError as exc:
        return jsonify({"error": exc.message}), exc.status_code

    socketio.emit(
        "enrollment:updated",
        {"course_id": course.id, "student_id": st.id, "status": "approved"},
    )
    return jsonify(
        {
            "status": "approved",
            "student_id": st.id,
            "pending_requests": overview.get("pending_requests", []),
            "students": overview.get("students", []),
        }
    )


@courses_bp.post("/api/courses/<int:course_id>/enrollment-requests/<int:student_id>/reject")
def api_reject_enrollment_request(course_id: int, student_id: int):
    _course, lecturer, err = require_course_access(course_id)
    if err is not None:
        return err
    assert lecturer is not None
    svc = _get_course_service()
    try:
        course, st = svc.reject_enrollment_request(
            course_id=course_id,
            student_id=student_id,
            lecturer_id=lecturer.id,
            ip_address=get_client_ip(),
        )
        overview = svc.get_course_overview(course_id)
    except CourseError as exc:
        return jsonify({"error": exc.message}), exc.status_code

    socketio.emit(
        "enrollment:updated",
        {"course_id": course.id, "student_id": st.id, "status": "rejected"},
    )
    return jsonify(
        {
            "status": "rejected",
            "student_id": st.id,
            "pending_requests": overview.get("pending_requests", []),
            "students": overview.get("students", []),
        }
    )
