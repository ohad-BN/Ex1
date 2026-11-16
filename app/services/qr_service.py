from __future__ import annotations

import base64
import hashlib
import hmac
import io
import re
import socket
import time
import urllib.request
from typing import Any, Optional

import segno
from itsdangerous import BadSignature, SignatureExpired, URLSafeSerializer, URLSafeTimedSerializer

_TUNNEL_URL_CACHE: tuple[float, Optional[str]] = (0.0, None)
_TRYCLOUDFLARE_RE = re.compile(r"https://[a-zA-Z0-9-]+\.trycloudflare\.com")


def discover_active_cloudflared_url(ttl_sec: float = 10.0) -> Optional[str]:
    """Auto-discover the live *.trycloudflare.com URL from the cloudflared sidecar metrics endpoint."""
    global _TUNNEL_URL_CACHE
    now = time.time()
    cached_ts, cached_url = _TUNNEL_URL_CACHE
    if cached_url and (now - cached_ts) < ttl_sec:
        return cached_url

    for endpoint in (
        "http://tunnel:2000/quicktunnel",
        "http://tunnel:2000/metrics",
        "http://127.0.0.1:2000/quicktunnel",
        "http://127.0.0.1:2000/metrics",
    ):
        try:
            with urllib.request.urlopen(endpoint, timeout=0.35) as resp:
                body = resp.read().decode("utf-8", errors="ignore")
                match = _TRYCLOUDFLARE_RE.search(body)
                if match:
                    found = match.group(0).rstrip("/")
                    _TUNNEL_URL_CACHE = (now, found)
                    return found
                # /quicktunnel JSON format: {"hostname":"xyz.trycloudflare.com"}
                host_match = re.search(
                    r'"hostname"\s*:\s*"([a-zA-Z0-9-]+\.trycloudflare\.com)"', body
                )
                if host_match:
                    found = f"https://{host_match.group(1)}"
                    _TUNNEL_URL_CACHE = (now, found)
                    return found
        except Exception:
            continue

    return cached_url


def get_lan_ip() -> str:
    """Detect the host PC's primary LAN IPv4 address so phones on Wi-Fi never get 'localhost'."""
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.connect(("8.8.8.8", 80))
            return str(s.getsockname()[0])
    except OSError:
        return "127.0.0.1"


def resolve_effective_base_url(configured_base_url: str, request_host_url: str) -> str:
    """Resolve the best external URL for QR codes (PUBLIC_BASE_URL > Live Cloudflare Tunnel > Request Tunnel Host > LAN IP)."""
    if configured_base_url:
        return configured_base_url.rstrip("/")

    clean = request_host_url.rstrip("/")
    if ".trycloudflare.com" in clean:
        return clean.replace("http://", "https://")

    discovered_tunnel = discover_active_cloudflared_url()
    if discovered_tunnel:
        return discovered_tunnel

    for local_name in ("//localhost:", "//127.0.0.1:"):
        if local_name in clean:
            lan_ip = get_lan_ip()
            if lan_ip != "127.0.0.1":
                return clean.replace("localhost", lan_ip).replace("127.0.0.1", lan_ip)
    return clean


