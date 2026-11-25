#!/usr/bin/env python3
"""
ClassPulse Narrated Product Walkthrough Recorder (1920x1080 @ 24fps, <= 120s).

Produces `demo_walkthrough.mp4` in the project root with synchronized natural
English voiceover narration (Microsoft Neural TTS via edge-tts + offline SAPI
fallback), isolated temporary demo seed data (leaving real `data/` untouched),
real TiledSCRFDDetector classroom head-counting, and dual-viewport Chrome CDP
automation across all Lecturer and Student workflows.
"""

from __future__ import annotations

import asyncio
import base64
import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import wave
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional

import imageio_ffmpeg
import requests
from PIL import Image, ImageDraw, ImageFont
from werkzeug.serving import make_server

ROOT_DIR = Path(__file__).resolve().parent.parent
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

from app import create_app
from app.config import Config
from app.models import AttendanceRecord, ClassSession, Course, Student
from app.services.attendance_service import AttendanceService
from app.services.chat_service import ChatService
from app.services.course_service import CourseService
from app.services.homework_pipeline import HomeworkReviewService
from app.services.lecture_pipeline import LectureSummarizerService
from app.services.llm_client import GeminiStudioClient
from app.services.poll_service import PollService
from scripts.e2e_ui_verify import CDPDriver


# ---------------------------------------------------------------------------
# Video & Audio Constants
# ---------------------------------------------------------------------------

OUT_W = 1920
OUT_H = 1080
FPS = 24
DESKTOP_W = 1440
DESKTOP_H = 900
MOBILE_W = 390
MOBILE_H = 844

VOICE_NAME = "en-US-AndrewNeural"
PAUSE_BETWEEN_BEATS_S = 0.28
MIN_TOTAL_S = 90.0
MAX_TOTAL_S = 119.5

BG_DARK = (11, 17, 32)
CARD_DARK = (19, 28, 49)
BORDER_SLATE = (51, 65, 85)
TEXT_WHITE = (248, 250, 252)
TEXT_MUTED = (148, 163, 184)
ACCENT_INDIGO = (99, 102, 241)
ACCENT_EMERALD = (16, 185, 129)
ACCENT_AMBER = (245, 158, 11)
ACCENT_CORAL = (244, 63, 94)


def _load_font(size: int, bold: bool = False) -> ImageFont.FreeTypeFont | ImageFont.ImageFont:
    candidates = (
        [
            "C:/Windows/Fonts/segoeuib.ttf",
            "C:/Windows/Fonts/arialbd.ttf",
            "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
        ]
        if bold
        else [
            "C:/Windows/Fonts/segoeui.ttf",
            "C:/Windows/Fonts/arial.ttf",
            "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
        ]
    )
    for path in candidates:
        if os.path.exists(path):
            try:
                return ImageFont.truetype(path, size)
            except Exception:
                pass
    return ImageFont.load_default()


FONT_BRAND = _load_font(22, bold=True)
FONT_BADGE = _load_font(17, bold=True)
FONT_CAPTION = _load_font(23, bold=True)
FONT_CAPTION_SUB = _load_font(17, bold=False)
FONT_TITLE_XL = _load_font(56, bold=True)
FONT_TITLE_SUB = _load_font(26, bold=False)
FONT_CARD_H = _load_font(25, bold=True)
FONT_CARD_B = _load_font(21, bold=False)


# ---------------------------------------------------------------------------
# Narration Script Specification (Tightly Paced for ~105s - 114s)
# ---------------------------------------------------------------------------

NARRATION_BEATS: list[dict[str, str]] = [
    {
        "id": "01_intro_login",
        "chapter": "01 · LECTURER SETUP & COURSE CREATION",
        "caption": "Dr. Daniel Cohen signs in to an organized, full-viewport lecturer hub.",
        "text": (
            "Welcome to ClassPulse. Dr. Daniel Cohen signs in cleanly to a full-viewport "
            "dashboard with every course and lecture organized in the sidebar."
        ),
    },
    {
        "id": "02_course_num_and_join",
        "chapter": "01 · EXACT COURSE NUMBER & ROSTER APPROVAL",
        "caption": "Custom Course Number 'CS305' saved accurately; pending student join approved in Roster.",
        "text": (
            "Creating a course preserves exact course numbers like CS305 without random IDs. "
            "When Yael Mizrahi requests to join by course number, a red dot alerts the roster "
            "for instant lecturer approval."
        ),
    },
    {
        "id": "03_qr_checkin",
        "chapter": "02 · QR ATTENDANCE CHECK-IN",
        "caption": "Scanning lecture QR checks Noa Levi directly into CS101 Lecture 2.",
        "text": (
            "In class, students scan the rotating QR code with only their name and ID, "
            "checking directly into the exact course and lecture while live metrics update."
        ),
    },
    {
        "id": "04_headcount_discrepancy",
        "chapter": "02 · AI HEAD-COUNTING & DISCREPANCY ALERT",
        "caption": "AI detects 34 faces vs. 38 QR check-ins -> Red Alert Banner & Roster Warning.",
        "text": (
            "Next, AI head-counting detects thirty-four faces against thirty-eight QR check-ins, "
            "raising an immediate red discrepancy alert and approval warning inside the roster."
        ),
    },
    {
        "id": "05_summary_upload",
        "chapter": "03 · LECTURE SUMMARY & STRUCTURED TAKEAWAYS",
        "caption": "Uploading lecture notes generates structured Takeaways, Topics & Transcript in Draft.",
        "text": (
            "Uploading a lecture recording automatically extracts key takeaways, structured topics, "
            "and a timestamped transcript organized directly beneath that lecture."
        ),
    },
    {
        "id": "06_publish_control",
        "chapter": "03 · DRAFT-TO-PUBLISHED ACCESS CONTROL",
        "caption": "Draft privacy shield -> one-click Publish to Students with Unpublish & Delete sync.",
        "text": (
            "Summaries remain private in Draft mode until Dr. Cohen publishes them to students, "
            "with instant unpublish and delete sync whenever revisions are needed."
        ),
    },
    {
        "id": "07_student_remote_nav",
        "chapter": "04 · REMOTE STUDENT LOGIN & HIERARCHICAL TREE",
        "caption": "Remote login (Name + ID only): time greeting, indented tree & multi-level dots.",
        "text": (
            "From home, Noa logs in with just her name and ID, seeing a live greeting, "
            "an indented sidebar tree, and multi-level unread notification dots."
        ),
    },
    {
        "id": "08_courses_log_offline_q",
        "chapter": "04 · COURSES LOG, PAST POLLS & OFFLINE Q&A",
        "caption": "Attendance %, Lecture 2 drill-down, Past Polls & submitting an Offline Question.",
        "text": (
            "Inside Courses, Noa checks her attendance percentage, reviews past lecture polls, "
            "and sends an offline question directly from home."
        ),
    },
    {
        "id": "09_summaries_comparative_rank",
        "chapter": "04 · GROUPED SUMMARIES & COMPARATIVE RANK",
        "caption": "Summaries grouped by Course/Lecture; Practice Draft reveals AI Feedback + Class Rank.",
        "text": (
            "Lecture summaries stay neatly grouped by course and order. In Assignments, a practice draft "
            "gives AI feedback and Noa's ordinal class rank privately inside the student app."
        ),
    },
    {
        "id": "10_final_submit_lecturer",
        "chapter": "04 · FINAL SUBMISSION & ISOLATED LECTURER VIEW",
        "caption": "Official Final Submission -> Lecturer receives ONLY finalized file & standalone AI Score.",
        "text": (
            "When ready, she sends her official Final Submission. Back in the lecturer view, "
            "Dr. Cohen sees only finalized files and standalone rubric scores."
        ),
    },
    {
        "id": "11_outro",
        "chapter": "SUMMARY · CLASSPULSE",
        "caption": "ClassPulse — Unified Attendance, AI Summaries, Student Workspace & Grading.",
        "text": (
            "ClassPulse streamlines classroom teaching and remote learning in one clear platform."
        ),
    },
]


# ---------------------------------------------------------------------------
# Audio Synthesis & Master Track Assembly
# ---------------------------------------------------------------------------

def _ffprobe_duration(ffprobe_exe: str, path: Path) -> float:
    out = subprocess.check_output(
        [
            ffprobe_exe,
            "-v",
            "error",
            "-show_entries",
            "format=duration",
            "-of",
            "default=noprint_wrappers=1:nokey=1",
            str(path),
        ],
        text=True,
    ).strip()
    return float(out)


def _find_ffprobe(ffmpeg_exe: str) -> str:
    probe = shutil.which("ffprobe")
    if probe:
        return probe
    candidate = Path(ffmpeg_exe).with_name("ffprobe.exe")
    if candidate.exists():
        return str(candidate)
    raise RuntimeError("ffprobe binary not found")


