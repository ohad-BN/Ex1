from __future__ import annotations

import io
import json
import zipfile
from pathlib import Path

import pytest

from app import create_app, socketio
from app.config import Config
from app.models import ClassSession, ClassStatsAggregate, Course, HomeworkSubmission, Student
from app.services.face_counter import BoundingBox, FaceDetector
from app.services.homework_pipeline import (
    CohortAggregator,
    HomeworkMetrics,
    HomeworkMetricsResponse,
    HomeworkPipelineError,
    HomeworkReviewService,
    SubmissionExtractor,
)


class StubDetector(FaceDetector):
    def detect(self, image) -> list[BoundingBox]:
        return []


@pytest.fixture()
def hw_app(tmp_path: Path):
    db_file = tmp_path / "test_phase8_homework.db"
    cfg = Config(
        SECRET_KEY="phase8-homework-secret",
        DATABASE_URL=f"sqlite:///{db_file.as_posix()}",
        UPLOAD_DIR=tmp_path / "uploads",
        REPORT_DIR=tmp_path / "reports",
        GEMINI_API_KEY="",
    )
    app = create_app(cfg, face_detector=StubDetector())
    app.config["TESTING"] = True
    yield app
    app.extensions["task_manager"].shutdown(wait=True)
    app.extensions["db_engine"].dispose()


def test_submission_extractor_filters_ignored_dirs_and_truncates() -> None:
    """Suite 8: SubmissionExtractor skips .git/node_modules/__pycache__ and respects max_chars."""
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr(".git/config", "secret git config")
        zf.writestr("node_modules/pkg/index.js", "ignored module")
        zf.writestr("src/__pycache__/main.cpython-312.pyc", "binary")
        zf.writestr("src/main.py", "class Bridge:\n    pass\n")
        zf.writestr("README.md", "# Project\n" + ("x" * 200))

    extractor = SubmissionExtractor(max_chars=120)
    extracted = extractor.extract_from_zip_bytes(buf.getvalue())
    assert "src/main.py" in extracted
    assert "secret git config" not in extracted
    assert "ignored module" not in extracted
    assert len(extracted) <= 120


def test_cohort_aggregator_o1_math_and_exact_resubmission_subtraction() -> None:
    """Suite 8: Verify incremental O(1) mean, topic sums, tags, percentile, and resubmission rollback."""
    stats = ClassStatsAggregate(assignment_id=1)

    m1 = HomeworkMetrics(
        overall_score=80.0,
        topic_scores={"OOP": 80.0, "Testing": 80.0},
        requirements_coverage_pct=85.0,
        strengths=["Good classes"],
        weaknesses=["Needs tests"],
        error_tags=["missing_edge_case_tests"],
        style_summary="Clean",
    )
    m2 = HomeworkMetrics(
        overall_score=60.0,
        topic_scores={"OOP": 60.0, "Testing": 60.0},
        requirements_coverage_pct=60.0,
        strengths=["Readable"],
        weaknesses=["No tests"],
        error_tags=["missing_edge_case_tests", "hardcoded_paths"],
        style_summary="Procedural",
    )

    CohortAggregator.apply_metrics(stats, m1)
    CohortAggregator.apply_metrics(stats, m2)
    assert stats.submission_count == 2
    assert stats.mean_overall_score == 70.0
    assert json.loads(stats.scores_list_json) == [60.0, 80.0]

    # Student 2 resubmits with score 90.0 (should cleanly revert 60.0 first!)
    m2_v2 = HomeworkMetrics(
        overall_score=90.0,
        topic_scores={"OOP": 90.0, "Testing": 90.0},
        requirements_coverage_pct=95.0,
        strengths=["Clean OOP", "Full tests"],
        weaknesses=[],
        error_tags=[],
        style_summary="Modular",
    )
    CohortAggregator.revert_metrics(stats, m2)
    CohortAggregator.apply_metrics(stats, m2_v2)

    assert stats.submission_count == 2
    assert stats.mean_overall_score == 85.0  # (80 + 90) / 2
    assert json.loads(stats.scores_list_json) == [80.0, 90.0]
    snap = CohortAggregator.build_anonymized_cohort_snapshot(stats, student_score=90.0)
    assert snap["student_percentile"] == 100.0
    assert snap["topic_averages"] == {"OOP": 85.0, "Testing": 85.0}
    assert snap["top_error_tags"] == [{"tag": "missing_edge_case_tests", "count": 1}]


