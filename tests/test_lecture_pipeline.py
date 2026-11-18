from __future__ import annotations

import io
from pathlib import Path
from types import SimpleNamespace

import pytest

from app import create_app, socketio
from app.config import Config
from app.models import ClassSession, Course
from app.services.chat_service import ChatService
from app.services.face_counter import BoundingBox, FaceDetector
from app.services.lecture_pipeline import LectureSummarizerService
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
    """Suite 7: Verify thread-safe RPM pacing, 429 retry backoff, and 404 model fallback."""
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
    assert 2.0 in clock.sleeps
    assert 4.0 in clock.sleeps

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
    assert client_404.generate_text("hello") == "OK via gemini-2.5-flash-lite"
    assert client_404.fast_model == "gemini-2.5-flash-lite"

    class QuotaExhaustedFake:
        def generate_content(self, **kwargs):
            if kwargs.get("model") == "gemini-2.5-flash-lite":
                raise RuntimeError("HTTP 429 RESOURCE_EXHAUSTED")
            return SimpleNamespace(text=f"Recovered via {kwargs.get('model')}")

    client_429 = GeminiStudioClient(
        api_key="test-key",
        fast_model="gemini-2.5-flash-lite",
        min_interval_sec=0.0,
        backoff_base_sec=0.0,
        max_retries=1,
        fallback_on_error=False,
        client_override=SimpleNamespace(models=QuotaExhaustedFake()),
        clock_fn=clock.time,
        sleep_fn=clock.sleep,
    )
    assert client_429.generate_text("hello") == "Recovered via gemini-3.1-flash-lite"
    assert client_429.fast_model == "gemini-3.1-flash-lite"


def test_lecture_2pass_correction_chat_crosslink_and_course_endpoint(lecture_app) -> None:
    """Suite 7: Pass 1 domain term correction, Pass 2 summary + chat cross-link, and downloads."""
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

    ChatService(db).post_message(
        session_id=sess.id,
        author_name="Ohad Ben Nun",
        category="Question",
        body="Why prefer Bridge Pattern over inheritance here?",
    )

    sio = socketio.test_client(lecture_app)
    sio.emit("join_session", {"session_code": "LEC7A"})
    sio.get_received()

    resp = client.post(
        f"/api/sessions/{sess.id}/lectures/summarize",
        json={"use_sample": True},
    )
    assert resp.status_code == 201
    data = resp.get_json()
    assert data["status"] == "COMPLETED"

    joined_corrected = " ".join(seg["text"] for seg in data["corrected_transcript"])
    assert "SOLID Principles" in joined_corrected
    assert "Bridge Pattern" in joined_corrected
    assert "O(1)" in joined_corrected
    assert "pytest" in joined_corrected
    assert data["corrected_transcript"][0]["offset_mmss"] == "00:15"

    assert "Ohad Ben Nun" in data["summary_markdown"]
    assert "Why prefer Bridge Pattern over inheritance here?" in data["summary_markdown"]

    events = [e["name"] for e in sio.get_received()]
    assert "lecture:summary_ready" in events
    sio.disconnect()

    course_lecs = client.get(f"/api/courses/{course.id}/lectures")
    assert course_lecs.status_code == 200
    assert len(course_lecs.get_json()["artifacts"]) == 1

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


def test_vtt_cue_parsing_and_chunked_pass1(tmp_path: Path) -> None:
    """Verify WebVTT cue timestamp parsing and >80-segment chunked Pass-1 correction."""
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
    finally:
        app.extensions["task_manager"].shutdown(wait=True)
        app.extensions["db_engine"].dispose()


