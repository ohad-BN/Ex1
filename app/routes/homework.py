from __future__ import annotations

import re
from pathlib import Path
from typing import Any, Optional

from flask import Blueprint, current_app, jsonify, request
from sqlalchemy.orm import sessionmaker
from werkzeug.utils import secure_filename

from app import socketio
from app.models import Assignment, ClassStatsAggregate, Student
from app.routes.guards import require_course_access
from app.services.homework_pipeline import (
    CohortAggregator,
    HomeworkPipelineError,
    HomeworkReviewService,
)
from app.services.qr_service import DeviceBindingManager

homework_bp = Blueprint("homework", __name__)


def _get_hw_service() -> HomeworkReviewService:
    db = current_app.extensions["db_session"]
    llm_client = current_app.extensions.get("llm_client")
    return HomeworkReviewService(db=db, llm_client=llm_client)


def _resolve_student_id(payload: dict[str, Any]) -> Optional[int]:
    device_mgr: DeviceBindingManager = current_app.extensions["device_mgr"]
    cookie_raw = request.cookies.get(DeviceBindingManager.COOKIE_NAME)
    parsed = device_mgr.parse_device_cookie(cookie_raw)
    if parsed is not None:
        return parsed[1]
    if payload.get("student_id") is not None and str(payload.get("student_id")).strip() != "":
        try:
            return int(payload["student_id"])
        except (TypeError, ValueError):
            return None
    return request.args.get("student_id", type=int)


def _emit_to_course_rooms(
    _db: Any, course_id: Optional[int], event: str, payload: dict[str, Any]
) -> None:
    """Broadcast homework updates to the course notification room (joined on connect & join_session)."""
    if course_id is None:
        return
    socketio.emit(event, payload, to=f"course_notify_{course_id}")


@homework_bp.get("/api/courses/<int:course_id>/assignments")
def list_course_assignments(course_id: int):
    svc = _get_hw_service()
    student_id = _resolve_student_id({})
    include_subs = str(request.args.get("include_submissions", "")).lower() in ("1", "true", "yes")
    return jsonify(
        {
            "assignments": svc.list_course_assignments(
                course_id, student_id=student_id, include_submissions=include_subs
            )
        }
    )


@homework_bp.post("/api/courses/<int:course_id>/assignments")
def create_course_assignment(course_id: int):
    _course, _lec, err = require_course_access(course_id)
    if err is not None:
        return err

    svc = _get_hw_service()
    payload: dict[str, Any] = request.get_json(silent=True) or request.form.to_dict()
    title = str(payload.get("title", "")).strip()
    rubric_text = str(payload.get("rubric_text", "")).strip()
    raw_topics = payload.get("topics")
    if isinstance(raw_topics, list):
        topics = [str(t).strip() for t in raw_topics if str(t).strip()]
    else:
        topics = [t.strip() for t in str(raw_topics or "").split(",") if t.strip()]

    try:
        assignment = svc.create_assignment(
            course_id=course_id,
            title=title,
            rubric_text=rubric_text,
            topics=topics,
        )
    except HomeworkPipelineError as exc:
        return jsonify({"error": exc.message}), exc.status_code

    _emit_to_course_rooms(
        svc.db,
        course_id,
        "assignment:created",
        svc.serialize_assignment(assignment, include_submissions=False),
    )
    return jsonify(svc.serialize_assignment(assignment, include_submissions=True)), 201


@homework_bp.post("/api/courses/<int:course_id>/assignments/seed-sample")
def seed_sample_assignment(course_id: int):
    _course, _lec, err = require_course_access(course_id)
    if err is not None:
        return err

    svc = _get_hw_service()
    existing_ids = {
        row[0] for row in svc.db.query(Assignment.id).filter_by(course_id=course_id).all()
    }
    try:
        assignment = svc.seed_sample_course_assignment(course_id)
    except HomeworkPipelineError as exc:
        return jsonify({"error": exc.message}), exc.status_code
    if assignment.id not in existing_ids:
        _emit_to_course_rooms(
            svc.db,
            course_id,
            "assignment:created",
            svc.serialize_assignment(assignment, include_submissions=False),
        )
    return jsonify(svc.serialize_assignment(assignment, include_submissions=True)), 201


@homework_bp.get("/api/assignments/<int:assignment_id>")
def get_assignment_detail(assignment_id: int):
    db = current_app.extensions["db_session"]
    svc = _get_hw_service()
    assignment = db.get(Assignment, assignment_id)
    if assignment is None:
        return jsonify({"error": "Assignment not found."}), 404
    student_id = _resolve_student_id({})
    return jsonify(
        svc.serialize_assignment(
            assignment, student_id=student_id, include_submissions=True
        )
    )


