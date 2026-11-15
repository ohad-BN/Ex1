from __future__ import annotations

import io
import json
import zipfile
from pathlib import Path

import pytest

from app import create_app
from app.config import Config
from app.models import ClassStatsAggregate, Course, Student
from app.services.face_counter import BoundingBox, FaceDetector
from app.services.homework_pipeline import (
    CohortAggregator,
    HomeworkMetrics,
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
