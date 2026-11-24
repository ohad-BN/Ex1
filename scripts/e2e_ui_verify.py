#!/usr/bin/env python3
"""ClassPulse Opaque-Box E2E + Headless Chromium CDP Verification Suite.

Covers Tiers 1-4 per docs/UI_OVERHAUL_DESIGN_PLAN.md (Sections 3, 4, 6, 7):
  - Tier 1: Feature coverage (all 8 views/routes, 6 teacher tabs, 4 modals, 3 student views),
            template parity check, and runtime/console/network cleanliness.
  - Tier 2: Multi-viewport boundary geometry across 7 viewports (overflow, clipped controls,
            sibling overlap, broken images, <=980px drawer toggle) & check-in 403/404/410 errors.
  - Tier 3: Live interactive E2E flows (login, course/lecture switch, poll create+vote,
            student Q&A post+upvote + popout, HW review & security audit modals, .xlsx/.csv
            reports, student onboarding), R2 exclusion & dark-hex scan, and light-theme audit.
  - Tier 4: Real-world classroom showcase PNG screenshot capture & PIL validation.

Usage:
  python scripts/e2e_ui_verify.py --smoke-only --allow-theme-fail
  python scripts/e2e_ui_verify.py --tier all --allow-theme-fail
  python scripts/e2e_ui_verify.py --tier 3
"""

from __future__ import annotations

import argparse
import atexit
import base64
from dataclasses import dataclass, field
import json
import os
from pathlib import Path
import re
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import time
from typing import Any, Callable, Optional
from urllib.parse import urlparse
import urllib.request

from PIL import Image, ImageStat
import requests
from websockets.sync.client import ClientConnection, connect
from werkzeug.serving import make_server

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from app import create_app  # noqa: E402
from app.config import Config  # noqa: E402
from app.models import ClassSession, HeadcountSnapshot, Student  # noqa: E402
from app.services.attendance_service import AttendanceService  # noqa: E402
from app.services.chat_service import ChatService  # noqa: E402
from app.services.course_service import CourseService  # noqa: E402
from app.services.face_counter import (  # noqa: E402
    BoundingBox,
    FaceDetector,
    read_image_unicode,
    write_image_unicode,
)
from app.services.homework_pipeline import HomeworkReviewService  # noqa: E402
from app.services.lecture_pipeline import LectureSummarizerService  # noqa: E402
from app.services.llm_client import GeminiStudioClient  # noqa: E402
from app.services.poll_service import PollService  # noqa: E402


DEFAULT_CHROME_EXE = os.path.expandvars(
    r"%LOCALAPPDATA%\ms-playwright\chromium_headless_shell-1217"
    r"\chrome-headless-shell-win64\chrome-headless-shell.exe"
)

VIEWPORTS: dict[str, tuple[int, int, bool]] = {
    "desktop": (1440, 900, False),
    "tablet": (1024, 768, False),
    "projector_1080": (1920, 1080, False),
    "projector_720": (1280, 720, False),
    "mobile": (390, 844, True),
    "mobile_sm": (360, 740, True),
    "popout": (480, 800, True),
}

R2_FORBIDDEN_TERMS: list[str] = [
    "Sense",
    "Meditate",
    "Mindfulness",
    "Relaxation",
    "Breath of Balance",
    "Ease into Sleep",
    "Calm Flow Journey",
    "Mindful Moments",
    "Andrew",
    "Sara Lee",
    "Alex Morgan",
    "Daniel Cru",
    "My Sessions",
    "Popular Sessions",
    "Premium Access",
    "Shared Sessions",
    "Saved Practices",
]

R2_REGEX = re.compile(
    r"\b(?:" + "|".join(re.escape(t) for t in R2_FORBIDDEN_TERMS) + r")\b",
    re.IGNORECASE,
)

FORBIDDEN_DARK_HEXES: tuple[str, ...] = ("#0b1120", "#0f172a", "#151f32", "#1e293b")
FORBIDDEN_DARK_RGBS: tuple[str, ...] = (
    "rgb(11, 17, 32)",
    "rgb(15, 23, 42)",
    "rgb(21, 31, 50)",
    "rgb(30, 41, 59)",
)

THEME_TOKENS: tuple[str, ...] = (
    "--cp-bg-base",
    "--cp-ink",
    "--cp-coral",
    "--cp-indigo",
)

TEACHER_TABS: list[tuple[str, str]] = [
    ("tab-overview", "Overview"),
    ("tab-attendance", "Attendance & QR"),
    ("tab-polls", "Polls"),
    ("tab-chat", "Q&A"),
    ("tab-ai-lectures", "AI Summary"),
    ("tab-homework", "Homework"),
    ("tab-course-roster", "Roster"),
    ("tab-course-details", "Course Details"),
]

STUDENT_VIEWS: list[tuple[str, str]] = [
    ("nav-student-courses", "screen-courses"),
    ("nav-student-assignments", "screen-course-assignments"),
    ("nav-student-summaries", "screen-course-summaries"),
]


class StubFaceDetector(FaceDetector):
    """Deterministic fast detector stub avoiding ONNX weights in E2E runs."""

    def detect(self, image: Any) -> list[BoundingBox]:
        return [
            BoundingBox(
                label_id=i + 1,
                x1=40.0 + i * 70.0,
                y1=50.0,
                x2=90.0 + i * 70.0,
                y2=110.0,
                confidence=0.95,
                source="auto",
            )
            for i in range(3)
        ]

    @staticmethod
    def render_annotated_image(
        image_path: str | Path,
        boxes: list[BoundingBox],
        out_path: str | Path,
    ) -> str:
        img = read_image_unicode(image_path)
        return write_image_unicode(out_path, img)


@dataclass
class SeedContext:
    base_url: str
    lecturer_username: str
    lecturer_password: str
    lecturer_cookie: str
    lecturer2_cookie: str
    alice_device_cookie: str
    alice_student_id: int
    cs101_id: int
    cs204_id: int
    ariel1_id: int
    ariel1_code: str
    ariel2_code: str
    algo01_code: str
    closed1_code: str
    projector_token: str
    active_poll_id: int
    seeded_snap_id: int
    fresh_checkin_url: Callable[[str], str]


@dataclass
class AuditResults:
    passes: list[str] = field(default_factory=list)
    failures: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    screenshots: list[Path] = field(default_factory=list)
    tokens_by_view: dict[str, dict[str, str]] = field(default_factory=dict)

    def ok(self, msg: str) -> None:
        self.passes.append(msg)
        print(f"[PASS] {msg}")

    def fail(self, msg: str) -> None:
        self.failures.append(msg)
        print(f"[FAIL] {msg}")

    def theme_issue(self, msg: str, allow_theme_fail: bool) -> None:
        if allow_theme_fail:
            w = f"[WARN (baseline --allow-theme-fail)] {msg}"
            self.warnings.append(w)
            print(w)
        else:
            self.fail(msg)

    def warn(self, msg: str) -> None:
        w = f"[WARN] {msg}"
        self.warnings.append(w)
        print(w)


def _find_free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


def _find_chrome_executable() -> Path:
    candidate = Path(DEFAULT_CHROME_EXE)
    if candidate.is_file():
        return candidate
    ms_pw = Path(os.path.expandvars(r"%LOCALAPPDATA%\ms-playwright"))
    if ms_pw.exists():
        matches = list(ms_pw.rglob("chrome-headless-shell.exe"))
        if matches:
            return matches[0]
    raise FileNotFoundError(
        f"chrome-headless-shell.exe not found at {DEFAULT_CHROME_EXE}"
    )


class EphemeralServer:
    """Isolated temp-SQLite ClassPulse Flask/SocketIO server with clean teardown."""

    def __init__(self) -> None:
        self.tmp_dir = Path(tempfile.mkdtemp(prefix="cp_e2e_srv_"))
        self.cfg = Config(
            SECRET_KEY="e2e-classpulse-test-secret-key-32bytes",
            DATABASE_URL=f"sqlite:///{(self.tmp_dir / 'e2e.db').as_posix()}",
            UPLOAD_DIR=self.tmp_dir / "uploads",
            REPORT_DIR=self.tmp_dir / "reports",
            GEMINI_API_KEY="",
            ENABLE_CSRF=True,
        )
        self.app = create_app(
            self.cfg,
            face_detector=StubFaceDetector(),
            llm_client=GeminiStudioClient(api_key=""),
        )
        self.srv = make_server("127.0.0.1", 0, self.app, threaded=True)
        self.port = int(self.srv.server_port)
        self.base_url = f"http://127.0.0.1:{self.port}"
        self.cfg.PUBLIC_BASE_URL = self.base_url
        self._thread = threading.Thread(
            target=self.srv.serve_forever,
            name="cp-e2e-server",
            daemon=True,
        )
        self.server_errors: list[str] = []
        self._install_5xx_tracker()

    def _install_5xx_tracker(self) -> None:
        errors = self.server_errors

        @self.app.after_request
        def _track_5xx(resp):
            if resp.status_code >= 500:
                errors.append(f"HTTP {resp.status_code}")
            return resp

    def start(self) -> SeedContext:
        self._thread.start()
        deadline = time.monotonic() + 10.0
        while time.monotonic() < deadline:
            try:
                r = requests.get(f"{self.base_url}/healthz", timeout=1.0)
                if r.status_code == 200:
                    break
            except Exception:
                pass
            time.sleep(0.1)
        else:
            raise RuntimeError("Ephemeral ClassPulse server did not become healthy")
        return self._seed_data()

    def _seed_data(self) -> SeedContext:
        with self.app.app_context():
            db = self.app.extensions["db_session"]
            qr_svc = self.app.extensions["qr_service"]
            device_mgr = self.app.extensions["device_mgr"]

            course_svc = CourseService(db=db)
            att_svc = AttendanceService(
                db=db, qr_service=qr_svc, device_mgr=device_mgr
            )
            poll_svc = PollService(db=db)
            chat_svc = ChatService(db=db)
            lec_svc = LectureSummarizerService(
                db=db,
                llm_client=self.app.extensions["llm_client"],
                upload_dir=self.cfg.UPLOAD_DIR,
            )
            hw_svc = HomeworkReviewService(
                db=db, llm_client=self.app.extensions["llm_client"]
            )

            lecturer = course_svc.register_lecturer(
                full_name="Prof. Boaz Ben-Moshe",
                username="boaz",
                email="boaz@ariel.ac.il",
                password="ClassPulse2026!",
                confirm_password="ClassPulse2026!",
                department="Computer Science",
            )

            # Seed CS101 first so CS204 gets higher Course.id (default on /teacher)
            cs101 = course_svc.create_course(
                course_code="CS101",
                name="Algorithms & Data Structures",
                department="Computer Science",
                semester="Fall 2026",
                lecturer_name=lecturer.full_name,
                lecturer_id=lecturer.id,
                initial_lecture_title="Lecture 1 — Graphs & Complexity",
            )
            algo_sess = (
                db.query(ClassSession)
                .filter_by(course_id=cs101.id)
                .order_by(ClassSession.id.asc())
                .first()
            )
            assert algo_sess is not None
            algo_sess.session_code = "ALGO01"
            db.commit()

            closed_sess = att_svc.create_session(
                title="Lecture 2 — Closed Archive",
                session_code="CLOSED1",
                course_id=cs101.id,
            )
            closed_sess.attendance_open = False
            db.commit()

            # Seed CS204 second; rename initial session to ARIEL2, then create ARIEL1 newest
            cs204 = course_svc.create_course(
                course_code="CS204",
                name="Object-Oriented Programming & SE",
                department="Computer Science",
                semester="Fall 2026",
                lecturer_name=lecturer.full_name,
                lecturer_id=lecturer.id,
                initial_lecture_title="Lecture 1 — SOLID & Patterns",
            )
            ariel2 = (
                db.query(ClassSession)
                .filter_by(course_id=cs204.id)
                .order_by(ClassSession.id.asc())
                .first()
            )
            assert ariel2 is not None
            ariel2.session_code = "ARIEL2"
            db.commit()

            ariel1 = att_svc.create_session(
                title="Lecture 2 — Adapter & Real-Time Architecture",
                session_code="ARIEL1",
                course_id=cs204.id,
            )

            # Enroll roster in CS204
            csv_roster = (
                "208111111, Alice Cohen, alice@ariel.ac.il\n"
                "208222222, Bob Levi, bob@ariel.ac.il\n"
                "208333333, Dana Mizrahi, dana@ariel.ac.il\n"
                "208444444, Ohad Ben Nun, ohad@ariel.ac.il\n"
            )
            course_svc.import_course_roster_csv(cs204.id, csv_roster)

            # Check Alice into ARIEL1
            qr_payload = qr_svc.build_qr_payload(self.base_url, "ARIEL1")
            _rec, alice, _is_new = att_svc.process_checkin(
                session_code="ARIEL1",
                bucket=int(qr_payload["bucket"]),
                token=str(qr_payload["sig"]),
                full_name="Alice Cohen",
                email="alice@ariel.ac.il",
                student_id_number="208111111",
                device_id="e2e-alice-dev-1",
            )
            bob = db.query(Student).filter_by(email="bob@ariel.ac.il").one()

            # HeadcountSnapshot with real Ariel classroom sample photos
            sample_raw = PROJECT_ROOT / "data" / "samples" / "classroom_ariel.jpg"
            sample_ann = PROJECT_ROOT / "data" / "samples" / "classroom_ariel_annotated.jpg"
            raw_dst = self.cfg.UPLOAD_DIR / "ariel1_raw.jpg"
            ann_dst = self.cfg.UPLOAD_DIR / "ariel1_ann.jpg"
            shutil.copyfile(sample_raw, raw_dst)
            shutil.copyfile(sample_ann, ann_dst)
            boxes = [
                {
                    "label_id": 1,
                    "x1": 120.0,
                    "y1": 140.0,
                    "x2": 180.0,
                    "y2": 210.0,
                    "confidence": 0.96,
                    "source": "auto",
                },
                {
                    "label_id": 2,
                    "x1": 280.0,
                    "y1": 135.0,
                    "x2": 340.0,
                    "y2": 205.0,
                    "confidence": 0.94,
                    "source": "auto",
                },
                {
                    "label_id": 3,
                    "x1": 440.0,
                    "y1": 150.0,
                    "x2": 500.0,
                    "y2": 220.0,
                    "confidence": 0.91,
                    "source": "manual",
                },
            ]
            snap = HeadcountSnapshot(
                session_id=ariel1.id,
                raw_image_path=str(raw_dst),
                annotated_image_path=str(ann_dst),
                auto_face_count=2,
                verified_face_count=3,
                boxes_json=json.dumps(boxes),
                lecturer_note="Seeded classroom snapshot",
            )
            db.add(snap)
            db.commit()

            # Polls: 1 DRAFT + 1 ACTIVE (with Bob's vote)
            poll_svc.create_poll(
                session_id=ariel1.id,
                question="Which SOLID principle prevents fat interfaces?",
                options=["Interface Segregation (ISP)", "Single Responsibility", "Open-Closed"],
                correct_index=0,
            )
            active_poll = poll_svc.create_poll(
                session_id=ariel1.id,
                question="Which pattern decouples FaceCounter & LLM adapters?",
                options=["Bridge / Adapter Pattern", "God Object", "Global Variables"],
                correct_index=0,
            )
            poll_svc.launch_poll(active_poll.id)
            poll_svc.cast_vote(active_poll.id, bob.id, 0)

            # 3 Chat Messages (1 pinned + upvoted Alice Question, 1 Bob Comment, 1 Lecturer Suggestion)
            m1 = chat_svc.post_message(
                session_id=ariel1.id,
                author_name="Alice Cohen",
                body="Can the Adapter pattern unify Gemini and local fallback?",
                category="Question",
                student_id=alice.id,
            )
            chat_svc.toggle_pin(m1.id)
            chat_svc.upvote_message(m1.id)
            chat_svc.post_message(
                session_id=ariel1.id,
                author_name="Bob Levi",
                body="Live QR rotation and headcount reconciliation look crisp.",
                category="Comment",
                student_id=bob.id,
            )
            chat_svc.post_message(
                session_id=ariel1.id,
                author_name=lecturer.full_name,
                body="Submit Ex2 architecture notes before Friday 18:00.",
                category="Suggestion",
                student_id=None,
            )

            # Lecture AI Artifact (built-in sample)
            lec_svc.summarize_session_recording(session_id=ariel1.id, use_sample=True)

            # Homework Assignment + 3 Sample Submissions
            hw_svc.seed_sample_course_assignment(cs204.id)

            # Second lecturer with 0 courses for setup=1 redirect & cross-tenant 403 checks
            lecturer2 = course_svc.register_lecturer(
                full_name="Dr. Zero Courses",
                username="zerocourses",
                email="zerocourses@ariel.ac.il",
                password="ClassPulse2026!",
                confirm_password="ClassPulse2026!",
                department="Computer Science",
            )

            lecturer_cookie = device_mgr.create_lecturer_cookie(
                lecturer.id, int(lecturer.token_version or 1)
            )
            lecturer2_cookie = device_mgr.create_lecturer_cookie(
                lecturer2.id, int(lecturer2.token_version or 1)
            )
            alice_cookie = device_mgr.create_device_cookie("e2e-alice-dev-1", alice.id)
            projector_token = device_mgr.create_projector_token("ARIEL1")

            def _fresh_checkin_url(code: str) -> str:
                return qr_svc.build_checkin_url(self.base_url, code)

            return SeedContext(
                base_url=self.base_url,
                lecturer_username="boaz",
                lecturer_password="ClassPulse2026!",
                lecturer_cookie=lecturer_cookie,
                lecturer2_cookie=lecturer2_cookie,
                alice_device_cookie=alice_cookie,
                alice_student_id=alice.id,
                cs101_id=cs101.id,
                cs204_id=cs204.id,
                ariel1_id=ariel1.id,
                ariel1_code="ARIEL1",
                ariel2_code="ARIEL2",
                algo01_code="ALGO01",
                closed1_code="CLOSED1",
                projector_token=projector_token,
                active_poll_id=active_poll.id,
                seeded_snap_id=snap.id,
                fresh_checkin_url=_fresh_checkin_url,
            )

    def stop(self) -> None:
        try:
            self.srv.shutdown()
        except Exception:
            pass
        try:
            self.srv.server_close()
        except Exception:
            pass
        try:
            self.app.extensions["task_manager"].shutdown(wait=True)
        except Exception:
            pass
        try:
            self.app.extensions["db_engine"].dispose()
        except Exception:
            pass
        for _ in range(10):
            shutil.rmtree(self.tmp_dir, ignore_errors=True)
            if not self.tmp_dir.exists():
                break
            time.sleep(0.15)


