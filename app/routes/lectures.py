from __future__ import annotations

import re
import uuid
from pathlib import Path
from typing import Any

from flask import Blueprint, Response, current_app, jsonify, request
from sqlalchemy.orm import sessionmaker
from werkzeug.utils import secure_filename

from app import socketio
from app.models import ClassSession, Course, CourseEnrollment, LectureArtifact
from app.routes.guards import (
    require_course_access,
    require_session_access,
    verify_course_read_access,
    verify_session_read_access,
)
from app.services.lecture_pipeline import LecturePipelineError, LectureSummarizerService

lectures_bp = Blueprint("lectures", __name__)


def _get_lecture_service() -> LectureSummarizerService:
    db = current_app.extensions["db_session"]
    llm_client = current_app.extensions.get("llm_client")
    cfg = current_app.config["CLASSPULSE_CONFIG"]
    return LectureSummarizerService(
        db=db,
        llm_client=llm_client,
        upload_dir=cfg.UPLOAD_DIR,
    )


@lectures_bp.get("/api/integrations/status")
def integration_status():
    cfg = current_app.config["CLASSPULSE_CONFIG"]
    llm_client = current_app.extensions.get("llm_client")
    return jsonify(
        {
            "gemini_configured": bool(llm_client and llm_client.is_configured()),
            "fast_model": cfg.GEMINI_FAST_MODEL,
            "pro_model": cfg.GEMINI_PRO_MODEL,
        }
    )


@lectures_bp.get("/api/sessions/<int:session_id>/lectures")
def list_session_lectures(session_id: int):
    _sess, lec, stu, err = verify_session_read_access(session_id, allow_open_attendance=True)
    if err is not None:
        return err
    svc = _get_lecture_service()
    is_student_view = (stu is not None and lec is None) or str(
        request.args.get("published_only", "")
    ).lower() in ("1", "true", "yes")
    return jsonify({"artifacts": svc.list_session_artifacts(session_id, published_only=is_student_view)})


@lectures_bp.get("/api/courses/<int:course_id>/lectures")
def list_course_lectures(course_id: int):
    _course, lec, stu, _all_subs, err = verify_course_read_access(course_id)
    if err is not None:
        return err
    svc = _get_lecture_service()
    is_student_view = (stu is not None and lec is None) or str(
        request.args.get("published_only", "")
    ).lower() in ("1", "true", "yes")
    return jsonify({"artifacts": svc.list_course_artifacts(course_id, published_only=is_student_view)})


