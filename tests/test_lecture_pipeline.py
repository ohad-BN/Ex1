from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from app import create_app, socketio
from app.config import Config
from app.models import ClassSession, Course
from app.services.chat_service import ChatService
from app.services.face_counter import BoundingBox, FaceDetector
from app.services.llm_client import GeminiStudioClient


class StubDetector(FaceDetector):
    def detect(self, image) -> list[BoundingBox]:
        return []


class FakeClock:
    def __init__(self) -> None:
        self.now = 100.0
        self.sleeps: list[float] = []

    def time(self) -> float:
        return self.now

    def sleep(self, sec: float) -> None:
        self.sleeps.append(sec)
        self.now += sec


@pytest.fixture()
def lecture_app(tmp_path: Path):
    db_file = tmp_path / "test_phase7_lectures.db"
    cfg = Config(
        SECRET_KEY="phase7-lecture-secret",
        DATABASE_URL=f"sqlite:///{db_file.as_posix()}",
        GEMINI_API_KEY="",
    )
    app = create_app(cfg, face_detector=StubDetector())
    app.config["TESTING"] = True
    yield app
    app.extensions["task_manager"].shutdown(wait=True)
    app.extensions["db_engine"].dispose()


def test_gemini_client_pacing_and_429_exponential_backoff() -> None:
    """Suite 7: Verify thread-safe RPM pacing and 429 retry backoff."""
    clock = FakeClock()
    attempts = {"n": 0}

    class FakeModels:
        def generate_content(self, **_kwargs):
            attempts["n"] += 1
            if attempts["n"] <= 2:
                raise RuntimeError("HTTP 429 RESOURCE_EXHAUSTED")
            return SimpleNamespace(text="Corrected response on 3rd attempt")

    fake_genai = SimpleNamespace(models=FakeModels())
    client = GeminiStudioClient(
        api_key="test-key",
        min_interval_sec=4.5,
        backoff_base_sec=2.0,
        max_retries=3,
        fallback_on_error=False,
        client_override=fake_genai,
        clock_fn=clock.time,
        sleep_fn=clock.sleep,
    )
    out = client.generate_text("test prompt")
    assert out == "Corrected response on 3rd attempt"
    assert attempts["n"] == 3
    # Backoff sleeps (2.0s, 4.0s) plus RPM pacing delays triggered between attempts
    assert 2.0 in clock.sleeps
    assert 4.0 in clock.sleeps

    # Verify automatic 404 model fallback (e.g., gemini-2.5-flash -> gemini-3.8-flash)
    class DeprecatedModelFake:
        def generate_content(self, **kwargs):
            if kwargs.get("model") == "gemini-2.5-flash":
                raise RuntimeError("404 NOT_FOUND: model no longer available")
            return SimpleNamespace(text=f"OK via {kwargs.get('model')}")

    client_404 = GeminiStudioClient(
        api_key="test-key",
        fast_model="gemini-2.5-flash",
        min_interval_sec=0.0,
        fallback_on_error=False,
        client_override=SimpleNamespace(models=DeprecatedModelFake()),
    )
    assert client_404.generate_text("hello") == "OK via gemini-3.8-flash"
    assert client_404.fast_model == "gemini-3.8-flash"


