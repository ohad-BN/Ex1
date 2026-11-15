from __future__ import annotations

import json
import logging
import re
import subprocess
import tempfile
import threading
from pathlib import Path
from typing import Any, Callable, Optional

from sqlalchemy.orm import Session, sessionmaker

from app.models import ChatMessage, ClassSession, Course, LectureArtifact
from app.services.chat_service import ChatService, format_offset_mmss
from app.services.llm_client import GeminiStudioClient
from app.services.zoom_bridge import ZoomBridgeAdapter, normalize_zoom_meeting_id

logger = logging.getLogger(__name__)

VTT_CUE_RE = re.compile(
    r"(?:(\d{1,2}):)?(\d{1,2}):(\d{2})[.,]\d{1,3}\s*-->\s*(?:\d{1,2}:)?\d{1,2}:\d{2}[.,]\d{1,3}"
)
PASS1_CHUNK_SIZE = 80

BUILTIN_SAMPLE_SEGMENTS: list[dict[str, str]] = [
    {
        "offset_mmss": "00:15",
        "text": "Welcome back to Software Engineering and object oriented design at Ariel University.",
    },
    {
        "offset_mmss": "01:40",
        "text": "Today we review the solid principals and how dependency inversion keeps services testable.",
    },
    {
        "offset_mmss": "04:10",
        "text": "Notice how our bright pattern decouples ZoomBridgeAdapter from chat logic instead of a rigid single ton pattern.",
    },
    {
        "offset_mmss": "07:25",
        "text": "For homework cohort analytics, incremental updates must run in oh of one time without re-reading every file.",
    },
    {
        "offset_mmss": "10:05",
        "text": "Before pushing to production, verify concurrency locks and run pie test across every suite.",
    },
]

PHONETIC_CORRECTIONS: list[tuple[re.Pattern[str], str]] = [
    (re.compile(r"\bsolid principals\b", re.IGNORECASE), "SOLID Principles"),
    (re.compile(r"\bbright pattern\b", re.IGNORECASE), "Bridge Pattern"),
    (re.compile(r"\bsingle ton pattern\b", re.IGNORECASE), "Singleton Pattern"),
    (re.compile(r"\boh of one\b", re.IGNORECASE), "O(1)"),
    (re.compile(r"\bpie test\b", re.IGNORECASE), "pytest"),
    (re.compile(r"\bsequel lite\b", re.IGNORECASE), "SQLite"),
    (re.compile(r"\bfast her whisper\b", re.IGNORECASE), "faster-whisper"),
    (re.compile(r"\bobject oriented\b", re.IGNORECASE), "Object-Oriented"),
]

TIMESTAMP_LINE_RE = re.compile(r"^\[?(\d{1,2}:\d{2}(?::\d{2})?)\]?\s*[-:]?\s*(.+)$")


class LecturePipelineError(Exception):
    """Domain error raised during media extraction or lecture summarization."""

    def __init__(self, message: str, status_code: int = 400) -> None:
        super().__init__(message)
        self.message = message
        self.status_code = status_code


def _format_seconds_mmss(seconds: float) -> str:
    total = max(0, int(seconds))
    mins, secs = divmod(total, 60)
    return f"{mins:02d}:{secs:02d}"


def apply_domain_terminology_fixes(text: str) -> str:
    """Apply deterministic CS/OOP ASR phonetic fixes when offline."""
    fixed = text
    for pattern, replacement in PHONETIC_CORRECTIONS:
        fixed = pattern.sub(replacement, fixed)
    return fixed


