from __future__ import annotations

import base64
from datetime import datetime, timezone
import io
import json
import shutil
import threading
import time
import uuid
from pathlib import Path
from typing import Any, Optional
from urllib.parse import quote

import cv2

from flask import (
    Blueprint,
    Response,
    current_app,
    jsonify,
    make_response,
    redirect,
    render_template,
    request,
    send_file,
    url_for,
)

from app import socketio
from app.models import (
    AttendanceRecord,
    ClassSession,
    Course,
    HeadcountSnapshot,
    Student,
)
from app.routes.guards import (
    get_current_lecturer,
    require_session_access,
    verify_session_read_access,
)
from app.services.attendance_service import (
    AttendanceError,
    AttendanceService,
    ProxyAttendanceBlockedError,
)
from app.services.course_service import CourseService
from app.services.identity_helpers import validate_image_magic_bytes
from app.services.face_counter import (
    BoundingBox,
    TiledSCRFDDetector,
    read_image_unicode,
    write_image_unicode,
)
from app.services.qr_service import DeviceBindingManager, resolve_effective_base_url
from app.services.report_exporter import SpreadsheetExporter

attendance_bp = Blueprint("attendance", __name__)


def _get_services() -> tuple[AttendanceService, Any, Any, SpreadsheetExporter]:
    db = current_app.extensions["db_session"]
    qr_service = current_app.extensions["qr_service"]
    device_mgr = current_app.extensions["device_mgr"]
    exporter = current_app.extensions["report_exporter"]
    att_service = AttendanceService(db=db, qr_service=qr_service, device_mgr=device_mgr)
    return att_service, qr_service, device_mgr, exporter


def _resolve_base_url() -> str:
    cfg = current_app.config["CLASSPULSE_CONFIG"]
    return resolve_effective_base_url(cfg.PUBLIC_BASE_URL, request.host_url)


@attendance_bp.get("/")
@attendance_bp.get("/lecturer")
def lecturer_dashboard():
    return redirect(
        url_for(
            "courses.teacher_portal",
            course_id=request.args.get("course_id", type=int),
            session=request.args.get("session", ""),
        )
    )


@attendance_bp.get("/projector/<session_code>")
def projector_view(session_code: str):
    att_service, qr_service, device_mgr, _exporter = _get_services()
    db = current_app.extensions["db_session"]
    code = session_code.strip().upper()
    session = db.query(ClassSession).filter_by(session_code=code).one_or_none()
    if session is None:
        session = att_service.get_or_create_default_session()
    ptok = request.args.get("ptok", "")
    if session.course_id is not None and not device_mgr.verify_projector_token(
        ptok, session.session_code
    ):
        _sess, _lec, err = require_session_access(session.id)
        if err is not None:
            if err[1] == 401:
                return redirect(url_for("courses.teacher_login_page"))
            return "Access denied: projector display belongs to another lecturer.", 403
    course = db.get(Course, session.course_id) if session.course_id else None
    qr_payload = qr_service.build_qr_payload(_resolve_base_url(), session.session_code)
    qr_count = db.query(AttendanceRecord).filter_by(session_id=session.id).count()
    return render_template(
        "projector.html",
        course=course,
        session=session,
        qr_payload=qr_payload,
        qr_count=qr_count,
        projector_token=ptok or device_mgr.create_projector_token(session.session_code),
    )


def _build_checkin_banner(
    session: ClassSession,
    course: Optional[Course],
    student: Optional[Student],
    *,
    attendance_recorded: bool,
) -> dict[str, Any]:
    lecture_num = session.lecture_number
    lecture_label = session.title or (
        f"Lecture {lecture_num}" if lecture_num else "Lecture"
    )
    return {
        "student_name": student.full_name if student else "Student",
        "course_name": course.name if course else "",
        "lecture_number": lecture_num,
        "lecture_label": lecture_label,
        "session_code": session.session_code,
        "session_title": session.title,
        "attendance_recorded": attendance_recorded,
    }


def _render_student_app_for_session(
    db: Any,
    session: ClassSession,
    student: Optional[Student],
    *,
    attendance_recorded: bool,
) -> str:
    course = db.get(Course, session.course_id) if session.course_id else None
    course_svc = CourseService(db=db)
    student_dashboard = course_svc.build_student_dashboard_payload(
        student_id=student.id if student else None
    )
    return render_template(
        "student_app.html",
        dashboard=student_dashboard,
        initial_session_code=session.session_code,
        initial_course_id=session.course_id,
        initial_session_id=session.id,
        checked_in_banner=_build_checkin_banner(
            session,
            course,
            student,
            attendance_recorded=attendance_recorded,
        ),
        from_qr_route=True,
    )