class RollingQRService:
    """Generates and validates 5-second rolling HMAC-SHA256 QR tokens with a grace window."""

    def __init__(
        self,
        secret_key: str,
        step_seconds: int = 5,
        grace_buckets: int = 1,
    ) -> None:
        self._secret_bytes = secret_key.encode("utf-8")
        self.step_seconds = max(1, step_seconds)
        self.grace_buckets = max(0, grace_buckets)
        self._grant_serializer = URLSafeTimedSerializer(
            secret_key, salt="classpulse-onboarding-grant"
        )

    def compute_bucket(self, timestamp: Optional[float] = None) -> int:
        """Compute discrete time bucket index floor(ts / step_seconds)."""
        ts = timestamp if timestamp is not None else time.time()
        return int(ts // self.step_seconds)

    def sign_token(self, session_code: str, bucket: int) -> str:
        """Create a 16-char HMAC-SHA256 signature for (session_code, bucket)."""
        msg = f"{session_code}:{bucket}".encode("utf-8")
        return hmac.new(self._secret_bytes, msg, hashlib.sha256).hexdigest()[:16]

    def verify_token(
        self,
        session_code: str,
        token: str,
        bucket: int,
        now_ts: Optional[float] = None,
    ) -> bool:
        """Verify HMAC signature and ensure bucket is within [current - grace, current]."""
        if not token or not session_code:
            return False
        current_bucket = self.compute_bucket(now_ts)
        allowed_buckets = range(
            current_bucket - self.grace_buckets, current_bucket + 1
        )
        if bucket not in allowed_buckets:
            return False
        expected = self.sign_token(session_code, bucket)
        return hmac.compare_digest(expected, str(token))

    def create_onboarding_grant(
        self, session_code: str, device_id: str, bucket: int
    ) -> str:
        """Issue a short-lived signed grant when a first-time student scans a valid QR.

        Allows the student 90 seconds to type their Name + Email without failing the 5s QR window.
        """
        return self._grant_serializer.dumps(
            {
                "session_code": session_code,
                "device_id": device_id,
                "bucket": bucket,
            }
        )

    def verify_onboarding_grant(
        self,
        grant_token: str,
        session_code: str,
        max_age_sec: int = 90,
    ) -> Optional[dict[str, Any]]:
        """Validate a short-lived onboarding grant token."""
        if not grant_token:
            return None
        try:
            data = self._grant_serializer.loads(grant_token, max_age=max_age_sec)
        except (BadSignature, SignatureExpired):
            return None
        if not isinstance(data, dict) or data.get("session_code") != session_code:
            return None
        return data

    def render_qr_svg_data_uri(self, url: str) -> str:
        """Render a high-contrast QR code as an inline base64 SVG data URI."""
        qr = segno.make(url, error="m")
        buf = io.BytesIO()
        qr.save(buf, kind="svg", scale=8, border=2, dark="#0f172a", light="#ffffff")
        b64 = base64.b64encode(buf.getvalue()).decode("ascii")
        return f"data:image/svg+xml;base64,{b64}"

    def build_checkin_url(
        self,
        base_url: str,
        session_code: str,
        now_ts: Optional[float] = None,
    ) -> str:
        """Construct the signed rotating check-in URL for the current 5s bucket."""
        bucket = self.compute_bucket(now_ts)
        sig = self.sign_token(session_code, bucket)
        clean_base = base_url.rstrip("/")
        return f"{clean_base}/s/{session_code}?t={bucket}&sig={sig}"

    def build_qr_payload(
        self,
        base_url: str,
        session_code: str,
        now_ts: Optional[float] = None,
    ) -> dict[str, Any]:
        """Build complete JSON + SVG payload for the projector Socket.IO rotation."""
        ts = now_ts if now_ts is not None else time.time()
        bucket = self.compute_bucket(ts)
        sig = self.sign_token(session_code, bucket)
        clean_base = base_url.rstrip("/")
        url = f"{clean_base}/s/{session_code}?t={bucket}&sig={sig}"
        next_bucket_ts = (bucket + 1) * self.step_seconds
        expires_in_ms = max(500, int((next_bucket_ts - ts) * 1000))
        return {
            "session_code": session_code,
            "bucket": bucket,
            "sig": sig,
            "url": url,
            "scan_url": url,
            "svg_data_uri": self.render_qr_svg_data_uri(url),
            "step_seconds": self.step_seconds,
            "expires_in_ms": expires_in_ms,
        }


class DeviceBindingManager:
    """Signs, times, and verifies persistent HttpOnly student device, lecturer, and projector tokens."""

    COOKIE_NAME = "cp_device"
    LECTURER_COOKIE_NAME = "cp_lecturer"

    def __init__(
        self, secret_key: str, signer_salt: str = "classpulse-device-v1"
    ) -> None:
        self._serializer = URLSafeTimedSerializer(secret_key, salt=signer_salt)
        self._lecturer_serializer = URLSafeTimedSerializer(
            secret_key, salt="classpulse-lecturer-v2"
        )
        self._projector_serializer = URLSafeTimedSerializer(
            secret_key, salt="classpulse-projector-v1"
        )

    def create_device_cookie(self, device_id: str, student_id: int) -> str:
        """Serialize and sign (device_id, student_id) for the browser cookie."""
        return self._serializer.dumps(
            {"device_id": str(device_id), "student_id": int(student_id)}
        )

    def parse_device_cookie(
        self,
        cookie_val: Optional[str],
        max_age: Optional[int] = 86400 * 180,
    ) -> Optional[tuple[str, int]]:
        """Verify signed timed cookie and return (device_id, student_id) or None if expired/tampered."""
        if not cookie_val:
            return None
        try:
            data = self._serializer.loads(cookie_val, max_age=max_age)
            if not isinstance(data, dict):
                return None
            device_id = str(data["device_id"])
            student_id = int(data["student_id"])
            return device_id, student_id
        except (BadSignature, SignatureExpired, KeyError, ValueError, TypeError):
            return None

    def create_lecturer_cookie(
        self, lecturer_id: int, token_version: int = 1
    ) -> str:
        """Serialize and sign (lecturer_id, token_version) for the teacher browser cookie."""
        return self._lecturer_serializer.dumps(
            {"lecturer_id": int(lecturer_id), "tv": int(token_version)}
        )

    def parse_lecturer_cookie(
        self,
        cookie_val: Optional[str],
        max_age: Optional[int] = 86400 * 7,
    ) -> Optional[tuple[int, int]]:
        """Verify signed timed lecturer cookie and return (lecturer_id, token_version) or None."""
        if not cookie_val:
            return None
        try:
            data = self._lecturer_serializer.loads(cookie_val, max_age=max_age)
            if not isinstance(data, dict):
                return None
            return int(data["lecturer_id"]), int(data.get("tv", 1))
        except (BadSignature, SignatureExpired, KeyError, ValueError, TypeError):
            return None

    def create_projector_token(self, session_code: str) -> str:
        """Issue a scoped share token for classroom projector displays."""
        return self._projector_serializer.dumps(
            {"session_code": str(session_code).strip().upper()}
        )

    def verify_projector_token(
        self, token: Optional[str], session_code: str, max_age: int = 43200
    ) -> bool:
        """Verify a classroom projector token matches the given session_code."""
        if not token or not session_code:
            return False
        try:
            data = self._projector_serializer.loads(token, max_age=max_age)
            return (
                isinstance(data, dict)
                and str(data.get("session_code", "")).upper()
                == str(session_code).strip().upper()
            )
        except (BadSignature, SignatureExpired, TypeError):
            return False