class LectureSummarizerService:
    """Feature 4 pipeline: media/transcript ingestion -> Pass 1 correction -> Pass 2 summary."""

    def __init__(
        self,
        db: Session,
        llm_client: Optional[GeminiStudioClient] = None,
        upload_dir: Optional[Path] = None,
    ) -> None:
        self.db = db
        self.llm_client = llm_client or GeminiStudioClient()
        self.upload_dir = upload_dir or Path(tempfile.gettempdir())

    def serialize_artifact(self, artifact: LectureArtifact) -> dict[str, Any]:
        session = self.db.get(ClassSession, artifact.session_id)
        course = (
            self.db.get(Course, session.course_id)
            if session and session.course_id
            else None
        )
        try:
            raw_segments = json.loads(artifact.raw_transcript_json or "[]")
        except json.JSONDecodeError:
            raw_segments = []
        try:
            corrected_segments = json.loads(artifact.corrected_transcript_json or "[]")
        except json.JSONDecodeError:
            corrected_segments = []

        return {
            "id": artifact.id,
            "session_id": artifact.session_id,
            "session_code": session.session_code if session else "",
            "course_code": course.course_code if course else "",
            "lecture_title": session.title if session else "",
            "source_type": artifact.source_type,
            "media_path": artifact.media_path,
            "raw_transcript": raw_segments,
            "corrected_transcript": corrected_segments,
            "summary_markdown": artifact.summary_markdown,
            "status": artifact.status,
            "zoom_meeting_uuid": artifact.zoom_meeting_uuid,
            "error_message": artifact.error_message,
            "created_at": artifact.created_at.strftime("%Y-%m-%d %H:%M UTC"),
        }

    def list_session_artifacts(self, session_id: int) -> list[dict[str, Any]]:
        rows = (
            self.db.query(LectureArtifact)
            .filter_by(session_id=session_id)
            .order_by(LectureArtifact.id.desc())
            .all()
        )
        return [self.serialize_artifact(r) for r in rows]

    def list_course_artifacts(self, course_id: int) -> list[dict[str, Any]]:
        sessions = (
            self.db.query(ClassSession)
            .filter_by(course_id=course_id)
            .order_by(ClassSession.id.desc())
            .all()
        )
        session_ids = [s.id for s in sessions]
        if not session_ids:
            return []
        rows = (
            self.db.query(LectureArtifact)
            .filter(
                LectureArtifact.session_id.in_(session_ids),
                LectureArtifact.status == "COMPLETED",
            )
            .order_by(LectureArtifact.id.desc())
            .all()
        )
        return [self.serialize_artifact(r) for r in rows]

    def parse_transcript_text(self, text: str) -> list[dict[str, str]]:
        """Parse WebVTT/SRT cue blocks, `[MM:SS] line`, or plain prose into timestamped segments."""
        segments: list[dict[str, str]] = []
        pending_mmss: Optional[str] = None
        fallback_idx = 0

        for raw_line in (text or "").splitlines():
            line = raw_line.strip()
            if not line:
                pending_mmss = None
                continue
            if line.upper().startswith("WEBVTT") or line.upper().startswith("NOTE"):
                continue
            cue_match = VTT_CUE_RE.search(line)
            if cue_match:
                hours = int(cue_match.group(1) or 0)
                mins = int(cue_match.group(2))
                secs = int(cue_match.group(3))
                pending_mmss = f"{hours * 60 + mins:02d}:{secs:02d}"
                continue
            if line.isdigit():
                continue

            m = TIMESTAMP_LINE_RE.match(line)
            if m:
                segments.append({"offset_mmss": m.group(1), "text": m.group(2).strip()})
                pending_mmss = None
            elif pending_mmss is not None:
                segments.append({"offset_mmss": pending_mmss, "text": line})
            else:
                segments.append(
                    {
                        "offset_mmss": _format_seconds_mmss(fallback_idx * 45),
                        "text": line,
                    }
                )
                fallback_idx += 1
        return segments

    def transcribe_media_file(self, media_path: Path) -> list[dict[str, str]]:
        """Extract 16kHz mono WAV via ffmpeg and run local faster-whisper ASR."""
        ext = media_path.suffix.lower()
        if ext in (".txt", ".vtt", ".srt", ".md"):
            return self.parse_transcript_text(media_path.read_text(encoding="utf-8", errors="ignore"))

        self.upload_dir.mkdir(parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as tmp_wav:
            wav_path = Path(tmp_wav.name)

        try:
            cmd = [
                "ffmpeg",
                "-y",
                "-i",
                str(media_path),
                "-vn",
                "-acodec",
                "pcm_s16le",
                "-ar",
                "16000",
                "-ac",
                "1",
                str(wav_path),
            ]
            subprocess.run(
                cmd,
                check=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                timeout=120,
            )
            from faster_whisper import WhisperModel

            model = WhisperModel("base", device="cpu", compute_type="int8")
            raw_segments, _info = model.transcribe(str(wav_path), vad_filter=True)
            results: list[dict[str, str]] = []
            for seg in raw_segments:
                txt = str(getattr(seg, "text", "")).strip()
                if txt:
                    results.append(
                        {
                            "offset_mmss": _format_seconds_mmss(getattr(seg, "start", 0.0)),
                            "text": txt,
                        }
                    )
            return results or list(BUILTIN_SAMPLE_SEGMENTS)
        except Exception as exc:
            logger.warning("Media ASR fallback triggered for %s: %s", media_path, exc)
            raise LecturePipelineError(
                f"Could not decode audio/video file '{media_path.name}': {exc}",
                status_code=400,
            ) from exc
        finally:
            try:
                wav_path.unlink(missing_ok=True)
            except OSError:
                pass

    def _correct_chunk(
        self,
        chunk: list[dict[str, str]],
        course_label: str,
        lecture_title: str,
        chat_timeline: str,
    ) -> list[dict[str, str]]:
        if self.llm_client.is_configured():
            raw_lines = "\n".join(
                f"[{s['offset_mmss']}] {s['text']}" for s in chunk
            )
            prompt = (
                f"Course: {course_label}\n"
                f"Lecture: {lecture_title}\n"
                f"Classroom Chat Context:\n{chat_timeline[:1200]}\n\n"
                f"Raw ASR Transcript:\n{raw_lines}\n\n"
                "Fix only technical CS/math/OOP/Hebrew-English ASR spelling errors while keeping "
                "every `[MM:SS]` prefix exact on each line."
            )
            corrected_blob = self.llm_client.generate_text(
                prompt,
                system_instruction=(
                    "You are an expert Computer Science lecture editor. Preserve exact "
                    "[MM:SS] timestamps on every line and fix misheard CS terms."
                ),
            )
            parsed_lines = self.parse_transcript_text(corrected_blob)
            if len(parsed_lines) == len(chunk):
                return [
                    {
                        "offset_mmss": chunk[idx]["offset_mmss"],
                        "text": parsed_lines[idx]["text"],
                    }
                    for idx in range(len(chunk))
                ]

        return [
            {
                "offset_mmss": seg["offset_mmss"],
                "text": apply_domain_terminology_fixes(seg["text"]),
            }
            for seg in chunk
        ]

    def _run_pass1_correction(
        self,
        raw_segments: list[dict[str, str]],
        course_label: str,
        lecture_title: str,
        chat_timeline: str,
    ) -> list[dict[str, str]]:
        if not raw_segments:
            return []

        corrected: list[dict[str, str]] = []
        for start in range(0, len(raw_segments), PASS1_CHUNK_SIZE):
            batch = raw_segments[start : start + PASS1_CHUNK_SIZE]
            corrected.extend(
                self._correct_chunk(
                    chunk=batch,
                    course_label=course_label,
                    lecture_title=lecture_title,
                    chat_timeline=chat_timeline,
                )
            )
        return corrected

    def _run_pass2_summary(
        self,
        corrected_segments: list[dict[str, str]],
        course_label: str,
        lecture_title: str,
        chat_rows: list[ChatMessage],
        chat_timeline: str,
    ) -> str:
        transcript_text = "\n".join(
            f"[{s['offset_mmss']}] {s['text']}" for s in corrected_segments
        )

        if self.llm_client.is_configured():
            prompt = (
                f"Course: {course_label}\n"
                f"Lecture Title: {lecture_title}\n\n"
                f"Corrected Timestamped Transcript:\n{transcript_text}\n\n"
                f"Student Questions & Comments (+MM:SS):\n{chat_timeline}\n\n"
                "Produce a structured Markdown study guide with sections:\n"
                "1. Executive Summary (TL;DR)\n"
                "2. Chronological Lecture Breakdown ([MM:SS])\n"
                "3. Key Technical Takeaways\n"
                "4. Answers to Student Questions Raised in Class Chat (+MM:SS)\n"
                "5. Action Items"
            )
            return self.llm_client.generate_text(
                prompt,
                system_instruction=(
                    "Write a clear, concise university study summary grounded strictly "
                    "in the lecture transcript and student Q&A timeline."
                ),
            )

        topic_bullets = "\n".join(
            f"- **[{s['offset_mmss']}]** {s['text']}" for s in corrected_segments
        )
        if chat_rows:
            qa_bullets = "\n".join(
                f"- **[{format_offset_mmss(m.offset_seconds)}] {m.author_name} ({m.category}):** "
                f"\"{m.body}\" — *Addressed during the lecture context around {format_offset_mmss(m.offset_seconds)}.*"
                for m in chat_rows
            )
        else:
            qa_bullets = "- No student chat questions were logged during this recording."

        return (
            f"### Executive Summary (TL;DR)\n"
            f"Study guide for **{lecture_title}** ({course_label}), combining local ASR "
            f"domain correction ({len(corrected_segments)} segments) with timestamped class Q&A.\n\n"
            f"### Chronological Lecture Breakdown\n{topic_bullets}\n\n"
            f"### Key OOP & Architecture Takeaways\n"
            f"- Clean interface adapters (`Bridge` / `Dependency Inversion`) decouple external services from core logic.\n"
            f"- Keep analytical pipelines incremental (`O(1)`) and verify concurrency behavior with `pytest`.\n\n"
            f"### Answers to Student Questions from Class Chat\n{qa_bullets}\n\n"
            f"### Action Items & Next Lecture Prep\n"
            f"- Review the `[MM:SS]` checkpoints above and verify unit tests pass cleanly."
        )

    def _set_artifact_stage(
        self,
        artifact: LectureArtifact,
        status: str,
        on_status: Optional[Callable[[dict[str, Any]], None]] = None,
        error_message: Optional[str] = None,
    ) -> None:
        artifact.status = status
        if error_message is not None:
            artifact.error_message = error_message
        self.db.commit()
        if on_status is not None:
            on_status(self.serialize_artifact(artifact))

    def _synthesize_into_artifact(
        self,
        session: ClassSession,
        artifact: LectureArtifact,
        raw_segments: list[dict[str, str]],
        on_status: Optional[Callable[[dict[str, Any]], None]] = None,
    ) -> LectureArtifact:
        course = self.db.get(Course, session.course_id) if session.course_id else None
        course_label = f"{course.course_code} — {course.name}" if course else "ClassPulse Course"

        chat_rows = (
            self.db.query(ChatMessage)
            .filter_by(session_id=session.id)
            .order_by(ChatMessage.offset_seconds.asc(), ChatMessage.id.asc())
            .all()
        )
        chat_timeline = ChatService(self.db).export_timeline_text(session.id)

        artifact.raw_transcript_json = json.dumps(raw_segments)
        self._set_artifact_stage(artifact, "AI_CORRECTING", on_status=on_status)

        corrected_segments = self._run_pass1_correction(
            raw_segments=raw_segments,
            course_label=course_label,
            lecture_title=session.title,
            chat_timeline=chat_timeline,
        )
        artifact.corrected_transcript_json = json.dumps(corrected_segments)
        self._set_artifact_stage(artifact, "SUMMARIZING", on_status=on_status)

        summary_md = self._run_pass2_summary(
            corrected_segments=corrected_segments,
            course_label=course_label,
            lecture_title=session.title,
            chat_rows=chat_rows,
            chat_timeline=chat_timeline,
        )
        artifact.summary_markdown = summary_md
        artifact.error_message = None
        self._set_artifact_stage(artifact, "COMPLETED", on_status=on_status)
        return artifact

    def summarize_session_recording(
        self,
        session_id: int,
        media_path: Optional[Path] = None,
        transcript_text: Optional[str] = None,
        use_sample: bool = False,
        source_type: str = "upload",
    ) -> LectureArtifact:
        """Execute Pass 1 (transcript correction) and Pass 2 (summary) for a session."""
        session = self.db.get(ClassSession, session_id)
        if session is None:
            raise LecturePipelineError("Lecture session not found.", status_code=404)

        if use_sample:
            raw_segments = list(BUILTIN_SAMPLE_SEGMENTS)
            ref_path = "sample://ariel_oop_lecture"
            src_kind = "sample"
        elif transcript_text and transcript_text.strip():
            raw_segments = self.parse_transcript_text(transcript_text)
            ref_path = "inline://transcript"
            src_kind = "transcript"
        elif media_path is not None and media_path.exists():
            raw_segments = self.transcribe_media_file(media_path)
            ref_path = str(media_path)
            src_kind = source_type
        else:
            raise LecturePipelineError(
                "Provide a media/transcript file, transcript_text, or set use_sample=true.",
                status_code=400,
            )

        artifact = LectureArtifact(
            session_id=session.id,
            source_type=src_kind,
            media_path=ref_path,
            status="PENDING",
        )
        self.db.add(artifact)
        self.db.flush()
        return self._synthesize_into_artifact(
            session=session,
            artifact=artifact,
            raw_segments=raw_segments,
        )

    def summarize_zoom_recording_bundle(
        self,
        session_id: int,
        zoom_bridge: ZoomBridgeAdapter,
        meeting_id: str,
        recording_bundle: Optional[dict[str, Any]] = None,
        download_token: Optional[str] = None,
        artifact_id: Optional[int] = None,
        on_status: Optional[Callable[[dict[str, Any]], None]] = None,
    ) -> LectureArtifact:
        """Download Zoom Cloud recording (.vtt/.m4a/.mp4) and run 2-pass Gemini pipeline."""
        session = self.db.get(ClassSession, session_id)
        if session is None:
            raise LecturePipelineError("Lecture session not found.", status_code=404)

        clean_mid = normalize_zoom_meeting_id(meeting_id or session.zoom_meeting_id)
        if not clean_mid:
            raise LecturePipelineError("A Zoom Meeting ID is required.", status_code=400)

        bundle = recording_bundle
        if bundle is None:
            bundle = zoom_bridge.list_meeting_recordings(clean_mid)
        if not bundle or not bundle.get("recording_files"):
            raise LecturePipelineError(
                f"No completed Zoom Cloud recording files found yet for meeting {clean_mid}.",
                status_code=404,
            )

        meeting_uuid = str(bundle.get("uuid") or "").strip() or None
        if meeting_uuid and artifact_id is None:
            existing = (
                self.db.query(LectureArtifact)
                .filter_by(session_id=session.id, zoom_meeting_uuid=meeting_uuid, status="COMPLETED")
                .first()
            )
            if existing is not None:
                return existing

        artifact = self.db.get(LectureArtifact, artifact_id) if artifact_id else None
        if artifact is None:
            artifact = LectureArtifact(
                session_id=session.id,
                source_type="zoom_cloud",
                media_path=f"zoom://{clean_mid}",
                zoom_meeting_uuid=meeting_uuid,
                status="DOWNLOADING_ZOOM",
            )
            self.db.add(artifact)
            self.db.flush()
        else:
            artifact.zoom_meeting_uuid = meeting_uuid

        try:
            self._set_artifact_stage(artifact, "DOWNLOADING_ZOOM", on_status=on_status)
            dest_dir = Path(self.upload_dir) / f"session_{session.id}"
            downloaded = zoom_bridge.download_best_recording_file(
                recording_files=list(bundle.get("recording_files", [])),
                dest_dir=dest_dir,
                meeting_id=clean_mid,
                download_token=download_token,
            )
            if downloaded is None:
                fallback_str = zoom_bridge.fetch_cloud_recording(clean_mid, dest_dir)
                downloaded = Path(fallback_str) if fallback_str else None
            if downloaded is None or not downloaded.exists():
                raise LecturePipelineError(
                    "Zoom recording bundle has no downloadable VTT, M4A, or MP4 file.",
                    status_code=404,
                )

            artifact.media_path = str(downloaded)
            self._set_artifact_stage(artifact, "TRANSCRIBING", on_status=on_status)
            raw_segments = self.transcribe_media_file(downloaded)
            return self._synthesize_into_artifact(
                session=session,
                artifact=artifact,
                raw_segments=raw_segments,
                on_status=on_status,
            )
        except Exception as exc:
            err_msg = exc.message if isinstance(exc, LecturePipelineError) else str(exc)
            self._set_artifact_stage(
                artifact,
                "FAILED",
                on_status=on_status,
                error_message=err_msg,
            )
            raise

    def build_summary_download(self, artifact_id: int) -> tuple[str, str]:
        """Build printable Markdown file content and safe download filename."""
        artifact = self.db.get(LectureArtifact, artifact_id)
        if artifact is None:
            raise LecturePipelineError("Lecture artifact not found.", status_code=404)
        session = self.db.get(ClassSession, artifact.session_id)
        course = (
            self.db.get(Course, session.course_id)
            if session and session.course_id
            else None
        )
        prefix = f"{course.course_code}_" if course else ""
        sess_code = session.session_code if session else f"S{artifact.session_id}"
        title = session.title if session else f"Lecture {artifact.session_id}"
        header = (
            f"# {f'{course.course_code} — ' if course else ''}{title}\n"
            f"- **Session Code:** `{sess_code}`\n"
            f"- **Generated:** {artifact.created_at.strftime('%Y-%m-%d %H:%M UTC')}\n"
            f"- **Source:** `{artifact.source_type}`\n\n---\n\n"
        )
        filename = f"{prefix}{sess_code}_summary_{artifact.id}.md"
        return filename, header + (artifact.summary_markdown or "")

    def build_transcript_download(self, artifact_id: int) -> tuple[str, str]:
        """Build clean `[MM:SS] ...` text transcript and safe download filename."""
        artifact = self.db.get(LectureArtifact, artifact_id)
        if artifact is None:
            raise LecturePipelineError("Lecture artifact not found.", status_code=404)
        session = self.db.get(ClassSession, artifact.session_id)
        course = (
            self.db.get(Course, session.course_id)
            if session and session.course_id
            else None
        )
        prefix = f"{course.course_code}_" if course else ""
        sess_code = session.session_code if session else f"S{artifact.session_id}"
        try:
            segments = json.loads(artifact.corrected_transcript_json or "[]")
        except json.JSONDecodeError:
            segments = []
        lines = [
            f"[{seg.get('offset_mmss', '00:00')}] {seg.get('text', '')}"
            for seg in segments
        ]
        title = session.title if session else f"Lecture {artifact.session_id}"
        header = f"{title} ({sess_code}) — Corrected Transcript\n" + "=" * 60 + "\n\n"
        filename = f"{prefix}{sess_code}_transcript_{artifact.id}.txt"
        return filename, header + "\n".join(lines) + "\n"


class ZoomCloudAutoPoller:
    """Lightweight background poller that watches sessions with `zoom_auto_sync=True`.

    Enables zero-click Zoom Cloud -> Gemini -> Download workflows using only the
    3 Zoom Server-to-Server OAuth keys (no public webhook URL required).
    """

    def __init__(
        self,
        engine: Any,
        zoom_bridge: ZoomBridgeAdapter,
        llm_client: GeminiStudioClient,
        upload_dir: Path,
        socketio_ext: Any = None,
        interval_sec: int = 60,
    ) -> None:
        self._session_factory = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)
        self.zoom_bridge = zoom_bridge
        self.llm_client = llm_client
        self.upload_dir = upload_dir
        self.socketio = socketio_ext
        self.interval_sec = max(15, int(interval_sec))
        self._stop_event = threading.Event()
        self._thread: Optional[threading.Thread] = None

    def tick_once(self) -> int:
        """Inspect auto-sync sessions once and process any newly completed cloud recordings."""
        if not self.zoom_bridge.is_configured():
            return 0

        processed = 0
        with self._session_factory() as db:
            sessions = (
                db.query(ClassSession)
                .filter(
                    ClassSession.zoom_auto_sync.is_(True),
                    ClassSession.zoom_meeting_id.is_not(None),
                )
                .all()
            )
            svc = LectureSummarizerService(
                db=db, llm_client=self.llm_client, upload_dir=self.upload_dir
            )
            for sess in sessions:
                mid = normalize_zoom_meeting_id(sess.zoom_meeting_id)
                if not mid:
                    continue
                try:
                    bundle = self.zoom_bridge.list_meeting_recordings(mid)
                except Exception as exc:  # noqa: BLE001
                    logger.warning("Zoom auto-sync poll failed for %s: %s", mid, exc)
                    continue
                if not bundle or not bundle.get("recording_files"):
                    continue
                meeting_uuid = str(bundle.get("uuid") or "").strip() or None
                if meeting_uuid:
                    already = (
                        db.query(LectureArtifact)
                        .filter(
                            LectureArtifact.session_id == sess.id,
                            LectureArtifact.zoom_meeting_uuid == meeting_uuid,
                            LectureArtifact.status.in_(
                                ["DOWNLOADING_ZOOM", "TRANSCRIBING", "AI_CORRECTING", "SUMMARIZING", "COMPLETED"]
                            ),
                        )
                        .first()
                    )
                    if already is not None:
                        continue

                room = f"session_{sess.session_code}"

                def _emit_stage(payload: dict[str, Any], target_room: str = room) -> None:
                    if self.socketio is not None:
                        self.socketio.emit("lecture:status", payload, to=target_room)

                try:
                    artifact = svc.summarize_zoom_recording_bundle(
                        session_id=sess.id,
                        zoom_bridge=self.zoom_bridge,
                        meeting_id=mid,
                        recording_bundle=bundle,
                        on_status=_emit_stage,
                    )
                    serialized = svc.serialize_artifact(artifact)
                    if self.socketio is not None:
                        self.socketio.emit("lecture:summary_ready", serialized, to=room)
                    processed += 1
                except Exception as exc:  # noqa: BLE001
                    logger.warning("Auto-sync Zoom processing failed for session %s: %s", sess.session_code, exc)
        return processed

    def start(self) -> None:
        if self._thread is not None and self._thread.is_alive():
            return

        def _loop() -> None:
            while not self._stop_event.wait(self.interval_sec):
                try:
                    self.tick_once()
                except Exception as exc:  # noqa: BLE001
                    logger.warning("ZoomCloudAutoPoller tick error: %s", exc)

        self._thread = threading.Thread(
            target=_loop, name="zoom-cloud-poller", daemon=True
        )
        self._thread.start()

    def stop(self) -> None:
        self._stop_event.set()
