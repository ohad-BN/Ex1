from __future__ import annotations

from pathlib import Path
from typing import Any, Callable, Optional

import pytest
from flask import Flask

from app import create_app
from app.config import Config
from app.services.face_counter import (
    BoundingBox,
    FaceDetector,
    read_image_unicode,
    write_image_unicode,
)
from tests import diagnostics


def pytest_sessionstart(session: pytest.Session) -> None:
    """Reset diagnostic logs and enable lightweight HTTP request tracing for this test run."""
    diagnostics.reset_pytest_logs()
    diagnostics.install_flask_client_tracer()


def pytest_sessionfinish(session: pytest.Session, exitstatus: int) -> None:
    """Restore original FlaskClient.open after the test session finishes."""
    diagnostics.uninstall_flask_client_tracer()


@pytest.fixture(autouse=True)
def _diagnostic_http_trace_guard():
    """Ensure every test starts with a clean HTTP request/response trace buffer."""
    diagnostics.clear_http_trace()
    yield
    diagnostics.clear_http_trace()


@pytest.hookimpl(hookwrapper=True)
def pytest_runtest_makereport(item: pytest.Item, call: pytest.CallInfo[Any]):
    """Capture structured failure diagnostics on any setup/call/teardown failure."""
    outcome = yield
    report = outcome.get_result()
    if report.failed:
        record = diagnostics.build_pytest_failure_record(item, call, report)
        diagnostics.record_pytest_failure(record)


def pytest_terminal_summary(terminalreporter: Any, exitstatus: int, config: pytest.Config) -> None:
    """Print crisp numbered failure diagnostic cards directly in terminal output."""
    failures = diagnostics.get_recorded_failures()
    if not failures:
        return
    passed = len(terminalreporter.stats.get("passed", []))
    failed = len(failures)
    terminalreporter.write_line("")
    terminalreporter.write_sep("=", f"TEST FAILURES ({passed + failed} run | {passed} passed | {failed} failed)", red=True, bold=True)
    for idx, rec in enumerate(failures, start=1):
        for line in diagnostics.format_console_block(rec, index=idx).splitlines():
            terminalreporter.write_line(line)
    terminalreporter.write_line(f"Saved report: {diagnostics.PYTEST_JSON_FILE}")


class StubDetector(FaceDetector):
    """Deterministic no-op face detector stub for fast isolated tests."""

    def detect(self, image) -> list[BoundingBox]:
        return []

    @staticmethod
    def render_annotated_image(
        image_path: str | Path,
        boxes: list[BoundingBox],
        out_path: str | Path,
    ) -> str:
        img = read_image_unicode(image_path)
        return write_image_unicode(out_path, img)


_UNSET = object()


def make_isolated_app(
    tmp_path: Path,
    *,
    db_name: str = "test.db",
    secret_key: str = "pytest-isolated-secret-32chars!!",
    face_detector: Any = _UNSET,
    llm_client: Any = None,
    **cfg_overrides: Any,
) -> Flask:
    """Create a Flask test app whose DB, UPLOAD_DIR, and REPORT_DIR stay inside tmp_path."""
    db_file = tmp_path / db_name
    defaults: dict[str, Any] = {
        "SECRET_KEY": secret_key,
        "DATABASE_URL": f"sqlite:///{db_file.as_posix()}",
        "UPLOAD_DIR": tmp_path / "uploads",
        "REPORT_DIR": tmp_path / "reports",
        "GEMINI_API_KEY": "",
    }
    defaults.update(cfg_overrides)
    cfg = Config(**defaults)
    detector = StubDetector() if face_detector is _UNSET else face_detector
    app = create_app(cfg, face_detector=detector, llm_client=llm_client)
    app.config["TESTING"] = True
    return app


@pytest.fixture()
def isolated_app_factory(tmp_path: Path):
    """Yield a factory building isolated Flask apps and shut down every created app cleanly."""
    created: list[Flask] = []

    def _build(**kwargs: Any) -> Flask:
        idx = len(created)
        sub_dir = tmp_path / f"app_{idx}"
        sub_dir.mkdir(parents=True, exist_ok=True)
        app = make_isolated_app(sub_dir, **kwargs)
        created.append(app)
        return app

    yield _build
    for app in created:
        app.extensions["task_manager"].shutdown(wait=True)
        app.extensions["db_engine"].dispose()


@pytest.fixture()
def app(isolated_app_factory: Callable[..., Flask]) -> Flask:
    """Default isolated Flask app using StubDetector and tmp_path storage."""
    return isolated_app_factory()


def register_lecturer(
    client,
    full_name: str,
    email: str,
    password: str = "Password1!",
    *,
    username: Optional[str] = None,
    department: str = "Computer Science",
) -> dict[str, Any]:
    """Register a lecturer via REST and assert HTTP 201."""
    payload: dict[str, Any] = {
        "full_name": full_name,
        "email": email,
        "password": password,
        "department": department,
    }
    if username is not None:
        payload["username"] = username
    resp = client.post("/api/auth/lecturer/register", json=payload)
    assert resp.status_code == 201, resp.get_data(as_text=True)
    return resp.get_json()["lecturer"]


def login_student(
    client,
    full_name: str,
    student_id_number: str,
    email: Optional[str] = None,
) -> dict[str, Any]:
    """Sign in a student via /api/student/login and assert HTTP 200."""
    payload: dict[str, Any] = {
        "full_name": full_name,
        "student_id_number": student_id_number,
    }
    if email is not None:
        payload["email"] = email
    resp = client.post("/api/student/login", json=payload)
    assert resp.status_code == 200, resp.get_data(as_text=True)
    return resp.get_json()["student"]