@attendance_bp.get("/s/<session_code>")
def student_entry(session_code: str):
    att_service, qr_service, device_mgr, _exporter = _get_services()
    db = current_app.extensions["db_session"]

    code = session_code.strip().upper()
    session = db.query(ClassSession).filter_by(session_code=code).one_or_none()
    if session is None:
        return render_template(
            "student_checkin.html",
            error_title="Lecture Not Found",
            error_message=f"No lecture was found for code '{code}'.",
            session=None,
            course=None,
        ), 404

    course = db.get(Course, session.course_id) if session.course_id else None
    cookie_raw = request.cookies.get(DeviceBindingManager.COOKIE_NAME)
    parsed_cookie = device_mgr.parse_device_cookie(cookie_raw)
    device_id = parsed_cookie[0] if parsed_cookie else uuid.uuid4().hex
    existing_student_id = parsed_cookie[1] if parsed_cookie else None

    t_param = request.args.get("t")
    sig_param = request.args.get("sig", "")
    bucket_val: Optional[int] = int(t_param) if (t_param and t_param.isdigit()) else None

    # Case 1: Student already checked into this session (by device or signed-in student)
    existing_rec = (
        db.query(AttendanceRecord)
        .filter_by(session_id=session.id, device_id=device_id)
        .one_or_none()
    )
    if existing_rec is None and existing_student_id is not None:
        existing_rec = (
            db.query(AttendanceRecord)
            .filter_by(session_id=session.id, student_id=existing_student_id)
            .one_or_none()
        )
    if existing_rec is not None:
        student = db.get(Student, existing_rec.student_id)
        return _render_student_app_for_session(
            db, session, student, attendance_recorded=True
        )

    if not session.attendance_open:
        if existing_student_id is not None:
            known_student = db.get(Student, existing_student_id)
            if known_student is not None:
                return _render_student_app_for_session(
                    db, session, known_student, attendance_recorded=False
                )
        return render_template(
            "student_checkin.html",
            session=session,
            course=course,
            error_title="Attendance Closed",
            error_message="The lecturer has locked attendance check-in for this lecture.",
        ), 403

    # Verify the rotating QR token
    if bucket_val is None or not qr_service.verify_token(code, sig_param, bucket_val):
        return render_template(
            "student_checkin.html",
            session=session,
            course=course,
            error_title="QR Code Expired",
            error_message="This QR code has expired. Please point your camera at the screen and scan again.",
        ), 410

    # Case 2: Returning student with a valid signed device cookie -> instant auto check-in!
    if existing_student_id is not None:
        known_student = db.get(Student, existing_student_id)
        if known_student is not None:
            try:
                _rec, st, _is_new = att_service.process_checkin(
                    session_code=code,
                    bucket=bucket_val,
                    token=sig_param,
                    full_name=known_student.full_name,
                    email=known_student.email,
                    student_id_number=known_student.student_id_number,
                    device_id=device_id,
                    existing_student_id=known_student.id,
                )
                qr_count = (
                    db.query(AttendanceRecord).filter_by(session_id=session.id).count()
                )
                checkin_event = {
                    "session_code": code,
                    "qr_count": qr_count,
                    "latest_student": st.full_name,
                }
                socketio.emit(
                    "attendance:count_update",
                    checkin_event,
                    to=f"session_{code}",
                )
                socketio.emit(
                    "attendance:new_checkin",
                    checkin_event,
                    to=f"session_{code}",
                )
                resp = make_response(
                    _render_student_app_for_session(
                        db, session, st, attendance_recorded=True
                    )
                )
                resp.set_cookie(
                    DeviceBindingManager.COOKIE_NAME,
                    device_mgr.create_device_cookie(device_id, st.id),
                    max_age=365 * 86400,
                    httponly=True,
                    samesite="Lax",
                    secure=request.is_secure,
                )
                return resp
            except AttendanceError as exc:
                return render_template(
                    "student_checkin.html",
                    session=session,
                    course=course,
                    error_title="Check-In Blocked",
                    error_message=exc.message,
                ), exc.status_code

    # Case 3: First-time student -> issue 300s onboarding grant so typing details won't fail the rotating QR
    onboarding_grant = qr_service.create_onboarding_grant(
        session_code=code, device_id=device_id, bucket=bucket_val
    )
    return render_template(
        "student_checkin.html",
        session=session,
        course=course,
        checked_in=False,
        onboarding_grant=onboarding_grant,
        bucket=bucket_val,
        sig=sig_param,
        device_id=device_id,
        form_values={},
    )


@attendance_bp.post("/api/sessions")
def create_or_update_session():
    att_service, _qr_service, _device_mgr, _exporter = _get_services()
    db = current_app.extensions["db_session"]
    data = request.get_json(silent=True) or request.form.to_dict()

    action = str(data.get("action", "create")).strip()
    if action == "toggle_open":
        session_id = int(data["session_id"])
        session, _lec, err = require_session_access(session_id)
        if err is not None:
            return err
        assert session is not None
        session.attendance_open = not session.attendance_open
        db.commit()
        return jsonify(
            {
                "id": session.id,
                "session_code": session.session_code,
                "attendance_open": session.attendance_open,
            }
        )

    title = str(data.get("title", "New Classroom Lecture")).strip()
    code = str(data.get("session_code", "")).strip() or None
    tolerance = int(data.get("headcount_tolerance", 2))
    raw_course_id = data.get("course_id")
    course_id_val: Optional[int] = None
    if raw_course_id is not None and str(raw_course_id).strip() != "":
        course_id_val = int(raw_course_id)
        from app.routes.guards import require_course_access

        _c, _lec, c_err = require_course_access(course_id_val)
        if c_err is not None:
            return c_err
    session = att_service.create_session(
        title=title,
        session_code=code,
        headcount_tolerance=tolerance,
        course_id=course_id_val,
    )
    if request.is_json:
        return jsonify(
            {
                "id": session.id,
                "session_code": session.session_code,
                "title": session.title,
                "attendance_open": session.attendance_open,
            }
        ), 201
    return redirect(url_for("attendance.lecturer_dashboard", session=session.session_code))