def test_lecture_2pass_correction_chat_crosslink_and_course_endpoint(lecture_app) -> None:
    """Suite 7: Pass 1 domain term correction, Pass 2 summary + chat cross-link, and course listing."""
    client = lecture_app.test_client()
    db = lecture_app.extensions["db_session"]

    course = Course(
        course_code="OOP404",
        name="Object-Oriented Software Engineering",
        department="CS",
        lecturer_name="Prof. Boaz",
    )
    db.add(course)
    db.flush()
    sess = ClassSession(
        course_id=course.id,
        session_code="LEC7A",
        title="Lecture 7 — SOLID & Bridge Patterns",
    )
    db.add(sess)
    db.commit()

    # Add a Feature 2 student question in the class chat so Pass 2 cross-links it
    ChatService(db).post_message(
        session_id=sess.id,
        author_name="Ohad Ben Nun",
        category="Question",
        body="Why prefer Bridge Pattern over inheritance here?",
    )

    sio = socketio.test_client(lecture_app)
    sio.emit("join_session", {"session_code": "LEC7A"})
    sio.get_received()

    # Summarize built-in Ariel sample recording with phonetic ASR errors
    resp = client.post(
        f"/api/sessions/{sess.id}/lectures/summarize",
        json={"use_sample": True},
    )
    assert resp.status_code == 201
    data = resp.get_json()
    assert data["status"] == "COMPLETED"

    # Verify Pass 1 fixed phonetic ASR errors while preserving [MM:SS]
    joined_corrected = " ".join(seg["text"] for seg in data["corrected_transcript"])
    assert "SOLID Principles" in joined_corrected
    assert "Bridge Pattern" in joined_corrected
    assert "O(1)" in joined_corrected
    assert "pytest" in joined_corrected
    assert data["corrected_transcript"][0]["offset_mmss"] == "00:15"

    # Verify Pass 2 cross-linked the student chat question
    assert "Ohad Ben Nun" in data["summary_markdown"]
    assert "Why prefer Bridge Pattern over inheritance here?" in data["summary_markdown"]

    # Verify Socket.IO emitted lecture:summary_ready
    events = [e["name"] for e in sio.get_received()]
    assert "lecture:summary_ready" in events
    sio.disconnect()

    # Verify multi-course Study Hub API exposes the completed summary under course_id
    course_lecs = client.get(f"/api/courses/{course.id}/lectures")
    assert course_lecs.status_code == 200
    assert len(course_lecs.get_json()["artifacts"]) == 1

    # Verify Phase 13C one-click Markdown summary & text transcript downloads
    art_id = data["id"]
    md_dl = client.get(f"/api/lectures/{art_id}/download/summary.md")
    assert md_dl.status_code == 200
    assert "attachment" in md_dl.headers.get("Content-Disposition", "")
    assert "OOP404" in md_dl.data.decode("utf-8")

    txt_dl = client.get(f"/api/lectures/{art_id}/download/transcript.txt")
    assert txt_dl.status_code == 200
    txt_body = txt_dl.data.decode("utf-8")
    assert "[00:15]" in txt_body
    assert "SOLID Principles" in txt_body


