from __future__ import annotations

import json
import logging
import os
import re
import subprocess
import tempfile
import xml.etree.ElementTree as ET
import zipfile
from pathlib import Path
from typing import Any, Callable, Optional

from sqlalchemy.orm import Session

from app.models import ChatMessage, ClassSession, Course, LectureArtifact, utcnow
from app.services.chat_service import ChatService, format_offset_mmss
from app.services.llm_client import GeminiStudioClient

logger = logging.getLogger(__name__)

VTT_CUE_RE = re.compile(
    r"(?:(\d{1,2}):)?(\d{1,2}):(\d{2})[.,]\d{1,3}\s*-->\s*(?:\d{1,2}:)?\d{1,2}:\d{2}[.,]\d{1,3}"
)
PASS1_CHUNK_SIZE = 80
DIRECT_GEMINI_AUDIO_EXTS = {".mp3", ".m4a", ".wav", ".ogg", ".flac", ".aac"}
VIDEO_MEDIA_EXTS = {".mp4", ".mov", ".webm", ".mkv", ".avi", ".m4v", ".flv", ".3gp"}
GEMINI_DIRECT_MAX_BYTES = 18 * 1024 * 1024

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
        "text": "Notice how our bright pattern decouples ReportAdapter from chat logic instead of a rigid single ton pattern.",
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

    def parse_summary_sections(self, summary_markdown: str) -> dict[str, Any]:
        """Parse summary markdown headings into structured UI sections cleanly."""
        buckets: dict[str, Any] = {
            "executive_summary": [],
            "key_topics": [],
            "action_items": [],
            "extra_sections": [],
        }
        raw = (summary_markdown or "").strip()
        if not raw:
            return buckets

        sections: list[tuple[str, list[str]]] = []
        current_title = ""
        current_lines: list[str] = []
        for raw_line in raw.splitlines():
            line = raw_line.strip()
            if not line:
                continue
            heading_match = re.match(r"^#{1,4}\s+(.+)$", line)
            if heading_match:
                if current_title or current_lines:
                    sections.append((current_title, current_lines))
                current_title = heading_match.group(1).strip()
                current_lines = []
            else:
                cleaned = re.sub(r"^[-*•]\s+", "", line)
                cleaned = re.sub(r"^\d+\.\s+", "", cleaned)
                if cleaned:
                    current_lines.append(cleaned)
        if current_title or current_lines:
            sections.append((current_title, current_lines))

        for title, items in sections:
            if not items:
                continue
            lower_t = title.lower()
            if not title or any(k in lower_t for k in ("executive", "tl;dr", "tldr", "overview", "summary")):
                buckets["executive_summary"].extend(items)
            elif any(k in lower_t for k in ("topic", "takeaway", "breakdown", "chronological", "architecture", "oop", "concept", "finding")):
                buckets["key_topics"].extend(items)
            elif any(k in lower_t for k in ("action", "question", "q&a", "prep", "next", "follow")):
                buckets["action_items"].extend(items)
            else:
                buckets["extra_sections"].append({"title": title, "items": items})

        if not any((buckets["executive_summary"], buckets["key_topics"], buckets["action_items"])):
            all_lines = [it for _t, its in sections for it in its]
            if all_lines:
                buckets["executive_summary"] = all_lines[:2]
                if len(all_lines) > 2:
                    buckets["key_topics"] = all_lines[2:]
        return buckets

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
            "course_id": course.id if course else None,
            "course_code": course.course_code if course else "",
            "course_name": course.name if course else "",
            "lecture_number": session.lecture_number if session else None,
            "lecture_title": session.title if session else "",
            "source_type": artifact.source_type,
            "media_path": artifact.media_path,
            "raw_transcript": raw_segments,
            "corrected_transcript": corrected_segments,
            "summary_markdown": artifact.summary_markdown,
            "structured_sections": self.parse_summary_sections(artifact.summary_markdown or ""),
            "status": artifact.status,
            "is_published": bool(artifact.is_published),
            "published_at": (
                artifact.published_at.strftime("%Y-%m-%d %H:%M UTC")
                if artifact.published_at
                else None
            ),
            "error_message": artifact.error_message,
            "created_at": artifact.created_at.strftime("%Y-%m-%d %H:%M UTC"),
        }

    def list_session_artifacts(
        self, session_id: int, *, published_only: bool = False
    ) -> list[dict[str, Any]]:
        query = self.db.query(LectureArtifact).filter_by(session_id=session_id)
        if published_only:
            query = query.filter(
                LectureArtifact.status == "COMPLETED",
                LectureArtifact.is_published.is_(True),
            )
        rows = query.order_by(LectureArtifact.id.desc()).all()
        return [self.serialize_artifact(r) for r in rows]

    def list_course_artifacts(
        self, course_id: int, *, published_only: bool = False
    ) -> list[dict[str, Any]]:
        sessions = (
            self.db.query(ClassSession)
            .filter_by(course_id=course_id)
            .order_by(ClassSession.lecture_number.asc(), ClassSession.id.asc())
            .all()
        )
        session_order = {
            s.id: (
                int(s.lecture_number) if s.lecture_number is not None else 10**9,
                s.id,
            )
            for s in sessions
        }
        session_ids = [s.id for s in sessions]
        if not session_ids:
            return []
        query = self.db.query(LectureArtifact).filter(
            LectureArtifact.session_id.in_(session_ids),
            LectureArtifact.status == "COMPLETED",
        )
        if published_only:
            query = query.filter(LectureArtifact.is_published.is_(True))
        rows = query.order_by(LectureArtifact.id.desc()).all()
        rows.sort(key=lambda r: (session_order.get(r.session_id, (10**9, 10**9)), -r.id))
        return [self.serialize_artifact(r) for r in rows]

    def publish_artifact(self, artifact_id: int, publish: bool = True) -> LectureArtifact:
        """Toggle visibility of a completed lecture summary for enrolled students."""
        artifact = self.db.get(LectureArtifact, artifact_id)
        if artifact is None:
            raise LecturePipelineError("Lecture artifact not found.", status_code=404)
        if publish and artifact.status != "COMPLETED":
            raise LecturePipelineError(
                "Only completed lecture summaries can be published to students.",
                status_code=400,
            )
        artifact.is_published = bool(publish)
        artifact.published_at = utcnow() if publish else None
        self.db.commit()
        return artifact

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

    def _transcribe_with_gemini(self, media_path: Path) -> list[dict[str, str]]:
        """Send recording audio (or ffmpeg-extracted MP3 for videos) to Google Gemini API for [MM:SS] transcription."""
        ext = media_path.suffix.lower()
        temp_mp3: Optional[Path] = None
        upload_path = media_path
        try:
            needs_audio_extract = (
                ext in VIDEO_MEDIA_EXTS
                or ext not in DIRECT_GEMINI_AUDIO_EXTS
                or media_path.stat().st_size > GEMINI_DIRECT_MAX_BYTES
            )
            if needs_audio_extract:
                with tempfile.NamedTemporaryFile(suffix=".mp3", delete=False) as tmp_mp3:
                    temp_mp3 = Path(tmp_mp3.name)
                cmd = [
                    "ffmpeg",
                    "-y",
                    "-i",
                    str(media_path),
                    "-vn",
                    "-ac",
                    "1",
                    "-ar",
                    "16000",
                    "-b:a",
                    "64k",
                    str(temp_mp3),
                ]
                proc = subprocess.run(
                    cmd,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    timeout=300,
                )
                if proc.returncode == 0 and temp_mp3.exists() and temp_mp3.stat().st_size > 0:
                    upload_path = temp_mp3
                elif ext in VIDEO_MEDIA_EXTS:
                    stderr_snippet = proc.stderr.decode("utf-8", errors="ignore").strip()[-300:]
                    raise LecturePipelineError(
                        f"Could not extract audio track from video '{media_path.name}': {stderr_snippet or 'no audio stream found.'}",
                        status_code=400,
                    )

            prompt = (
                "Transcribe this university lecture recording completely and accurately.\n"
                "Format EVERY spoken paragraph or topic shift on its own line starting with an exact timestamp: "
                "`[MM:SS] <spoken text>`.\n"
                "Fix technical Computer Science, OOP, math, and Hebrew/English terms cleanly."
            )
            transcript_blob = self.llm_client.transcribe_audio_file(
                audio_path=upload_path,
                prompt=prompt,
                system_instruction=(
                    "You are an accurate university lecture transcription assistant. "
                    "Always output clean `[MM:SS] ...` timestamped lines."
                ),
            )
            segments = self.parse_transcript_text(transcript_blob)
            if not segments:
                raise LecturePipelineError("Gemini returned no transcript segments.", status_code=502)
            return segments
        finally:
            if temp_mp3 is not None:
                try:
                    temp_mp3.unlink(missing_ok=True)
                except OSError:
                    pass

    def _extract_document_text(self, media_path: Path) -> str:
        """Extract clean UTF-8 text from .md, .txt, .vtt, .srt, .pdf, or .docx files."""
        ext = media_path.suffix.lower()
        if ext in (".txt", ".vtt", ".srt", ".md"):
            return media_path.read_text(encoding="utf-8", errors="ignore").strip()
        if ext == ".pdf":
            from pypdf import PdfReader

            try:
                reader = PdfReader(str(media_path))
                return "\n".join(page.extract_text() or "" for page in reader.pages).strip()
            except Exception as exc:
                raise LecturePipelineError(f"Could not read PDF file: {exc}", status_code=400) from exc
        if ext == ".docx":
            try:
                with zipfile.ZipFile(str(media_path)) as zf:
                    doc_xml = zf.read("word/document.xml")
                root = ET.fromstring(doc_xml)
            except Exception as exc:
                raise LecturePipelineError("Could not read .docx file.", status_code=400) from exc
            w_ns = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"
            paragraphs: list[str] = []
            for p_node in root.iter(f"{w_ns}p"):
                texts = [t_node.text for t_node in p_node.iter(f"{w_ns}t") if t_node.text]
                line = "".join(texts).strip()
                if line:
                    paragraphs.append(line)
            return "\n".join(paragraphs).strip()
        return ""

    def transcribe_media_file(self, media_path: Path) -> list[dict[str, str]]:
        """Transcribe uploaded lecture media via Google Gemini API (with local faster-whisper fallback)."""
        segments, _used_gemini = self._transcribe_media_with_origin(media_path)
        return segments

    def _transcribe_media_with_origin(
        self, media_path: Path
    ) -> tuple[list[dict[str, str]], bool]:
        """Transcribe uploaded lecture media and report whether Gemini produced the transcript."""
        ext = media_path.suffix.lower()
        if ext in (".txt", ".vtt", ".srt", ".md", ".pdf", ".docx"):
            doc_text = self._extract_document_text(media_path)
            if not doc_text:
                raise LecturePipelineError(
                    f"The uploaded file '{media_path.name}' contained no readable text.",
                    status_code=400,
                )
            return self.parse_transcript_text(doc_text), False

        self.upload_dir.mkdir(parents=True, exist_ok=True)

        if self.llm_client.is_configured():
            try:
                return self._transcribe_with_gemini(media_path), True
            except Exception as exc:
                logger.warning(
                    "Gemini audio transcription failed for %s; falling back to local faster-whisper: %s",
                    media_path.name,
                    exc,
                )

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
            proc = subprocess.run(
                cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                timeout=300,
            )
            if proc.returncode != 0 or not wav_path.exists() or wav_path.stat().st_size == 0:
                stderr_msg = proc.stderr.decode("utf-8", errors="ignore").strip()[-300:]
                raise LecturePipelineError(
                    f"Audio extraction failed for '{media_path.name}': {stderr_msg or 'invalid media or missing audio track.'}",
                    status_code=400,
                )

            from faster_whisper import WhisperModel
            import wave
            import numpy as np

            with wave.open(str(wav_path), "rb") as wf:
                raw_pcm = wf.readframes(wf.getnframes())
            audio_np = np.frombuffer(raw_pcm, dtype=np.int16).astype(np.float32) / 32768.0
            if audio_np.size == 0:
                return (
                    [
                        {
                            "offset_mmss": "00:00",
                            "text": f"Uploaded lecture recording ({media_path.name}) processed; audio track is empty.",
                        }
                    ],
                    False,
                )

            preferred_whisper = os.getenv("CLASSPULSE_WHISPER_MODEL", "small").strip() or "small"
            try:
                model = WhisperModel(preferred_whisper, device="cpu", compute_type="int8")
            except Exception as model_exc:
                logger.warning(
                    "Whisper model '%s' failed to load (%s); falling back to 'base'.",
                    preferred_whisper,
                    model_exc,
                )
                model = WhisperModel("base", device="cpu", compute_type="int8")

            raw_segments, _info = model.transcribe(
                audio_np,
                vad_filter=True,
                beam_size=5,
                condition_on_previous_text=True,
            )
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
            if not results:
                raw_segments_novad, _info2 = model.transcribe(
                    audio_np,
                    vad_filter=False,
                    beam_size=5,
                    condition_on_previous_text=True,
                )
                for seg in raw_segments_novad:
                    txt = str(getattr(seg, "text", "")).strip()
                    if txt:
                        results.append(
                            {
                                "offset_mmss": _format_seconds_mmss(getattr(seg, "start", 0.0)),
                                "text": txt,
                            }
                        )
            return (
                results
                or [
                    {
                        "offset_mmss": "00:00",
                        "text": f"Uploaded lecture recording ({media_path.name}) processed; no clear speech detected in audio track.",
                    }
                ],
                False,
            )
        except LecturePipelineError:
            raise
        except Exception as exc:
            logger.warning("Media ASR fallback triggered for %s: %s", media_path, exc)
            raise LecturePipelineError(
                f"Could not transcribe audio/video file '{media_path.name}': {exc}",
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
            try:
                corrected_blob = self.llm_client.generate_text(
                    prompt,
                    system_instruction=(
                        "You are an expert Computer Science lecture editor. Preserve exact "
                        "[MM:SS] timestamps on every line and fix misheard CS terms."
                    ),
                    allow_fallback=False,
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
            except Exception as exc:
                logger.warning("Pass-1 Gemini correction failed, using local terminology fixes: %s", exc)

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
            try:
                generated_md = self.llm_client.generate_text(
                    prompt,
                    system_instruction=(
                        "Write a clear, concise university study summary grounded strictly "
                        "in the lecture transcript and student Q&A timeline. "
                        "Use Markdown headings: ### Executive Summary, ### Key Topics & Takeaways, "
                        "and ### Action Items & Student Questions."
                    ),
                    allow_fallback=False,
                )
                if generated_md and generated_md.strip():
                    return generated_md
            except Exception as exc:
                logger.warning("Pass-2 Gemini summary failed, using local summary fallback: %s", exc)

        substantive = [
            s for s in corrected_segments if str(s.get("text", "")).strip()
        ]
        n_segs = len(substantive)
        first_ts = substantive[0]["offset_mmss"] if substantive else "00:00"
        last_ts = substantive[-1]["offset_mmss"] if substantive else "00:00"

        exec_lines: list[str] = [
            f"- Lecture **{lecture_title}** ({course_label}) covers {n_segs} timestamped segment(s) spanning `{first_ts}` to `{last_ts}`."
        ]
        if substantive:
            exec_lines.append(
                f"- Opening point (`{substantive[0]['offset_mmss']}`): {substantive[0]['text']}"
            )
            if n_segs >= 3:
                mid = substantive[n_segs // 2]
                exec_lines.append(
                    f"- Core focus (`{mid['offset_mmss']}`): {mid['text']}"
                )
            if n_segs >= 2:
                last = substantive[-1]
                exec_lines.append(
                    f"- Closing point (`{last['offset_mmss']}`): {last['text']}"
                )

        topic_bullets = "\n".join(
            f"- **[{s['offset_mmss']}]** {s['text']}" for s in substantive[:24]
        ) or "- No spoken transcript segments recorded."

        if chat_rows:
            qa_bullets = "\n".join(
                f"- **[{format_offset_mmss(m.offset_seconds)}] {m.author_name} ({m.category}):** "
                f"\"{m.body}\" — *Addressed during the lecture context around {format_offset_mmss(m.offset_seconds)}.*"
                for m in chat_rows
            )
        else:
            qa_bullets = "- No student chat questions were logged during this lecture."

        exec_block = "\n".join(exec_lines)
        return (
            f"### Executive Summary\n"
            f"{exec_block}\n\n"
            f"### Key Topics & Takeaways\n"
            f"{topic_bullets}\n\n"
            f"### Action Items & Student Questions\n"
            f"{qa_bullets}\n"
            f"- Review the timestamped checkpoints from `{first_ts}` to `{last_ts}` in **{lecture_title}**."
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
        *,
        skip_pass1_llm: bool = False,
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

        if skip_pass1_llm:
            corrected_segments = [
                {
                    "offset_mmss": seg["offset_mmss"],
                    "text": apply_domain_terminology_fixes(seg["text"]),
                }
                for seg in raw_segments
            ]
        else:
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
        artifact_id: Optional[int] = None,
        on_status: Optional[Callable[[dict[str, Any]], None]] = None,
    ) -> LectureArtifact:
        """Transcribe (Google Gemini API / fallback) + Pass 1 correction + Pass 2 summary."""
        session = self.db.get(ClassSession, session_id)
        if session is None:
            raise LecturePipelineError("Lecture session not found.", status_code=404)

        if use_sample:
            ref_path = "sample://ariel_oop_lecture"
            src_kind = "sample"
        elif transcript_text and transcript_text.strip():
            ref_path = "inline://transcript"
            src_kind = "transcript"
        elif media_path is not None and media_path.exists():
            ref_path = str(media_path)
            src_kind = source_type
        else:
            raise LecturePipelineError(
                "Provide a media/transcript file, transcript_text, or set use_sample=true.",
                status_code=400,
            )

        artifact = self.db.get(LectureArtifact, artifact_id) if artifact_id else None
        if artifact is None:
            artifact = LectureArtifact(
                session_id=session.id,
                source_type=src_kind,
                media_path=ref_path,
                status="PENDING",
            )
            self.db.add(artifact)
            self.db.flush()

        used_gemini_transcribe = False
        try:
            if use_sample:
                raw_segments = list(BUILTIN_SAMPLE_SEGMENTS)
            elif transcript_text and transcript_text.strip():
                raw_segments = self.parse_transcript_text(transcript_text)
            else:
                assert media_path is not None
                ext = media_path.suffix.lower()
                # If lecturer uploaded a ready Markdown summary (.md), preserve it directly cleanly!
                if ext == ".md":
                    raw_md = self._extract_document_text(media_path)
                    if not raw_md:
                        raise LecturePipelineError(
                            "Uploaded summary file is empty.", status_code=400
                        )
                    raw_segments = self.parse_transcript_text(raw_md)
                    artifact.raw_transcript_json = json.dumps(raw_segments)
                    artifact.corrected_transcript_json = json.dumps(raw_segments)
                    artifact.summary_markdown = raw_md
                    artifact.error_message = None
                    self._set_artifact_stage(artifact, "COMPLETED", on_status=on_status)
                    return artifact
                self._set_artifact_stage(artifact, "TRANSCRIBING", on_status=on_status)
                raw_segments, used_gemini_transcribe = self._transcribe_media_with_origin(media_path)

            return self._synthesize_into_artifact(
                session=session,
                artifact=artifact,
                raw_segments=raw_segments,
                on_status=on_status,
                skip_pass1_llm=used_gemini_transcribe,
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
        if artifact.status != "COMPLETED" or not (artifact.summary_markdown or "").strip():
            raise LecturePipelineError("Lecture summary is not ready for download.", status_code=400)
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
        if artifact.status != "COMPLETED":
            raise LecturePipelineError("Lecture transcript is not ready for download.", status_code=400)
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
