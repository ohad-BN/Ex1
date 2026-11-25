from __future__ import annotations

from collections import deque
from datetime import datetime, timezone
import difflib
import json
from pathlib import Path
import re
import threading
import traceback as tb_mod
from typing import Any, Optional

from flask.testing import FlaskClient

PROJECT_ROOT = Path(__file__).resolve().parent.parent
LOG_DIR = Path(__file__).resolve().parent / "logs"
PYTEST_LOG_FILE = LOG_DIR / "test_failures.log"
PYTEST_JSON_FILE = LOG_DIR / "test_failure_report.json"

_NOISE_PATH_MARKERS = (
    "site-packages",
    "_pytest",
    "pluggy",
    "werkzeug",
    "flask",
    "sqlalchemy",
    "importlib",
    "<frozen",
)

_LOCK = threading.Lock()
_HTTP_TRACE: deque[dict[str, Any]] = deque(maxlen=6)
_RECORDED_FAILURES: list[dict[str, Any]] = []
_RUN_STARTED_AT: str = ""
_ORIG_FLASK_OPEN: Any = None


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _rel_path(raw_path: str | Path) -> str:
    try:
        p = Path(raw_path)
        if not p.is_absolute():
            p = PROJECT_ROOT / p
        return p.resolve().relative_to(PROJECT_ROOT).as_posix()
    except Exception:
        return str(raw_path).replace("\\", "/")


def _abs_path(raw_path: str | Path) -> Path:
    p = Path(raw_path)
    return p if p.is_absolute() else (PROJECT_ROOT / p)


def _is_framework_frame(path_str: str) -> bool:
    norm = path_str.replace("\\", "/").lower()
    return any(marker in norm for marker in _NOISE_PATH_MARKERS)


def read_code_snippet(
    file_path: str | Path,
    target_line: int,
    *,
    radius: int = 3,
) -> list[dict[str, Any]]:
    """Read lines [target_line - radius .. target_line + radius] from file_path cleanly."""
    p = _abs_path(file_path)
    if not p.is_file() or target_line <= 0:
        return []
    try:
        lines = p.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return []
    start = max(1, target_line - radius)
    end = min(len(lines), target_line + radius)
    return [
        {"line": ln, "is_target": ln == target_line, "code": lines[ln - 1]}
        for ln in range(start, end + 1)
    ]


def reset_pytest_logs() -> None:
    """Clear and initialize tests/logs/test_failures.log and test_failure_report.json."""
    global _RUN_STARTED_AT
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    _RUN_STARTED_AT = _utc_now_iso()
    with _LOCK:
        _RECORDED_FAILURES.clear()
        _HTTP_TRACE.clear()
        PYTEST_LOG_FILE.write_text(
            f"ClassPulse Automated Test Failure Diagnostics Log\nRun Started: {_RUN_STARTED_AT}\n"
            + ("=" * 80)
            + "\n",
            encoding="utf-8",
        )
        PYTEST_JSON_FILE.write_text(
            json.dumps(
                {
                    "run_started_at": _RUN_STARTED_AT,
                    "total_failures": 0,
                    "failures": [],
                },
                indent=2,
            ),
            encoding="utf-8",
        )


def clear_http_trace() -> None:
    with _LOCK:
        _HTTP_TRACE.clear()


def get_http_trace() -> list[dict[str, Any]]:
    with _LOCK:
        return list(_HTTP_TRACE)


def get_recorded_failures() -> list[dict[str, Any]]:
    with _LOCK:
        return list(_RECORDED_FAILURES)


def install_flask_client_tracer() -> None:
    """Idempotently wrap FlaskClient.open to record recent HTTP request/response pairs."""
    global _ORIG_FLASK_OPEN
    if _ORIG_FLASK_OPEN is not None:
        return
    orig = FlaskClient.open
    _ORIG_FLASK_OPEN = orig

    def _traced_open(self: FlaskClient, *args: Any, **kwargs: Any):
        method = str(kwargs.get("method") or "GET").upper()
        raw_path = str(args[0]) if args else str(kwargs.get("path", ""))
        qs = kwargs.get("query_string")
        resp = orig(self, *args, **kwargs)
        preview = ""
        try:
            if not getattr(resp, "is_streamed", False):
                mime = str(getattr(resp, "mimetype", "") or "")
                if "json" in mime or "text" in mime:
                    raw_txt = resp.get_data(as_text=True) or ""
                    preview = re.sub(r"\s+", " ", raw_txt).strip()[:320]
        except Exception:
            preview = "<unreadable>"
        entry = {
            "method": method,
            "path": raw_path,
            "query_string": str(qs) if qs else "",
            "status_code": int(getattr(resp, "status_code", 0) or 0),
            "response_preview": preview,
        }
        with _LOCK:
            _HTTP_TRACE.append(entry)
        return resp

    FlaskClient.open = _traced_open  # type: ignore[method-assign]


