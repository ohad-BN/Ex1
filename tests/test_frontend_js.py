from __future__ import annotations

import json
from pathlib import Path
import shutil
import subprocess

import pytest


def test_frontend_static_js_unit_suite() -> None:
    """Execute zero-dependency Node.js unit tests covering app/static/js/*.js helpers."""
    node_bin = shutil.which("node")
    if not node_bin:
        pytest.skip("Node.js binary not installed on host")

    test_script = Path(__file__).resolve().with_name("test_frontend_js.mjs")
    proc = subprocess.run(
        [node_bin, "--test", str(test_script)],
        cwd=str(test_script.parent.parent),
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=30,
    )
    js_report = Path(__file__).resolve().parent / "logs" / "frontend_failure_report.json"
    diag_summary = ""
    if proc.returncode != 0 and js_report.is_file():
        try:
            data = json.loads(js_report.read_text(encoding="utf-8"))
            if data.get("failures"):
                first = data["failures"][0]
                diag_summary = (
                    f"\nJS Diagnostic: {first.get('location')} -> "
                    f"{first.get('root_cause_analysis', {}).get('category')}: "
                    f"{first.get('error_message')}"
                )
        except Exception:
            diag_summary = ""
    assert proc.returncode == 0, (
        f"node --test failed (rc={proc.returncode}):{diag_summary}\nSTDOUT:\n{proc.stdout}\nSTDERR:\n{proc.stderr}"
    )
