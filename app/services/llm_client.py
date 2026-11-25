from __future__ import annotations

import json
import logging
from pathlib import Path
import re
import threading
import time
from typing import Any, Callable, Optional, TypeVar

from pydantic import BaseModel

logger = logging.getLogger(__name__)

T = TypeVar("T", bound=BaseModel)


def _is_transient_rate_or_server_error(exc: Exception) -> bool:
    code = getattr(exc, "code", None) or getattr(exc, "status_code", None)
    if code in (429, 503):
        return True
    msg = str(exc).lower()
    return any(
        token in msg
        for token in (
            "429",
            "503",
            "rate limit",
            "resource_exhausted",
            "unavailable",
            "too many requests",
        )
    )


def _is_daily_quota_exhausted(exc: Exception) -> bool:
    """Return True when the error clearly signals a daily quota exhaustion (fail over immediately)."""
    msg = str(exc).lower()
    if "perday" in msg or "generaterequestsperday" in msg:
        return True
    m = re.search(r"retry\s*(?:in|delay)[\"']?\s*[:=]?\s*[\"']?(\d+)", msg)
    if m and int(m.group(1)) >= 60:
        return True
    return False


def _is_model_not_found_error(exc: Exception) -> bool:
    code = getattr(exc, "code", None) or getattr(exc, "status_code", None)
    if code == 404:
        return True
    msg = str(exc).lower()
    return "404" in msg or "not_found" in msg or "no longer available" in msg


