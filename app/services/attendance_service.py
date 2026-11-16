from __future__ import annotations

import csv
from datetime import datetime, timedelta, timezone
import io
from pathlib import Path
import re
import secrets
from typing import Any, Optional

from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.models import (
    AttendanceRecord,
    ClassSession,
    Course,
    CourseEnrollment,
    HeadcountSnapshot,
    RosterEntry,
    Student,
)
from app.services.identity_helpers import parse_roster_csv_rows, resolve_or_upsert_student
from app.services.qr_service import DeviceBindingManager, RollingQRService
from app.services.reconciler import AttendanceReconciler, AuditVerdict


class AttendanceError(Exception):
    """Base domain error for attendance workflows with HTTP status code."""

    def __init__(self, message: str, status_code: int = 400) -> None:
        super().__init__(message)
        self.message = message
        self.status_code = status_code


class QRExpiredError(AttendanceError):
    def __init__(
        self,
        message: str = "QR code expired or invalid. Please re-scan the live projector screen.",
    ) -> None:
        super().__init__(message, status_code=410)


class ProxyAttendanceBlockedError(AttendanceError):
    def __init__(
        self,
        message: str = "Proxy attendance blocked: this device has already checked in another student for this session.",
    ) -> None:
        super().__init__(message, status_code=409)


class SessionClosedError(AttendanceError):
    def __init__(
        self, message: str = "Attendance check-in is closed for this session."
    ) -> None:
        super().__init__(message, status_code=403)


