from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from dotenv import load_dotenv

load_dotenv()

BASE_DIR = Path(__file__).resolve().parent.parent
DATA_DIR = BASE_DIR / "data"


@dataclass
class Config:
    """Centralized environment-driven configuration for ClassPulse."""

    SECRET_KEY: str = os.getenv("SECRET_KEY", "dev-classpulse-hmac-secret-key-2026")
    LECTURER_PASSCODE: str = os.getenv("LECTURER_PASSCODE", "admin123")
    DATABASE_URL: str = os.getenv(
        "DATABASE_URL", f"sqlite:///{(DATA_DIR / 'classpulse.db').as_posix()}"
    )

    # QR & Attendance settings
    QR_STEP_SECONDS: int = int(os.getenv("QR_STEP_SECONDS", "5"))
    QR_GRACE_BUCKETS: int = int(os.getenv("QR_GRACE_BUCKETS", "1"))
    HEADCOUNT_TOLERANCE: int = int(os.getenv("HEADCOUNT_TOLERANCE", "2"))
    LATE_AFTER_MINUTES: int = int(os.getenv("LATE_AFTER_MINUTES", "15"))

    # Tunnel & URL settings
    PUBLIC_BASE_URL: str = os.getenv("PUBLIC_BASE_URL", "").rstrip("/")

    # Media & cache directories
    UPLOAD_DIR: Path = DATA_DIR / "uploads"
    REPORT_DIR: Path = DATA_DIR / "reports"
    SAMPLE_DIR: Path = DATA_DIR / "samples"
    MODELS_CACHE_DIR: Path = Path(
        os.getenv("INSIGHTFACE_HOME", str(BASE_DIR / "models_cache"))
    )

    # Google AI Studio settings (Features 4 & 5)
    GEMINI_API_KEY: str = os.getenv("GEMINI_API_KEY", "")
    GEMINI_FAST_MODEL: str = os.getenv("GEMINI_FAST_MODEL", "gemini-3.8-flash")
    GEMINI_PRO_MODEL: str = os.getenv("GEMINI_PRO_MODEL", "gemini-3.8-flash")
    GEMINI_MIN_INTERVAL_SEC: float = float(os.getenv("GEMINI_MIN_INTERVAL_SEC", "4.5"))

    # Optional Zoom OAuth Bridge (Features 2 & 4)
    ZOOM_ACCOUNT_ID: str = os.getenv("ZOOM_ACCOUNT_ID", "")
    ZOOM_CLIENT_ID: str = os.getenv("ZOOM_CLIENT_ID", "")
    ZOOM_CLIENT_SECRET: str = os.getenv("ZOOM_CLIENT_SECRET", "")
    ZOOM_WEBHOOK_SECRET_TOKEN: str = os.getenv("ZOOM_WEBHOOK_SECRET_TOKEN", "")
    ZOOM_DEFAULT_USER_ID: str = os.getenv("ZOOM_DEFAULT_USER_ID", "me")
    ZOOM_TEAM_CHANNEL_ID: str = os.getenv("ZOOM_TEAM_CHANNEL_ID", "")
    ZOOM_POLL_INTERVAL_SEC: int = int(os.getenv("ZOOM_POLL_INTERVAL_SEC", "60"))

    # Background task executor threads
    MAX_TASK_WORKERS: int = int(os.getenv("MAX_TASK_WORKERS", "2"))

    def ensure_directories(self) -> None:
        """Ensure all required runtime data directories exist on disk."""
        for directory in (
            DATA_DIR,
            self.UPLOAD_DIR,
            self.REPORT_DIR,
            self.SAMPLE_DIR,
            self.MODELS_CACHE_DIR,
        ):
            directory.mkdir(parents=True, exist_ok=True)
