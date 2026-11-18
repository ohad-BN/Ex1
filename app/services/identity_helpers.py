from __future__ import annotations

import csv
from datetime import datetime, timezone
import io
import re
from typing import Any, Iterable, Optional

from sqlalchemy.orm import Session

from app.models import AuditLog, Student

EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
USERNAME_RE = re.compile(r"^[a-z0-9][a-z0-9._-]{2,31}$")
STUDENT_ID_RE = re.compile(r"^[A-Za-z0-9]{4,32}$")
RESERVED_USERNAMES = frozenset(
    {"admin", "administrator", "root", "system", "support", "teacher", "student", "api"}
)


def normalize_student_id(raw_id: Optional[str]) -> Optional[str]:
    """Normalize university/student ID (strip spaces/hyphens), returning None if blank."""
    cleaned = re.sub(r"[\s\-]+", "", (raw_id or "").strip()).upper()
    return cleaned or None


def validate_student_id_format(norm_id: str) -> bool:
    return bool(STUDENT_ID_RE.match(norm_id))


def record_audit_event(
    db: Session,
    *,
    actor_type: str,
    action: str,
    actor_id: Optional[int] = None,
    target_type: Optional[str] = None,
    target_id: Optional[int] = None,
    detail: Optional[str] = None,
    ip_address: Optional[str] = None,
) -> None:
    """Append a security/administrative audit entry without committing prematurely."""
    db.add(
        AuditLog(
            actor_type=actor_type,
            actor_id=actor_id,
            action=action,
            target_type=target_type,
            target_id=target_id,
            detail=(detail or "")[:500] or None,
            ip_address=(ip_address or "")[:64] or None,
        )
    )


def resolve_or_upsert_student(
    db: Session,
    *,
    full_name: str,
    email: str,
    student_id_number: Optional[str] = None,
    device_id: Optional[str] = None,
) -> tuple[Student, Optional[tuple[str, int]]]:
    """Resolve or register Student strictly enforcing (student_id_number, email) consistency.

    Returns (Student, None) on success or (None, (error_message, http_status)) on validation/conflict.
    """
    norm_email = (email or "").strip().lower()
    clean_name = (full_name or "").strip()
    norm_sid = normalize_student_id(student_id_number)

    if not clean_name or not norm_email:
        return None, ("Full name and email are required.", 400)  # type: ignore[return-value]
    if not EMAIL_RE.match(norm_email):
        return None, ("Please enter a valid university or personal email address.", 400)  # type: ignore[return-value]
    if norm_sid is not None and not validate_student_id_format(norm_sid):
        return None, ("Student ID must be 4-32 letters or digits.", 400)  # type: ignore[return-value]

    by_email = db.query(Student).filter_by(email=norm_email).one_or_none()
    by_sid = (
        db.query(Student).filter_by(student_id_number=norm_sid).one_or_none()
        if norm_sid is not None
        else None
    )

    if by_email is not None and by_sid is not None and by_email.id != by_sid.id:
        return None, (  # type: ignore[return-value]
            "Student ID and Email belong to different registered accounts.",
            409,
        )
    if by_sid is not None and by_sid.email.lower() != norm_email:
        return None, (  # type: ignore[return-value]
            f"Student ID '{norm_sid}' is already registered with a different email address.",
            409,
        )
    if (
        by_email is not None
        and norm_sid is not None
        and by_email.student_id_number is not None
        and by_email.student_id_number != norm_sid
    ):
        return None, (  # type: ignore[return-value]
            "Submitted Student ID does not match the ID registered for this email.",
            409,
        )

    student = by_email or by_sid
    now_dt = datetime.now(timezone.utc)
    if student is None:
        student = Student(
            full_name=clean_name,
            email=norm_email,
            student_id_number=norm_sid,
            primary_device_id=device_id,
            device_bound_at=now_dt if device_id else None,
        )
        db.add(student)
        db.flush()
    else:
        student.full_name = clean_name
        if norm_sid is not None and not student.student_id_number:
            student.student_id_number = norm_sid
        if device_id and not student.primary_device_id:
            student.primary_device_id = device_id
            student.device_bound_at = now_dt
        db.flush()

    return student, None