def test_homework_rest_workflow_and_zero_pii_prompt_guarantee(hw_app) -> None:
    """Suite 8: Multi-course homework API, resubmission v2, and zero peer PII in Stage 2."""
    client = hw_app.test_client()
    db = hw_app.extensions["db_session"]

    course = Course(course_code="SE202", name="Software Design", department="CS")
    s1 = Student(full_name="Peer One PrivateName", email="peer1_secret@ariel.ac.il", primary_device_id="p1")
    s2 = Student(full_name="Ohad Ben Nun", email="ohad@ariel.ac.il", primary_device_id="p2")
    db.add_all([course, s1, s2])
    db.commit()

    # 1. Lecturer creates assignment for course SE202
    a_resp = client.post(
        f"/api/courses/{course.id}/assignments",
        json={
            "title": "HW2 — Strategy & Adapter",
            "rubric_text": "Grade OOP design, tests, error handling.",
            "topics": ["OOP", "Testing"],
        },
    )
    assert a_resp.status_code == 201
    aid = a_resp.get_json()["id"]

    # 2. Peer 1 submits homework containing unique secret string & PII
    p1_code = 'SECRET_PEER_TOKEN_XYZ = 42\ndef run():\n    return "peer1_only"\n'
    r1 = client.post(
        f"/api/assignments/{aid}/submissions",
        json={"student_id": s1.id, "content_text": p1_code},
    )
    assert r1.status_code == 201

    # 3. Student 2 submits v1, then resubmits v2 with tests & try/except
    r2_v1 = client.post(
        f"/api/assignments/{aid}/submissions",
        json={"student_id": s2.id, "content_text": 'def hello():\n    print("v1")\n'},
    )
    assert r2_v1.status_code == 201
    assert r2_v1.get_json()["version"] == 1

    r2_v2 = client.post(
        f"/api/assignments/{aid}/submissions",
        json={
            "student_id": s2.id,
            "content_text": (
                'class Strategy:\n    """Doc."""\n    def run(self):\n        try:\n            return 1\n        except Exception:\n            return 0\n'
                '\ndef test_strategy():\n    assert Strategy().run() == 1\n'
            ),
        },
    )
    assert r2_v2.status_code == 201
    v2_data = r2_v2.get_json()
    assert v2_data["version"] == 2
    assert v2_data["cohort_snapshot"]["submission_count"] == 2
    assert "Comparative Performance Summary" in v2_data["deep_review_md"]

    # 4. Inspect Stage-2 prompt payload and verify ZERO peer PII or peer code leaked!
    svc = HomeworkReviewService(db=db)
    stage2_json = json.dumps(svc.build_stage2_prompt_payload(v2_data["id"]))
    assert "Peer One PrivateName" not in stage2_json
    assert "peer1_secret@ariel.ac.il" not in stage2_json
    assert "SECRET_PEER_TOKEN_XYZ" not in stage2_json

    # 5. Seed-sample 1-click endpoint populates cohort cleanly
    seed_resp = client.post(f"/api/courses/{course.id}/assignments/seed-sample")
    assert seed_resp.status_code == 201
    assert seed_resp.get_json()["cohort_stats"]["submission_count"] == 3


def _contains_schema_key(node: object, target_key: str) -> bool:
    if isinstance(node, dict):
        if target_key in node:
            return True
        return any(_contains_schema_key(v, target_key) for v in node.values())
    if isinstance(node, list):
        return any(_contains_schema_key(item, target_key) for item in node)
    return False


