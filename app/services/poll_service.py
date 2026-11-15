from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any, Optional

from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.models import ClassSession, Poll, PollVote


class PollError(Exception):
    """Domain error for polling operations with HTTP status code."""

    def __init__(self, message: str, status_code: int = 400) -> None:
        super().__init__(message)
        self.message = message
        self.status_code = status_code


class PollService:
    """Manages pre-class Poll Bank drafts, live poll lifecycle, voting, and reconnect sync."""

    def __init__(self, db: Session) -> None:
        self.db = db

    def serialize_poll(
        self, poll: Poll, student_id: Optional[int] = None
    ) -> dict[str, Any]:
        """Serialize a Poll with live option counts, timer remaining, and student vote state."""
        try:
            options: list[str] = json.loads(poll.options_json)
        except Exception:  # noqa: BLE001
            options = []

        counts = [0] * len(options)
        my_vote_index: Optional[int] = None
        votes = self.db.query(PollVote).filter_by(poll_id=poll.id).all()
        for v in votes:
            if 0 <= v.selected_index < len(counts):
                counts[v.selected_index] += 1
            if student_id is not None and v.student_id == student_id:
                my_vote_index = v.selected_index

        total_votes = sum(counts)
        percentages = [
            round((c / total_votes) * 100.0, 1) if total_votes > 0 else 0.0
            for c in counts
        ]

        remaining_sec = poll.time_limit_sec
        if poll.status == "ACTIVE" and poll.activated_at is not None:
            act_dt = poll.activated_at
            if act_dt.tzinfo is None:
                act_dt = act_dt.replace(tzinfo=timezone.utc)
            elapsed = (datetime.now(timezone.utc) - act_dt).total_seconds()
            remaining_sec = max(0, int(round(poll.time_limit_sec - elapsed)))

        return {
            "id": poll.id,
            "session_id": poll.session_id,
            "question": poll.question,
            "options": options,
            "counts": counts,
            "percentages": percentages,
            "total_votes": total_votes,
            "correct_index": poll.correct_index,
            "time_limit_sec": poll.time_limit_sec,
            "remaining_sec": remaining_sec,
            "status": poll.status,
            "sort_order": poll.sort_order,
            "already_voted": my_vote_index is not None,
            "my_vote_index": my_vote_index,
        }

    def create_poll(
        self,
        session_id: int,
        question: str,
        options: list[str],
        time_limit_sec: int = 60,
        correct_index: Optional[int] = None,
        sort_order: int = 0,
    ) -> Poll:
        """Create a pre-class DRAFT poll in the session's Poll Bank."""
        session = self.db.get(ClassSession, session_id)
        if session is None:
            raise PollError("Session not found.", status_code=404)

        clean_q = (question or "").strip()
        if not clean_q:
            raise PollError("Poll question cannot be empty.", status_code=400)

        clean_opts = [str(o).strip() for o in options if str(o).strip()]
        if len(clean_opts) < 2 or len(clean_opts) > 6:
            raise PollError(
                "A poll must have between 2 and 6 non-empty options.", status_code=400
            )

        if correct_index is not None and not (0 <= int(correct_index) < len(clean_opts)):
            raise PollError("correct_index is out of bounds for the given options.")

        poll = Poll(
            session_id=session_id,
            question=clean_q,
            options_json=json.dumps(clean_opts),
            correct_index=int(correct_index) if correct_index is not None else None,
            time_limit_sec=max(10, int(time_limit_sec)),
            status="DRAFT",
            sort_order=int(sort_order),
        )
        self.db.add(poll)
        self.db.commit()
        return poll

    def launch_poll(self, poll_id: int) -> Poll:
        """Activate a poll (closing any previously ACTIVE poll in the same session)."""
        poll = self.db.get(Poll, poll_id)
        if poll is None:
            raise PollError("Poll not found.", status_code=404)

        # Close any other active polls in this session
        active_others = (
            self.db.query(Poll)
            .filter(
                Poll.session_id == poll.session_id,
                Poll.status == "ACTIVE",
                Poll.id != poll.id,
            )
            .all()
        )
        for other in active_others:
            other.status = "CLOSED"

        poll.status = "ACTIVE"
        poll.activated_at = datetime.now(timezone.utc)
        self.db.commit()
        return poll

    def close_poll(self, poll_id: int) -> Poll:
        """Close an active poll and lock further voting."""
        poll = self.db.get(Poll, poll_id)
        if poll is None:
            raise PollError("Poll not found.", status_code=404)
        poll.status = "CLOSED"
        self.db.commit()
        return poll

    def cast_vote(
        self, poll_id: int, student_id: int, option_index: int
    ) -> tuple[dict[str, Any], bool]:
        """Record a student vote idempotently. Returns (serialized_poll, is_new_vote)."""
        poll = self.db.get(Poll, poll_id)
        if poll is None:
            raise PollError("Poll not found.", status_code=404)
        if poll.status != "ACTIVE":
            raise PollError("This poll is not currently active.", status_code=409)

        options = json.loads(poll.options_json)
        if not (0 <= int(option_index) < len(options)):
            raise PollError("Selected option index is invalid.", status_code=400)

        existing = (
            self.db.query(PollVote)
            .filter_by(poll_id=poll.id, student_id=student_id)
            .one_or_none()
        )
        if existing is not None:
            return self.serialize_poll(poll, student_id=student_id), False

        vote = PollVote(
            poll_id=poll.id,
            student_id=student_id,
            selected_index=int(option_index),
        )
        self.db.add(vote)
        try:
            self.db.commit()
            is_new = True
        except IntegrityError:
            self.db.rollback()
            is_new = False

        return self.serialize_poll(poll, student_id=student_id), is_new

    def list_session_polls(
        self, session_id: int, student_id: Optional[int] = None
    ) -> list[dict[str, Any]]:
        """List all polls in a session ordered by sort_order and id."""
        polls = (
            self.db.query(Poll)
            .filter_by(session_id=session_id)
            .order_by(Poll.sort_order.asc(), Poll.id.asc())
            .all()
        )
        return [self.serialize_poll(p, student_id=student_id) for p in polls]

    def build_sync_payload(
        self, session_id: int, student_id: Optional[int] = None
    ) -> dict[str, Any]:
        """Build state hydration payload for newly connected or woken mobile sockets."""
        active_poll = (
            self.db.query(Poll)
            .filter_by(session_id=session_id, status="ACTIVE")
            .order_by(Poll.id.desc())
            .first()
        )
        if active_poll is not None:
            serialized = self.serialize_poll(active_poll, student_id=student_id)
            return {
                "has_active_poll": True,
                "poll": serialized,
                "active_poll": serialized,
                "my_vote": serialized.get("my_vote_index"),
            }

        recent_closed = (
            self.db.query(Poll)
            .filter_by(session_id=session_id, status="CLOSED")
            .order_by(Poll.id.desc())
            .first()
        )
        serialized_closed = (
            self.serialize_poll(recent_closed, student_id=student_id)
            if recent_closed is not None
            else None
        )
        return {
            "has_active_poll": False,
            "poll": serialized_closed,
            "active_poll": serialized_closed,
            "my_vote": serialized_closed.get("my_vote_index") if serialized_closed else None,
        }