def uninstall_flask_client_tracer() -> None:
    global _ORIG_FLASK_OPEN
    if _ORIG_FLASK_OPEN is not None:
        FlaskClient.open = _ORIG_FLASK_OPEN  # type: ignore[method-assign]
        _ORIG_FLASK_OPEN = None


def _extract_frames_from_excinfo(excinfo: Any) -> tuple[Optional[dict[str, Any]], Optional[dict[str, Any]]]:
    """Return (test_frame, app_frame) from pytest ExcInfo traceback."""
    test_frame: Optional[dict[str, Any]] = None
    app_frame: Optional[dict[str, Any]] = None

    if excinfo is not None and getattr(excinfo, "traceback", None) is not None:
        for entry in excinfo.traceback:
            fpath = str(entry.path)
            if _is_framework_frame(fpath):
                continue
            rel = _rel_path(fpath)
            lineno = int(entry.lineno) + 1
            func_name = getattr(entry, "name", "") or ""
            info = {
                "file": rel,
                "abs_file": str(Path(fpath).resolve()),
                "line": lineno,
                "func": func_name,
            }
            if rel.startswith("tests/") and rel not in ("tests/diagnostics.py", "tests/conftest.py"):
                test_frame = info
            if rel.startswith("app/"):
                app_frame = info
    return test_frame, app_frame


def extract_failure_location(
    excinfo: Any,
    fallback_file: Optional[str] = None,
    fallback_line: Optional[int] = None,
) -> dict[str, Any]:
    """Extract pinpointed fault location (preferring innermost app/ frame, else exact tests/ frame)."""
    test_frame, app_frame = _extract_frames_from_excinfo(excinfo)

    # Pinpoint innermost production crash line in app/ when present; otherwise exact assertion in tests/
    primary = app_frame or test_frame
    if primary is None and fallback_file:
        abs_f = str(_abs_path(fallback_file).resolve())
        primary = {
            "file": _rel_path(abs_f),
            "abs_file": abs_f,
            "line": int(fallback_line or 0),
            "func": "",
        }

    test_loc = f"{test_frame['file']}:{test_frame['line']}" if test_frame else ""
    app_loc = (
        f"{app_frame['file']}:{app_frame['line']}"
        + (f" in {app_frame['func']}()" if app_frame.get("func") else "")
        if app_frame
        else ""
    )
    loc_str = f"{primary['file']}:{primary['line']}" if primary else "unknown:0"
    snippet = (
        read_code_snippet(primary["abs_file"], int(primary["line"]))
        if primary and primary.get("abs_file")
        else []
    )

    return {
        "file": primary["file"] if primary else "unknown",
        "line": int(primary["line"]) if primary else 0,
        "location": loc_str,
        "test_location": test_loc,
        "app_culprit": app_loc,
        "code_snippet": snippet,
    }


def extract_expected_vs_actual(
    exc_type: str,
    exc_msg: str,
    longrepr_text: str,
) -> dict[str, Any]:
    """Parse pytest AssertionError representation lines into Expected vs Actual & optional diff."""
    assertion_lines = [
        ln[4:] if ln.startswith("E   ") else ln[2:]
        for ln in longrepr_text.splitlines()
        if ln.startswith(("E   ", "+   ", "E "))
    ]
    joined = "\n".join(assertion_lines)
    raw_assert = next(
        (ln.strip() for ln in assertion_lines if ln.strip().startswith("assert ")),
        "",
    )

    operator = ""
    actual_val = ""
    expected_val = ""

    m_bin = re.match(
        r"^assert\s+(.+?)\s+(==|!=|not in|in|is not|is|>=|<=|>|<)\s+(.+)$",
        raw_assert,
    )
    if m_bin:
        actual_val = m_bin.group(1).strip()
        operator = m_bin.group(2).strip()
        expected_val = m_bin.group(3).strip()
    elif exc_type == "AssertionError" and assertion_lines:
        actual_val = exc_msg.splitlines()[0] if exc_msg else ""

    diff_lines: list[str] = []
    if actual_val and expected_val and ("\n" in actual_val or len(actual_val) > 80 or len(expected_val) > 80):
        diff_lines = list(
            difflib.unified_diff(
                expected_val.splitlines(),
                actual_val.splitlines(),
                fromfile="expected",
                tofile="actual",
                lineterm="",
            )
        )

    return {
        "operator": operator,
        "expected": expected_val,
        "actual": actual_val,
        "raw_assertion": raw_assert if raw_assert else (f"{exc_type}: {exc_msg}"[:260] if exc_type != "AssertionError" else joined[:260]),
        "diff": diff_lines,
    }


