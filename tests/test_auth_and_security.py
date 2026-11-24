from __future__ import annotations

from pathlib import Path

import pytest

from app import create_app, socketio
from app.config import Config
from app.services.face_counter import BoundingBox, FaceDetector
from app.services.qr_service import RollingQRService
from app.services.rate_limiter import SlidingWindowRateLimiter


class StubDetector(FaceDetector):
    def detect(self, image) -> list[BoundingBox]:
        return []


@pytest.fixture()
def auth_app(tmp_path: Path):
    db_file = tmp_path / "test_auth_sec.db"
    cfg = Config(
        SECRET_KEY="phase12-auth-secret-very-strong-key",
        DATABASE_URL=f"sqlite:///{db_file.as_posix()}",
    )
    app = create_app(cfg, face_detector=StubDetector())
    app.config["TESTING"] = True
    yield app
    app.extensions["task_manager"].shutdown(wait=True)
    app.extensions["db_engine"].dispose()


def test_professor_register_login_lockout_and_session_revocation(auth_app) -> None:
    """Phase 1: Verify professor registration, username/email sign-in, lockout, and logout-all token revocation."""
    c1 = auth_app.test_client()
    c2 = auth_app.test_client()

    # 1. Validation & conflict checks on register
    weak_pw = c1.post(
        "/api/auth/lecturer/register",
        json={
            "full_name": "Prof. Test",
            "username": "proftest",
            "email": "prof@ariel.ac.il",
            "password": "short",
        },
    )
    assert weak_pw.status_code == 400

    reserved = c1.post(
        "/api/auth/lecturer/register",
        json={
            "full_name": "Admin User",
            "username": "admin",
            "email": "admin2@ariel.ac.il",
            "password": "Password1!",
        },
    )
    assert reserved.status_code == 400

    # 2. Clean registration succeeds (201)
    ok_reg = c1.post(
        "/api/auth/lecturer/register",
        json={
            "full_name": "Prof. Avi Cohen",
            "username": "avi.cohen",
            "email": "avi@ariel.ac.il",
            "password": "StrongPass1!",
            "confirm_password": "StrongPass1!",
            "department": "Computer Science",
        },
    )
    assert ok_reg.status_code == 201
    assert ok_reg.get_json()["lecturer"]["username"] == "avi.cohen"

    # Duplicate email and duplicate username return 409
    assert (
        c2.post(
            "/api/auth/lecturer/register",
            json={
                "full_name": "Clone",
                "username": "other_user",
                "email": "avi@ariel.ac.il",
                "password": "StrongPass1!",
            },
        ).status_code
        == 409
    )
    assert (
        c2.post(
            "/api/auth/lecturer/register",
            json={
                "full_name": "Clone",
                "username": "avi.cohen",
                "email": "other@ariel.ac.il",
                "password": "StrongPass1!",
            },
        ).status_code
        == 409
    )

    # 3. Login via username AND via email both work
    assert (
        c2.post(
            "/api/auth/lecturer/login",
            json={"identifier": "avi.cohen", "password": "StrongPass1!"},
        ).status_code
        == 200
    )
    assert (
        c2.post(
            "/api/auth/lecturer/login",
            json={"identifier": "avi@ariel.ac.il", "password": "StrongPass1!"},
        ).status_code
        == 200
    )

    # 4. Logout-all on c2 revokes c1's active session immediately via token_version
    assert c1.get("/api/auth/lecturer/me").status_code == 200
    assert c2.post("/api/auth/lecturer/logout-all").status_code == 200
    assert c1.get("/api/auth/lecturer/me").status_code == 401
    assert c1.get("/api/courses").status_code == 401

    # 5. Brute-force wrong passwords trigger 429 lockout after 5 attempts
    attacker = auth_app.test_client()
    for _ in range(5):
        bad = attacker.post(
            "/api/auth/lecturer/login",
            json={"identifier": "avi.cohen", "password": "WrongPassword99!"},
        )
        assert bad.status_code == 401
    locked = attacker.post(
        "/api/auth/lecturer/login",
        json={"identifier": "avi.cohen", "password": "StrongPass1!"},
    )
    assert locked.status_code == 429


