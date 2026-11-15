from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Optional

from sqlalchemy import (
    Boolean,
    DateTime,
    Float,
    ForeignKey,
    Integer,
    String,
    Text,
    UniqueConstraint,
    create_engine,
    event,
)
from sqlalchemy.engine import Engine
from sqlalchemy.orm import (
    DeclarativeBase,
    Mapped,
    Session,
    mapped_column,
    relationship,
    scoped_session,
    sessionmaker,
)


def utcnow() -> datetime:
    """Return timezone-aware UTC timestamp."""
    return datetime.now(timezone.utc)


class Base(DeclarativeBase):
    """Base declarative class for all ClassPulse ORM entities."""


class Lecturer(Base):
    """Registered teacher/lecturer account owning school courses and lectures."""

    __tablename__ = "lecturers"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    full_name: Mapped[str] = mapped_column(String(120), nullable=False)
    email: Mapped[str] = mapped_column(
        String(160), unique=True, index=True, nullable=False
    )
    department: Mapped[str] = mapped_column(
        String(120), default="Computer Science", nullable=False
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, nullable=False
    )

    courses: Mapped[list["Course"]] = relationship(
        back_populates="lecturer", cascade="all, delete-orphan"
    )


class Course(Base):
    """Represents a school-wide course containing multiple lecture sessions, enrollments, and assignments."""

    __tablename__ = "courses"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    lecturer_id: Mapped[Optional[int]] = mapped_column(
        ForeignKey("lecturers.id", ondelete="SET NULL"), index=True, nullable=True
    )
    course_code: Mapped[str] = mapped_column(
        String(32), unique=True, index=True, nullable=False
    )
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    department: Mapped[str] = mapped_column(
        String(120), default="Computer Science", nullable=False
    )
    semester: Mapped[str] = mapped_column(
        String(64), default="Fall 2026", nullable=False
    )
    lecturer_name: Mapped[str] = mapped_column(
        String(120), default="Prof. Boaz Ben-Moshe", nullable=False
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, nullable=False
    )

    lecturer: Mapped[Optional["Lecturer"]] = relationship(back_populates="courses")
    sessions: Mapped[list["ClassSession"]] = relationship(
        back_populates="course", cascade="all, delete-orphan"
    )
    enrollments: Mapped[list["CourseEnrollment"]] = relationship(
        back_populates="course", cascade="all, delete-orphan"
    )
    assignments: Mapped[list["Assignment"]] = relationship(
        back_populates="course", cascade="all, delete-orphan"
    )


class CourseEnrollment(Base):
    """Links a registered Student to a school Course."""

    __tablename__ = "course_enrollments"
    __table_args__ = (
        UniqueConstraint("course_id", "student_id", name="uq_course_student_enrollment"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    course_id: Mapped[int] = mapped_column(
        ForeignKey("courses.id", ondelete="CASCADE"), index=True, nullable=False
    )
    student_id: Mapped[int] = mapped_column(
        ForeignKey("students.id", ondelete="CASCADE"), index=True, nullable=False
    )
    enrolled_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, nullable=False
    )

    course: Mapped["Course"] = relationship(back_populates="enrollments")
    student: Mapped["Student"] = relationship(back_populates="course_enrollments")


class ClassSession(Base):
    """Represents a live or archived classroom session."""

    __tablename__ = "class_sessions"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    course_id: Mapped[Optional[int]] = mapped_column(
        ForeignKey("courses.id", ondelete="SET NULL"), index=True, nullable=True
    )
    session_code: Mapped[str] = mapped_column(
        String(16), unique=True, index=True, nullable=False
    )
    title: Mapped[str] = mapped_column(String(200), nullable=False)
    started_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, nullable=False
    )
    attendance_open: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    late_after_minutes: Mapped[int] = mapped_column(Integer, default=15, nullable=False)
    headcount_tolerance: Mapped[int] = mapped_column(Integer, default=2, nullable=False)
    zoom_meeting_id: Mapped[Optional[str]] = mapped_column(
        String(32), index=True, nullable=True
    )
    zoom_auto_sync: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)

    course: Mapped[Optional["Course"]] = relationship(back_populates="sessions")
    attendance_records: Mapped[list["AttendanceRecord"]] = relationship(
        back_populates="session", cascade="all, delete-orphan"
    )
    roster_entries: Mapped[list["RosterEntry"]] = relationship(
        back_populates="session", cascade="all, delete-orphan"
    )
    headcount_snapshots: Mapped[list["HeadcountSnapshot"]] = relationship(
        back_populates="session", cascade="all, delete-orphan"
    )
    polls: Mapped[list["Poll"]] = relationship(
        back_populates="session", cascade="all, delete-orphan"
    )
    chat_messages: Mapped[list["ChatMessage"]] = relationship(
        back_populates="session", cascade="all, delete-orphan"
    )
    lecture_artifacts: Mapped[list["LectureArtifact"]] = relationship(
        back_populates="session", cascade="all, delete-orphan"
    )

    def is_active(self) -> bool:
        return bool(self.attendance_open)


