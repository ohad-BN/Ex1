"""Unified Test Suite Runner & Fault Localization Engine (ClassPulse / Ex1).

Runs all suites cleanly in one terminal with crisp pass/fail counts and compact
failure cards pinpointing exact failing lines and root causes.

Usage:
    python run_all_tests.py              # Fast suite (skips heavy @slow CV/ffmpeg)
    python run_all_tests.py --all        # Full suite (includes real SCRFD/ffmpeg)
    python run_all_tests.py --e2e        # Include Headless Chrome E2E Tier 1 checks
    python run_all_tests.py --self-test  # In-memory injected bug demo ("testing the tester")
"""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile
import time
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from tests import diagnostics  # noqa: E402
from tests.conftest import make_isolated_app  # noqa: E402
from tests.diagnostics import FaultLocalizationEngine  # noqa: E402

CLASSPULSE_SUITES: list[dict[str, str]] = [
    {"id": "Suite 1", "name": "Foundation, DB & WAL Concurrency", "files": "tests/test_foundation_models.py"},
    {"id": "Suite 2", "name": "Auth, Security, RBAC & Profile", "files": "tests/test_auth_and_security.py tests/test_gaps_auth_and_courses.py"},
    {"id": "Suite 3", "name": "Lecturer Isolation & Mobile Portals", "files": "tests/test_lecturer_isolation.py tests/test_multi_course_portals.py tests/test_mobile_student_fixes.py"},
    {"id": "Suite 4", "name": "QR Attendance, Headcount & Phone Cam", "files": "tests/test_qr_and_attendance.py tests/test_headcount_and_reports.py tests/test_phone_headcount_cam.py"},
    {"id": "Suite 5", "name": "Polls, Chat, Notifications & Guards", "files": "tests/test_polls_socketio.py tests/test_chat_core.py tests/test_chat_notifications_tabs.py tests/test_gaps_attendance_headcount_polls_chat.py"},
    {"id": "Suite 6", "name": "Assignments, Ranking & Isolation", "files": "tests/test_homework_incremental.py tests/test_gaps_homework_ranking_security.py"},
    {"id": "Suite 7", "name": "Lecture Summarization & Video Pipeline", "files": "tests/test_lecture_pipeline.py tests/test_gaps_lecture_pipeline_security.py"},
    {"id": "Suite 8", "name": "Multi-Tenant UX & Isolation Overhaul", "files": "tests/test_overhaul_isolation_and_ux.py"},
    {"id": "Suite 9", "name": "Frontend Static JS Unit Tests (Node)", "files": "tests/test_frontend_js.py"},
    {"id": "Suite 10", "name": "Diagnostic Engine Self-Verification", "files": "tests/test_diagnostics.py"},
]


def _parse_pytest_counts(output: str, returncode: int) -> tuple[int, int]:
    """Extract (passed, failed) test counts from pytest summary footer."""
    summary_line = next(
        (
            ln
            for ln in reversed(output.splitlines())
            if " in " in ln and ("passed" in ln or "failed" in ln or "error" in ln)
        ),
        "",
    )
    m_pass = re.search(r"(\d+)\s+passed", summary_line)
    m_fail = re.search(r"(\d+)\s+failed", summary_line)
    m_err = re.search(r"(\d+)\s+error", summary_line)
    passed = int(m_pass.group(1)) if m_pass else 0
    failed = (int(m_fail.group(1)) if m_fail else 0) + (int(m_err.group(1)) if m_err else 0)
    if returncode != 0 and failed == 0:
        failed = 1
    return passed, failed


def _read_last_pytest_failures() -> list[dict[str, Any]]:
    """Read structured failures captured by conftest during the just-finished suite."""
    if not diagnostics.PYTEST_JSON_FILE.is_file():
        return []
    try:
        data = json.loads(diagnostics.PYTEST_JSON_FILE.read_text(encoding="utf-8"))
        return list(data.get("failures") or [])
    except Exception:
        return []


