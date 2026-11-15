from __future__ import annotations

import atexit
import os
import sys
from typing import Optional

from flask import Flask, jsonify, request
from flask_socketio import SocketIO
from werkzeug.middleware.proxy_fix import ProxyFix

from app.config import Config
from app.models import init_db_engine
from app.services.face_counter import FaceDetector, TiledSCRFDDetector
from app.services.llm_client import GeminiStudioClient
from app.services.qr_service import DeviceBindingManager, RollingQRService
from app.services.report_exporter import SpreadsheetExporter
from app.services.task_runner import AsyncTaskManager
from app.services.zoom_bridge import (
    NoOpZoomBridge,
    ZoomBridgeAdapter,
    ZoomOAuthRestBridge,
)

socketio = SocketIO()


def create_app(
    config: Optional[Config] = None,
    face_detector: Optional[FaceDetector] = None,
    zoom_bridge: Optional[ZoomBridgeAdapter] = None,
    llm_client: Optional[GeminiStudioClient] = None,
) -> Flask:
    """Application factory initializing Flask, ProxyFix, SQLite WAL, SocketIO, and Services."""
    cfg = config or Config()
    cfg.ensure_directories()

    flask_app = Flask(__name__)
    flask_app.config["SECRET_KEY"] = cfg.SECRET_KEY
    flask_app.config["CLASSPULSE_CONFIG"] = cfg

    # Trust Cloudflare Tunnel X-Forwarded-* headers for HTTPS/WSS URL generation
    flask_app.wsgi_app = ProxyFix(flask_app.wsgi_app, x_for=1, x_proto=1, x_host=1)  # type: ignore[method-assign]

    # Initialize SQLite WAL engine & thread-safe scoped session
    engine, db_session = init_db_engine(cfg.DATABASE_URL)
    flask_app.extensions["db_engine"] = engine
    flask_app.extensions["db_session"] = db_session

    # Initialize core Feature 1 services
    flask_app.extensions["qr_service"] = RollingQRService(
        secret_key=cfg.SECRET_KEY,
        step_seconds=cfg.QR_STEP_SECONDS,
        grace_buckets=cfg.QR_GRACE_BUCKETS,
    )
    flask_app.extensions["device_mgr"] = DeviceBindingManager(secret_key=cfg.SECRET_KEY)
    flask_app.extensions["face_detector"] = face_detector or TiledSCRFDDetector()
    flask_app.extensions["report_exporter"] = SpreadsheetExporter()

    # Initialize Zoom Bridge Adapter (Feature 2 & Feature 4)
    if zoom_bridge is not None:
        flask_app.extensions["zoom_bridge"] = zoom_bridge
    elif cfg.ZOOM_ACCOUNT_ID and cfg.ZOOM_CLIENT_ID and cfg.ZOOM_CLIENT_SECRET:
        flask_app.extensions["zoom_bridge"] = ZoomOAuthRestBridge(
            account_id=cfg.ZOOM_ACCOUNT_ID,
            client_id=cfg.ZOOM_CLIENT_ID,
            client_secret=cfg.ZOOM_CLIENT_SECRET,
            team_channel_id=cfg.ZOOM_TEAM_CHANNEL_ID,
        )
    else:
        flask_app.extensions["zoom_bridge"] = NoOpZoomBridge()

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

    # Initialize optional background Zoom Cloud Auto-Poller (uses 3 OAuth keys, no webhook needed)
    from app.services.lecture_pipeline import ZoomCloudAutoPoller

    zoom_poller = ZoomCloudAutoPoller(
        engine=engine,
        zoom_bridge=flask_app.extensions["zoom_bridge"],
        llm_client=flask_app.extensions["llm_client"],
        upload_dir=cfg.UPLOAD_DIR,
        socketio_ext=socketio,
        interval_sec=cfg.ZOOM_POLL_INTERVAL_SEC,
    )
    flask_app.extensions["zoom_poller"] = zoom_poller
    if (
        flask_app.extensions["zoom_bridge"].is_configured()
        and cfg.ZOOM_POLL_INTERVAL_SEC > 0
        and "pytest" not in sys.modules
        and os.getenv("CLASSPULSE_ENABLE_ZOOM_POLLER", "1") == "1"
    ):
        zoom_poller.start()
        atexit.register(zoom_poller.stop)

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

    @flask_app.teardown_appcontext
    def cleanup_db_session(_exc: Optional[BaseException] = None) -> None:
        db_session.remove()

    @flask_app.get("/health")
    def healthcheck():
        return jsonify(
            {
                "status": "ok",
                "service": "ClassPulse",
                "scheme": request.scheme,
                "host": request.host,
            }
        )

    @flask_app.get("/api/jobs/<job_id>")
    def get_job_status(job_id: str):
        status = task_manager.get_status(job_id)
        if status is None:
            return jsonify({"error": "Job not found"}), 404
        return jsonify(status)

    return flask_app