async def _render_edge_tts_batch(
    beats: list[dict[str, str]],
    out_dir: Path,
    rate: str,
) -> list[Path]:
    import edge_tts

    paths: list[Path] = []
    for beat in beats:
        mp3_path = out_dir / f"{beat['id']}.mp3"
        comm = edge_tts.Communicate(beat["text"], VOICE_NAME, rate=rate)
        await comm.save(str(mp3_path))
        paths.append(mp3_path)
    return paths


def _render_sapi_fallback(beats: list[dict[str, str]], out_dir: Path) -> list[Path]:
    ps_lines = [
        "Add-Type -AssemblyName System.Speech",
        "$synth = New-Object System.Speech.Synthesis.SpeechSynthesizer",
        "try { $synth.SelectVoice('Microsoft Zira Desktop') } catch {}",
        "$synth.Rate = 1",
    ]
    paths: list[Path] = []
    for beat in beats:
        wav_path = out_dir / f"{beat['id']}.wav"
        paths.append(wav_path)
        escaped = beat["text"].replace("'", "''")
        ps_lines.append(f"$synth.SetOutputToWaveFile('{str(wav_path)}')")
        ps_lines.append(f"$synth.Speak('{escaped}')")
        ps_lines.append("$synth.SetOutputToNull()")
    ps_script = out_dir / "sapi_tts.ps1"
    ps_script.write_text("\n".join(ps_lines), encoding="utf-8")
    subprocess.run(
        ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(ps_script)],
        check=True,
    )
    return paths


@dataclass
class TimedBeat:
    id: str
    chapter: str
    caption: str
    audio_path: Path
    speech_dur: float
    start_sec: float
    end_sec: float  # includes trailing PAUSE_BETWEEN_BEATS_S


