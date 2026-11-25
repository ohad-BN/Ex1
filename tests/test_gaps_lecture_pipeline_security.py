from __future__ import annotations

import io
from pathlib import Path
from unittest.mock import patch

from flask import Flask

from app.services.lecture_pipeline import LecturePipelineError, LectureSummarizerService
from tests.conftest import login_student, register_lecturer


def test_corrupt_video_transitions_cleanly_to_failed_without_hallucination(app: Flask) -> None:
    """Uploading corrupt/unreadable .mp4 transitions cleanly to FAILED with no mock summary or hang."""
    prof = app.test_client()
    register_lecturer(prof, "Prof. Corrupt Vid", "corrupt_vid@ariel.ac.il")
    course = prof.post(
        "/api/courses", json={"course_code": "VID400", "name": "Corrupt Video Test"}
    ).get_json()
    sid = course["sessions"][0]["id"]

    # Force _transcribe_media_with_origin to raise LecturePipelineError just as ffmpeg/corrupt decode fails
    with patch.object(
        LectureSummarizerService,
        "_transcribe_media_with_origin",
        side_effect=LecturePipelineError(
            "Could not extract audio track from corrupted MP4 media.", status_code=400
        ),
    ):
        # 1. Sync corrupt upload returns clean 400 and records FAILED artifact with error_message
        sync_res = prof.post(
            f"/api/sessions/{sid}/lectures/summarize",
            data={"media": (io.BytesIO(b"not-a-real-mp4-stream"), "broken_sync.mp4")},
            content_type="multipart/form-data",
        )
        assert sync_res.status_code == 400
        assert "corrupted" in sync_res.get_json()["error"].lower()

        # 2. Async corrupt upload returns 202 and completes as FAILED in task_manager
        async_res = prof.post(
            f"/api/sessions/{sid}/lectures/summarize?async=1",
            data={"media": (io.BytesIO(b"not-a-real-mp4-stream"), "broken_async.mp4")},
            content_type="multipart/form-data",
        )
        assert async_res.status_code == 202
        app.extensions["task_manager"].shutdown(wait=True)

    arts = prof.get(f"/api/sessions/{sid}/lectures").get_json()["artifacts"]
    assert len(arts) == 2
    for art in arts:
        assert art["status"] == "FAILED"
        assert art["is_published"] is False
        assert not (art.get("summary_markdown") or "").strip()
        assert "corrupted" in (art.get("error_message") or "").lower()
        assert prof.post(f"/api/lectures/{art['id']}/publish", json={"publish": True}).status_code == 400


def test_lecture_publish_unpublish_delete_rbac_and_disk_cleanup(app: Flask) -> None:
    """Enforce student/cross-lecturer RBAC on publish/delete, string boolean coercion, and disk file removal."""
    prof_a = app.test_client()
    prof_b = app.test_client()
    stu = app.test_client()

    register_lecturer(prof_a, "Prof. Lec A", "leca@ariel.ac.il")
    register_lecturer(prof_b, "Prof. Lec B", "lecb@ariel.ac.il")
    c_a = prof_a.post("/api/courses", json={"course_code": "LECA1", "name": "Lec Sec A"}).get_json()
    cid_a = c_a["id"]
    sid_a = c_a["sessions"][0]["id"]

    login_student(stu, "Lec Student", "315000111")
    assert stu.post("/api/student/enroll", json={"course_id": cid_a}).status_code == 200

    md_bytes = b"### Executive Summary\nKey lecture insights.\n"
    created = prof_a.post(
        f"/api/sessions/{sid_a}/lectures/summarize",
        data={"media": (io.BytesIO(md_bytes), "notes.md")},
        content_type="multipart/form-data",
    )
    assert created.status_code == 201
    art = created.get_json()
    aid = art["id"]
    saved_media = Path(art["media_path"])
    assert saved_media.exists()

    # Unpublished transcript & summary download checks
    assert stu.get(f"/api/lectures/{aid}/download/transcript.txt").status_code == 403
    assert prof_a.get(f"/api/lectures/{aid}/download/transcript.txt?published_only=1").status_code == 403
    assert prof_a.get(f"/api/lectures/{aid}/download/transcript.txt").status_code == 200

    # Student lacks lecturer auth (401) and Outsider Lecturer blocked (403) on publish/unpublish/delete/download
    assert stu.post(f"/api/lectures/{aid}/publish", json={"publish": True}).status_code == 401
    assert stu.delete(f"/api/lectures/{aid}").status_code == 401
    assert prof_b.post(f"/api/lectures/{aid}/publish", json={"publish": True}).status_code == 403
    assert prof_b.delete(f"/api/lectures/{aid}").status_code == 403
    assert prof_b.get(f"/api/lectures/{aid}/download/summary.md").status_code == 403

    # String boolean coercion on /publish ("true" / "false")
    pub_str = prof_a.post(f"/api/lectures/{aid}/publish", json={"publish": "true"})
    assert pub_str.status_code == 200 and pub_str.get_json()["is_published"] is True
    assert stu.get(f"/api/lectures/{aid}/download/transcript.txt").status_code == 200

    unpub_str = prof_a.post(f"/api/lectures/{aid}/publish", json={"publish": "false"})
    assert unpub_str.status_code == 200 and unpub_str.get_json()["is_published"] is False

    # Deleting artifact removes physical media file on disk and returns 404 thereafter
    assert prof_a.delete(f"/api/lectures/{aid}").status_code == 200
    assert not saved_media.exists()
    assert prof_a.get(f"/api/lectures/{aid}/download/summary.md").status_code == 404


def test_structured_summary_sections_parser_edge_cases(app: Flask) -> None:
    """Cover parse_summary_sections for empty, unheaded prose, bullet-only, standard, and RTL sections."""
    svc = LectureSummarizerService(db=app.extensions["db_session"])

    assert svc.parse_summary_sections("") == {
        "executive_summary": [],
        "key_topics": [],
        "action_items": [],
        "extra_sections": [],
    }

    bullets_only = svc.parse_summary_sections("- First takeaway\n- Second takeaway\n- Third takeaway\n")
    assert bullets_only["executive_summary"] == ["First takeaway", "Second takeaway", "Third takeaway"]

    rtl_extra = svc.parse_summary_sections("## סיכום\n- נקודה ראשונה\n- נקודה שנייה\n- נקודה שלישית\n")
    assert len(rtl_extra["executive_summary"]) == 2
    assert rtl_extra["key_topics"] == ["נקודה שלישית"]
    assert rtl_extra["extra_sections"][0]["title"] == "סיכום"