@attendance_bp.get("/api/sessions/<session_code>/qr")
def get_rotating_qr(session_code: str):
    _att_service, qr_service, _device_mgr, _exporter = _get_services()
    db = current_app.extensions["db_session"]
    code = session_code.strip().upper()
    session = db.query(ClassSession).filter_by(session_code=code).one_or_none()
    if session is None:
        return jsonify({"error": "Session not found"}), 404
    _sess, _lec, _stu, err = verify_session_read_access(session.id, allow_open_attendance=True)
    if err is not None:
        return err
    payload = qr_service.build_qr_payload(_resolve_base_url(), code)
    payload["attendance_open"] = session.attendance_open
    return jsonify(payload)


@attendance_bp.post("/api/attendance/checkin")
def api_student_checkin():
    att_service, qr_service, device_mgr, _exporter = _get_services()
    db = current_app.extensions["db_session"]

    data = request.get_json(silent=True) or request.form.to_dict()
    session_code = str(data.get("session_code", "")).strip().upper()
    full_name = str(data.get("full_name", "")).strip() or None
    email = str(data.get("email", "")).strip() or None
    student_id_number = str(data.get("student_id_number", "")).strip() or None
    onboarding_grant = str(data.get("onboarding_grant", "")).strip() or None
    sig = str(data.get("sig", "")).strip() or None
    t_raw = data.get("t")
    bucket = int(t_raw) if (t_raw is not None and str(t_raw).isdigit()) else None

    cookie_raw = request.cookies.get(DeviceBindingManager.COOKIE_NAME)
    parsed_cookie = device_mgr.parse_device_cookie(cookie_raw)
    explicit_device = str(data.get("device_id", "")).strip()
    if parsed_cookie and (not explicit_device or explicit_device == parsed_cookie[0]):
        device_id = parsed_cookie[0]
        existing_student_id = parsed_cookie[1]
    else:
        device_id = explicit_device or uuid.uuid4().hex
        existing_student_id = None

    try:
        rec, student, is_new = att_service.process_checkin(
            session_code=session_code,
            bucket=bucket,
            token=sig,
            full_name=full_name,
            email=email,
            student_id_number=student_id_number,
            device_id=device_id,
            existing_student_id=existing_student_id,
            onboarding_grant=onboarding_grant,
        )
    except AttendanceError as exc:
        if request.is_json:
            return jsonify({"error": exc.message, "status": "error"}), exc.status_code
        session = (
            db.query(ClassSession).filter_by(session_code=session_code).one_or_none()
        )
        course = (
            db.get(Course, session.course_id)
            if (session is not None and session.course_id)
            else None
        )
        can_retry_inline = bool(
            session is not None
            and session.attendance_open
            and not isinstance(exc, ProxyAttendanceBlockedError)
            and (
                (
                    onboarding_grant
                    and qr_service.verify_onboarding_grant(onboarding_grant, session_code)
                    is not None
                )
                or (
                    bucket is not None
                    and sig
                    and qr_service.verify_token(session_code, sig, bucket)
                )
            )
        )
        if can_retry_inline:
            return (
                render_template(
                    "student_checkin.html",
                    session=session,
                    course=course,
                    checked_in=False,
                    inline_error=exc.message,
                    form_values={
                        "full_name": full_name or "",
                        "student_id_number": student_id_number or "",
                        "email": email or "",
                    },
                    onboarding_grant=onboarding_grant or "",
                    bucket=bucket if bucket is not None else "",
                    sig=sig or "",
                    device_id=device_id,
                ),
                exc.status_code,
            )
        return render_template(
            "student_checkin.html",
            session=session,
            course=course,
            error_title="Check-In Failed",
            error_message=exc.message,
        ), exc.status_code

    session = db.get(ClassSession, rec.session_id)
    qr_count = db.query(AttendanceRecord).filter_by(session_id=rec.session_id).count()
    checkin_event = {
        "session_code": session_code,
        "qr_count": qr_count,
        "latest_student": student.full_name,
    }
    socketio.emit(
        "attendance:count_update",
        checkin_event,
        to=f"session_{session_code}",
    )
    socketio.emit(
        "attendance:new_checkin",
        checkin_event,
        to=f"session_{session_code}",
    )

    signed_cookie = device_mgr.create_device_cookie(device_id, student.id)
    if request.is_json:
        resp = jsonify(
            {
                "status": "checked_in",
                "is_new_checkin": is_new,
                "student_id": student.id,
                "student_id_number": student.student_id_number,
                "full_name": student.full_name,
                "email": student.email,
                "checked_in_at": rec.checked_in_at.isoformat(),
                "is_late": rec.is_late,
                "qr_count": qr_count,
            }
        )
    else:
        target_url = url_for(
            "courses.student_app",
            course_id=session.course_id if (session and session.course_id is not None) else None,
            session=session.session_code if session else session_code,
            checked_in=1,
        )
        resp = make_response(redirect(target_url, code=303))

    resp.set_cookie(
        DeviceBindingManager.COOKIE_NAME,
        signed_cookie,
        max_age=365 * 86400,
        httponly=True,
        samesite="Lax",
        secure=request.is_secure,
    )
    return resp


