from __future__ import annotations

from flask import Flask

from tests import diagnostics
from tests.diagnostics import FaultLocalizationEngine


def test_fault_localizer_pinpoints_app_source_line_from_traceback() -> None:
    """Verify FaultLocalizationEngine prioritizes the innermost app/ crash line & renders compact card."""
    simulated_tb = (
        "tests/test_gaps_homework_ranking_security.py:202: in test_bad_zip\n"
        "    resp = stu.post('/api/assignments/1/submissions')\n"
        "app/services/homework_pipeline.py:241: in extract_from_zip_bytes\n"
        "    with zipfile.ZipFile(io.BytesIO(raw_bytes)) as zf:\n"
        "E   zipfile.BadZipFile: File is not a zip file\n"
    )
    record = FaultLocalizationEngine.analyze_raw_output(
        "Suite 6",
        "Assignments & Isolation",
        "tests/test_gaps_homework_ranking_security.py",
        simulated_tb,
    )
    assert record["file"] == "app/services/homework_pipeline.py"
    assert record["line"] == 241
    assert any(r["line"] == 241 and r["is_target"] for r in record["code_snippet"])

    card = diagnostics.format_console_block(record, index=1)
    assert "FAIL #1:" in card
    assert "Where : app/services/homework_pipeline.py:241" in card
    assert "Why   :" in card
    assert "Code  :" in card
    assert ">  241 |" in card
    assert "Hint  :" in card


def test_live_injected_assertion_bug_captured_with_http_context(app: Flask) -> None:
    """Execute a real Flask request, trigger a controlled assertion failure, and verify compact card output."""
    client = app.test_client()
    resp = client.get("/healthz")

    captured: dict[str, object] = {}
    try:
        assert resp.status_code == 500, f"{resp.status_code} == 500"
    except AssertionError as exc:
        captured = diagnostics.build_record_from_exception(
            test_name="test_live_injected_assertion_bug_captured_with_http_context",
            exc=exc,
        )

    assert captured["file"] == "tests/test_diagnostics.py"
    assert captured["expected_vs_actual"]["actual"] == "200"
    assert captured["expected_vs_actual"]["expected"] == "500"
    assert captured["execution_context"]["recent_http_calls"][-1]["path"] == "/healthz"
    assert captured["root_cause_analysis"]["category"] == "Assertion Mismatch"

    card = diagnostics.format_console_block(captured, index=2)
    assert "FAIL #2:" in card
    assert "Diff  : Expected 500 | Got 200" in card
    assert "HTTP  : GET /healthz -> 200" in card


def test_live_injected_missing_key_and_isolation_classification() -> None:
    """Verify KeyError and isolation/HTTP-500 root-cause classification without modifying repo files."""
    captured_key: dict[str, object] = {}
    try:
        sample = {"submission_type": "comparative"}
        _ = sample["class_standing"]
    except KeyError as exc:
        captured_key = diagnostics.build_record_from_exception(
            test_name="test_missing_key_probe",
            exc=exc,
        )

    assert captured_key["exception_type"] == "KeyError"
    assert captured_key["root_cause_analysis"]["category"] == "Missing Key / Schema Error"

    rca_500 = diagnostics.classify_root_cause(
        exc_type="AssertionError",
        exc_msg="assert 500 == 201",
        test_name="test_submit",
        app_culprit="app/routes/homework.py:379 in submit()",
        location="tests/test_hw.py:10",
        http_trace=[{"method": "POST", "path": "/api/assignments/1/submissions", "query_string": "", "status_code": 500, "response_preview": "Internal Server Error"}],
        exp_actual={"expected": "201", "actual": "500", "raw_assertion": "assert 500 == 201"},
    )
    assert rca_500["category"] == "HTTP 500 Internal Server Error"
    assert "app/routes/homework.py:379" in rca_500["likely_culprit"]

    rca_iso = diagnostics.classify_root_cause(
        exc_type="AssertionError",
        exc_msg="assert 200 == 403",
        test_name="test_lecturer_comparative_isolation",
        app_culprit="",
        location="tests/test_hw.py:55",
        http_trace=[{"method": "GET", "path": "/api/submissions/1/download", "query_string": "", "status_code": 403, "response_preview": ""}],
        exp_actual={"expected": "403", "actual": "200", "raw_assertion": "assert 200 == 403"},
    )
    assert rca_iso["category"] == "Data Isolation / RBAC Breach"