def prepare_timed_narration(
    audio_dir: Path,
    ffmpeg_exe: str,
    ffprobe_exe: str,
) -> tuple[list[TimedBeat], Path, float]:
    """Synthesize all voiceover clips, normalize pace to 90-119s, and build master WAV."""
    chosen_rate = "+8%"
    raw_clips: list[Path] = []
    used_edge = False

    for rate_try in ("+8%", "+15%", "+2%", "+22%"):
        try:
            raw_clips = asyncio.run(_render_edge_tts_batch(NARRATION_BEATS, audio_dir, rate_try))
            used_edge = True
        except Exception as exc:
            print(f"[TTS] edge-tts unavailable ({exc}), falling back to Windows SAPI...")
            raw_clips = _render_sapi_fallback(NARRATION_BEATS, audio_dir)
            break

        durations = [_ffprobe_duration(ffprobe_exe, p) for p in raw_clips]
        total_est = sum(durations) + PAUSE_BETWEEN_BEATS_S * len(durations) + 0.6
        chosen_rate = rate_try
        print(f"[TTS] edge-tts rate={rate_try} -> estimated duration={total_est:.2f}s")
        if MIN_TOTAL_S <= total_est <= MAX_TOTAL_S:
            break
        if total_est > MAX_TOTAL_S and rate_try == "+8%":
            continue
        if total_est < MIN_TOTAL_S and rate_try == "+8%":
            continue
        if total_est <= MAX_TOTAL_S:
            break

    # Decode every clip cleanly to 48kHz mono 16-bit PCM WAV and assemble exact timeline
    sample_rate = 48000
    pcm_chunks: list[bytes] = []
    timed_beats: list[TimedBeat] = []
    cursor_s = 0.25  # tiny lead-in before beat 1 voice starts

    lead_silence = b"\x00\x00" * int(round(cursor_s * sample_rate))
    pcm_chunks.append(lead_silence)

    pause_frames = int(round(PAUSE_BETWEEN_BEATS_S * sample_rate))
    pause_bytes = b"\x00\x00" * pause_frames

    for idx, (beat, clip_path) in enumerate(zip(NARRATION_BEATS, raw_clips)):
        wav_tmp = audio_dir / f"{beat['id']}_48k.wav"
        subprocess.run(
            [
                ffmpeg_exe,
                "-y",
                "-i",
                str(clip_path),
                "-ac",
                "1",
                "-ar",
                str(sample_rate),
                "-c:a",
                "pcm_s16le",
                str(wav_tmp),
            ],
            check=True,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        with wave.open(str(wav_tmp), "rb") as wf:
            frames_bytes = wf.readframes(wf.getnframes())
            speech_dur = wf.getnframes() / float(sample_rate)

        pcm_chunks.append(frames_bytes)
        tail_pause = PAUSE_BETWEEN_BEATS_S if idx < len(raw_clips) - 1 else 0.55
        if idx < len(raw_clips) - 1:
            pcm_chunks.append(pause_bytes)
        else:
            pcm_chunks.append(b"\x00\x00" * int(round(tail_pause * sample_rate)))

        start_s = cursor_s if idx > 0 else 0.0
        end_s = cursor_s + speech_dur + tail_pause
        timed_beats.append(
            TimedBeat(
                id=beat["id"],
                chapter=beat["chapter"],
                caption=beat["caption"],
                audio_path=wav_tmp,
                speech_dur=speech_dur,
                start_sec=start_s,
                end_sec=end_s,
            )
        )
        cursor_s = end_s

    raw_master = audio_dir / "master_raw.wav"
    with wave.open(str(raw_master), "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(sample_rate)
        wf.writeframes(b"".join(pcm_chunks))

    norm_master = audio_dir / "master_norm.wav"
    subprocess.run(
        [
            ffmpeg_exe,
            "-y",
            "-i",
            str(raw_master),
            "-af",
            "loudnorm=I=-16:TP=-1.5:LRA=11",
            "-ar",
            str(sample_rate),
            "-ac",
            "2",
            str(norm_master),
        ],
        check=True,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )

    total_audio_dur = _ffprobe_duration(ffprobe_exe, norm_master)
    print(
        f"[TTS] Master track ready ({'edge-tts ' + chosen_rate if used_edge else 'SAPI'}): "
        f"{total_audio_dur:.2f}s across {len(timed_beats)} beats"
    )
    if total_audio_dur > 120.0:
        raise RuntimeError(f"Master audio {total_audio_dur:.2f}s exceeds strict 120s budget")
    return timed_beats, norm_master, total_audio_dur


# ---------------------------------------------------------------------------
# Isolated Fictional Seed Server (Zero Writes to Real `data/`)
# ---------------------------------------------------------------------------

@dataclass
class DemoSeedInfo:
    base_url: str
    lecturer_user: str
    lecturer_pw: str
    cs101_id: int
    cs101_code: str
    cs101_l1_id: int
    cs101_l2_id: int
    cs101_l2_code: str
    cs101_assign1_id: int
    cs202_id: int
    cs202_assign1_id: int
    noa_name: str
    noa_sid: str
    yael_name: str
    yael_sid: str
    omer_name: str
    omer_sid: str


class NarratedDemoServer:
    """Isolated ClassPulse server seeded cleanly in a temp directory."""

    def __init__(self) -> None:
        self.tmp_dir = Path(tempfile.mkdtemp(prefix="cp_narr_srv_"))
        self.server = None
        self.thread = None
        self.app = None

    def start(self) -> DemoSeedInfo:
        db_path = self.tmp_dir / "narrated_demo.db"
        upload_dir = self.tmp_dir / "uploads"
        report_dir = self.tmp_dir / "reports"
        upload_dir.mkdir(parents=True, exist_ok=True)
        report_dir.mkdir(parents=True, exist_ok=True)

        cfg = Config(
            SECRET_KEY="classpulse-narrated-demo-secret",
            DATABASE_URL=f"sqlite:///{db_path.as_posix()}",
            UPLOAD_DIR=upload_dir,
            REPORT_DIR=report_dir,
            GEMINI_API_KEY="",
            ENABLE_CSRF=True,
            ENABLE_DEMO_SEED=False,
        )

        self.app = create_app(cfg, llm_client=GeminiStudioClient(api_key=""))
        self.server = make_server("127.0.0.1", 0, self.app, threaded=True)
        port = int(self.server.server_port)
        base_url = f"http://127.0.0.1:{port}"
        cfg.PUBLIC_BASE_URL = base_url
        self.app.config["PUBLIC_BASE_URL"] = base_url

        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

        deadline = time.monotonic() + 10.0
        while time.monotonic() < deadline:
            try:
                if requests.get(f"{base_url}/healthz", timeout=1.0).status_code == 200:
                    break
            except Exception:
                pass
            time.sleep(0.1)

        with self.app.app_context():
            db = self.app.extensions["db_session"]
            qr_svc = self.app.extensions["qr_service"]
            device_mgr = self.app.extensions["device_mgr"]

            course_svc = CourseService(db=db)
            att_svc = AttendanceService(db=db, qr_service=qr_svc, device_mgr=device_mgr)
            poll_svc = PollService(db=db)
            chat_svc = ChatService(db=db)
            lec_svc = LectureSummarizerService(
                db=db, llm_client=self.app.extensions["llm_client"], upload_dir=upload_dir
            )
            hw_svc = HomeworkReviewService(db=db, llm_client=self.app.extensions["llm_client"])

            # Lecturer: Dr. Daniel Cohen
            cohen = course_svc.register_lecturer(
                full_name="Dr. Daniel Cohen",
                username="daniel.cohen",
                email="daniel.cohen@university.edu",
                password="ClassPulse2026!",
                confirm_password="ClassPulse2026!",
                department="Computer Science",
            )

            # Course 1: Introduction to Computer Science (CS101)
            cs101 = course_svc.create_course(
                course_code="CS101",
                name="Introduction to Computer Science",
                department="Computer Science",
                semester="Fall 2026",
                lecturer_name=cohen.full_name,
                lecturer_id=cohen.id,
                initial_lecture_title="Lecture 1 — Algorithms & Big-O Complexity",
            )
            cs101_l1 = db.query(ClassSession).filter_by(course_id=cs101.id).first()
            assert cs101_l1 is not None
            cs101_l1.session_code = "CS101L1"
            db.commit()

            cs101_l2 = att_svc.create_session(
                title="Lecture 2 — Recursion & Graph Search",
                session_code="CS101L2",
                course_id=cs101.id,
            )

            # Core students
            _, noa = course_svc.enroll_student_by_code(
                course_code="CS101",
                full_name="Noa Levi",
                email="noa.levi@university.edu",
                student_id_number="315928374",
                device_id="demo-noa-dev",
            )
            # Noa attended Lecture 1 so far (Lecture 2 check-in happens live on camera)
            db.add(AttendanceRecord(session_id=cs101_l1.id, student_id=noa.id, device_id="noa-l1", is_late=False, token_bucket=0))

            _, omer = course_svc.enroll_student_by_code(
                course_code="CS101",
                full_name="Omer Katz",
                email="omer.katz@university.edu",
                student_id_number="209384712",
                device_id="demo-omer-dev",
            )
            db.add(AttendanceRecord(session_id=cs101_l1.id, student_id=omer.id, device_id="omer-l1", is_late=False, token_bucket=0))
            db.add(AttendanceRecord(session_id=cs101_l2.id, student_id=omer.id, device_id="omer-l2", is_late=False, token_bucket=0))

            # Seed 36 classmates in CS101 Lecture 2 so QR check-ins reach 37 before Noa -> 38 total vs 34 visual faces
            for i in range(1, 37):
                sid_num = f"210000{i:03d}"
                _, classmate = course_svc.enroll_student_by_code(
                    course_code="CS101",
                    full_name=f"Student {i:02d}",
                    email=f"student{i:02d}@university.edu",
                    student_id_number=sid_num,
                    device_id=f"demo-c-{i}",
                )
                db.add(AttendanceRecord(session_id=cs101_l1.id, student_id=classmate.id, device_id=f"c-l1-{i}", is_late=False, token_bucket=0))
                db.add(AttendanceRecord(session_id=cs101_l2.id, student_id=classmate.id, device_id=f"c-l2-{i}", is_late=False, token_bucket=0))
            db.commit()

            # Past Poll + Live Q&A on CS101 Lecture 2
            poll = poll_svc.create_poll(
                session_id=cs101_l2.id,
                question="What is the worst-case time complexity of Binary Search?",
                options=["O(log n)", "O(n)", "O(n log n)"],
                correct_index=0,
            )
            try:
                poll_svc.launch_poll(poll.id)
                poll_svc.cast_vote(poll.id, omer.id, 0)
                poll_svc.close_poll(poll.id)
            except Exception:
                pass

            chat_svc.post_message(
                session_id=cs101_l2.id,
                author_name="Omer Katz",
                body="Does BFS always find the shortest path in unweighted graphs?",
                category="Question",
                student_id=omer.id,
            )
            chat_svc.post_message(
                session_id=cs101_l2.id,
                author_name="Dr. Daniel Cohen",
                body="Yes! Exploring layer by layer guarantees the minimum edge distance.",
                category="Question",
                student_id=None,
            )

            # Published Summary on CS101 Lecture 1 (so Lecture 1 + Lecture 2 appear in lecture order!)
            art1 = lec_svc.summarize_session_recording(
                session_id=cs101_l1.id,
                transcript_text=(
                    "[00:00] Welcome to CS101 Lecture 1: Algorithms and Big-O Complexity.\n"
                    "[04:10] Asymptotic bounds compare growth rates independently of hardware constants.\n"
                    "[12:30] Divide-and-conquer halves input sizes recursively yielding O(n log n) sorting."
                ),
                source_type="upload",
            )
            lec_svc.publish_artifact(art1.id, publish=True)

            # CS101 Assignment 1 + Comparative & Final submissions from Omer Katz for real Class Rank
            cs101_a1 = hw_svc.create_assignment(
                course_id=cs101.id,
                title="Ex1 — Recursive Binary Search & Graph BFS",
                rubric_text="Correctness & Base Case (40 pts), BFS Queue Traversal (35 pts), Clean Typing (25 pts)",
            )
            hw_svc.submit_homework(
                assignment_id=cs101_a1.id,
                student_id=omer.id,
                content_text=(
                    "def binary_search(arr, target):\n"
                    "    lo, hi = 0, len(arr) - 1\n"
                    "    while lo <= hi:\n"
                    "        mid = (lo + hi) // 2\n"
                    "        if arr[mid] == target: return mid\n"
                    "        if arr[mid] < target: lo = mid + 1\n"
                    "        else: hi = mid - 1\n"
                    "    return -1\n"
                ),
                submission_type="comparative",
            )
            hw_svc.submit_homework(
                assignment_id=cs101_a1.id,
                student_id=omer.id,
                content_text=(
                    "from collections import deque\n"
                    "def bfs_shortest(adj: dict[int, list[int]], start: int, goal: int) -> int:\n"
                    "    q = deque([(start, 0)])\n"
                    "    seen = {start}\n"
                    "    while q:\n"
                    "        u, d = q.popleft()\n"
                    "        if u == goal: return d\n"
                    "        for v in adj.get(u, []):\n"
                    "            if v not in seen:\n"
                    "                seen.add(v)\n"
                    "                q.append((v, d + 1))\n"
                    "    return -1\n"
                ),
                submission_type="final",
            )

            # Course 2: Data Structures & Algorithms (CS202)
            cs202 = course_svc.create_course(
                course_code="CS202",
                name="Data Structures & Algorithms",
                department="Computer Science",
                semester="Fall 2026",
                lecturer_name=cohen.full_name,
                lecturer_id=cohen.id,
                initial_lecture_title="Lecture 1 — Balanced Trees & Hash Maps",
            )
            cs202_l1 = db.query(ClassSession).filter_by(course_id=cs202.id).first()
            assert cs202_l1 is not None
            cs202_l1.session_code = "CS202L1"
            db.commit()

            for stu, dev in ((noa, "noa-202"), (omer, "omer-202")):
                course_svc.enroll_student_by_code(
                    course_code="CS202",
                    full_name=stu.full_name,
                    email=stu.email or "",
                    student_id_number=stu.student_id_number,
                    device_id=dev,
                )
            db.add(AttendanceRecord(session_id=cs202_l1.id, student_id=noa.id, device_id="noa-202-l1", is_late=False, token_bucket=0))
            db.commit()

            art202 = lec_svc.summarize_session_recording(
                session_id=cs202_l1.id,
                transcript_text=(
                    "[00:00] Today we cover AVL tree rotations and amortized hash table resizing.\n"
                    "[08:15] Left-Right double rotations restore balance factor in O(1) pointer updates.\n"
                    "[15:40] Load factor below 0.75 keeps lookup expected constant time."
                ),
                source_type="upload",
            )
            lec_svc.publish_artifact(art202.id, publish=True)

            cs202_a1 = hw_svc.create_assignment(
                course_id=cs202.id,
                title="Ex1 — AVL Rotations & Open Addressing",
                rubric_text="Rotation Invariants (50 pts), Collision Probing (50 pts)",
            )

            # Fictional third student Yael Mizrahi (enrolled in CS202 only initially; joins CS101 via Course Number)
            _, yael = course_svc.enroll_student_by_code(
                course_code="CS202",
                full_name="Yael Mizrahi",
                email="yael.mizrahi@university.edu",
                student_id_number="318472910",
                device_id="demo-yael-dev",
            )

            return DemoSeedInfo(
                base_url=base_url,
                lecturer_user="daniel.cohen",
                lecturer_pw="ClassPulse2026!",
                cs101_id=int(cs101.id),
                cs101_code="CS101",
                cs101_l1_id=int(cs101_l1.id),
                cs101_l2_id=int(cs101_l2.id),
                cs101_l2_code="CS101L2",
                cs101_assign1_id=int(cs101_a1.id),
                cs202_id=int(cs202.id),
                cs202_assign1_id=int(cs202_a1.id),
                noa_name="Noa Levi",
                noa_sid="315928374",
                yael_name="Yael Mizrahi",
                yael_sid="318472910",
                omer_name="Omer Katz",
                omer_sid="209384712",
            )

    def stop(self) -> None:
        if self.server is not None:
            try:
                self.server.shutdown()
                self.server.server_close()
            except Exception:
                pass
        shutil.rmtree(self.tmp_dir, ignore_errors=True)


# ---------------------------------------------------------------------------
# Headless Chrome CDP Helpers
# ---------------------------------------------------------------------------

def set_cdp_viewport(cdp: CDPDriver, width: int, height: int, scale: int = 1, mobile: bool = False) -> None:
    cdp.cdp_call(
        "Emulation.setDeviceMetricsOverride",
        {"width": width, "height": height, "deviceScaleFactor": scale, "mobile": mobile},
    )


def capture_pil(cdp: CDPDriver, expected_size: tuple[int, int]) -> Image.Image:
    res = cdp.cdp_call("Page.captureScreenshot", {"format": "png", "fromSurface": True})
    raw = base64.b64decode(res["data"])
    img = Image.open(io.BytesIO(raw)).convert("RGB")
    if img.size != expected_size:
        img = img.resize(expected_size, Image.Resampling.LANCZOS)
    return img


def get_el_center(cdp: CDPDriver, selector: str) -> tuple[float, float]:
    pos = cdp.eval_js(
        f"""(() => {{
            const el = document.querySelector({json.dumps(selector)});
            if (!el) return null;
            el.scrollIntoView({{ block: 'nearest', inline: 'nearest' }});
            const r = el.getBoundingClientRect();
            return {{ x: r.left + r.width / 2, y: r.top + r.height / 2 }};
        }})()"""
    )
    if not pos:
        return (720.0, 450.0)
    return (float(pos["x"]), float(pos["y"]))


def click_el_js(cdp: CDPDriver, selector: str) -> tuple[float, float]:
    pos = get_el_center(cdp, selector)
    cdp.eval_js(
        f"""(() => {{
            const el = document.querySelector({json.dumps(selector)});
            if (el) el.click();
        }})()"""
    )
    time.sleep(0.16)
    return pos


def type_el_js(cdp: CDPDriver, selector: str, value: str) -> tuple[float, float]:
    pos = get_el_center(cdp, selector)
    cdp.eval_js(
        f"""(() => {{
            const el = document.querySelector({json.dumps(selector)});
            if (!el) return;
            el.focus();
            el.value = {json.dumps(value)};
            el.dispatchEvent(new Event('input', {{ bubbles: true }}));
            el.dispatchEvent(new Event('change', {{ bubbles: true }}));
        }})()"""
    )
    time.sleep(0.10)
    return pos


def set_file_input(cdp: CDPDriver, selector: str, file_path: Path) -> None:
    cdp.cdp_call("DOM.enable")
    doc = cdp.cdp_call("DOM.getDocument", {"depth": -1})
    q = cdp.cdp_call(
        "DOM.querySelector",
        {"nodeId": doc["root"]["nodeId"], "selector": selector},
    )
    node_id = int(q.get("nodeId") or 0)
    if not node_id:
        raise RuntimeError(f"File input not found: {selector}")
    cdp.cdp_call(
        "DOM.setFileInputFiles",
        {"files": [str(file_path.resolve())], "nodeId": node_id},
    )
    time.sleep(0.22)


def scroll_into_view(cdp: CDPDriver, selector: str, block: str = "center") -> None:
    cdp.eval_js(
        f"""(() => {{
            const el = document.querySelector({json.dumps(selector)});
            if (el) el.scrollIntoView({{ block: {json.dumps(block)}, behavior: 'instant' }});
        }})()"""
    )
    time.sleep(0.12)


# ---------------------------------------------------------------------------
# Audio-Synchronized Compositor (1920x1080 @ 24fps)
# ---------------------------------------------------------------------------

class NarratedCompositor:
    """Renders 24fps composited frames locked to exact narration beat end frames."""

    def __init__(self, raw_video_mp4: Path, ffmpeg_exe: str, total_audio_sec: float) -> None:
        self.raw_video_mp4 = raw_video_mp4
        self.total_audio_sec = total_audio_sec
        self.expected_total_frames = max(1, int(round(total_audio_sec * FPS)))
        self.frames_written = 0

        self.chapter_label = "01 · LECTURER SETUP & COURSE CREATION"
        self.caption = "ClassPulse — Classroom Engagement, AI Summaries & Student Grading"
        self.layout_mode = "desktop"  # "desktop" | "dual" | "title"
        self.cursor_x = 960.0
        self.cursor_y = 540.0
        self.cursor_visible = False

        self._desktop_img = Image.new("RGB", (DESKTOP_W, DESKTOP_H), (248, 250, 252))
        self._mobile_img = Image.new("RGB", (MOBILE_W, MOBILE_H), (248, 250, 252))

        cmd = [
            ffmpeg_exe,
            "-y",
            "-f", "rawvideo",
            "-vcodec", "rawvideo",
            "-pix_fmt", "rgb24",
            "-s", f"{OUT_W}x{OUT_H}",
            "-r", str(FPS),
            "-i", "-",
            "-an",
            "-c:v", "libx264",
            "-preset", "fast",
            "-crf", "18",
            "-pix_fmt", "yuv420p",
            str(self.raw_video_mp4),
        ]
        self.proc = subprocess.Popen(cmd, stdin=subprocess.PIPE, stderr=subprocess.PIPE)

        self.desk_single_box = ((OUT_W - 1440) // 2, 84, 1440, 900)
        self.dual_desk_box = (48, 114, 1320, 825)
        self.dual_mob_box = (1454, 108, 390, 844)

    def _screen_coords(self, target: str, bx: float, by: float) -> tuple[float, float]:
        if self.layout_mode == "desktop":
            ox, oy, w, h = self.desk_single_box
            return ox + (bx / DESKTOP_W) * w, oy + (by / DESKTOP_H) * h
        if target == "desktop":
            ox, oy, w, h = self.dual_desk_box
            return ox + (bx / DESKTOP_W) * w, oy + (by / DESKTOP_H) * h
        ox, oy, w, h = self.dual_mob_box
        return ox + (bx / MOBILE_W) * w, oy + (by / MOBILE_H) * h

    def _draw_chrome_bars(self, draw: ImageDraw.ImageDraw) -> None:
        # Top bar
        draw.rectangle([0, 0, OUT_W, 74], fill=CARD_DARK)
        draw.line([0, 74, OUT_W, 74], fill=BORDER_SLATE, width=2)
        draw.ellipse([32, 23, 60, 51], fill=ACCENT_INDIGO)
        draw.ellipse([40, 31, 52, 43], fill=ACCENT_EMERALD)
        draw.text((74, 24), "ClassPulse", fill=TEXT_WHITE, font=FONT_BRAND)

        # Chapter badge pill
        pill_w = max(360, int(len(self.chapter_label) * 10.6) + 40)
        draw.rounded_rectangle([240, 18, 240 + pill_w, 56], radius=16, fill=(30, 41, 59), outline=ACCENT_INDIGO, width=2)
        draw.text((260, 26), self.chapter_label, fill=(199, 210, 254), font=FONT_BADGE)

        # Progress bar on top right
        cur_s = self.frames_written / FPS
        frac = min(1.0, max(0.0, cur_s / max(1.0, self.total_audio_sec)))
        bar_x, bar_y, bar_w, bar_h = 1520, 32, 270, 10
        draw.rounded_rectangle([bar_x, bar_y, bar_x + bar_w, bar_y + bar_h], radius=5, fill=(30, 41, 59))
        if frac > 0.0:
            draw.rounded_rectangle(
                [bar_x, bar_y, bar_x + max(10, int(bar_w * frac)), bar_y + bar_h],
                radius=5,
                fill=ACCENT_EMERALD,
            )
        draw.text((1806, 25), f"{int(cur_s)}s / {int(round(self.total_audio_sec))}s", fill=TEXT_MUTED, font=FONT_BADGE)

        # Bottom caption bar
        draw.rectangle([0, OUT_H - 78, OUT_W, OUT_H], fill=CARD_DARK)
        draw.line([0, OUT_H - 78, OUT_W, OUT_H - 78], fill=BORDER_SLATE, width=2)
        draw.text((48, OUT_H - 54), self.caption, fill=TEXT_WHITE, font=FONT_CAPTION)

    def _draw_cursor(self, canvas: Image.Image, ripple_r: float = 0.0, ripple_alpha: float = 0.0) -> None:
        if not self.cursor_visible:
            return
        draw = ImageDraw.Draw(canvas, "RGBA")
        cx, cy = int(round(self.cursor_x)), int(round(self.cursor_y))
        if ripple_r > 1.0 and ripple_alpha > 0.0:
            a = int(220 * ripple_alpha)
            draw.ellipse(
                [cx - ripple_r, cy - ripple_r, cx + ripple_r, cy + ripple_r],
                outline=(99, 102, 241, a),
                width=4,
            )
        pts = [
            (cx, cy),
            (cx, cy + 24),
            (cx + 7, cy + 19),
            (cx + 12, cy + 29),
            (cx + 16, cy + 27),
            (cx + 11, cy + 17),
            (cx + 19, cy + 17),
        ]
        draw.polygon(pts, fill=(15, 23, 42, 245), outline=(255, 255, 255, 255))

    def _compose_frame(self, ripple_r: float = 0.0, ripple_alpha: float = 0.0) -> Image.Image:
        canvas = Image.new("RGB", (OUT_W, OUT_H), BG_DARK)
        draw = ImageDraw.Draw(canvas)
        if self.layout_mode == "desktop":
            ox, oy, w, h = self.desk_single_box
            draw.rounded_rectangle([ox - 6, oy - 6, ox + w + 6, oy + h + 6], radius=14, fill=(30, 41, 59), outline=BORDER_SLATE, width=2)
            canvas.paste(self._desktop_img.resize((w, h), Image.Resampling.LANCZOS), (ox, oy))
        elif self.layout_mode == "dual":
            dx, dy, dw, dh = self.dual_desk_box
            draw.rounded_rectangle([dx - 5, dy - 5, dx + dw + 5, dy + dh + 5], radius=12, fill=(30, 41, 59), outline=BORDER_SLATE, width=2)
            canvas.paste(self._desktop_img.resize((dw, dh), Image.Resampling.LANCZOS), (dx, dy))

            mx, my, mw, mh = self.dual_mob_box
            draw.rounded_rectangle([mx - 14, my - 22, mx + mw + 14, my + mh + 22], radius=32, fill=(17, 24, 39), outline=(71, 85, 105), width=3)
            draw.rounded_rectangle([mx + mw // 2 - 46, my - 13, mx + mw // 2 + 46, my - 5], radius=4, fill=(30, 41, 59))
            canvas.paste(self._mobile_img.resize((mw, mh), Image.Resampling.LANCZOS), (mx, my))

        self._draw_chrome_bars(draw)
        self._draw_cursor(canvas, ripple_r=ripple_r, ripple_alpha=ripple_alpha)
        return canvas

    def _emit_image(self, img: Image.Image, n_frames: int = 1) -> None:
        if n_frames <= 0:
            return
        assert self.proc.stdin is not None
        raw = img.tobytes()
        for _ in range(n_frames):
            self.proc.stdin.write(raw)
            self.frames_written += 1

    def update_state(
        self,
        *,
        desktop: Optional[Image.Image] = None,
        mobile: Optional[Image.Image] = None,
        chapter: Optional[str] = None,
        caption: Optional[str] = None,
        layout: Optional[str] = None,
    ) -> None:
        if desktop is not None:
            self._desktop_img = desktop
        if mobile is not None:
            self._mobile_img = mobile
        if chapter is not None:
            self.chapter_label = chapter
        if caption is not None:
            self.caption = caption
        if layout is not None:
            self.layout_mode = layout

    def render_card_frames(self, title: str, subtitle: str, bullets: list[str], seconds: float) -> None:
        prev_layout = self.layout_mode
        self.layout_mode = "title"
        self.cursor_visible = False
        canvas = Image.new("RGB", (OUT_W, OUT_H), BG_DARK)
        draw = ImageDraw.Draw(canvas)
        draw.rounded_rectangle([210, 165, 1710, 915], radius=24, fill=(22, 31, 52), outline=BORDER_SLATE, width=2)
        draw.ellipse([275, 222, 331, 278], fill=ACCENT_INDIGO)
        draw.ellipse([290, 237, 316, 263], fill=ACCENT_EMERALD)
        draw.text((352, 230), "ClassPulse University Engagement Platform", fill=(165, 180, 252), font=FONT_CARD_H)
        draw.text((275, 320), title, fill=TEXT_WHITE, font=FONT_TITLE_XL)
        draw.text((275, 405), subtitle, fill=(203, 213, 225), font=FONT_TITLE_SUB)
        y = 492
        for line in bullets:
            draw.ellipse([282, y + 8, 296, y + 22], fill=ACCENT_EMERALD)
            draw.text((318, y), line, fill=TEXT_WHITE, font=FONT_CARD_B)
            y += 54
        self._draw_chrome_bars(draw)

        n = max(1, int(round(seconds * FPS)))
        fade = min(8, n // 2)
        dark = Image.new("RGB", (OUT_W, OUT_H), BG_DARK)
        for i in range(fade):
            self._emit_image(Image.blend(dark, canvas, (i + 1) / fade), 1)
        self._emit_image(canvas, n - fade)
        self.layout_mode = prev_layout

    def hold(self, seconds: float) -> None:
        n = max(1, int(round(seconds * FPS)))
        self._emit_image(self._compose_frame(), n)

    def move_and_click(self, target: str, bx: float, by: float, glide_s: float = 0.28) -> None:
        tx, ty = self._screen_coords(target, bx, by)
        sx, sy = self.cursor_x, self.cursor_y
        self.cursor_visible = True
        steps = max(4, int(round(glide_s * FPS)))
        for i in range(1, steps + 1):
            t = i / steps
            ease = 1.0 - (1.0 - t) ** 3
            self.cursor_x = sx + (tx - sx) * ease
            self.cursor_y = sy + (ty - sy) * ease
            self._emit_image(self._compose_frame(), 1)
        for i in range(5):
            frac = (i + 1) / 5.0
            self._emit_image(self._compose_frame(ripple_r=8.0 + 24.0 * frac, ripple_alpha=1.0 - frac), 1)

    def pad_to_beat_end(self, beat: TimedBeat) -> None:
        target_frame = int(round(beat.end_sec * FPS))
        delta = target_frame - self.frames_written
        if delta > 0:
            self._emit_image(self._compose_frame(), delta)
        else:
            print(f"[TIMING] Beat {beat.id} slightly overran by {-delta} frames ({-delta / FPS:.2f}s)")

    def close(self) -> float:
        assert self.proc.stdin is not None
        self.proc.stdin.close()
        stderr = self.proc.stderr.read() if self.proc.stderr else b""
        rc = self.proc.wait()
        if rc != 0:
            raise RuntimeError(f"ffmpeg video encode failed ({rc}): {stderr.decode('utf-8', 'ignore')}")
        return self.frames_written / FPS


# ---------------------------------------------------------------------------
# End-to-End Choreographed Walkthrough
# ---------------------------------------------------------------------------

def _snap_d(cdp: CDPDriver, comp: NarratedCompositor) -> None:
    comp.update_state(desktop=capture_pil(cdp, (DESKTOP_W, DESKTOP_H)))


def _snap_m(cdp: CDPDriver, comp: NarratedCompositor) -> None:
    comp.update_state(mobile=capture_pil(cdp, (MOBILE_W, MOBILE_H)))


def run_narrated_demo(out_mp4: Path) -> None:
    ffmpeg_exe = imageio_ffmpeg.get_ffmpeg_exe() or shutil.which("ffmpeg")
    if not ffmpeg_exe:
        raise RuntimeError("ffmpeg binary not found")
    ffprobe_exe = _find_ffprobe(ffmpeg_exe)

    work_dir = Path(tempfile.mkdtemp(prefix="cp_narr_work_"))
    audio_dir = work_dir / "audio"
    audio_dir.mkdir(parents=True, exist_ok=True)

    srv = NarratedDemoServer()
    seed = srv.start()
    desktop_cdp = CDPDriver(seed.base_url)
    mobile_cdp = CDPDriver(seed.base_url)

    try:
        # 1. Pre-render and measure exact narration beat timings
        timed_beats, master_audio_wav, total_audio_sec = prepare_timed_narration(
            audio_dir, ffmpeg_exe, ffprobe_exe
        )
        beat_by_id = {b.id: b for b in timed_beats}

        # Prepare demo upload artifacts inside temp work_dir
        lecture2_transcript_file = work_dir / "CS101_Lecture2_Recursion_GraphSearch.txt"
        lecture2_transcript_file.write_text(
            "[00:00] Welcome back to CS101 Lecture 2: Recursion and Graph Search.\n"
            "[05:20] Every recursive function requires a clear base case to guarantee termination.\n"
            "[14:45] Breadth-First Search uses a FIFO queue to find shortest paths in unweighted graphs.\n"
            "[24:10] Depth-First Search explores branches deeply via recursion stack or explicit stack.\n"
            "[31:00] Action item: complete Ex1 Recursive Binary Search and BFS before Friday.\n",
            encoding="utf-8",
        )

        comp_draft_py = work_dir / "noa_practice_draft.py"
        comp_draft_py.write_text(
            "from collections import deque\n\n"
            "def binary_search_rec(arr: list[int], target: int, lo: int = 0, hi: int | None = None) -> int:\n"
            "    if hi is None:\n"
            "        hi = len(arr) - 1\n"
            "    if lo > hi:\n"
            "        return -1\n"
            "    mid = (lo + hi) // 2\n"
            "    if arr[mid] == target:\n"
            "        return mid\n"
            "    if arr[mid] < target:\n"
            "        return binary_search_rec(arr, target, mid + 1, hi)\n"
            "    return binary_search_rec(arr, target, lo, mid - 1)\n",
            encoding="utf-8",
        )

        final_py = work_dir / "noa_final_submission.py"
        final_py.write_text(
            "from collections import deque\n\n"
            "def binary_search_rec(arr: list[int], target: int, lo: int = 0, hi: int | None = None) -> int:\n"
            "    \"\"\"Recursive binary search with explicit O(log n) bounds.\"\"\"\n"
            "    if hi is None:\n"
            "        hi = len(arr) - 1\n"
            "    if lo > hi:\n"
            "        return -1\n"
            "    mid = (lo + hi) // 2\n"
            "    if arr[mid] == target:\n"
            "        return mid\n"
            "    if arr[mid] < target:\n"
            "        return binary_search_rec(arr, target, mid + 1, hi)\n"
            "    return binary_search_rec(arr, target, lo, mid - 1)\n\n"
            "def bfs_shortest_path(graph: dict[int, list[int]], start: int, goal: int) -> int:\n"
            "    \"\"\"Returns shortest unweighted hop distance from start to goal, or -1.\"\"\"\n"
            "    queue: deque[tuple[int, int]] = deque([(start, 0)])\n"
            "    visited: set[int] = {start}\n"
            "    while queue:\n"
            "        node, dist = queue.popleft()\n"
            "        if node == goal:\n"
            "            return dist\n"
            "        for nxt in graph.get(node, []):\n"
            "            if nxt not in visited:\n"
            "                visited.add(nxt)\n"
            "                queue.append((nxt, dist + 1))\n"
            "    return -1\n",
            encoding="utf-8",
        )

        desktop_cdp.start()
        mobile_cdp.start()
        set_cdp_viewport(desktop_cdp, DESKTOP_W, DESKTOP_H, scale=1, mobile=False)
        set_cdp_viewport(mobile_cdp, MOBILE_W, MOBILE_H, scale=2, mobile=True)

        raw_video_mp4 = work_dir / "video_only.mp4"
        comp = NarratedCompositor(raw_video_mp4, ffmpeg_exe, total_audio_sec)

        # ------------------------------------------------------------------
        # BEAT 01: Opening Title + Lecturer Login + Clean Dashboard Overview
        # ------------------------------------------------------------------
        b1 = beat_by_id["01_intro_login"]
        print(f"[Beat 01] {b1.id} -> target {b1.end_sec:.2f}s")
        comp.update_state(chapter=b1.chapter, caption=b1.caption)
        comp.render_card_frames(
            "ClassPulse Walkthrough",
            "End-to-End Classroom Attendance, AI Summaries & Guided Grading",
            [
                "Exact Course Numbers & Lecturer Enrollment Approvals",
                "Rotating Lecture QR + SCRFD Headcount Discrepancy Detection",
                "Structured Lecture Summaries with Draft / Publish Controls",
                "Remote Student Portal, Comparative Practice Rank & Final Submit",
            ],
            seconds=2.2,
        )

        comp.update_state(layout="desktop")
        desktop_cdp.goto(f"{seed.base_url}/teacher/login", settle_s=0.5)
        _snap_d(desktop_cdp, comp)
        comp.hold(0.5)

        cx, cy = type_el_js(desktop_cdp, "#login-identifier", seed.lecturer_user)
        comp.move_and_click("desktop", cx, cy, glide_s=0.22)
        type_el_js(desktop_cdp, "#login-password", seed.lecturer_pw)
        _snap_d(desktop_cdp, comp)

        cx, cy = click_el_js(desktop_cdp, "#btn-lecturer-signin")
        comp.move_and_click("desktop", cx, cy, glide_s=0.22)
        desktop_cdp.wait_for("location.pathname === '/teacher' && !!document.getElementById('tab-overview')", timeout=8.0)
        time.sleep(0.45)
        _snap_d(desktop_cdp, comp)
        comp.pad_to_beat_end(b1)

        # ------------------------------------------------------------------
        # BEAT 02: Custom Course Number CS305 + Student Join Request + Approval
        # ------------------------------------------------------------------
        b2 = beat_by_id["02_course_num_and_join"]
        print(f"[Beat 02] {b2.id} -> target {b2.end_sec:.2f}s")
        comp.update_state(chapter=b2.chapter, caption=b2.caption)

        cx, cy = get_el_center(desktop_cdp, "button[onclick*='modal-new-course']")
        desktop_cdp.eval_js("openModal('modal-new-course')")
        time.sleep(0.25)
        comp.move_and_click("desktop", cx, cy, glide_s=0.24)
        type_el_js(desktop_cdp, "#new-course-code", "CS305")
        type_el_js(desktop_cdp, "#new-course-name", "Software Architecture")
        _snap_d(desktop_cdp, comp)
        comp.hold(0.5)

        cx, cy = click_el_js(desktop_cdp, "button[onclick*='submitNewCourse']")
        comp.move_and_click("desktop", cx, cy, glide_s=0.22)
        time.sleep(0.7)
        desktop_cdp.eval_js("switchTab('tab-course-details')")
        time.sleep(0.35)
        _snap_d(desktop_cdp, comp)
        comp.hold(0.8)

        # Navigate lecturer back to CS101 Lecture 2
        desktop_cdp.goto(
            f"{seed.base_url}/teacher?course_id={seed.cs101_id}&session_id={seed.cs101_l2_id}",
            settle_s=0.5,
        )

        # Dual layout: Yael Mizrahi logs in on Mobile and requests to join CS101 by Course Number
        mobile_cdp.goto(f"{seed.base_url}/student", settle_s=0.5)
        type_el_js(mobile_cdp, "#login-name", seed.yael_name)
        type_el_js(mobile_cdp, "#login-sid", seed.yael_sid)
        click_el_js(mobile_cdp, "#btn-student-signin")
        mobile_cdp.wait_for("!!document.getElementById('join-course-code-input')", timeout=8.0)
        time.sleep(0.35)
        comp.update_state(layout="dual")
        _snap_d(desktop_cdp, comp)
        _snap_m(mobile_cdp, comp)

        cx, cy = type_el_js(mobile_cdp, "#join-course-code-input", "CS101")
        comp.move_and_click("mobile", cx, cy, glide_s=0.22)
        cx, cy = click_el_js(mobile_cdp, "#btn-join-course-by-number")
        comp.move_and_click("mobile", cx, cy, glide_s=0.22)
        mobile_cdp.wait_for(
            "document.getElementById('join-course-feedback').innerText.length > 0",
            timeout=6.0,
        )
        time.sleep(0.55)
        _snap_m(mobile_cdp, comp)

        # Lecturer sidebar badge lights up; open Roster & click Approve
        desktop_cdp.eval_js("loadPendingEnrollmentRequests()")
        time.sleep(0.45)
        _snap_d(desktop_cdp, comp)
        comp.hold(0.6)

        cx, cy = get_el_center(desktop_cdp, "#tab-btn-course-monitor")
        desktop_cdp.eval_js("switchTab('tab-course-roster')")
        time.sleep(0.35)
        comp.move_and_click("desktop", cx, cy, glide_s=0.22)
        desktop_cdp.wait_for(
            "document.getElementById('pending-enrollment-requests-list').innerText.includes('Yael Mizrahi')",
            timeout=6.0,
        )
        _snap_d(desktop_cdp, comp)
        comp.hold(0.6)

        cx, cy = click_el_js(desktop_cdp, "#pending-enrollment-requests-list button[onclick*='approveEnrollmentRequest']")
        comp.move_and_click("desktop", cx, cy, glide_s=0.22)
        time.sleep(0.55)
        _snap_d(desktop_cdp, comp)
        comp.pad_to_beat_end(b2)

        # ------------------------------------------------------------------
        # BEAT 03: QR Attendance Check-In (Noa Levi into CS101 Lecture 2)
        # ------------------------------------------------------------------
        b3 = beat_by_id["03_qr_checkin"]
        print(f"[Beat 03] {b3.id} -> target {b3.end_sec:.2f}s")
        comp.update_state(layout="dual", chapter=b3.chapter, caption=b3.caption)

        desktop_cdp.eval_js("switchTab('tab-attendance')")
        time.sleep(0.4)
        qr_json = desktop_cdp.eval_js(f"fetch('/api/sessions/{seed.cs101_l2_code}/qr').then(r => r.json())")
        checkin_url = qr_json["url"]

        # Clear mobile cookies so Noa performs fresh QR check-in
        mobile_cdp.cdp_call("Network.clearBrowserCookies")
        mobile_cdp.goto(checkin_url, settle_s=0.5)
        _snap_d(desktop_cdp, comp)
        _snap_m(mobile_cdp, comp)
        comp.hold(0.6)

        cx, cy = type_el_js(mobile_cdp, "#full_name", seed.noa_name)
        comp.move_and_click("mobile", cx, cy, glide_s=0.22)
        cx, cy = type_el_js(mobile_cdp, "#student_id_number", seed.noa_sid)
        comp.move_and_click("mobile", cx, cy, glide_s=0.20)
        _snap_m(mobile_cdp, comp)

        cx, cy = click_el_js(mobile_cdp, "button[type='submit']")
        comp.move_and_click("mobile", cx, cy, glide_s=0.20)
        mobile_cdp.wait_for("!!document.getElementById('verified-checkin-banner')", timeout=8.0)
        time.sleep(0.55)
        desktop_cdp.eval_js("if (typeof refreshAttendance === 'function') refreshAttendance(); true;")
        time.sleep(0.35)
        _snap_m(mobile_cdp, comp)
        _snap_d(desktop_cdp, comp)
        comp.pad_to_beat_end(b3)

        # ------------------------------------------------------------------
        # BEAT 04: Classroom AI Head-Counting + Discrepancy Alert in Roster
        # ------------------------------------------------------------------
        b4 = beat_by_id["04_headcount_discrepancy"]
        print(f"[Beat 04] {b4.id} -> target {b4.end_sec:.2f}s")
        comp.update_state(layout="desktop", chapter=b4.chapter, caption=b4.caption)

        classroom_photo = ROOT_DIR / "data" / "samples" / "classroom_ariel.jpg"
        set_file_input(desktop_cdp, "#photo-input", classroom_photo)
        cx, cy = click_el_js(desktop_cdp, "#btn-upload-headcount")
        comp.move_and_click("desktop", cx, cy, glide_s=0.22)
        ok_hc = desktop_cdp.wait_for(
            "Number(document.getElementById('snap-auto-count').textContent || '0') > 0",
            timeout=25.0,
        )
        if not ok_hc:
            raise RuntimeError("Headcount upload did not populate snap-auto-count")
        time.sleep(0.55)
        scroll_into_view(desktop_cdp, "#attendance-discrepancy-banner", block="center")
        _snap_d(desktop_cdp, comp)
        comp.hold(2.6)

        # Also showcase the warning + red '!' badge inside Attendance Roster
        cx, cy = click_el_js(desktop_cdp, "#tab-btn-course-monitor")
        comp.move_and_click("desktop", cx, cy, glide_s=0.22)
        time.sleep(0.45)
        scroll_into_view(desktop_cdp, "#roster-discrepancy-banner", block="center")
        _snap_d(desktop_cdp, comp)
        comp.pad_to_beat_end(b4)

        # ------------------------------------------------------------------
        # BEAT 05: Lecture Recording Upload -> Structured Summary Cards (Draft)
        # ------------------------------------------------------------------
        b5 = beat_by_id["05_summary_upload"]
        print(f"[Beat 05] {b5.id} -> target {b5.end_sec:.2f}s")
        comp.update_state(layout="desktop", chapter=b5.chapter, caption=b5.caption)

        desktop_cdp.eval_js("switchTab('tab-ai-lectures')")
        time.sleep(0.4)
        _snap_d(desktop_cdp, comp)

        set_file_input(desktop_cdp, "#lecture-media-input", lecture2_transcript_file)
        cx, cy = click_el_js(desktop_cdp, "#btn-upload-lecture-summary")
        comp.move_and_click("desktop", cx, cy, glide_s=0.24)
        desktop_cdp.wait_for(
            "document.getElementById('lecture-artifacts-list').innerText.includes('Draft')",
            timeout=10.0,
        )
        time.sleep(0.45)
        scroll_into_view(desktop_cdp, "#lecture-artifacts-list", block="start")
        _snap_d(desktop_cdp, comp)
        comp.pad_to_beat_end(b5)

        # ------------------------------------------------------------------
        # BEAT 06: Draft Privacy Shield -> Publish to Students (with Unpublish/Delete)
        # ------------------------------------------------------------------
        b6 = beat_by_id["06_publish_control"]
        print(f"[Beat 06] {b6.id} -> target {b6.end_sec:.2f}s")
        comp.update_state(layout="desktop", chapter=b6.chapter, caption=b6.caption)

        cx, cy = click_el_js(desktop_cdp, "#lecture-artifacts-list button[data-publish-artifact]")
        comp.move_and_click("desktop", cx, cy, glide_s=0.24)
        desktop_cdp.wait_for(
            "document.getElementById('lecture-artifacts-list').innerText.includes('Published to Students')",
            timeout=8.0,
        )
        time.sleep(0.4)
        _snap_d(desktop_cdp, comp)
        comp.pad_to_beat_end(b6)

        # ------------------------------------------------------------------
        # BEAT 07: Remote Student Login (Name + ID only), Greeting & Tree + Dots
        # ------------------------------------------------------------------
        b7 = beat_by_id["07_student_remote_nav"]
        print(f"[Beat 07] {b7.id} -> target {b7.end_sec:.2f}s")
        comp.update_state(layout="desktop", chapter=b7.chapter, caption=b7.caption)

        desktop_cdp.cdp_call("Network.clearBrowserCookies")
        desktop_cdp.goto(f"{seed.base_url}/student", settle_s=0.5)
        desktop_cdp.eval_js("localStorage.clear()")
        desktop_cdp.goto(f"{seed.base_url}/student", settle_s=0.5)
        _snap_d(desktop_cdp, comp)

        cx, cy = type_el_js(desktop_cdp, "#login-name", seed.noa_name)
        comp.move_and_click("desktop", cx, cy, glide_s=0.22)
        type_el_js(desktop_cdp, "#login-sid", seed.noa_sid)
        cx, cy = click_el_js(desktop_cdp, "#btn-student-signin")
        comp.move_and_click("desktop", cx, cy, glide_s=0.20)
        desktop_cdp.wait_for("document.getElementById('courses-list').children.length >= 2", timeout=8.0)
        time.sleep(0.45)

        # Light up multi-level notification dots across Courses, Assignments & Summaries cleanly!
        desktop_cdp.eval_js(
            f"""(() => {{
                if (window.ClassPulseNotify) {{
                    window.ClassPulseNotify.markUnread({{
                        key: 'qa:{seed.cs101_id}:{seed.cs101_l2_id}:demo',
                        kind: 'qa',
                        courseId: {seed.cs101_id},
                        sessionCode: '{seed.cs101_l2_code}',
                        itemId: 'demo-qa'
                    }});
                    window.ClassPulseNotify.markUnread({{
                        key: 'assignment:{seed.cs101_id}:{seed.cs101_assign1_id}',
                        kind: 'assignment',
                        courseId: {seed.cs101_id},
                        itemId: {seed.cs101_assign1_id}
                    }});
                    window.ClassPulseNotify.markUnread({{
                        key: 'summary:{seed.cs101_id}:new',
                        kind: 'summary',
                        courseId: {seed.cs101_id},
                        sessionCode: '{seed.cs101_l2_code}',
                        itemId: 'new-sum'
                    }});
                }}
                if (typeof renderStudentSidebarTree === 'function') renderStudentSidebarTree();
                if (typeof renderStudentNotificationDots === 'function') renderStudentNotificationDots();
                true;
            }})()"""
        )
        time.sleep(0.35)
        _snap_d(desktop_cdp, comp)
        comp.pad_to_beat_end(b7)

        # ------------------------------------------------------------------
        # BEAT 08: Courses Log -> Attendance %, Lecture 2 -> Past Polls & Offline Q
        # ------------------------------------------------------------------
        b8 = beat_by_id["08_courses_log_offline_q"]
        print(f"[Beat 08] {b8.id} -> target {b8.end_sec:.2f}s")
        comp.update_state(layout="desktop", chapter=b8.chapter, caption=b8.caption)

        cx, cy = click_el_js(desktop_cdp, f"[data-course-card-id='{seed.cs101_id}'], button[onclick*='openCourseHub({seed.cs101_id})']")
        comp.move_and_click("desktop", cx, cy, glide_s=0.22)
        time.sleep(0.4)
        _snap_d(desktop_cdp, comp)
        comp.hold(0.7)

        # Drill into Lectures list -> Lecture 2
        cx, cy = click_el_js(desktop_cdp, "#hub-tile-lectures")
        comp.move_and_click("desktop", cx, cy, glide_s=0.20)
        time.sleep(0.35)
        cx, cy = click_el_js(desktop_cdp, f"button[onclick*='{seed.cs101_l2_code}']")
        comp.move_and_click("desktop", cx, cy, glide_s=0.20)
        desktop_cdp.wait_for("document.getElementById('screen-lecture').classList.contains('is-active') || document.getElementById('student-offline-body') !== null", timeout=6.0)
        time.sleep(0.45)

        scroll_into_view(desktop_cdp, "#student-past-polls", block="center")
        _snap_d(desktop_cdp, comp)
        comp.hold(0.8)

        cx, cy = type_el_js(desktop_cdp, "#student-offline-body", "Could you review the recursion base case in office hours?")
        comp.move_and_click("desktop", cx, cy, glide_s=0.20)
        cx, cy = click_el_js(desktop_cdp, "#btn-send-offline-q")
        comp.move_and_click("desktop", cx, cy, glide_s=0.20)
        time.sleep(0.45)
        scroll_into_view(desktop_cdp, "#lecture-offline-section", block="center")
        _snap_d(desktop_cdp, comp)
        comp.pad_to_beat_end(b8)

        # ------------------------------------------------------------------
        # BEAT 09: Grouped Summaries + Comparative Practice Submission & Rank
        # ------------------------------------------------------------------
        b9 = beat_by_id["09_summaries_comparative_rank"]
        print(f"[Beat 09] {b9.id} -> target {b9.end_sec:.2f}s")
        comp.update_state(layout="desktop", chapter=b9.chapter, caption=b9.caption)

        cx, cy = click_el_js(desktop_cdp, "#nav-student-summaries")
        comp.move_and_click("desktop", cx, cy, glide_s=0.22)
        time.sleep(0.55)
        _snap_d(desktop_cdp, comp)
        comp.hold(1.1)

        cx, cy = click_el_js(desktop_cdp, "#nav-student-assignments")
        comp.move_and_click("desktop", cx, cy, glide_s=0.22)
        time.sleep(0.45)
        cx, cy = click_el_js(
            desktop_cdp,
            f"button[onclick*='openAssignmentDetail({seed.cs101_id}, {seed.cs101_assign1_id})']",
        )
        comp.move_and_click("desktop", cx, cy, glide_s=0.22)
        time.sleep(0.45)

        aid = seed.cs101_assign1_id
        set_file_input(desktop_cdp, f"#hw-comp-file-{aid}", comp_draft_py)
        cx, cy = click_el_js(desktop_cdp, f"#hw-comp-btn-{aid}")
        comp.move_and_click("desktop", cx, cy, glide_s=0.22)
        desktop_cdp.wait_for(
            f"document.getElementById('screen-assignment-detail').innerText.includes('Place')",
            timeout=10.0,
        )
        time.sleep(0.45)
        scroll_into_view(desktop_cdp, f"#hw-comparative-card-{aid}", block="center")
        _snap_d(desktop_cdp, comp)
        comp.pad_to_beat_end(b9)

        # ------------------------------------------------------------------
        # BEAT 10: Official Final Submission -> Lecturer Sees ONLY Final Files + Score
        # ------------------------------------------------------------------
        b10 = beat_by_id["10_final_submit_lecturer"]
        print(f"[Beat 10] {b10.id} -> target {b10.end_sec:.2f}s")
        comp.update_state(layout="desktop", chapter=b10.chapter, caption=b10.caption)

        set_file_input(desktop_cdp, f"#hw-final-file-{aid}", final_py)
        scroll_into_view(desktop_cdp, f"#hw-final-card-{aid}", block="center")
        cx, cy = click_el_js(desktop_cdp, f"#hw-final-btn-{aid}")
        comp.move_and_click("desktop", cx, cy, glide_s=0.22)
        desktop_cdp.wait_for(
            f"document.getElementById('hw-final-card-{aid}').innerText.includes('Final Score') || "
            f"document.getElementById('hw-final-status-{aid}').innerText.includes('Final')",
            timeout=10.0,
        )
        time.sleep(0.5)
        _snap_d(desktop_cdp, comp)
        comp.hold(0.8)

        # Switch back cleanly to Dr. Daniel Cohen's Homework tab -> strictly Final submissions & AI Score
        desktop_cdp.cdp_call("Network.clearBrowserCookies")
        desktop_cdp.goto(f"{seed.base_url}/teacher/login", settle_s=0.5)
        type_el_js(desktop_cdp, "#login-identifier", seed.lecturer_user)
        type_el_js(desktop_cdp, "#login-password", seed.lecturer_pw)
        desktop_cdp.eval_js("handleLecturerSignIn()")
        desktop_cdp.wait_for("location.pathname === '/teacher' && !!document.getElementById('tab-overview')", timeout=8.0)
        desktop_cdp.goto(
            f"{seed.base_url}/teacher?course_id={seed.cs101_id}&session_id={seed.cs101_l2_id}",
            settle_s=0.5,
        )
        desktop_cdp.eval_js("switchTab('tab-homework')")
        desktop_cdp.wait_for(
            "document.getElementById('hw-submissions-list').innerText.includes('Noa Levi')",
            timeout=8.0,
        )
        time.sleep(0.45)
        scroll_into_view(desktop_cdp, "#hw-submissions-list", block="center")
        _snap_d(desktop_cdp, comp)
        comp.pad_to_beat_end(b10)

        # ------------------------------------------------------------------
        # BEAT 11: Outro Summary Card
        # ------------------------------------------------------------------
        b11 = beat_by_id["11_outro"]
        print(f"[Beat 11] {b11.id} -> target {b11.end_sec:.2f}s")
        comp.update_state(chapter=b11.chapter, caption=b11.caption)
        rem_sec = max(1.5, (int(round(b11.end_sec * FPS)) - comp.frames_written) / FPS)
        comp.render_card_frames(
            "ClassPulse",
            "Built for Seamless Classroom & Remote University Collaboration",
            [
                "Exact Course Numbers, Pending Roster Approvals & Discrepancy Alerts",
                "Privacy-First Draft -> Publish Lecture Summaries & Transcripts",
                "Remote Student Navigation Tree with Unread Notification Dots",
                "Private Comparative Practice Rank + Official Final Grading for Lecturers",
            ],
            seconds=rem_sec,
        )
        comp.pad_to_beat_end(b11)

        video_dur = comp.close()
        print(f"[Video] Silent track rendered: {video_dur:.2f}s ({comp.frames_written} frames)")

        # Mux audio + video cleanly with +faststart
        out_mp4.parent.mkdir(parents=True, exist_ok=True)
        subprocess.run(
            [
                ffmpeg_exe,
                "-y",
                "-i", str(raw_video_mp4),
                "-i", str(master_audio_wav),
                "-map", "0:v:0",
                "-map", "1:a:0",
                "-c:v", "copy",
                "-c:a", "aac",
                "-b:a", "192k",
                "-movflags", "+faststart",
                "-shortest",
                str(out_mp4),
            ],
            check=True,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        final_dur = _ffprobe_duration(ffprobe_exe, out_mp4)
        print(f"[Done] Narrated demo exported to {out_mp4} ({final_dur:.2f}s)")
        if not (MIN_TOTAL_S <= final_dur <= 120.0):
            raise RuntimeError(f"Final duration {final_dur:.2f}s outside 90s-120s window")

    finally:
        for obj in (desktop_cdp, mobile_cdp):
            try:
                obj.close()
            except Exception:
                pass
        srv.stop()
        shutil.rmtree(work_dir, ignore_errors=True)


def main() -> int:
    out_mp4 = ROOT_DIR / "demo_walkthrough.mp4"
    run_narrated_demo(out_mp4)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