@lectures_bp.get("/api/lectures/<int:artifact_id>/download/summary.md")
def download_lecture_summary_md(artifact_id: int):
    db = current_app.extensions["db_session"]
    artifact = db.get(LectureArtifact, artifact_id)
    if artifact is None:
        return jsonify({"error": "Lecture artifact not found."}), 404
    _sess, lec, stu, err = verify_session_read_access(
        artifact.session_id, allow_open_attendance=True
    )
    if err is not None:
        return err
    is_student_view = (stu is not None and lec is None) or str(
        request.args.get("published_only", "")
    ).lower() in ("1", "true", "yes")
    if is_student_view and not artifact.is_published:
        return jsonify({"error": "This lecture summary has not been published to students yet."}), 403
    svc = _get_lecture_service()
    try:
        filename, content = svc.build_summary_download(artifact_id)
    except LecturePipelineError as exc:
        return jsonify({"error": exc.message}), exc.status_code
    return Response(
        content,
        mimetype="text/markdown; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@lectures_bp.get("/api/lectures/<int:artifact_id>/download/transcript.txt")
def download_lecture_transcript_txt(artifact_id: int):
    db = current_app.extensions["db_session"]
    artifact = db.get(LectureArtifact, artifact_id)
    if artifact is None:
        return jsonify({"error": "Lecture artifact not found."}), 404
    _sess, lec, stu, err = verify_session_read_access(
        artifact.session_id, allow_open_attendance=True
    )
    if err is not None:
        return err
    is_student_view = (stu is not None and lec is None) or str(
        request.args.get("published_only", "")
    ).lower() in ("1", "true", "yes")
    if is_student_view and not artifact.is_published:
        return jsonify({"error": "This lecture summary has not been published to students yet."}), 403
    svc = _get_lecture_service()
    try:
        filename, content = svc.build_transcript_download(artifact_id)
    except LecturePipelineError as exc:
        return jsonify({"error": exc.message}), exc.status_code
    return Response(
        content,
        mimetype="text/plain; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@lectures_bp.delete("/api/lectures/<int:artifact_id>")
def delete_lecture_artifact(artifact_id: int):
    db = current_app.extensions["db_session"]
    artifact = db.get(LectureArtifact, artifact_id)
    if artifact is None:
        return jsonify({"error": "Lecture artifact not found."}), 404
    session, _lec, err = require_session_access(artifact.session_id)
    if err is not None:
        return err

    session_id = artifact.session_id
    session_code = session.session_code if session else ""
    course_id = session.course_id if session else None

    if artifact.media_path and not artifact.media_path.startswith(("sample://", "inline://")):
        media_file = Path(artifact.media_path)
        if media_file.exists() and media_file.is_file():
            try:
                media_file.unlink()
            except OSError:
                pass
    db.delete(artifact)
    db.commit()

    del_payload = {
        "id": artifact_id,
        "artifact_id": artifact_id,
        "session_id": session_id,
        "course_id": course_id,
    }
    if session_code:
        socketio.emit("lecture:deleted", del_payload, to=f"session_{session_code}")
    if course_id is not None:
        socketio.emit("lecture:deleted", del_payload, to=f"course_notify_{course_id}")
    return jsonify({"status": "deleted", "artifact_id": artifact_id})


@lectures_bp.post("/api/sessions/<int:session_id>/lectures/summarize")
def summarize_session_lecture(session_id: int):
    session, _lec, err = require_session_access(session_id)
    if err is not None:
        return err
    assert session is not None

    db = current_app.extensions["db_session"]
    engine = current_app.extensions["db_engine"]
    llm_client = current_app.extensions.get("llm_client")
    cfg = current_app.config["CLASSPULSE_CONFIG"]
    svc = _get_lecture_service()

    saved_path: Path | None = None
    transcript_text: str | None = None
    use_sample = False
    async_mode = str(request.args.get("async", "")).lower() in ("1", "true", "yes")

    if "media" in request.files and request.files["media"].filename:
        file_storage = request.files["media"]
        orig_path = Path(file_storage.filename)
        raw_suffix = orig_path.suffix.lower()
        safe_ext = raw_suffix if re.fullmatch(r"\.[a-z0-9]{1,10}", raw_suffix) else ".bin"
        safe_stem = secure_filename(orig_path.stem) or f"recording_{uuid.uuid4().hex[:8]}"
        clean_name = f"{safe_stem}{safe_ext}"
        dest_dir = Path(cfg.UPLOAD_DIR) / f"session_{session.id}"
        dest_dir.mkdir(parents=True, exist_ok=True)
        saved_path = dest_dir / clean_name
        file_storage.save(str(saved_path))
        if not saved_path.exists() or saved_path.stat().st_size == 0:
            saved_path.unlink(missing_ok=True)
            return jsonify({"error": "Selected summary or recording file is empty."}), 400
        if not async_mode:
            async_mode = str(request.form.get("async", "")).lower() in ("1", "true", "yes")
    else:
        payload: dict[str, Any] = request.get_json(silent=True) or request.form.to_dict()
        transcript_text = payload.get("transcript_text")
        use_sample = str(payload.get("use_sample", "")).lower() in ("1", "true", "yes")
        if not async_mode:
            async_mode = str(payload.get("async", "")).lower() in ("1", "true", "yes")

    if not use_sample and not (transcript_text and transcript_text.strip()) and saved_path is None:
        return (
            jsonify({"error": "Please select a summary or recording file (or enter notes) before uploading."}),
            400,
        )

    room = f"session_{session.session_code}"
    course_room = f"course_notify_{session.course_id}" if session.course_id is not None else None

    def _emit_status(payload_dict: dict[str, Any]) -> None:
        socketio.emit("lecture:status", payload_dict, to=room)

    def _emit_draft_ready(payload_dict: dict[str, Any]) -> None:
        # Notify the lecturer session workspace that the draft completed, without auto-publishing to students
        socketio.emit("lecture:status", payload_dict, to=room)

    if async_mode and saved_path is not None:
        provisional = LectureArtifact(
            session_id=session.id,
            source_type="upload",
            media_path=str(saved_path),
            status="TRANSCRIBING",
            is_published=False,
        )
        db.add(provisional)
        db.commit()
        provisional_payload = svc.serialize_artifact(provisional)
        _emit_status(provisional_payload)

        factory = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)
        session_id_val = session.id
        artifact_id_val = provisional.id

        def _worker() -> dict[str, Any]:
            with factory() as bg_db:
                bg_svc = LectureSummarizerService(
                    db=bg_db, llm_client=llm_client, upload_dir=cfg.UPLOAD_DIR
                )
                artifact = bg_svc.summarize_session_recording(
                    session_id=session_id_val,
                    media_path=saved_path,
                    artifact_id=artifact_id_val,
                    on_status=_emit_status,
                )
                serialized_bg = bg_svc.serialize_artifact(artifact)
                _emit_draft_ready(serialized_bg)
                return serialized_bg

        job_id = current_app.extensions["task_manager"].submit(
            f"lecture-summarize-{session.session_code}", _worker
        )
        return jsonify({"artifact": provisional_payload, "job_id": job_id}), 202

    try:
        artifact = svc.summarize_session_recording(
            session_id=session.id,
            media_path=saved_path,
            transcript_text=transcript_text,
            use_sample=use_sample,
            on_status=_emit_status,
        )
    except LecturePipelineError as exc:
        return jsonify({"error": exc.message}), exc.status_code

    serialized = svc.serialize_artifact(artifact)
    _emit_draft_ready(serialized)
    return jsonify(serialized), 201


@lectures_bp.post("/api/lectures/<int:artifact_id>/publish")
def publish_lecture_artifact(artifact_id: int):
    """Explicitly publish or unpublish a completed lecture summary for enrolled students."""
    db = current_app.extensions["db_session"]
    artifact = db.get(LectureArtifact, artifact_id)
    if artifact is None:
        return jsonify({"error": "Lecture artifact not found."}), 404
    session, _lec, err = require_session_access(artifact.session_id)
    if err is not None:
        return err
    assert session is not None

    payload = request.get_json(silent=True) or {}
    raw_pub = payload.get("publish", True)
    publish_flag = (
        raw_pub
        if isinstance(raw_pub, bool)
        else str(raw_pub).strip().lower() not in ("false", "0", "no", "")
    )
    svc = _get_lecture_service()
    try:
        updated = svc.publish_artifact(artifact_id, publish=publish_flag)
    except LecturePipelineError as exc:
        return jsonify({"error": exc.message}), exc.status_code

    serialized = svc.serialize_artifact(updated)
    room = f"session_{session.session_code}"
    course_room = f"course_notify_{session.course_id}" if session.course_id is not None else None
    if updated.is_published:
        socketio.emit("lecture:summary_ready", serialized, to=room)
        if course_room is not None:
            socketio.emit("lecture:summary_ready", serialized, to=course_room)
    else:
        unpub_payload = {
            "id": updated.id,
            "session_id": updated.session_id,
            "course_id": session.course_id,
        }
        socketio.emit("lecture:unpublished", unpub_payload, to=room)
        if course_room is not None:
            socketio.emit("lecture:unpublished", unpub_payload, to=course_room)
    return jsonify(serialized), 200
