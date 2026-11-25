from __future__ import annotations

import io
from pathlib import Path
import zipfile

from flask import Flask
import pytest

from app.models import ClassStatsAggregate
from app.services.homework_pipeline import CohortAggregator, format_ordinal
from tests.conftest import login_student, register_lecturer


def test_ordinal_rank_formatting_and_cohort_tie_edge_cases() -> None:
    """Cover 11th/12th/13th/21st ordinals, empty/single cohort, multi-way ties, and uniform low ties."""
    expected = {
        1: "1st",
        2: "2nd",
        3: "3rd",
        4: "4th",
        11: "11th",
        12: "12th",
        13: "13th",
        21: "21st",
        22: "22nd",
        23: "23rd",
    }
    for n, label in expected.items():
        assert format_ordinal(n) == label

    empty = CohortAggregator.compute_relative_class_score([], 75.0)
    assert empty["rank"] == 1
    assert empty["rank_label"] == "1st Place"
    assert empty["cohort_size"] == 1
    assert empty["class_score"] == 10

    # Multi-way ties get identical rank (1, 1, 3) and label
    top_tie = CohortAggregator.compute_relative_class_score([90.0, 90.0, 70.0], 90.0)
    third = CohortAggregator.compute_relative_class_score([90.0, 90.0, 70.0], 70.0)
    assert top_tie["rank"] == 1 and top_tie["rank_label"] == "1st Place"
    assert third["rank"] == 3 and third["rank_label"] == "3rd Place"

    # Low identical scores across >=2 students scale cleanly below 10
    low_tie = CohortAggregator.compute_relative_class_score([40.0, 40.0], 40.0)
    assert low_tie["rank"] == 1
    assert 1 <= low_tie["class_score"] < 10


def test_comparative_and_final_submissions_are_completely_independent(app: Flask) -> None:
    """Comparative practice iterations and final lecturer submissions keep separate version history & stats."""
    prof = app.test_client()
    stu = app.test_client()
    db = app.extensions["db_session"]

    register_lecturer(prof, "Prof. Dual Mode", "dual_mode@ariel.ac.il")
    course = prof.post("/api/courses", json={"course_code": "DM101", "name": "Dual Mode"}).get_json()
    cid = course["id"]
    aid = prof.post(
        f"/api/courses/{cid}/assignments",
        json={"title": "HW1 Dual Track", "rubric_text": "Implement Queue with tests"},
    ).get_json()["id"]

    login_student(stu, "Dual Student", "311000999")
    assert stu.post("/api/student/enroll", json={"course_id": cid}).status_code == 200

    # 1. Comparative v1
    c1 = stu.post(
        f"/api/assignments/{aid}/submissions",
        json={"submission_type": "comparative", "content_text": "def f():\n    return 1\n"},
    ).get_json()
    assert c1["submission_type"] == "comparative" and c1["version"] == 1
    stats_after_c1 = db.get(ClassStatsAggregate, aid)
    assert stats_after_c1 is not None and stats_after_c1.submission_count == 1
    comp_score_snapshot = stats_after_c1.scores_list_json

    # 2. Final v1 -> MUST NOT increment ClassStatsAggregate
    f1 = stu.post(
        f"/api/assignments/{aid}/submissions",
        json={"submission_type": "final", "content_text": "class Queue:\n    pass\n"},
    ).get_json()
    assert f1["submission_type"] == "final" and f1["version"] == 1
    db.remove()
    stats_after_f1 = db.get(ClassStatsAggregate, aid)
    assert stats_after_f1 is not None and stats_after_f1.submission_count == 1
    assert stats_after_f1.scores_list_json == comp_score_snapshot

    # 3. Comparative v2 -> leaves Final v1 untouched for lecturer & increments comparative version to 2
    c2 = stu.post(
        f"/api/assignments/{aid}/submissions",
        json={
            "submission_type": "comparative",
            "content_text": "class Queue:\n    def push(self, x):\n        return x\n\ndef test_q():\n    assert Queue().push(1) == 1\n",
        },
    ).get_json()
    assert c2["submission_type"] == "comparative" and c2["version"] == 2
    lec_view = prof.get(f"/api/assignments/{aid}").get_json()
    assert len(lec_view["submissions"]) == 1
    assert lec_view["submissions"][0]["id"] == f1["id"]
    assert lec_view["submissions"][0]["version"] == 1

    # 4. Final v2 -> increments final version to 2, lecturer now sees final v2 only, cohort count stays 1
    f2 = stu.post(
        f"/api/assignments/{aid}/submissions",
        json={"submission_type": "final", "content_text": "class QueueV2:\n    pass\n"},
    ).get_json()
    assert f2["submission_type"] == "final" and f2["version"] == 2
    db.remove()
    stats_after_f2 = db.get(ClassStatsAggregate, aid)
    assert stats_after_f2 is not None and stats_after_f2.submission_count == 1
    lec_view2 = prof.get(f"/api/assignments/{aid}").get_json()
    assert len(lec_view2["submissions"]) == 1 and lec_view2["submissions"][0]["version"] == 2


