from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app import create_app
from app.config import Config
from app.models import ClassSession, Course, Student
from app.services.attendance_service import AttendanceService
from app.services.chat_service import ChatService
from app.services.course_service import CourseService
from app.services.homework_pipeline import HomeworkReviewService
from app.services.lecture_pipeline import LectureSummarizerService
from app.services.poll_service import PollService


def seed_demo() -> None:
    """Populate realistic school-wide multi-course demo data without duplicating records."""
    cfg = Config()
    app = create_app(cfg)
    with app.app_context():
        db = app.extensions["db_session"]
        qr_service = app.extensions["qr_service"]
        device_mgr = app.extensions["device_mgr"]

        course_svc = CourseService(db)
        courses = course_svc.ensure_default_school_courses()
        cs204 = next((c for c in courses if c.course_code == "CS204"), courses[0])
        sess = (
            db.query(ClassSession)
            .filter_by(course_id=cs204.id)
            .order_by(ClassSession.id.asc())
            .first()
        )
        if sess is None:
            print("No default session found.")
            return

        # 1. Seed Course Roster
        att_svc = AttendanceService(db=db, qr_service=qr_service, device_mgr=device_mgr)
        sample_roster = (
            "Ohad Ben Nun, ohad@ariel.ac.il\n"
            "Alice Cohen, alice@ariel.ac.il\n"
            "Bob Levi, bob@ariel.ac.il\n"
            "Dana Mizrahi, dana@ariel.ac.il\n"
        )
        att_svc.import_roster_csv(sess.id, sample_roster)

        # 2. Seed Saved Polls in Lecture
        poll_svc = PollService(db)
        if not poll_svc.list_session_polls(sess.id):
            poll_svc.create_poll(
                session_id=sess.id,
                question="Which pattern decouples FaceCounter/LLM adapters from services?",
                options=["Bridge / Adapter Pattern", "God Object", "Global Variables"],
                correct_index=0,
            )

        # 3. Seed Timestamped Classroom Q&A
        chat_svc = ChatService(db)
        if not chat_svc.list_messages(sess.id):
            chat_svc.post_message(
                session_id=sess.id,
                author_name="Alice Cohen",
                category="Question",
                body="How does O(1) incremental cohort aggregation handle resubmissions?",
            )

        # 4. Seed Feature 4 AI Lecture Summary
        lec_svc = LectureSummarizerService(db=db, upload_dir=cfg.UPLOAD_DIR)
        if not lec_svc.list_session_artifacts(sess.id):
            lec_svc.summarize_session_recording(session_id=sess.id, use_sample=True)

        # 5. Seed Feature 5 Homework Assignment + Cohort Submissions
        hw_svc = HomeworkReviewService(db=db)
        hw_svc.seed_sample_course_assignment(cs204.id)

        print(f"Seeded ClassPulse demo data for course {cs204.course_code} (session {sess.session_code}).")

    app.extensions["task_manager"].shutdown(wait=True)
    app.extensions["db_engine"].dispose()


if __name__ == "__main__":
    seed_demo()