def _run_pytest_suite(
    suite: dict[str, str], *, include_slow: bool
) -> tuple[bool, int, int, float, list[dict[str, Any]]]:
    """Run one pytest suite quietly and return (ok, passed, failed, sec, failure_records)."""
    cmd = [sys.executable, "-m", "pytest", "-q", "--tb=short"]
    if not include_slow:
        cmd.extend(["-m", "not slow"])
    cmd.extend(suite["files"].split())

    env = os.environ.copy()
    env["PYTHONIOENCODING"] = "utf-8"

    t0 = time.perf_counter()
    proc = subprocess.run(
        cmd,
        cwd=str(PROJECT_ROOT),
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        env=env,
    )
    elapsed = time.perf_counter() - t0
    combined = proc.stdout + ("\n" + proc.stderr if proc.stderr else "")
    ok = proc.returncode == 0
    passed, failed = _parse_pytest_counts(combined, proc.returncode)
    failure_records: list[dict[str, Any]] = []
    if not ok:
        failure_records = _read_last_pytest_failures()
        if not failure_records:
            failure_records = [
                FaultLocalizationEngine.analyze_raw_output(
                    suite["id"], suite["name"], suite["files"], combined
                )
            ]
    return ok, passed, failed, elapsed, failure_records


def _run_e2e_tier1() -> tuple[bool, int, int, float, list[dict[str, Any]]]:
    """Run Headless Chromium E2E Tier 1 checks quietly and extract counts."""
    cmd = [
        sys.executable,
        str(PROJECT_ROOT / "scripts" / "e2e_ui_verify.py"),
        "--tier",
        "1",
        "--allow-theme-fail",
    ]
    env = os.environ.copy()
    env["PYTHONIOENCODING"] = "utf-8"
    t0 = time.perf_counter()
    proc = subprocess.run(
        cmd,
        cwd=str(PROJECT_ROOT),
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        env=env,
    )
    elapsed = time.perf_counter() - t0
    combined = proc.stdout + ("\n" + proc.stderr if proc.stderr else "")
    ok = proc.returncode == 0
    m = re.search(r"passes=(\d+).*?failures=(\d+)", combined)
    passed = int(m.group(1)) if m else (1 if ok else 0)
    failed = int(m.group(2)) if m else (0 if ok else 1)
    if not ok and failed == 0:
        failed = 1

    failure_records: list[dict[str, Any]] = []
    if not ok:
        e2e_json = PROJECT_ROOT / "tests" / "logs" / "e2e_failure_report.json"
        if e2e_json.is_file():
            try:
                data = json.loads(e2e_json.read_text(encoding="utf-8"))
                for rec in data.get("failures") or []:
                    failure_records.append(
                        {
                            "test_name": f"E2E: {rec.get('function') or 'check'}",
                            "nodeid": f"E2E: {rec.get('function') or 'check'}",
                            "file": (rec.get("location") or "scripts/e2e_ui_verify.py:0").split(":")[0],
                            "line": int((rec.get("location") or ":0").split(":")[-1] or 0),
                            "location": rec.get("location") or "",
                            "test_location": "",
                            "code_snippet": rec.get("code_snippet") or [],
                            "exception_type": "E2EFailure",
                            "error_message": rec.get("message") or "",
                            "expected_vs_actual": {
                                "expected": rec.get("expected") or "",
                                "actual": rec.get("actual") or "",
                                "operator": "",
                                "diff": [],
                            },
                            "execution_context": {"recent_http_calls": []},
                            "root_cause_analysis": rec.get("root_cause_analysis") or {},
                        }
                    )
            except Exception:
                pass
        if not failure_records:
            failure_records = [
                FaultLocalizationEngine.analyze_raw_output(
                    "E2E", "Headless Chromium Tier 1", "scripts/e2e_ui_verify.py", combined
                )
            ]
    return ok, passed, failed, elapsed, failure_records


