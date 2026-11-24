from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Optional

from sqlalchemy.orm import Session

from app.models import ChatMessage, ClassSession, Course, CourseStaff

VALID_CATEGORIES = ("Question", "Comment", "Suggestion", "Offline Question")


class ChatError(Exception):
    """Domain error for classroom chat operations."""

    def __init__(self, message: str, status_code: int = 400) -> None:
        super().__init__(message)
        self.message = message
        self.status_code = status_code


def format_offset_mmss(offset_seconds: int) -> str:
    """Format lecture-relative seconds as +MM:SS (or +HH:MM:SS if >= 1 hour)."""
    secs = max(0, int(offset_seconds))
    hours, rem = divmod(secs, 3600)
    minutes, seconds = divmod(rem, 60)
    if hours > 0:
        return f"+{hours:02d}:{minutes:02d}:{seconds:02d}"
    return f"+{minutes:02d}:{seconds:02d}"


class ChatService:
    """Manages timestamped student Q&A/comments, upvotes, pinning, and timeline export."""

    def __init__(self, db: Session) -> None:
        self.db = db

    def serialize_message(self, msg: ChatMessage) -> dict[str, Any]:
        session = self.db.get(ClassSession, msg.session_id)
        course = (
            self.db.get(Course, session.course_id)
            if session is not None and session.course_id is not None
            else None
        )
        return {
            "id": msg.id,
            "session_id": msg.session_id,
            "session_code": session.session_code if session else "",
            "lecture_number": session.lecture_number if session else None,
            "lecture_title": session.title if session else "",
            "course_id": course.id if course else None,
            "course_code": course.course_code if course else "",
            "course_name": course.name if course else "",
            "student_id": msg.student_id,
            "author_name": msg.author_name,
            "sender_role": "student" if msg.student_id is not None else "lecturer",
            "category": msg.category,
            "body": msg.body,
            "offset_seconds": msg.offset_seconds,
            "offset_mmss": format_offset_mmss(msg.offset_seconds),
            "upvotes": msg.upvotes,
            "is_pinned": msg.is_pinned,
            "created_at": msg.created_at.strftime("%H:%M:%S UTC"),
        }

    def get_course_lecturer_recipient_ids(self, session_id: int) -> list[int]:
        """Return primary owner + CourseStaff lecturer IDs for cross-lecture chat notifications."""
        session = self.db.get(ClassSession, session_id)
        if session is None or session.course_id is None:
            return []
        course = self.db.get(Course, session.course_id)
        if course is None:
            return []
        ids: list[int] = []
        if course.lecturer_id is not None:
            ids.append(course.lecturer_id)
        staff_rows = self.db.query(CourseStaff).filter_by(course_id=course.id).all()
        for row in staff_rows:
            if row.lecturer_id not in ids:
                ids.append(row.lecturer_id)
        return ids

    def post_message(
        self,
        session_id: int,
        author_name: str,
        body: str,
        category: str = "Question",
        student_id: Optional[int] = None,
        now_ts: Optional[float] = None,
    ) -> ChatMessage:
        """Create a timestamped classroom message anchored to session start time."""
        session = self.db.get(ClassSession, session_id)
        if session is None:
            raise ChatError("Session not found.", status_code=404)

        clean_body = (body or "").strip()
        if not clean_body:
            raise ChatError("Message body cannot be empty.", status_code=400)
        if len(clean_body) > 1000:
            clean_body = clean_body[:1000]

        clean_author = (author_name or "Anonymous Student").strip()[:120]
        clean_cat = category if category in VALID_CATEGORIES else "Question"

        now_dt = (
            datetime.fromtimestamp(now_ts, tz=timezone.utc)
            if now_ts is not None
            else datetime.now(timezone.utc)
        )
        started_dt = session.started_at
        if started_dt.tzinfo is None:
            started_dt = started_dt.replace(tzinfo=timezone.utc)
        offset_sec = max(0, int((now_dt - started_dt).total_seconds()))

        msg = ChatMessage(
            session_id=session.id,
            student_id=student_id,
            author_name=clean_author,
            category=clean_cat,
            body=clean_body,
            offset_seconds=offset_sec,
            upvotes=0,
            is_pinned=False,
            created_at=now_dt,
        )
        self.db.add(msg)
        self.db.commit()
        return msg

    def upvote_message(self, message_id: int) -> ChatMessage:
        """Increment the upvote counter on a classroom message."""
        msg = self.db.get(ChatMessage, message_id)
        if msg is None:
            raise ChatError("Message not found.", status_code=404)
        msg.upvotes += 1
        self.db.commit()
        return msg

    def toggle_pin(self, message_id: int) -> ChatMessage:
        """Toggle lecturer pin highlight on a classroom message."""
        msg = self.db.get(ChatMessage, message_id)
        if msg is None:
            raise ChatError("Message not found.", status_code=404)
        msg.is_pinned = not msg.is_pinned
        self.db.commit()
        return msg

    def list_messages(self, session_id: int) -> list[dict[str, Any]]:
        """Return session chat messages ordered with pinned first, then chronological."""
        msgs = (
            self.db.query(ChatMessage)
            .filter_by(session_id=session_id)
            .order_by(ChatMessage.is_pinned.desc(), ChatMessage.id.asc())
            .all()
        )
        return [self.serialize_message(m) for m in msgs]

    def export_timeline_text(self, session_id: int) -> str:
        """Export chronological timestamped chat & Q&A log for download or LLM lecture summary."""
        session = self.db.get(ClassSession, session_id)
        title = session.title if session else f"Session {session_id}"
        msgs = (
            self.db.query(ChatMessage)
            .filter_by(session_id=session_id)
            .order_by(ChatMessage.offset_seconds.asc(), ChatMessage.id.asc())
            .all()
        )
        lines = [
            f"# ClassPulse Classroom Chat & Q&A Timeline — {title}",
            f"# Total Messages: {len(msgs)}",
            "",
        ]
        for m in msgs:
            ts = format_offset_mmss(m.offset_seconds)
            pin_tag = " [PINNED]" if m.is_pinned else ""
            up_tag = f" (+{m.upvotes} upvotes)" if m.upvotes > 0 else ""
            lines.append(
                f"[{ts}] [{m.category}]{pin_tag} {m.author_name}: {m.body}{up_tag}"
            )
        return "\n".join(lines) + "\n"