class GeminiStudioClient:
    """Thread-safe Google AI Studio client with RPM pacing, 429/503 backoff, and local fallback."""

    FALLBACK_MODELS: tuple[str, ...] = (
        "gemini-2.5-flash-lite",
        "gemini-2.5-flash",
        "gemini-3.5-flash-lite",
        "gemini-3.5-flash",
        "gemini-3.1-flash-lite",
        "gemini-2.5-pro",
    )

    def __init__(
        self,
        api_key: str = "",
        fast_model: str = "gemini-2.5-flash-lite",
        pro_model: str = "gemini-2.5-flash-lite",
        min_interval_sec: float = 1.5,
        backoff_base_sec: float = 2.0,
        max_retries: int = 3,
        fallback_on_error: bool = True,
        client_override: Optional[Any] = None,
        clock_fn: Callable[[], float] = time.monotonic,
        sleep_fn: Callable[[float], None] = time.sleep,
    ) -> None:
        self.api_key = (api_key or "").strip()
        self.fast_model = fast_model
        self.pro_model = pro_model
        self.min_interval_sec = max(0.0, float(min_interval_sec))
        self.backoff_base_sec = max(0.0, float(backoff_base_sec))
        self.max_retries = max(0, int(max_retries))
        self.fallback_on_error = fallback_on_error
        self._client_override = client_override
        self._client_instance: Optional[Any] = client_override
        self._clock = clock_fn
        self._sleep = sleep_fn
        self._pace_lock = threading.Lock()
        self._last_call_ts: Optional[float] = None

    def is_configured(self) -> bool:
        return bool(self.api_key or self._client_override is not None)

    def _get_genai_client(self) -> Any:
        if self._client_instance is not None:
            return self._client_instance
        from google import genai  # local import so app boots cleanly offline

        self._client_instance = genai.Client(
            api_key=self.api_key,
            http_options={"api_version": "v1beta"},
        )
        return self._client_instance

    def _wait_for_pacing_slot(self) -> None:
        if self.min_interval_sec <= 0:
            return
        with self._pace_lock:
            now = self._clock()
            if self._last_call_ts is not None:
                elapsed = now - self._last_call_ts
                remaining = self.min_interval_sec - elapsed
                if remaining > 0:
                    self._sleep(remaining)
            self._last_call_ts = self._clock()

    def _call_with_retry(self, fn: Callable[[], Any]) -> Any:
        attempt = 0
        while True:
            self._wait_for_pacing_slot()
            try:
                return fn()
            except Exception as exc:
                if _is_daily_quota_exhausted(exc):
                    logger.warning("Gemini daily quota exhausted on active model; switching immediately: %s", exc)
                    raise
                if _is_transient_rate_or_server_error(exc) and attempt < self.max_retries:
                    delay = self.backoff_base_sec * (2**attempt)
                    logger.warning(
                        "Gemini API transient error (attempt %d/%d): %s. Sleeping %.1fs.",
                        attempt + 1,
                        self.max_retries,
                        exc,
                        delay,
                    )
                    if delay > 0:
                        self._sleep(delay)
                    attempt += 1
                    continue
                raise

    def _generate_with_model_fallback(
        self,
        requested_model: str,
        call_for_model: Callable[[Any, str], Any],
    ) -> Any:
        client = self._get_genai_client()
        candidates = [requested_model] + [
            m for m in self.FALLBACK_MODELS if m != requested_model
        ]
        last_exc: Optional[Exception] = None
        for idx, candidate in enumerate(candidates):
            try:
                res = self._call_with_retry(lambda m=candidate: call_for_model(client, m))
                if idx > 0:
                    logger.info("Switched unavailable Gemini model %s -> %s", requested_model, candidate)
                    if requested_model == self.fast_model:
                        self.fast_model = candidate
                    if requested_model == self.pro_model:
                        self.pro_model = candidate
                return res
            except Exception as exc:
                last_exc = exc
                if (
                    _is_model_not_found_error(exc) or _is_transient_rate_or_server_error(exc)
                ) and idx < len(candidates) - 1:
                    logger.warning(
                        "Gemini model %s unavailable/rate-limited (%s); switching to next fallback.",
                        candidate,
                        type(exc).__name__,
                    )
                    continue
                raise
        if last_exc is not None:
            raise last_exc
        raise RuntimeError("No Gemini candidate models available.")

    def generate_text(
        self,
        prompt: str,
        system_instruction: str = "",
        model: Optional[str] = None,
        allow_fallback: bool = True,
    ) -> str:
        """Generate free-form Markdown/text via Gemini or deterministic local synthesis."""
        if not self.is_configured():
            if not allow_fallback:
                raise RuntimeError("Gemini API key is not configured.")
            return self._local_text_fallback(prompt, system_instruction)

        target_model = model or self.fast_model

        def _invoke(client: Any, model_name: str) -> Any:
            config: dict[str, Any] = {}
            if system_instruction:
                config["system_instruction"] = system_instruction
            return client.models.generate_content(
                model=model_name,
                contents=prompt,
                config=config or None,
            )

        try:
            response = self._generate_with_model_fallback(target_model, _invoke)
            text = getattr(response, "text", None)
            if text and str(text).strip():
                return str(text).strip()
            if not allow_fallback:
                raise RuntimeError("Gemini returned an empty text response.")
            return self._local_text_fallback(prompt, system_instruction)
        except Exception as exc:
            logger.error("Gemini generate_text failed; using fallback=%s: %s", self.fallback_on_error and allow_fallback, exc)
            if not self.fallback_on_error or not allow_fallback:
                raise
            return self._local_text_fallback(prompt, system_instruction)

    def generate_structured(
        self,
        prompt: str,
        schema_cls: type[T],
        system_instruction: str = "",
        model: Optional[str] = None,
    ) -> T:
        """Generate structured JSON matching a Pydantic schema via Gemini or local analyzer."""
        if not self.is_configured():
            return self._local_structured_fallback(prompt, schema_cls)

        target_model = model or self.fast_model

        def _invoke(client: Any, model_name: str) -> Any:
            config: dict[str, Any] = {
                "response_mime_type": "application/json",
                "response_schema": schema_cls,
            }
            if system_instruction:
                config["system_instruction"] = system_instruction
            return client.models.generate_content(
                model=model_name,
                contents=prompt,
                config=config,
            )

        try:
            response = self._generate_with_model_fallback(target_model, _invoke)
            parsed = getattr(response, "parsed", None)
            if isinstance(parsed, schema_cls):
                return parsed
            if isinstance(parsed, dict):
                return schema_cls.model_validate(parsed)
            raw_text = getattr(response, "text", None)
            if raw_text:
                return schema_cls.model_validate_json(str(raw_text))
            return self._local_structured_fallback(prompt, schema_cls)
        except Exception as exc:
            logger.error(
                "Gemini generate_structured failed; using fallback=%s: %s",
                self.fallback_on_error,
                exc,
            )
            if not self.fallback_on_error:
                raise
            return self._local_structured_fallback(prompt, schema_cls)

    AUDIO_MIME_TYPES: dict[str, str] = {
        ".mp3": "audio/mpeg",
        ".wav": "audio/wav",
        ".m4a": "audio/mp4",
        ".mp4": "video/mp4",
        ".mov": "video/quicktime",
        ".mkv": "video/x-matroska",
        ".avi": "video/x-msvideo",
        ".m4v": "video/x-m4v",
        ".ogg": "audio/ogg",
        ".flac": "audio/flac",
        ".webm": "video/webm",
        ".aac": "audio/aac",
    }

    def transcribe_audio_file(
        self,
        audio_path: Path,
        prompt: str,
        system_instruction: str = "",
        model: Optional[str] = None,
    ) -> str:
        """Transcribe a class recording using Google Gemini API (inline bytes or Files API)."""
        if not self.is_configured():
            raise RuntimeError("Gemini API key is not configured.")

        target_model = model or self.fast_model
        client = self._get_genai_client()
        mime_type = self.AUDIO_MIME_TYPES.get(audio_path.suffix.lower(), "audio/mpeg")
        uploaded_file: Any = None
        file_size = audio_path.stat().st_size if audio_path.exists() else 0

        try:
            media_part: Any = None
            prefer_inline = self._client_override is None and file_size <= 15 * 1024 * 1024
            if prefer_inline:
                try:
                    from google.genai import types

                    media_part = types.Part.from_bytes(
                        data=audio_path.read_bytes(),
                        mime_type=mime_type,
                    )
                except Exception:
                    media_part = None

            if media_part is None and hasattr(client, "files"):
                try:
                    uploaded_file = client.files.upload(
                        file=str(audio_path),
                        config={"mime_type": mime_type},
                    )
                except TypeError:
                    uploaded_file = client.files.upload(file=str(audio_path))
                for _ in range(40):
                    state_obj = getattr(uploaded_file, "state", None)
                    state_name = str(getattr(state_obj, "name", state_obj or "")).upper()
                    if state_name and "PROCESSING" not in state_name:
                        break
                    self._sleep(1.5)
                    uploaded_file = client.files.get(name=uploaded_file.name)
                media_part = uploaded_file
            elif media_part is None:
                from google.genai import types

                media_part = types.Part.from_bytes(
                    data=audio_path.read_bytes(),
                    mime_type=mime_type,
                )

            def _invoke(cl: Any, model_name: str) -> Any:
                config: dict[str, Any] = {}
                if system_instruction:
                    config["system_instruction"] = system_instruction
                return cl.models.generate_content(
                    model=model_name,
                    contents=[media_part, prompt],
                    config=config or None,
                )

            response = self._generate_with_model_fallback(target_model, _invoke)
            text = getattr(response, "text", None)
            if not text or not str(text).strip():
                raise RuntimeError("Gemini returned an empty transcription.")
            return str(text).strip()
        finally:
            if uploaded_file is not None and hasattr(client, "files"):
                try:
                    client.files.delete(name=uploaded_file.name)
                except Exception:  # noqa: BLE001
                    pass

    @staticmethod
    def _local_text_fallback(prompt: str, system_instruction: str) -> str:
        """Offline fallback text generator preserving structured sections when API key is unset."""
        return (
            f"### Automated Synthesis\n\n"
            f"Generated locally (no external Gemini quota consumed).\n\n"
            f"{prompt[:1500]}"
        )

    @staticmethod
    def _local_structured_fallback(prompt: str, schema_cls: type[T]) -> T:
        """Deterministic content-aware analyzer fallback for HomeworkMetrics grading."""
        field_names = set(schema_cls.model_fields.keys())
        if {"overall_score", "topic_scores", "error_tags"}.issubset(field_names):
            marker = "Student Submission Code/Text:"
            if marker in prompt:
                header_part, sub_raw = prompt.split(marker, 1)
            else:
                header_part, sub_raw = "", prompt
            submission_text = sub_raw.strip()
            lower = submission_text.lower()
            lines = [ln for ln in submission_text.splitlines() if ln.strip()]
            non_trivial_lines = [
                ln for ln in lines
                if not ln.strip().startswith(("#", "//", "/*", "*"))
                and ln.strip() not in ("pass", "...", "TODO", "todo")
            ]

            char_len = len(submission_text)
            line_cnt = len(non_trivial_lines)
            func_or_cls = re.findall(
                r"(?m)^\s*(?:def |async def |class |public class |function )", submission_text
            )
            has_oop = bool(re.search(r"(?m)^\s*(?:class |public class |interface )", submission_text)) or ("abc" in lower and "class " in lower)
            has_tests = bool(re.findall(r"\b(?:pytest|unittest|def test_|assert\s+|@test)\b", lower))
            test_count = len(re.findall(r"\b(?:def test_|assert\s+|@test)\b", lower))
            has_docstrings = ('"""' in submission_text or "'''" in submission_text or "/**" in submission_text)
            comment_lines = sum(1 for ln in lines if ln.strip().startswith(("#", "//", "*")))
            has_error_handling = ("try:" in lower and "except" in lower) or bool(
                re.search(r"\b(?:raise\s+\w+Error|throw\s+new\s+|if\s+not\s+\w+:)\b", lower)
            )
            has_control_flow = bool(re.search(r"\b(?:for |while |if |return |yield |switch )\b", lower))
            is_stub = (
                char_len < 60
                or line_cnt <= 2
                or bool(re.search(r"\b(?:todo|notimplementederror)\b", lower)) and line_cnt < 6
            )
            hardcoded_path = bool(re.search(r"[a-zA-Z]:\\\\|/Users/|/home/", submission_text))

            # Rubric / title keyword alignment
            stop_words = {
                "assignment", "title", "rubric", "topics", "evaluate", "grade", "submission",
                "write", "using", "with", "from", "that", "this", "the", "and", "for", "into",
            }
            req_tokens = {
                tok for tok in re.findall(r"[a-z]{4,}", header_part.lower()) if tok not in stop_words
            }
            if req_tokens:
                matched_tokens = sum(1 for tok in req_tokens if tok in lower)
                relevance_ratio = min(1.0, matched_tokens / max(2, min(6, len(req_tokens))))
            else:
                relevance_ratio = 0.65 if char_len > 150 else 0.35

            # Score Materials & Details (0-100)
            details_score = 32.0
            details_score += min(28.0, line_cnt * 1.4)
            details_score += 18.0 * relevance_ratio
            if has_docstrings:
                details_score += 12.0
            elif comment_lines >= 2:
                details_score += 6.0
            if is_stub:
                details_score = min(details_score, 44.0)
            if hardcoded_path:
                details_score -= 10.0
            details_score = round(max(15.0, min(97.0, details_score)), 1)

            # Score Execution Style (0-100)
            clean_score = 38.0
            clean_score += min(18.0, len(func_or_cls) * 5.0)
            if has_docstrings or comment_lines >= 2:
                clean_score += 18.0
            elif comment_lines == 1:
                clean_score += 8.0
            if line_cnt >= 6:
                clean_score += 10.0
            if line_cnt >= 14:
                clean_score += 8.0
            if is_stub:
                clean_score = min(clean_score, 46.0)
            if hardcoded_path:
                clean_score -= 18.0
            clean_score = round(max(18.0, min(97.0, clean_score)), 1)

            # Score Technical Logic & Architecture (0-100)
            arch_score = 34.0
            if has_control_flow:
                arch_score += 14.0
            if len(func_or_cls) >= 2:
                arch_score += 12.0
            elif len(func_or_cls) == 1:
                arch_score += 6.0
            if has_oop:
                arch_score += 14.0
            if has_error_handling:
                arch_score += 16.0
            if is_stub:
                arch_score = min(arch_score, 42.0)
            arch_score = round(max(18.0, min(97.0, arch_score)), 1)

            # Score Testing & Verification (0-100)
            if has_tests:
                test_score = round(min(96.0, 68.0 + min(24.0, test_count * 7.0) + (4.0 if has_error_handling else 0.0)), 1)
            elif line_cnt >= 10 and has_error_handling:
                test_score = 48.0
            elif line_cnt >= 4:
                test_score = 36.0
            else:
                test_score = 22.0

            oop_score = arch_score
            overall = round(
                (details_score * 0.25)
                + (clean_score * 0.25)
                + (arch_score * 0.30)
                + (test_score * 0.20),
                1,
            )

            strengths: list[str] = []
            weaknesses: list[str] = []
            error_tags: list[str] = []

            if is_stub:
                weaknesses.append("Submission is minimal or incomplete relative to the assignment requirements.")
                error_tags.append("incomplete_submission")
            if has_oop:
                strengths.append("Modular class decomposition and encapsulated responsibilities.")
            elif line_cnt >= 3:
                weaknesses.append("Uses script/procedural structure without cohesive abstractions.")
                error_tags.append("low_modularity")

            if has_tests:
                strengths.append(f"Includes automated verification/assertions ({test_count} test check(s)).")
            else:
                weaknesses.append("Missing automated unit tests or verification assertions.")
                error_tags.append("missing_edge_case_tests")

            if has_error_handling:
                strengths.append("Guards against edge cases or invalid inputs explicitly.")
            else:
                weaknesses.append("Insufficient error handling or validation for edge/failure inputs.")
                error_tags.append("missing_error_handling")

            if has_docstrings:
                strengths.append("Clear docstrings/documentation explaining component behavior.")
            if hardcoded_path:
                weaknesses.append("Contains machine-specific hardcoded filesystem paths.")
                error_tags.append("hardcoded_paths")

            if not strengths:
                strengths.append("Readable baseline syntax submitted successfully.")

            coverage = round(
                max(
                    15.0,
                    min(
                        98.0,
                        24.0
                        + min(26.0, line_cnt * 1.3)
                        + (14.0 if has_oop else 0.0)
                        + (16.0 if has_tests else 0.0)
                        + (12.0 if has_error_handling else 0.0)
                        + (10.0 * relevance_ratio),
                    ),
                ),
                1,
            )
            categories_payload = [
                {
                    "category": "Materials & Details",
                    "score": details_score,
                    "note": (
                        "Covers deliverables with clear documentation and detail."
                        if has_docstrings and not is_stub and not hardcoded_path
                        else "Expand implementation completeness, docstrings, and requirement coverage."
                    ),
                },
                {
                    "category": "Execution Style",
                    "score": clean_score,
                    "note": (
                        "Clean structure, naming, and readable organization."
                        if not hardcoded_path and not is_stub
                        else "Refactor hardcoded literals/stubs into well-structured modules."
                    ),
                },
                {
                    "category": "Technical Logic & Architecture",
                    "score": arch_score,
                    "note": (
                        "Cohesive abstractions and explicit runtime error handling."
                        if (has_oop or len(func_or_cls) >= 2) and has_error_handling
                        else "Add stronger validation, modularity, and explicit error handling."
                    ),
                },
                {
                    "category": "Testing & Verification",
                    "score": test_score,
                    "note": (
                        "Automated assertions/tests validate expected behavior."
                        if has_tests
                        else "Add automated pytest/unit tests covering edge and failure cases."
                    ),
                },
            ]
            payload = {
                "overall_score": overall,
                "topic_scores": {
                    "Completeness & Requirements": details_score,
                    "Code Quality & Style": clean_score,
                    "Logic & Architecture": arch_score,
                    "Testing & Verification": test_score,
                },
                "categories": categories_payload,
                "category_scores": {c["category"]: c["score"] for c in categories_payload},
                "category_notes": {c["category"]: c["note"] for c in categories_payload},
                "requirements_coverage_pct": coverage,
                "strengths": strengths,
                "weaknesses": weaknesses or ["Minor opportunities to expand edge-case test coverage."],
                "error_tags": error_tags or ["minor_style_nit"],
                "style_summary": (
                    f"Submission spans {line_cnt} code line(s) with "
                    f"{'cohesive OOP/modular structure' if has_oop else 'compact logic'} and "
                    f"{'automated test assertions' if has_tests else 'no automated test assertions'}."
                ),
            }
            return schema_cls.model_validate(payload)

        return schema_cls.model_validate({})
