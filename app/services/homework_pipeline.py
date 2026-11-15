from __future__ import annotations

import bisect
import io
import json
import logging
import zipfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

import requests
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from app.models import (
    Assignment,
    ClassStatsAggregate,
    Course,
    HomeworkSubmission,
    Student,
)
from app.services.llm_client import GeminiStudioClient

logger = logging.getLogger(__name__)

ALLOWED_TEXT_EXTENSIONS = {
    ".py",
    ".java",
    ".js",
    ".ts",
    ".tsx",
    ".jsx",
    ".c",
    ".cpp",
    ".h",
    ".hpp",
    ".go",
    ".rs",
    ".html",
    ".css",
    ".sql",
    ".md",
    ".txt",
    ".json",
    ".yml",
    ".yaml",
}
IGNORED_DIR_PARTS = {
    ".git",
    "node_modules",
    "venv",
    ".venv",
    "__pycache__",
    ".pytest_cache",
    "dist",
    "build",
    "target",
}


class HomeworkPipelineError(Exception):
    """Domain error for homework extraction, grading, or cohort analytics."""

    def __init__(self, message: str, status_code: int = 400) -> None:
        super().__init__(message)
        self.message = message
        self.status_code = status_code


class HomeworkMetrics(BaseModel):
    """Stage-1 Pydantic schema extracted for a single student submission."""

    overall_score: float = Field(ge=0.0, le=100.0)
    topic_scores: dict[str, float] = Field(default_factory=dict)
    requirements_coverage_pct: float = Field(ge=0.0, le=100.0)
    strengths: list[str] = Field(default_factory=list)
    weaknesses: list[str] = Field(default_factory=list)
    error_tags: list[str] = Field(default_factory=list)
    style_summary: str = ""


class SubmissionExtractor:
    """Extracts sanitized text from source files, PDFs, ZIP archives, or GitHub repos."""

    def __init__(self, max_chars: int = 60000) -> None:
        self.max_chars = max_chars

    @staticmethod
    def _should_skip_zip_member(member_name: str) -> bool:
        parts = Path(member_name).parts
        return any(p in IGNORED_DIR_PARTS for p in parts)

    def extract_from_zip_bytes(self, raw_bytes: bytes) -> str:
        chunks: list[str] = []
        total_len = 0
        with zipfile.ZipFile(io.BytesIO(raw_bytes)) as zf:
            ordered = sorted(
                zf.infolist(),
                key=lambda i: (Path(i.filename).suffix.lower() in (".md", ".txt"), i.filename),
            )
            for info in ordered:
                if info.is_dir() or self._should_skip_zip_member(info.filename):
                    continue
                ext = Path(info.filename).suffix.lower()
                if ext not in ALLOWED_TEXT_EXTENSIONS:
                    continue
                data = zf.read(info.filename)
                text = data.decode("utf-8", errors="ignore").strip()
                if not text:
                    continue
                section = f"\n--- FILE: {info.filename} ---\n{text}\n"
                chunks.append(section)
                total_len += len(section)
                if total_len >= self.max_chars:
                    break
        return "".join(chunks)[: self.max_chars]

    def extract_from_file(self, file_path: Path) -> str:
        if not file_path.exists():
            raise HomeworkPipelineError(f"Submission file not found: {file_path}", status_code=404)
        ext = file_path.suffix.lower()
        if ext == ".zip":
            return self.extract_from_zip_bytes(file_path.read_bytes())
        if ext == ".pdf":
            from pypdf import PdfReader

            reader = PdfReader(str(file_path))
            pages = [page.extract_text() or "" for page in reader.pages]
            return "\n".join(pages)[: self.max_chars]

        return file_path.read_text(encoding="utf-8", errors="ignore")[: self.max_chars]

    def extract_from_github_url(self, github_url: str, timeout: float = 8.0) -> str:
        clean = (github_url or "").strip().rstrip("/")
        if not clean.startswith("https://github.com/"):
            raise HomeworkPipelineError(
                "GitHub URL must start with https://github.com/<owner>/<repo>.",
                status_code=400,
            )
        tail = clean.removeprefix("https://github.com/").split("/")
        if len(tail) < 2:
            raise HomeworkPipelineError("Invalid GitHub repository URL.", status_code=400)
        owner, repo = tail[0], tail[1].removesuffix(".git")
        zip_url = f"https://api.github.com/repos/{owner}/{repo}/zipball"
        try:
            resp = requests.get(zip_url, timeout=timeout, allow_redirects=True)
            resp.raise_for_status()
            extracted = self.extract_from_zip_bytes(resp.content)
            if not extracted.strip():
                raise HomeworkPipelineError(
                    "GitHub repository archive contained no readable text/code files.",
                    status_code=400,
                )
            return extracted
        except HomeworkPipelineError:
            raise
        except Exception as exc:
            raise HomeworkPipelineError(
                f"Failed to fetch GitHub repository '{owner}/{repo}': {exc}",
                status_code=502,
            ) from exc

    def extract(
        self,
        content_text: Optional[str] = None,
        file_path: Optional[Path] = None,
        github_url: Optional[str] = None,
    ) -> tuple[str, str, str]:
        """Return (extracted_text, source_type, source_ref)."""
        if content_text and content_text.strip():
            return content_text.strip()[: self.max_chars], "text", "inline://submission"
        if file_path is not None:
            return self.extract_from_file(file_path), "file", str(file_path)
        if github_url and github_url.strip():
            return self.extract_from_github_url(github_url), "github", github_url.strip()
        raise HomeworkPipelineError(
            "Provide code/text, upload a submission file, or enter a GitHub repository URL.",
            status_code=400,
        )