def test_upload_recording_transcribed_and_summarized_by_gemini_api(tmp_path: Path) -> None:
    """Upload class recording -> Google Gemini Files API transcription -> Gemini summary + download."""
    uploaded_names: list[str] = []
    deleted_names: list[str] = []

    class FakeFilesAPI:
        def upload(self, *, file: str, config=None):
            uploaded_names.append(file)
            return SimpleNamespace(name="files/lecture-audio-1", state=SimpleNamespace(name="ACTIVE"))

        def get(self, *, name: str, config=None):
            return SimpleNamespace(name=name, state=SimpleNamespace(name="ACTIVE"))

        def delete(self, *, name: str, config=None):
            deleted_names.append(name)
            return SimpleNamespace()

    class FakeAudioGenAIModels:
        def generate_content(self, **kwargs):
            contents = kwargs.get("contents")
            # Multimodal call [uploaded_file, prompt] -> returns [MM:SS] audio transcript
            if isinstance(contents, list):
                return SimpleNamespace(
                    text=(
                        "[00:10] Welcome to Software Engineering at Ariel University.\n"
                        "[01:20] Today we cover Dependency Inversion and Bridge Pattern."
                    )
                )
            prompt_str = str(contents)
            if "Raw ASR Transcript:" in prompt_str:
                return SimpleNamespace(
                    text=(
                        "[00:10] Welcome to Software Engineering at Ariel University.\n"
                        "[01:20] Today we cover Dependency Inversion and Bridge Pattern."
                    )
                )
            return SimpleNamespace(
                text="### Executive Summary (TL;DR)\nLecturer summary covering Dependency Inversion and Bridge Pattern."
            )

    fake_genai = SimpleNamespace(files=FakeFilesAPI(), models=FakeAudioGenAIModels())
    gemini_client = GeminiStudioClient(
        api_key="fake-gemini-key",
        min_interval_sec=0.0,
        client_override=fake_genai,
    )

    db_file = tmp_path / "test_gemini_audio.db"
    cfg = Config(
        SECRET_KEY="gemini-audio-secret",
        DATABASE_URL=f"sqlite:///{db_file.as_posix()}",
        GEMINI_API_KEY="fake-gemini-key",
    )
    app = create_app(cfg, face_detector=StubDetector(), llm_client=gemini_client)
    app.config["TESTING"] = True

    try:
        client = app.test_client()
        db = app.extensions["db_session"]

        st = client.get("/api/integrations/status").get_json()
        assert st["gemini_configured"] is True
        assert "zoom_configured" not in st

        course = Course(
            course_code="SE301",
            name="Software Architecture",
            department="CS",
            lecturer_name="Prof. Boaz",
        )
        db.add(course)
        db.flush()
        sess = ClassSession(course_id=course.id, session_code="GEM1", title="Lecture 1 — Gemini Audio")
        db.add(sess)
        db.commit()

        resp = client.post(
            f"/api/sessions/{sess.id}/lectures/summarize",
            data={"media": (io.BytesIO(b"ID3fake-mp3-audio-bytes"), "lecture1.mp3")},
            content_type="multipart/form-data",
        )
        assert resp.status_code == 201
        payload = resp.get_json()
        assert payload["status"] == "COMPLETED"
        assert "Dependency Inversion" in payload["summary_markdown"]
        assert payload["corrected_transcript"][0]["offset_mmss"] == "00:10"
        assert len(uploaded_names) == 1
        assert deleted_names == ["files/lecture-audio-1"]

        # Also verify async=1 returns 202 and completes in AsyncTaskManager
        async_resp = client.post(
            f"/api/sessions/{sess.id}/lectures/summarize?async=1",
            data={"media": (io.BytesIO(b"ID3fake-mp3-audio-bytes-2"), "lecture2.mp3")},
            content_type="multipart/form-data",
        )
        assert async_resp.status_code == 202
        assert async_resp.get_json()["job_id"]
        app.extensions["task_manager"].shutdown(wait=True)

        all_arts = client.get(f"/api/sessions/{sess.id}/lectures").get_json()["artifacts"]
        assert len(all_arts) == 2
        assert all(a["status"] == "COMPLETED" for a in all_arts)
    finally:
        app.extensions["task_manager"].shutdown(wait=True)
        app.extensions["db_engine"].dispose()
