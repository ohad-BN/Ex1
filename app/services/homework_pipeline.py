from __future__ import annotations

import bisect
import io
import json
import logging
import re
import xml.etree.ElementTree as ET
import zipfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

import requests
from pydantic import BaseModel, Field, field_validator
from sqlalchemy import func
from sqlalchemy.orm import Session

from app.models import (
    Assignment,
    ClassStatsAggregate,
    Course,
    HomeworkSubmission,
    Student,
)
from app.services.identity_helpers import is_synthetic_student_email
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


STANDARD_CATEGORIES: tuple[tuple[str, str, str], ...] = (
    (
        "materials_details",
        "Materials & Details",
        "Completeness of deliverables, prompt adherence, documentation, and edge-case details.",
    ),
    (
        "execution_style",
        "Execution Style",
        "Readability, naming conventions, modularity, and structural craftsmanship.",
    ),
    (
        "technical_logic",
        "Technical Logic & Architecture",
        "Functional correctness, architectural patterns, error handling, and portability.",
    ),
    (
        "testing_verification",
        "Testing & Verification",
        "Automated test coverage, assertions, and verification of edge/failure cases.",
    ),
)
STANDARD_CATEGORY_LABELS: tuple[str, ...] = tuple(item[1] for item in STANDARD_CATEGORIES)
STANDARD_CATEGORY_BY_LABEL: dict[str, tuple[str, str]] = {
    label: (key, desc) for key, label, desc in STANDARD_CATEGORIES
}


def format_ordinal(rank: int) -> str:
    """Convert positive integer rank into ordinal English string (1st, 2nd, 3rd, 4th...)."""
    n = max(1, int(rank))
    if 11 <= (n % 100) <= 13:
        suffix = "th"
    else:
        suffix = {1: "st", 2: "nd", 3: "rd"}.get(n % 10, "th")
    return f"{n}{suffix}"


def _normalize_standard_categories(
    raw_scores: dict[str, float],
    raw_notes: dict[str, str],
    overall_score: float,
    topic_scores: dict[str, float],
) -> tuple[dict[str, float], dict[str, str]]:
    """Ensure all 4 standardized comparative categories exist, deriving sensible fallbacks if omitted."""
    fallback_pool = list(topic_scores.values())
    avg_topic = round(sum(fallback_pool) / len(fallback_pool), 1) if fallback_pool else float(overall_score)
    scores_out: dict[str, float] = {}
    notes_out: dict[str, str] = {}

    lower_score_map = {str(k).strip().lower(): float(v) for k, v in raw_scores.items()}
    lower_note_map = {str(k).strip().lower(): str(v).strip() for k, v in raw_notes.items()}

    for key, label, _desc in STANDARD_CATEGORIES:
        val = (
            raw_scores.get(label)
            if label in raw_scores
            else lower_score_map.get(label.lower(), lower_score_map.get(key))
        )
        if val is None:
            val = float(overall_score) if overall_score else avg_topic
        scores_out[label] = round(max(0.0, min(100.0, float(val))), 1)

        note = (
            raw_notes.get(label)
            if label in raw_notes
            else lower_note_map.get(label.lower(), lower_note_map.get(key, ""))
        )
        notes_out[label] = str(note or "").strip()
    return scores_out, notes_out


class TopicScoreItem(BaseModel):
    """Single topic-score pair avoiding JSON-schema additionalProperties for Gemini API."""

    topic: str
    score: float = Field(ge=0.0, le=100.0)


class CategoryEvaluationItem(BaseModel):
    """Structured rubric score and diagnostic note for a standardized homework category."""

    category: str
    score: float = Field(ge=0.0, le=100.0)
    note: str = ""


class HomeworkMetrics(BaseModel):
    """Stage-1 Pydantic schema extracted for a single student submission."""

    overall_score: float = Field(ge=0.0, le=100.0)
    topic_scores: dict[str, float] = Field(default_factory=dict)
    category_scores: dict[str, float] = Field(default_factory=dict)
    category_notes: dict[str, str] = Field(default_factory=dict)
    telemetry: dict[str, float] = Field(default_factory=dict)
    requirements_coverage_pct: float = Field(ge=0.0, le=100.0)
    strengths: list[str] = Field(default_factory=list)
    weaknesses: list[str] = Field(default_factory=list)
    error_tags: list[str] = Field(default_factory=list)
    style_summary: str = ""