def test_zoom_vtt_parsing_chunked_pass1_and_signature_helpers(tmp_path: Path) -> None:
    """Phase 13B/C: VTT timestamp cues, >80-segment chunking, ID extraction, and webhook HMAC."""
    import hashlib
    import hmac
    import time

    from app.services.lecture_pipeline import LectureSummarizerService
    from app.services.zoom_bridge import (
        ZoomOAuthRestBridge,
        build_zoom_url_validation_response,
        normalize_zoom_meeting_id,
        verify_zoom_webhook_signature,
    )

    assert normalize_zoom_meeting_id("https://us02web.zoom.us/j/81234567890?pwd=abc") == "81234567890"
    assert normalize_zoom_meeting_id("812 3456-7890") == "81234567890"

    secret = "zoom-webhook-secret-test"
    val_resp = build_zoom_url_validation_response("plain_abc", secret)
    assert val_resp["plainToken"] == "plain_abc"
    assert len(val_resp["encryptedToken"]) == 64

    ts = str(int(time.time()))
    raw_body = b'{"event":"recording.completed"}'
    good_sig = "v0=" + hmac.new(
        secret.encode(), f"v0:{ts}:{raw_body.decode()}".encode(), hashlib.sha256
    ).hexdigest()
    assert verify_zoom_webhook_signature(
        {"x-zm-signature": good_sig, "x-zm-request-timestamp": ts}, raw_body, secret
    )
    assert not verify_zoom_webhook_signature(
        {"x-zm-signature": "v0=deadbeef", "x-zm-request-timestamp": ts}, raw_body, secret
    )

    # Verify VTT parser extracts real HH:MM:SS timestamps from cues
    db_file = tmp_path / "test_vtt.db"
    cfg = Config(SECRET_KEY="s", DATABASE_URL=f"sqlite:///{db_file.as_posix()}", GEMINI_API_KEY="")
    app = create_app(cfg, face_detector=StubDetector())
    try:
        svc = LectureSummarizerService(db=app.extensions["db_session"])
        vtt_sample = (
            "WEBVTT\n\n"
            "1\n"
            "00:01:23.450 --> 00:01:28.100\n"
            "Today we fix solid principals using pie test.\n\n"
            "2\n"
            "01:02:05.000 --> 01:02:09.000\n"
            "Recall oh of one lookups in SQLite.\n"
        )
        parsed = svc.parse_transcript_text(vtt_sample)
        assert parsed[0]["offset_mmss"] == "01:23"
        assert parsed[1]["offset_mmss"] == "62:05"

        # Verify chunked Pass 1 handles >80 segments across batches with mocked Gemini client
        calls = {"count": 0}

        class BatchFakeModels:
            def generate_content(self, **kwargs):
                calls["count"] += 1
                prompt_text = str(kwargs.get("contents", ""))
                lines = [ln for ln in prompt_text.splitlines() if ln.startswith("[")]
                return SimpleNamespace(text="\n".join(lines))

        batch_client = GeminiStudioClient(
            api_key="fake",
            min_interval_sec=0.0,
            client_override=SimpleNamespace(models=BatchFakeModels()),
        )
        svc_chunked = LectureSummarizerService(
            db=app.extensions["db_session"], llm_client=batch_client
        )
        many_segs = [{"offset_mmss": f"{i:02d}:00", "text": f"Segment {i}"} for i in range(95)]
        out = svc_chunked._run_pass1_correction(many_segs, "CS204", "Long Lecture", "")
        assert len(out) == 95
        assert calls["count"] == 2  # 80 + 15 batches

        # Verify download_best_recording_file prioritizes VTT over M4A/MP4
        class FakeStreamResp:
            def __init__(self, payload: bytes) -> None:
                self._payload = payload

            def __enter__(self):
                return self

            def __exit__(self, *_args):
                return False

            def raise_for_status(self):
                return None

            def iter_content(self, chunk_size=65536):
                yield self._payload

        import app.services.zoom_bridge as zb

        orig_get = zb.requests.get
        try:
            zb.requests.get = lambda *_a, **_kw: FakeStreamResp(b"WEBVTT\n\n00:00:05.000 --> 00:00:08.000\nHello VTT\n")
            bridge = ZoomOAuthRestBridge("acc", "cid", "csec")
            bridge._cached_token = "tok"
            bridge._token_expires_at = time.time() + 1000
            dl = bridge.download_best_recording_file(
                recording_files=[
                    {"file_type": "MP4", "file_extension": "MP4", "status": "completed", "download_url": "https://zoom.us/mp4"},
                    {"file_type": "TRANSCRIPT", "file_extension": "VTT", "status": "completed", "download_url": "https://zoom.us/vtt"},
                ],
                dest_dir=tmp_path,
                meeting_id="81234567890",
            )
            assert dl is not None and dl.suffix == ".vtt"
        finally:
            zb.requests.get = orig_get
    finally:
        app.extensions["task_manager"].shutdown(wait=True)
        app.extensions["db_engine"].dispose()