def test_homework_metrics_response_schema_has_no_additional_properties() -> None:
    """Gemini Developer API rejects schemas with additionalProperties keys from dict fields."""
    assert _contains_schema_key(HomeworkMetrics.model_json_schema(), "additionalProperties") is True
    assert _contains_schema_key(HomeworkMetricsResponse.model_json_schema(), "additionalProperties") is False

    from_list = HomeworkMetricsResponse.model_validate(
        {
            "overall_score": 91.5,
            "topic_scores": [{"topic": "OOP", "score": 94.0}, {"topic": "Tests", "score": 89.0}],
            "requirements_coverage_pct": 95.0,
            "strengths": ["Clear encapsulation"],
            "weaknesses": [],
            "error_tags": [],
            "style_summary": "Solid",
        }
    ).to_metrics()
    assert isinstance(from_list, HomeworkMetrics)
    assert from_list.topic_scores == {"OOP": 94.0, "Tests": 89.0}

    from_dict = HomeworkMetricsResponse.model_validate(
        {
            "overall_score": 82.0,
            "topic_scores": {"OOP": 82.0},
            "requirements_coverage_pct": 80.0,
        }
    ).to_metrics()
    assert from_dict.topic_scores == {"OOP": 82.0}


def test_assignment_created_socketio_broadcast_and_async_submission(hw_app, tmp_path: Path) -> None:
    """Verify real-time assignment:created emits and non-blocking ?async=1 homework review."""
    client = hw_app.test_client()
    db = hw_app.extensions["db_session"]

    course = Course(course_code="RT301", name="Realtime OOP", department="CS")
    db.add(course)
    db.flush()
    sess = ClassSession(course_id=course.id, session_code="RT301-S1", title="Lecture 1")
    student = Student(full_name="Dana Student", email="dana_rt@ariel.ac.il", primary_device_id="rt-1")
    db.add_all([sess, student])
    db.commit()

    sock = socketio.test_client(hw_app, flask_test_client=client)
    assert sock.is_connected()
    sock.emit("join_session", {"session_code": "RT301-S1", "student_id": student.id, "as_student": True})
    sock.get_received()  # flush initial join_session events

    # 1. Creating an assignment emits assignment:created immediately without private submissions
    create_resp = client.post(
        f"/api/courses/{course.id}/assignments",
        json={
            "title": "HW3 — Async Events",
            "rubric_text": "Implement non-blocking observer broadcasts.",
            "topics": ["Observer", "Async"],
        },
    )
    assert create_resp.status_code == 201
    aid = create_resp.get_json()["id"]

    received = sock.get_received()
    created_events = [evt for evt in received if evt["name"] == "assignment:created"]
    assert len(created_events) == 1
    assert created_events[0]["args"][0]["title"] == "HW3 — Async Events"
    assert "submissions" not in created_events[0]["args"][0]

    # 2. Seeding sample emits once, and repeating seed-sample does not emit duplicate toast
    seed_resp = client.post(f"/api/courses/{course.id}/assignments/seed-sample")
    assert seed_resp.status_code == 201
    seed_events = [e for e in sock.get_received() if e["name"] == "assignment:created"]
    assert len(seed_events) == 1

    seed_again = client.post(f"/api/courses/{course.id}/assignments/seed-sample")
    assert seed_again.status_code == 201
    dup_events = [e for e in sock.get_received() if e["name"] == "assignment:created"]
    assert len(dup_events) == 0

    # 3. Validation errors: unauthenticated (401) and empty upload (400)
    no_auth = client.post(f"/api/assignments/{aid}/submissions", json={"content_text": "class A: pass"})
    assert no_auth.status_code == 401

    empty_file = tmp_path / "empty.txt"
    empty_file.write_text("", encoding="utf-8")
    with pytest.raises(HomeworkPipelineError):
        SubmissionExtractor().extract(file_path=empty_file)

    # 4. Async file submission returns 202 immediately, finishes in background, and emits assignment:review_ready
    sub_resp = client.post(
        f"/api/assignments/{aid}/submissions?async=1",
        data={
            "student_id": str(student.id),
            "content_text": "Includes Observer unit tests.",
            "file": (io.BytesIO(b"class Observer:\n    def update(self):\n        return True\n"), "solution.py"),
        },
        content_type="multipart/form-data",
    )
    assert sub_resp.status_code == 202
    assert sub_resp.get_json()["status"] == "PENDING"

    # Wait for background worker completion via task_manager and verify DB + Socket.IO notification
    hw_app.extensions["task_manager"].shutdown(wait=True)
    review_events = [e for e in sock.get_received() if e["name"] == "assignment:review_ready"]
    assert len(review_events) == 1
    assert review_events[0]["args"][0]["assignment_id"] == aid
    assert review_events[0]["args"][0]["student_id"] == student.id
    assert "Comparative Performance Summary" in review_events[0]["args"][0]["submission"]["deep_review_md"]

    sub_row = db.query(HomeworkSubmission).filter_by(assignment_id=aid, student_id=student.id).one()
    assert sub_row.source_type == "file"
    assert "Comparative Performance Summary" in sub_row.deep_review_md
    sock.disconnect()