def test_professor_password_reset_and_change_password(auth_app) -> None:
    """Phase 1: Verify single-use forgot/reset password flow and change-password rotation."""
    c = auth_app.test_client()
    assert (
        c.post(
            "/api/auth/lecturer/register",
            json={
                "full_name": "Dr. Maya Ben",
                "username": "maya.b",
                "email": "maya@ariel.ac.il",
                "password": "InitialPass1!",
            },
        ).status_code
        == 201
    )

    forgot = c.post(
        "/api/auth/lecturer/forgot-password", json={"identifier": "maya.b"}
    )
    assert forgot.status_code == 200
    reset_token = forgot.get_json()["reset_token"]

    reset_resp = c.post(
        "/api/auth/lecturer/reset-password",
        json={"token": reset_token, "new_password": "UpdatedPass2!"},
    )
    assert reset_resp.status_code == 200

    # Reuse of same token rejected
    assert (
        c.post(
            "/api/auth/lecturer/reset-password",
            json={"token": reset_token, "new_password": "AnotherPass3!"},
        ).status_code
        == 400
    )

    # Old password fails; new password succeeds
    assert (
        c.post(
            "/api/auth/lecturer/login",
            json={"identifier": "maya@ariel.ac.il", "password": "InitialPass1!"},
        ).status_code
        == 401
    )
    assert (
        c.post(
            "/api/auth/lecturer/login",
            json={"identifier": "maya.b", "password": "UpdatedPass2!"},
        ).status_code
        == 200
    )


def test_student_id_email_consistency_and_device_reset(auth_app) -> None:
    """Phase 1/2: Verify Student ID + Email QR check-in consistency, 3-col CSV roster, and device reset."""
    prof = auth_app.test_client()
    phone1 = auth_app.test_client()
    phone2 = auth_app.test_client()
    qr: RollingQRService = auth_app.extensions["qr_service"]

    assert (
        prof.post(
            "/api/auth/lecturer/register",
            json={
                "full_name": "Prof. Boaz",
                "username": "boaz.idtest",
                "email": "boaz_id@ariel.ac.il",
                "password": "Password1!",
            },
        ).status_code
        == 201
    )
    course = prof.post(
        "/api/courses",
        json={"course_code": "SID101", "name": "Identity Design"},
    ).get_json()
    cid = course["id"]
    scode = course["sessions"][0]["session_code"]
    sid = course["sessions"][0]["id"]

    # Import 3-column CSV roster (Student ID, Full Name, Email)
    imp = prof.post(
        f"/api/courses/{cid}/roster",
        json={"csv_text": "208123456, Ohad Ben Nun, ohad@ariel.ac.il\n"},
    )
    assert imp.status_code == 200
    assert imp.get_json()["imported"] == 1

    # Matching Student ID + Email succeeds and returns student_id_number
    b = qr.compute_bucket()
    sig = qr.sign_token(scode, b)
    ck = phone1.post(
        "/api/attendance/checkin",
        json={
            "session_code": scode,
            "t": b,
            "sig": sig,
            "full_name": "Ohad Ben Nun",
            "student_id_number": "208123456",
            "email": "ohad@ariel.ac.il",
            "device_id": "ohad-old-phone",
        },
    )
    assert ck.status_code == 200
    assert ck.get_json()["student_id_number"] == "208123456"
    st_db_id = ck.get_json()["student_id"]

    # Spoofing wrong Student ID for Ohad's email returns 409 Conflict
    mismatch = phone2.post(
        "/api/attendance/checkin",
        json={
            "session_code": scode,
            "t": b,
            "sig": sig,
            "full_name": "Ohad Impersonator",
            "student_id_number": "999888777",
            "email": "ohad@ariel.ac.il",
            "device_id": "attacker-phone",
        },
    )
    assert mismatch.status_code == 409

    # Lecturer resets Ohad's bound device -> primary_device_id cleared
    reset_resp = prof.post(f"/api/courses/{cid}/students/{st_db_id}/reset-device")
    assert reset_resp.status_code == 200
    overview = prof.get(f"/api/courses/{cid}/overview").get_json()
    assert overview["students"][0]["student_id_number"] == "208123456"
    assert overview["students"][0]["device_bound"] is False