def classify_root_cause(
    *,
    exc_type: str,
    exc_msg: str,
    test_name: str,
    app_culprit: str,
    location: str,
    http_trace: list[dict[str, Any]],
    exp_actual: dict[str, Any],
) -> dict[str, str]:
    """Produce plain-language engineering root-cause categorization + actionable culprit pointer."""
    low_msg = f"{exc_type} {exc_msg} {test_name} {exp_actual.get('raw_assertion', '')}".lower()
    last_http = http_trace[-1] if http_trace else None
    last_http_500 = next((h for h in reversed(http_trace) if h["status_code"] >= 500), None)
    actual_val = str(exp_actual.get("actual") or "").strip()

    if last_http_500 is not None or actual_val == "500":
        ep = last_http_500 or last_http
        culprit = app_culprit or (f"{ep['method']} {ep['path']} -> HTTP {ep['status_code']}" if ep else location)
        return {
            "category": "HTTP 500 Internal Server Error",
            "summary": "Backend route or service raised an unhandled exception returning HTTP 500.",
            "likely_culprit": culprit,
        }

    if exc_type in ("TimeoutError", "ReadTimeout", "ConnectTimeout", "subprocess.TimeoutExpired") or "timeout" in low_msg or "timed out" in low_msg:
        return {
            "category": "Timeout / Async Hang",
            "summary": "Operation exceeded timeout threshold (network, subprocess, or async background job did not settle).",
            "likely_culprit": app_culprit or location,
        }

    if "frontend_static_js" in test_name or "node --test" in low_msg:
        return {
            "category": "Frontend JS Unit Failure",
            "summary": "A JavaScript helper test in tests/test_frontend_js.mjs failed.",
            "likely_culprit": "tests/test_frontend_js.mjs / app/static/js/",
        }

    if exc_type in ("KeyError", "AttributeError", "JSONDecodeError", "ValidationError") or (
        exc_type == "TypeError" and ("argument" in low_msg or "subscriptable" in low_msg or "nonetype" in low_msg)
    ):
        return {
            "category": "Missing Key / Schema Error",
            "summary": f"Expected JSON field, dict key, or attribute was missing or malformed ({exc_type}: {exc_msg}).",
            "likely_culprit": app_culprit or (f"{last_http['method']} {last_http['path']}" if last_http else location),
        }

    if "integrityerror" in low_msg or "operationalerror" in low_msg or "sqlite" in low_msg or "invalidrequesterror" in low_msg:
        return {
            "category": "Database / SQLAlchemy Session Error",
            "summary": "Database constraint violation, SQLite lock, or detached ORM session occurred.",
            "likely_culprit": app_culprit or location,
        }

    if any(k in low_msg for k in ("isolation", "comparative", "cross_tenant", "peer", "pii", "leak", "unauthorized", "forbidden")) and (
        "403" in low_msg or "401" in low_msg or "not in" in low_msg or "isolation" in test_name.lower()
    ):
        return {
            "category": "Data Isolation / RBAC Breach",
            "summary": "Tenant/student/lecturer access boundary or comparative-vs-final privacy expectation did not match.",
            "likely_culprit": app_culprit or (f"{last_http['method']} {last_http['path']}" if last_http else location),
        }

    if last_http and last_http["status_code"] in (400, 401, 403, 404, 409, 429) and (
        "status_code" in low_msg or str(last_http["status_code"]) in low_msg
    ):
        return {
            "category": f"Auth / Validation Status Mismatch (HTTP {last_http['status_code']})",
            "summary": f"Endpoint {last_http['method']} {last_http['path']} returned HTTP {last_http['status_code']}: {last_http['response_preview'][:180]}",
            "likely_culprit": app_culprit or f"{last_http['method']} {last_http['path']}",
        }

    if exc_type == "AssertionError":
        return {
            "category": "Assertion Mismatch",
            "summary": f"Test assertion failed ({exp_actual.get('raw_assertion') or exc_msg}).",
            "likely_culprit": app_culprit or (f"{last_http['method']} {last_http['path']}" if last_http else location),
        }

    return {
        "category": f"Unhandled {exc_type}",
        "summary": f"Unexpected {exc_type} raised during execution: {exc_msg}",
        "likely_culprit": app_culprit or location,
    }


