from __future__ import annotations

import csv
import io
import re
from typing import Any, Optional

from sqlalchemy.orm import Session

from app.models import (
    Assignment,
    AttendanceRecord,
    ChatMessage,
    ClassSession,
    Course,
    CourseEnrollment,
    HomeworkSubmission,
    LectureArtifact,
    Lecturer,
    Poll,
    RosterEntry,
    Student,
)
from app.services.homework_pipeline import HomeworkReviewService
from app.services.lecture_pipeline import LectureSummarizerService

EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


class CourseError(Exception):
    """Domain error for multi-course management."""

    def __init__(self, message: str, status_code: int = 400) -> None:
        super().__init__(message)
        self.message = message
        self.status_code = status_code


class CourseService:
    """Manages school-wide courses, enrollments, course-level rosters, and student app views."""

    def __init__(self, db: Session) -> None:
        self.db = db

    def ensure_default_school_courses(self) -> list[Course]:
        """Ensure at least two school courses exist so the multi-course selector is populated."""
        existing = self.db.query(Course).order_by(Course.id.asc()).all()
        if existing:
            # Attach any orphan sessions to the first course
            first_course = existing[0]
            orphans = (
                self.db.query(ClassSession).filter(ClassSession.course_id.is_(None)).all()
            )
            if orphans:
                for s in orphans:
                    s.course_id = first_course.id
                self.db.commit()
            return existing

        cs204 = Course(
            course_code="CS204",
            name="Object-Oriented Programming & SE",
            department="Computer Science",
            semester="Fall 2026",
            lecturer_name="Prof. Boaz Ben-Moshe",
        )
        cs101 = Course(
            course_code="CS101",
            name="Algorithms & Data Structures",
            department="Computer Science",
            semester="Fall 2026",
            lecturer_name="Prof. Boaz Ben-Moshe",
        )
        self.db.add_all([cs204, cs101])
        self.db.flush()

        # Attach any existing orphan sessions to CS204
        orphans = (
            self.db.query(ClassSession).filter(ClassSession.course_id.is_(None)).all()
        )
        if orphans:
            for s in orphans:
                s.course_id = cs204.id
        else:
            s1 = ClassSession(
                course_id=cs204.id,
                session_code="ARIEL1",
                title="Lecture 1 — OOD & Architecture",
                attendance_open=True,
            )
            self.db.add(s1)

        s2 = ClassSession(
            course_id=cs101.id,
            session_code="ALGO01",
            title="Lecture 1 — Asymptotic Complexity & Graphs",
            attendance_open=True,
        )
        self.db.add(s2)
        self.db.commit()
        return [cs204, cs101]

    def get_or_create_lecturer(
        self,
        full_name: str,
        email: str,
        department: str = "Computer Science",
    ) -> Lecturer:
        """Sign in or register a Lecturer account by email."""
        norm_email = (email or "").strip().lower()
        clean_name = (full_name or "").strip()
        clean_dept = (department or "Computer Science").strip()

        if not norm_email or not EMAIL_RE.match(norm_email):
            raise CourseError("Please enter a valid university or personal email address.")

        lecturer = self.db.query(Lecturer).filter_by(email=norm_email).one_or_none()
        if lecturer is None:
            if not clean_name:
                raise CourseError("Full name is required for first-time lecturer sign-in.")
            lecturer = Lecturer(
                full_name=clean_name,
                email=norm_email,
                department=clean_dept,
            )
            self.db.add(lecturer)
            self.db.commit()
        else:
            if clean_name:
                lecturer.full_name = clean_name
            if clean_dept:
                lecturer.department = clean_dept
            self.db.commit()

        return lecturer

    def list_lecturer_courses(self, lecturer_id: Optional[int] = None) -> list[Course]:
        """List courses belonging to a specific lecturer, or all courses if unspecified."""
        if lecturer_id is not None:
            owned = (
                self.db.query(Course)
                .filter_by(lecturer_id=lecturer_id)
                .order_by(Course.id.desc())
                .all()
            )
            if owned:
                return owned
            # Attach any unowned courses if none are owned yet
            unowned = (
                self.db.query(Course)
                .filter(Course.lecturer_id.is_(None))
                .order_by(Course.id.asc())
                .all()
            )
            if unowned:
                lec = self.db.get(Lecturer, lecturer_id)
                for c in unowned:
                    c.lecturer_id = lecturer_id
                    if lec:
                        c.lecturer_name = lec.full_name
                self.db.commit()
                return unowned
            return []
        return self.ensure_default_school_courses()

    def create_course(
        self,
        course_code: str,
        name: str,
        department: str = "Computer Science",
        semester: str = "Fall 2026",
        lecturer_name: str = "Prof. Boaz Ben-Moshe",
        lecturer_id: Optional[int] = None,
        initial_lecture_title: Optional[str] = None,
    ) -> Course:
        code = (course_code or "").strip().upper()
        clean_name = (name or "").strip()
        if not code or not clean_name:
            raise CourseError("Course code and course name are required.")

        existing = self.db.query(Course).filter_by(course_code=code).one_or_none()
        if existing is not None:
            if lecturer_id is not None and existing.lecturer_id is None:
                existing.lecturer_id = lecturer_id
                self.db.commit()
            return existing

        course = Course(
            lecturer_id=lecturer_id,
            course_code=code,
            name=clean_name,
            department=(department or "Computer Science").strip(),
            semester=(semester or "Fall 2026").strip(),
            lecturer_name=(lecturer_name or "Prof. Boaz Ben-Moshe").strip(),
        )
        self.db.add(course)
        self.db.flush()

        # Automatically create an initial Lecture 1 session for the new course
        initial_code = f"{code[:4]}01"
        suffix = 1
        while (
            self.db.query(ClassSession).filter_by(session_code=initial_code).first()
            is not None
        ):
            suffix += 1
            initial_code = f"{code[:4]}{suffix:02d}"

        first_session = ClassSession(
            course_id=course.id,
            session_code=initial_code,
            title=(initial_lecture_title or f"Lecture 1 — {clean_name}").strip(),
            attendance_open=True,
        )
        self.db.add(first_session)
        self.db.commit()
        return course

    def serialize_course_summary(self, course: Course) -> dict[str, Any]:
        sessions = (
            self.db.query(ClassSession)
            .filter_by(course_id=course.id)
            .order_by(ClassSession.id.desc())
            .all()
        )
        enrollment_count = (
            self.db.query(CourseEnrollment).filter_by(course_id=course.id).count()
        )
        active_session = next((s for s in sessions if s.attendance_open), None)
        return {
            "id": course.id,
            "course_code": course.course_code,
            "name": course.name,
            "department": course.department,
            "semester": course.semester,
            "lecturer_name": course.lecturer_name,
            "session_count": len(sessions),
            "enrollment_count": enrollment_count,
            "active_session_code": (
                active_session.session_code if active_session else None
            ),
            "sessions": [
                {
                    "id": s.id,
                    "session_code": s.session_code,
                    "title": s.title,
                    "started_at": s.started_at.isoformat(),
                    "attendance_open": s.attendance_open,
                    "qr_count": self.db.query(AttendanceRecord)
                    .filter_by(session_id=s.id)
                    .count(),
                }
                for s in sessions
            ],
        }

    def get_course_overview(self, course_id: int) -> dict[str, Any]:
        course = self.db.get(Course, course_id)
        if course is None:
            raise CourseError("Course not found.", status_code=404)

        sessions = (
            self.db.query(ClassSession)
            .filter_by(course_id=course.id)
            .order_by(ClassSession.id.asc())
            .all()
        )
        session_ids = [s.id for s in sessions]
        total_sessions = len(sessions)

        enrollments = (
            self.db.query(CourseEnrollment).filter_by(course_id=course.id).all()
        )
        student_map: dict[int, Student] = {}
        for enr in enrollments:
            st = self.db.get(Student, enr.student_id)
            if st is not None:
                student_map[st.id] = st

        # Also include any student who checked into a session of this course
        if session_ids:
            recs = (
                self.db.query(AttendanceRecord)
                .filter(AttendanceRecord.session_id.in_(session_ids))
                .all()
            )
            for r in recs:
                if r.student_id not in student_map:
                    st = self.db.get(Student, r.student_id)
                    if st is not None:
                        student_map[st.id] = st

        student_rows = []
        for st_id, st in sorted(student_map.items(), key=lambda item: item[1].full_name.lower()):
            attended_count = 0
            if session_ids:
                attended_count = (
                    self.db.query(AttendanceRecord)
                    .filter(
                        AttendanceRecord.session_id.in_(session_ids),
                        AttendanceRecord.student_id == st_id,
                    )
                    .count()
                )
            rate_pct = (
                round((attended_count / total_sessions) * 100.0, 1)
                if total_sessions > 0
                else 0.0
            )
            student_rows.append(
                {
                    "student_id": st.id,
                    "full_name": st.full_name,
                    "email": st.email,
                    "attended_sessions": attended_count,
                    "total_sessions": total_sessions,
                    "attendance_rate_pct": rate_pct,
                }
            )

        summary = self.serialize_course_summary(course)
        summary["students"] = student_rows
        summary["lecture_summaries"] = LectureSummarizerService(self.db).list_course_artifacts(
            course.id
        )
        summary["assignments"] = HomeworkReviewService(self.db).list_course_assignments(
            course.id, include_submissions=True
        )
        return summary

    def enroll_student_by_code(
        self,
        course_code: str,
        full_name: str,
        email: str,
        device_id: Optional[str] = None,
    ) -> tuple[Course, Student]:
        code = (course_code or "").strip().upper()
        course = self.db.query(Course).filter_by(course_code=code).one_or_none()
        if course is None:
            # Also check if the student entered a lecture session_code instead of course_code!
            sess = (
                self.db.query(ClassSession).filter_by(session_code=code).one_or_none()
            )
            if sess is not None and sess.course_id is not None:
                course = self.db.get(Course, sess.course_id)
        if course is None:
            raise CourseError(
                f"Course or lecture code '{code}' not found.", status_code=404
            )

        norm_email = (email or "").strip().lower()
        clean_name = (full_name or "").strip()
        if not clean_name or not EMAIL_RE.match(norm_email):
            raise CourseError("Valid full name and email are required.")

        student = self.db.query(Student).filter_by(email=norm_email).one_or_none()
        if student is None:
            student = Student(
                full_name=clean_name,
                email=norm_email,
                primary_device_id=device_id,
            )
            self.db.add(student)
            self.db.flush()
        else:
            student.full_name = clean_name
            if device_id and not student.primary_device_id:
                student.primary_device_id = device_id
            self.db.flush()

        existing = (
            self.db.query(CourseEnrollment)
            .filter_by(course_id=course.id, student_id=student.id)
            .one_or_none()
        )
        if existing is None:
            self.db.add(CourseEnrollment(course_id=course.id, student_id=student.id))

        # Also add student to RosterEntry for all sessions in this course
        sessions = (
            self.db.query(ClassSession).filter_by(course_id=course.id).all()
        )
        for s in sessions:
            r_exist = (
                self.db.query(RosterEntry)
                .filter_by(session_id=s.id, email=norm_email)
                .one_or_none()
            )
            if r_exist is None:
                self.db.add(
                    RosterEntry(
                        session_id=s.id,
                        full_name=clean_name,
                        email=norm_email,
                    )
                )

        self.db.commit()
        return course, student

    def import_course_roster_csv(self, course_id: int, csv_content: str) -> int:
        """Import a CSV of (Full Name, Email) into CourseEnrollment and all course sessions."""
        course = self.db.get(Course, course_id)
        if course is None:
            raise CourseError("Course not found.", status_code=404)

        reader = csv.reader(io.StringIO(csv_content.strip()))
        count = 0
        for row in reader:
            if not row or len(row) < 2:
                continue
            name_val = row[0].strip().lstrip("\ufeff")
            email_val = row[1].strip().lower()
            if name_val.lower() in ("name", "full name", "student name") or "email" in email_val:
                continue
            if not name_val or not EMAIL_RE.match(email_val):
                continue
            self.enroll_student_by_code(
                course_code=course.course_code,
                full_name=name_val,
                email=email_val,
            )
            count += 1
        return count

    def build_student_dashboard_payload(
        self, student_id: Optional[int]
    ) -> dict[str, Any]:
        """Build the multi-course Student App state for a logged-in student."""
        self.ensure_default_school_courses()
        student = self.db.get(Student, student_id) if student_id else None

        all_courses = self.db.query(Course).order_by(Course.id.asc()).all()
        enrolled_course_ids: set[int] = set()
        if student is not None:
            enrs = (
                self.db.query(CourseEnrollment)
                .filter_by(student_id=student.id)
                .all()
            )
            enrolled_course_ids = {e.course_id for e in enrs}
            # Also include courses where the student has any AttendanceRecord
            recs = (
                self.db.query(AttendanceRecord)
                .filter_by(student_id=student.id)
                .all()
            )
            for r in recs:
                sess = self.db.get(ClassSession, r.session_id)
                if sess and sess.course_id:
                    enrolled_course_ids.add(sess.course_id)

        my_courses = []
        catalog_courses = []

        for c in all_courses:
            sessions = (
                self.db.query(ClassSession)
                .filter_by(course_id=c.id)
                .order_by(ClassSession.id.desc())
                .all()
            )
            session_ids = [s.id for s in sessions]
            records_by_session: dict[int, AttendanceRecord] = {}
            if student is not None and session_ids:
                s_recs = (
                    self.db.query(AttendanceRecord)
                    .filter(
                        AttendanceRecord.session_id.in_(session_ids),
                        AttendanceRecord.student_id == student.id,
                    )
                    .all()
                )
                records_by_session = {r.session_id: r for r in s_recs}

            attended_session_ids = set(records_by_session.keys())
            session_cards = []
            attendance_logs = []

            for s in sessions:
                rec = records_by_session.get(s.id)
                checked_in = rec is not None
                status_label = (
                    ("LATE" if rec.is_late else "PRESENT")
                    if rec is not None
                    else "ABSENT"
                )
                checked_in_time = (
                    rec.checked_in_at.strftime("%Y-%m-%d %H:%M:%S UTC")
                    if rec is not None
                    else None
                )
                s_card = {
                    "id": s.id,
                    "session_code": s.session_code,
                    "title": s.title,
                    "started_at": s.started_at.strftime("%Y-%m-%d %H:%M"),
                    "attendance_open": s.attendance_open,
                    "checked_in": checked_in,
                    "attendance_status": status_label,
                    "checked_in_at": checked_in_time,
                    "chat_count": self.db.query(ChatMessage)
                    .filter_by(session_id=s.id)
                    .count(),
                    "active_poll": self.db.query(Poll)
                    .filter_by(session_id=s.id, status="ACTIVE")
                    .count()
                    > 0,
                    "has_summary": self.db.query(LectureArtifact)
                    .filter_by(session_id=s.id, status="COMPLETED")
                    .count()
                    > 0,
                }
                session_cards.append(s_card)
                attendance_logs.append(
                    {
                        "session_id": s.id,
                        "session_code": s.session_code,
                        "lecture_title": s.title,
                        "lecture_date": s.started_at.strftime("%Y-%m-%d %H:%M"),
                        "status": status_label,
                        "checked_in_at": checked_in_time or "Not checked in",
                    }
                )

            course_card = {
                "id": c.id,
                "course_code": c.course_code,
                "name": c.name,
                "department": c.department,
                "semester": c.semester,
                "lecturer_name": c.lecturer_name,
                "is_enrolled": c.id in enrolled_course_ids,
                "attended_count": len(attended_session_ids),
                "total_sessions": len(sessions),
                "attendance_pct": (
                    round((len(attended_session_ids) / len(sessions)) * 100.0, 1)
                    if sessions
                    else 0.0
                ),
                "sessions": session_cards,
                "my_attendance_logs": attendance_logs,
                "lecture_summaries": LectureSummarizerService(self.db).list_course_artifacts(
                    c.id
                ),
                "assignments": HomeworkReviewService(self.db).list_course_assignments(
                    c.id,
                    student_id=student.id if student is not None else None,
                    include_submissions=False,
                ),
            }
            if c.id in enrolled_course_ids:
                my_courses.append(course_card)
            catalog_courses.append(course_card)

        return {
            "authenticated": student is not None,
            "student": (
                {
                    "id": student.id,
                    "full_name": student.full_name,
                    "email": student.email,
                }
                if student is not None
                else None
            ),
            "my_courses": my_courses,
            "all_courses": catalog_courses,
        }