class HomeworkMetricsResponse(BaseModel):
    """Gemini-compatible Stage-1 schema mapping topic_scores and categories as arrays."""

    overall_score: float = Field(ge=0.0, le=100.0)
    topic_scores: list[TopicScoreItem] = Field(default_factory=list)
    categories: list[CategoryEvaluationItem] = Field(default_factory=list)
    requirements_coverage_pct: float = Field(ge=0.0, le=100.0)
    strengths: list[str] = Field(default_factory=list)
    weaknesses: list[str] = Field(default_factory=list)
    error_tags: list[str] = Field(default_factory=list)
    style_summary: str = ""

    @field_validator("topic_scores", mode="before")
    @classmethod
    def _coerce_topic_scores(cls, value: Any) -> Any:
        if isinstance(value, dict):
            return [{"topic": str(k), "score": float(v)} for k, v in value.items()]
        return value

    @field_validator("categories", mode="before")
    @classmethod
    def _coerce_categories(cls, value: Any) -> Any:
        if isinstance(value, dict):
            return [
                {"category": str(k), "score": float(v), "note": ""}
                for k, v in value.items()
            ]
        return value

    def to_metrics(self) -> HomeworkMetrics:
        topics = {item.topic: float(item.score) for item in self.topic_scores}
        raw_cat_scores = {item.category: float(item.score) for item in self.categories}
        raw_cat_notes = {item.category: item.note for item in self.categories if item.note}
        cat_scores, cat_notes = _normalize_standard_categories(
            raw_cat_scores, raw_cat_notes, self.overall_score, topics
        )
        return HomeworkMetrics(
            overall_score=self.overall_score,
            topic_scores=topics,
            category_scores=cat_scores,
            category_notes=cat_notes,
            requirements_coverage_pct=self.requirements_coverage_pct,
            strengths=list(self.strengths),
            weaknesses=list(self.weaknesses),
            error_tags=list(self.error_tags),
            style_summary=self.style_summary,
        )


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
        if ext == ".docx":
            try:
                with zipfile.ZipFile(str(file_path)) as zf:
                    doc_xml = zf.read("word/document.xml")
                root = ET.fromstring(doc_xml)
            except Exception as exc:
                raise HomeworkPipelineError("Could not read .docx file.", status_code=400) from exc
            w_ns = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"
            paragraphs: list[str] = []
            for p_node in root.iter(f"{w_ns}p"):
                texts = [t_node.text for t_node in p_node.iter(f"{w_ns}t") if t_node.text]
                line = "".join(texts).strip()
                if line:
                    paragraphs.append(line)
            return "\n".join(paragraphs)[: self.max_chars]

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
        clean_notes = (content_text or "").strip()
        if file_path is not None:
            file_text = self.extract_from_file(file_path).strip()
            combined = (
                f"{file_text}\n\n--- STUDENT NOTES ---\n{clean_notes}".strip()
                if file_text and clean_notes
                else (file_text or clean_notes)
            )
            if not combined:
                raise HomeworkPipelineError(
                    "Uploaded file contained no readable text or supported source files.",
                    status_code=400,
                )
            return combined[: self.max_chars], "file", str(file_path)
        if clean_notes:
            return clean_notes[: self.max_chars], "text", "inline://submission"
        if github_url and github_url.strip():
            return self.extract_from_github_url(github_url), "github", github_url.strip()
        raise HomeworkPipelineError(
            "Provide code/text, upload a submission file, or enter a GitHub repository URL.",
            status_code=400,
        )

    @staticmethod
    def extract_telemetry(extracted_text: str) -> dict[str, float]:
        """Compute deterministic structural metrics (zero LLM calls) for peer comparison."""
        text = extracted_text or ""
        non_empty_lines = [ln for ln in text.splitlines() if ln.strip()]
        structures = re.findall(
            r"(?m)^\s*(?:def |async def |class |public class |function )", text
        )
        assertions = re.findall(
            r"\b(?:assert\w*|expect|describe|test_|pytest|@Test)\b", text
        )
        doc_signals = re.findall(r'("""|\'\'\'|/\*\*|\s#\s|//)', text)
        return {
            "line_count": float(len(non_empty_lines)),
            "char_count": float(len(text)),
            "structure_count": float(len(structures)),
            "assertion_count": float(len(assertions)),
            "doc_signals": float(len(doc_signals)),
        }


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

    @staticmethod
    def _remove_nearest_score(scores: list[float], old_score: float) -> None:
        if not scores:
            return
        idx = bisect.bisect_left(scores, old_score - 1e-4)
        if idx < len(scores) and abs(scores[idx] - old_score) <= 1e-3:
            scores.pop(idx)
        else:
            nearest_idx = min(range(len(scores)), key=lambda i: abs(scores[i] - old_score))
            scores.pop(nearest_idx)

    @classmethod
    def apply_metrics(cls, stats: ClassStatsAggregate, metrics: HomeworkMetrics) -> None:
        """Update aggregate mean, topic sums, category lists, telemetry, and tags in O(1)."""
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

        cat_scores_norm, _ = _normalize_standard_categories(
            metrics.category_scores, metrics.category_notes, new_score, metrics.topic_scores
        )
        cat_lists = cls._load_dict(getattr(stats, "category_scores_json", "{}") or "{}")
        for cat_label, cat_val in cat_scores_norm.items():
            lst = [float(x) for x in cat_lists.get(cat_label, [])]
            bisect.insort(lst, float(cat_val))
            cat_lists[cat_label] = lst
        stats.category_scores_json = json.dumps(cat_lists)

        telem_sums = cls._load_dict(getattr(stats, "telemetry_sums_json", "{}") or "{}")
        for t_key, t_val in metrics.telemetry.items():
            telem_sums[t_key] = round(float(telem_sums.get(t_key, 0.0)) + float(t_val), 4)
        stats.telemetry_sums_json = json.dumps(telem_sums)

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
        cls._remove_nearest_score(scores, old_score)
        stats.scores_list_json = json.dumps(scores)

        old_cat_norm, _ = _normalize_standard_categories(
            old_metrics.category_scores, old_metrics.category_notes, old_score, old_metrics.topic_scores
        )
        cat_lists = cls._load_dict(getattr(stats, "category_scores_json", "{}") or "{}")
        for cat_label, cat_val in old_cat_norm.items():
            raw_lst = cat_lists.get(cat_label)
            if isinstance(raw_lst, list) and raw_lst:
                lst = [float(x) for x in raw_lst]
                cls._remove_nearest_score(lst, float(cat_val))
                if lst and new_count > 0:
                    cat_lists[cat_label] = lst
                else:
                    cat_lists.pop(cat_label, None)
        stats.category_scores_json = json.dumps(cat_lists)

        telem_sums = cls._load_dict(getattr(stats, "telemetry_sums_json", "{}") or "{}")
        for t_key, t_val in old_metrics.telemetry.items():
            if t_key in telem_sums:
                rem_t = round(float(telem_sums[t_key]) - float(t_val), 4)
                if rem_t <= 1e-6 or new_count == 0:
                    telem_sums.pop(t_key, None)
                else:
                    telem_sums[t_key] = rem_t
        stats.telemetry_sums_json = json.dumps(telem_sums)

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
    def compute_relative_class_score(
        scores_list: list[float], score: float
    ) -> dict[str, Any]:
        """Compute peer-only dynamic 1-10 Class Standing & ordinal rank from sorted cohort scores."""
        target = float(score)
        sorted_scores = sorted(float(x) for x in scores_list)
        if not sorted_scores:
            ord_str = format_ordinal(1)
            return {
                "class_score": 10,
                "class_score_precise": 10.0,
                "rank": 1,
                "rank_ordinal": ord_str,
                "rank_label": f"{ord_str} Place",
                "rank_summary": f"{ord_str} / 1",
                "cohort_size": 1,
                "leader_score": round(target, 1),
                "cohort_mean": round(target, 1),
            }

        n = len(sorted_scores)
        s_max = sorted_scores[-1]
        s_min = sorted_scores[0]
        mean = round(sum(sorted_scores) / n, 1)
        rank = 1 + sum(1 for x in sorted_scores if x > target + 1e-6)
        ord_str = format_ordinal(rank)
        rank_label = f"{ord_str} Place"
        rank_summary = f"{ord_str} / {n}"

        if n <= 1 or abs(s_max - s_min) <= 1e-6 or target >= s_max - 1e-6:
            if n >= 2 and abs(s_max - s_min) <= 1e-6 and s_max < 60.0:
                scaled = max(1, min(9, int(round(s_max / 10.0))))
                return {
                    "class_score": scaled,
                    "class_score_precise": round(max(1.0, min(9.9, s_max / 10.0)), 1),
                    "rank": rank,
                    "rank_ordinal": ord_str,
                    "rank_label": rank_label,
                    "rank_summary": rank_summary,
                    "cohort_size": n,
                    "leader_score": round(s_max, 1),
                    "cohort_mean": mean,
                }
            return {
                "class_score": 10,
                "class_score_precise": 10.0,
                "rank": rank,
                "rank_ordinal": ord_str,
                "rank_label": rank_label,
                "rank_summary": rank_summary,
                "cohort_size": n,
                "leader_score": round(s_max, 1),
                "cohort_mean": mean,
            }

        spread = max(35.0, s_max - s_min)
        gap_score = 10.0 - 9.0 * min(1.0, max(0.0, s_max - target) / spread)
        below = sum(1 for x in sorted_scores if x < target - 1e-6)
        equal = sum(1 for x in sorted_scores if abs(x - target) <= 1e-6)
        pct = (below + 0.5 * max(0, equal - 1)) / max(1, n - 1)
        rank_floor = max(1.0, 10.0 - 1.5 * (n - 1))
        ord_score = rank_floor + (10.0 - rank_floor) * pct
        raw = 0.65 * gap_score + 0.35 * ord_score
        return {
            "class_score": max(1, min(9, int(round(raw)))),
            "class_score_precise": round(max(1.0, min(9.9, raw)), 1),
            "rank": rank,
            "rank_ordinal": ord_str,
            "rank_label": rank_label,
            "rank_summary": rank_summary,
            "cohort_size": n,
            "leader_score": round(s_max, 1),
            "cohort_mean": mean,
        }

    @classmethod
    def build_category_comparison(
        cls, stats: ClassStatsAggregate, metrics_dict: dict[str, Any]
    ) -> list[dict[str, Any]]:
        """Build zero-PII per-category relative standing (1-10) + cohort mean/best."""
        overall = float(metrics_dict.get("overall_score", 0.0) or 0.0)
        topic_scores = metrics_dict.get("topic_scores") or {}
        cat_scores, cat_notes = _normalize_standard_categories(
            metrics_dict.get("category_scores") or {},
            metrics_dict.get("category_notes") or {},
            overall,
            topic_scores,
        )
        cat_lists = cls._load_dict(getattr(stats, "category_scores_json", "{}") or "{}")
        overall_list = cls._load_list(stats.scores_list_json)

        out: list[dict[str, Any]] = []
        for key, label, desc in STANDARD_CATEGORIES:
            mine = float(cat_scores[label])
            peer_scores = cat_lists.get(label)
            if not isinstance(peer_scores, list) or not peer_scores:
                peer_scores = overall_list or [mine]
            rel = cls.compute_relative_class_score([float(x) for x in peer_scores], mine)
            out.append(
                {
                    "key": key,
                    "label": label,
                    "description": desc,
                    "ai_rubric_score": round(mine, 1),
                    "class_score": rel["class_score"],
                    "class_score_precise": rel["class_score_precise"],
                    "rank": rel["rank"],
                    "rank_ordinal": rel["rank_ordinal"],
                    "rank_label": rel["rank_label"],
                    "cohort_size": rel["cohort_size"],
                    "cohort_mean": rel["cohort_mean"],
                    "cohort_best": rel["leader_score"],
                    "note": cat_notes.get(label, ""),
                }
            )
        return out

    @classmethod
    def build_telemetry_comparison(
        cls, stats: ClassStatsAggregate, metrics_dict: dict[str, Any]
    ) -> dict[str, dict[str, float]]:
        """Compare deterministic structural metrics of this submission against cohort average."""
        telemetry = metrics_dict.get("telemetry") or {}
        if not telemetry:
            return {}
        count = max(1, int(stats.submission_count or 0))
        telem_sums = cls._load_dict(getattr(stats, "telemetry_sums_json", "{}") or "{}")
        result: dict[str, dict[str, float]] = {}
        for key in ("line_count", "structure_count", "assertion_count", "doc_signals"):
            if key in telemetry:
                mine_val = float(telemetry[key])
                avg_val = round(float(telem_sums.get(key, mine_val)) / count, 1)
                result[key] = {"mine": round(mine_val, 1), "cohort_avg": avg_val}
        return result

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

        cat_lists = cls._load_dict(getattr(stats, "category_scores_json", "{}") or "{}")
        category_averages: dict[str, float] = {}
        category_bests: dict[str, float] = {}
        for _key, label, _desc in STANDARD_CATEGORIES:
            raw_lst = cat_lists.get(label)
            if isinstance(raw_lst, list) and raw_lst:
                vals = [float(x) for x in raw_lst]
                category_averages[label] = round(sum(vals) / len(vals), 1)
                category_bests[label] = round(max(vals), 1)

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
            "category_averages": category_averages,
            "category_bests": category_bests,
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
        if not clean_title:
            raise HomeworkPipelineError("Assignment title is required.", status_code=400)
        clean_topics = [str(t).strip() for t in (topics or []) if str(t).strip()]

        max_num = (
            self.db.query(func.max(Assignment.assignment_number))
            .filter_by(course_id=course.id)
            .scalar()
        )
        if max_num is not None:
            next_num = int(max_num) + 1
        else:
            existing_cnt = (
                self.db.query(Assignment).filter_by(course_id=course.id).count()
            )
            next_num = existing_cnt + 1

        assignment = Assignment(
            course_id=course.id,
            assignment_number=next_num,
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
        self,
        sub: HomeworkSubmission,
        stats: Optional[ClassStatsAggregate] = None,
        *,
        for_lecturer: bool = False,
    ) -> dict[str, Any]:
        student = self.db.get(Student, sub.student_id)
        assignment = self.db.get(Assignment, sub.assignment_id)
        course = (
            self.db.get(Course, assignment.course_id)
            if assignment is not None and assignment.course_id is not None
            else None
        )
        try:
            metrics = json.loads(sub.metrics_json or "{}")
        except json.JSONDecodeError:
            metrics = {}
        overall_score = float(metrics.get("overall_score", 0.0) or 0.0)
        sub_type = (getattr(sub, "submission_type", None) or "comparative").lower()
        has_file = bool(sub.source_type == "file" and sub.source_ref)
        file_name = Path(sub.source_ref).name if has_file else ""
        download_url = f"/api/submissions/{sub.id}/download" if has_file else ""

        if for_lecturer or sub_type == "final":
            cohort_snapshot = None
            class_standing = None
            category_breakdown: list[dict[str, Any]] = []
            telemetry_comparison: dict[str, dict[str, float]] = {}
        else:
            cohort_snapshot = (
                CohortAggregator.build_anonymized_cohort_snapshot(
                    stats, student_score=overall_score
                )
                if stats is not None
                else None
            )
            scores_list = (
                CohortAggregator._load_list(stats.scores_list_json)
                if stats is not None
                else [overall_score]
            )
            class_standing = CohortAggregator.compute_relative_class_score(
                scores_list or [overall_score], overall_score
            )
            category_breakdown = (
                CohortAggregator.build_category_comparison(stats, metrics)
                if stats is not None
                else []
            )
            telemetry_comparison = (
                CohortAggregator.build_telemetry_comparison(stats, metrics)
                if stats is not None
                else {}
            )

        return {
            "id": sub.id,
            "assignment_id": sub.assignment_id,
            "course_id": assignment.course_id if assignment is not None else None,
            "course_name": course.name if course is not None else "",
            "assignment_number": int(assignment.assignment_number or 1) if assignment is not None else 1,
            "assignment_title": assignment.title if assignment is not None else "",
            "submission_type": sub_type,
            "student_id": sub.student_id,
            "student_name": student.full_name if student else f"Student #{sub.student_id}",
            "student_email": (
                ""
                if (student and is_synthetic_student_email(student.email))
                else (student.email if student else "")
            ),
            "version": sub.version,
            "source_type": sub.source_type,
            "source_ref": sub.source_ref,
            "file_name": file_name,
            "has_file": has_file,
            "download_url": download_url,
            "metrics": metrics,
            "ai_rubric_grade": round(overall_score, 1),
            "class_standing": class_standing,
            "category_breakdown": category_breakdown,
            "telemetry_comparison": telemetry_comparison,
            "deep_review_md": sub.deep_review_md,
            "cohort_snapshot": cohort_snapshot,
            "submitted_at": sub.submitted_at.strftime("%Y-%m-%d %H:%M UTC"),
        }

    def serialize_assignment(
        self,
        assignment: Assignment,
        student_id: Optional[int] = None,
        include_submissions: bool = False,
        for_lecturer: bool = False,
    ) -> dict[str, Any]:
        stats = self._ensure_stats(assignment.id)
        cohort = (
            {
                "submission_count": 0,
                "mean_overall_score": 0.0,
                "student_percentile": None,
                "topic_averages": {},
                "category_averages": {},
                "category_bests": {},
                "top_error_tags": [],
            }
            if for_lecturer
            else CohortAggregator.build_anonymized_cohort_snapshot(stats)
        )
        try:
            topics = json.loads(assignment.topics_json or "[]")
        except json.JSONDecodeError:
            topics = []

        my_comp = None
        my_final = None
        if student_id is not None:
            comp_row = (
                self.db.query(HomeworkSubmission)
                .filter_by(
                    assignment_id=assignment.id,
                    student_id=student_id,
                    submission_type="comparative",
                )
                .one_or_none()
            )
            if comp_row is not None:
                my_comp = self.serialize_submission(comp_row, stats=stats, for_lecturer=False)

            final_row = (
                self.db.query(HomeworkSubmission)
                .filter_by(
                    assignment_id=assignment.id,
                    student_id=student_id,
                    submission_type="final",
                )
                .one_or_none()
            )
            if final_row is not None:
                my_final = self.serialize_submission(final_row, stats=None, for_lecturer=False)

        final_subs_all = (
            self.db.query(HomeworkSubmission)
            .filter_by(assignment_id=assignment.id, submission_type="final")
            .order_by(HomeworkSubmission.submitted_at.desc())
            .all()
        )
        final_scores: list[float] = []
        for fs_row in final_subs_all:
            try:
                m_obj = json.loads(fs_row.metrics_json or "{}")
                final_scores.append(float(m_obj.get("overall_score", 0.0) or 0.0))
            except (json.JSONDecodeError, TypeError, ValueError):
                pass
        final_mean = round(sum(final_scores) / len(final_scores), 1) if final_scores else 0.0

        course = (
            self.db.get(Course, assignment.course_id)
            if assignment.course_id is not None
            else None
        )
        payload: dict[str, Any] = {
            "id": assignment.id,
            "course_id": assignment.course_id,
            "course_name": course.name if course is not None else "",
            "assignment_number": int(assignment.assignment_number or 1),
            "title": assignment.title,
            "rubric_text": assignment.rubric_text,
            "topics": topics,
            "cohort_stats": cohort,
            "final_submission_count": len(final_subs_all),
            "final_mean_score": final_mean,
            "my_comparative_submission": my_comp,
            "my_final_submission": my_final,
            "my_submission": my_comp or my_final,
            "created_at": assignment.created_at.strftime("%Y-%m-%d %H:%M UTC"),
        }
        if include_submissions:
            if for_lecturer:
                # Strict separation: Lecturers see ONLY explicit Final Submissions with standalone AI Score
                payload["submissions"] = [
                    self.serialize_submission(s, stats=None, for_lecturer=True)
                    for s in final_subs_all
                ]
            else:
                subs = (
                    self.db.query(HomeworkSubmission)
                    .filter_by(assignment_id=assignment.id)
                    .order_by(HomeworkSubmission.submitted_at.desc())
                    .all()
                )
                payload["submissions"] = [self.serialize_submission(s, stats=stats) for s in subs]
        return payload

    def list_course_assignments(
        self,
        course_id: int,
        student_id: Optional[int] = None,
        include_submissions: bool = False,
        for_lecturer: bool = False,
    ) -> list[dict[str, Any]]:
        rows = (
            self.db.query(Assignment)
            .filter(Assignment.course_id == int(course_id))
            .order_by(Assignment.assignment_number.asc(), Assignment.id.asc())
            .all()
        )
        return [
            self.serialize_assignment(
                a,
                student_id=student_id,
                include_submissions=include_submissions,
                for_lecturer=for_lecturer,
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
        submission_type: str = "comparative",
    ) -> HomeworkSubmission:
        """Stage-1 extraction -> comparative (student-only) OR final (lecturer-facing) submission."""
        clean_type = (
            "final"
            if str(submission_type or "").strip().lower() == "final"
            else "comparative"
        )
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

        cat_instructions = "\n".join(
            f"- {label}: {desc}" for _k, label, desc in STANDARD_CATEGORIES
        )
        stage1_prompt = (
            f"Assignment Title: {assignment.title}\n"
            f"Rubric: {assignment.rubric_text}\n"
            f"Topics: {assignment.topics_json}\n\n"
            f"Standardized Evaluation Categories (populate ALL 4 inside `categories` with 0-100 score + 1-sentence note):\n"
            f"{cat_instructions}\n\n"
            f"Student Submission Code/Text:\n{extracted_code}"
        )
        metrics_resp = self.llm_client.generate_structured(
            stage1_prompt,
            HomeworkMetricsResponse,
            system_instruction=(
                "Grade this student submission rigorously and realistically across the full 0-100 scale based ONLY "
                "on the actual submitted code/text relative to the assignment title and rubric. Do NOT inflate scores: "
                "empty/stub/trivial or off-topic submissions must score 15-49, partial solutions 50-74, solid solutions 75-87, "
                "and only comprehensive, clean, tested solutions 88-98. Populate both assignment topic_scores and all 4 "
                "standardized categories ('Materials & Details', 'Execution Style', 'Technical Logic & Architecture', "
                "'Testing & Verification') with concise 1-sentence diagnostic notes in HomeworkMetricsResponse JSON."
            ),
            model=self.llm_client.fast_model,
        )
        metrics = metrics_resp.to_metrics()
        metrics.telemetry = self.extractor.extract_telemetry(extracted_code)

        stats = self._ensure_stats(assignment.id)
        existing = (
            self.db.query(HomeworkSubmission)
            .filter_by(
                assignment_id=assignment.id,
                student_id=student.id,
                submission_type=clean_type,
            )
            .one_or_none()
        )
        if existing is not None:
            if clean_type == "comparative":
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
                submission_type=clean_type,
                version=1,
                source_type=src_type,
                source_ref=src_ref,
                metrics_json=metrics.model_dump_json(),
                deep_review_md="",
            )
            self.db.add(submission)

        if clean_type == "comparative":
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
        scores_list = CohortAggregator._load_list(stats.scores_list_json) or [
            metrics.overall_score
        ]
        class_standing = CohortAggregator.compute_relative_class_score(
            scores_list, metrics.overall_score
        )
        category_breakdown = CohortAggregator.build_category_comparison(
            stats, metrics.model_dump()
        )
        telemetry_comparison = CohortAggregator.build_telemetry_comparison(
            stats, metrics.model_dump()
        )
        return {
            "assignment_title": assignment.title if assignment else "",
            "rubric_text": assignment.rubric_text if assignment else "",
            "student_metrics": metrics.model_dump(),
            "class_standing": class_standing,
            "category_breakdown": category_breakdown,
            "telemetry_comparison": telemetry_comparison,
            "anonymized_cohort_snapshot": anonymized_cohort,
        }

    def generate_deep_review(self, submission_id: int) -> str:
        """Stage-2 review: standalone rubric evaluation for Final submissions, comparative ranking for Practice."""
        sub = self.db.get(HomeworkSubmission, submission_id)
        if sub is None:
            raise HomeworkPipelineError("Submission not found.", status_code=404)

        sub_type = (getattr(sub, "submission_type", None) or "comparative").lower()
        if sub_type == "final":
            assignment = self.db.get(Assignment, sub.assignment_id)
            metrics_obj = HomeworkMetrics.model_validate_json(sub.metrics_json)
            metrics = metrics_obj.model_dump()
            score = metrics["overall_score"]
            if self.llm_client.is_configured():
                standalone_payload = {
                    "assignment_title": assignment.title if assignment else "",
                    "rubric_text": assignment.rubric_text if assignment else "",
                    "student_metrics": metrics,
                }
                prompt = (
                    "Write a SHORT, direct standalone evaluation for the lecturer and student (MAX 110 words total). "
                    "Do NOT mention class ranking, percentiles, or comparisons against classmates. Use ONLY these 3 Markdown sections:\n"
                    "### Summary\n(1-2 crisp sentences evaluating submission quality vs. the assignment)\n"
                    "### What Worked\n(2 short bullet points)\n"
                    "### Areas to Improve\n(2-3 concrete bullet points)\n\n"
                    f"Payload JSON:\n{json.dumps(standalone_payload, indent=2)}"
                )
                review_md = self.llm_client.generate_text(
                    prompt,
                    system_instruction=(
                        "You are an objective university grader writing a standalone AI evaluation. "
                        "Never include peer comparisons or class ranks."
                    ),
                    model=self.llm_client.fast_model,
                )
            else:
                strengths_md = "\n".join(f"- {s}" for s in metrics.get("strengths", [])[:2])
                weaknesses_md = "\n".join(f"- {w}" for w in metrics.get("weaknesses", [])[:2])
                review_md = (
                    f"### Standalone AI Evaluation\n"
                    f"AI Score **{score}/100** (Coverage {metrics.get('requirements_coverage_pct', 0)}%). {metrics.get('style_summary', '')}\n\n"
                    f"### What Worked\n{strengths_md}\n\n"
                    f"### Areas to Improve\n{weaknesses_md}"
                )
            sub.deep_review_md = review_md
            self.db.commit()
            return review_md

        payload = self.build_stage2_prompt_payload(submission_id)
        metrics = payload["student_metrics"]
        cohort = payload["anonymized_cohort_snapshot"]
        standing = payload["class_standing"]
        categories = payload["category_breakdown"]

        if self.llm_client.is_configured():
            prompt = (
                "Write a SHORT, direct, scannable student review (MAX 110 words total). "
                "The UI already displays the 0-100 AI Rubric Grade, ordinal Class Rank (1st, 2nd...), and Category bars — "
                "so do NOT repeat big numeric tables. Use ONLY these 3 compact Markdown sections:\n"
                "### Summary\n(1-2 crisp sentences explaining where they stand vs classmates)\n"
                "### What Worked\n(2 short bullet points)\n"
                "### Fix Next\n(2-3 high-impact bullets starting with their lowest category)\n"
                "Do not speculate about individual classmates.\n\n"
                f"Payload JSON:\n{json.dumps(payload, indent=2)}"
            )
            review_md = self.llm_client.generate_text(
                prompt,
                system_instruction=(
                    "You are a concise university TA. Be crisp, encouraging, and to the point (<= 110 words). "
                    "Use only simple bullets and ### headings; zero filler."
                ),
                model=self.llm_client.fast_model,
            )
        else:
            score = metrics["overall_score"]
            mean = cohort["mean_overall_score"]
            delta = round(score - mean, 1)
            delta_str = f"+{delta}" if delta >= 0 else f"{delta}"
            weakest = sorted(categories, key=lambda c: c["ai_rubric_score"])[0] if categories else None
            strengths_md = "\n".join(f"- {s}" for s in metrics.get("strengths", [])[:2])
            weaknesses_md = "\n".join(f"- {w}" for w in metrics.get("weaknesses", [])[:2])
            top_tag = cohort.get("top_error_tags", [])
            pitfall_line = (
                f"- Watch out for `{top_tag[0]['tag']}` (common in {top_tag[0]['count']} submission(s))."
                if top_tag
                else ""
            )
            ord_rank = standing.get("rank_label") or format_ordinal(standing.get("rank", 1))
            focus_line = (
                f"Priority focus: **{weakest['label']}** (`{weakest.get('rank_ordinal') or format_ordinal(weakest.get('rank', 1))}` of `{weakest['cohort_size']}`)."
                if weakest
                else ""
            )

            review_md = (
                f"### Comparative Performance Summary\n"
                f"AI Score **{score}/100** (`{delta_str}` vs. class avg `{mean}`) &bull; Class Rank **{ord_rank}** (#{standing['rank']} of {standing['cohort_size']}). {focus_line}\n\n"
                f"### What Worked\n{strengths_md}\n\n"
                f"### Fix Next\n{weaknesses_md}"
                + (f"\n{pitfall_line}" if pitfall_line else "")
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
            rubric_text="",
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
                'class ReportExporter:\n    """Decoupled spreadsheet exporter."""\n    def export(self, rows):\n        try:\n            return len(rows) > 0\n        except Exception:\n            return False\n\ndef test_exporter():\n    assert ReportExporter().export(["row1"]) is True\n',
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
                submission_type="comparative",
            )
            self.submit_homework(
                assignment_id=assignment.id,
                student_id=st.id,
                content_text=sample_code,
                generate_review=True,
                submission_type="final",
            )

        return assignment
