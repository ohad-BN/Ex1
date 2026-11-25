from __future__ import annotations

import concurrent.futures
import time
from pathlib import Path

import pytest
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError

from app import create_app, socketio
from app.config import Config
from app.models import (
    Assignment,
    AttendanceRecord,
    ChatMessage,
    ClassSession,
    ClassStatsAggregate,
    HeadcountSnapshot,
    HomeworkSubmission,
    LectureArtifact,
    Poll,
    PollVote,
    RosterEntry,
    Student,
)


@pytest.fixture()
def test_app(tmp_path: Path):
    db_file = tmp_path / "test_classpulse.db"
    cfg = Config(
        SECRET_KEY="pytest-secret",
        DATABASE_URL=f"sqlite:///{db_file.as_posix()}",
        UPLOAD_DIR=tmp_path / "uploads",
        REPORT_DIR=tmp_path / "reports",
        MAX_TASK_WORKERS=2,
    )
    app = create_app(cfg)
    app.config["TESTING"] = True
    yield app
    app.extensions["task_manager"].shutdown(wait=True)
    app.extensions["db_engine"].dispose()


def test_health_and_cloudflare_proxy_headers(test_app) -> None:
    """Verify Phase 1 Flask factory and ProxyFix HTTPS tunnel header resolution."""
    client = test_app.test_client()
    response = client.get(
        "/health",
        headers={
            "X-Forwarded-Proto": "https",
            "X-Forwarded-Host": "ariel-demo.trycloudflare.com",
        },
    )
    assert response.status_code == 200
    payload = response.get_json()
    assert payload["status"] == "ok"
    assert payload["scheme"] == "https"
    assert payload["host"] == "ariel-demo.trycloudflare.com"


def test_socketio_connection(test_app) -> None:
    """Verify Phase 1 Flask-SocketIO threading initialization."""
    sio_client = socketio.test_client(test_app)
    assert sio_client.is_connected() is True
    sio_client.disconnect()


def test_async_task_manager_success_and_failure_isolation(test_app) -> None:
    """Verify Phase 1 AsyncTaskManager executes background jobs and isolates exceptions."""
    task_mgr = test_app.extensions["task_manager"]
    client = test_app.test_client()

    job_ok = task_mgr.submit("add_numbers", lambda a, b: a + b, 19, 23)
    job_fail = task_mgr.submit("boom", lambda: 1 / 0)

    for _ in range(40):
        s1 = task_mgr.get_status(job_ok)
        s2 = task_mgr.get_status(job_fail)
        if s1["status"] == "DONE" and s2["status"] == "FAILED":
            break
        time.sleep(0.02)

    resp_ok = client.get(f"/api/jobs/{job_ok}").get_json()
    resp_fail = client.get(f"/api/jobs/{job_fail}").get_json()
    assert resp_ok["status"] == "DONE"
    assert resp_ok["result"] == 42
    assert resp_fail["status"] == "FAILED"
    assert "ZeroDivisionError" in resp_fail["error"]


def test_sqlite_wal_mode_enabled(test_app) -> None:
    """Verify Phase 2 SQLite database engine enforces WAL journal mode."""
    db_session = test_app.extensions["db_session"]
    journal_mode = db_session.execute(text("PRAGMA journal_mode;")).scalar()
    assert str(journal_mode).lower() == "wal"


def test_domain_models_and_anti_proxy_constraints(test_app) -> None:
    """Verify Phase 2 ORM models and composite uniqueness constraints on AttendanceRecord & PollVote."""
    db = test_app.extensions["db_session"]

    cs = ClassSession(session_code="OOP101", title="OOP Lecture 1")
    s1 = Student(full_name="Ohad Ben Nun", email="ohad@ariel.ac.il", primary_device_id="dev-1")
    s2 = Student(full_name="Dana Levi", email="dana@ariel.ac.il", primary_device_id="dev-2")
    db.add_all([cs, s1, s2])
    db.commit()

    # Valid check-in for Student 1 on dev-1
    rec1 = AttendanceRecord(
        session_id=cs.id, student_id=s1.id, device_id="dev-1", token_bucket="200"
    )
    db.add(rec1)
    db.commit()

    # Proxy attempt: Student 2 tries checking in using Student 1's phone (dev-1) in the same session
    proxy_rec = AttendanceRecord(
        session_id=cs.id, student_id=s2.id, device_id="dev-1", token_bucket="200"
    )
    db.add(proxy_rec)
    with pytest.raises(IntegrityError):
        db.commit()
    db.rollback()

    # Duplicate student attempt: Student 1 tries checking in again from another device in the same session
    dup_student = AttendanceRecord(
        session_id=cs.id, student_id=s1.id, device_id="dev-99", token_bucket="201"
    )
    db.add(dup_student)
    with pytest.raises(IntegrityError):
        db.commit()
    db.rollback()

    # Verify remaining entities instantiate cleanly
    roster = RosterEntry(session_id=cs.id, full_name="Ohad Ben Nun", email="ohad@ariel.ac.il")
    snap = HeadcountSnapshot(
        session_id=cs.id,
        raw_image_path="raw.jpg",
        annotated_image_path="ann.jpg",
        auto_face_count=28,
        verified_face_count=29,
    )
    poll = Poll(session_id=cs.id, question="Is WAL mode enabled?", options_json='["Yes","No"]')
    db.add_all([roster, snap, poll])
    db.commit()

    vote1 = PollVote(poll_id=poll.id, student_id=s1.id, selected_index=0)
    db.add(vote1)
    db.commit()

    # Duplicate vote by same student on same poll must fail
    vote_dup = PollVote(poll_id=poll.id, student_id=s1.id, selected_index=1)
    db.add(vote_dup)
    with pytest.raises(IntegrityError):
        db.commit()
    db.rollback()

    msg = ChatMessage(session_id=cs.id, student_id=s1.id, author_name="Ohad", body="Great class!")
    art = LectureArtifact(session_id=cs.id, media_path="lecture1.mp4")
    assign = Assignment(title="Ex2 Web Tool", rubric_text="OOP & Design")
    db.add_all([msg, art, assign])
    db.commit()

    sub = HomeworkSubmission(
        assignment_id=assign.id, student_id=s1.id, source_ref="https://github.com/example/ex2"
    )
    agg = ClassStatsAggregate(assignment_id=assign.id, submission_count=1, mean_overall_score=95.0)
    db.add_all([sub, agg])
    db.commit()
    assert agg.mean_overall_score == 95.0


def test_concurrent_50_student_sqlite_wal_burst(test_app) -> None:
    """Verify Phase 2 SQLite WAL handles 50 concurrent student check-in writes without locking."""
    db = test_app.extensions["db_session"]
    cs = ClassSession(session_code="BURST50", title="Concurrency Stress Test")
    db.add(cs)
    db.commit()
    session_id = cs.id

    def _worker(idx: int) -> bool:
        local_db = test_app.extensions["db_session"]
        try:
            st = Student(
                full_name=f"Student {idx}",
                email=f"student{idx}@ariel.ac.il",
                primary_device_id=f"device-{idx}",
            )
            local_db.add(st)
            local_db.flush()
            rec = AttendanceRecord(
                session_id=session_id,
                student_id=st.id,
                device_id=f"device-{idx}",
                token_bucket="500",
            )
            local_db.add(rec)
            local_db.commit()
            return True
        finally:
            local_db.remove()

    with concurrent.futures.ThreadPoolExecutor(max_workers=20) as pool:
        results = list(pool.map(_worker, range(50)))

    assert all(results)
    total = db.query(AttendanceRecord).filter_by(session_id=session_id).count()
    assert total == 50