def test_dynamic_class_relative_score_and_category_breakdown(hw_app) -> None:
    """Verify dynamic 1-10 Class Standing (Ohad 10/10 -> Enav 10/10 & Ohad 7/10) + categories."""
    # 1. Pure formula unit verification matching the exact user progression
    ohad_only = CohortAggregator.compute_relative_class_score([78.0], 78.0)
    assert ohad_only["class_score"] == 10
    assert ohad_only["rank"] == 1

    enav_n2 = CohortAggregator.compute_relative_class_score([78.0, 92.0], 92.0)
    ohad_n2 = CohortAggregator.compute_relative_class_score([78.0, 92.0], 78.0)
    assert enav_n2["class_score"] == 10
    assert enav_n2["rank"] == 1
    assert ohad_n2["class_score"] == 7
    assert ohad_n2["rank"] == 2

    # Ties get identical score & rank
    tie_a = CohortAggregator.compute_relative_class_score([60.0, 85.0, 85.0], 85.0)
    tie_b = CohortAggregator.compute_relative_class_score([60.0, 85.0, 85.0], 85.0)
    assert tie_a["class_score"] == 10 and tie_a["rank"] == 1
    assert tie_a == tie_b

    # 2. End-to-end API + Socket.IO assignment:cohort_updated verification
    client = hw_app.test_client()
    db = hw_app.extensions["db_session"]
    course = Course(course_code="CLS10", name="Dual Score Design", department="CS")
    db.add(course)
    db.flush()
    sess = ClassSession(course_id=course.id, session_code="CLS10-L1", title="Lecture 1")
    ohad = Student(full_name="Ohad", email="ohad_cls@ariel.ac.il", primary_device_id="cls-ohad")
    enav = Student(full_name="Enav", email="enav_cls@ariel.ac.il", primary_device_id="cls-enav")
    db.add_all([sess, ohad, enav])
    db.commit()

    aid = client.post(
        f"/api/courses/{course.id}/assignments",
        json={
            "title": "HW4 — Class Comparison",
            "rubric_text": "Grade materials, style, architecture, and tests.",
            "topics": ["Materials", "Style"],
        },
    ).get_json()["id"]

    sock = socketio.test_client(hw_app, flask_test_client=client)
    sock.emit("join_session", {"session_code": "CLS10-L1", "student_id": ohad.id, "as_student": True})
    sock.get_received()

    # Step A: Ohad submits basic script first -> gets 10/10 Class Score
    r_ohad = client.post(
        f"/api/assignments/{aid}/submissions",
        json={"student_id": ohad.id, "content_text": "def solve(x):\n    return x + 1\n"},
    )
    assert r_ohad.status_code == 201
    ohad_sub = r_ohad.get_json()
    ohad_rubric_grade = ohad_sub["ai_rubric_grade"]
    assert ohad_sub["class_standing"]["class_score"] == 10
    assert ohad_sub["class_standing"]["cohort_size"] == 1
    cat_labels = [c["label"] for c in ohad_sub["category_breakdown"]]
    assert cat_labels == [
        "Materials & Details",
        "Execution Style",
        "Technical Logic & Architecture",
        "Testing & Verification",
    ]
    assert "line_count" in ohad_sub["telemetry_comparison"]

    # Step B: Enav submits rich OOP + docstring + try/except + pytest -> Enav 10/10 & Socket emits cohort_updated
    enav_code = (
        'class SolutionManager:\n    """Well-documented OOP solution."""\n'
        '    def run(self, items):\n        try:\n            return len(items) > 0\n'
        '        except Exception:\n            return False\n\n'
        'def test_solution():\n    assert SolutionManager().run([1]) is True\n'
    )
    r_enav = client.post(
        f"/api/assignments/{aid}/submissions",
        json={"student_id": enav.id, "content_text": enav_code},
    )
    assert r_enav.status_code == 201
    enav_sub = r_enav.get_json()
    assert enav_sub["class_standing"]["class_score"] == 10
    assert enav_sub["class_standing"]["rank"] == 1
    assert enav_sub["ai_rubric_grade"] > ohad_rubric_grade

    cohort_events = [e for e in sock.get_received() if e["name"] == "assignment:cohort_updated"]
    assert len(cohort_events) >= 1
    assert "Enav" not in json.dumps(cohort_events[-1])

    # Step C: Ohad's AI Rubric Grade is UNCHANGED, while his dynamic Class Standing dropped below 10!
    ohad_refetched = client.get(f"/api/assignments/{aid}?student_id={ohad.id}").get_json()["my_submission"]
    assert ohad_refetched["ai_rubric_grade"] == ohad_rubric_grade
    assert ohad_refetched["class_standing"]["class_score"] < 10
    assert ohad_refetched["class_standing"]["rank"] == 2
    assert ohad_refetched["class_standing"]["cohort_size"] == 2

    # Step D: Ohad resubmits improved solution -> category distributions stay balanced at count=2
    r_ohad_v2 = client.post(
        f"/api/assignments/{aid}/submissions",
        json={"student_id": ohad.id, "content_text": enav_code},
    )
    assert r_ohad_v2.status_code == 201
    v2 = r_ohad_v2.get_json()
    assert v2["version"] == 2
    assert v2["class_standing"]["cohort_size"] == 2
    assert v2["class_standing"]["class_score"] == 10
    stats_row = db.get(ClassStatsAggregate, aid)
    cat_lists = json.loads(stats_row.category_scores_json)
    assert len(cat_lists["Materials & Details"]) == 2
    sock.disconnect()


