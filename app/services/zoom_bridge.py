from __future__ import annotations

from abc import ABC, abstractmethod
from datetime import datetime, timedelta, timezone
import hashlib
import hmac
from pathlib import Path
import re
import time
from typing import Any, Mapping, Optional
from urllib.parse import quote

import requests

from app.models import ChatMessage

ZOOM_MEETING_URL_RE = re.compile(r"/j/(\d+)")


class ZoomRecordingError(Exception):
    """Descriptive error raised during explicit Zoom Cloud Recording sync."""

    def __init__(self, message: str, status_code: int = 400) -> None:
        super().__init__(message)
        self.message = message
        self.status_code = status_code


def normalize_zoom_meeting_id(raw: str | int | None) -> str:
    """Extract digits-only Zoom Meeting ID from a join URL, spaced ID, or raw string."""
    text = str(raw or "").strip()
    if not text:
        return ""
    m = ZOOM_MEETING_URL_RE.search(text)
    if m:
        return m.group(1)
    compact = re.sub(r"[\s\-]+", "", text)
    if compact.isdigit():
        return compact
    return text


def _classify_recording_file(entry: Mapping[str, Any]) -> Optional[tuple[int, str]]:
    """Map a Zoom recording_files item to (priority_rank, file_extension)."""
    status = str(entry.get("status", "")).strip().lower()
    if status and status != "completed":
        return None
    if not entry.get("download_url"):
        return None

    ftype = str(entry.get("file_type", "")).strip().upper()
    fext = str(entry.get("file_extension", "")).strip().upper()
    if fext == "VTT" or ftype in ("TRANSCRIPT", "VTT", "CC"):
        return (0, ".vtt")
    if fext == "M4A" or ftype in ("M4A", "AUDIO_ONLY"):
        return (1, ".m4a")
    if fext == "MP4" or ftype == "MP4" or "SPEAKER" in ftype or "SCREEN" in ftype:
        return (2, ".mp4")
    return None


