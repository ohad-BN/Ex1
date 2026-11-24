from __future__ import annotations

from datetime import datetime, timedelta, timezone
import hashlib
import re
import secrets
from typing import Any, Optional

from sqlalchemy import func
from sqlalchemy.orm import Session
from werkzeug.security import check_password_hash, generate_password_hash

from app.models import (
    Assignment,
    AttendanceRecord,
    AuditLog,
    ChatMessage,
    ClassSession,
    Course,
    CourseEnrollment,
    CourseStaff,
    HomeworkSubmission,
    LectureArtifact,
    Lecturer,
    PasswordResetToken,
    Poll,
    RosterEntry,
    Student,
)
from app.services.homework_pipeline import HomeworkReviewService
from app.services.identity_helpers import (
    EMAIL_RE,
    RESERVED_USERNAMES,
    USERNAME_RE,
    generate_deterministic_session_code,
    is_synthetic_student_email,
    normalize_student_id,
    parse_roster_csv_rows,
    record_audit_event,
    resolve_or_upsert_student,
)
from app.services.lecture_pipeline import LectureSummarizerService

MAX_LOGIN_FAILURES = 5
LOCKOUT_MINUTES = 15
RESET_TOKEN_MINUTES = 15


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
        """Ensure at least two demo school courses exist (owned by the default demo lecturer)."""
        demo_lecturer = self.db.query(Lecturer).filter_by(email="boaz@ariel.ac.il").one_or_none()
        if demo_lecturer is None:
            demo_lecturer = Lecturer(
                username="boaz",
                full_name="Prof. Boaz Ben-Moshe",
                email="boaz@ariel.ac.il",
                password_hash=generate_password_hash("ClassPulse2026!", method="scrypt"),
                department="Computer Science",
                role="lecturer",
            )
            self.db.add(demo_lecturer)
            self.db.commit()
        else:
            updated = False
            if not demo_lecturer.username:
                demo_lecturer.username = "boaz"
                updated = True
            if not demo_lecturer.password_hash:
                demo_lecturer.password_hash = generate_password_hash("ClassPulse2026!", method="scrypt")
                updated = True
            if updated:
                self.db.commit()
        existing = self.db.query(Course).order_by(Course.id.asc()).all()
        if existing:
            changed = False
            for c in existing:
                if c.lecturer_id is None and c.course_code in ("CS204", "CS101"):
                    c.lecturer_id = demo_lecturer.id
                    changed = True
            if changed:
                self.db.commit()
            return existing

        cs204 = Course(
            lecturer_id=demo_lecturer.id,
            course_code="CS204",
            name="Object-Oriented Programming & SE",
            department="Computer Science",
            semester="Fall 2026",
            lecturer_name=demo_lecturer.full_name,
        )
        cs101 = Course(
            lecturer_id=demo_lecturer.id,
            course_code="CS101",
            name="Algorithms & Data Structures",
            department="Computer Science",
            semester="Fall 2026",
            lecturer_name=demo_lecturer.full_name,
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
                lecture_number=1,
                session_code="ARIEL1",
                title="Lecture 1 — OOD & Architecture",
                attendance_open=True,
            )
            self.db.add(s1)

        s2 = ClassSession(
            course_id=cs101.id,
            lecture_number=1,
            session_code="ALGO01",
            title="Lecture 1 — Asymptotic Complexity & Graphs",
            attendance_open=True,
        )
        self.db.add(s2)
        self.db.commit()
        return [cs204, cs101]

    @staticmethod
    def _validate_password_strength(password: str) -> None:
        pw = password or ""
        if len(pw) < 8:
            raise CourseError("Password must be at least 8 characters long.", status_code=400)
        has_alpha = any(ch.isalpha() for ch in pw)
        has_num_or_sym = any(ch.isdigit() or not ch.isalnum() for ch in pw)
        if not (has_alpha and has_num_or_sym):
            raise CourseError(
                "Password must contain at least one letter and one number or symbol.",
                status_code=400,
            )

    @staticmethod
    def _normalize_username(username: str) -> str:
        clean = (username or "").strip().lower()
        if clean.startswith("@"):
            clean = clean[1:]
        if not USERNAME_RE.match(clean):
            raise CourseError(
                "Username must be 3-32 characters using lowercase letters, digits, '.', '_' or '-'.",
                status_code=400,
            )
        if clean in RESERVED_USERNAMES:
            raise CourseError(f"Username '{clean}' is reserved.", status_code=400)
        return clean

    def _derive_unique_username_from_email(self, email: str) -> str:
        local = (email.split("@", 1)[0] if "@" in email else email).strip().lower()
        base = re.sub(r"[^a-z0-9._-]", "", local).strip("._-")
        if len(base) < 3:
            base = f"{base or 'prof'}_u"
        if not base[0].isalnum():
            base = f"u{base}"
        base = base[:26]
        if base in RESERVED_USERNAMES:
            base = f"{base}_lec"
        candidate = base
        idx = 2
        while self.db.query(Lecturer).filter(func.lower(Lecturer.username) == candidate).first() is not None:
            candidate = f"{base}{idx}"
            idx += 1
        return candidate

    def register_lecturer(
        self,
        *,
        full_name: str,
        username: str = "",
        email: str,
        password: str,
        confirm_password: Optional[str] = None,
        department: str = "Computer Science",
        ip_address: Optional[str] = None,
    ) -> Lecturer:
        """Create a new Lecturer account with unique username, email, and scrypt password hash."""
        clean_name = (full_name or "").strip()
        norm_email = (email or "").strip().lower()
        clean_dept = (department or "Computer Science").strip()

        if not clean_name:
            raise CourseError("Full name is required.", status_code=400)
        if not norm_email or not EMAIL_RE.match(norm_email):
            raise CourseError("Please enter a valid university or personal email address.", status_code=400)
        raw_username = (username or "").strip()
        norm_username = (
            self._normalize_username(raw_username)
            if raw_username
            else self._derive_unique_username_from_email(norm_email)
        )

        if confirm_password is not None and password != confirm_password:
            raise CourseError("Passwords do not match.", status_code=400)
        self._validate_password_strength(password)
        if password.lower() in (norm_username, norm_email):
            raise CourseError("Password cannot be the same as your username or email.", status_code=400)

        existing_by_email = (
            self.db.query(Lecturer)
            .filter(func.lower(Lecturer.email) == norm_email)
            .order_by(Lecturer.id.asc())
            .first()
        )
        if existing_by_email is not None:
            if existing_by_email.password_hash:
                raise CourseError("An account with this email already exists.", status_code=409)
            username_conflict = (
                self.db.query(Lecturer)
                .filter(
                    func.lower(Lecturer.username) == norm_username,
                    Lecturer.id != existing_by_email.id,
                )
                .first()
            )
            if username_conflict is not None:
                raise CourseError(f"Username '@{norm_username}' is already taken.", status_code=409)
            existing_by_email.username = norm_username
            existing_by_email.full_name = clean_name
            existing_by_email.department = clean_dept
            existing_by_email.password_hash = generate_password_hash(password, method="scrypt")
            existing_by_email.is_active = True
            existing_by_email.failed_login_attempts = 0
            existing_by_email.locked_until = None
            existing_by_email.last_login_at = datetime.now(timezone.utc)
            record_audit_event(
                self.db,
                actor_type="lecturer",
                actor_id=existing_by_email.id,
                action="lecturer.legacy_account_activated",
                target_type="lecturer",
                target_id=existing_by_email.id,
                detail=f"Activated @{norm_username} ({norm_email})",
                ip_address=ip_address,
            )
            self.db.commit()
            return existing_by_email

        if self.db.query(Lecturer).filter(func.lower(Lecturer.username) == norm_username).first():
            raise CourseError(f"Username '@{norm_username}' is already taken.", status_code=409)

        lecturer = Lecturer(
            username=norm_username,
            full_name=clean_name,
            email=norm_email,
            password_hash=generate_password_hash(password, method="scrypt"),
            department=clean_dept,
            role="lecturer",
            is_active=True,
            token_version=1,
            last_login_at=datetime.now(timezone.utc),
        )
        self.db.add(lecturer)
        self.db.flush()
        record_audit_event(
            self.db,
            actor_type="lecturer",
            actor_id=lecturer.id,
            action="lecturer.register",
            target_type="lecturer",
            target_id=lecturer.id,
            detail=f"Registered @{norm_username} ({norm_email})",
            ip_address=ip_address,
        )
        self.db.commit()
        return lecturer

    def authenticate_lecturer(
        self,
        *,
        identifier: str,
        password: str,
        ip_address: Optional[str] = None,
    ) -> Lecturer:
        """Verify username-or-email + password with brute-force lockout protection."""
        clean_id = (identifier or "").strip().lower()
        if clean_id.startswith("@"):
            clean_id = clean_id[1:]
        if not clean_id or not password:
            raise CourseError("Username/email and password are required.", status_code=400)

        lecturer = (
            self.db.query(Lecturer)
            .filter(
                (func.lower(Lecturer.email) == clean_id)
                | (func.lower(Lecturer.username) == clean_id)
            )
            .order_by(Lecturer.id.asc())
            .first()
        )
        if lecturer is None or not lecturer.is_active:
            record_audit_event(
                self.db,
                actor_type="anonymous",
                action="lecturer.login_failed",
                detail=f"Unknown or inactive identifier: {clean_id[:80]}",
                ip_address=ip_address,
            )
            self.db.commit()
            raise CourseError("Invalid username/email or password.", status_code=401)

        if not lecturer.password_hash:
            record_audit_event(
                self.db,
                actor_type="lecturer",
                actor_id=lecturer.id,
                action="lecturer.login_legacy_unclaimed",
                target_type="lecturer",
                target_id=lecturer.id,
                ip_address=ip_address,
            )
            self.db.commit()
            raise CourseError(
                "This legacy account does not have a password set yet. Switch to 'Create Account' with this email to activate your password and keep your courses.",
                status_code=401,
            )

        now_dt = datetime.now(timezone.utc)
        if lecturer.locked_until is not None:
            locked_dt = lecturer.locked_until
            if locked_dt.tzinfo is None:
                locked_dt = locked_dt.replace(tzinfo=timezone.utc)
            if locked_dt > now_dt:
                rem_min = max(1, int((locked_dt - now_dt).total_seconds() // 60) + 1)
                raise CourseError(
                    f"Account temporarily locked after failed logins. Try again in {rem_min} minute(s).",
                    status_code=429,
                )
            lecturer.locked_until = None
            lecturer.failed_login_attempts = 0

        if not check_password_hash(lecturer.password_hash, password):
            lecturer.failed_login_attempts = int(lecturer.failed_login_attempts or 0) + 1
            if lecturer.failed_login_attempts >= MAX_LOGIN_FAILURES:
                lecturer.locked_until = now_dt + timedelta(minutes=LOCKOUT_MINUTES)
            record_audit_event(
                self.db,
                actor_type="lecturer",
                actor_id=lecturer.id,
                action="lecturer.login_failed",
                target_type="lecturer",
                target_id=lecturer.id,
                detail=f"Attempt #{lecturer.failed_login_attempts}",
                ip_address=ip_address,
            )
            self.db.commit()
            raise CourseError("Invalid username/email or password.", status_code=401)

        lecturer.failed_login_attempts = 0
        lecturer.locked_until = None
        lecturer.last_login_at = now_dt
        record_audit_event(
            self.db,
            actor_type="lecturer",
            actor_id=lecturer.id,
            action="lecturer.login_success",
            target_type="lecturer",
            target_id=lecturer.id,
            ip_address=ip_address,
        )
        self.db.commit()
        return lecturer

    def invalidate_all_lecturer_sessions(
        self, lecturer_id: int, ip_address: Optional[str] = None
    ) -> int:
        """Increment token_version so every active browser cookie for this lecturer is revoked."""
        lecturer = self.db.get(Lecturer, lecturer_id)
        if lecturer is None:
            raise CourseError("Lecturer not found.", status_code=404)
        lecturer.token_version = int(lecturer.token_version or 1) + 1
        record_audit_event(
            self.db,
            actor_type="lecturer",
            actor_id=lecturer.id,
            action="lecturer.logout_all",
            target_type="lecturer",
            target_id=lecturer.id,
            ip_address=ip_address,
        )
        self.db.commit()
        return lecturer.token_version

    def change_lecturer_password(
        self,
        lecturer_id: int,
        current_password: str,
        new_password: str,
        confirm_password: Optional[str] = None,
        ip_address: Optional[str] = None,
    ) -> Lecturer:
        lecturer = self.db.get(Lecturer, lecturer_id)
        if lecturer is None:
            raise CourseError("Lecturer not found.", status_code=404)
        if not lecturer.password_hash or not check_password_hash(
            lecturer.password_hash, current_password or ""
        ):
            raise CourseError("Current password is incorrect.", status_code=401)
        if confirm_password is not None and new_password != confirm_password:
            raise CourseError("New passwords do not match.", status_code=400)
        self._validate_password_strength(new_password)
        lecturer.password_hash = generate_password_hash(new_password, method="scrypt")
        lecturer.token_version = int(lecturer.token_version or 1) + 1
        record_audit_event(
            self.db,
            actor_type="lecturer",
            actor_id=lecturer.id,
            action="lecturer.password_changed",
            target_type="lecturer",
            target_id=lecturer.id,
            ip_address=ip_address,
        )
        self.db.commit()
        return lecturer

    def create_password_reset_token(
        self, identifier: str, ip_address: Optional[str] = None
    ) -> Optional[str]:
        """Issue a 15-min hashed reset token if the lecturer exists; returns raw token or None."""
        clean_id = (identifier or "").strip().lower()
        if clean_id.startswith("@"):
            clean_id = clean_id[1:]
        if not clean_id:
            return None
        lecturer = (
            self.db.query(Lecturer)
            .filter(
                (func.lower(Lecturer.email) == clean_id)
                | (func.lower(Lecturer.username) == clean_id)
            )
            .one_or_none()
        )
        if lecturer is None or not lecturer.is_active:
            return None

        raw_token = secrets.token_urlsafe(32)
        tok_hash = hashlib.sha256(raw_token.encode("utf-8")).hexdigest()
        expires = datetime.now(timezone.utc) + timedelta(minutes=RESET_TOKEN_MINUTES)
        self.db.add(
            PasswordResetToken(
                lecturer_id=lecturer.id,
                token_hash=tok_hash,
                expires_at=expires,
            )
        )
        record_audit_event(
            self.db,
            actor_type="lecturer",
            actor_id=lecturer.id,
            action="lecturer.password_reset_requested",
            target_type="lecturer",
            target_id=lecturer.id,
            ip_address=ip_address,
        )
        self.db.commit()
        return raw_token

    def reset_password_with_token(
        self,
        raw_token: str,
        new_password: str,
        confirm_password: Optional[str] = None,
        ip_address: Optional[str] = None,
    ) -> Lecturer:
        if not raw_token:
            raise CourseError("Reset token is required.", status_code=400)
        if confirm_password is not None and new_password != confirm_password:
            raise CourseError("Passwords do not match.", status_code=400)
        self._validate_password_strength(new_password)

        tok_hash = hashlib.sha256(raw_token.strip().encode("utf-8")).hexdigest()
        rec = (
            self.db.query(PasswordResetToken)
            .filter_by(token_hash=tok_hash)
            .one_or_none()
        )
        if rec is None or rec.used_at is not None:
            raise CourseError("Reset token is invalid or has already been used.", status_code=400)

        now_dt = datetime.now(timezone.utc)
        exp_dt = rec.expires_at
        if exp_dt.tzinfo is None:
            exp_dt = exp_dt.replace(tzinfo=timezone.utc)
        if exp_dt < now_dt:
            raise CourseError("Reset token has expired. Please request a new one.", status_code=400)

        lecturer = self.db.get(Lecturer, rec.lecturer_id)
        if lecturer is None:
            raise CourseError("Lecturer account not found.", status_code=404)

        rec.used_at = now_dt
        lecturer.password_hash = generate_password_hash(new_password, method="scrypt")
        lecturer.failed_login_attempts = 0
        lecturer.locked_until = None
        lecturer.token_version = int(lecturer.token_version or 1) + 1
        record_audit_event(
            self.db,
            actor_type="lecturer",
            actor_id=lecturer.id,
            action="lecturer.password_reset_completed",
            target_type="lecturer",
            target_id=lecturer.id,
            ip_address=ip_address,
        )
        self.db.commit()
        return lecturer

    def update_lecturer_profile(
        self,
        lecturer_id: int,
        full_name: Optional[str] = None,
        department: Optional[str] = None,
    ) -> Lecturer:
        lecturer = self.db.get(Lecturer, lecturer_id)
        if lecturer is None:
            raise CourseError("Lecturer not found.", status_code=404)
        if full_name is not None and full_name.strip():
            lecturer.full_name = full_name.strip()
        if department is not None and department.strip():
            lecturer.department = department.strip()
        self.db.commit()
        return lecturer

    def _generate_unique_join_code(self, course_code: str) -> str:
        prefix = "".join(ch for ch in course_code.upper() if ch.isalnum())[:6] or "CLS"
        for _ in range(12):
            candidate = f"{prefix}-{secrets.token_hex(2).upper()}"
            if self.db.query(Course).filter_by(join_code=candidate).first() is None:
                return candidate
        return f"{prefix}-{secrets.token_hex(3).upper()}"

    def is_course_staff(self, course: Course, lecturer_id: int) -> bool:
        if course.lecturer_id == lecturer_id:
            return True
        return (
            self.db.query(CourseStaff)
            .filter_by(course_id=course.id, lecturer_id=lecturer_id)
            .first()
            is not None
        )

    def list_course_staff(self, course_id: int) -> list[dict[str, Any]]:
        rows = self.db.query(CourseStaff).filter_by(course_id=course_id).all()
        staff: list[dict[str, Any]] = []
        for row in rows:
            lec = self.db.get(Lecturer, row.lecturer_id)
            if lec is None:
                continue
            staff.append(
                {
                    "lecturer_id": lec.id,
                    "full_name": lec.full_name,
                    "username": lec.username,
                    "email": lec.email,
                    "role": row.role,
                }
            )
        return staff

    def add_course_staff(
        self,
        course_id: int,
        identifier: str,
        role: str,
        actor_lecturer_id: int,
        ip_address: Optional[str] = None,
    ) -> dict[str, Any]:
        course = self.db.get(Course, course_id)
        if course is None:
            raise CourseError("Course not found.", status_code=404)
        if course.lecturer_id != actor_lecturer_id:
            raise CourseError("Only the primary course owner can manage course staff.", status_code=403)

        clean_id = (identifier or "").strip().lower()
        if clean_id.startswith("@"):
            clean_id = clean_id[1:]
        target = (
            self.db.query(Lecturer)
            .filter(
                (func.lower(Lecturer.email) == clean_id)
                | (func.lower(Lecturer.username) == clean_id)
            )
            .one_or_none()
        )
        if target is None:
            raise CourseError("No registered professor/TA found with that username or email.", status_code=404)
        if target.id == course.lecturer_id:
            raise CourseError("The primary owner already has full access to this course.", status_code=400)

        norm_role = role if role in ("ta", "co_lecturer") else "ta"
        existing = (
            self.db.query(CourseStaff)
            .filter_by(course_id=course_id, lecturer_id=target.id)
            .one_or_none()
        )
        if existing is None:
            self.db.add(CourseStaff(course_id=course_id, lecturer_id=target.id, role=norm_role))
        else:
            existing.role = norm_role
        record_audit_event(
            self.db,
            actor_type="lecturer",
            actor_id=actor_lecturer_id,
            action="course.staff_added",
            target_type="course",
            target_id=course_id,
            detail=f"Added lecturer_id={target.id} as {norm_role}",
            ip_address=ip_address,
        )
        self.db.commit()
        return {
            "lecturer_id": target.id,
            "full_name": target.full_name,
            "username": target.username,
            "email": target.email,
            "role": norm_role,
        }

    def remove_course_staff(
        self,
        course_id: int,
        staff_lecturer_id: int,
        actor_lecturer_id: int,
        ip_address: Optional[str] = None,
    ) -> None:
        course = self.db.get(Course, course_id)
        if course is None:
            raise CourseError("Course not found.", status_code=404)
        if course.lecturer_id != actor_lecturer_id:
            raise CourseError("Only the primary course owner can remove course staff.", status_code=403)
        self.db.query(CourseStaff).filter_by(
            course_id=course_id, lecturer_id=staff_lecturer_id
        ).delete()
        record_audit_event(
            self.db,
            actor_type="lecturer",
            actor_id=actor_lecturer_id,
            action="course.staff_removed",
            target_type="course",
            target_id=course_id,
            detail=f"Removed lecturer_id={staff_lecturer_id}",
            ip_address=ip_address,
        )
        self.db.commit()

    def list_lecturer_audit_logs(self, lecturer_id: int, limit: int = 30) -> list[dict[str, Any]]:
        rows = (
            self.db.query(AuditLog)
            .filter_by(actor_type="lecturer", actor_id=lecturer_id)
            .order_by(AuditLog.id.desc())
            .limit(max(1, min(limit, 100)))
            .all()
        )
        return [
            {
                "id": r.id,
                "action": r.action,
                "target_type": r.target_type,
                "target_id": r.target_id,
                "detail": r.detail,
                "ip_address": r.ip_address,
                "created_at": r.created_at.isoformat(),
            }
            for r in rows
        ]

    def list_lecturer_courses(self, lecturer_id: Optional[int] = None) -> list[Course]:
        """List courses owned by or co-staffed with the given lecturer."""
        if lecturer_id is not None:
            staff_cids = [
                r.course_id
                for r in self.db.query(CourseStaff).filter_by(lecturer_id=lecturer_id).all()
            ]
            query = self.db.query(Course)
            if staff_cids:
                query = query.filter(
                    (Course.lecturer_id == lecturer_id) | (Course.id.in_(staff_cids))
                )
            else:
                query = query.filter_by(lecturer_id=lecturer_id)
            return query.order_by(Course.id.desc()).all()
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
            raise CourseError("Course code and course name are required.", status_code=400)
        if len(code) > 32:
            raise CourseError("Course code must be 32 characters or fewer.", status_code=400)

        existing = self.db.query(Course).filter_by(course_code=code).one_or_none()
        if existing is not None:
            if lecturer_id is not None:
                raise CourseError(
                    f"Course code '{code}' already exists. Please choose a unique course number.",
                    status_code=409,
                )
            return existing

        course = Course(
            lecturer_id=lecturer_id,
            course_code=code,
            join_code=self._generate_unique_join_code(code),
            name=clean_name,
            department=(department or "Computer Science").strip(),
            semester=(semester or "Fall 2026").strip(),
            lecturer_name=(lecturer_name or "Prof. Boaz Ben-Moshe").strip(),
        )
        self.db.add(course)
        self.db.flush()

        # Automatically create an initial Lecture 1 session for the new course
        initial_code = generate_deterministic_session_code(self.db, code, 1)

        first_session = ClassSession(
            course_id=course.id,
            lecture_number=1,
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
            "join_code": course.join_code or course.course_code,
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
                    "lecture_number": s.lecture_number,
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
                    "student_id_number": st.student_id_number or "-",
                    "full_name": st.full_name,
                    "email": "-" if is_synthetic_student_email(st.email) else st.email,
                    "device_bound": bool(st.primary_device_id),
                    "attended_sessions": attended_count,
                    "total_sessions": total_sessions,
                    "attendance_rate_pct": rate_pct,
                }
            )

        summary = self.serialize_course_summary(course)
        summary["students"] = student_rows
        summary["staff"] = self.list_course_staff(course.id)
        summary["lecture_summaries"] = LectureSummarizerService(self.db).list_course_artifacts(
            course.id
        )
        summary["assignments"] = HomeworkReviewService(self.db).list_course_assignments(
            course.id, include_submissions=True
        )
        return summary

    def enroll_student_by_code(
        self,
        course_code: str = "",
        full_name: str = "",
        email: str = "",
        student_id_number: Optional[str] = None,
        device_id: Optional[str] = None,
        course_id: Optional[int] = None,
    ) -> tuple[Course, Student]:
        course = None
        if course_id is not None:
            course = self.db.get(Course, int(course_id))
            if course is None:
                raise CourseError("Course not found.", status_code=404)
        else:
            code = (course_code or "").strip().upper()
            course = (
                self.db.query(Course)
                .filter((Course.course_code == code) | (Course.join_code == code))
                .one_or_none()
            )
            if course is None:
                # Also check if the student entered a lecture session_code instead of course_code
                sess = (
                    self.db.query(ClassSession).filter_by(session_code=code).one_or_none()
                )
                if sess is not None and sess.course_id is not None:
                    course = self.db.get(Course, sess.course_id)
            if course is None:
                raise CourseError(
                    f"Course or lecture code '{code}' not found.", status_code=404
                )

        student, err = resolve_or_upsert_student(
            self.db,
            full_name=full_name,
            email=email,
            student_id_number=student_id_number,
            device_id=device_id,
        )
        if err is not None:
            raise CourseError(err[0], status_code=err[1])

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
                .filter_by(session_id=s.id, email=student.email)
                .one_or_none()
            )
            if r_exist is None:
                self.db.add(
                    RosterEntry(
                        session_id=s.id,
                        student_id_number=student.student_id_number,
                        full_name=student.full_name,
                        email=student.email,
                    )
                )
            elif student.student_id_number and not r_exist.student_id_number:
                r_exist.student_id_number = student.student_id_number

        self.db.commit()
        return course, student

    def import_course_roster_rows(
        self, course_id: int, rows: list[tuple[Optional[str], str, str]]
    ) -> int:
        """Enroll parsed (Student ID, Full Name, Email) rows into CourseEnrollment and all sessions."""
        course = self.db.get(Course, course_id)
        if course is None:
            raise CourseError("Course not found.", status_code=404)

        count = 0
        for sid_val, name_val, email_val in rows:
            self.enroll_student_by_code(
                course_code=course.course_code,
                full_name=name_val,
                email=email_val,
                student_id_number=sid_val,
            )
            count += 1
        return count

    def import_course_roster_csv(self, course_id: int, csv_content: str) -> int:
        """Import a CSV of (Full Name, Email[, Student ID]) into CourseEnrollment and all sessions."""
        return self.import_course_roster_rows(
            course_id, parse_roster_csv_rows(csv_content)
        )

    def remove_student_from_course(
        self,
        course_id: int,
        student_id: int,
        lecturer_id: int,
        ip_address: Optional[str] = None,
    ) -> Student:
        """Remove a student's enrollment, session roster entries, and course attendance records."""
        course = self.db.get(Course, course_id)
        if course is None:
            raise CourseError("Course not found.", status_code=404)
        student = self.db.get(Student, student_id)
        if student is None:
            raise CourseError("Student not found.", status_code=404)

        self.db.query(CourseEnrollment).filter_by(
            course_id=course_id, student_id=student.id
        ).delete()

        session_ids = [
            s.id
            for s in self.db.query(ClassSession.id).filter_by(course_id=course_id).all()
        ]
        if session_ids:
            self.db.query(AttendanceRecord).filter(
                AttendanceRecord.session_id.in_(session_ids),
                AttendanceRecord.student_id == student.id,
            ).delete(synchronize_session=False)
            self.db.query(RosterEntry).filter(
                RosterEntry.session_id.in_(session_ids),
                func.lower(RosterEntry.email) == student.email.lower(),
            ).delete(synchronize_session=False)

        record_audit_event(
            self.db,
            actor_type="lecturer",
            actor_id=lecturer_id,
            action="course.student_removed",
            target_type="student",
            target_id=student.id,
            detail=f"Removed student_id={student.id} ({student.email}) from course_id={course_id}",
            ip_address=ip_address,
        )
        self.db.commit()
        return student

    def reset_student_device(
        self,
        course_id: int,
        student_id: int,
        lecturer_id: int,
        ip_address: Optional[str] = None,
    ) -> Student:
        """Allow an owning lecturer to clear a student's bound device so they can re-bind a new phone."""
        student = self.db.get(Student, student_id)
        if student is None:
            raise CourseError("Student not found.", status_code=404)
        student.primary_device_id = None
        student.device_bound_at = None
        record_audit_event(
            self.db,
            actor_type="lecturer",
            actor_id=lecturer_id,
            action="attendance.device_reset",
            target_type="student",
            target_id=student.id,
            detail=f"Reset primary device in course_id={course_id}",
            ip_address=ip_address,
        )
        self.db.commit()
        return student

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

        my_courses_ranked: list[tuple[tuple[int, datetime, int, int], dict[str, Any]]] = []
        catalog_courses_ranked: list[tuple[tuple[int, datetime, int, int], dict[str, Any]]] = []

        for c in all_courses:
            sessions = (
                self.db.query(ClassSession)
                .filter_by(course_id=c.id)
                .order_by(ClassSession.started_at.desc(), ClassSession.id.desc())
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
                checked_in_iso = (
                    rec.checked_in_at.isoformat() if rec is not None else None
                )
                started_str = s.started_at.strftime("%Y-%m-%d %H:%M") if s.started_at else ""
                started_iso = s.started_at.isoformat() if s.started_at else ""
                s_card = {
                    "id": s.id,
                    "lecture_number": s.lecture_number,
                    "course_name": c.name,
                    "session_code": s.session_code,
                    "title": s.title,
                    "started_at": started_str,
                    "started_at_iso": started_iso,
                    "attendance_open": s.attendance_open,
                    "checked_in": checked_in,
                    "attendance_status": status_label,
                    "checked_in_at": checked_in_time,
                    "checked_in_at_iso": checked_in_iso,
                    "chat_count": self.db.query(ChatMessage)
                    .filter_by(session_id=s.id)
                    .count(),
                    "poll_count": self.db.query(Poll)
                    .filter(Poll.session_id == s.id, Poll.status != "DRAFT")
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
                        "lecture_number": s.lecture_number,
                        "session_code": s.session_code,
                        "lecture_title": s.title,
                        "lecture_date": started_str,
                        "status": status_label,
                        "checked_in_at": checked_in_time or "Not checked in",
                    }
                )

            latest_session = sessions[0] if sessions else None
            latest_dt = (
                latest_session.started_at.replace(tzinfo=None)
                if (latest_session is not None and latest_session.started_at is not None)
                else None
            )
            latest_lecture_at = (
                latest_session.started_at.isoformat()
                if (latest_session is not None and latest_session.started_at is not None)
                else ""
            )

            is_enrolled = c.id in enrolled_course_ids
            if is_enrolled:
                course_card = {
                    "id": c.id,
                    "course_code": c.course_code,
                    "name": c.name,
                    "department": c.department,
                    "semester": c.semester,
                    "lecturer_name": c.lecturer_name,
                    "is_enrolled": True,
                    "attended_count": len(attended_session_ids),
                    "total_sessions": len(sessions),
                    "attendance_pct": (
                        round((len(attended_session_ids) / len(sessions)) * 100.0, 1)
                        if sessions
                        else 0.0
                    ),
                    "latest_lecture_at": latest_lecture_at,
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
            else:
                # Strict isolation: unenrolled catalog entries expose only discovery metadata
                course_card = {
                    "id": c.id,
                    "course_code": c.course_code,
                    "name": c.name,
                    "department": c.department,
                    "semester": c.semester,
                    "lecturer_name": c.lecturer_name,
                    "is_enrolled": False,
                    "attended_count": 0,
                    "total_sessions": len(sessions),
                    "attendance_pct": 0.0,
                    "latest_lecture_at": latest_lecture_at,
                    "sessions": [],
                    "my_attendance_logs": [],
                    "lecture_summaries": [],
                    "assignments": [],
                }
            sort_key = (
                1 if latest_dt is not None else 0,
                latest_dt if latest_dt is not None else datetime.min,
                latest_session.id if latest_session is not None else 0,
                c.id,
            )
            if is_enrolled:
                my_courses_ranked.append((sort_key, course_card))
            catalog_courses_ranked.append((sort_key, course_card))

        my_courses_ranked.sort(key=lambda item: item[0], reverse=True)
        catalog_courses_ranked.sort(key=lambda item: item[0], reverse=True)
        my_courses = [card for _, card in my_courses_ranked]
        catalog_courses = [card for _, card in catalog_courses_ranked]

        return {
            "authenticated": student is not None,
            "student": (
                {
                    "id": student.id,
                    "student_id_number": student.student_id_number,
                    "full_name": student.full_name,
                    "email": (
                        ""
                        if is_synthetic_student_email(student.email)
                        else student.email
                    ),
                }
                if student is not None
                else None
            ),
            "my_courses": my_courses,
            "all_courses": catalog_courses,
        }
