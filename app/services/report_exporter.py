from __future__ import annotations

import csv
import io
from typing import Any

from openpyxl import Workbook
from openpyxl.styles import Alignment, Font, PatternFill

from app.models import ClassSession
from app.services.reconciler import AuditVerdict


class SpreadsheetExporter:
    """Generates formatted online/offline Excel (.xlsx) and CSV attendance audit reports."""

    def export_xlsx(
        self,
        session: ClassSession,
        verdict: AuditVerdict,
        rows: list[dict[str, Any]],
    ) -> bytes:
        """Build a 2-sheet Excel workbook (Audit Summary + Student Attendance)."""
        wb = Workbook()

        # Sheet 1: Audit Summary
        ws_sum = wb.active
        ws_sum.title = "Audit Summary"

        header_fill = PatternFill("solid", fgColor="1E293B")
        header_font = Font(bold=True, color="FFFFFF", size=11)
        bold_font = Font(bold=True, size=11)

        ws_sum["A1"] = "ClassPulse — Classroom Attendance & Headcount Audit Report"
        ws_sum["A1"].font = Font(bold=True, size=14, color="0F172A")

        summary_pairs = [
            ("Session Code", session.session_code),
            ("Session Title", session.title),
            (
                "Session Started (UTC)",
                session.started_at.strftime("%Y-%m-%d %H:%M:%S UTC"),
            ),
            ("Registered Roster Count", verdict.roster_total),
            ("QR Self-Check-In Count", verdict.qr_count),
            ("Verified Visual Face Headcount", verdict.visual_count),
            ("Discrepancy Delta (QR - Visual)", verdict.delta),
            ("Audit Integrity Status", verdict.status),
            ("Audit Summary Note", verdict.summary_note),
        ]

        for row_idx, (label, val) in enumerate(summary_pairs, start=3):
            cell_a = ws_sum.cell(row=row_idx, column=1, value=label)
            cell_b = ws_sum.cell(row=row_idx, column=2, value=val)
            cell_a.font = bold_font
            cell_b.alignment = Alignment(horizontal="left")

        ws_sum.column_dimensions["A"].width = 34
        ws_sum.column_dimensions["B"].width = 68

        # Sheet 2: Student Attendance
        ws_att = wb.create_sheet(title="Student Attendance")
        headers = [
            "#",
            "Full Name",
            "Email",
            "Roster Status",
            "Attendance Status",
            "Check-In Timestamp",
            "Late",
            "Device Hash",
        ]
        ws_att.append(headers)
        for col_idx in range(1, len(headers) + 1):
            c = ws_att.cell(row=1, column=col_idx)
            c.fill = header_fill
            c.font = header_font

        present_fill = PatternFill("solid", fgColor="DCFCE7")
        absent_fill = PatternFill("solid", fgColor="FEE2E2")

        for idx, item in enumerate(rows, start=1):
            att_status = item.get("attendance_status", "Present")
            ws_att.append(
                [
                    idx,
                    item.get("full_name", ""),
                    item.get("email", ""),
                    item.get("roster_status", "Registered"),
                    att_status,
                    item.get("checked_in_at", "-"),
                    "Yes" if item.get("is_late") else "No",
                    item.get("device_hash", "-"),
                ]
            )
            status_cell = ws_att.cell(row=idx + 1, column=5)
            status_cell.fill = (
                present_fill if att_status == "Present" else absent_fill
            )

        widths = [6, 26, 32, 20, 18, 24, 10, 14]
        for i, w in enumerate(widths, start=1):
            ws_att.column_dimensions[chr(64 + i)].width = w

        buf = io.BytesIO()
        wb.save(buf)
        return buf.getvalue()

    def export_csv(
        self,
        session: ClassSession,
        verdict: AuditVerdict,
        rows: list[dict[str, Any]],
    ) -> str:
        """Export UTF-8 BOM CSV containing audit summary metadata and student rows."""
        buf = io.StringIO()
        buf.write("\ufeff")  # UTF-8 BOM for Excel Hebrew/English compatibility
        writer = csv.writer(buf)

        writer.writerow(["# ClassPulse Attendance & Headcount Audit Report"])
        writer.writerow(["Session Code", session.session_code])
        writer.writerow(["Session Title", session.title])
        writer.writerow(["Roster Total", verdict.roster_total])
        writer.writerow(["QR Check-In Count", verdict.qr_count])
        writer.writerow(["Verified Visual Headcount", verdict.visual_count])
        writer.writerow(["Delta (QR - Visual)", verdict.delta])
        writer.writerow(["Audit Status", verdict.status])
        writer.writerow(["Summary Note", verdict.summary_note])
        writer.writerow([])

        writer.writerow(
            [
                "#",
                "Full Name",
                "Email",
                "Roster Status",
                "Attendance Status",
                "Check-In Timestamp",
                "Late",
                "Device Hash",
            ]
        )
        for idx, item in enumerate(rows, start=1):
            writer.writerow(
                [
                    idx,
                    item.get("full_name", ""),
                    item.get("email", ""),
                    item.get("roster_status", "Registered"),
                    item.get("attendance_status", "Present"),
                    item.get("checked_in_at", "-"),
                    "Yes" if item.get("is_late") else "No",
                    item.get("device_hash", "-"),
                ]
            )
        return buf.getvalue()