@attendance_bp.post("/api/sessions/<int:session_id>/roster")
def import_session_roster(session_id: int):
    att_service, _qr_service, _device_mgr, _exporter = _get_services()
    session, _lec, err = require_session_access(session_id)
    if err is not None:
        return err
    assert session is not None

    csv_text = ""
    if "roster_file" in request.files and request.files["roster_file"].filename:
        raw = request.files["roster_file"].read()
        csv_text = raw.decode("utf-8-sig", errors="replace")
    else:
        data = request.get_json(silent=True) or request.form.to_dict()
        csv_text = str(data.get("csv_text", ""))

    imported = att_service.import_roster_csv(session_id, csv_text)
    verdict = att_service.get_audit_summary(session_id)
    if request.is_json:
        return jsonify(
            {
                "status": "ok",
                "imported": imported,
                "verdict": verdict.to_dict(),
            }
        )
    return redirect(
        url_for("attendance.lecturer_dashboard", session=session.session_code)
    )


def _get_pairings_store() -> tuple[threading.Lock, dict[str, dict[str, Any]]]:
    """Return thread-safe in-memory pairing store for phone headcount QR sessions."""
    store = current_app.extensions.get("headcount_cam_pairings")
    if store is None:
        store = (threading.Lock(), {})
        current_app.extensions["headcount_cam_pairings"] = store
    return store


def _prune_old_pairings(registry: dict[str, dict[str, Any]], now_ts: float) -> None:
    stale = [
        nonce
        for nonce, entry in registry.items()
        if (now_ts - float(entry.get("created_at", 0))) > 3600.0
    ]
    for nonce in stale:
        registry.pop(nonce, None)


def _run_headcount_detection_and_persist(
    *,
    db: Any,
    detector: Any,
    att_service: AttendanceService,
    session: ClassSession,
    raw_dest: Path,
    ann_dest: Path,
    source: str = "desktop",
) -> tuple[HeadcountSnapshot, dict[str, Any]]:
    """Run face detection on raw_dest, save HeadcountSnapshot, broadcast headcount:updated, and return payload."""
    img = read_image_unicode(raw_dest)
    h, w = img.shape[:2]
    if max(h, w) > 1920:
        scale = 1920.0 / float(max(h, w))
        img = cv2.resize(
            img,
            (max(1, int(round(w * scale))), max(1, int(round(h * scale)))),
            interpolation=cv2.INTER_AREA,
        )

    if hasattr(detector, "detect_with_orientation"):
        boxes, img, _was_rotated = detector.detect_with_orientation(img)
    else:
        boxes = detector.detect(img)

    # Always normalize stored raw_dest as an upright JPEG so web canvas & <img> render identically
    write_image_unicode(raw_dest, img)

    if isinstance(detector, TiledSCRFDDetector):
        detector.render_annotated_image(raw_dest, boxes, ann_dest, image=img)
    else:
        detector.render_annotated_image(raw_dest, boxes, ann_dest)
    boxes_dicts = [b.to_dict() for b in boxes]

    snap = HeadcountSnapshot(
        session_id=session.id,
        raw_image_path=str(raw_dest),
        annotated_image_path=str(ann_dest),
        auto_face_count=len(boxes),
        verified_face_count=len(boxes),
        boxes_json=json.dumps(boxes_dicts),
    )
    db.add(snap)
    db.commit()

    verdict = att_service.get_audit_summary(session.id)
    raw_url = url_for(
        "attendance.get_headcount_image",
        session_id=session.id,
        kind="raw",
        snap_id=snap.id,
    )
    ann_url = url_for(
        "attendance.get_headcount_image",
        session_id=session.id,
        kind="annotated",
        snap_id=snap.id,
    )
    payload = {
        "snapshot_id": snap.id,
        "session_id": session.id,
        "session_code": session.session_code,
        "auto_face_count": snap.auto_face_count,
        "verified_face_count": snap.verified_face_count,
        "auto_count": snap.auto_face_count,
        "verified_count": snap.verified_face_count,
        "boxes": boxes_dicts,
        "raw_image_url": raw_url,
        "image_url": raw_url,
        "annotated_image_url": ann_url,
        "verdict": verdict.to_dict(),
        "source": source,
    }
    socketio.emit("headcount:updated", payload, to=f"session_{session.session_code}")
    return snap, payload


def _decode_base64_image_data(raw_data: str) -> Optional[bytes]:
    """Decode a data URL or base64 image string into raw bytes."""
    txt = (raw_data or "").strip()
    if not txt:
        return None
    if txt.startswith("data:") and "," in txt:
        txt = txt.split(",", 1)[1]
    try:
        return base64.b64decode(txt, validate=False)
    except Exception:
        return None


@attendance_bp.post("/api/sessions/<int:session_id>/headcount")
def upload_and_count_classroom_photo(session_id: int):
    att_service, _qr_service, _device_mgr, _exporter = _get_services()
    db = current_app.extensions["db_session"]
    cfg = current_app.config["CLASSPULSE_CONFIG"]
    detector = current_app.extensions["face_detector"]

    session, _lec, err = require_session_access(session_id)
    if err is not None:
        return err
    assert session is not None

    data = request.get_json(silent=True) or request.form.to_dict()
    source_kind = str(data.get("source") or "desktop").strip() or "desktop"

    cfg.UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
    upload_id = uuid.uuid4().hex[:10]
    raw_dest = cfg.UPLOAD_DIR / f"session_{session_id}_{upload_id}_raw.jpg"
    ann_dest = cfg.UPLOAD_DIR / f"session_{session_id}_{upload_id}_annotated.jpg"

    if "photo" in request.files and request.files["photo"].filename:
        photo_file = request.files["photo"]
        file_bytes = photo_file.read()
        if not validate_image_magic_bytes(file_bytes):
            return jsonify({"error": "Invalid image upload: expected JPEG, PNG, GIF, or WebP file."}), 400
        raw_dest.write_bytes(file_bytes)
    elif data.get("image_data"):
        file_bytes = _decode_base64_image_data(str(data.get("image_data", "")))
        if not file_bytes or not validate_image_magic_bytes(file_bytes):
            return jsonify({"error": "Invalid camera capture image data."}), 400
        raw_dest.write_bytes(file_bytes)
        source_kind = str(data.get("source") or "camera").strip() or "camera"
    else:
        return jsonify({"error": "No classroom photo file provided."}), 400

    try:
        _snap, payload = _run_headcount_detection_and_persist(
            db=db,
            detector=detector,
            att_service=att_service,
            session=session,
            raw_dest=raw_dest,
            ann_dest=ann_dest,
            source=source_kind,
        )
    except Exception as exc:  # noqa: BLE001
        return jsonify({"error": f"Invalid image file: {exc}"}), 400

    return jsonify(payload)