def build_record_from_exception(
    *,
    test_name: str,
    exc: BaseException,
    nodeid: Optional[str] = None,
    phase: str = "call",
    raw_assert: str = "",
) -> dict[str, Any]:
    """Build a structured failure record directly from a live caught Python exception (used by self-test & unit tests)."""
    exc_type = type(exc).__name__
    exc_msg = str(exc)
    tb = exc.__traceback__
    test_frame: Optional[dict[str, Any]] = None
    app_frame: Optional[dict[str, Any]] = None

    for frame_summary in tb_mod.extract_tb(tb):
        fpath = str(frame_summary.filename)
        if _is_framework_frame(fpath):
            continue
        rel = _rel_path(fpath)
        info = {
            "file": rel,
            "abs_file": str(Path(fpath).resolve()),
            "line": int(frame_summary.lineno),
            "func": frame_summary.name,
        }
        if rel.startswith("tests/") or rel == "run_all_tests.py":
            test_frame = info
        if rel.startswith("app/"):
            app_frame = info

    primary = app_frame or test_frame
    file_rel = primary["file"] if primary else "unknown"
    line_no = int(primary["line"]) if primary else 0
    snippet = read_code_snippet(primary["abs_file"], line_no) if primary else []

    if not raw_assert and exc_type == "AssertionError" and snippet:
        target_row = next((r for r in snippet if r["is_target"]), None)
        if target_row and "assert " in target_row["code"]:
            raw_assert = target_row["code"].strip()

    operator = ""
    actual_val = ""
    expected_val = ""
    first_msg_line = exc_msg.splitlines()[0] if exc_msg else ""
    if first_msg_line and "==" in first_msg_line:
        parts = first_msg_line.split("==", 1)
        actual_val = parts[0].strip()
        expected_val = parts[1].strip()
        operator = "=="
    elif raw_assert:
        m_bin = re.match(
            r"^assert\s+(.+?)\s+(==|!=|not in|in|is not|is|>=|<=|>|<)\s+(.+)$",
            raw_assert,
        )
        if m_bin:
            actual_val = m_bin.group(1).strip()
            operator = m_bin.group(2).strip()
            expected_val = m_bin.group(3).strip()

    exp_actual = {
        "operator": operator,
        "expected": expected_val,
        "actual": actual_val,
        "raw_assertion": raw_assert or f"{exc_type}: {exc_msg}"[:260],
        "diff": [],
    }
    http_trace = get_http_trace()
    app_culprit = (
        f"{app_frame['file']}:{app_frame['line']} in {app_frame['func']}()"
        if app_frame
        else ""
    )
    test_loc = f"{test_frame['file']}:{test_frame['line']}" if test_frame else ""
    location = f"{file_rel}:{line_no}"
    analysis = classify_root_cause(
        exc_type=exc_type,
        exc_msg=exc_msg,
        test_name=nodeid or test_name,
        app_culprit=app_culprit,
        location=location,
        http_trace=http_trace,
        exp_actual=exp_actual,
    )

    return {
        "timestamp": _utc_now_iso(),
        "test_name": test_name,
        "nodeid": nodeid or test_name,
        "phase": phase,
        "file": file_rel,
        "line": line_no,
        "location": location,
        "test_location": test_loc,
        "app_culprit": app_culprit,
        "code_snippet": snippet,
        "exception_type": exc_type,
        "error_message": exc_msg[:1500],
        "expected_vs_actual": exp_actual,
        "execution_context": {
            "fixtures": [],
            "params": {},
            "recent_http_calls": http_trace,
        },
        "root_cause_analysis": analysis,
    }