def execute_self_test() -> None:
    """Non-destructive 'Testing the Tester' simulation with 3 on-purpose bugs in memory."""
    records: list[dict[str, Any]] = []

    # 1. Production crash localization inside app/services/homework_pipeline.py:241
    simulated_tb = (
        "tests/test_gaps_homework_ranking_security.py:202: in test_corrupt_archive\n"
        "    resp = stu.post('/api/assignments/1/submissions', ...)\n"
        "app/services/homework_pipeline.py:241: in extract_from_zip_bytes\n"
        "    with zipfile.ZipFile(io.BytesIO(raw_bytes)) as zf:\n"
        "E   zipfile.BadZipFile: File is not a zip file\n"
    )
    rec1 = FaultLocalizationEngine.analyze_raw_output(
        "Sim 1", "Injected Zip Crash", "tests/test_gaps_homework_ranking_security.py", simulated_tb
    )
    assert rec1["file"] == "app/services/homework_pipeline.py" and rec1["line"] == 241
    records.append(rec1)

    # 2. Live HTTP request + deliberate assertion mismatch (200 vs 500)
    diagnostics.clear_http_trace()
    diagnostics.install_flask_client_tracer()
    try:
        with tempfile.TemporaryDirectory(prefix="cp_selftest_") as tmp_dir:
            test_app = make_isolated_app(Path(tmp_dir))
            try:
                resp = test_app.test_client().get("/healthz")
                try:
                    assert resp.status_code == 500, f"{resp.status_code} == 500"
                except AssertionError as exc:
                    rec2 = diagnostics.build_record_from_exception(
                        test_name="self_test_api_status_mismatch",
                        exc=exc,
                    )
                    assert rec2["expected_vs_actual"]["actual"] == "200"
                    assert rec2["expected_vs_actual"]["expected"] == "500"
                    assert rec2["execution_context"]["recent_http_calls"][-1]["path"] == "/healthz"
                    records.append(rec2)
            finally:
                test_app.extensions["task_manager"].shutdown(wait=True)
                test_app.extensions["db_engine"].dispose()
    finally:
        diagnostics.uninstall_flask_client_tracer()
        diagnostics.clear_http_trace()

    # 3. Live Missing-Key / Schema Error
    try:
        mock_payload = {"submission_type": "comparative", "version": 1}
        _ = mock_payload["class_standing"]
    except KeyError as exc:
        rec3 = diagnostics.build_record_from_exception(
            test_name="self_test_missing_key_error",
            exc=exc,
        )
        assert rec3["root_cause_analysis"]["category"] == "Missing Key / Schema Error"
        records.append(rec3)

    bar = "=" * 72
    print(bar)
    print(f"SELF-TEST: {len(records)} on-purpose bugs injected | {len(records)} caught | 0 missed")
    print(bar)
    for idx, rec in enumerate(records, start=1):
        print(diagnostics.format_console_block(rec, index=idx))
    print("All fault detectors verified (non-destructive, zero files created).")


def main() -> int:
    parser = argparse.ArgumentParser(description="ClassPulse Unified Test Runner")
    parser.add_argument("--all", action="store_true", help="Include @slow real SCRFD/ffmpeg tests")
    parser.add_argument("--e2e", action="store_true", help="Include Headless Chrome E2E Tier 1 checks")
    parser.add_argument("--self-test", action="store_true", help="Run in-memory injected bug demo")
    args = parser.parse_args()

    if args.self_test:
        execute_self_test()
        return 0

    mode_label = "full mode" if args.all else "fast mode"
    bar = "=" * 72
    print(bar)
    print(f"ClassPulse Test Run ({mode_label}" + (" + E2E" if args.e2e else "") + ")")
    print(bar)

    t_start = time.perf_counter()
    total_passed = 0
    total_failed = 0
    all_failures: list[dict[str, Any]] = []

    for suite in CLASSPULSE_SUITES:
        ok, passed, failed, dur, fail_recs = _run_pytest_suite(suite, include_slow=args.all)
        total_passed += passed
        total_failed += failed
        all_failures.extend(fail_recs)
        tag = "[PASS]" if ok else "[FAIL]"
        print(f"  {tag} {suite['id']:<8} {suite['name']:<38} | {passed:>2} passed, {failed:>2} failed | {dur:>5.1f}s")

    if args.e2e:
        ok_e2e, passed_e2e, failed_e2e, dur_e2e, fail_recs_e2e = _run_e2e_tier1()
        total_passed += passed_e2e
        total_failed += failed_e2e
        all_failures.extend(fail_recs_e2e)
        tag = "[PASS]" if ok_e2e else "[FAIL]"
        print(f"  {tag} {'E2E':<8} {'Headless Chrome Tier 1 Checks':<38} | {passed_e2e:>2} passed, {failed_e2e:>2} failed | {dur_e2e:>5.1f}s")

    total_sec = time.perf_counter() - t_start
    total_run = total_passed + total_failed
    print(bar)
    print(f"TOTAL: {total_run} tests run | {total_passed} passed | {total_failed} failed ({total_sec:.1f}s)")
    print(bar)

    if total_failed > 0:
        print(f"\nFAILURE DETAILS ({len(all_failures)}):")
        for idx, rec in enumerate(all_failures, start=1):
            print(diagnostics.format_console_block(rec, index=idx))
        return 1

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