@attendance_bp.post("/api/sessions/<int:session_id>/headcount/approve-discrepancy")
def approve_headcount_discrepancy(session_id: int):
    """Allow the lecturer to review and approve a QR vs. headcount discrepancy alert."""
    att_service, _qr_service, _device_mgr, _exporter = _get_services()
    db = current_app.extensions["db_session"]

    session, _lec, err = require_session_access(session_id)
    if err is not None:
        return err
    assert session is not None

    snap = (
        db.query(HeadcountSnapshot)
        .filter_by(session_id=session.id)
        .order_by(HeadcountSnapshot.id.desc())
        .first()
    )
    if snap is None:
        return jsonify({"error": "No headcount snapshot exists for this lecture yet."}), 404

    data = request.get_json(silent=True) or {}
    snap.discrepancy_approved = True
    snap.discrepancy_approved_at = datetime.now(timezone.utc)
    if data.get("lecturer_note"):
        snap.lecturer_note = str(data["lecturer_note"])[:500]
    db.commit()

    verdict = att_service.get_audit_summary(session.id)
    payload = {
        "ok": True,
        "snapshot_id": snap.id,
        "session_id": session.id,
        "session_code": session.session_code,
        "auto_face_count": snap.auto_face_count,
        "verified_face_count": snap.verified_face_count,
        "auto_count": snap.auto_face_count,
        "verified_count": snap.verified_face_count,
        "verdict": verdict.to_dict(),
        "source": "discrepancy_approved",
    }
    socketio.emit("headcount:updated", payload, to=f"session_{session.session_code}")
    return jsonify(payload)


@attendance_bp.patch("/api/sessions/<int:session_id>/headcount")
def update_headcount_verification(session_id: int):
    att_service, _qr_service, _device_mgr, _exporter = _get_services()
    db = current_app.extensions["db_session"]
    detector = current_app.extensions["face_detector"]

    session, _lec, err = require_session_access(session_id)
    if err is not None:
        return err
    assert session is not None

    snap = (
        db.query(HeadcountSnapshot)
        .filter_by(session_id=session.id)
        .order_by(HeadcountSnapshot.id.desc())
        .first()
    )
    if snap is None:
        return jsonify({"error": "No headcount snapshot exists for this session"}), 404

    data = request.get_json(silent=True) or {}
    if "boxes" in data and isinstance(data["boxes"], list):
        parsed_boxes = [
            BoundingBox.from_dict(item, default_id=idx + 1)
            for idx, item in enumerate(data["boxes"])
        ]
        for idx, b in enumerate(parsed_boxes, start=1):
            b.label_id = idx
        snap.boxes_json = json.dumps([b.to_dict() for b in parsed_boxes])
        snap.verified_face_count = len(parsed_boxes)
        if Path(snap.raw_image_path).exists():
            detector.render_annotated_image(
                snap.raw_image_path, parsed_boxes, snap.annotated_image_path
            )
    elif "verified_face_count" in data:
        snap.verified_face_count = max(0, int(data["verified_face_count"]))

    if "lecturer_note" in data:
        snap.lecturer_note = str(data["lecturer_note"])[:500]

    qr_count = db.query(AttendanceRecord).filter_by(session_id=session.id).count()
    if qr_count > int(snap.verified_face_count or 0):
        snap.discrepancy_approved = False
        snap.discrepancy_approved_at = None

    db.commit()
    verdict = att_service.get_audit_summary(session.id)
    payload = {
        "snapshot_id": snap.id,
        "session_id": session.id,
        "session_code": session.session_code,
        "auto_face_count": snap.auto_face_count,
        "verified_face_count": snap.verified_face_count,
        "auto_count": snap.auto_face_count,
        "verified_count": snap.verified_face_count,
        "boxes": json.loads(snap.boxes_json),
        "verdict": verdict.to_dict(),
        "source": "desktop_patch",
    }
    socketio.emit("headcount:updated", payload, to=f"session_{session.session_code}")
    return jsonify(payload)


@attendance_bp.get("/api/sessions/<int:session_id>/headcount/image/<kind>")
def get_headcount_image(session_id: int, kind: str):
    _sess, _lec, err = require_session_access(session_id)
    if err is not None:
        return err

    db = current_app.extensions["db_session"]
    snap_id = request.args.get("snap_id", type=int)
    query = db.query(HeadcountSnapshot).filter_by(session_id=session_id)
    if snap_id:
        snap = query.filter_by(id=snap_id).one_or_none()
    else:
        snap = query.order_by(HeadcountSnapshot.id.desc()).first()

    if snap is None:
        return jsonify({"error": "Snapshot not found"}), 404

    img_path = Path(
        snap.raw_image_path if kind == "raw" else snap.annotated_image_path
    )
    if not img_path.exists():
        return jsonify({"error": "Image file missing on disk"}), 404

    return send_file(io.BytesIO(img_path.read_bytes()), mimetype="image/jpeg")