@homework_bp.post("/api/assignments/<int:assignment_id>/submissions")
def submit_assignment_homework(assignment_id: int):
    db = current_app.extensions["db_session"]
    cfg = current_app.config["CLASSPULSE_CONFIG"]
    svc = _get_hw_service()

    payload: dict[str, Any] = request.get_json(silent=True) or request.form.to_dict()
    student_id = _resolve_student_id(payload)

    if student_id is None:
        email = str(payload.get("email", "")).strip().lower()
        if email:
            st = db.query(Student).filter_by(email=email).one_or_none()
            if st is not None:
                student_id = st.id

    if student_id is None:
        return jsonify({"error": "Student sign-in required before submitting homework."}), 401

    saved_file: Path | None = None
    if "file" in request.files and request.files["file"].filename:
        fs = request.files["file"]
        orig_name = Path(fs.filename)
        raw_suffix = orig_name.suffix.lower()
        safe_ext = raw_suffix if re.fullmatch(r"\.[a-z0-9]{1,10}", raw_suffix) else ".txt"
        safe_stem = secure_filename(orig_name.stem) or "submission"
        clean_name = f"{safe_stem}{safe_ext}"
        dest_dir = Path(cfg.UPLOAD_DIR) / f"homework_{assignment_id}"
        dest_dir.mkdir(parents=True, exist_ok=True)
        saved_file = dest_dir / f"student_{student_id}_{clean_name}"
        fs.save(str(saved_file))

    content_text = payload.get("content_text") or payload.get("code_text")
    github_url = payload.get("github_url")
    gen_review = str(payload.get("generate_review", "true")).lower() in ("1", "true", "yes")
    async_mode = str(request.args.get("async") or payload.get("async") or "").lower() in (
        "1",
        "true",
        "yes",
    )

    if async_mode and "task_manager" in current_app.extensions:
        assignment = db.get(Assignment, assignment_id)
        if assignment is None:
            return jsonify({"error": "Assignment not found."}), 404
        if db.get(Student, student_id) is None:
            return jsonify({"error": "Student not found."}), 404
        try:
            svc.extractor.extract(
                content_text=content_text,
                file_path=saved_file,
                github_url=github_url,
            )
        except HomeworkPipelineError as exc:
            return jsonify({"error": exc.message}), exc.status_code

        engine = current_app.extensions["db_engine"]
        llm_client = current_app.extensions.get("llm_client")
        factory = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)
        course_id = assignment.course_id

        def _worker() -> dict[str, Any]:
            with factory() as bg_db:
                bg_svc = HomeworkReviewService(db=bg_db, llm_client=llm_client)
                try:
                    sub = bg_svc.submit_homework(
                        assignment_id=assignment_id,
                        student_id=student_id,
                        content_text=content_text,
                        file_path=saved_file,
                        github_url=github_url,
                        generate_review=gen_review,
                    )
                    bg_stats = bg_db.get(ClassStatsAggregate, assignment_id)
                    serialized_sub = bg_svc.serialize_submission(sub, stats=bg_stats)
                    _emit_to_course_rooms(
                        bg_db,
                        course_id,
                        "assignment:review_ready",
                        {
                            "assignment_id": assignment_id,
                            "course_id": course_id,
                            "student_id": student_id,
                            "submission": serialized_sub,
                        },
                    )
                    if bg_stats is not None:
                        _emit_to_course_rooms(
                            bg_db,
                            course_id,
                            "assignment:cohort_updated",
                            {
                                "assignment_id": assignment_id,
                                "course_id": course_id,
                                "cohort_stats": CohortAggregator.build_anonymized_cohort_snapshot(
                                    bg_stats
                                ),
                            },
                        )
                    return serialized_sub
                except Exception as exc:  # noqa: BLE001
                    _emit_to_course_rooms(
                        bg_db,
                        course_id,
                        "assignment:review_error",
                        {
                            "assignment_id": assignment_id,
                            "course_id": course_id,
                            "student_id": student_id,
                            "error": str(exc),
                        },
                    )
                    raise

        job_id = current_app.extensions["task_manager"].submit(
            f"homework-submit-{assignment_id}-student-{student_id}", _worker
        )
        return (
            jsonify(
                {
                    "status": "PENDING",
                    "assignment_id": assignment_id,
                    "student_id": student_id,
                    "job_id": job_id,
                }
            ),
            202,
        )

    try:
        sub = svc.submit_homework(
            assignment_id=assignment_id,
            student_id=student_id,
            content_text=content_text,
            file_path=saved_file,
            github_url=github_url,
            generate_review=gen_review,
        )
    except HomeworkPipelineError as exc:
        return jsonify({"error": exc.message}), exc.status_code

    stats = db.get(ClassStatsAggregate, assignment_id)
    assignment = db.get(Assignment, assignment_id)
    if stats is not None and assignment is not None:
        _emit_to_course_rooms(
            db,
            assignment.course_id,
            "assignment:cohort_updated",
            {
                "assignment_id": assignment_id,
                "course_id": assignment.course_id,
                "cohort_stats": CohortAggregator.build_anonymized_cohort_snapshot(stats),
            },
        )
    return jsonify(svc.serialize_submission(sub, stats=stats)), 201


@homework_bp.post("/api/submissions/<int:submission_id>/deep-review")
def generate_submission_deep_review(submission_id: int):
    db = current_app.extensions["db_session"]
    svc = _get_hw_service()
    try:
        review_md = svc.generate_deep_review(submission_id)
    except HomeworkPipelineError as exc:
        return jsonify({"error": exc.message}), exc.status_code

    from app.models import HomeworkSubmission

    sub = db.get(HomeworkSubmission, submission_id)
    stats = db.get(ClassStatsAggregate, sub.assignment_id) if sub else None
    serialized = svc.serialize_submission(sub, stats=stats) if sub else {}
    serialized["deep_review_md"] = review_md
    return jsonify(serialized), 200