def build_pytest_failure_record(item: Any, call: Any, report: Any) -> dict[str, Any]:
    """Build complete structured diagnostic record for a failed pytest phase."""
    excinfo = getattr(call, "excinfo", None)
    exc_type = excinfo.typename if excinfo is not None else "Failure"
    exc_msg = str(excinfo.value) if excinfo is not None else str(getattr(report, "longreprtext", ""))
    longrepr_text = getattr(report, "longreprtext", "") or ""

    fspath, lineno, _domain = getattr(report, "location", ("", 0, ""))
    loc = extract_failure_location(
        excinfo,
        fallback_file=str(fspath) if fspath else None,
        fallback_line=(int(lineno) + 1) if lineno is not None else None,
    )
    http_trace = get_http_trace()
    exp_actual = extract_expected_vs_actual(exc_type, exc_msg, longrepr_text)
    analysis = classify_root_cause(
        exc_type=exc_type,
        exc_msg=exc_msg,
        test_name=str(item.nodeid),
        app_culprit=loc["app_culprit"],
        location=loc["location"],
        http_trace=http_trace,
        exp_actual=exp_actual,
    )

    callspec = getattr(item, "callspec", None)
    params = dict(callspec.params) if callspec is not None else {}
    fixtures = [f for f in getattr(item, "fixturenames", ()) if not str(f).startswith("_")]

    return {
        "timestamp": _utc_now_iso(),
        "test_name": str(item.name),
        "nodeid": str(item.nodeid),
        "phase": str(getattr(report, "when", "call")),
        "file": loc["file"],
        "line": loc["line"],
        "location": loc["location"],
        "test_location": loc["test_location"],
        "app_culprit": loc["app_culprit"],
        "code_snippet": loc["code_snippet"],
        "exception_type": exc_type,
        "error_message": exc_msg[:1500],
        "expected_vs_actual": exp_actual,
        "execution_context": {
            "fixtures": fixtures,
            "params": {k: repr(v)[:120] for k, v in params.items()},
            "recent_http_calls": http_trace,
        },
        "root_cause_analysis": analysis,
    }


def format_console_block(
    record: dict[str, Any],
    *,
    index: Optional[int] = None,
) -> str:
    """Format a failure record into a compact, human-readable diagnostic card."""
    rule = "-" * 72
    rca = record.get("root_cause_analysis") or {}
    err_line = record["error_message"].splitlines()[0] if record.get("error_message") else ""
    label = f"FAIL #{index}" if index is not None else "FAIL"
    title = record.get("test_name") or record.get("nodeid") or "unknown"

    where = f"{record.get('file', 'unknown')}:{record.get('line', 0)}"
    test_loc = record.get("test_location") or ""
    if test_loc and test_loc != record.get("location"):
        where += f"  (called from {test_loc})"

    lines: list[str] = [
        rule,
        f"{label}: {title}",
        f"Where : {where}",
        f"Why   : {rca.get('category', 'Unknown')} - {record.get('exception_type', 'Error')}: {err_line}",
    ]

    eva = record.get("expected_vs_actual") or {}
    if eva.get("expected") or eva.get("actual"):
        exp_str = eva.get("expected") or "?"
        act_str = eva.get("actual") or "?"
        op_str = f"  (op: {eva['operator']})" if eva.get("operator") else ""
        lines.append(f"Diff  : Expected {exp_str} | Got {act_str}{op_str}")
        for d in (eva.get("diff") or [])[:6]:
            lines.append(f"        {d}")

    ctx = record.get("execution_context") or {}
    http_calls = ctx.get("recent_http_calls") or []
    if http_calls:
        hc = http_calls[-1]
        qs = f"?{hc['query_string']}" if hc.get("query_string") else ""
        lines.append(f"HTTP  : {hc['method']} {hc['path']}{qs} -> {hc['status_code']}")

    if ctx.get("params"):
        lines.append(f"Params: {ctx['params']}")

    # Slice snippet to compact +-1 line window around the failing line for readable console output
    snippet = record.get("code_snippet") or []
    if snippet:
        target_idx = next((i for i, r in enumerate(snippet) if r.get("is_target")), None)
        if target_idx is not None:
            compact = snippet[max(0, target_idx - 1) : min(len(snippet), target_idx + 2)]
        else:
            compact = snippet[:3]
        lines.append("Code  :")
        for row in compact:
            marker = ">" if row.get("is_target") else " "
            lines.append(f"  {marker} {row['line']:>4} | {row['code']}")

    hint = rca.get("summary") or rca.get("likely_culprit") or ""
    if hint:
        lines.append(f"Hint  : {hint}")
    lines.append(rule)
    return "\n".join(lines)


