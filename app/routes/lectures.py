from __future__ import annotations

from pathlib import Path
from typing import Any, Optional

from flask import Blueprint, Response, current_app, jsonify, request
from sqlalchemy.orm import sessionmaker
from werkzeug.utils import secure_filename

from app import socketio
from app.models import ClassSession, LectureArtifact
from app.services.lecture_pipeline import LecturePipelineError, LectureSummarizerService
from app.services.zoom_bridge import (
    ZoomBridgeAdapter,
    ZoomRecordingError,
    build_zoom_url_validation_response,
    normalize_zoom_meeting_id,
    verify_zoom_webhook_signature,
)

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


def _match_session_from_zoom_object(db, zoom_obj: dict[str, Any]) -> Optional[ClassSession]:
    """Resolve a ClassSession from Zoom meeting ID or topic containing `[SESSION_CODE]`."""
    meeting_id = normalize_zoom_meeting_id(zoom_obj.get("id"))
    if meeting_id:
        by_id = (
            db.query(ClassSession)
            .filter_by(zoom_meeting_id=meeting_id)
            .order_by(ClassSession.id.desc())
            .first()
        )
        if by_id is not None:
            return by_id

    topic = str(zoom_obj.get("topic", "")).upper()
    if topic:
        sessions = db.query(ClassSession).order_by(ClassSession.id.desc()).all()
        for sess in sessions:
            if sess.session_code and sess.session_code.upper() in topic:
                return sess
    return None


def _enqueue_zoom_pipeline_job(
    session: ClassSession,
    meeting_id: str,
    bundle: dict[str, Any],
    download_token: Optional[str] = None,
    sync: bool = False,
) -> tuple[dict[str, Any], Optional[str]]:
    """Run or enqueue Zoom Cloud -> 2-Pass Gemini summarization for a session."""
    db = current_app.extensions["db_session"]
    engine = current_app.extensions["db_engine"]
    zoom_bridge: ZoomBridgeAdapter = current_app.extensions["zoom_bridge"]
    llm_client = current_app.extensions["llm_client"]
    cfg = current_app.config["CLASSPULSE_CONFIG"]
    svc = _get_lecture_service()

    meeting_uuid = str(bundle.get("uuid") or "").strip() or None
    if meeting_uuid:
        existing = (
            db.query(LectureArtifact)
            .filter_by(session_id=session.id, zoom_meeting_uuid=meeting_uuid, status="COMPLETED")
            .first()
        )
        if existing is not None:
            return svc.serialize_artifact(existing), None

    room = f"session_{session.session_code}"

    def _emit_status(payload: dict[str, Any]) -> None:
        socketio.emit("lecture:status", payload, to=room)

    if sync:
        artifact = svc.summarize_zoom_recording_bundle(
            session_id=session.id,
            zoom_bridge=zoom_bridge,
            meeting_id=meeting_id,
            recording_bundle=bundle,
            download_token=download_token,
            on_status=_emit_status,
        )
        serialized = svc.serialize_artifact(artifact)
        socketio.emit("lecture:summary_ready", serialized, to=room)
        return serialized, None

    provisional = LectureArtifact(
        session_id=session.id,
        source_type="zoom_cloud",
        media_path=f"zoom://{meeting_id}",
        zoom_meeting_uuid=meeting_uuid,
        status="DOWNLOADING_ZOOM",
    )
    db.add(provisional)
    db.commit()
    provisional_payload = svc.serialize_artifact(provisional)
    _emit_status(provisional_payload)

    session_id_val = session.id
    artifact_id_val = provisional.id
    factory = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)

    def _worker() -> dict[str, Any]:
        with factory() as bg_db:
            bg_svc = LectureSummarizerService(
                db=bg_db, llm_client=llm_client, upload_dir=cfg.UPLOAD_DIR
            )
            artifact = bg_svc.summarize_zoom_recording_bundle(
                session_id=session_id_val,
                zoom_bridge=zoom_bridge,
                meeting_id=meeting_id,
                recording_bundle=bundle,
                download_token=download_token,
                artifact_id=artifact_id_val,
                on_status=_emit_status,
            )
            serialized_bg = bg_svc.serialize_artifact(artifact)
            socketio.emit("lecture:summary_ready", serialized_bg, to=room)
            return serialized_bg

    task_mgr = current_app.extensions["task_manager"]
    job_id = task_mgr.submit(f"zoom-summarize-{session.session_code}", _worker)
    return provisional_payload, job_id