def _parse_roster_table_rows(
    raw_rows: Iterable[Iterable[Any]],
) -> list[tuple[Optional[str], str, str]]:
    """Parse 2- or 3-column tabular roster rows into [(student_id_number, full_name, email), ...]."""
    parsed: list[tuple[Optional[str], str, str]] = []
    for raw_row in raw_rows:
        cols: list[str] = []
        for cell in raw_row:
            if cell is None:
                continue
            if isinstance(cell, float) and cell.is_integer():
                text = str(int(cell))
            else:
                text = str(cell)
            text = text.strip().lstrip("\ufeff")
            if text:
                cols.append(text)
        if len(cols) < 2:
            continue
        email_idx = next((i for i, c in enumerate(cols) if EMAIL_RE.match(c)), None)
        if email_idx is None:
            # Header row or malformed row without valid email
            continue
        email_val = cols[email_idx].lower()
        others = [c for i, c in enumerate(cols) if i != email_idx]
        if not others:
            continue

        sid_val: Optional[str] = None
        name_val = ""
        if len(others) == 1:
            name_val = others[0]
        else:
            # Identify column with Student ID (digits/short alphanumeric code without spaces)
            sid_idx = next(
                (i for i, c in enumerate(others) if " " not in c and any(ch.isdigit() for ch in c)),
                None,
            )
            if sid_idx is not None:
                sid_val = normalize_student_id(others[sid_idx])
                name_candidates = [c for i, c in enumerate(others) if i != sid_idx]
                name_val = name_candidates[0] if name_candidates else ""
            else:
                name_val = others[0]

        if not name_val or name_val.lower() in ("name", "full name", "full_name", "student name"):
            continue
        parsed.append((sid_val, name_val, email_val))
    return parsed


def parse_roster_csv_rows(csv_content: str) -> list[tuple[Optional[str], str, str]]:
    """Parse 2- or 3-column CSV roster rows into [(student_id_number, full_name, email), ...]."""
    reader = csv.reader(io.StringIO((csv_content or "").strip()))
    return _parse_roster_table_rows(reader)


def parse_roster_excel_rows(raw_bytes: bytes) -> list[tuple[Optional[str], str, str]]:
    """Parse 2- or 3-column Excel (.xlsx/.xlsm) roster rows via openpyxl."""
    from openpyxl import load_workbook

    wb = load_workbook(io.BytesIO(raw_bytes), read_only=True, data_only=True)
    try:
        ws = wb.active
        if ws is None:
            return []
        return _parse_roster_table_rows(ws.iter_rows(values_only=True))
    finally:
        wb.close()


def parse_roster_upload(
    filename: str, raw_bytes: bytes
) -> list[tuple[Optional[str], str, str]]:
    """Parse either an uploaded Excel (.xlsx/.xlsm) or CSV roster file into normalized rows."""
    lower_name = (filename or "").lower()
    if lower_name.endswith((".xlsx", ".xlsm")) or raw_bytes[:2] == b"PK":
        return parse_roster_excel_rows(raw_bytes)
    text = raw_bytes.decode("utf-8-sig", errors="replace")
    return parse_roster_csv_rows(text)


def validate_image_magic_bytes(data: bytes) -> bool:
    """Verify uploaded headcount photo begins with JPEG, PNG, GIF, or WebP magic bytes."""
    if not data or len(data) < 12:
        return False
    if data[:3] == b"\xff\xd8\xff":
        return True
    if data[:8] == b"\x89PNG\r\n\x1a\n":
        return True
    if data[:6] in (b"GIF87a", b"GIF89a"):
        return True
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return True
    return False