class CohortAggregator:
    """Pure-Python O(1) incremental statistical aggregator (zero LLM calls)."""

    @staticmethod
    def _load_dict(raw: str) -> dict[str, Any]:
        try:
            val = json.loads(raw or "{}")
            return val if isinstance(val, dict) else {}
        except json.JSONDecodeError:
            return {}

    @staticmethod
    def _load_list(raw: str) -> list[float]:
        try:
            val = json.loads(raw or "[]")
            return [float(x) for x in val] if isinstance(val, list) else []
        except (json.JSONDecodeError, TypeError, ValueError):
            return []

    @classmethod
    def apply_metrics(cls, stats: ClassStatsAggregate, metrics: HomeworkMetrics) -> None:
        """Update aggregate mean, topic sums, sorted scores, and error tag frequencies in O(1)."""
        prev_count = int(stats.submission_count or 0)
        prev_total = float(stats.mean_overall_score or 0.0) * prev_count
        new_count = prev_count + 1
        new_score = float(metrics.overall_score)
        stats.submission_count = new_count
        stats.mean_overall_score = round((prev_total + new_score) / new_count, 4)

        topic_sums = cls._load_dict(stats.topic_sums_json)
        for topic, score in metrics.topic_scores.items():
            topic_sums[topic] = round(float(topic_sums.get(topic, 0.0)) + float(score), 4)
        stats.topic_sums_json = json.dumps(topic_sums)

        scores = cls._load_list(stats.scores_list_json)
        bisect.insort(scores, new_score)
        stats.scores_list_json = json.dumps(scores)

        tag_counts = cls._load_dict(stats.error_tag_counts_json)
        for tag in metrics.error_tags:
            clean_tag = str(tag).strip()
            if clean_tag:
                tag_counts[clean_tag] = int(tag_counts.get(clean_tag, 0)) + 1
        stats.error_tag_counts_json = json.dumps(tag_counts)
        stats.updated_at = datetime.now(timezone.utc)

    @classmethod
    def revert_metrics(cls, stats: ClassStatsAggregate, old_metrics: HomeworkMetrics) -> None:
        """Subtract prior student metrics before applying a resubmission."""
        prev_count = int(stats.submission_count or 0)
        if prev_count <= 0:
            return

        old_score = float(old_metrics.overall_score)
        total_before = float(stats.mean_overall_score or 0.0) * prev_count
        new_count = prev_count - 1
        stats.submission_count = new_count
        if new_count == 0:
            stats.mean_overall_score = 0.0
        else:
            stats.mean_overall_score = round(
                max(0.0, (total_before - old_score) / new_count), 4
            )

        topic_sums = cls._load_dict(stats.topic_sums_json)
        for topic, score in old_metrics.topic_scores.items():
            if topic in topic_sums:
                rem = round(float(topic_sums[topic]) - float(score), 4)
                if rem <= 1e-6 or new_count == 0:
                    topic_sums.pop(topic, None)
                else:
                    topic_sums[topic] = rem
        stats.topic_sums_json = json.dumps(topic_sums)

        scores = cls._load_list(stats.scores_list_json)
        if scores:
            idx = bisect.bisect_left(scores, old_score - 1e-4)
            if idx < len(scores) and abs(scores[idx] - old_score) <= 1e-3:
                scores.pop(idx)
            else:
                # Remove nearest match if float serialization drifted slightly
                nearest_idx = min(range(len(scores)), key=lambda i: abs(scores[i] - old_score))
                scores.pop(nearest_idx)
        stats.scores_list_json = json.dumps(scores)

        tag_counts = cls._load_dict(stats.error_tag_counts_json)
        for tag in old_metrics.error_tags:
            clean_tag = str(tag).strip()
            if clean_tag in tag_counts:
                rem_tag = int(tag_counts[clean_tag]) - 1
                if rem_tag <= 0:
                    tag_counts.pop(clean_tag, None)
                else:
                    tag_counts[clean_tag] = rem_tag
        stats.error_tag_counts_json = json.dumps(tag_counts)
        stats.updated_at = datetime.now(timezone.utc)

    @staticmethod
    def compute_percentile(scores_list: list[float], score: float) -> float:
        if not scores_list:
            return 100.0
        sorted_scores = sorted(scores_list)
        rank = bisect.bisect_right(sorted_scores, float(score))
        return round((rank / len(sorted_scores)) * 100.0, 1)

    @classmethod
    def build_anonymized_cohort_snapshot(
        cls, stats: ClassStatsAggregate, student_score: Optional[float] = None
    ) -> dict[str, Any]:
        """Build zero-PII cohort aggregate dictionary (strictly no peer names/emails/code)."""
        count = int(stats.submission_count or 0)
        topic_sums = cls._load_dict(stats.topic_sums_json)
        topic_averages = (
            {
                k: round(float(v) / count, 1)
                for k, v in topic_sums.items()
            }
            if count > 0
            else {}
        )
        scores = cls._load_list(stats.scores_list_json)
        tag_counts = cls._load_dict(stats.error_tag_counts_json)
        top_tags = [
            {"tag": tag, "count": int(cnt)}
            for tag, cnt in sorted(
                tag_counts.items(), key=lambda item: (-int(item[1]), item[0])
            )[:5]
        ]

        percentile = (
            cls.compute_percentile(scores, student_score)
            if student_score is not None and count > 0
            else None
        )
        return {
            "submission_count": count,
            "mean_overall_score": round(float(stats.mean_overall_score or 0.0), 1),
            "student_percentile": percentile,
            "topic_averages": topic_averages,
            "top_error_tags": top_tags,
        }


