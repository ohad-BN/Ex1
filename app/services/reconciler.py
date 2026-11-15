from __future__ import annotations

from dataclasses import dataclass


@dataclass
class AuditVerdict:
    """Cross-reference result comparing Roster, QR Self-Attendance, and Visual Headcount."""

    roster_total: int
    qr_count: int
    visual_count: int
    delta: int
    status: str
    summary_note: str
    has_snapshot: bool = True

    @property
    def roster_count(self) -> int:
        return self.roster_total

    @property
    def explanation(self) -> str:
        return self.summary_note

    def to_dict(self) -> dict[str, int | str | bool]:
        return {
            "roster_total": self.roster_total,
            "roster_count": self.roster_total,
            "qr_count": self.qr_count,
            "visual_count": self.visual_count,
            "delta": self.delta,
            "status": self.status,
            "summary_note": self.summary_note,
            "explanation": self.summary_note,
            "has_snapshot": self.has_snapshot,
        }


class AttendanceReconciler:
    """Evaluates discrepancy between QR self-check-ins and CV classroom headcount."""

    def reconcile(
        self,
        roster_total: int,
        qr_count: int,
        visual_count: int,
        tolerance: int = 2,
        has_snapshot: bool = True,
    ) -> AuditVerdict:
        delta = qr_count - visual_count
        if not has_snapshot:
            return AuditVerdict(
                roster_total=roster_total,
                qr_count=qr_count,
                visual_count=0,
                delta=delta,
                status="AWAITING_PHOTO",
                summary_note="No classroom headcount photo uploaded yet.",
                has_snapshot=False,
            )

        if abs(delta) <= tolerance:
            status = "VERIFIED"
            note = (
                f"Attendance verified: QR check-ins ({qr_count}) match visual classroom "
                f"headcount ({visual_count}) within tolerance (±{tolerance})."
            )
        elif delta > tolerance:
            status = "DISCREPANCY_ALERT"
            note = (
                f"Alert: {delta} more QR check-ins ({qr_count}) than faces detected in "
                f"classroom ({visual_count}). Possible remote/proxy check-in attempt."
            )
        else:
            status = "UNDER_SCANNED"
            note = (
                f"Notice: {abs(delta)} more students visible in classroom ({visual_count}) "
                f"than QR check-ins ({qr_count}). Some students may not have scanned."
            )

        return AuditVerdict(
            roster_total=roster_total,
            qr_count=qr_count,
            visual_count=visual_count,
            delta=delta,
            status=status,
            summary_note=note,
            has_snapshot=True,
        )