@attendance_bp.get("/api/sessions/<int:session_id>/status")
@attendance_bp.get("/api/sessions/<int:session_id>/overview")
def get_session_live_status(session_id: int):
    session, _lec, _stu, err = verify_session_read_access(session_id, allow_open_attendance=True)
    if err is not None:
        return err
    assert session is not None
    att_service, _qr_service, _device_mgr, _exporter = _get_services()
    db = current_app.extensions["db_session"]

    verdict = att_service.get_audit_summary(session.id)
    rows = att_service.get_attendance_rows(session.id)
    latest_snap = (
        db.query(HeadcountSnapshot)
        .filter_by(session_id=session.id)
        .order_by(HeadcountSnapshot.id.desc())
        .first()
    )
    return jsonify(
        {
            "session": {
                "id": session.id,
                "session_code": session.session_code,
                "title": session.title,
                "attendance_open": session.attendance_open,
            },
            "verdict": verdict.to_dict(),
            "rows": rows,
            "snapshot": (
                {
                    "id": latest_snap.id,
                    "auto_face_count": latest_snap.auto_face_count,
                    "verified_face_count": latest_snap.verified_face_count,
                    "boxes": json.loads(latest_snap.boxes_json),
                    "raw_image_url": url_for(
                        "attendance.get_headcount_image",
                        session_id=session.id,
                        kind="raw",
                        snap_id=latest_snap.id,
                    ),
                }
                if latest_snap
                else None
            ),
        }
    )


@attendance_bp.get("/api/sessions/<int:session_id>/report")
def download_attendance_report(session_id: int):
    att_service, _qr_service, _device_mgr, exporter = _get_services()
    session, _lec, err = require_session_access(session_id)
    if err is not None:
        return err
    assert session is not None

    fmt = request.args.get("format", "xlsx").lower()
    verdict = att_service.get_audit_summary(session.id)
    rows = att_service.get_attendance_rows(session.id)

    if fmt == "csv":
        csv_data = exporter.export_csv(session, verdict, rows)
        filename = f"ClassPulse_{session.session_code}_Audit_Report.csv"
        return Response(
            csv_data.encode("utf-8"),
            mimetype="text/csv; charset=utf-8",
            headers={"Content-Disposition": f'attachment; filename="{filename}"'},
        )

    xlsx_bytes = exporter.export_xlsx(session, verdict, rows)
    filename = f"ClassPulse_{session.session_code}_Audit_Report.xlsx"
    return send_file(
        io.BytesIO(xlsx_bytes),
        mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        as_attachment=True,
        download_name=filename,
    )


@attendance_bp.post("/api/maintenance/purge-expired-photos")
def purge_expired_photos():
    lecturer = get_current_lecturer()
    if lecturer is None:
        return jsonify({"error": "Lecturer sign-in required."}), 401
    att_service, _qr, _dm, _exp = _get_services()
    cfg = current_app.config["CLASSPULSE_CONFIG"]
    payload = request.get_json(silent=True) or {}
    hours = int(payload.get("retention_hours", cfg.HEADCOUNT_RETENTION_HOURS or 72))
    removed = att_service.purge_expired_headcount_photos(hours)
    return jsonify({"deleted_files": removed, "retention_hours": hours})


# ==========================================================================
# Professor Phone Headcount Companion (Pairing QR + Mobile Capture + ±1 Step)
# ==========================================================================


def _verify_phone_cam_request(
    session_code: str,
    token: Optional[str],
) -> tuple[Optional[ClassSession], Optional[dict[str, Any]], Optional[tuple[str, int]]]:
    """Validate ctok + session_code + non-revoked nonce for phone camera routes."""
    _att, _qr, device_mgr, _exp = _get_services()
    db = current_app.extensions["db_session"]
    code = session_code.strip().upper()
    session = db.query(ClassSession).filter_by(session_code=code).one_or_none()
    if session is None:
        return None, None, (f"No classroom session exists with code '{code}'.", 404)

    claims = device_mgr.verify_headcount_cam_token(token, code, max_age=1800)
    if claims is None or int(claims["session_id"]) != int(session.id):
        return (
            session,
            None,
            (
                "This phone camera link has expired or is invalid. Click 'Phone Camera' on your laptop to generate a fresh QR code.",
                403,
            ),
        )

    lock, registry = _get_pairings_store()
    with lock:
        entry = registry.get(claims["nonce"])
        if entry is not None and entry.get("revoked"):
            return (
                session,
                None,
                (
                    "This phone camera QR was replaced by a newer code. Please scan the latest QR on your laptop.",
                    403,
                ),
            )
    return session, claims, None