class CDPDriver:
    """Zero-install synchronous Chrome DevTools Protocol driver via websockets."""

    def __init__(self, base_url: str) -> None:
        self.base_url = base_url
        self.chrome_exe = _find_chrome_executable()
        self.port = _find_free_port()
        self.user_data_dir = Path(tempfile.mkdtemp(prefix="cp_e2e_chrome_"))
        self.proc: Optional[subprocess.Popen[bytes]] = None
        self._ws_ctx: Any = None
        self.ws: Optional[ClientConnection] = None
        self._cmd_id = 0
        self._events: list[dict[str, Any]] = []
        self._req_urls: dict[str, str] = {}
        self.dialogs_opened: list[str] = []
        atexit.register(self.close)

    def start(self) -> None:
        cmd = [
            str(self.chrome_exe),
            "--headless",
            f"--remote-debugging-port={self.port}",
            f"--user-data-dir={self.user_data_dir}",
            "--disable-gpu",
            "--no-first-run",
            "--no-default-browser-check",
        ]
        self.proc = subprocess.Popen(
            cmd,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        ws_url = self._discover_ws_url()
        self._ws_ctx = connect(ws_url, max_size=None, open_timeout=10)
        self.ws = self._ws_ctx.__enter__()
        self.cdp_call("Page.enable")
        self.cdp_call("Runtime.enable")
        self.cdp_call("Log.enable")
        self.cdp_call("Network.enable")
        self.cdp_call("Network.setCacheDisabled", {"cacheDisabled": True})

    def _discover_ws_url(self) -> str:
        deadline = time.monotonic() + 12.0
        list_url = f"http://127.0.0.1:{self.port}/json/list"
        new_url = f"http://127.0.0.1:{self.port}/json/new?about:blank"
        while time.monotonic() < deadline:
            try:
                with urllib.request.urlopen(list_url, timeout=1.5) as resp:
                    targets = json.loads(resp.read().decode("utf-8"))
                for t in targets:
                    if t.get("type") == "page" and t.get("webSocketDebuggerUrl"):
                        return str(t["webSocketDebuggerUrl"])
                req = urllib.request.Request(new_url, method="PUT")
                with urllib.request.urlopen(req, timeout=2.0) as resp:
                    created = json.loads(resp.read().decode("utf-8"))
                if created.get("webSocketDebuggerUrl"):
                    return str(created["webSocketDebuggerUrl"])
            except Exception:
                pass
            time.sleep(0.15)
        raise RuntimeError("Could not connect to HeadlessChrome CDP target")

    def _handle_event(self, msg: dict[str, Any]) -> None:
        self._events.append(msg)
        method = msg.get("method")
        params = msg.get("params") or {}
        if method == "Network.requestWillBeSent":
            req_id = params.get("requestId")
            url = (params.get("request") or {}).get("url")
            if req_id and url:
                self._req_urls[str(req_id)] = str(url)
        elif method == "Page.javascriptDialogOpening":
            dlg_msg = str(params.get("message", ""))
            self.dialogs_opened.append(dlg_msg)
            self._cmd_id += 1
            assert self.ws is not None
            self.ws.send(
                json.dumps(
                    {
                        "id": self._cmd_id,
                        "method": "Page.handleJavaScriptDialog",
                        "params": {"accept": True},
                    }
                )
            )

    def cdp_call(
        self,
        method: str,
        params: Optional[dict[str, Any]] = None,
        timeout: float = 12.0,
    ) -> dict[str, Any]:
        assert self.ws is not None
        self._cmd_id += 1
        cid = self._cmd_id
        payload: dict[str, Any] = {"id": cid, "method": method}
        if params is not None:
            payload["params"] = params
        self.ws.send(json.dumps(payload))
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            rem = max(0.05, deadline - time.monotonic())
            raw = self.ws.recv(timeout=rem)
            msg = json.loads(raw)
            if msg.get("id") == cid:
                if "error" in msg:
                    raise RuntimeError(f"CDP {method} error: {msg['error']}")
                return msg.get("result") or {}
            if "method" in msg:
                self._handle_event(msg)
        raise TimeoutError(f"CDP {method} timed out")

    def drain_events(self, wait_s: float = 0.4) -> list[dict[str, Any]]:
        assert self.ws is not None
        end = time.monotonic() + wait_s
        while time.monotonic() < end:
            rem = max(0.01, end - time.monotonic())
            try:
                raw = self.ws.recv(timeout=min(rem, 0.1))
                msg = json.loads(raw)
                if "method" in msg:
                    self._handle_event(msg)
            except TimeoutError:
                pass
        captured = list(self._events)
        self._events.clear()
        return captured

    def clear_buffers(self) -> None:
        self.drain_events(0.05)
        self._events.clear()
        self.dialogs_opened.clear()

    def set_viewport(self, name: str) -> tuple[int, int]:
        w, h, is_mobile = VIEWPORTS[name]
        self.cdp_call(
            "Emulation.setDeviceMetricsOverride",
            {
                "width": w,
                "height": h,
                "deviceScaleFactor": 1,
                "mobile": is_mobile,
            },
        )
        time.sleep(0.15)
        return w, h

    def set_role_cookies(
        self, role: str, seed: SeedContext
    ) -> None:
        self.cdp_call("Network.clearBrowserCookies")
        if role == "lecturer":
            self.cdp_call(
                "Network.setCookie",
                {
                    "name": "cp_lecturer",
                    "value": seed.lecturer_cookie,
                    "url": f"{self.base_url}/",
                    "httpOnly": True,
                    "sameSite": "Lax",
                },
            )
        elif role == "alice":
            self.cdp_call(
                "Network.setCookie",
                {
                    "name": "cp_device",
                    "value": seed.alice_device_cookie,
                    "url": f"{self.base_url}/",
                    "httpOnly": True,
                    "sameSite": "Lax",
                },
            )

    def goto(
        self,
        url: str,
        settle_s: float = 0.55,
        allow_statuses: tuple[int, ...] = (),
        results: Optional[AuditResults] = None,
        label: str = "",
    ) -> dict[str, Any]:
        self.clear_buffers()
        nav = self.cdp_call("Page.navigate", {"url": url})
        if nav.get("errorText"):
            raise RuntimeError(f"Navigation to {url} failed: {nav['errorText']}")

        assert self.ws is not None
        deadline = time.monotonic() + 10.0
        loaded = False
        while time.monotonic() < deadline:
            try:
                raw = self.ws.recv(timeout=0.15)
                msg = json.loads(raw)
                if "method" in msg:
                    self._handle_event(msg)
                    if msg["method"] == "Page.loadEventFired":
                        loaded = True
                        break
            except TimeoutError:
                pass
        if not loaded:
            raise TimeoutError(f"Timed out waiting for Page.loadEventFired on {url}")

        events = self.drain_events(settle_s)
        summary = self.classify_events(events, allow_statuses=allow_statuses)
        if results is not None:
            tag = label or url
            for err in summary["fatal_errors"]:
                results.fail(f"[{tag}] Runtime/Network error: {err}")
            for w in summary["cdn_warnings"]:
                results.warn(f"[{tag}] External CDN offline/blocked: {w}")
        return summary

    def classify_events(
        self,
        events: list[dict[str, Any]],
        allow_statuses: tuple[int, ...] = (),
    ) -> dict[str, Any]:
        fatal_errors: list[str] = []
        cdn_warnings: list[str] = []
        responses: list[tuple[str, int]] = []
        css_loaded = False

        # Collect local HTTP >=400 URLs reported by Network.responseReceived first
        # so Log.entryAdded does not double-count the same failed URL.
        resp_error_urls: set[str] = set()
        for ev in events:
            if ev.get("method") == "Network.responseReceived":
                resp = (ev.get("params") or {}).get("response") or {}
                r_url = str(resp.get("url") or "")
                status = int(resp.get("status") or 0)
                if r_url.startswith(self.base_url) and status >= 400:
                    resp_error_urls.add(r_url)

        for ev in events:
            method = ev.get("method")
            params = ev.get("params") or {}

            if method == "Runtime.exceptionThrown":
                details = params.get("exceptionDetails") or {}
                exc = details.get("exception") or {}
                desc = exc.get("description") or details.get("text") or str(details)
                fatal_errors.append(f"JS exception: {desc[:240]}")

            elif method == "Runtime.consoleAPICalled":
                if params.get("type") == "error":
                    args = [
                        str(a.get("value") or a.get("description") or "")
                        for a in (params.get("args") or [])
                    ]
                    text = " ".join(args).strip()
                    fatal_errors.append(f"console.error: {text[:240]}")

            elif method == "Log.entryAdded":
                entry = params.get("entry") or {}
                if entry.get("level") == "error":
                    e_url = str(entry.get("url") or "")
                    e_src = str(entry.get("source") or "")
                    e_txt = str(entry.get("text") or "")
                    if "cdn.socket.io" in e_url or "cdn.socket.io" in e_txt:
                        cdn_warnings.append(f"CDN log warning: {e_url or e_txt}")
                    elif e_src == "network" and any(
                        f"status of {code}" in e_txt for code in allow_statuses
                    ):
                        pass
                    elif e_src == "network" and e_url.startswith(self.base_url):
                        if e_url in resp_error_urls:
                            pass  # Already handled with exact status in Network.responseReceived
                        elif not allow_statuses:
                            fatal_errors.append(f"Network log error: {e_txt} ({e_url})")
                    else:
                        fatal_errors.append(f"Log.error [{e_src}]: {e_txt[:220]}")

            elif method == "Network.responseReceived":
                resp = params.get("response") or {}
                r_url = str(resp.get("url") or "")
                status = int(resp.get("status") or 0)
                responses.append((r_url, status))
                if r_url.endswith("/static/css/classpulse.css") and status in (200, 304):
                    css_loaded = True
                if r_url.startswith(self.base_url) and status >= 400:
                    if status not in allow_statuses:
                        fatal_errors.append(f"Local HTTP {status}: {r_url}")

            elif method == "Network.loadingFailed":
                req_id = str(params.get("requestId") or "")
                r_url = self._req_urls.get(req_id, "")
                if params.get("canceled"):
                    continue
                if "cdn.socket.io" in r_url:
                    cdn_warnings.append(
                        f"CDN loadingFailed: {r_url} ({params.get('errorText')})"
                    )
                elif r_url.startswith(self.base_url):
                    fatal_errors.append(
                        f"Local loadingFailed: {r_url} ({params.get('errorText')})"
                    )

        return {
            "fatal_errors": fatal_errors,
            "cdn_warnings": cdn_warnings,
            "responses": responses,
            "classpulse_css_200": css_loaded,
        }

    def eval_js(self, expression: str, await_promise: bool = True) -> Any:
        res = self.cdp_call(
            "Runtime.evaluate",
            {
                "expression": expression,
                "returnByValue": True,
                "awaitPromise": await_promise,
            },
        )
        if "exceptionDetails" in res:
            details = res["exceptionDetails"]
            raise RuntimeError(f"eval_js exception: {details}")
        return (res.get("result") or {}).get("value")

    def wait_for(self, js_predicate: str, timeout: float = 6.0) -> bool:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            try:
                if bool(self.eval_js(js_predicate)):
                    return True
            except Exception:
                pass
            time.sleep(0.1)
        return False

    def capture_png(
        self,
        out_path: Path,
        expected_size: tuple[int, int],
        results: AuditResults,
        full_page: bool = False,
    ) -> None:
        out_path.parent.mkdir(parents=True, exist_ok=True)
        res = self.cdp_call(
            "Page.captureScreenshot",
            {"format": "png", "captureBeyondViewport": full_page},
            timeout=20.0,
        )
        png_bytes = base64.b64decode(res["data"])
        out_path.write_bytes(png_bytes)
        with Image.open(out_path) as im:
            im.load()
            if im.format != "PNG":
                results.fail(f"Screenshot {out_path.name} invalid format {im.format}")
                return
            w, h = im.size
            exp_w, exp_h = expected_size
            if full_page:
                if w != exp_w or h < exp_h:
                    results.fail(
                        f"Screenshot {out_path.name} size {im.size}, expected ({exp_w}, >={exp_h})"
                    )
                    return
            else:
                if (w, h) != (exp_w, exp_h):
                    results.fail(
                        f"Screenshot {out_path.name} size {im.size}, expected {(exp_w, exp_h)}"
                    )
                    return
            stat = ImageStat.Stat(im.convert("RGB"))
            if max(stat.stddev) <= 2.0:
                results.fail(
                    f"Screenshot {out_path.name} appears blank/uniform (stddev={stat.stddev})"
                )
                return
        results.screenshots.append(out_path)
        results.ok(f"Screenshot verified: {out_path.name} ({im.size[0]}x{im.size[1]})")

    def close(self) -> None:
        if self.ws is not None:
            try:
                self.cdp_call("Browser.close", timeout=2.0)
            except Exception:
                pass
        if self._ws_ctx is not None:
            try:
                self._ws_ctx.__exit__(None, None, None)
            except Exception:
                pass
            self._ws_ctx = None
            self.ws = None
        elif self.ws is not None:
            try:
                self.ws.close()
            except Exception:
                pass
            self.ws = None
        if self.proc is not None:
            if self.proc.poll() is None:
                try:
                    self.proc.terminate()
                    self.proc.wait(timeout=4)
                except subprocess.TimeoutExpired:
                    if os.name == "nt":
                        subprocess.run(
                            ["taskkill", "/F", "/T", "/PID", str(self.proc.pid)],
                            stdout=subprocess.DEVNULL,
                            stderr=subprocess.DEVNULL,
                            check=False,
                        )
                    else:
                        self.proc.kill()
                    try:
                        self.proc.wait(timeout=4)
                    except subprocess.TimeoutExpired:
                        pass
            self.proc = None
        for _ in range(10):
            shutil.rmtree(self.user_data_dir, ignore_errors=True)
            if not self.user_data_dir.exists():
                break
            time.sleep(0.15)


# ---------------------------------------------------------------------------
# Audit Helpers (Geometry, R2 Exclusions, Light Theme, Template Parity)
# ---------------------------------------------------------------------------

GEOMETRY_JS = r"""
((expectedWidth) => {
  const isVisible = (el) => {
    const st = window.getComputedStyle(el);
    if (!st || st.display === "none" || st.visibility === "hidden" || parseFloat(st.opacity) === 0) return false;
    if (el.tagName === "INPUT" && el.type === "hidden") return false;
    const details = el.closest("details");
    if (details && !details.open && el.tagName !== "SUMMARY" && !el.closest("summary")) return false;
    const r = el.getBoundingClientRect();
    if (r.width <= 0 || r.height <= 0) return false;
    if (el.offsetParent === null && st.position !== "fixed" && el.tagName !== "BODY") return false;
    return true;
  };

  const innerW = window.innerWidth;
  const scrollW = document.documentElement.scrollWidth;
  const clipped = [];
  const controls = document.querySelectorAll("button, a, input, select, textarea, summary, [role='button'], [role='tab']");
  for (const el of controls) {
    if (!isVisible(el)) continue;
    const r = el.getBoundingClientRect();
    let scrollAnc = null;
    let p = el.parentElement;
    while (p && p !== document.body) {
      const pst = window.getComputedStyle(p);
      if ((pst.overflowX === "auto" || pst.overflowX === "scroll") && p.scrollWidth > p.clientWidth + 1) {
        scrollAnc = p;
        break;
      }
      p = p.parentElement;
    }
    if (scrollAnc) {
      const ar = scrollAnc.getBoundingClientRect();
      if (ar.right > innerW + 1 || ar.left < -1) {
        clipped.push({tag: el.tagName, id: el.id, text: (el.innerText || "").trim().slice(0, 40), reason: "scrollAncOverflow", right: ar.right});
      }
      if (innerW >= 1024 && scrollAnc.matches(".cp-tab-bar, .cp-mode-tabs, .tab-bar, .mode-tabs, nav")) {
        if (r.right > innerW + 1 || r.left < -1) {
          clipped.push({tag: el.tagName, id: el.id, text: (el.innerText || "").trim().slice(0, 40), reason: "desktopNavClipped", right: r.right});
        }
      }
    } else {
      if (r.left < -1 || r.right > innerW + 1) {
        clipped.push({tag: el.tagName, id: el.id, text: (el.innerText || el.value || "").trim().slice(0, 40), left: Math.round(r.left), right: Math.round(r.right), innerW});
      }
    }
  }

  const containerOverflows = [];
  const containerSel = ".cp-card, .card, .cp-bento, .cp-well, .cp-shell, table, .cp-table-scroll, header, main, section, .cp-card > *, .card > *, main > *, section > *";
  for (const el of document.querySelectorAll(containerSel)) {
    if (!isVisible(el)) continue;
    const r = el.getBoundingClientRect();
    if (r.right <= innerW + 1 && r.left >= -1) continue;
    let containedAnc = null;
    let p = el.parentElement;
    while (p && p !== document.body && p !== document.documentElement) {
      const pst = window.getComputedStyle(p);
      if (pst.overflowX === "auto" || pst.overflowX === "scroll" || pst.overflowX === "hidden" || pst.overflowX === "clip") {
        const pr = p.getBoundingClientRect();
        if (pr.right <= innerW + 1 && pr.left >= -1) {
          containedAnc = p;
          break;
        }
      }
      p = p.parentElement;
    }
    if (!containedAnc) {
      containerOverflows.push({
        tag: el.tagName,
        id: el.id,
        cls: (el.className || "").toString().slice(0, 40),
        left: Math.round(r.left),
        right: Math.round(r.right),
        innerW
      });
    }
  }

  const overlaps = [];
  const groupSel = ".cp-tab-bar, .cp-mode-tabs, .cp-kpi-grid-4, .cp-kpi-grid-3, .cp-grid-2, .cp-sidebar, .cp-step-indicator, .cp-segmented, nav, .tab-bar, .mode-tabs, .step-indicator, .auth-tabs, .kpi-row, .cards, .grid-2, aside.sidebar, header > div";
  for (const container of document.querySelectorAll(groupSel)) {
    const cst = window.getComputedStyle(container);
    if (cst.display === "none" || cst.visibility === "hidden") continue;
    const children = Array.from(container.children).filter((ch) => {
      const st = window.getComputedStyle(ch);
      if (!st || st.display === "none" || st.visibility === "hidden" || parseFloat(st.opacity) === 0) return false;
      if (st.position === "absolute" || st.position === "fixed") return false;
      const r = ch.getBoundingClientRect();
      return r.width >= 2 && r.height >= 2;
    });
    for (let i = 0; i < children.length; i++) {
      for (let j = i + 1; j < children.length; j++) {
        const a = children[i].getBoundingClientRect();
        const b = children[j].getBoundingClientRect();
        const ox = Math.min(a.right, b.right) - Math.max(a.left, b.left);
        const oy = Math.min(a.bottom, b.bottom) - Math.max(a.top, b.top);
        if (ox > 2 && oy > 2) {
          overlaps.push({
            container: container.className || container.tagName,
            a: (children[i].id || children[i].className || children[i].tagName).slice(0, 40),
            b: (children[j].id || children[j].className || children[j].tagName).slice(0, 40),
            ox: Math.round(ox),
            oy: Math.round(oy)
          });
        }
      }
    }
  }

  const brokenImages = [];
  for (const img of document.querySelectorAll("img")) {
    if (!isVisible(img)) continue;
    if (!img.complete || img.naturalWidth <= 0) {
      brokenImages.push({id: img.id, src: (img.getAttribute("src") || "").slice(0, 80)});
    }
  }

  return {
    expectedWidth,
    innerW,
    scrollW,
    clipped,
    containerOverflows,
    overlaps,
    brokenImages
  };
})
"""

THEME_INSPECT_JS = r"""
(() => {
  const parseRgb = (s) => {
    const m = (s || "").match(/rgba?\((\d+),\s*(\d+),\s*(\d+)(?:,\s*([\d.]+))?\)/i);
    if (!m) return null;
    return [parseInt(m[1], 10), parseInt(m[2], 10), parseInt(m[3], 10), m[4] !== undefined ? parseFloat(m[4]) : 1];
  };
  const lum = (rgb) => {
    const c = rgb.slice(0, 3).map(v => {
      const s = v / 255;
      return s <= 0.03928 ? s / 12.92 : Math.pow((s + 0.055) / 1.055, 2.4);
    });
    return 0.2126 * c[0] + 0.7152 * c[1] + 0.0722 * c[2];
  };

  const bodySt = window.getComputedStyle(document.body);
  const bgRgb = parseRgb(bodySt.backgroundColor) || [0, 0, 0, 1];
  const fgRgb = parseRgb(bodySt.color) || [255, 255, 255, 1];
  const bgL = lum(bgRgb);
  const fgL = lum(fgRgb);
  const contrast = (Math.max(bgL, fgL) + 0.05) / (Math.min(bgL, fgL) + 0.05);

  const sheets = Array.from(document.styleSheets).map(s => s.href || "");
  const hasClassPulseCss = sheets.some(h => h.includes("/static/css/classpulse.css"));

  const rootSt = window.getComputedStyle(document.documentElement);
  const tokens = {
    "--cp-bg-base": rootSt.getPropertyValue("--cp-bg-base").trim(),
    "--cp-ink": rootSt.getPropertyValue("--cp-ink").trim(),
    "--cp-coral": rootSt.getPropertyValue("--cp-coral").trim(),
    "--cp-indigo": rootSt.getPropertyValue("--cp-indigo").trim(),
  };

  const forbiddenRgb = new Set(["rgb(11, 17, 32)", "rgb(15, 23, 42)", "rgb(21, 31, 50)", "rgb(30, 41, 59)"]);
  const darkHits = [];
  for (const el of document.querySelectorAll("*")) {
    if (el.id === "headcount-canvas" || el.closest(".cp-cv-stage, .cp-cv-frame")) continue;
    const st = window.getComputedStyle(el);
    if (st.display === "none" || st.visibility === "hidden") continue;
    const inlineStyle = (el.getAttribute("style") || "").toLowerCase();
    for (const hex of ["#0b1120", "#0f172a", "#151f32", "#1e293b"]) {
      if (inlineStyle.includes(hex)) {
        darkHits.push({tag: el.tagName, id: el.id, cls: (el.className || "").toString().slice(0, 40), hit: hex});
      }
    }
    if (forbiddenRgb.has(st.backgroundColor)) {
      darkHits.push({tag: el.tagName, id: el.id, cls: (el.className || "").toString().slice(0, 40), hit: st.backgroundColor});
    }
    for (const rgb of forbiddenRgb) {
      if ((st.backgroundImage || "").includes(rgb)) {
        darkHits.push({tag: el.tagName, id: el.id, cls: (el.className || "").toString().slice(0, 40), hit: "bgImage:" + rgb});
      }
    }
  }

  return {
    bgColor: bodySt.backgroundColor,
    fgColor: bodySt.color,
    bgLuminance: bgL,
    contrast,
    hasClassPulseCss,
    tokens,
    darkHits: darkHits.slice(0, 10),
    bodyText: document.body.innerText || ""
  };
})()
"""


def audit_geometry(
    cdp: CDPDriver,
    viewport_name: str,
    label: str,
    results: AuditResults,
) -> None:
    exp_w, _exp_h = cdp.set_viewport(viewport_name)
    res = cdp.eval_js(f"({GEOMETRY_JS})({exp_w})")
    if res["innerW"] != exp_w:
        results.fail(
            f"[{label} @ {viewport_name}] window.innerWidth={res['innerW']} != {exp_w} (missing viewport meta?)"
        )
    if res["scrollW"] > res["innerW"]:
        results.fail(
            f"[{label} @ {viewport_name}] Horizontal overflow: scrollWidth={res['scrollW']} > innerWidth={res['innerW']}"
        )
    if res["clipped"]:
        results.fail(
            f"[{label} @ {viewport_name}] Clipped interactive controls: {res['clipped'][:5]}"
        )
    if res.get("containerOverflows"):
        results.fail(
            f"[{label} @ {viewport_name}] Container horizontal overflow: {res['containerOverflows'][:5]}"
        )
    if res["overlaps"]:
        results.fail(
            f"[{label} @ {viewport_name}] Overlapping sibling controls: {res['overlaps'][:5]}"
        )
    if res["brokenImages"]:
        results.fail(
            f"[{label} @ {viewport_name}] Broken visible images: {res['brokenImages'][:5]}"
        )
    if (
        res["innerW"] == exp_w
        and res["scrollW"] <= res["innerW"]
        and not res["clipped"]
        and not res.get("containerOverflows")
        and not res["overlaps"]
        and not res["brokenImages"]
    ):
        results.ok(f"Geometry clean: {label} @ {viewport_name} ({exp_w}px)")


def audit_theme_and_r2_dom(
    cdp: CDPDriver,
    view_key: str,
    results: AuditResults,
    allow_theme_fail: bool,
    nav_summary: Optional[dict[str, Any]] = None,
) -> None:
    info = cdp.eval_js(THEME_INSPECT_JS)
    text = info.get("bodyText") or ""
    r2_matches = R2_REGEX.findall(text)
    if r2_matches:
        results.fail(f"[{view_key}] R2 forbidden terms in rendered DOM: {sorted(set(r2_matches))}")
    else:
        results.ok(f"[{view_key}] R2 exclusion scan clean (DOM)")

    bg_lum = float(info.get("bgLuminance") or 0.0)
    contrast = float(info.get("contrast") or 0.0)
    if bg_lum < 0.70:
        results.theme_issue(
            f"[{view_key}] Body background not light theme: {info.get('bgColor')} (L={bg_lum:.3f} < 0.70)",
            allow_theme_fail,
        )
    elif contrast < 4.5:
        results.theme_issue(
            f"[{view_key}] Body contrast {contrast:.2f} < 4.5 ({info.get('fgColor')} on {info.get('bgColor')})",
            allow_theme_fail,
        )
    else:
        results.ok(f"[{view_key}] Light theme luminance L={bg_lum:.3f}, contrast={contrast:.2f}")

    css_ok = bool(info.get("hasClassPulseCss"))
    if nav_summary is not None:
        css_ok = css_ok and bool(nav_summary.get("classpulse_css_200"))
    if not css_ok:
        results.theme_issue(
            f"[{view_key}] /static/css/classpulse.css not loaded (HTTP 200/304 + stylesheet)",
            allow_theme_fail,
        )
    else:
        results.ok(f"[{view_key}] /static/css/classpulse.css loaded (200/304)")

    dark_hits = info.get("darkHits") or []
    if dark_hits:
        results.theme_issue(
            f"[{view_key}] Forbidden dark-slate hexes outside CV frame: {dark_hits[:4]}",
            allow_theme_fail,
        )
    else:
        results.ok(f"[{view_key}] Zero dark-slate hexes in DOM outside CV frame")

    tokens = info.get("tokens") or {}
    results.tokens_by_view[view_key] = tokens
    missing_toks = [k for k in THEME_TOKENS if not tokens.get(k)]
    if missing_toks:
        results.theme_issue(
            f"[{view_key}] Missing --cp-* CSS tokens: {missing_toks}",
            allow_theme_fail,
        )


def audit_source_files(results: AuditResults, allow_theme_fail: bool) -> None:
    # R3 stack guard: zero React/Vue/Node artifacts
    forbidden_globs = ("package.json", "package-lock.json", "tsconfig.json")
    for name in forbidden_globs:
        if (PROJECT_ROOT / name).exists():
            results.fail(f"R3 stack violation: {name} present in project root")
    if (PROJECT_ROOT / "node_modules").exists():
        results.fail("R3 stack violation: node_modules directory present")
    jsx_tsx = [
        p for p in (PROJECT_ROOT / "app").rglob("*")
        if p.suffix.lower() in {".jsx", ".tsx", ".ts"}
    ]
    if jsx_tsx:
        results.fail(f"R3 stack violation: forbidden frontend files {[p.name for p in jsx_tsx]}")
    else:
        results.ok("R3 stack constraint verified (zero Node/React/TS files)")

    # R2 word-boundary scan + dark-hex scan across app/templates & app/static
    r2_file_hits: list[str] = []
    dark_file_hits: list[str] = []
    scan_roots = [PROJECT_ROOT / "app" / "templates", PROJECT_ROOT / "app" / "static"]
    for root in scan_roots:
        if not root.exists():
            continue
        for fpath in root.rglob("*"):
            if not fpath.is_file() or fpath.suffix.lower() not in {".html", ".css", ".js"}:
                continue
            raw = fpath.read_text(encoding="utf-8", errors="replace")
            r2_m = R2_REGEX.findall(raw)
            if r2_m:
                r2_file_hits.append(f"{fpath.relative_to(PROJECT_ROOT)}: {sorted(set(r2_m))}")
            for lineno, line in enumerate(raw.splitlines(), 1):
                low = line.lower()
                if "headcount-canvas" in low or "cp-cv-stage" in low or "cp-cv-frame" in low:
                    continue
                for hx in FORBIDDEN_DARK_HEXES:
                    if hx in low:
                        dark_file_hits.append(
                            f"{fpath.relative_to(PROJECT_ROOT)}:{lineno} ({hx})"
                        )
    if r2_file_hits:
        results.fail(f"R2 forbidden terms in source files: {r2_file_hits[:5]}")
    else:
        results.ok("R2 exclusion scan clean across app/templates and app/static")

    if dark_file_hits:
        results.theme_issue(
            f"Dark-slate hexes found in source files ({len(dark_file_hits)} hits, e.g. {dark_file_hits[:4]})",
            allow_theme_fail,
        )
    else:
        results.ok("Zero forbidden dark-slate hexes in app/templates and app/static")


def audit_lecturer_dashboard_template(
    app: Any,
    seed: SeedContext,
    results: AuditResults,
    allow_theme_fail: bool,
) -> None:
    """Inspect lecturer_dashboard.html via Jinja2 render without modifying app/."""
    with app.test_request_context("/lecturer"):
        db = app.extensions["db_session"]
        qr_svc = app.extensions["qr_service"]
        att_svc = AttendanceService(
            db=db, qr_service=qr_svc, device_mgr=app.extensions["device_mgr"]
        )
        sess = db.get(ClassSession, seed.ariel1_id)
        snap = db.get(HeadcountSnapshot, seed.seeded_snap_id)
        qr_payload = qr_svc.build_qr_payload(seed.base_url, seed.ariel1_code)
        verdict = att_svc.get_audit_summary(sess.id)
        rows = att_svc.get_attendance_rows(sess.id)
        tpl = app.jinja_env.get_template("lecturer_dashboard.html")
        html = tpl.render(
            session=sess,
            qr_payload=qr_payload,
            verdict=verdict,
            rows=rows,
            latest_snap=snap,
            base_url=seed.base_url,
            projector_token=seed.projector_token,
        )

    required_dom = [
        'id="kpi-visual-count"',
        'id="headcount-canvas"',
        f'data-session-id="{seed.ariel1_id}"',
        'id="canvas-empty-hint"',
        "/static/js/headcount_canvas.js",
    ]
    missing = [t for t in required_dom if t not in html]
    if missing:
        results.fail(f"[lecturer_dashboard.html] Missing required DOM/script tokens: {missing}")
    else:
        results.ok("[lecturer_dashboard.html] Baseline DOM/headcount contract intact")

    m2_scripts = [s for s in ("/static/js/csrf.js", "/static/js/notifications.js") if s not in html]
    if m2_scripts:
        results.theme_issue(
            f"[lecturer_dashboard.html] Missing M2 overhaul scripts: {m2_scripts}",
            allow_theme_fail,
        )
    else:
        results.ok("[lecturer_dashboard.html] M2 scripts (csrf.js, notifications.js) present")

    if R2_REGEX.search(html):
        results.fail("[lecturer_dashboard.html] R2 forbidden terms found in rendered HTML")
    if "/static/css/classpulse.css" not in html:
        results.theme_issue(
            "[lecturer_dashboard.html] Missing /static/css/classpulse.css link",
            allow_theme_fail,
        )
    else:
        results.ok("[lecturer_dashboard.html] Linked to /static/css/classpulse.css")

    low = html.lower()
    if any(hx in low for hx in FORBIDDEN_DARK_HEXES):
        results.theme_issue(
            "[lecturer_dashboard.html] Contains forbidden dark-slate hexes in rendered HTML",
            allow_theme_fail,
        )


# ---------------------------------------------------------------------------
# Tier Runners
# ---------------------------------------------------------------------------

def run_tier1(
    srv: EphemeralServer,
    seed: SeedContext,
    cdp: CDPDriver,
    results: AuditResults,
    allow_theme_fail: bool,
) -> None:
    print("\n=== TIER 1: Feature Coverage, Routes & Runtime Cleanliness ===")

    # 1. HTTP route & redirect verification
    s_anon = requests.Session()
    r = s_anon.get(f"{seed.base_url}/portal", timeout=5)
    assert_http(results, r, 200, "/portal", contains="ClassPulse Multi-Course Hub")

    r_root = s_anon.get(f"{seed.base_url}/", allow_redirects=False, timeout=5)
    if r_root.status_code == 302 and "/teacher" in r_root.headers.get("Location", ""):
        results.ok("GET / redirects 302 -> /teacher")
    else:
        results.fail(f"GET / expected 302 -> /teacher, got {r_root.status_code} ({r_root.headers.get('Location')})")

    r_lec = s_anon.get(f"{seed.base_url}/lecturer", allow_redirects=False, timeout=5)
    if r_lec.status_code == 302 and "/teacher" in r_lec.headers.get("Location", ""):
        results.ok("GET /lecturer redirects 302 -> /teacher")
    else:
        results.fail(f"GET /lecturer expected 302 -> /teacher, got {r_lec.status_code}")

    r_t_unauth = s_anon.get(f"{seed.base_url}/teacher", allow_redirects=False, timeout=5)
    if r_t_unauth.status_code == 302 and "/teacher/login" in r_t_unauth.headers.get("Location", ""):
        results.ok("GET /teacher (unauth) redirects 302 -> /teacher/login")
    else:
        results.fail(f"GET /teacher (unauth) expected 302 -> /teacher/login, got {r_t_unauth.status_code}")

    r_login = s_anon.get(f"{seed.base_url}/teacher/login", timeout=5)
    assert_http(results, r_login, 200, "/teacher/login", contains="pill-step-1")
    r_setup = s_anon.get(f"{seed.base_url}/teacher/login?setup=1", timeout=5)
    assert_http(results, r_setup, 200, "/teacher/login?setup=1", contains="card-step-2")

    s_lec = requests.Session()
    s_lec.cookies.set("cp_lecturer", seed.lecturer_cookie)
    r_t = s_lec.get(f"{seed.base_url}/teacher", timeout=5)
    assert_http(results, r_t, 200, "/teacher (auth)", contains=seed.ariel1_code)

    # 0-courses lecturer -> 302 /teacher/login?setup=1 and cross-tenant 403 checks
    s_lec2 = requests.Session()
    s_lec2.cookies.set("cp_lecturer", seed.lecturer2_cookie)
    r_zero = s_lec2.get(f"{seed.base_url}/teacher", allow_redirects=False, timeout=5)
    if r_zero.status_code == 302 and "/teacher/login?setup=1" in r_zero.headers.get("Location", ""):
        results.ok("GET /teacher (0 courses) redirects 302 -> /teacher/login?setup=1")
    else:
        results.fail(f"GET /teacher (0 courses) expected 302 -> /teacher/login?setup=1, got {r_zero.status_code} ({r_zero.headers.get('Location')})")

    r_403_course = s_lec2.get(f"{seed.base_url}/teacher?course_id={seed.cs204_id}", allow_redirects=False, timeout=5)
    assert_http(results, r_403_course, 403, "/teacher?course_id (cross-tenant 403)")

    r_403_sess = s_lec2.get(f"{seed.base_url}/teacher?session={seed.ariel1_code}", allow_redirects=False, timeout=5)
    assert_http(results, r_403_sess, 403, "/teacher?session (cross-tenant 403)")

    r_403_pop = s_lec2.get(f"{seed.base_url}/lecturer/chat-popout/{seed.ariel1_code}", allow_redirects=False, timeout=5)
    assert_http(results, r_403_pop, 403, "/lecturer/chat-popout (cross-tenant 403)")

    r_403_proj = s_lec2.get(f"{seed.base_url}/projector/{seed.ariel1_code}", allow_redirects=False, timeout=5)
    assert_http(results, r_403_proj, 403, "/projector (cross-tenant 403)")

    r_pop = s_lec.get(f"{seed.base_url}/lecturer/chat-popout/{seed.ariel1_code}", timeout=5)
    assert_http(results, r_pop, 200, "/lecturer/chat-popout", contains="Classroom Q&amp;A Feed")

    r_proj = s_anon.get(
        f"{seed.base_url}/projector/{seed.ariel1_code}?ptok={seed.projector_token}",
        timeout=5,
    )
    assert_http(results, r_proj, 200, "/projector", contains="proj-qr")

    s_alice = requests.Session()
    s_alice.cookies.set("cp_device", seed.alice_device_cookie)
    r_stu = s_alice.get(f"{seed.base_url}/student", timeout=5)
    assert_http(results, r_stu, 200, "/student", contains="ClassPulse Student App")
    if "Alice Cohen" in r_stu.text and "Assignments" in r_stu.text and "Summaries" in r_stu.text:
        results.ok("/student renders student full_name and card navigation sections")
    else:
        results.fail("/student missing Alice Cohen or Assignments/Summaries sections")

    r_chk_new = s_anon.get(seed.fresh_checkin_url(seed.ariel1_code), timeout=5)
    assert_http(results, r_chk_new, 200, "/s/ARIEL1 (first-time)", contains="First-Time Student Sign-In")

    r_chk_ret = s_alice.get(seed.fresh_checkin_url(seed.ariel1_code), timeout=5)
    assert_http(results, r_chk_ret, 200, "/s/ARIEL1 (returning Alice)", contains="Attendance Recorded")

    audit_lecturer_dashboard_template(srv.app, seed, results, allow_theme_fail)

    # 2. Browser CDP visits across all views + 6 teacher tabs + 4 modals + 3 student views
    cdp.set_viewport("desktop")
    cdp.set_role_cookies("anonymous", seed)
    cdp.goto(f"{seed.base_url}/portal", results=results, label="portal_home")
    cdp.goto(f"{seed.base_url}/teacher/login", results=results, label="teacher_login")

    cdp.set_role_cookies("lecturer", seed)
    cdp.goto(f"{seed.base_url}/teacher", settle_s=0.7, results=results, label="teacher_portal")

    # Verify strict tab label order & absence of forbidden strings
    tab_check = cdp.eval_js(
        """(() => {
            const labels = Array.from(document.querySelectorAll("#teacher-sidebar [data-tab-target], .cp-tab-bar [data-tab-target]")).map(b => b.innerText.trim());
            const html = document.documentElement.outerHTML;
            return { labels, html };
        })()"""
    )
    labels = tab_check["labels"]
    idx4 = next((i for i, l in enumerate(labels) if "AI Summary" in l), -1)
    idx5 = next((i for i, l in enumerate(labels) if "Homework" in l), -1)
    idx6 = next((i for i, l in enumerate(labels) if "Roster" in l), -1)
    if 0 <= idx4 < idx5 < idx6:
        results.ok(f"Strict teacher tab ordering verified: {labels}")
    else:
        results.fail(f"Broken teacher tab ordering: {labels}")

    html_src = tab_check["html"]
    for bad in ('id="tab-ai-hub"', "Course Overview &amp; Roster", "Course Overview & Roster", "Reset Device", "/chat/export.txt", "OOP modularity, automated pytest coverage, error handling."):
        if bad in html_src:
            results.fail(f"Forbidden string/ID found in teacher_portal: {bad}")

    # Verify restructured compact zero-scroll sidebar, removed Lectures item, and enlarged tiles
    ux_metrics = cdp.eval_js(
        """(() => {
            const sb = document.getElementById("teacher-sidebar");
            const lecItem = sb ? sb.querySelector('[data-tab-target="tab-lectures"]') : null;
            const myCoursesList = document.getElementById("my-courses-list");
            const profileBtn = document.getElementById("lecturer-profile-trigger");
            const closeBtn = document.getElementById("btn-lecturer-close-app");
            const tile = document.querySelector("#tab-overview .cp-hub-tile");
            return {
                sidebarScrollH: sb ? sb.scrollHeight : 0,
                sidebarClientH: sb ? sb.clientHeight : 0,
                noRedundantLectures: !lecItem,
                myCoursesInitiallyCollapsed: Boolean(myCoursesList && myCoursesList.hidden),
                hasProfilePopover: Boolean(profileBtn && closeBtn),
                tileHeight: tile ? Math.round(tile.getBoundingClientRect().height) : 0,
            };
        })()"""
    )
    if ux_metrics["sidebarScrollH"] <= ux_metrics["sidebarClientH"] + 2:
        results.ok(f"Lecturer sidebar has ZERO vertical scrollbar ({ux_metrics['sidebarScrollH']} <= {ux_metrics['sidebarClientH']})")
    else:
        results.fail(f"Lecturer sidebar overflows vertically ({ux_metrics['sidebarScrollH']} > {ux_metrics['sidebarClientH']})")
    if ux_metrics["noRedundantLectures"] and ux_metrics["myCoursesInitiallyCollapsed"] and ux_metrics["hasProfilePopover"]:
        results.ok("Sidebar restructured cleanly: no Lectures item, collapsible My Courses, Profile popover + Close App")
    else:
        results.fail(f"Sidebar structure assertion failed: {ux_metrics}")
    if ux_metrics["tileHeight"] >= 190:
        results.ok(f"Dashboard hub tiles enlarged cleanly (height={ux_metrics['tileHeight']}px on 1440x900)")
    else:
        results.fail(f"Dashboard hub tiles too small (height={ux_metrics['tileHeight']}px)")

    # Click all 6 teacher tabs
    for tab_id, expected_lbl in TEACHER_TABS:
        clicked = cdp.eval_js(
            f"""(() => {{
                const btn = document.querySelector('[data-tab-target="{tab_id}"]');
                if (!btn) return false;
                btn.click();
                const pane = document.getElementById("{tab_id}");
                return Boolean(pane && pane.classList.contains("active") && window.getComputedStyle(pane).display !== "none");
            }})()"""
        )
        ev_sum = cdp.classify_events(cdp.drain_events(0.25))
        if ev_sum["fatal_errors"]:
            results.fail(f"[teacher tab {tab_id}] Runtime errors: {ev_sum['fatal_errors']}")
        if clicked:
            results.ok(f"Teacher tab clicked & active: {expected_lbl} (#{tab_id})")
        else:
            results.fail(f"Teacher tab failed to activate: #{tab_id}")

    # Open & close all static modals (open profile popover first for modal-account-security)
    static_modals = [
        ("modal-new-course", 'button[onclick*="modal-new-course"]:not([onclick*="closeModal"]), [data-modal-open="modal-new-course"]'),
        ("modal-new-lecture", 'button[onclick*="modal-new-lecture"]:not([onclick*="closeModal"]), [data-modal-open="modal-new-lecture"]'),
        ("modal-switch-lecture", '#btn-switch-lecture'),
        ("modal-account-security", 'button[onclick*="modal-account-security"]:not([onclick*="closeModal"]), [data-modal-open="modal-account-security"]'),
    ]
    for modal_id, open_sel in static_modals:
        opened = cdp.eval_js(
            f"""(() => {{
                const trig = document.getElementById("lecturer-profile-trigger");
                const pop = document.getElementById("lecturer-profile-popover");
                if ({json.dumps(modal_id)} === "modal-account-security" && trig && pop && pop.hidden) trig.click();
                const b = document.querySelector({json.dumps(open_sel)});
                if (!b) return false;
                b.click();
                const m = document.getElementById("{modal_id}");
                return Boolean(m && m.classList.contains("open") && window.getComputedStyle(m).display !== "none");
            }})()"""
        )
        closed = cdp.eval_js(
            f"""(() => {{
                const cb = document.querySelector("#{modal_id} button[onclick*=\\"closeModal\\"], #{modal_id} [data-modal-close]");
                if (!cb) return false;
                cb.click();
                const m = document.getElementById("{modal_id}");
                return Boolean(m && !m.classList.contains("open"));
            }})()"""
        )
        if opened and closed:
            results.ok(f"Modal open & close verified: #{modal_id}")
        else:
            results.fail(f"Modal open/close failed: #{modal_id} (opened={opened}, closed={closed})")

    # 4th Modal: #modal-hw-review (requires #tab-homework + seeded submission button)
    cdp.eval_js('document.querySelector(\'[data-tab-target="tab-homework"]\').click()')
    has_rev_btn = cdp.wait_for(
        "Boolean(document.querySelector('#hw-submissions-list button[onclick*=\"openReviewModal\"]'))",
        timeout=5.0,
    )
    if has_rev_btn:
        opened_hw = cdp.eval_js(
            """(() => {
                document.querySelector('#hw-submissions-list button[onclick*="openReviewModal"]').click();
                const m = document.getElementById("modal-hw-review");
                const t = document.getElementById("modal-hw-review-title");
                return Boolean(m && m.classList.contains("open") && t && t.innerText.trim().length > 0);
            })()"""
        )
        closed_hw = cdp.eval_js(
            """(() => {
                const cb = document.querySelector('#modal-hw-review button[onclick*="closeModal"], #modal-hw-review [data-modal-close]');
                if (!cb) return false;
                cb.click();
                return !document.getElementById("modal-hw-review").classList.contains("open");
            })()"""
        )
        if opened_hw and closed_hw:
            results.ok("Modal open & close verified: #modal-hw-review")
        else:
            results.fail(f"Modal #modal-hw-review open/close failed (opened={opened_hw}, closed={closed_hw})")
    else:
        results.fail("Modal #modal-hw-review trigger button did not render in #hw-submissions-list")

    # Chat popout & Projector
    cdp.set_viewport("popout")
    cdp.goto(
        f"{seed.base_url}/lecturer/chat-popout/{seed.ariel1_code}",
        results=results,
        label="chat_popout",
    )
    cdp.set_viewport("projector_1080")
    cdp.goto(
        f"{seed.base_url}/projector/{seed.ariel1_code}?ptok={seed.projector_token}",
        results=results,
        label="projector",
    )

    # Student App (3 views) + Check-in (onboarding & returning)
    cdp.set_viewport("mobile")
    cdp.set_role_cookies("alice", seed)
    cdp.goto(f"{seed.base_url}/student", settle_s=0.65, results=results, label="student_app")
    for btn_id, view_id in STUDENT_VIEWS:
        ok_view = cdp.eval_js(
            f"""(() => {{
                const btn = document.getElementById("{btn_id}");
                if (!btn) return false;
                btn.click();
                const v = document.getElementById("{view_id}");
                return Boolean(v && v.classList.contains("active") && window.getComputedStyle(v).display !== "none");
            }})()"""
        )
        ev_sum = cdp.classify_events(cdp.drain_events(0.25))
        if ev_sum["fatal_errors"]:
            results.fail(f"[student view {view_id}] Runtime errors: {ev_sum['fatal_errors']}")
        if ok_view:
            results.ok(f"Student view clicked & active: #{view_id}")
        else:
            results.fail(f"Student view failed to activate: #{view_id}")

    cdp.goto(seed.fresh_checkin_url(seed.ariel1_code), results=results, label="checkin_returning_alice")
    cdp.set_role_cookies("anonymous", seed)
    cdp.goto(seed.fresh_checkin_url(seed.ariel1_code), results=results, label="checkin_onboarding_fresh")


def assert_http(
    results: AuditResults,
    resp: requests.Response,
    expected_status: int,
    label: str,
    contains: Optional[str] = None,
) -> None:
    if resp.status_code != expected_status:
        results.fail(f"HTTP {label}: expected {expected_status}, got {resp.status_code}")
        return
    if contains and contains not in resp.text:
        results.fail(f"HTTP {label}: missing expected text '{contains}'")
        return
    results.ok(f"HTTP {label}: {resp.status_code}" + (f" (contains '{contains}')" if contains else ""))


def run_tier2(
    seed: SeedContext,
    cdp: CDPDriver,
    results: AuditResults,
    allow_theme_fail: bool,
) -> None:
    print("\n=== TIER 2: Multi-Viewport Geometry & Check-In Error States ===")

    # 1. Desktop 1440x900
    cdp.set_viewport("desktop")
    cdp.set_role_cookies("anonymous", seed)
    cdp.goto(f"{seed.base_url}/portal", results=results, label="t2_portal_1440")
    audit_geometry(cdp, "desktop", "portal_home", results)

    cdp.goto(f"{seed.base_url}/teacher/login", results=results, label="t2_login_1440")
    audit_geometry(cdp, "desktop", "teacher_login", results)

    cdp.set_role_cookies("lecturer", seed)
    cdp.goto(f"{seed.base_url}/teacher", settle_s=0.7, results=results, label="t2_teacher_1440")
    for tab_id, _lbl in TEACHER_TABS:
        cdp.eval_js(f'document.querySelector(\'[data-tab-target="{tab_id}"]\').click()')
        time.sleep(0.15)
        audit_geometry(cdp, "desktop", f"teacher_{tab_id}", results)

    # 2. Tablet 1024x768
    audit_geometry(cdp, "tablet", "teacher_portal", results)

    # 3. Mobile 390x844 & 480x800 on teacher_portal + <=980px drawer toggle check
    for vp in ("mobile", "popout"):
        audit_geometry(cdp, vp, "teacher_portal", results)
        toggle_res = cdp.eval_js(
            """(() => {
                const btn = document.querySelector("#mobile-sidebar-toggle, .cp-mobile-drawer-bar button[aria-expanded], aside.cp-sidebar ~ * button[aria-expanded], aside.sidebar ~ * button[aria-expanded], header button[aria-expanded], button[aria-expanded]");
                if (!btn) return { found: false };
                const before = btn.getAttribute("aria-expanded");
                btn.click();
                const after = btn.getAttribute("aria-expanded");
                return { found: true, before, after };
            })()"""
        )
        if not toggle_res.get("found"):
            results.theme_issue(
                f"[teacher_portal @ {vp}] Missing <=980px sidebar drawer toggle ([aria-expanded] / #mobile-sidebar-toggle)",
                allow_theme_fail,
            )
        elif toggle_res["before"] == toggle_res["after"]:
            results.fail(f"[teacher_portal @ {vp}] Sidebar toggle did not change aria-expanded ({toggle_res})")
        else:
            time.sleep(0.15)
            audit_geometry(cdp, vp, "teacher_portal_drawer_open", results)
            cdp.eval_js(
                """(() => {
                    const btn = document.querySelector("#mobile-sidebar-toggle, .cp-mobile-drawer-bar button[aria-expanded], aside.cp-sidebar ~ * button[aria-expanded], aside.sidebar ~ * button[aria-expanded], header button[aria-expanded], button[aria-expanded]");
                    if (btn) btn.click();
                })()"""
            )
            results.ok(f"[teacher_portal @ {vp}] Sidebar drawer toggle verified ({toggle_res['before']} -> {toggle_res['after']})")

    # 4. Projector 1920x1080 & 1280x720
    cdp.goto(
        f"{seed.base_url}/projector/{seed.ariel1_code}?ptok={seed.projector_token}",
        results=results,
        label="t2_projector",
    )
    audit_geometry(cdp, "projector_1080", "projector", results)
    audit_geometry(cdp, "projector_720", "projector", results)

    # 5. Chat popout 480x800
    cdp.goto(
        f"{seed.base_url}/lecturer/chat-popout/{seed.ariel1_code}",
        results=results,
        label="t2_chat_popout",
    )
    audit_geometry(cdp, "popout", "chat_popout", results)

    # 6. Student app across desktop, mobile (390x844), small mobile (360x740)
    cdp.set_role_cookies("alice", seed)
    cdp.goto(f"{seed.base_url}/student", settle_s=0.65, results=results, label="t2_student")
    audit_geometry(cdp, "desktop", "student_app", results)
    for vp in ("mobile", "mobile_sm"):
        drawer_check = cdp.eval_js(
            """(() => {
                const btn = document.getElementById("student-drawer-toggle");
                const drawer = document.getElementById("student-nav-drawer");
                if (!btn || !drawer) return { missing: true };
                const before = btn.getAttribute("aria-expanded");
                btn.click();
                const after = btn.getAttribute("aria-expanded");
                const opened = drawer.classList.contains("open");
                return { missing: false, before, after, opened };
            })()"""
        )
        if drawer_check.get("missing") or drawer_check.get("after") != "true" or not drawer_check.get("opened"):
            results.fail(f"[student_app @ {vp}] Slide-over drawer failed to open ({drawer_check})")
        else:
            time.sleep(0.28)
            audit_geometry(cdp, vp, "student_drawer_open", results)
            cdp.eval_js("document.getElementById('nav-student-courses').click()")
            time.sleep(0.28)
            results.ok(f"[student_app @ {vp}] Slide-over drawer toggle & auto-close verified")

        for btn_id, view_id in STUDENT_VIEWS:
            cdp.eval_js(f"document.getElementById('{btn_id}').click()")
            time.sleep(0.12)
            audit_geometry(cdp, vp, f"student_{view_id}", results)

    # 7. Portal mobile
    cdp.set_role_cookies("anonymous", seed)
    cdp.goto(f"{seed.base_url}/portal", results=results, label="t2_portal_mobile")
    audit_geometry(cdp, "mobile", "portal_home", results)

    # 8. Student check-in onboarding, verified & 404/403/410 error states in fresh anonymous context
    cdp.goto(seed.fresh_checkin_url(seed.ariel1_code), results=results, label="t2_checkin_onboarding")
    audit_geometry(cdp, "mobile", "checkin_onboarding", results)
    audit_geometry(cdp, "mobile_sm", "checkin_onboarding", results)

    cdp.set_role_cookies("alice", seed)
    cdp.goto(seed.fresh_checkin_url(seed.ariel1_code), results=results, label="t2_checkin_verified")
    audit_geometry(cdp, "mobile", "checkin_verified", results)

    # Error states MUST run in fresh anonymous context (unchecked-in device)
    cdp.set_role_cookies("anonymous", seed)
    s_err = requests.Session()
    r404 = s_err.get(f"{seed.base_url}/s/NOPE999", timeout=5)
    assert_http(results, r404, 404, "checkin 404", contains="Lecture Not Found")
    r403 = s_err.get(seed.fresh_checkin_url(seed.closed1_code), timeout=5)
    assert_http(results, r403, 403, "checkin 403", contains="Attendance Closed")
    r410 = s_err.get(f"{seed.base_url}/s/{seed.ariel1_code}?t=1&sig=bad", timeout=5)
    assert_http(results, r410, 410, "checkin 410", contains="QR Code Expired")

    cdp.goto(f"{seed.base_url}/s/NOPE999", allow_statuses=(404,), results=results, label="checkin_err_404_cdp")
    audit_geometry(cdp, "mobile", "checkin_err_404", results)
    cdp.goto(seed.fresh_checkin_url(seed.closed1_code), allow_statuses=(403,), results=results, label="checkin_err_403_cdp")
    audit_geometry(cdp, "mobile", "checkin_err_403", results)
    cdp.goto(f"{seed.base_url}/s/{seed.ariel1_code}?t=1&sig=bad", allow_statuses=(410,), results=results, label="checkin_err_410_cdp")
    audit_geometry(cdp, "mobile", "checkin_err_410", results)


def run_tier3(
    seed: SeedContext,
    cdp: CDPDriver,
    results: AuditResults,
    allow_theme_fail: bool,
) -> None:
    print("\n=== TIER 3: Interactive E2E Flows, R2 Exclusions & Light-Theme Audit ===")

    audit_source_files(results, allow_theme_fail)

    # 1. Lecturer Login UI + Course Switch + Lecture Switch
    cdp.set_viewport("desktop")
    cdp.set_role_cookies("anonymous", seed)
    nav_login = cdp.goto(f"{seed.base_url}/teacher/login", results=results, label="t3_teacher_login")
    audit_theme_and_r2_dom(cdp, "teacher_login", results, allow_theme_fail, nav_login)

    toggled_auth = cdp.eval_js(
        """(() => {
            document.getElementById("tab-btn-register").click();
            const regVis = window.getComputedStyle(document.getElementById("panel-register")).display !== "none";
            document.getElementById("tab-btn-signin").click();
            const sigVis = window.getComputedStyle(document.getElementById("panel-signin")).display !== "none";
            return regVis && sigVis;
        })()"""
    )
    if toggled_auth:
        results.ok("teacher_login Sign-In <-> Register switcher works")
    else:
        results.fail("teacher_login Sign-In <-> Register switcher failed")

    cdp.eval_js(
        f"""(() => {{
            const u = document.getElementById("login-identifier");
            const p = document.getElementById("login-password");
            u.value = {json.dumps(seed.lecturer_username)};
            p.value = {json.dumps(seed.lecturer_password)};
            const btn = document.querySelector('button[onclick*="handleLecturerSignIn"]') || document.querySelector('#panel-signin button[type="submit"], #panel-signin > button[type="button"]');
            if (btn) btn.click();
        }})()"""
    )
    landed = cdp.wait_for("window.location.pathname === '/teacher'", timeout=6.0)
    ev_login = cdp.classify_events(cdp.drain_events(0.6))
    if ev_login["fatal_errors"]:
        results.fail(f"Post-login /teacher errors: {ev_login['fatal_errors']}")
    if landed:
        results.ok("teacher_login returning lecturer sign-in redirected directly to /teacher")
    else:
        results.fail("teacher_login returning lecturer sign-in failed to navigate to /teacher")

    # Course switch CS204 -> CS101 -> CS204 (expanding collapsible My Courses accordion first)
    cdp.eval_js(
        f"""(() => {{
            const acc = document.getElementById("btn-my-courses-toggle");
            const list = document.getElementById("my-courses-list");
            if (acc && list && list.hidden) acc.click();
            const link = document.querySelector('#my-courses-list a[href*="course_id={seed.cs101_id}"]');
            if (link) link.click();
        }})()"""
    )
    if cdp.wait_for(f"window.location.search.includes('course_id={seed.cs101_id}')", timeout=5.0):
        cdp.drain_events(0.5)
        results.ok("Course switch to CS101 succeeded")
    else:
        results.fail("Course switch to CS101 failed")

    cdp.eval_js(
        f"""(() => {{
            const acc = document.getElementById("btn-my-courses-toggle");
            const list = document.getElementById("my-courses-list");
            if (acc && list && list.hidden) acc.click();
            const link = document.querySelector('#my-courses-list a[href*="course_id={seed.cs204_id}"]');
            if (link) link.click();
        }})()"""
    )
    if cdp.wait_for(f"window.location.search.includes('course_id={seed.cs204_id}')", timeout=5.0):
        cdp.drain_events(0.5)
        results.ok("Course switch back to CS204 succeeded")
    else:
        results.fail("Course switch back to CS204 failed")

    # Lecture switch via Switch Lecture modal ARIEL2 -> /teacher
    cdp.eval_js(
        f"""(() => {{
            document.getElementById("btn-switch-lecture")?.click();
            const btn = document.querySelector('#modal-switch-lecture [data-switch-session-code="{seed.ariel2_code}"]');
            if (btn) btn.click();
        }})()"""
    )
    if cdp.wait_for(f"window.location.search.includes('session={seed.ariel2_code}')", timeout=5.0):
        cdp.drain_events(0.5)
        results.ok("Switch Lecture modal switched to ARIEL2 cleanly")
    else:
        results.fail("Switch Lecture modal to ARIEL2 failed")

    # Verify lecturer Back button steps back through visited stack cleanly on desktop
    back_lec_ok = cdp.eval_js(
        """(() => {
            document.querySelector('[data-tab-target="tab-attendance"]').click();
            document.querySelector('[data-tab-target="tab-course-details"]').click();
            const b = document.getElementById("lecturer-back-btn");
            const vis = Boolean(b && !b.hidden && getComputedStyle(b).display !== "none");
            b.click();
            const midActive = document.getElementById("tab-attendance").classList.contains("active");
            b.click();
            const rootActive = document.getElementById("tab-overview").classList.contains("active") && b.hidden;
            return vis && midActive && rootActive;
        })()"""
    )
    if back_lec_ok:
        results.ok("Lecturer desktop Back button steps back cleanly without losing session")
    else:
        results.fail("Lecturer desktop Back button check failed")

    nav_tp = cdp.goto(f"{seed.base_url}/teacher", settle_s=0.7, results=results, label="t3_teacher_portal")
    audit_theme_and_r2_dom(cdp, "teacher_portal", results, allow_theme_fail, nav_tp)
    for tab_id, _lbl in TEACHER_TABS[1:]:
        cdp.eval_js(f'document.querySelector(\'[data-tab-target="{tab_id}"]\').click()')
        time.sleep(0.15)
        audit_theme_and_r2_dom(cdp, f"teacher_portal:{tab_id}", results, allow_theme_fail)
    cdp.eval_js('document.querySelector(\'[data-tab-target="tab-attendance"]\').click()')
    time.sleep(0.1)

    # 2. Report hrefs on #tab-attendance + .xlsx / .csv HTTP 200 verification
    report_hrefs = cdp.eval_js(
        """(() => {
            const links = Array.from(document.querySelectorAll("#tab-attendance a[href]")).map(a => a.getAttribute("href"));
            return {
                xlsx: links.find(h => h.includes("xlsx")) || "",
                csv: links.find(h => h.includes("csv")) || ""
            };
        })()"""
    )
    s_lec = requests.Session()
    s_lec.cookies.set("cp_lecturer", seed.lecturer_cookie)

    r_xlsx_api = s_lec.get(f"{seed.base_url}/api/sessions/{seed.ariel1_id}/report?format=xlsx", timeout=5)
    r_csv_api = s_lec.get(f"{seed.base_url}/api/sessions/{seed.ariel1_id}/report?format=csv", timeout=5)
    if r_xlsx_api.status_code == 200 and r_xlsx_api.content.startswith(b"PK"):
        results.ok(f"Report API ?format=xlsx -> 200 ({len(r_xlsx_api.content)} bytes)")
    else:
        results.fail(f"Report API ?format=xlsx failed: {r_xlsx_api.status_code}")

    if r_csv_api.status_code == 200 and "ARIEL1" in r_csv_api.text and "Alice Cohen" in r_csv_api.text:
        results.ok(f"Report API ?format=csv -> 200 ({len(r_csv_api.content)} bytes)")
    else:
        results.fail(f"Report API ?format=csv failed: {r_csv_api.status_code}")

    for fmt, href in (("xlsx", report_hrefs.get("xlsx", "")), ("csv", report_hrefs.get("csv", ""))):
        if not href:
            results.fail(f"#tab-attendance missing {fmt} report <a> link")
            continue
        r_dom = s_lec.get(f"{seed.base_url}{href}", timeout=5)
        if r_dom.status_code == 200:
            results.ok(f"#tab-attendance DOM {fmt} href '{href}' -> 200")
        elif r_dom.status_code == 404 and "/reports/attendance." in href:
            results.theme_issue(
                f"#tab-attendance DOM {fmt} href '{href}' returned HTTP 404 (baseline bug fixed in M3)",
                allow_theme_fail,
            )
        else:
            results.fail(
                f"#tab-attendance DOM {fmt} href '{href}' returned HTTP {r_dom.status_code}"
            )

    # 3. #modal-hw-review & #modal-account-security audit logs
    cdp.eval_js('document.querySelector(\'[data-tab-target="tab-homework"]\').click()')
    if cdp.wait_for("Boolean(document.querySelector('#hw-submissions-list button[onclick*=\"openReviewModal\"]'))", 5.0):
        hw_ok = cdp.eval_js(
            """(() => {
                document.querySelector('#hw-submissions-list button[onclick*="openReviewModal"]').click();
                const body = document.getElementById("modal-hw-review-body").innerText.trim();
                const open = document.getElementById("modal-hw-review").classList.contains("open");
                document.querySelector('#modal-hw-review button[onclick*="closeModal"], #modal-hw-review [data-modal-close]').click();
                return open && body.length > 20;
            })()"""
        )
        if hw_ok:
            results.ok("Interactive #modal-hw-review rendered seeded AI review & closed")
        else:
            results.fail("Interactive #modal-hw-review failed to render review content")
    else:
        results.fail("Interactive #modal-hw-review trigger missing")

    cdp.eval_js(
        """(() => {
            const trig = document.getElementById("lecturer-profile-trigger");
            const pop = document.getElementById("lecturer-profile-popover");
            if (trig && pop && pop.hidden) trig.click();
            const b = document.querySelector('button[onclick*="modal-account-security"]:not([onclick*="closeModal"]), [data-modal-open="modal-account-security"]');
            if (b) b.click();
        })()"""
    )
    cdp.eval_js('document.querySelector(\'#modal-account-security button[onclick*="loadAuditLogs"]\').click()')
    audit_ok = cdp.wait_for(
        "document.getElementById('security-audit-list').innerText.trim().length > 10 && !document.getElementById('security-audit-list').innerText.includes('Loading')",
        timeout=5.0,
    )
    cdp.eval_js('document.querySelector(\'#modal-account-security button[onclick*="closeModal"], #modal-account-security [data-modal-close]\').click()')
    if audit_ok:
        results.ok("Interactive #modal-account-security loaded real audit logs & closed")
    else:
        results.fail("Interactive #modal-account-security failed to populate audit logs")

    # 3b. Professor Phone Headcount QR modal -> auto-close on phone connect -> mobile +1/-1 stepper
    cdp.eval_js('document.querySelector(\'[data-tab-target="tab-attendance"]\').click()')
    cdp.eval_js('document.getElementById("btn-open-phone-cam-qr").click()')
    qr_ready = cdp.wait_for(
        "Boolean(document.getElementById('modal-phone-headcount-qr')?.classList.contains('open')) && "
        "String(document.getElementById('phone-cam-qr-img')?.src || '').startsWith('data:image/svg+xml') && "
        "String(document.getElementById('phone-cam-qr-link')?.href || '').includes('/headcount-cam/')",
        timeout=5.0,
    )
    pair_url = cdp.eval_js("document.getElementById('phone-cam-qr-link')?.href || ''") if qr_ready else ""
    if qr_ready and pair_url:
        # Simulate professor's phone scanning the QR code -> desktop modal must auto-close
        r_phone_open = requests.get(pair_url, timeout=5)
        auto_closed = cdp.wait_for(
            "!document.getElementById('modal-phone-headcount-qr').classList.contains('open') && "
            "document.getElementById('cv-status').innerText.toLowerCase().includes('phone connected')",
            timeout=5.0,
        )
        if r_phone_open.status_code == 200 and auto_closed:
            results.ok("Phone Headcount QR opened & auto-closed automatically upon phone connection")
        else:
            results.fail(f"Phone Headcount QR auto-close failed (http={r_phone_open.status_code}, closed={auto_closed})")

        # Open companion page on mobile viewport, audit geometry/theme, and test +1 / -1 stepper
        cdp.set_viewport("mobile")
        cdp.set_role_cookies("anonymous", seed)
        nav_cam = cdp.goto(pair_url, settle_s=0.45, results=results, label="t3_phone_headcount_cam")
        audit_theme_and_r2_dom(cdp, "phone_headcount_cam", results, allow_theme_fail, nav_cam)
        audit_geometry(cdp, "mobile", "phone_headcount_cam", results)
        step_res = cdp.eval_js(
            """(() => {
                const el = document.getElementById("phone-verified-count");
                const plus = document.getElementById("btn-step-plus");
                if (!el || !plus) return { ok: false };
                const before = parseInt(el.innerText.trim(), 10);
                plus.click();
                return { ok: true, before };
            })()"""
        )
        if step_res.get("ok"):
            expected_after = step_res["before"] + 1
            step_ok = cdp.wait_for(
                f"parseInt(document.getElementById('phone-verified-count').innerText.trim(), 10) === {expected_after} && "
                "document.getElementById('phone-sync-badge').innerText.includes('Synced')",
                timeout=4.0,
            )
            if step_ok:
                cdp.eval_js("document.getElementById('btn-step-minus').click()")
                time.sleep(0.3)
                results.ok("Phone Headcount companion +1/-1 stepper synced live")
            else:
                results.fail("Phone Headcount companion +1 stepper did not sync")
        # Restore desktop lecturer session for step 4
        cdp.set_viewport("desktop")
        cdp.set_role_cookies("lecturer", seed)
        cdp.goto(f"{seed.base_url}/teacher", settle_s=0.6, results=results, label="t3_teacher_restore_after_cam")
    else:
        results.fail("Phone Headcount QR modal did not generate QR SVG & pairing link")

    # 4. Create & Launch Live Poll on #tab-polls -> Vote as Alice on /student
    poll_title = "E2E Live Poll: Which pattern decouples adapters?"
    cdp.eval_js('document.querySelector(\'[data-tab-target="tab-polls"]\').click()')
    cdp.eval_js(
        f"""(() => {{
            document.getElementById("poll-question").value = {json.dumps(poll_title)};
            document.getElementById("poll-options").value = "Adapter Pattern\\nSingleton Abuse\\nHardcoded Globals";
            document.getElementById("poll-correct-idx").value = "1";
            document.querySelector('button[onclick*="createPoll(true)"]').click();
        }})()"""
    )
    poll_launched = cdp.wait_for(
        f"document.getElementById('live-poll-title').innerText.includes({json.dumps(poll_title)}) && document.getElementById('live-poll-status-pill').innerText.includes('Open')",
        timeout=6.0,
    )
    if poll_launched:
        results.ok("Lecturer created & launched live poll on #tab-polls")
    else:
        results.fail("Lecturer failed to create & launch live poll on #tab-polls")

    # Student navigates Course -> Course Hub -> Lectures -> Lecture View on /student
    cdp.set_viewport("mobile")
    cdp.set_role_cookies("alice", seed)
    nav_stu = cdp.goto(f"{seed.base_url}/student", settle_s=0.7, results=results, label="t3_student_app")
    audit_theme_and_r2_dom(cdp, "student_app", results, allow_theme_fail, nav_stu)
    for btn_id, view_id in STUDENT_VIEWS[1:]:
        cdp.clear_buffers()
        cdp.eval_js(f"document.getElementById('{btn_id}').click()")
        time.sleep(0.25)
        ev_sv = cdp.classify_events(cdp.drain_events(0.25))
        if ev_sv["fatal_errors"]:
            results.fail(f"[t3_student_app:{view_id}] Runtime/Network error: {ev_sv['fatal_errors']}")
        audit_theme_and_r2_dom(cdp, f"student_app:{view_id}", results, allow_theme_fail)
    cdp.eval_js(
        f"""(() => {{
            openCourseHub({seed.cs204_id});
            navigateStudent({{ screen: 'course-lectures', courseId: {seed.cs204_id} }});
            openStudentLecture({seed.cs204_id}, {json.dumps(seed.ariel1_code)});
        }})()"""
    )
    time.sleep(0.25)

    poll_seen = cdp.wait_for(
        f"document.getElementById('student-poll-box').innerText.includes({json.dumps(poll_title)}) && document.querySelectorAll('#student-poll-box button.cp-poll-opt, #student-poll-box button.poll-opt').length >= 3",
        timeout=5.0,
    )
    if poll_seen:
        cdp.clear_buffers()
        cdp.eval_js("document.querySelectorAll('#student-poll-box button.cp-poll-opt, #student-poll-box button.poll-opt')[0].click()")
        voted = cdp.wait_for(
            "document.getElementById('student-poll-badge').innerText.includes('Vote Recorded') && !document.getElementById('student-poll-badge').innerText.includes('Live') && Boolean(document.querySelector('#student-poll-box button.cp-poll-opt.selected, #student-poll-box button.poll-opt.selected'))",
            timeout=5.0,
        )
        ev_vote = cdp.classify_events(cdp.drain_events(0.3))
        if ev_vote["fatal_errors"]:
            results.fail(f"Student poll vote errors: {ev_vote['fatal_errors']}")
        if voted:
            results.ok("Student vote recorded cleanly on #student-poll-box without Live badge clutter")
        else:
            results.fail("Student poll vote UI did not update to Vote Recorded")
    else:
        results.fail("Launched poll did not appear on #screen-lecture")

    # 5. Student Q&A post & upvote + Offline Question post + chat_popout verification
    qa_text = "E2E Question: Does Adapter support async summaries?"
    cdp.clear_buffers()
    cdp.eval_js(
        f"""(() => {{
            document.getElementById("student-chat-cat").value = "Question";
            document.getElementById("student-chat-body").value = {json.dumps(qa_text)};
            document.querySelector("#screen-lecture .chat-composer button[onclick*='sendStudentChat']").click();
        }})()"""
    )
    msg_posted = cdp.wait_for(
        f"Array.from(document.querySelectorAll('#student-chat-feed [data-msg-id]')).some(el => el.innerText.includes({json.dumps(qa_text)}))",
        timeout=5.0,
    )
    if msg_posted:
        results.ok("Student Q&A message posted and rendered with [data-msg-id]")
        cdp.clear_buffers()
        cdp.eval_js(
            f"""(() => {{
                const item = Array.from(document.querySelectorAll('#student-chat-feed [data-msg-id]')).find(el => el.innerText.includes({json.dumps(qa_text)}));
                item.querySelector('button.cp-upvote-pill, button.upvote-pill, button[onclick*="upvote"]').click();
            }})()"""
        )
        ev_up = cdp.classify_events(cdp.drain_events(0.6))
        if ev_up["fatal_errors"]:
            results.fail(f"Chat upvote errors: {ev_up['fatal_errors']}")
        up_200 = any("/api/chat/" in u and "/upvote" in u and st == 200 for u, st in ev_up["responses"])
        if up_200:
            results.ok("Student Q&A upvote POST /api/chat/<id>/upvote -> 200")
        else:
            results.fail(f"Student Q&A upvote did not observe HTTP 200 ({ev_up['responses']})")
    else:
        results.fail("Student Q&A post did not render in #student-chat-feed")

    offline_q_text = "E2E Offline Question: Will slides be uploaded?"
    cdp.eval_js(
        f"""(() => {{
            document.getElementById("student-offline-body").value = {json.dumps(offline_q_text)};
            document.getElementById("btn-send-offline-q").click();
        }})()"""
    )
    offline_ok = cdp.wait_for(
        f"Array.from(document.querySelectorAll('#student-offline-feed [data-msg-id]')).some(el => el.innerText.includes({json.dumps(offline_q_text)}))",
        timeout=5.0,
    )
    if offline_ok:
        results.ok("Student Offline Question submitted and rendered in #student-offline-feed")
    else:
        results.fail("Student Offline Question did not render in #student-offline-feed")

    # Lecturer chat_popout
    cdp.set_viewport("popout")
    cdp.set_role_cookies("lecturer", seed)
    nav_pop = cdp.goto(
        f"{seed.base_url}/lecturer/chat-popout/{seed.ariel1_code}",
        settle_s=0.6,
        results=results,
        label="t3_chat_popout",
    )
    audit_theme_and_r2_dom(cdp, "chat_popout", results, allow_theme_fail, nav_pop)
    pop_check = cdp.eval_js(
        f"""(() => {{
            const msgs = document.querySelectorAll("#feed [data-msg-id], #feed .cp-chat-msg, #feed .msg");
            const withId = document.querySelectorAll("#feed [data-msg-id]");
            const hasQa = Array.from(msgs).some(m => m.innerText.includes({json.dumps(qa_text)}));
            return {{ count: msgs.length, withId: withId.length, hasQa }};
        }})()"""
    )
    if pop_check["count"] >= 3 and pop_check["hasQa"]:
        results.ok(f"chat_popout renders posted Q&A feed ({pop_check['count']} msgs)")
    else:
        results.fail(f"chat_popout missing expected messages: {pop_check}")
    if pop_check["withId"] == 0:
        results.theme_issue(
            "[chat_popout] #feed messages missing [data-msg-id] attribute (added in M2)",
            allow_theme_fail,
        )

    # 6. First-Time Student Onboarding on /s/ARIEL1 -> Verified
    cdp.set_viewport("mobile")
    cdp.set_role_cookies("anonymous", seed)
    nav_chk = cdp.goto(
        seed.fresh_checkin_url(seed.ariel1_code),
        settle_s=0.5,
        results=results,
        label="t3_student_checkin_onboarding",
    )
    audit_theme_and_r2_dom(cdp, "student_checkin", results, allow_theme_fail, nav_chk)

    cdp.clear_buffers()
    cdp.eval_js(
        """(() => {
            document.getElementById("full_name").value = "Ohad Ben Nun";
            document.getElementById("student_id_number").value = "208444444";
            document.querySelector('form[action="/api/attendance/checkin"] button[type="submit"]').click();
        })()"""
    )
    onboard_ok = cdp.wait_for(
        "document.body.innerText.includes('Attendance Recorded') && (document.getElementById('student-identity-line')?.textContent || '').includes('Ohad Ben Nun') && Boolean(document.getElementById('student-nav-drawer')) && Boolean(document.getElementById('screen-lecture')) && document.getElementById('screen-lecture').classList.contains('active') && window.location.pathname === '/student'",
        timeout=6.0,
    )
    ev_onb = cdp.classify_events(cdp.drain_events(0.5))
    if ev_onb["fatal_errors"]:
        results.fail(f"Student onboarding submit errors: {ev_onb['fatal_errors']}")
    if onboard_ok:
        results.ok("First-time student onboarding form submitted -> Direct Lecture View with Attendance Recorded")
    else:
        results.fail("First-time student onboarding failed to open Lecture View with Attendance Recorded!")

    # 6b. Progressive Disclosure Drill-Down, Web+Mobile Back Button & Upload Dropzone Verifications
    if onboard_ok:
        # Back steps cleanly from Lecture -> Lectures -> Course Hub -> Courses Home (where Back hides)
        back_steps_ok = cdp.eval_js(
            """(() => {
                const backBtn = document.getElementById("student-back-btn");
                const vLecture = Boolean(backBtn && !backBtn.hidden && getComputedStyle(backBtn).display !== "none" && document.getElementById("screen-lecture").classList.contains("active"));
                backBtn.click();
                const vLectures = Boolean(document.getElementById("screen-course-lectures").classList.contains("active"));
                backBtn.click();
                const vHub = Boolean(document.getElementById("screen-course-hub").classList.contains("active"));
                backBtn.click();
                const vCourses = Boolean(document.getElementById("screen-courses").classList.contains("active") && backBtn.hidden);
                return { vLecture, vLectures, vHub, vCourses };
            })()"""
        )
        if all(back_steps_ok.values()):
            results.ok("Student Back stack traverses Lecture -> Lectures -> Course Hub -> Courses cleanly")
        else:
            results.fail(f"Student Back stack traversal failed: {back_steps_ok}")

        # Verify hardware/browser Back on Level 0 stays inside /student Courses
        cdp.eval_js("window.history.back()")
        time.sleep(0.35)
        stayed_on_home = cdp.eval_js(
            "window.location.pathname === '/student' && document.getElementById('screen-courses').classList.contains('active')"
        )
        if stayed_on_home:
            results.ok("Hardware/browser Back on Courses Home stays inside Student App (never returns to sign-in)")
        else:
            results.fail("Browser Back on Courses Home left Student App!")

        # Level 0 omits raw course codes; Level 2 Course Info displays it
        drill_ok = cdp.eval_js(
            f"""(() => {{
                const lvl0Text = document.getElementById("courses-list").innerText;
                const noCodeOnLvl0 = !lvl0Text.includes("CS204");
                openCourseHub({seed.cs204_id});
                document.getElementById("hub-tile-info").click();
                const infoCode = document.getElementById("course-info-number")?.innerText?.trim() || "";
                document.getElementById("student-back-btn").click();
                document.getElementById("hub-tile-assignments").click();
                return {{ noCodeOnLvl0, infoCode, hasAssignmentsScreen: document.getElementById("screen-course-assignments").classList.contains("active") }};
            }})()"""
        )
        if drill_ok["noCodeOnLvl0"] and drill_ok["infoCode"] == "CS204" and drill_ok["hasAssignmentsScreen"]:
            results.ok("Student Course Info isolates course number while Level 0 hides raw codes & Assignments opens cleanly")
        else:
            results.fail(f"Student drill-down / Course Info check failed: {drill_ok}")

        # Check drawer Back closes drawer without losing screen, AND check Desktop Back button visibility
        cdp.eval_js("document.getElementById('student-drawer-toggle').click()")
        time.sleep(0.15)
        cdp.eval_js("handleStudentBack()")
        time.sleep(0.15)
        drawer_closed_stayed = cdp.eval_js(
            "!document.getElementById('student-nav-drawer').classList.contains('open') && document.getElementById('screen-course-assignments').classList.contains('active')"
        )
        cdp.set_viewport("desktop")
        desktop_back_vis = cdp.eval_js(
            "Boolean(!document.getElementById('student-back-btn').hidden && getComputedStyle(document.getElementById('student-back-btn')).display !== 'none')"
        )
        if drawer_closed_stayed and desktop_back_vis:
            results.ok("Student persistent Back button verified on BOTH Mobile (drawer close) and Desktop")
        else:
            results.fail(f"Student Back button check failed (drawer={drawer_closed_stayed}, desktop={desktop_back_vis})")

    # 7. Remaining views theme & R2 audit: portal_home & projector
    cdp.set_viewport("desktop")
    cdp.set_role_cookies("anonymous", seed)
    nav_port = cdp.goto(f"{seed.base_url}/portal", results=results, label="t3_portal_home")
    audit_theme_and_r2_dom(cdp, "portal_home", results, allow_theme_fail, nav_port)

    cdp.set_viewport("projector_1080")
    nav_proj = cdp.goto(
        f"{seed.base_url}/projector/{seed.ariel1_code}?ptok={seed.projector_token}",
        results=results,
        label="t3_projector",
    )
    audit_theme_and_r2_dom(cdp, "projector", results, allow_theme_fail, nav_proj)

    # 8. Cross-view --cp-* token consistency
    views_with_tokens = {
        k: v for k, v in results.tokens_by_view.items() if all(v.get(t) for t in THEME_TOKENS)
    }
    if len(views_with_tokens) == len(results.tokens_by_view) and views_with_tokens:
        baseline_toks = next(iter(views_with_tokens.values()))
        mismatches = {
            k: v for k, v in views_with_tokens.items() if v != baseline_toks
        }
        if mismatches:
            results.theme_issue(
                f"Cross-view --cp-* token mismatch: {mismatches}",
                allow_theme_fail,
            )
        else:
            results.ok(f"Matching --cp-* design tokens across all {len(views_with_tokens)} audited views")


def run_tier4(
    seed: SeedContext,
    cdp: CDPDriver,
    shots_dir: Path,
    results: AuditResults,
) -> None:
    print(f"\n=== TIER 4: Real-World Classroom Showcase & Screenshots ({shots_dir}) ===")
    shots_dir.mkdir(parents=True, exist_ok=True)

    # 1. Portal Home (Desktop + Mobile)
    cdp.set_role_cookies("anonymous", seed)
    cdp.set_viewport("desktop")
    cdp.goto(f"{seed.base_url}/portal", results=results, label="shot_portal_desktop")
    cdp.capture_png(shots_dir / "01_portal_home_desktop_1440x900.png", (1440, 900), results)

    cdp.set_viewport("mobile")
    cdp.capture_png(shots_dir / "02_portal_home_mobile_390x844.png", (390, 844), results)

    # 2. Teacher Login (Desktop)
    cdp.set_viewport("desktop")
    cdp.goto(f"{seed.base_url}/teacher/login", results=results, label="shot_teacher_login")
    cdp.capture_png(shots_dir / "03_teacher_login_desktop_1440x900.png", (1440, 900), results)

    # 3. Teacher Portal (all 6 tabs + 2 rich modals + tablet + mobile)
    cdp.set_role_cookies("lecturer", seed)
    cdp.set_viewport("desktop")
    cdp.goto(f"{seed.base_url}/teacher", settle_s=0.75, results=results, label="shot_teacher_portal")

    tab_shots = [
        ("tab-overview", "04_teacher_tab0_overview_1440x900.png"),
        ("tab-attendance", "04b_teacher_tab1_attendance_1440x900.png"),
        ("tab-polls", "05_teacher_tab2_polls_1440x900.png"),
        ("tab-chat", "06_teacher_tab3_chat_1440x900.png"),
        ("tab-ai-lectures", "07_teacher_tab4_ai_lectures_1440x900.png"),
        ("tab-homework", "08_teacher_tab5_homework_1440x900.png"),
        ("tab-course-roster", "09_teacher_tab6_roster_1440x900.png"),
        ("tab-course-details", "09b_teacher_tab7_details_1440x900.png"),
    ]
    for tab_id, fname in tab_shots:
        cdp.eval_js(f'document.querySelector(\'[data-tab-target="{tab_id}"]\').click()')
        time.sleep(0.25)
        cdp.capture_png(shots_dir / fname, (1440, 900), results)

    # Modal HW Review
    cdp.eval_js('document.querySelector(\'[data-tab-target="tab-homework"]\').click()')
    if cdp.wait_for("Boolean(document.querySelector('#hw-submissions-list button[onclick*=\"openReviewModal\"]'))", 4.0):
        cdp.eval_js('document.querySelector(\'#hw-submissions-list button[onclick*="openReviewModal"]\').click()')
        time.sleep(0.2)
        cdp.capture_png(shots_dir / "10_teacher_modal_hw_review_1440x900.png", (1440, 900), results)
        cdp.eval_js('document.querySelector(\'#modal-hw-review button[onclick*="closeModal"], #modal-hw-review [data-modal-close]\').click()')
    else:
        results.fail("Tier 4 screenshot 10_teacher_modal_hw_review_1440x900.png skipped: openReviewModal trigger never appeared")

    # Modal Security
    cdp.eval_js(
        """(() => {
            const trig = document.getElementById("lecturer-profile-trigger");
            const pop = document.getElementById("lecturer-profile-popover");
            if (trig && pop && pop.hidden) trig.click();
            const b = document.querySelector('button[onclick*="modal-account-security"]:not([onclick*="closeModal"]), [data-modal-open="modal-account-security"]');
            if (b) b.click();
        })()"""
    )
    cdp.eval_js('document.querySelector(\'#modal-account-security button[onclick*="loadAuditLogs"]\').click()')
    time.sleep(0.3)
    cdp.capture_png(shots_dir / "11_teacher_modal_security_1440x900.png", (1440, 900), results)
    cdp.eval_js('document.querySelector(\'#modal-account-security button[onclick*="closeModal"], #modal-account-security [data-modal-close]\').click()')

    # Tablet & Mobile Teacher Portal
    cdp.eval_js('document.querySelector(\'[data-tab-target="tab-attendance"]\').click()')
    cdp.set_viewport("tablet")
    cdp.capture_png(shots_dir / "12_teacher_tablet_1024x768.png", (1024, 768), results)
    cdp.set_viewport("mobile")
    cdp.capture_png(shots_dir / "13_teacher_mobile_390x844.png", (390, 844), results)

    # 4. Projector (1920x1080 & 1280x720)
    cdp.set_viewport("projector_1080")
    cdp.goto(
        f"{seed.base_url}/projector/{seed.ariel1_code}?ptok={seed.projector_token}",
        results=results,
        label="shot_projector_1080",
    )
    cdp.capture_png(shots_dir / "14_projector_1920x1080.png", (1920, 1080), results)
    cdp.set_viewport("projector_720")
    cdp.capture_png(shots_dir / "15_projector_1280x720.png", (1280, 720), results)

    # 5. Chat Popout (480x800)
    cdp.set_viewport("popout")
    cdp.goto(
        f"{seed.base_url}/lecturer/chat-popout/{seed.ariel1_code}",
        results=results,
        label="shot_chat_popout",
    )
    cdp.capture_png(shots_dir / "16_chat_popout_480x800.png", (480, 800), results)

    # 6. Student App Mobile (Courses Home, Assignments, Summaries + Lecture View)
    cdp.set_role_cookies("alice", seed)
    cdp.set_viewport("mobile")
    cdp.goto(f"{seed.base_url}/student", settle_s=0.7, results=results, label="shot_student_app")
    for btn_id, fname in (
        ("nav-student-courses", "17_student_courses_mobile_390x844.png"),
        ("nav-student-assignments", "18_student_assignments_mobile_390x844.png"),
        ("nav-student-summaries", "19_student_summaries_mobile_390x844.png"),
    ):
        cdp.eval_js(f"document.getElementById('{btn_id}').click()")
        time.sleep(0.2)
        cdp.capture_png(shots_dir / fname, (390, 844), results)

    cdp.eval_js("document.getElementById('nav-student-courses').click()")
    cdp.eval_js("document.getElementById('student-drawer-toggle').click()")
    time.sleep(0.3)
    cdp.capture_png(shots_dir / "20b_student_drawer_mobile_390x844.png", (390, 844), results)
    cdp.eval_js("document.getElementById('nav-student-courses').click()")
    time.sleep(0.25)
    cdp.set_viewport("mobile_sm")
    cdp.capture_png(shots_dir / "20_student_courses_small_360x740.png", (360, 740), results)

    # 7. Student Check-In (verified, onboarding, 403 error)
    cdp.set_viewport("mobile")
    cdp.goto(seed.fresh_checkin_url(seed.ariel1_code), results=results, label="shot_checkin_verified")
    cdp.capture_png(shots_dir / "21_checkin_verified_390x844.png", (390, 844), results)

    cdp.set_role_cookies("anonymous", seed)
    cdp.goto(seed.fresh_checkin_url(seed.ariel1_code), results=results, label="shot_checkin_onboarding")
    cdp.capture_png(shots_dir / "22_checkin_onboarding_390x844.png", (390, 844), results)

    cdp.goto(
        seed.fresh_checkin_url(seed.closed1_code),
        allow_statuses=(403,),
        results=results,
        label="shot_checkin_error_403",
    )
    cdp.capture_png(shots_dir / "23_checkin_error_403_390x844.png", (390, 844), results)

    # 8. Professor Phone Headcount Companion (390x844)
    s_lec = requests.Session()
    s_lec.cookies.set("cp_lecturer", seed.lecturer_cookie)
    pair_r = s_lec.post(
        f"{seed.base_url}/api/sessions/{seed.ariel1_id}/headcount-cam/pair",
        json={},
        timeout=5,
    )
    if pair_r.status_code == 200:
        cam_url = pair_r.json().get("url", "")
        if cam_url:
            cdp.set_viewport("mobile")
            cdp.set_role_cookies("anonymous", seed)
            cdp.goto(cam_url, settle_s=0.45, results=results, label="shot_phone_headcount_cam")
            cdp.capture_png(shots_dir / "24_phone_headcount_cam_390x844.png", (390, 844), results)


def parse_args(argv: Optional[list[str]] = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="ClassPulse Opaque-Box E2E + Headless Chromium CDP Verification Suite"
    )
    parser.add_argument(
        "--tier",
        choices=["1", "2", "3", "4", "all"],
        default="all",
        help="Verification tier to run (default: all)",
    )
    parser.add_argument(
        "--smoke-only",
        action="store_true",
        help="Run fast Tier 1 route/console sanity check + clean teardown",
    )
    parser.add_argument(
        "--screenshots-dir",
        type=str,
        default="",
        help="Directory for Tier 4 PNG screenshots (default: temp dir outside .agents/teamwork/)",
    )
    parser.add_argument(
        "--allow-theme-fail",
        action="store_true",
        help="Downgrade known pre-overhaul baseline theme/R2-css/report-href/drawer/popout-id diffs to [WARN]",
    )
    return parser.parse_args(argv)


def main(argv: Optional[list[str]] = None) -> int:
    args = parse_args(argv)
    tiers_to_run = (
        {"1"}
        if args.smoke_only
        else ({"1", "2", "3", "4"} if args.tier == "all" else {args.tier})
    )
    shots_dir: Optional[Path] = None
    if "4" in tiers_to_run:
        if args.screenshots_dir:
            shots_dir = Path(args.screenshots_dir).resolve()
        else:
            shots_dir = Path(tempfile.mkdtemp(prefix="cp_e2e_shots_"))
    elif args.screenshots_dir:
        shots_dir = Path(args.screenshots_dir).resolve()

    print(f"Screenshots directory: {shots_dir if shots_dir is not None else 'n/a (Tier 4 not selected)'}")
    print(f"Mode: {'smoke-only (Tier 1)' if args.smoke_only else f'tier={args.tier}'}, allow_theme_fail={args.allow_theme_fail}")

    results = AuditResults()
    srv = EphemeralServer()
    cdp: Optional[CDPDriver] = None

    try:
        seed = srv.start()
        print(f"Ephemeral server started at {seed.base_url} (temp={srv.tmp_dir})")

        cdp = CDPDriver(seed.base_url)
        cdp.start()
        print(f"Headless Chromium CDP started on port {cdp.port}")

        if "1" in tiers_to_run:
            run_tier1(srv, seed, cdp, results, args.allow_theme_fail)
        if "2" in tiers_to_run:
            run_tier2(seed, cdp, results, args.allow_theme_fail)
        if "3" in tiers_to_run:
            run_tier3(seed, cdp, results, args.allow_theme_fail)
        if "4" in tiers_to_run:
            assert shots_dir is not None
            run_tier4(seed, cdp, shots_dir, results)

        if srv.server_errors:
            results.fail(f"Server emitted 5xx errors: {srv.server_errors}")
        else:
            results.ok("Zero server 5xx errors across executed tiers")

    except Exception as exc:
        results.fail(f"Unhandled E2E runner exception: {type(exc).__name__}: {exc}")
    finally:
        if cdp is not None:
            cdp.close()
        srv.stop()

    print("\n" + "=" * 68)
    print(
        f"E2E SUMMARY: passes={len(results.passes)}, "
        f"warnings={len(results.warnings)}, "
        f"failures={len(results.failures)}, "
        f"screenshots={len(results.screenshots)}"
    )
    print(f"Screenshots path: {shots_dir if shots_dir is not None else 'n/a'}")
    if results.warnings:
        print("\nWarnings:")
        for w in results.warnings[:20]:
            print(f"  - {w}")
    if results.failures:
        print("\nFailures:")
        for f in results.failures:
            print(f"  - {f}")
    print("=" * 68)

    return 0 if not results.failures else 1


if __name__ == "__main__":
    raise SystemExit(main())