@lectures_bp.get("/api/integrations/status")
def integration_status():
    cfg = current_app.config["CLASSPULSE_CONFIG"]
    llm_client = current_app.extensions.get("llm_client")
    zoom_bridge = current_app.extensions.get("zoom_bridge")
    return jsonify(
        {
            "gemini_configured": bool(llm_client and llm_client.is_configured()),
            "zoom_configured": bool(zoom_bridge and zoom_bridge.is_configured()),
            "webhook_secret_configured": bool(cfg.ZOOM_WEBHOOK_SECRET_TOKEN.strip()),
            "fast_model": cfg.GEMINI_FAST_MODEL,
            "pro_model": cfg.GEMINI_PRO_MODEL,
        }
    )


@lectures_bp.get("/api/sessions/<int:session_id>/lectures")
def list_session_lectures(session_id: int):
    svc = _get_lecture_service()
    return jsonify({"artifacts": svc.list_session_artifacts(session_id)})


@lectures_bp.get("/api/courses/<int:course_id>/lectures")
def list_course_lectures(course_id: int):
    svc = _get_lecture_service()
    return jsonify({"artifacts": svc.list_course_artifacts(course_id)})


@lectures_bp.post("/api/sessions/<int:session_id>/zoom-config")
def save_session_zoom_config(session_id: int):
    db = current_app.extensions["db_session"]
    sess = db.get(ClassSession, session_id)
    if sess is None:
        return jsonify({"error": "Session not found."}), 404
    payload: dict[str, Any] = request.get_json(silent=True) or {}
    if "zoom_meeting_id" in payload:
        clean_id = normalize_zoom_meeting_id(payload.get("zoom_meeting_id"))
        sess.zoom_meeting_id = clean_id or None
    if "zoom_auto_sync" in payload:
        sess.zoom_auto_sync = str(payload.get("zoom_auto_sync")).lower() in (
            "1",
            "true",
            "yes",
        ) or payload.get("zoom_auto_sync") is True
    db.commit()
    return jsonify(
        {
            "session_id": sess.id,
            "session_code": sess.session_code,
            "zoom_meeting_id": sess.zoom_meeting_id or "",
            "zoom_auto_sync": bool(sess.zoom_auto_sync),
        }
    )


@lectures_bp.get("/api/zoom/recordings")
def list_recent_zoom_recordings():
    zoom_bridge: ZoomBridgeAdapter = current_app.extensions["zoom_bridge"]
    cfg = current_app.config["CLASSPULSE_CONFIG"]
    if not zoom_bridge.is_configured():
        return (
            jsonify({"error": "Zoom Server-to-Server OAuth credentials are not configured in .env."}),
            400,
        )
    try:
        meetings = zoom_bridge.list_recent_recordings(user_id=cfg.ZOOM_DEFAULT_USER_ID)
    except ZoomRecordingError as exc:
        return jsonify({"error": exc.message}), exc.status_code
    return jsonify({"meetings": meetings})


@lectures_bp.post("/api/sessions/<int:session_id>/lectures/zoom-fetch")
def fetch_zoom_recording_for_session(session_id: int):
    db = current_app.extensions["db_session"]
    zoom_bridge: ZoomBridgeAdapter = current_app.extensions["zoom_bridge"]
    cfg = current_app.config["CLASSPULSE_CONFIG"]

    sess = db.get(ClassSession, session_id)
    if sess is None:
        return jsonify({"error": "Session not found."}), 404
    if not zoom_bridge.is_configured():
        return (
            jsonify({"error": "Zoom Server-to-Server OAuth credentials are not configured in .env."}),
            400,
        )

    payload: dict[str, Any] = request.get_json(silent=True) or {}
    req_mid = normalize_zoom_meeting_id(payload.get("zoom_meeting_id"))
    if req_mid:
        sess.zoom_meeting_id = req_mid
    if "zoom_auto_sync" in payload:
        sess.zoom_auto_sync = bool(payload.get("zoom_auto_sync"))
    db.commit()

    target_mid = req_mid or normalize_zoom_meeting_id(sess.zoom_meeting_id)
    sync_mode = str(payload.get("sync", "")).lower() in ("1", "true", "yes") or (
        payload.get("sync") is True
    )

    try:
        bundle: Optional[dict[str, Any]] = None
        if target_mid:
            bundle = zoom_bridge.list_meeting_recordings(target_mid)
        else:
            recent = zoom_bridge.list_recent_recordings(user_id=cfg.ZOOM_DEFAULT_USER_ID)
            for candidate in recent:
                if candidate.get("recording_files"):
                    bundle = candidate
                    target_mid = normalize_zoom_meeting_id(candidate.get("id"))
                    if target_mid:
                        sess.zoom_meeting_id = target_mid
                        db.commit()
                    break

        if not bundle or not bundle.get("recording_files"):
            return (
                jsonify(
                    {
                        "error": (
                            f"No completed Zoom Cloud recording found yet"
                            f"{f' for meeting {target_mid}' if target_mid else ''}. "
                            "Make sure Zoom Cloud Recording has finished processing."
                        )
                    }
                ),
                404,
            )

        artifact_payload, job_id = _enqueue_zoom_pipeline_job(
            session=sess,
            meeting_id=target_mid or str(bundle.get("id", "")),
            bundle=bundle,
            sync=sync_mode,
        )
    except ZoomRecordingError as exc:
        return jsonify({"error": exc.message}), exc.status_code
    except LecturePipelineError as exc:
        return jsonify({"error": exc.message}), exc.status_code

    status_code = 201 if job_id is None else 202
    return jsonify({"artifact": artifact_payload, "job_id": job_id}), status_code