@attendance_bp.post("/api/sessions/<int:session_id>/headcount-cam/pair")
def create_phone_headcount_pairing(session_id: int):
    """Issue a scoped 30-minute phone camera QR code for the active lecture."""
    _att, qr_service, device_mgr, _exp = _get_services()
    session, lecturer, err = require_session_access(session_id)
    if err is not None:
        return err
    assert session is not None

    data = request.get_json(silent=True) or {}
    revoke_nonce = str(data.get("revoke_nonce", "")).strip()

    nonce = uuid.uuid4().hex[:16]
    lec_id = int(lecturer.id) if lecturer is not None else 0
    ctok = device_mgr.create_headcount_cam_token(
        session_id=session.id,
        session_code=session.session_code,
        lecturer_id=lec_id,
        nonce=nonce,
    )

    now_ts = time.time()
    lock, registry = _get_pairings_store()
    with lock:
        _prune_old_pairings(registry, now_ts)
        if revoke_nonce and revoke_nonce in registry:
            if int(registry[revoke_nonce].get("session_id", -1)) == int(session.id):
                registry[revoke_nonce]["revoked"] = True
        registry[nonce] = {
            "session_id": int(session.id),
            "session_code": session.session_code,
            "lecturer_id": lec_id,
            "created_at": now_ts,
            "connected": False,
            "connected_at": None,
            "revoked": False,
        }

    base_url = _resolve_base_url().rstrip("/")
    pair_url = f"{base_url}/headcount-cam/{session.session_code}?ctok={quote(ctok, safe='')}"
    svg_data_uri = qr_service.render_qr_svg_data_uri(pair_url)
    return jsonify(
        {
            "nonce": nonce,
            "session_id": session.id,
            "session_code": session.session_code,
            "url": pair_url,
            "svg_data_uri": svg_data_uri,
            "expires_in_seconds": 1800,
        }
    )


@attendance_bp.get("/api/sessions/<int:session_id>/headcount-cam/pair/<nonce>/status")
def get_phone_headcount_pairing_status(session_id: int, nonce: str):
    """Polling fallback for desktop QR modal auto-close when professor scans on phone."""
    session, _lec, err = require_session_access(session_id)
    if err is not None:
        return err
    assert session is not None
    db = current_app.extensions["db_session"]

    lock, registry = _get_pairings_store()
    with lock:
        entry = registry.get(nonce.strip())
        if entry is None or int(entry.get("session_id", -1)) != int(session.id):
            return jsonify({"error": "Pairing session not found."}), 404
        connected = bool(entry.get("connected"))
        connected_at = entry.get("connected_at")
        revoked = bool(entry.get("revoked"))

    latest_snap = (
        db.query(HeadcountSnapshot)
        .filter_by(session_id=session.id)
        .order_by(HeadcountSnapshot.id.desc())
        .first()
    )
    return jsonify(
        {
            "nonce": nonce.strip(),
            "connected": connected,
            "connected_at": connected_at,
            "revoked": revoked,
            "latest_snapshot_id": latest_snap.id if latest_snap else None,
        }
    )


@attendance_bp.get("/headcount-cam/<session_code>")
def phone_headcount_cam_view(session_code: str):
    """Mobile-first companion camera page opened when professor scans the pairing QR."""
    ctok = request.args.get("ctok", "").strip()
    session, claims, err = _verify_phone_cam_request(session_code, ctok)
    if err is not None or session is None or claims is None:
        msg, status_code = err if err is not None else ("Invalid camera link.", 403)
        return (
            render_template(
                "phone_headcount_cam.html",
                error_title="Camera Link Unavailable",
                error_message=msg,
                course=None,
                session=session,
                ctok="",
                latest_snap=None,
            ),
            status_code,
        )

    now_ts = time.time()
    nonce = claims["nonce"]
    lock, registry = _get_pairings_store()
    with lock:
        entry = registry.get(nonce)
        if entry is None:
            registry[nonce] = {
                "session_id": int(session.id),
                "session_code": session.session_code,
                "lecturer_id": int(claims["lecturer_id"]),
                "created_at": now_ts,
                "connected": True,
                "connected_at": now_ts,
                "revoked": False,
            }
        else:
            entry["connected"] = True
            if not entry.get("connected_at"):
                entry["connected_at"] = now_ts

    socketio.emit(
        "headcount:phone_connected",
        {
            "session_id": session.id,
            "session_code": session.session_code,
            "nonce": nonce,
            "connected_at": now_ts,
        },
        to=f"session_{session.session_code}",
    )

    db = current_app.extensions["db_session"]
    latest = (
        db.query(HeadcountSnapshot)
        .filter_by(session_id=session.id)
        .order_by(HeadcountSnapshot.id.desc())
        .first()
    )
    latest_snap = (
        {
            "snapshot_id": latest.id,
            "auto_count": latest.auto_face_count,
            "verified_count": latest.verified_face_count,
            "annotated_image_url": url_for(
                "attendance.get_phone_headcount_image",
                session_code=session.session_code,
                snap_id=latest.id,
                ctok=ctok,
            ),
        }
        if latest
        else None
    )

    course = db.get(Course, session.course_id) if session.course_id else None
    return render_template(
        "phone_headcount_cam.html",
        error_title=None,
        error_message=None,
        course=course,
        session=session,
        ctok=ctok,
        latest_snap=latest_snap,
    )


