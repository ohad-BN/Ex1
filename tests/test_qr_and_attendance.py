from __future__ import annotations

from pathlib import Path

import pytest

from app import create_app
from app.config import Config
from app.services.qr_service import DeviceBindingManager, RollingQRService


@pytest.fixture()
def app_ctx(tmp_path: Path):
    db_file = tmp_path / "test_phase3.db"
    cfg = Config(
        SECRET_KEY="phase3-test-secret",
        DATABASE_URL=f"sqlite:///{db_file.as_posix()}",
        QR_STEP_SECONDS=5,
        QR_GRACE_BUCKETS=1,
    )
    app = create_app(cfg)
    app.config["TESTING"] = True
    yield app
    app.extensions["task_manager"].shutdown(wait=True)
    app.extensions["db_engine"].dispose()


def test_rolling_qr_bucket_and_grace_window() -> None:
    """Suite 1: Verify 5s bucket math, N-1 grace window, and expiry/tamper rejection."""
    qr = RollingQRService(secret_key="sec-123", step_seconds=5, grace_buckets=1)
    # Scan at t=1004.9s (bucket 200)
    b200 = qr.compute_bucket(1004.9)
    assert b200 == 200
    sig = qr.sign_token("CS101", b200)

    # Verify in same bucket (t=1004.95s -> bucket 200)
    assert qr.verify_token("CS101", sig, b200, now_ts=1004.95) is True

    # Verify in grace bucket N-1 (t=1006.1s -> current bucket 201, token from 200)
    assert qr.verify_token("CS101", sig, b200, now_ts=1006.1) is True

    # Expired in bucket N-2 (t=1010.5s -> current bucket 202, token from 200)
    assert qr.verify_token("CS101", sig, b200, now_ts=1010.5) is False

    # Tampered signature or wrong session code rejected
    assert qr.verify_token("CS101", sig[:-1] + "0", b200, now_ts=1004.95) is False
    assert qr.verify_token("OTHER", sig, b200, now_ts=1004.95) is False


def test_device_cookie_signing_and_tamper_protection() -> None:
    """Suite 1: Verify DeviceBindingManager signs and rejects tampered cookies."""
    mgr = DeviceBindingManager(secret_key="cookie-secret")
    signed = mgr.create_device_cookie("dev-abc-123", 77)
    parsed = mgr.parse_device_cookie(signed)
    assert parsed == ("dev-abc-123", 77)

    # Tamper with signed payload
    assert mgr.parse_device_cookie(signed + "tamper") is None
    assert mgr.parse_device_cookie("") is None
    assert mgr.parse_device_cookie(None) is None


def test_first_time_onboarding_and_returning_student_auto_checkin(app_ctx) -> None:
    """Suite 2: End-to-end first-time onboarding followed by 0-typing returning scan."""
    client = app_ctx.test_client()
    qr: RollingQRService = app_ctx.extensions["qr_service"]

    # Create Session 1 & Session 2
    r1 = client.post(
        "/api/sessions",
        json={"title": "Lecture 1", "session_code": "LEC01"},
    )
    assert r1.status_code == 201
    r2 = client.post(
        "/api/sessions",
        json={"title": "Lecture 2", "session_code": "LEC02"},
    )
    assert r2.status_code == 201

    # 1. Student scans LEC01 QR for the first time
    b = qr.compute_bucket()
    sig = qr.sign_token("LEC01", b)
    scan_resp = client.get(f"/s/LEC01?t={b}&sig={sig}")
    assert scan_resp.status_code == 200
    assert b"First-Time Student Sign-In" in scan_resp.data

    # 2. Student submits Name + Email via JSON API
    checkin_resp = client.post(
        "/api/attendance/checkin",
        json={
            "session_code": "LEC01",
            "t": b,
            "sig": sig,
            "full_name": "Ohad Ben Nun",
            "email": "ohad@ariel.ac.il",
            "device_id": "phone-ohad-1",
        },
    )
    assert checkin_resp.status_code == 200
    data = checkin_resp.get_json()
    assert data["status"] == "checked_in"
    assert data["is_new_checkin"] is True
    assert data["qr_count"] == 1

    # 3. In Lecture 2 (LEC02), the same browser (holding cp_device cookie) scans QR
    b2 = qr.compute_bucket()
    sig2 = qr.sign_token("LEC02", b2)
    return_scan = client.get(f"/s/LEC02?t={b2}&sig={sig2}")
    assert return_scan.status_code == 200
    # Returning student is automatically checked in without showing the registration form!
    assert b"Attendance Recorded" in return_scan.data
    assert b"Ohad Ben Nun" in return_scan.data