class Student(Base):
    """Registered student identity (Name + Email + Primary Bound Device ID)."""

    __tablename__ = "students"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    full_name: Mapped[str] = mapped_column(String(120), nullable=False)
    email: Mapped[str] = mapped_column(
        String(160), unique=True, index=True, nullable=False
    )
    primary_device_id: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, nullable=False
    )

    attendance_records: Mapped[list["AttendanceRecord"]] = relationship(
        back_populates="student", cascade="all, delete-orphan"
    )
    course_enrollments: Mapped[list["CourseEnrollment"]] = relationship(
        back_populates="student", cascade="all, delete-orphan"
    )


class RosterEntry(Base):
    """Optional imported course roster entry for a session (Name + Email, no ID#)."""

    __tablename__ = "roster_entries"
    __table_args__ = (
        UniqueConstraint("session_id", "email", name="uq_roster_session_email"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    session_id: Mapped[int] = mapped_column(
        ForeignKey("class_sessions.id", ondelete="CASCADE"), index=True, nullable=False
    )
    full_name: Mapped[str] = mapped_column(String(120), nullable=False)
    email: Mapped[str] = mapped_column(String(160), nullable=False)

    session: Mapped["ClassSession"] = relationship(back_populates="roster_entries")


class AttendanceRecord(Base):
    """Verified QR self-attendance record with anti-proxy device and student constraints."""

    __tablename__ = "attendance_records"
    __table_args__ = (
        UniqueConstraint(
            "session_id", "student_id", name="uq_attendance_session_student"
        ),
        UniqueConstraint(
            "session_id", "device_id", name="uq_attendance_session_device"
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    session_id: Mapped[int] = mapped_column(
        ForeignKey("class_sessions.id", ondelete="CASCADE"), index=True, nullable=False
    )
    student_id: Mapped[int] = mapped_column(
        ForeignKey("students.id", ondelete="CASCADE"), index=True, nullable=False
    )
    device_id: Mapped[str] = mapped_column(String(64), nullable=False)
    checked_in_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, nullable=False
    )
    is_late: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    token_bucket: Mapped[str] = mapped_column(String(32), nullable=False)

    session: Mapped["ClassSession"] = relationship(back_populates="attendance_records")
    student: Mapped["Student"] = relationship(back_populates="attendance_records")


class HeadcountSnapshot(Base):
    """Computer-vision classroom headcount result and lecturer verification state."""

    __tablename__ = "headcount_snapshots"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    session_id: Mapped[int] = mapped_column(
        ForeignKey("class_sessions.id", ondelete="CASCADE"), index=True, nullable=False
    )
    raw_image_path: Mapped[str] = mapped_column(String(500), nullable=False)
    annotated_image_path: Mapped[str] = mapped_column(String(500), nullable=False)
    auto_face_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    verified_face_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    boxes_json: Mapped[str] = mapped_column(Text, default="[]", nullable=False)
    lecturer_note: Mapped[str] = mapped_column(String(500), default="", nullable=False)
    captured_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, nullable=False
    )

    session: Mapped["ClassSession"] = relationship(back_populates="headcount_snapshots")

    @property
    def auto_count(self) -> int:
        return self.auto_face_count

    @property
    def verified_count(self) -> int:
        return self.verified_face_count

    @property
    def image_url(self) -> str:
        return f"/api/sessions/{self.session_id}/headcount/image/raw?snap_id={self.id}"


class Poll(Base):
    """Pre-prepared or on-the-spot live classroom poll (Feature 3)."""

    __tablename__ = "polls"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    session_id: Mapped[int] = mapped_column(
        ForeignKey("class_sessions.id", ondelete="CASCADE"), index=True, nullable=False
    )
    question: Mapped[str] = mapped_column(String(500), nullable=False)
    options_json: Mapped[str] = mapped_column(Text, nullable=False)
    correct_index: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    time_limit_sec: Mapped[int] = mapped_column(Integer, default=60, nullable=False)
    status: Mapped[str] = mapped_column(String(16), default="DRAFT", nullable=False)
    sort_order: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    activated_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    session: Mapped["ClassSession"] = relationship(back_populates="polls")
    votes: Mapped[list["PollVote"]] = relationship(
        back_populates="poll", cascade="all, delete-orphan"
    )


class PollVote(Base):
    """Single student response to an active poll with idempotent uniqueness."""

    __tablename__ = "poll_votes"
    __table_args__ = (
        UniqueConstraint("poll_id", "student_id", name="uq_poll_student_vote"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    poll_id: Mapped[int] = mapped_column(
        ForeignKey("polls.id", ondelete="CASCADE"), index=True, nullable=False
    )
    student_id: Mapped[int] = mapped_column(
        ForeignKey("students.id", ondelete="CASCADE"), index=True, nullable=False
    )
    selected_index: Mapped[int] = mapped_column(Integer, nullable=False)
    voted_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, nullable=False
    )

    poll: Mapped["Poll"] = relationship(back_populates="votes")


class ChatMessage(Base):
    """Timestamped classroom comment, question, or suggestion (Feature 2)."""

    __tablename__ = "chat_messages"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    session_id: Mapped[int] = mapped_column(
        ForeignKey("class_sessions.id", ondelete="CASCADE"), index=True, nullable=False
    )
    student_id: Mapped[Optional[int]] = mapped_column(
        ForeignKey("students.id", ondelete="SET NULL"), nullable=True
    )
    author_name: Mapped[str] = mapped_column(String(120), nullable=False)
    category: Mapped[str] = mapped_column(String(24), default="Question", nullable=False)
    body: Mapped[str] = mapped_column(Text, nullable=False)
    offset_seconds: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    upvotes: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    is_pinned: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    synced_to_zoom: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, nullable=False
    )

    session: Mapped["ClassSession"] = relationship(back_populates="chat_messages")


class LectureArtifact(Base):
    """Transcribed and Gemini-summarized lecture recording (Feature 4)."""

    __tablename__ = "lecture_artifacts"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    session_id: Mapped[int] = mapped_column(
        ForeignKey("class_sessions.id", ondelete="CASCADE"), index=True, nullable=False
    )
    source_type: Mapped[str] = mapped_column(String(32), default="upload", nullable=False)
    media_path: Mapped[str] = mapped_column(String(500), nullable=False)
    raw_transcript_json: Mapped[str] = mapped_column(Text, default="[]", nullable=False)
    corrected_transcript_json: Mapped[str] = mapped_column(
        Text, default="[]", nullable=False
    )
    summary_markdown: Mapped[str] = mapped_column(Text, default="", nullable=False)
    status: Mapped[str] = mapped_column(String(24), default="PENDING", nullable=False)
    zoom_meeting_uuid: Mapped[Optional[str]] = mapped_column(String(128), nullable=True)
    error_message: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, nullable=False
    )

    session: Mapped["ClassSession"] = relationship(back_populates="lecture_artifacts")


class Assignment(Base):
    """Homework assignment definition and rubric for LLM evaluation (Feature 5)."""

    __tablename__ = "assignments"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    course_id: Mapped[Optional[int]] = mapped_column(
        ForeignKey("courses.id", ondelete="SET NULL"), index=True, nullable=True
    )
    title: Mapped[str] = mapped_column(String(200), nullable=False)
    rubric_text: Mapped[str] = mapped_column(Text, nullable=False)
    topics_json: Mapped[str] = mapped_column(Text, default="[]", nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, nullable=False
    )

    course: Mapped[Optional["Course"]] = relationship(back_populates="assignments")
    submissions: Mapped[list["HomeworkSubmission"]] = relationship(
        back_populates="assignment", cascade="all, delete-orphan"
    )
    stats_aggregate: Mapped[Optional["ClassStatsAggregate"]] = relationship(
        back_populates="assignment", uselist=False, cascade="all, delete-orphan"
    )


class HomeworkSubmission(Base):
    """Individual student homework submission and extracted JSON metrics (Feature 5)."""

    __tablename__ = "homework_submissions"
    __table_args__ = (
        UniqueConstraint(
            "assignment_id", "student_id", name="uq_submission_assignment_student"
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    assignment_id: Mapped[int] = mapped_column(
        ForeignKey("assignments.id", ondelete="CASCADE"), index=True, nullable=False
    )
    student_id: Mapped[int] = mapped_column(
        ForeignKey("students.id", ondelete="CASCADE"), index=True, nullable=False
    )
    version: Mapped[int] = mapped_column(Integer, default=1, nullable=False)
    source_type: Mapped[str] = mapped_column(String(32), default="file", nullable=False)
    source_ref: Mapped[str] = mapped_column(String(500), nullable=False)
    metrics_json: Mapped[str] = mapped_column(Text, default="{}", nullable=False)
    deep_review_md: Mapped[str] = mapped_column(Text, default="", nullable=False)
    submitted_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, nullable=False
    )

    assignment: Mapped["Assignment"] = relationship(back_populates="submissions")


class ClassStatsAggregate(Base):
    """Incremental O(1) class-wide statistical summary for an Assignment (Feature 5)."""

    __tablename__ = "class_stats_aggregates"

    assignment_id: Mapped[int] = mapped_column(
        ForeignKey("assignments.id", ondelete="CASCADE"), primary_key=True
    )
    submission_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    mean_overall_score: Mapped[float] = mapped_column(Float, default=0.0, nullable=False)
    topic_sums_json: Mapped[str] = mapped_column(Text, default="{}", nullable=False)
    scores_list_json: Mapped[str] = mapped_column(Text, default="[]", nullable=False)
    error_tag_counts_json: Mapped[str] = mapped_column(
        Text, default="{}", nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, nullable=False
    )

    assignment: Mapped["Assignment"] = relationship(back_populates="stats_aggregate")


def _configure_sqlite_wal(dbapi_connection: Any, _connection_record: Any) -> None:
    """Enable WAL mode, busy_timeout, and foreign keys on every SQLite connection."""
    cursor = dbapi_connection.cursor()
    try:
        cursor.execute("PRAGMA journal_mode=WAL;")
        cursor.execute("PRAGMA synchronous=NORMAL;")
        cursor.execute("PRAGMA busy_timeout=5000;")
        cursor.execute("PRAGMA foreign_keys=ON;")
    finally:
        cursor.close()


def _migrate_sqlite_columns(engine: Engine) -> None:
    """Safely add Phase 10-13 columns to existing SQLite tables if upgrading in-place."""
    if not str(engine.url).startswith("sqlite"):
        return

    migrations: tuple[tuple[str, str, str], ...] = (
        ("class_sessions", "course_id", "INTEGER REFERENCES courses(id) ON DELETE SET NULL"),
        ("class_sessions", "zoom_meeting_id", "VARCHAR(32)"),
        ("class_sessions", "zoom_auto_sync", "BOOLEAN NOT NULL DEFAULT 0"),
        ("assignments", "course_id", "INTEGER REFERENCES courses(id) ON DELETE SET NULL"),
        ("courses", "lecturer_id", "INTEGER REFERENCES lecturers(id) ON DELETE SET NULL"),
        ("lecture_artifacts", "zoom_meeting_uuid", "VARCHAR(128)"),
        ("lecture_artifacts", "error_message", "TEXT"),
    )

    with engine.begin() as conn:
        table_cols_cache: dict[str, set[str]] = {}
        for table_name, col_name, col_def in migrations:
            if table_name not in table_cols_cache:
                rows = conn.exec_driver_sql(f"PRAGMA table_info({table_name});").fetchall()
                table_cols_cache[table_name] = {r[1] for r in rows} if rows else set()
            existing_cols = table_cols_cache[table_name]
            if existing_cols and col_name not in existing_cols:
                conn.exec_driver_sql(
                    f"ALTER TABLE {table_name} ADD COLUMN {col_name} {col_def};"
                )
                existing_cols.add(col_name)


def init_db_engine(database_url: str) -> tuple[Engine, scoped_session[Session]]:
    """Create a thread-safe SQLAlchemy engine and scoped_session factory."""
    connect_args: dict[str, Any] = {}
    if database_url.startswith("sqlite"):
        connect_args["check_same_thread"] = False
        connect_args["timeout"] = 10.0

    engine = create_engine(
        database_url,
        connect_args=connect_args,
        future=True,
    )
    if database_url.startswith("sqlite"):
        event.listen(engine, "connect", _configure_sqlite_wal)

    Base.metadata.create_all(engine)
    _migrate_sqlite_columns(engine)
    session_factory = scoped_session(
        sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)
    )
    return engine, session_factory