@attendance_bp.post("/api/headcount-cam/<session_code>")
def upload_phone_headcount_photo(session_code: str):
    """Receive auto-uploaded JPEG from professor's phone, run SCRFD, and sync live to desktop."""
    ctok = (
        request.headers.get("X-Headcount-Token", "").strip()
        or str(request.form.get("ctok", "")).strip()
    )
    session, _claims, err = _verify_phone_cam_request(session_code, ctok)
    if err is not None or session is None:
        msg, status_code = err if err is not None else ("Unauthorized.", 403)
        return jsonify({"error": msg}), status_code

    if "photo" not in request.files or not request.files["photo"].filename:
        return jsonify({"error": "No classroom photo provided."}), 400

    file_bytes = request.files["photo"].read()
    if not validate_image_magic_bytes(file_bytes):
        return (
            jsonify(
                {"error": "Invalid image upload: expected JPEG, PNG, GIF, or WebP file."}
            ),
            400,
        )

    att_service, _qr, _dm, _exp = _get_services()
    db = current_app.extensions["db_session"]
    cfg = current_app.config["CLASSPULSE_CONFIG"]
    detector = current_app.extensions["face_detector"]

    cfg.UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
    upload_id = uuid.uuid4().hex[:10]
    raw_dest = cfg.UPLOAD_DIR / f"session_{session.id}_{upload_id}_raw.jpg"
    ann_dest = cfg.UPLOAD_DIR / f"session_{session.id}_{upload_id}_annotated.jpg"
    raw_dest.write_bytes(file_bytes)

    try:
        snap, payload = _run_headcount_detection_and_persist(
            db=db,
            detector=detector,
            att_service=att_service,
            session=session,
            raw_dest=raw_dest,
            ann_dest=ann_dest,
            source="phone",
        )
    except Exception as exc:  # noqa: BLE001
        return jsonify({"error": f"Invalid image file: {exc}"}), 400

    phone_payload = dict(payload)
    phone_payload["annotated_image_url"] = url_for(
        "attendance.get_phone_headcount_image",
        session_code=session.session_code,
        snap_id=snap.id,
        ctok=ctok,
    )
    return jsonify(phone_payload)


@attendance_bp.get("/api/headcount-cam/<session_code>/image/<int:snap_id>")
def get_phone_headcount_image(session_code: str, snap_id: int):
    """Stream annotated headcount JPEG to the paired phone for brief visual verification."""
    ctok = request.args.get("ctok", "").strip()
    session, _claims, err = _verify_phone_cam_request(session_code, ctok)
    if err is not None or session is None:
        msg, status_code = err if err is not None else ("Unauthorized.", 403)
        return jsonify({"error": msg}), status_code

    db = current_app.extensions["db_session"]
    snap = (
        db.query(HeadcountSnapshot)
        .filter_by(id=snap_id, session_id=session.id)
        .one_or_none()
    )
    if snap is None:
        return jsonify({"error": "Headcount snapshot not found."}), 404

    img_path = Path(snap.annotated_image_path)
    if not img_path.exists():
        img_path = Path(snap.raw_image_path)
    if not img_path.exists():
        return jsonify({"error": "Image file not found on disk."}), 404
    return send_file(io.BytesIO(img_path.read_bytes()), mimetype="image/jpeg")


@attendance_bp.patch("/api/headcount-cam/<session_code>")
def adjust_phone_headcount_count(session_code: str):
    """Quick +1 / -1 verified headcount adjustment from professor's phone, synced live to laptop."""
    data = request.get_json(silent=True) or {}
    ctok = (
        request.headers.get("X-Headcount-Token", "").strip()
        or str(data.get("ctok", "")).strip()
    )
    session, _claims, err = _verify_phone_cam_request(session_code, ctok)
    if err is not None or session is None:
        msg, status_code = err if err is not None else ("Unauthorized.", 403)
        return jsonify({"error": msg}), status_code

    att_service, _qr, _dm, _exp = _get_services()
    db = current_app.extensions["db_session"]

    snap_id = data.get("snapshot_id")
    query = db.query(HeadcountSnapshot).filter_by(session_id=session.id)
    if snap_id is not None:
        snap = query.filter_by(id=int(snap_id)).one_or_none()
    else:
        snap = query.order_by(HeadcountSnapshot.id.desc()).first()
    if snap is None:
        return jsonify({"error": "No headcount snapshot exists for this session."}), 404

    if "delta" in data:
        try:
            delta = int(data["delta"])
        except (TypeError, ValueError):
            return jsonify({"error": "delta must be +1 or -1."}), 400
        if delta not in (-1, 1):
            return jsonify({"error": "delta must be +1 or -1."}), 400
        snap.verified_face_count = max(0, int(snap.verified_face_count) + delta)
    elif "verified_face_count" in data:
        try:
            snap.verified_face_count = max(0, int(data["verified_face_count"]))
        except (TypeError, ValueError):
            return jsonify({"error": "Invalid verified_face_count."}), 400
    else:
        return jsonify({"error": "Provide delta (+1 or -1) or verified_face_count."}), 400

    qr_count = db.query(AttendanceRecord).filter_by(session_id=session.id).count()
    if qr_count > int(snap.verified_face_count or 0):
        snap.discrepancy_approved = False
        snap.discrepancy_approved_at = None

    db.commit()
    verdict = att_service.get_audit_summary(session.id)
    payload = {
        "snapshot_id": snap.id,
        "session_id": session.id,
        "session_code": session.session_code,
        "auto_face_count": snap.auto_face_count,
        "verified_face_count": snap.verified_face_count,
        "auto_count": snap.auto_face_count,
        "verified_count": snap.verified_face_count,
        "boxes": json.loads(snap.boxes_json),
        "verdict": verdict.to_dict(),
        "source": "phone_step",
    }
    socketio.emit("headcount:updated", payload, to=f"session_{session.session_code}")
    return jsonify(payload)