def verify_zoom_webhook_signature(
    headers: Mapping[str, str],
    raw_body: bytes | str,
    webhook_secret: str,
    now_ts: Optional[float] = None,
    max_skew_seconds: int = 300,
) -> bool:
    """Validate Zoom's v0 HMAC-SHA256 webhook signature (`x-zm-signature`)."""
    secret = (webhook_secret or "").strip()
    if not secret:
        return False

    lowered = {k.lower(): v for k, v in headers.items()}
    sig = str(lowered.get("x-zm-signature", "")).strip()
    ts_raw = str(lowered.get("x-zm-request-timestamp", "")).strip()
    if not sig.startswith("v0=") or not ts_raw:
        return False

    try:
        req_ts = int(ts_raw)
    except ValueError:
        return False

    current_ts = now_ts if now_ts is not None else time.time()
    if abs(current_ts - req_ts) > max_skew_seconds:
        return False

    body_text = (
        raw_body.decode("utf-8", errors="replace")
        if isinstance(raw_body, (bytes, bytearray))
        else str(raw_body)
    )
    message = f"v0:{ts_raw}:{body_text}"
    digest = hmac.new(
        secret.encode("utf-8"),
        message.encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()
    return hmac.compare_digest(f"v0={digest}", sig)


def build_zoom_url_validation_response(
    plain_token: str, webhook_secret: str
) -> dict[str, str]:
    """Compute encryptedToken for Zoom's `endpoint.url_validation` challenge."""
    digest = hmac.new(
        webhook_secret.strip().encode("utf-8"),
        plain_token.encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()
    return {"plainToken": plain_token, "encryptedToken": digest}


class ZoomBridgeAdapter(ABC):
    """Abstract adapter isolating Zoom API constraints from live classroom chat and audio."""

    @abstractmethod
    def is_configured(self) -> bool:
        """Return True if external Zoom credentials are configured."""

    @abstractmethod
    def dispatch_chat_message(
        self, msg: ChatMessage, offset_mmss: str, session_code: str
    ) -> bool:
        """Relay a classroom chat message to Zoom Team Chat. Never raises on failure."""

    @abstractmethod
    def fetch_cloud_recording(
        self, meeting_id: str, dest_dir: str | Path
    ) -> Optional[str]:
        """Download a Zoom Cloud Recording (Pro/Edu accounts) or return None."""

    def list_meeting_recordings(self, meeting_id: str) -> Optional[dict[str, Any]]:
        """Optional hook: return recording metadata dict for a Zoom meeting ID."""
        return None

    def list_recent_recordings(
        self, user_id: str = "me", days_back: int = 7
    ) -> list[dict[str, Any]]:
        """Optional hook: return recent cloud recording summaries for an account."""
        return []

    def download_best_recording_file(
        self,
        recording_files: list[dict[str, Any]],
        dest_dir: str | Path,
        meeting_id: str,
        download_token: Optional[str] = None,
    ) -> Optional[Path]:
        """Optional hook: download highest-priority file (.vtt -> .m4a -> .mp4)."""
        return None


class NoOpZoomBridge(ZoomBridgeAdapter):
    """Default safe adapter when running without Zoom OAuth credentials."""

    def is_configured(self) -> bool:
        return False

    def dispatch_chat_message(
        self, msg: ChatMessage, offset_mmss: str, session_code: str
    ) -> bool:
        return False

    def fetch_cloud_recording(
        self, meeting_id: str, dest_dir: str | Path
    ) -> Optional[str]:
        return None


class ZoomOAuthRestBridge(ZoomBridgeAdapter):
    """Server-to-Server OAuth Zoom adapter for Team Chat channel relay and Pro Cloud Recordings."""

    TOKEN_URL = "https://zoom.us/oauth/token"
    API_BASE = "https://api.zoom.us/v2"

    def __init__(
        self,
        account_id: str,
        client_id: str,
        client_secret: str,
        team_channel_id: str = "",
        timeout_sec: float = 3.5,
    ) -> None:
        self.account_id = account_id.strip()
        self.client_id = client_id.strip()
        self.client_secret = client_secret.strip()
        self.team_channel_id = team_channel_id.strip()
        self.timeout_sec = timeout_sec
        self._cached_token: Optional[str] = None
        self._token_expires_at: float = 0.0

    def is_configured(self) -> bool:
        return bool(self.account_id and self.client_id and self.client_secret)

    def _get_access_token(self) -> str:
        now = time.time()
        if self._cached_token and now < self._token_expires_at - 30:
            return self._cached_token

        resp = requests.post(
            self.TOKEN_URL,
            params={
                "grant_type": "account_credentials",
                "account_id": self.account_id,
            },
            auth=(self.client_id, self.client_secret),
            timeout=self.timeout_sec,
        )
        resp.raise_for_status()
        payload = resp.json()
        self._cached_token = str(payload["access_token"])
        self._token_expires_at = now + float(payload.get("expires_in", 3500))
        return self._cached_token

    def dispatch_chat_message(
        self, msg: ChatMessage, offset_mmss: str, session_code: str
    ) -> bool:
        if not self.is_configured() or not self.team_channel_id:
            return False
        try:
            token = self._get_access_token()
            formatted = (
                f"[{session_code} {offset_mmss}] [{msg.category}] "
                f"{msg.author_name}: {msg.body}"
            )
            resp = requests.post(
                f"{self.API_BASE}/chat/users/me/messages",
                headers={"Authorization": f"Bearer {token}"},
                json={
                    "message": formatted,
                    "to_channel": self.team_channel_id,
                },
                timeout=self.timeout_sec,
            )
            return 200 <= resp.status_code < 300
        except Exception:  # noqa: BLE001
            # Strictly isolate external Zoom network/API errors from live classroom chat
            return False

    @staticmethod
    def _raise_if_zoom_api_error(resp: requests.Response) -> None:
        if resp.status_code in (400, 401, 403):
            try:
                detail = str(resp.json().get("message", ""))
            except Exception:  # noqa: BLE001
                detail = resp.text[:200]
            raise ZoomRecordingError(
                f"Zoom API HTTP {resp.status_code}: {detail or 'Check cloud_recording:read scopes or Zoom Pro/Edu license.'}",
                status_code=resp.status_code,
            )

    def _encode_meeting_ref(self, meeting_id: str) -> str:
        clean = normalize_zoom_meeting_id(meeting_id)
        if "/" in clean:
            return quote(quote(clean, safe=""), safe="")
        return quote(clean, safe="")

    def list_meeting_recordings(self, meeting_id: str) -> Optional[dict[str, Any]]:
        clean_id = normalize_zoom_meeting_id(meeting_id)
        if not self.is_configured() or not clean_id:
            return None
        try:
            token = self._get_access_token()
            ref = self._encode_meeting_ref(clean_id)
            resp = requests.get(
                f"{self.API_BASE}/meetings/{ref}/recordings",
                headers={"Authorization": f"Bearer {token}"},
                timeout=max(self.timeout_sec, 6.0),
            )
            if resp.status_code == 200:
                return dict(resp.json())
            self._raise_if_zoom_api_error(resp)
            return None
        except ZoomRecordingError:
            raise
        except Exception:  # noqa: BLE001
            return None

    def _resolve_default_user_id(self, token: str, preferred_user: str) -> str:
        user_ref = (preferred_user or "me").strip() or "me"
        if user_ref != "me":
            return user_ref
        resp = requests.get(
            f"{self.API_BASE}/users",
            params={"page_size": 5, "status": "active"},
            headers={"Authorization": f"Bearer {token}"},
            timeout=max(self.timeout_sec, 6.0),
        )
        if resp.status_code == 200:
            users = resp.json().get("users", [])
            if users:
                return str(users[0].get("id") or users[0].get("email") or "me")
        return "me"

    def list_recent_recordings(
        self, user_id: str = "me", days_back: int = 14
    ) -> list[dict[str, Any]]:
        if not self.is_configured():
            return []
        try:
            token = self._get_access_token()
            target_user = self._resolve_default_user_id(token, user_id)
            now = datetime.now(timezone.utc).date()
            start = now - timedelta(days=max(1, min(days_back, 30)))
            resp = requests.get(
                f"{self.API_BASE}/users/{quote(target_user, safe='')}/recordings",
                params={"from": start.isoformat(), "to": now.isoformat(), "page_size": 20},
                headers={"Authorization": f"Bearer {token}"},
                timeout=max(self.timeout_sec, 6.0),
            )
            self._raise_if_zoom_api_error(resp)
            if resp.status_code != 200:
                return []
            meetings = list(resp.json().get("meetings", []))
            meetings.sort(key=lambda m: str(m.get("start_time", "")), reverse=True)
            return meetings
        except ZoomRecordingError:
            raise
        except Exception:  # noqa: BLE001
            return []

    def download_best_recording_file(
        self,
        recording_files: list[dict[str, Any]],
        dest_dir: str | Path,
        meeting_id: str,
        download_token: Optional[str] = None,
    ) -> Optional[Path]:
        if not recording_files:
            return None

        candidates: list[tuple[int, str, dict[str, Any]]] = []
        for item in recording_files:
            classified = _classify_recording_file(item)
            if classified is not None:
                rank, ext = classified
                candidates.append((rank, ext, item))
        if not candidates:
            return None

        candidates.sort(key=lambda tup: tup[0])
        _best_rank, ext, target_file = candidates[0]
        dl_url = str(target_file["download_url"])

        bearer = (download_token or "").strip()
        if not bearer:
            if not self.is_configured():
                return None
            bearer = self._get_access_token()

        safe_id = re.sub(r"[^A-Za-z0-9_-]", "_", normalize_zoom_meeting_id(meeting_id) or "rec")
        out_path = Path(dest_dir) / f"zoom_{safe_id}_{int(time.time())}{ext}"
        out_path.parent.mkdir(parents=True, exist_ok=True)
        with requests.get(
            dl_url,
            headers={"Authorization": f"Bearer {bearer}"},
            stream=True,
            timeout=60.0,
        ) as dl_resp:
            dl_resp.raise_for_status()
            with out_path.open("wb") as fh:
                for chunk in dl_resp.iter_content(chunk_size=65536):
                    if chunk:
                        fh.write(chunk)
        return out_path

    def fetch_cloud_recording(
        self, meeting_id: str, dest_dir: str | Path
    ) -> Optional[str]:
        try:
            bundle = self.list_meeting_recordings(meeting_id)
            if not bundle:
                return None
            files = bundle.get("recording_files", [])
            saved = self.download_best_recording_file(
                recording_files=files,
                dest_dir=dest_dir,
                meeting_id=meeting_id,
            )
            return str(saved) if saved is not None else None
        except Exception:  # noqa: BLE001
            return None
