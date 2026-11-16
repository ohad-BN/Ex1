from __future__ import annotations

import io
import json
import shutil
import uuid
from pathlib import Path
from typing import Any, Optional

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
    HeadcountSnapshot,
    Student,
)
from app.routes.guards import get_current_lecturer, require_session_access
from app.services.attendance_service import (
    AttendanceError,
    AttendanceService,
)
from app.services.course_service import CourseService
from app.services.identity_helpers import validate_image_magic_bytes
from app.services.face_counter import BoundingBox, read_image_unicode
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
    qr_payload = qr_service.build_qr_payload(_resolve_base_url(), session.session_code)
    qr_count = db.query(AttendanceRecord).filter_by(session_id=session.id).count()
    return render_template(
        "projector.html",
        session=session,
        qr_payload=qr_payload,
        qr_count=qr_count,
        projector_token=ptok or device_mgr.create_projector_token(session.session_code),
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
            error_title="Session Not Found",
            error_message=f"No classroom session exists with code '{code}'.",
            session=None,
        ), 404

    cookie_raw = request.cookies.get(DeviceBindingManager.COOKIE_NAME)
    parsed_cookie = device_mgr.parse_device_cookie(cookie_raw)
    device_id = parsed_cookie[0] if parsed_cookie else uuid.uuid4().hex
    existing_student_id = parsed_cookie[1] if parsed_cookie else None

    t_param = request.args.get("t")
    sig_param = request.args.get("sig", "")
    bucket_val: Optional[int] = int(t_param) if (t_param and t_param.isdigit()) else None

    # Case 1: Student already checked into this session on this device and refreshes page
    existing_rec = (
        db.query(AttendanceRecord)
        .filter_by(session_id=session.id, device_id=device_id)
        .one_or_none()
    )
    if existing_rec is not None:
        student = db.get(Student, existing_rec.student_id)
        course_svc = CourseService(db=db)
        student_dashboard = course_svc.build_student_dashboard_payload(
            student_id=student.id if student else None
        )
        return render_template(
            "student_checkin.html",
            session=session,
            checked_in=True,
            student=student,
            record=existing_rec,
            student_dashboard=student_dashboard,
        )

    if not session.attendance_open:
        return render_template(
            "student_checkin.html",
            session=session,
            error_title="Attendance Closed",
            error_message="The lecturer has locked attendance check-in for this session.",
        ), 403

    # Verify the 5-second rotating QR token
    if bucket_val is None or not qr_service.verify_token(code, sig_param, bucket_val):
        return render_template(
            "student_checkin.html",
            session=session,
            error_title="QR Code Expired",
            error_message="This 5-second QR token has expired. Please point your camera at the live projector screen and scan again.",
        ), 410

    # Case 2: Returning student with a valid signed device cookie -> instant auto check-in!
    if existing_student_id is not None:
        known_student = db.get(Student, existing_student_id)
        if known_student is not None:
            try:
                rec, st, _is_new = att_service.process_checkin(
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
                course_svc = CourseService(db=db)
                student_dashboard = course_svc.build_student_dashboard_payload(
                    student_id=st.id
                )
                resp = make_response(
                    render_template(
                        "student_checkin.html",
                        session=session,
                        checked_in=True,
                        student=st,
                        record=rec,
                        student_dashboard=student_dashboard,
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
                    error_title="Check-In Blocked",
                    error_message=exc.message,
                ), exc.status_code

    # Case 3: First-time student -> issue 90s onboarding grant so typing Name + Email won't fail the 5s QR
    onboarding_grant = qr_service.create_onboarding_grant(
        session_code=code, device_id=device_id, bucket=bucket_val
    )
    return render_template(
        "student_checkin.html",
        session=session,
        checked_in=False,
        onboarding_grant=onboarding_grant,
        bucket=bucket_val,
        sig=sig_param,
        device_id=device_id,
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
    session = att_service.create_session(
        title=title, session_code=code, headcount_tolerance=tolerance
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
    payload = qr_service.build_qr_payload(_resolve_base_url(), code)
    payload["attendance_open"] = session.attendance_open
    return jsonify(payload)


@attendance_bp.post("/api/attendance/checkin")
def api_student_checkin():
    att_service, _qr_service, device_mgr, _exporter = _get_services()
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
        return render_template(
            "student_checkin.html",
            session=session,
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
        course_svc = CourseService(db=db)
        student_dashboard = course_svc.build_student_dashboard_payload(
            student_id=student.id
        )
        resp = make_response(
            render_template(
                "student_checkin.html",
                session=session,
                checked_in=True,
                student=student,
                record=rec,
                student_dashboard=student_dashboard,
            )
        )

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
    use_sample = str(data.get("use_sample", "")).lower() in ("1", "true", "yes")

    upload_id = uuid.uuid4().hex[:10]
    raw_dest = cfg.UPLOAD_DIR / f"session_{session_id}_{upload_id}_raw.jpg"
    ann_dest = cfg.UPLOAD_DIR / f"session_{session_id}_{upload_id}_annotated.jpg"

    if use_sample:
        sample_file = cfg.SAMPLE_DIR / "classroom_ariel.jpg"
        if not sample_file.exists():
            return jsonify({"error": "Bundled sample classroom photo not found."}), 404
        shutil.copy2(sample_file, raw_dest)
    elif "photo" in request.files and request.files["photo"].filename:
        photo_file = request.files["photo"]
        file_bytes = photo_file.read()
        if not validate_image_magic_bytes(file_bytes):
            return jsonify({"error": "Invalid image upload: expected JPEG, PNG, GIF, or WebP file."}), 400
        raw_dest.write_bytes(file_bytes)
    else:
        return jsonify({"error": "No classroom photo file provided."}), 400

    try:
        img = read_image_unicode(raw_dest)
    except Exception as exc:  # noqa: BLE001
        return jsonify({"error": f"Invalid image file: {exc}"}), 400

    boxes = detector.detect(img)
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
    return jsonify(
        {
            "snapshot_id": snap.id,
            "auto_face_count": snap.auto_face_count,
            "verified_face_count": snap.verified_face_count,
            "auto_count": snap.auto_face_count,
            "verified_count": snap.verified_face_count,
            "boxes": boxes_dicts,
            "raw_image_url": raw_url,
            "image_url": raw_url,
            "annotated_image_url": url_for(
                "attendance.get_headcount_image",
                session_id=session.id,
                kind="annotated",
                snap_id=snap.id,
            ),
            "verdict": verdict.to_dict(),
        }
    )


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

    db.commit()
    verdict = att_service.get_audit_summary(session.id)
    return jsonify(
        {
            "snapshot_id": snap.id,
            "auto_face_count": snap.auto_face_count,
            "verified_face_count": snap.verified_face_count,
            "auto_count": snap.auto_face_count,
            "verified_count": snap.verified_face_count,
            "boxes": json.loads(snap.boxes_json),
            "verdict": verdict.to_dict(),
        }
    )


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
    att_service, _qr_service, _device_mgr, _exporter = _get_services()
    db = current_app.extensions["db_session"]
    session = db.get(ClassSession, session_id)
    if session is None:
        return jsonify({"error": "Session not found"}), 404

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