def test_zoom_cloud_fetch_autopoller_and_optional_webhook(tmp_path: Path) -> None:
    """Phase 13B/C/D: 3-key Zoom Cloud fetch, ZoomCloudAutoPoller idempotency, and webhook verification."""
    import hashlib
    import hmac
    import time

    from app.services.zoom_bridge import ZoomBridgeAdapter

    class StubZoomCloudBridge(ZoomBridgeAdapter):
        def __init__(self) -> None:
            self.uuid_counter = 1
            self.downloads = 0

        def is_configured(self) -> bool:
            return True

        def dispatch_chat_message(self, msg, offset_mmss: str, session_code: str) -> bool:
            return True

        def fetch_cloud_recording(self, meeting_id: str, dest_dir: str | Path) -> str | None:
            return None

        def list_meeting_recordings(self, meeting_id: str):
            return {
                "id": meeting_id,
                "uuid": f"uuid-{self.uuid_counter}",
                "topic": "OOP Lecture",
                "recording_files": [
                    {
                        "file_type": "TRANSCRIPT",
                        "file_extension": "VTT",
                        "status": "completed",
                        "download_url": "https://zoom.example/rec.vtt",
                    }
                ],
            }

        def list_recent_recordings(self, user_id: str = "me", days_back: int = 7):
            rec = self.list_meeting_recordings("9988776655")
            return [rec] if rec else []

        def download_best_recording_file(
            self, recording_files, dest_dir: str | Path, meeting_id: str, download_token=None
        ):
            self.downloads += 1
            out = Path(dest_dir) / f"zoom_{meeting_id}_{self.downloads}.vtt"
            out.parent.mkdir(parents=True, exist_ok=True)
            out.write_text(
                "WEBVTT\n\n00:00:12.000 --> 00:00:18.000\nWelcome to bright pattern and pie test.\n",
                encoding="utf-8",
            )
            return out

    db_file = tmp_path / "test_zoom_flow.db"
    cfg = Config(
        SECRET_KEY="zoom-flow-secret",
        DATABASE_URL=f"sqlite:///{db_file.as_posix()}",
        GEMINI_API_KEY="",
        ZOOM_WEBHOOK_SECRET_TOKEN="wh-secret-123",
    )
    stub_zoom = StubZoomCloudBridge()
    app = create_app(cfg, face_detector=StubDetector(), zoom_bridge=stub_zoom)
    app.config["TESTING"] = True
    try:
        client = app.test_client()
        db = app.extensions["db_session"]

        # Verify non-secret integration status route
        st = client.get("/api/integrations/status").get_json()
        assert st["zoom_configured"] is True
        assert st["webhook_secret_configured"] is True
        assert st["gemini_configured"] is False

        course = Course(
            course_code="CS999",
            name="Zoom Cloud Automation",
            department="CS",
            lecturer_name="Prof. Boaz",
        )
        db.add(course)
        db.flush()
        sess = ClassSession(
            course_id=course.id,
            session_code="ZMPULL",
            title="Zoom Cloud Test",
            zoom_meeting_id="9988776655",
            zoom_auto_sync=True,
        )
        db.add(sess)
        db.commit()

        # 1) Manual 3-key Zoom Cloud Fetch (sync=true for deterministic assertion)
        res = client.post(
            f"/api/sessions/{sess.id}/lectures/zoom-fetch",
            json={"zoom_meeting_id": "https://zoom.us/j/9988776655", "sync": True},
        )
        assert res.status_code == 201
        art = res.get_json()["artifact"]
        assert art["status"] == "COMPLETED"
        assert art["zoom_meeting_uuid"] == "uuid-1"
        assert "Bridge Pattern" in art["summary_markdown"] or "Bridge Pattern" in str(art["corrected_transcript"])

        # 2) Auto-poller skips already completed uuid-1, then picks up uuid-2 automatically
        poller = app.extensions["zoom_poller"]
        assert poller.tick_once() == 0
        stub_zoom.uuid_counter = 2
        assert poller.tick_once() == 1
        assert len(client.get(f"/api/sessions/{sess.id}/lectures").get_json()["artifacts"]) == 2

        # 3) Optional webhook endpoint: validation challenge + invalid signature rejection
        val = client.post(
            "/api/webhooks/zoom",
            json={"event": "endpoint.url_validation", "payload": {"plainToken": "abc123"}},
        )
        assert val.status_code == 200
        assert val.get_json()["plainToken"] == "abc123"

        bad_wh = client.post(
            "/api/webhooks/zoom",
            data=b'{"event":"recording.completed"}',
            content_type="application/json",
            headers={"x-zm-signature": "v0=bad", "x-zm-request-timestamp": str(int(time.time()))},
        )
        assert bad_wh.status_code == 401

        stub_zoom.uuid_counter = 3
        wh_body = (
            '{"event":"recording.completed","download_token":"tok123","payload":{"object":'
            '{"id":"9988776655","uuid":"uuid-3","recording_files":'
            '[{"file_type":"TRANSCRIPT","file_extension":"VTT","status":"completed","download_url":"https://zoom.example/3.vtt"}]}}}'
        ).encode("utf-8")
        ts = str(int(time.time()))
        sig = "v0=" + hmac.new(
            b"wh-secret-123", f"v0:{ts}:{wh_body.decode()}".encode("utf-8"), hashlib.sha256
        ).hexdigest()
        good_wh = client.post(
            "/api/webhooks/zoom",
            data=wh_body,
            content_type="application/json",
            headers={"x-zm-signature": sig, "x-zm-request-timestamp": ts},
        )
        assert good_wh.status_code == 202
        # Wait briefly for background AsyncTaskManager job to finish cleanly before teardown
        app.extensions["task_manager"].shutdown(wait=True)
    finally:
        app.extensions["task_manager"].shutdown(wait=True)
        app.extensions["db_engine"].dispose()