class FaultLocalizationEngine:
    """Ex0-style unified facade for analyzing raw pytest output and localizing project faults."""

    _TB_PATTERNS = (
        re.compile(
            r'File\s+["\'](?P<path>[^"\']+)["\'],\s+line\s+(?P<line>\d+)(?:,\s+in\s+(?P<func>\w+))?',
            re.IGNORECASE,
        ),
        re.compile(
            r'(?P<path>[a-zA-Z0-9_\-./\\]+\.py):(?P<line>\d+):(?:\s+in\s+(?P<func>\w+))?',
            re.IGNORECASE,
        ),
    )

    @classmethod
    def parse_traceback_for_source_fault(
        cls, raw_output: str
    ) -> Optional[tuple[str, int, str, list[dict[str, Any]]]]:
        """Return innermost app/ frame if crashed in production code, else innermost tests/ frame."""
        matches: list[re.Match[str]] = []
        for pattern in cls._TB_PATTERNS:
            matches.extend(pattern.finditer(raw_output))
        matches.sort(key=lambda m: m.start())

        app_hit: Optional[tuple[str, int, str, list[dict[str, Any]]]] = None
        test_hit: Optional[tuple[str, int, str, list[dict[str, Any]]]] = None
        for match in matches:
            raw_path = match.group("path")
            if _is_framework_frame(raw_path):
                continue
            rel = _rel_path(raw_path)
            line_no = int(match.group("line"))
            if rel.startswith("app/"):
                app_hit = (rel, line_no, f"{rel}:{line_no}", read_code_snippet(rel, line_no))
            elif rel.startswith("tests/") and rel not in ("tests/diagnostics.py", "tests/conftest.py"):
                test_hit = (rel, line_no, f"{rel}:{line_no}", read_code_snippet(rel, line_no))

        return app_hit or test_hit

    @classmethod
    def analyze_raw_output(
        cls,
        suite_id: str,
        suite_name: str,
        test_target: str,
        raw_output: str,
    ) -> dict[str, Any]:
        """Parse subprocess pytest failure output into a standardized diagnostic record."""
        exc_type = "AssertionError"
        exc_msg = ""
        for line in reversed(raw_output.splitlines()):
            stripped = line.strip()
            if stripped.startswith("E "):
                clean = stripped[2:].strip()
                if ":" in clean and not clean.startswith("assert "):
                    exc_type, _, exc_msg = clean.partition(":")
                    exc_type = exc_type.strip()
                    exc_msg = exc_msg.strip()
                    break
                if not exc_msg:
                    exc_msg = clean

        if not exc_msg:
            exc_msg = f"{suite_name} failed without standard exception trace."

        hit = cls.parse_traceback_for_source_fault(raw_output)
        file_rel = hit[0] if hit else test_target.split()[0]
        line_no = hit[1] if hit else 0
        location = hit[2] if hit else file_rel
        snippet = hit[3] if hit else []

        exp_actual = extract_expected_vs_actual(exc_type, exc_msg, raw_output)
        analysis = classify_root_cause(
            exc_type=exc_type,
            exc_msg=exc_msg,
            test_name=f"{suite_id} ({suite_name})",
            app_culprit=location if file_rel.startswith("app/") else "",
            location=location,
            http_trace=[],
            exp_actual=exp_actual,
        )
        return {
            "timestamp": _utc_now_iso(),
            "test_name": suite_name,
            "nodeid": f"{suite_id}: {suite_name}",
            "phase": "call",
            "file": file_rel,
            "line": line_no,
            "location": location,
            "test_location": "",
            "app_culprit": location if file_rel.startswith("app/") else "",
            "code_snippet": snippet,
            "exception_type": exc_type,
            "error_message": exc_msg[:1500],
            "expected_vs_actual": exp_actual,
            "execution_context": {"fixtures": [], "params": {}, "recent_http_calls": []},
            "root_cause_analysis": analysis,
        }


def record_pytest_failure(record: dict[str, Any]) -> None:
    """Append diagnostic record to tests/logs/test_failures.log and test_failure_report.json."""
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    block = format_console_block(record)
    with _LOCK:
        _RECORDED_FAILURES.append(record)
        with PYTEST_LOG_FILE.open("a", encoding="utf-8") as fh:
            fh.write(block + "\n\n")
        payload = {
            "run_started_at": _RUN_STARTED_AT or _utc_now_iso(),
            "total_failures": len(_RECORDED_FAILURES),
            "failures": _RECORDED_FAILURES,
        }
        PYTEST_JSON_FILE.write_text(json.dumps(payload, indent=2), encoding="utf-8")