@lectures_bp.post("/api/webhooks/zoom")
def zoom_recording_webhook():
    """Optional signed Zoom Webhook receiver (`endpoint.url_validation` & `recording.completed`)."""
    cfg = current_app.config["CLASSPULSE_CONFIG"]
    raw_body = request.get_data()
    payload: dict[str, Any] = request.get_json(silent=True) or {}
    event_type = str(payload.get("event", "")).strip()

    if event_type == "endpoint.url_validation":
        plain_token = str((payload.get("payload") or {}).get("plainToken", ""))
        if not cfg.ZOOM_WEBHOOK_SECRET_TOKEN:
            return jsonify({"error": "ZOOM_WEBHOOK_SECRET_TOKEN not configured."}), 400
        return jsonify(
            build_zoom_url_validation_response(plain_token, cfg.ZOOM_WEBHOOK_SECRET_TOKEN)
        ), 200

    if cfg.ZOOM_WEBHOOK_SECRET_TOKEN:
        if not verify_zoom_webhook_signature(
            request.headers, raw_body, cfg.ZOOM_WEBHOOK_SECRET_TOKEN
        ):
            return jsonify({"error": "Invalid Zoom webhook signature."}), 401

    if event_type != "recording.completed":
        return jsonify({"status": "ignored", "event": event_type}), 200

    inner = payload.get("payload") or {}
    zoom_obj = inner.get("object") or {}
    download_token = payload.get("download_token")
    db = current_app.extensions["db_session"]
    sess = _match_session_from_zoom_object(db, zoom_obj)
    if sess is None:
        return (
            jsonify(
                {
                    "status": "unmatched",
                    "reason": "No ClassSession matched this Zoom meeting ID or topic.",
                }
            ),
            202,
        )

    meeting_id = normalize_zoom_meeting_id(zoom_obj.get("id")) or str(zoom_obj.get("id", ""))
    artifact_payload, job_id = _enqueue_zoom_pipeline_job(
        session=sess,
        meeting_id=meeting_id,
        bundle=zoom_obj,
        download_token=str(download_token) if download_token else None,
        sync=False,
    )
    return jsonify({"status": "queued", "artifact": artifact_payload, "job_id": job_id}), 202


@lectures_bp.get("/api/lectures/<int:artifact_id>/download/summary.md")
def download_lecture_summary_md(artifact_id: int):
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


@lectures_bp.post("/api/sessions/<int:session_id>/lectures/summarize")
def summarize_session_lecture(session_id: int):
    db = current_app.extensions["db_session"]
    cfg = current_app.config["CLASSPULSE_CONFIG"]
    svc = _get_lecture_service()

    session = db.get(ClassSession, session_id)
    if session is None:
        return jsonify({"error": "Session not found."}), 404

    saved_path: Path | None = None
    transcript_text: str | None = None
    use_sample = False

    if "media" in request.files and request.files["media"].filename:
        file_storage = request.files["media"]
        clean_name = secure_filename(file_storage.filename) or "recording.bin"
        dest_dir = Path(cfg.UPLOAD_DIR) / f"session_{session.id}"
        dest_dir.mkdir(parents=True, exist_ok=True)
        saved_path = dest_dir / clean_name
        file_storage.save(str(saved_path))
    else:
        payload: dict[str, Any] = request.get_json(silent=True) or request.form.to_dict()
        transcript_text = payload.get("transcript_text")
        use_sample = str(payload.get("use_sample", "")).lower() in ("1", "true", "yes")

    try:
        artifact = svc.summarize_session_recording(
            session_id=session.id,
            media_path=saved_path,
            transcript_text=transcript_text,
            use_sample=use_sample,
        )
    except LecturePipelineError as exc:
        return jsonify({"error": exc.message}), exc.status_code

    serialized = svc.serialize_artifact(artifact)
    socketio.emit(
        "lecture:summary_ready",
        serialized,
        to=f"session_{session.session_code}",
    )
    return jsonify(serialized), 201