class HomeworkReviewService:
    """Feature 5 service orchestrating assignment creation, O(1) aggregation, and comparative review."""

    def __init__(
        self,
        db: Session,
        llm_client: Optional[GeminiStudioClient] = None,
        extractor: Optional[SubmissionExtractor] = None,
    ) -> None:
        self.db = db
        self.llm_client = llm_client or GeminiStudioClient()
        self.extractor = extractor or SubmissionExtractor()

    def create_assignment(
        self,
        course_id: int,
        title: str,
        rubric_text: str,
        topics: Optional[list[str]] = None,
    ) -> Assignment:
        course = self.db.get(Course, course_id)
        if course is None:
            raise HomeworkPipelineError("Course not found.", status_code=404)
        clean_title = (title or "").strip()
        clean_rubric = (rubric_text or "").strip()
        if not clean_title or not clean_rubric:
            raise HomeworkPipelineError(
                "Assignment title and rubric are required.", status_code=400
            )
        clean_topics = [str(t).strip() for t in (topics or []) if str(t).strip()]

        assignment = Assignment(
            course_id=course.id,
            title=clean_title,
            rubric_text=clean_rubric,
            topics_json=json.dumps(clean_topics),
        )
        self.db.add(assignment)
        self.db.flush()

        stats = ClassStatsAggregate(assignment_id=assignment.id)
        self.db.add(stats)
        self.db.commit()
        return assignment

    def _ensure_stats(self, assignment_id: int) -> ClassStatsAggregate:
        stats = self.db.get(ClassStatsAggregate, assignment_id)
        if stats is None:
            stats = ClassStatsAggregate(assignment_id=assignment_id)
            self.db.add(stats)
            self.db.flush()
        return stats

    def serialize_submission(
        self, sub: HomeworkSubmission, stats: Optional[ClassStatsAggregate] = None
    ) -> dict[str, Any]:
        student = self.db.get(Student, sub.student_id)
        try:
            metrics = json.loads(sub.metrics_json or "{}")
        except json.JSONDecodeError:
            metrics = {}
        overall_score = metrics.get("overall_score", 0.0)
        cohort_snapshot = (
            CohortAggregator.build_anonymized_cohort_snapshot(
                stats, student_score=float(overall_score)
            )
            if stats is not None
            else None
        )
        return {
            "id": sub.id,
            "assignment_id": sub.assignment_id,
            "student_id": sub.student_id,
            "student_name": student.full_name if student else f"Student #{sub.student_id}",
            "student_email": student.email if student else "",
            "version": sub.version,
            "source_type": sub.source_type,
            "source_ref": sub.source_ref,
            "metrics": metrics,
            "deep_review_md": sub.deep_review_md,
            "cohort_snapshot": cohort_snapshot,
            "submitted_at": sub.submitted_at.strftime("%Y-%m-%d %H:%M UTC"),
        }

    def serialize_assignment(
        self,
        assignment: Assignment,
        student_id: Optional[int] = None,
        include_submissions: bool = False,
    ) -> dict[str, Any]:
        stats = self._ensure_stats(assignment.id)
        cohort = CohortAggregator.build_anonymized_cohort_snapshot(stats)
        try:
            topics = json.loads(assignment.topics_json or "[]")
        except json.JSONDecodeError:
            topics = []

        my_sub = None
        if student_id is not None:
            sub_row = (
                self.db.query(HomeworkSubmission)
                .filter_by(assignment_id=assignment.id, student_id=student_id)
                .one_or_none()
            )
            if sub_row is not None:
                my_sub = self.serialize_submission(sub_row, stats=stats)

        payload: dict[str, Any] = {
            "id": assignment.id,
            "course_id": assignment.course_id,
            "title": assignment.title,
            "rubric_text": assignment.rubric_text,
            "topics": topics,
            "cohort_stats": cohort,
            "my_submission": my_sub,
            "created_at": assignment.created_at.strftime("%Y-%m-%d %H:%M UTC"),
        }
        if include_submissions:
            subs = (
                self.db.query(HomeworkSubmission)
                .filter_by(assignment_id=assignment.id)
                .order_by(HomeworkSubmission.submitted_at.desc())
                .all()
            )
            payload["submissions"] = [self.serialize_submission(s, stats=stats) for s in subs]
        return payload

    def list_course_assignments(
        self, course_id: int, student_id: Optional[int] = None, include_submissions: bool = False
    ) -> list[dict[str, Any]]:
        rows = (
            self.db.query(Assignment)
            .filter_by(course_id=course_id)
            .order_by(Assignment.id.desc())
            .all()
        )
        return [
            self.serialize_assignment(
                a, student_id=student_id, include_submissions=include_submissions
            )
            for a in rows
        ]

    def submit_homework(
        self,
        assignment_id: int,
        student_id: int,
        content_text: Optional[str] = None,
        file_path: Optional[Path] = None,
        github_url: Optional[str] = None,
        generate_review: bool = True,
    ) -> HomeworkSubmission:
        """Stage-1 extraction -> O(1) incremental cohort update -> optional Stage-2 review."""
        assignment = self.db.get(Assignment, assignment_id)
        if assignment is None:
            raise HomeworkPipelineError("Assignment not found.", status_code=404)
        student = self.db.get(Student, student_id)
        if student is None:
            raise HomeworkPipelineError("Student not found.", status_code=404)

        extracted_code, src_type, src_ref = self.extractor.extract(
            content_text=content_text,
            file_path=file_path,
            github_url=github_url,
        )

        stage1_prompt = (
            f"Assignment Title: {assignment.title}\n"
            f"Rubric: {assignment.rubric_text}\n"
            f"Topics: {assignment.topics_json}\n\n"
            f"Student Submission Code/Text:\n{extracted_code}"
        )
        metrics = self.llm_client.generate_structured(
            stage1_prompt,
            HomeworkMetrics,
            system_instruction=(
                "Grade this single submission strictly against the rubric and return "
                "structured HomeworkMetrics JSON."
            ),
            model=self.llm_client.fast_model,
        )

        stats = self._ensure_stats(assignment.id)
        existing = (
            self.db.query(HomeworkSubmission)
            .filter_by(assignment_id=assignment.id, student_id=student.id)
            .one_or_none()
        )
        if existing is not None:
            try:
                old_metrics = HomeworkMetrics.model_validate_json(existing.metrics_json)
                CohortAggregator.revert_metrics(stats, old_metrics)
            except Exception:
                pass
            existing.version += 1
            existing.source_type = src_type
            existing.source_ref = src_ref
            existing.metrics_json = metrics.model_dump_json()
            existing.submitted_at = datetime.now(timezone.utc)
            submission = existing
        else:
            submission = HomeworkSubmission(
                assignment_id=assignment.id,
                student_id=student.id,
                version=1,
                source_type=src_type,
                source_ref=src_ref,
                metrics_json=metrics.model_dump_json(),
                deep_review_md="",
            )
            self.db.add(submission)

        CohortAggregator.apply_metrics(stats, metrics)
        self.db.commit()

        if generate_review:
            self.generate_deep_review(submission.id)

        return submission

    def build_stage2_prompt_payload(self, submission_id: int) -> dict[str, Any]:
        """Construct Stage-2 anonymized payload guaranteed to contain zero peer PII."""
        sub = self.db.get(HomeworkSubmission, submission_id)
        if sub is None:
            raise HomeworkPipelineError("Submission not found.", status_code=404)
        assignment = self.db.get(Assignment, sub.assignment_id)
        stats = self._ensure_stats(sub.assignment_id)
        metrics = HomeworkMetrics.model_validate_json(sub.metrics_json)
        anonymized_cohort = CohortAggregator.build_anonymized_cohort_snapshot(
            stats, student_score=metrics.overall_score
        )
        return {
            "assignment_title": assignment.title if assignment else "",
            "rubric_text": assignment.rubric_text if assignment else "",
            "student_metrics": metrics.model_dump(),
            "anonymized_cohort_snapshot": anonymized_cohort,
        }

    def generate_deep_review(self, submission_id: int) -> str:
        """Stage-2 comparative review comparing student metrics against O(1) cohort stats."""
        sub = self.db.get(HomeworkSubmission, submission_id)
        if sub is None:
            raise HomeworkPipelineError("Submission not found.", status_code=404)

        payload = self.build_stage2_prompt_payload(submission_id)
        metrics = payload["student_metrics"]
        cohort = payload["anonymized_cohort_snapshot"]

        if self.llm_client.is_configured():
            prompt = (
                "Compare ONLY this student's structured metrics against the anonymized class cohort statistics. "
                "Do not speculate about individual classmates.\n\n"
                f"Payload JSON:\n{json.dumps(payload, indent=2)}"
            )
            review_md = self.llm_client.generate_text(
                prompt,
                system_instruction=(
                    "Write an encouraging, actionable university TA feedback report comparing "
                    "the student's rubric scores against the anonymized cohort averages and common pitfalls."
                ),
                model=self.llm_client.fast_model,
            )
        else:
            score = metrics["overall_score"]
            mean = cohort["mean_overall_score"]
            pct = cohort["student_percentile"]
            delta = round(score - mean, 1)
            delta_str = f"+{delta}" if delta >= 0 else f"{delta}"
            topic_lines = "\n".join(
                f"- **{topic}**: You scored `{val}` vs. Class Average `{cohort['topic_averages'].get(topic, val)}`"
                for topic, val in metrics.get("topic_scores", {}).items()
            )
            strengths_md = "\n".join(f"- {s}" for s in metrics.get("strengths", []))
            weaknesses_md = "\n".join(f"- {w}" for w in metrics.get("weaknesses", []))
            pitfalls_md = "\n".join(
                f"- `{item['tag']}` ({item['count']} cohort submission(s))"
                for item in cohort.get("top_error_tags", [])
            ) or "- None recorded yet."

            review_md = (
                f"### Comparative Performance Summary\n"
                f"- **Your Score:** `{score}/100` (**{pct}th percentile** across {cohort['submission_count']} submission(s))\n"
                f"- **Class Mean:** `{mean}/100` (`{delta_str}` pts vs. cohort mean)\n"
                f"- **Requirements Coverage:** `{metrics['requirements_coverage_pct']}%`\n\n"
                f"### Topic Comparison vs. Anonymized Cohort\n{topic_lines}\n\n"
                f"### Key Strengths\n{strengths_md}\n\n"
                f"### Recommended Next Improvements\n{weaknesses_md}\n\n"
                f"### Most Common Class Cohort Error Tags\n{pitfalls_md}"
            )

        sub.deep_review_md = review_md
        self.db.commit()
        return review_md

    def seed_sample_course_assignment(self, course_id: int) -> Assignment:
        """Create a sample OOP assignment and 3 anonymized cohort submissions in 1 click."""
        existing = (
            self.db.query(Assignment)
            .filter_by(
                course_id=course_id,
                title="Ex2 — Object-Oriented Design & Real-Time Architecture",
            )
            .one_or_none()
        )
        if existing is not None:
            return existing

        assignment = self.create_assignment(
            course_id=course_id,
            title="Ex2 — Object-Oriented Design & Real-Time Architecture",
            rubric_text=(
                "Evaluate OOP encapsulation/Adapter design, unit test coverage (pytest), "
                "error handling & path portability, and concurrency modularity."
            ),
            topics=[
                "OOP Design & Patterns",
                "Automated Testing",
                "Code Quality & Error Handling",
                "Architecture Modularity",
            ],
        )

        sample_cohort = [
            (
                "Alice Cohen",
                "alice@ariel.ac.il",
                'class ZoomBridgeAdapter:\n    """Decoupled bridge adapter."""\n    def send(self, msg):\n        try:\n            return True\n        except Exception:\n            return False\n\ndef test_bridge():\n    assert ZoomBridgeAdapter().send("hi") is True\n',
            ),
            (
                "Bob Levi",
                "bob@ariel.ac.il",
                'class AttendanceHandler:\n    """Handles QR checkins."""\n    def check(self):\n        try:\n            return 1\n        except Exception:\n            return 0\n',
            ),
            (
                "Dana Mizrahi",
                "dana@ariel.ac.il",
                'def run_app():\n    path = "C:\\\\Users\\\\dana\\\\data.db"\n    print(path)\n',
            ),
        ]
        for full_name, email, sample_code in sample_cohort:
            st = self.db.query(Student).filter_by(email=email).one_or_none()
            if st is None:
                st = Student(
                    full_name=full_name,
                    email=email,
                    primary_device_id=f"seed-{email.split('@')[0]}",
                )
                self.db.add(st)
                self.db.flush()
            self.submit_homework(
                assignment_id=assignment.id,
                student_id=st.id,
                content_text=sample_code,
                generate_review=True,
            )

        return assignment
