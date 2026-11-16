from __future__ import annotations

import hmac
import secrets
from typing import Optional

from flask import Flask, Response, jsonify, request
from flask_socketio import SocketIO
from sqlalchemy import text
from werkzeug.middleware.proxy_fix import ProxyFix

from app.config import Config
from app.models import LectureArtifact, init_db_engine
from app.schemas import build_openapi_spec
from app.services.face_counter import FaceDetector, TiledSCRFDDetector
from app.services.llm_client import GeminiStudioClient
from app.services.qr_service import DeviceBindingManager, RollingQRService
from app.services.rate_limiter import SlidingWindowRateLimiter
from app.services.report_exporter import SpreadsheetExporter
from app.services.task_runner import AsyncTaskManager

socketio = SocketIO()


def create_app(
    config: Optional[Config] = None,
    face_detector: Optional[FaceDetector] = None,
    llm_client: Optional[GeminiStudioClient] = None,
) -> Flask:
    """Application factory initializing Flask, ProxyFix, SQLite WAL, SocketIO, and Services."""
    cfg = config or Config()
    cfg.validate_production_safety()
    cfg.ensure_directories()

    flask_app = Flask(__name__)
    flask_app.config["SECRET_KEY"] = cfg.SECRET_KEY
    flask_app.config["CLASSPULSE_CONFIG"] = cfg
    flask_app.config["MAX_CONTENT_LENGTH"] = 150 * 1024 * 1024
    flask_app.extensions["rate_limiter"] = SlidingWindowRateLimiter()

    # Trust Cloudflare Tunnel X-Forwarded-* headers for HTTPS/WSS URL generation
    flask_app.wsgi_app = ProxyFix(flask_app.wsgi_app, x_for=1, x_proto=1, x_host=1)  # type: ignore[method-assign]

    # Initialize SQLite WAL engine & thread-safe scoped session
    engine, db_session = init_db_engine(cfg.DATABASE_URL)
    flask_app.extensions["db_engine"] = engine
    flask_app.extensions["db_session"] = db_session

    # Recover any orphaned in-flight AI jobs interrupted by a server restart
    stale_jobs = (
        db_session.query(LectureArtifact)
        .filter(LectureArtifact.status.in_(("PENDING", "TRANSCRIBING", "PROCESSING")))
        .all()
    )
    if stale_jobs:
        for art in stale_jobs:
            art.status = "FAILED"
            art.error_message = "Interrupted by server restart — please re-run summarization."
        db_session.commit()
    db_session.remove()

    # Initialize core Feature 1 services
    flask_app.extensions["qr_service"] = RollingQRService(
        secret_key=cfg.SECRET_KEY,
        step_seconds=cfg.QR_STEP_SECONDS,
        grace_buckets=cfg.QR_GRACE_BUCKETS,
    )
    flask_app.extensions["device_mgr"] = DeviceBindingManager(secret_key=cfg.SECRET_KEY)
    flask_app.extensions["face_detector"] = face_detector or TiledSCRFDDetector()
    flask_app.extensions["report_exporter"] = SpreadsheetExporter()

    # Initialize Gemini AI Studio Client (Features 4 & 5)
    flask_app.extensions["llm_client"] = llm_client or GeminiStudioClient(
        api_key=cfg.GEMINI_API_KEY,
        fast_model=cfg.GEMINI_FAST_MODEL,
        pro_model=cfg.GEMINI_PRO_MODEL,
        min_interval_sec=cfg.GEMINI_MIN_INTERVAL_SEC,
    )

    # Initialize bounded background task executor
    task_manager = AsyncTaskManager(max_workers=cfg.MAX_TASK_WORKERS)
    flask_app.extensions["task_manager"] = task_manager

    # Initialize Flask-SocketIO in standard threading mode (compatible with simple-websocket + gthread)
    socketio.init_app(flask_app, async_mode="threading", cors_allowed_origins="*")

    # Register blueprints and Socket.IO event handlers
    from app.routes.attendance import attendance_bp
    from app.routes.chat import chat_bp
    from app.routes.courses import courses_bp
    from app.routes.homework import homework_bp
    from app.routes.lectures import lectures_bp
    from app.routes.polls import polls_bp
    from app.sockets.chat_events import register_chat_events
    from app.sockets.poll_events import register_poll_events
    from app.sockets.session_events import register_session_events

    register_session_events(socketio)
    register_poll_events(socketio)
    register_chat_events(socketio)
    flask_app.register_blueprint(courses_bp)
    flask_app.register_blueprint(attendance_bp)
    flask_app.register_blueprint(polls_bp)
    flask_app.register_blueprint(chat_bp)
    flask_app.register_blueprint(lectures_bp)
    flask_app.register_blueprint(homework_bp)

    @flask_app.before_request
    def enforce_csrf_when_cookie_present():
        if not cfg.ENABLE_CSRF or request.method in ("GET", "HEAD", "OPTIONS", "TRACE"):
            return None
        if flask_app.config.get("TESTING") and not flask_app.config.get("TEST_ENFORCE_CSRF"):
            return None
        csrf_cookie = request.cookies.get("cp_csrf")
        if not csrf_cookie:
            return None
        supplied = request.headers.get("X-CSRF-Token") or request.form.get("csrf_token") or ""
        if not supplied or not hmac.compare_digest(csrf_cookie, supplied):
            return jsonify({"error": "CSRF verification failed. Reload the page and retry."}), 403
        return None

    @flask_app.after_request
    def attach_csrf_cookie(response: Response) -> Response:
        if request.method == "GET" and not request.cookies.get("cp_csrf"):
            token = secrets.token_urlsafe(24)
            response.set_cookie(
                "cp_csrf",
                token,
                httponly=False,
                samesite="Lax",
                secure=request.is_secure,
            )
        return response

    @flask_app.teardown_appcontext
    def cleanup_db_session(_exc: Optional[BaseException] = None) -> None:
        db_session.remove()

    @flask_app.get("/health")
    @flask_app.get("/healthz")
    def healthcheck():
        return jsonify(
            {
                "status": "ok",
                "service": "ClassPulse",
                "scheme": request.scheme,
                "host": request.host,
            }
        )

    @flask_app.get("/readyz")
    def readiness_check():
        try:
            db_session.execute(text("SELECT 1"))
            cfg.UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
            return jsonify({"status": "ready", "database": "ok", "storage": "ok"}), 200
        except Exception as exc:  # noqa: BLE001
            return jsonify({"status": "not_ready", "error": str(exc)}), 503

    @flask_app.get("/api/openapi.json")
    def openapi_spec():
        return jsonify(build_openapi_spec())

    @flask_app.get("/api/docs")
    def api_docs_page():
        return (
            "<!doctype html><html><head><title>ClassPulse API Spec</title></head>"
            "<body style='font-family:system-ui;background:#0b1120;color:#f8fafc;padding:24px;'>"
            "<h1>ClassPulse API v2</h1>"
            "<p>OpenAPI 3.0 JSON Specification: <a href='/api/openapi.json' style='color:#38bdf8;'>/api/openapi.json</a></p>"
            "<ul><li><code>/healthz</code> &amp; <code>/readyz</code></li>"
            "<li><code>POST /api/auth/lecturer/register</code> &amp; <code>/api/auth/lecturer/login</code></li>"
            "<li><code>POST /api/auth/lecturer/logout-all</code> &amp; <code>GET /api/auth/lecturer/audit-logs</code></li>"
            "<li><code>GET/POST /api/courses</code> &amp; <code>/api/courses/&lt;id&gt;/staff</code></li>"
            "<li><code>POST /api/student/login</code> (Student ID + Email)</li>"
            "<li><code>DELETE /api/lectures/&lt;artifact_id&gt;</code></li></ul>"
            "</body></html>",
            200,
        )

    @flask_app.get("/api/jobs/<job_id>")
    def get_job_status(job_id: str):
        status = task_manager.get_status(job_id)
        if status is None:
            return jsonify({"error": "Job not found"}), 404
        return jsonify(status)

    return flask_app