def test_socketio_and_projector_cross_tenant_protection(auth_app) -> None:
    """Phase 2: Verify Socket.IO rejects cross-lecturer room joins and projector enforces token/owner."""
    boaz = auth_app.test_client()
    daniel = auth_app.test_client()

    boaz.post(
        "/api/auth/lecturer/register",
        json={
            "full_name": "Prof. Boaz",
            "username": "boaz.ws",
            "email": "boaz_ws@ariel.ac.il",
            "password": "Password1!",
        },
    )
    daniel.post(
        "/api/auth/lecturer/register",
        json={
            "full_name": "Dr. Daniel",
            "username": "daniel.ws",
            "email": "daniel_ws@ariel.ac.il",
            "password": "Password1!",
        },
    )

    c_data = boaz.post(
        "/api/courses", json={"course_code": "WS101", "name": "Sockets Course"}
    ).get_json()
    scode = c_data["sessions"][0]["session_code"]

    # Daniel cannot open Boaz's projector without valid token
    assert daniel.get(f"/projector/{scode}").status_code == 403
    # Valid projector token works
    ptok = auth_app.extensions["device_mgr"].create_projector_token(scode)
    assert auth_app.test_client().get(f"/projector/{scode}?ptok={ptok}").status_code == 200

    # Daniel's Socket.IO client cannot join Boaz's classroom room
    dan_sio = socketio.test_client(auth_app, flask_test_client=daniel)
    dan_sio.emit("join_session", {"session_code": scode})
    ev_names = [e["name"] for e in dan_sio.get_received()]
    assert "session:error" in ev_names
    assert "qr:rotate" not in ev_names
    dan_sio.disconnect()

    # Boaz's Socket.IO client joins cleanly
    boaz_sio = socketio.test_client(auth_app, flask_test_client=boaz)
    boaz_sio.emit("join_session", {"session_code": scode})
    boaz_evs = [e["name"] for e in boaz_sio.get_received()]
    assert "qr:rotate" in boaz_evs
    assert "poll:sync" in boaz_evs
    boaz_sio.disconnect()

    # Rate limiter unit check
    limiter = SlidingWindowRateLimiter()
    assert limiter.allow("k", limit=2, window_seconds=60.0, now_ts=10.0)[0] is True
    assert limiter.allow("k", limit=2, window_seconds=60.0, now_ts=11.0)[0] is True
    allowed, retry = limiter.allow("k", limit=2, window_seconds=60.0, now_ts=12.0)
    assert allowed is False
    assert retry >= 1


def test_course_staff_join_code_csrf_upload_magic_and_retention(auth_app, tmp_path: Path) -> None:
    """Phases 2-5: CourseStaff (TA), join_code, CSRF protection, magic-byte validation, delete, retention, probes, OpenAPI."""
    owner = auth_app.test_client()
    ta = auth_app.test_client()
    student = auth_app.test_client()

    # Register owner & TA
    assert (
        owner.post(
            "/api/auth/lecturer/register",
            json={
                "full_name": "Prof. Owner",
                "username": "owner.prof",
                "email": "owner@ariel.ac.il",
                "password": "Password1!",
            },
        ).status_code
        == 201
    )
    assert (
        ta.post(
            "/api/auth/lecturer/register",
            json={
                "full_name": "TA Helper",
                "username": "ta.helper",
                "email": "ta@ariel.ac.il",
                "password": "Password1!",
            },
        ).status_code
        == 201
    )

    c_data = owner.post(
        "/api/courses", json={"course_code": "STAFF101", "name": "Staff & Docs"}
    ).get_json()
    cid = c_data["id"]
    sid = c_data["sessions"][0]["id"]
    join_code = c_data["join_code"]
    assert join_code.startswith("STAFF1-")

    # TA initially denied (403)
    assert ta.get(f"/api/courses/{cid}/overview").status_code == 403

    # Owner adds TA -> TA now sees course & overview, but TA cannot add another staff (403)
    add_res = owner.post(
        f"/api/courses/{cid}/staff",
        json={"identifier": "ta.helper", "role": "ta"},
    )
    assert add_res.status_code == 201
    assert ta.get(f"/api/courses/{cid}/overview").status_code == 200
    assert "STAFF101" in [c["course_code"] for c in ta.get("/api/courses").get_json()["courses"]]
    assert (
        ta.post(
            f"/api/courses/{cid}/staff",
            json={"identifier": "owner.prof", "role": "ta"},
        ).status_code
        == 403
    )

    # Owner removes TA -> TA immediately blocked again (403)
    ta_id = add_res.get_json()["member"]["lecturer_id"]
    assert owner.delete(f"/api/courses/{cid}/staff/{ta_id}").status_code == 200
    assert ta.get(f"/api/courses/{cid}/overview").status_code == 403

    # Student enrolls using unique join_code
    assert (
        student.post(
            "/api/student/login",
            json={
                "full_name": "Student Joiner",
                "student_id_number": "312000111",
                "email": "joiner@ariel.ac.il",
                "course_code": join_code,
            },
        ).status_code
        == 200
    )

    # Reject non-image spoofed headcount photo (magic byte validation)
    import io

    bad_img = owner.post(
        f"/api/sessions/{sid}/headcount",
        data={"photo": (io.BytesIO(b"not-a-real-image-payload"), "evil.jpg")},
        content_type="multipart/form-data",
    )
    assert bad_img.status_code == 400

    # Delete lecture artifact endpoint cleans up artifact
    art = owner.post(
        f"/api/sessions/{sid}/lectures/summarize", json={"use_sample": True}
    ).get_json()
    aid = art["id"]
    assert ta.delete(f"/api/lectures/{aid}").status_code == 403
    assert owner.delete(f"/api/lectures/{aid}").status_code == 200

    # CSRF cookie enforcement when cp_csrf is active on client
    auth_app.config["TEST_ENFORCE_CSRF"] = True
    try:
        browser = auth_app.test_client()
        assert browser.get("/healthz").status_code == 200  # issues cp_csrf cookie
        csrf_val = browser.get_cookie("cp_csrf").value
        no_hdr = browser.post(
            "/api/auth/lecturer/login",
            json={"identifier": "owner.prof", "password": "Password1!"},
        )
        assert no_hdr.status_code == 403
        with_hdr = browser.post(
            "/api/auth/lecturer/login",
            headers={"X-CSRF-Token": csrf_val},
            json={"identifier": "owner.prof", "password": "Password1!"},
        )
        assert with_hdr.status_code == 200
    finally:
        auth_app.config["TEST_ENFORCE_CSRF"] = False

    # Audit logs, readiness probes, OpenAPI spec & production secret check
    assert owner.get("/api/auth/lecturer/audit-logs").get_json()["logs"]
    assert owner.get("/readyz").status_code == 200
    spec = owner.get("/api/openapi.json").get_json()
    assert spec["openapi"] == "3.0.3"
    assert owner.get("/api/docs").status_code == 200

    with pytest.raises(RuntimeError):
        Config(CLASSPULSE_ENV="production", SECRET_KEY="short").validate_production_safety()