def test_homework_docx_and_hebrew_filename_upload_preservation(hw_app) -> None:
    """Phase 4: Uploading .docx and Unicode/Hebrew filenames preserves extension and extracts text cleanly."""
    client = hw_app.test_client()
    db = hw_app.extensions["db_session"]

    course = Course(course_code="DOCX1", name="File Upload Course", department="CS")
    student = Student(full_name="Ohad Ben Nun", email="ohad_docx@ariel.ac.il", primary_device_id="docx-1")
    db.add_all([course, student])
    db.commit()

    aid = client.post(
        f"/api/courses/{course.id}/assignments",
        json={"title": "HW5 — Word & Hebrew Uploads", "rubric_text": "Algorithm analysis."},
    ).get_json()["id"]

    # Build valid minimal .docx archive in memory
    docx_buf = io.BytesIO()
    doc_xml = (
        '<?xml version="1.0" encoding="UTF-8"?>'
        '<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">'
        "<w:body>"
        "<w:p><w:r><w:t>def fibonacci(n): return n</w:t></w:r></w:p>"
        "<w:p><w:r><w:t>פתרון בעברית למטלה</w:t></w:r></w:p>"
        "</w:body>"
        "</w:document>"
    ).encode("utf-8")
    with zipfile.ZipFile(docx_buf, "w") as zf:
        zf.writestr("word/document.xml", doc_xml)
    docx_buf.seek(0)

    r_docx = client.post(
        f"/api/assignments/{aid}/submissions",
        data={
            "student_id": str(student.id),
            "file": (docx_buf, "מטלה1.docx"),
        },
        content_type="multipart/form-data",
    )
    assert r_docx.status_code == 201
    sub_docx = r_docx.get_json()
    saved_docx = Path(sub_docx["source_ref"])
    assert saved_docx.suffix == ".docx"
    extracted_docx = SubmissionExtractor().extract_from_file(saved_docx)
    assert "def fibonacci" in extracted_docx
    assert "פתרון בעברית" in extracted_docx

    # Hebrew Python filename also preserves .py extension
    r_py = client.post(
        f"/api/assignments/{aid}/submissions",
        data={
            "student_id": str(student.id),
            "file": (io.BytesIO(b"def solve():\n    return 42\n"), "פתרון.py"),
        },
        content_type="multipart/form-data",
    )
    assert r_py.status_code == 201
    assert Path(r_py.get_json()["source_ref"]).suffix == ".py"