class AttendanceService:
    """Manages session creation, QR check-in, onboarding, anti-proxy rules, and rosters."""

    def __init__(
        self,
        db: Session,
        qr_service: RollingQRService,
        device_mgr: DeviceBindingManager,
        reconciler: Optional[AttendanceReconciler] = None,
    ) -> None:
        self.db = db
        self.qr_service = qr_service
        self.device_mgr = device_mgr
        self.reconciler = reconciler or AttendanceReconciler()

    def create_session(
        self,
        title: str,
        session_code: Optional[str] = None,
        headcount_tolerance: int = 2,
        late_after_minutes: int = 15,
        course_id: Optional[int] = None,
    ) -> ClassSession:
        """Create a new classroom session with a unique 6-character code."""
        clean_title = (title or "Classroom Lecture").strip()
        code = (
            session_code.strip().upper()
            if session_code
            else secrets.token_hex(3).upper()
        )
        existing = (
            self.db.query(ClassSession).filter_by(session_code=code).one_or_none()
        )
        if existing is not None:
            if course_id is not None and existing.course_id is None:
                existing.course_id = course_id
                self.db.commit()
            return existing

        session = ClassSession(
            course_id=course_id,
            session_code=code,
            title=clean_title,
            attendance_open=True,
            headcount_tolerance=headcount_tolerance,
            late_after_minutes=late_after_minutes,
        )
        self.db.add(session)
        self.db.commit()
        return session

    def get_or_create_default_session(self) -> ClassSession:
        """Return the latest active session or create a default demo course & session."""
        latest = (
            self.db.query(ClassSession).order_by(ClassSession.id.desc()).first()
        )
        if latest is not None:
            if latest.course_id is None:
                default_course = (
                    self.db.query(Course).filter_by(course_code="CS204").one_or_none()
                )
                if default_course is None:
                    default_course = Course(
                        course_code="CS204",
                        name="Object-Oriented Programming & SE",
                        department="Computer Science",
                        semester="Fall 2026",
                        lecturer_name="Prof. Boaz Ben-Moshe",
                    )
                    self.db.add(default_course)
                    self.db.flush()
                latest.course_id = default_course.id
                self.db.commit()
            return latest

        default_course = (
            self.db.query(Course).filter_by(course_code="CS204").one_or_none()
        )
        if default_course is None:
            default_course = Course(
                course_code="CS204",
                name="Object-Oriented Programming & SE",
                department="Computer Science",
                semester="Fall 2026",
                lecturer_name="Prof. Boaz Ben-Moshe",
            )
            self.db.add(default_course)
            self.db.commit()

        return self.create_session(
            title="Lecture 1 — OOP & Software Engineering",
            session_code="ARIEL1",
            course_id=default_course.id,
        )

    def process_checkin(
        self,
        session_code: str,
        bucket: Optional[int],
        token: Optional[str],
        full_name: Optional[str],
        email: Optional[str],
        device_id: str,
        student_id_number: Optional[str] = None,
        existing_student_id: Optional[int] = None,
        onboarding_grant: Optional[str] = None,
        now_ts: Optional[float] = None,
    ) -> tuple[AttendanceRecord, Student, bool]:
        """Validate rotating QR (or onboarding grant), bind student/device, and record attendance.

        Returns (AttendanceRecord, Student, is_new_checkin).
        """
        session = (
            self.db.query(ClassSession)
            .filter_by(session_code=session_code)
            .one_or_none()
        )
        if session is None:
            raise AttendanceError("Session not found.", status_code=404)
        if not session.attendance_open:
            raise SessionClosedError()

        verified_bucket_str = ""
        if onboarding_grant:
            grant_data = self.qr_service.verify_onboarding_grant(
                onboarding_grant, session_code
            )
            if grant_data is None:
                raise QRExpiredError(
                    "Registration window expired. Please scan the live QR code again."
                )
            verified_bucket_str = str(grant_data.get("bucket", "grant"))
        else:
            if bucket is None or not token:
                raise QRExpiredError()
            if not self.qr_service.verify_token(
                session_code, token, int(bucket), now_ts=now_ts
            ):
                raise QRExpiredError()
            verified_bucket_str = str(bucket)

        # 1. Check if this device_id already checked in during this session
        device_record = (
            self.db.query(AttendanceRecord)
            .filter_by(session_id=session.id, device_id=device_id)
            .one_or_none()
        )
        if device_record is not None:
            bound_student = self.db.get(Student, device_record.student_id)
            norm_email = (email or "").strip().lower()
            if bound_student is not None:
                if norm_email and norm_email != bound_student.email:
                    raise ProxyAttendanceBlockedError()
                if (
                    (existing_student_id and existing_student_id == bound_student.id)
                    or (norm_email and norm_email == bound_student.email)
                    or (not norm_email and not full_name)
                ):
                    return device_record, bound_student, False
            raise ProxyAttendanceBlockedError()

        # 2. Resolve Student (by existing cookie student_id or by Name + Email + optional Student ID)
        student: Optional[Student] = None
        norm_email = (email or "").strip().lower()
        clean_name = (full_name or "").strip()

        if existing_student_id and not norm_email:
            student = self.db.get(Student, existing_student_id)

        if student is None:
            resolved_student, err = resolve_or_upsert_student(
                self.db,
                full_name=clean_name,
                email=norm_email,
                student_id_number=student_id_number,
                device_id=device_id,
            )
            if err is not None:
                raise AttendanceError(err[0], status_code=err[1])
            student = resolved_student

        # 3. Check if this student already checked in during this session (e.g. from another device)
        student_record = (
            self.db.query(AttendanceRecord)
            .filter_by(session_id=session.id, student_id=student.id)
            .one_or_none()
        )
        if student_record is not None:
            return student_record, student, False

        # 4. Compute tardiness flag and insert AttendanceRecord
        now_dt = (
            datetime.fromtimestamp(now_ts, tz=timezone.utc)
            if now_ts is not None
            else datetime.now(timezone.utc)
        )
        started_dt = session.started_at
        if started_dt.tzinfo is None:
            started_dt = started_dt.replace(tzinfo=timezone.utc)
        elapsed_min = (now_dt - started_dt).total_seconds() / 60.0
        is_late = elapsed_min > float(session.late_after_minutes)

        record = AttendanceRecord(
            session_id=session.id,
            student_id=student.id,
            device_id=device_id,
            checked_in_at=now_dt,
            is_late=is_late,
            token_bucket=verified_bucket_str,
        )
        self.db.add(record)

        # Auto-enroll student in the parent Course when checking into any lecture session
        if session.course_id is not None:
            existing_enrollment = (
                self.db.query(CourseEnrollment)
                .filter_by(course_id=session.course_id, student_id=student.id)
                .one_or_none()
            )
            if existing_enrollment is None:
                self.db.add(
                    CourseEnrollment(
                        course_id=session.course_id,
                        student_id=student.id,
                    )
                )

        try:
            self.db.commit()
        except IntegrityError as exc:
            self.db.rollback()
            raise ProxyAttendanceBlockedError() from exc

        return record, student, True

    def import_roster_csv(self, session_id: int, csv_content: str) -> int:
        """Parse CSV rows into RosterEntry records supporting (Name, Email) and optional Student ID."""
        imported = 0
        for sid_val, name_val, email_val in parse_roster_csv_rows(csv_content):
            existing = (
                self.db.query(RosterEntry)
                .filter_by(session_id=session_id, email=email_val)
                .one_or_none()
            )
            if existing is None:
                self.db.add(
                    RosterEntry(
                        session_id=session_id,
                        student_id_number=sid_val,
                        full_name=name_val,
                        email=email_val,
                    )
                )
                imported += 1
            else:
                existing.full_name = name_val
                if sid_val is not None:
                    existing.student_id_number = sid_val
        self.db.commit()
        return imported

    def get_attendance_rows(self, session_id: int) -> list[dict[str, Any]]:
        """Build merged roster + QR attendance table rows for UI and Excel export."""
        roster_list = (
            self.db.query(RosterEntry)
            .filter_by(session_id=session_id)
            .order_by(RosterEntry.full_name.asc())
            .all()
        )
        records = (
            self.db.query(AttendanceRecord)
            .filter_by(session_id=session_id)
            .order_by(AttendanceRecord.checked_in_at.asc())
            .all()
        )

        rec_by_email: dict[str, tuple[AttendanceRecord, Student]] = {}
        for rec in records:
            st = self.db.get(Student, rec.student_id)
            if st is not None:
                rec_by_email[st.email.lower()] = (rec, st)

        rows: list[dict[str, Any]] = []
        seen_emails: set[str] = set()

        for r_entry in roster_list:
            em = r_entry.email.lower()
            seen_emails.add(em)
            matched = rec_by_email.get(em)
            if matched is not None:
                rec, st = matched
                rows.append(
                    {
                        "student_id": st.id,
                        "student_id_number": st.student_id_number or r_entry.student_id_number or "-",
                        "full_name": st.full_name,
                        "email": st.email,
                        "roster_status": "Registered",
                        "attendance_status": "Present",
                        "checked_in_at": rec.checked_in_at.strftime("%Y-%m-%d %H:%M:%S UTC"),
                        "is_late": rec.is_late,
                        "device_hash": rec.device_id[:8],
                    }
                )
            else:
                rows.append(
                    {
                        "student_id": None,
                        "student_id_number": r_entry.student_id_number or "-",
                        "full_name": r_entry.full_name,
                        "email": r_entry.email,
                        "roster_status": "Registered",
                        "attendance_status": "Absent",
                        "checked_in_at": "-",
                        "is_late": False,
                        "device_hash": "-",
                    }
                )

        # Include checked-in students who were not pre-listed on the optional roster
        for em, (rec, st) in rec_by_email.items():
            if em not in seen_emails:
                rows.append(
                    {
                        "student_id": st.id,
                        "student_id_number": st.student_id_number or "-",
                        "full_name": st.full_name,
                        "email": st.email,
                        "roster_status": "Walk-In / Unlisted" if roster_list else "Registered (Self)",
                        "attendance_status": "Present",
                        "checked_in_at": rec.checked_in_at.strftime("%Y-%m-%d %H:%M:%S UTC"),
                        "is_late": rec.is_late,
                        "device_hash": rec.device_id[:8],
                    }
                )

        for idx, row_item in enumerate(rows, start=1):
            row_item["index"] = idx

        return rows

    def get_audit_summary(self, session_id: int) -> AuditVerdict:
        """Compute current cross-reference AuditVerdict between Roster, QR, and CV Headcount."""
        session = self.db.get(ClassSession, session_id)
        tolerance = session.headcount_tolerance if session else 2
        roster_total = (
            self.db.query(RosterEntry).filter_by(session_id=session_id).count()
        )
        qr_count = (
            self.db.query(AttendanceRecord).filter_by(session_id=session_id).count()
        )
        latest_snap = (
            self.db.query(HeadcountSnapshot)
            .filter_by(session_id=session_id)
            .order_by(HeadcountSnapshot.id.desc())
            .first()
        )
        visual_count = latest_snap.verified_face_count if latest_snap else 0
        return self.reconciler.reconcile(
            roster_total=roster_total,
            qr_count=qr_count,
            visual_count=visual_count,
            tolerance=tolerance,
            has_snapshot=latest_snap is not None,
        )

    def purge_expired_headcount_photos(self, retention_hours: int) -> int:
        """Delete classroom headcount image files older than retention_hours while preserving numeric counts."""
        if retention_hours <= 0:
            return 0
        cutoff = datetime.now(timezone.utc) - timedelta(hours=retention_hours)
        snaps = self.db.query(HeadcountSnapshot).all()
        deleted_files = 0
        for snap in snaps:
            created = snap.captured_at
            if created.tzinfo is None:
                created = created.replace(tzinfo=timezone.utc)
            if created >= cutoff:
                continue
            for p_str in (snap.raw_image_path, snap.annotated_image_path):
                if p_str:
                    p = Path(p_str)
                    if p.exists() and p.is_file():
                        try:
                            p.unlink()
                            deleted_files += 1
                        except OSError:
                            pass
        return deleted_files