def test_legacy_lecturer_activation_and_student_login_device_isolation(auth_app) -> None:
    """Phase 1: Legacy NULL-password lecturer activation preserves courses; student login/logout isolates device IDs."""
    from app.models import Course, Lecturer

    with auth_app.app_context():
        db = auth_app.extensions["db_session"]
        legacy = Lecturer(
            full_name="Legacy Lecturer",
            email="legacy@ariel.ac.il",
            username=None,
            password_hash=None,
            department="Math",
        )
        db.add(legacy)
        db.flush()
        db.add(
            Course(
                lecturer_id=legacy.id,
                course_code="LEG101",
                name="Legacy Course",
                lecturer_name="Legacy Lecturer",
            )
        )
        db.commit()

    client = auth_app.test_client()
    # Repeated login attempts on unclaimed legacy account do NOT trigger 429 lockout
    for _ in range(6):
        r_login = client.post(
            "/api/auth/lecturer/login",
            json={"identifier": "legacy@ariel.ac.il", "password": "SomePassword1!"},
        )
        assert r_login.status_code == 401
        assert "Create Account" in r_login.get_json()["error"]

    # Registering with that email activates the legacy account in-place and preserves LEG101
    act = client.post(
        "/api/auth/lecturer/register",
        json={
            "full_name": "Legacy Lecturer",
            "username": "legacy.prof",
            "email": "legacy@ariel.ac.il",
            "password": "NewStrongPass1!",
            "confirm_password": "NewStrongPass1!",
        },
    )
    assert act.status_code == 201
    assert [c["course_code"] for c in act.get_json()["courses"]] == ["LEG101"]

    # Student login, logout, and second student on same client gets isolated device_id (no 409 proxy block)
    qr: RollingQRService = auth_app.extensions["qr_service"]
    sess_resp = client.post(
        f"/api/courses/{act.get_json()['courses'][0]['id']}/sessions",
        json={"title": "Lecture 1"},
    )
    assert sess_resp.status_code == 201
    scode = sess_resp.get_json()["session_code"]

    st_client = auth_app.test_client()
    s1 = st_client.post(
        "/api/student/login",
        json={"full_name": "Student One", "student_id_number": "11112222", "email": "s1@ariel.ac.il"},
    )
    assert s1.status_code == 200
    # Returning login with course_code and blank full_name works
    s1_ret = st_client.post(
        "/api/student/login",
        json={"email": "S1@ariel.ac.il", "course_code": "LEG101"},
    )
    assert s1_ret.status_code == 200
    assert s1_ret.get_json()["student"]["full_name"] == "Student One"

    b = qr.compute_bucket()
    sig = qr.sign_token(scode, b)
    ck1 = st_client.get(f"/s/{scode}?t={b}&sig={sig}")
    assert ck1.status_code == 200

    # Sign out student 1 and sign in student 2 on same browser client -> fresh device_id avoids 409
    assert st_client.post("/api/student/logout").status_code == 200
    s2 = st_client.post(
        "/api/student/login",
        json={"full_name": "Student Two", "student_id_number": "33334444", "email": "s2@ariel.ac.il"},
    )
    assert s2.status_code == 200
    ck2 = st_client.get(f"/s/{scode}?t={b}&sig={sig}")
    assert ck2.status_code == 200
    assert b"Student Two" in ck2.data