def test_proxy_attendance_same_device_blocked_409(app_ctx) -> None:
    """Suite 2: Blocking a student from checking in a friend on the same phone (HTTP 409)."""
    client = app_ctx.test_client()
    qr: RollingQRService = app_ctx.extensions["qr_service"]

    client.post("/api/sessions", json={"title": "Anti-Proxy Test", "session_code": "PROXY1"})
    b = qr.compute_bucket()
    sig = qr.sign_token("PROXY1", b)

    # Student 1 checks in on device "shared-phone-99"
    res1 = client.post(
        "/api/attendance/checkin",
        json={
            "session_code": "PROXY1",
            "t": b,
            "sig": sig,
            "full_name": "Student One",
            "email": "one@ariel.ac.il",
            "device_id": "shared-phone-99",
        },
    )
    assert res1.status_code == 200

    # Student 2 tries checking in using the same device "shared-phone-99"
    res2 = client.post(
        "/api/attendance/checkin",
        json={
            "session_code": "PROXY1",
            "t": b,
            "sig": sig,
            "full_name": "Student Two",
            "email": "two@ariel.ac.il",
            "device_id": "shared-phone-99",
        },
    )
    assert res2.status_code == 409
    assert "Proxy attendance blocked" in res2.get_json()["error"]


def test_roster_csv_import_and_merged_attendance_rows(app_ctx) -> None:
    """Suite 2: Import course roster CSV and verify Present vs Absent status merging."""
    client = app_ctx.test_client()
    qr: RollingQRService = app_ctx.extensions["qr_service"]

    s_resp = client.post(
        "/api/sessions", json={"title": "Roster Session", "session_code": "ROST1"}
    ).get_json()
    sid = s_resp["id"]

    csv_text = "Full Name,Email\nAlice Cohen,alice@ariel.ac.il\nBob Levi,bob@ariel.ac.il\n"
    imp_resp = client.post(
        f"/api/sessions/{sid}/roster", json={"csv_text": csv_text}
    )
    assert imp_resp.status_code == 200
    assert imp_resp.get_json()["imported"] == 2

    # Only Alice checks in via QR
    b = qr.compute_bucket()
    sig = qr.sign_token("ROST1", b)
    client.post(
        "/api/attendance/checkin",
        json={
            "session_code": "ROST1",
            "t": b,
            "sig": sig,
            "full_name": "Alice Cohen",
            "email": "alice@ariel.ac.il",
            "device_id": "dev-alice",
        },
    )

    status_data = client.get(f"/api/sessions/{sid}/status").get_json()
    rows_by_email = {r["email"]: r for r in status_data["rows"]}
    assert rows_by_email["alice@ariel.ac.il"]["attendance_status"] == "Present"
    assert rows_by_email["bob@ariel.ac.il"]["attendance_status"] == "Absent"


def test_qr_form_csrf_inline_error_recovery_303_redirect_and_expired_reentry(app_ctx) -> None:
    """Phase 2: Verify CSRF token hydration, inline form retry on bad email, 303 PRG redirect, and expired-QR re-entry."""
    import re

    app_ctx.config["TEST_ENFORCE_CSRF"] = True
    try:
        client = app_ctx.test_client()
        qr: RollingQRService = app_ctx.extensions["qr_service"]

        client.post("/api/sessions", json={"title": "Lecture 1 — Routing", "session_code": "ROUTE1"})
        b = qr.compute_bucket()
        sig = qr.sign_token("ROUTE1", b)

        scan = client.get(f"/s/ROUTE1?t={b}&sig={sig}")
        assert scan.status_code == 200
        html = scan.data.decode("utf-8")
        csrf_cookie = client.get_cookie("cp_csrf")
        assert csrf_cookie is not None
        assert f'name="csrf_token" value="{csrf_cookie.value}"' in html

        grant_m = re.search(r'name="onboarding_grant" value="([^"]+)"', html)
        dev_m = re.search(r'name="device_id" value="([^"]+)"', html)
        assert grant_m and dev_m

        # Submitting invalid email keeps the onboarding form intact with preserved values
        bad_form = client.post(
            "/api/attendance/checkin",
            data={
                "csrf_token": csrf_cookie.value,
                "session_code": "ROUTE1",
                "onboarding_grant": grant_m.group(1),
                "t": str(b),
                "sig": sig,
                "device_id": dev_m.group(1),
                "full_name": "Dana Levi",
                "student_id_number": "209123456",
                "email": "not-an-email",
            },
        )
        assert bad_form.status_code == 400
        bad_html = bad_form.data.decode("utf-8")
        assert "First-Time Student Sign-In" in bad_html
        assert 'value="Dana Levi"' in bad_html
        assert 'value="209123456"' in bad_html

        # Valid submit returns 303 redirect to /student?session=ROUTE1&checked_in=1
        good_form = client.post(
            "/api/attendance/checkin",
            data={
                "csrf_token": csrf_cookie.value,
                "session_code": "ROUTE1",
                "onboarding_grant": grant_m.group(1),
                "t": str(b),
                "sig": sig,
                "device_id": dev_m.group(1),
                "full_name": "Dana Levi",
                "student_id_number": "209123456",
                "email": "dana.levi@ariel.ac.il",
            },
            follow_redirects=False,
        )
        assert good_form.status_code == 303
        loc = good_form.headers["Location"]
        assert "/student" in loc and "session=ROUTE1" in loc

        landed = client.get(loc)
        assert landed.status_code == 200
        assert b"Attendance Recorded" in landed.data
        assert b"Dana Levi" in landed.data

        # Already checked-in student scanning an old/expired QR token still enters lecture cleanly
        reenter = client.get("/s/ROUTE1?t=1&sig=expired")
        assert reenter.status_code == 200
        assert b"Attendance Recorded" in reenter.data
    finally:
        app_ctx.config["TEST_ENFORCE_CSRF"] = False