def test_submission_download_and_deep_review_security_matrix(app: Flask) -> None:
    """Enforce strict RBAC on /download and /deep-review, including blocking Lecturer access to comparative files."""
    prof_a = app.test_client()
    prof_b = app.test_client()
    stu_a = app.test_client()
    stu_b = app.test_client()

    register_lecturer(prof_a, "Prof. HW A", "hwa@ariel.ac.il")
    register_lecturer(prof_b, "Prof. HW B", "hwb@ariel.ac.il")
    course = prof_a.post("/api/courses", json={"course_code": "HWS10", "name": "HW Sec"}).get_json()
    cid = course["id"]
    aid = prof_a.post(
        f"/api/courses/{cid}/assignments", json={"title": "Sec Lab", "rubric_text": "Clean OOP"}
    ).get_json()["id"]

    login_student(stu_a, "Student A", "322000111")
    login_student(stu_b, "Student B", "322000222")
    assert stu_a.post("/api/student/enroll", json={"course_id": cid}).status_code == 200
    assert stu_b.post("/api/student/enroll", json={"course_id": cid}).status_code == 200

    # Student A submits comparative file, final file, and text-only final
    comp_file_res = stu_a.post(
        f"/api/assignments/{aid}/submissions",
        data={
            "submission_type": "comparative",
            "file": (io.BytesIO(b"# private draft\nx = 1\n"), "draft.py"),
        },
        content_type="multipart/form-data",
    )
    assert comp_file_res.status_code == 201
    comp_id = comp_file_res.get_json()["id"]

    final_file_res = stu_a.post(
        f"/api/assignments/{aid}/submissions",
        data={
            "submission_type": "final",
            "file": (io.BytesIO(b"# official final\nx = 2\n"), "final.py"),
        },
        content_type="multipart/form-data",
    )
    assert final_file_res.status_code == 201
    final_id = final_file_res.get_json()["id"]

    # Download RBAC matrix
    assert stu_b.get(f"/api/submissions/{comp_id}/download").status_code == 403
    assert stu_b.get(f"/api/submissions/{final_id}/download").status_code == 403
    assert prof_b.get(f"/api/submissions/{comp_id}/download").status_code == 403
    assert prof_b.get(f"/api/submissions/{final_id}/download").status_code == 403
    # CRITICAL: Lecturer A CANNOT download Student A's comparative draft!
    assert prof_a.get(f"/api/submissions/{comp_id}/download").status_code == 403
    # Lecturer A CAN download Student A's final file
    dl_lec = prof_a.get(f"/api/submissions/{final_id}/download")
    assert dl_lec.status_code == 200 and b"official final" in dl_lec.data
    # Student A CAN download both own files
    assert stu_a.get(f"/api/submissions/{comp_id}/download").status_code == 200
    assert stu_a.get(f"/api/submissions/{final_id}/download").status_code == 200

    # Deep-review RBAC matrix
    assert stu_b.post(f"/api/submissions/{comp_id}/deep-review").status_code == 403
    assert prof_b.post(f"/api/submissions/{final_id}/deep-review").status_code == 403
    assert prof_a.post(f"/api/submissions/{comp_id}/deep-review").status_code == 403
    rev_stu = stu_a.post(f"/api/submissions/{comp_id}/deep-review")
    assert rev_stu.status_code == 200 and rev_stu.get_json()["deep_review_md"]
    rev_lec = prof_a.post(f"/api/submissions/{final_id}/deep-review")
    assert rev_lec.status_code == 200 and rev_lec.get_json()["deep_review_md"]

    # Text-only submission has no downloadable file -> 404
    txt_sub = stu_a.post(
        f"/api/assignments/{aid}/submissions",
        json={"submission_type": "final", "content_text": "class TextOnly:\n    pass\n"},
    ).get_json()
    assert prof_a.get(f"/api/submissions/{txt_sub['id']}/download").status_code == 404


def test_homework_corrupt_archives_and_path_traversal_sanitization(app: Flask) -> None:
    """Reject corrupt .zip/.docx/binary payloads with 400 and confine uploads strictly inside configured UPLOAD_DIR."""
    prof = app.test_client()
    stu = app.test_client()
    upload_root = Path(app.config["CLASSPULSE_CONFIG"].UPLOAD_DIR).resolve()

    register_lecturer(prof, "Prof. Fuzz", "fuzz_hw@ariel.ac.il")
    course = prof.post("/api/courses", json={"course_code": "FZ101", "name": "Upload Safety"}).get_json()
    aid = prof.post(
        f"/api/courses/{course['id']}/assignments", json={"title": "Upload Lab", "rubric_text": ""}
    ).get_json()["id"]
    login_student(stu, "Fuzz Stu", "333444555")
    assert stu.post("/api/student/enroll", json={"course_id": course["id"]}).status_code == 200

    # Corrupt zip -> 400
    bad_zip = stu.post(
        f"/api/assignments/{aid}/submissions",
        data={"file": (io.BytesIO(b"PK\x03\x04corrupt_not_a_zip"), "bad.zip")},
        content_type="multipart/form-data",
    )
    assert bad_zip.status_code == 400

    # Corrupt docx -> 400
    bad_docx = stu.post(
        f"/api/assignments/{aid}/submissions",
        data={"file": (io.BytesIO(b"not-a-docx-archive"), "bad.docx")},
        content_type="multipart/form-data",
    )
    assert bad_docx.status_code == 400

    # Path-traversal filename sanitized into course upload subtree (never escaping UPLOAD_DIR)
    trav = stu.post(
        f"/api/assignments/{aid}/submissions",
        data={"file": (io.BytesIO(b"def safe():\n    return 1\n"), "../../escape_test.py")},
        content_type="multipart/form-data",
    )
    assert trav.status_code == 201
    saved = Path(trav.get_json()["source_ref"]).resolve()
    assert saved.is_file()
    assert upload_root in saved.parents
    assert not (Path("escape_test.py")).exists()
