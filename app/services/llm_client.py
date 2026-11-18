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
        "gemini-3.1-flash-lite",
        "gemini-3.5-flash-lite",
        "gemini-3.6-flash",
        "gemini-3.7-flash",
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
    ) -> str:
        """Generate free-form Markdown/text via Gemini or deterministic local synthesis."""
        if not self.is_configured():
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
            return self._local_text_fallback(prompt, system_instruction)
        except Exception as exc:
            logger.error("Gemini generate_text failed; using fallback=%s: %s", self.fallback_on_error, exc)
            if not self.fallback_on_error:
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
        ".mp4": "audio/mp4",
        ".ogg": "audio/ogg",
        ".flac": "audio/flac",
        ".webm": "audio/webm",
        ".aac": "audio/aac",
    }

    def transcribe_audio_file(
        self,
        audio_path: Path,
        prompt: str,
        system_instruction: str = "",
        model: Optional[str] = None,
    ) -> str:
        """Transcribe a class recording using Google Gemini API (Files API or inline bytes)."""
        if not self.is_configured():
            raise RuntimeError("Gemini API key is not configured.")

        target_model = model or self.fast_model
        client = self._get_genai_client()
        mime_type = self.AUDIO_MIME_TYPES.get(audio_path.suffix.lower(), "audio/mpeg")
        uploaded_file: Any = None

        try:
            if hasattr(client, "files"):
                uploaded_file = client.files.upload(file=str(audio_path))
                for _ in range(40):
                    state_obj = getattr(uploaded_file, "state", None)
                    state_name = str(getattr(state_obj, "name", state_obj or "")).upper()
                    if state_name and "PROCESSING" not in state_name:
                        break
                    self._sleep(1.5)
                    uploaded_file = client.files.get(name=uploaded_file.name)
                media_part: Any = uploaded_file
            else:
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
        """Deterministic static analyzer fallback for structured schemas such as HomeworkMetrics."""
        field_names = set(schema_cls.model_fields.keys())
        if {"overall_score", "topic_scores", "error_tags"}.issubset(field_names):
            lower = prompt.lower()
            has_tests = any(k in lower for k in ("pytest", "unittest", "assert ", "def test_"))
            has_docstrings = '"""' in prompt or "'''" in prompt or "/**" in prompt
            has_oop = any(k in lower for k in ("class ", "interface ", "abc", "abstract"))
            has_error_handling = "try:" in lower or "except " in lower
            hardcoded_path = bool(re.search(r"[a-zA-Z]:\\\\|/Users/", prompt))

            oop_score = 88.0 if has_oop else 64.0
            test_score = 89.0 if has_tests else 55.0
            clean_score = 86.0 if (has_docstrings and not hardcoded_path) else 66.0
            arch_score = 87.0 if (has_oop and has_error_handling) else 68.0
            details_score = round((clean_score * 0.6 + (90.0 if len(prompt) > 350 else 68.0) * 0.4), 1)
            overall = round((oop_score + test_score + clean_score + arch_score) / 4.0, 1)

            strengths: list[str] = []
            weaknesses: list[str] = []
            error_tags: list[str] = []

            if has_oop:
                strengths.append("Clean object-oriented class decomposition and encapsulation.")
            else:
                weaknesses.append("Logic relies heavily on procedural functions rather than cohesive classes.")
                error_tags.append("low_modularity")

            if has_tests:
                strengths.append("Includes automated test coverage validating core behavior.")
            else:
                weaknesses.append("Missing automated unit tests or edge-case assertions.")
                error_tags.append("missing_edge_case_tests")

            if has_error_handling:
                strengths.append("Handles runtime edge cases with explicit try/except guards.")
            else:
                weaknesses.append("Insufficient error handling around I/O or external calls.")
                error_tags.append("missing_error_handling")

            if hardcoded_path:
                weaknesses.append("Contains hardcoded machine-specific filesystem paths.")
                error_tags.append("hardcoded_paths")

            if not strengths:
                strengths.append("Readable baseline structure and clear naming conventions.")

            coverage = round(min(98.0, 58.0 + (12.0 if has_oop else 0.0) + (15.0 if has_tests else 0.0) + (10.0 if has_error_handling else 0.0)), 1)
            categories_payload = [
                {
                    "category": "Materials & Details",
                    "score": details_score,
                    "note": (
                        "Good documentation and specification detail coverage."
                        if has_docstrings and not hardcoded_path
                        else "Add docstrings/comments and remove hardcoded assumptions."
                    ),
                },
                {
                    "category": "Execution Style",
                    "score": clean_score,
                    "note": (
                        "Clean naming and readable structural organization."
                        if not hardcoded_path
                        else "Clean up machine-specific literals and improve formatting consistency."
                    ),
                },
                {
                    "category": "Technical Logic & Architecture",
                    "score": arch_score,
                    "note": (
                        "Cohesive OOP abstractions with explicit error handling."
                        if has_oop and has_error_handling
                        else "Strengthen class encapsulation and explicit try/except guards."
                    ),
                },
                {
                    "category": "Testing & Verification",
                    "score": test_score,
                    "note": (
                        "Automated assertions/tests validate core functionality."
                        if has_tests
                        else "Add unit tests and edge-case assertions."
                    ),
                },
            ]
            payload = {
                "overall_score": overall,
                "topic_scores": {
                    "OOP Design & Patterns": oop_score,
                    "Automated Testing": test_score,
                    "Code Quality & Error Handling": clean_score,
                    "Architecture Modularity": arch_score,
                },
                "categories": categories_payload,
                "category_scores": {c["category"]: c["score"] for c in categories_payload},
                "category_notes": {c["category"]: c["note"] for c in categories_payload},
                "requirements_coverage_pct": coverage,
                "strengths": strengths,
                "weaknesses": weaknesses or ["Minor opportunities to improve edge-case documentation."],
                "error_tags": error_tags or ["minor_style_nit"],
                "style_summary": (
                    f"Code demonstrates {'modular OOP abstractions' if has_oop else 'script-style flow'} "
                    f"with {'automated test coverage' if has_tests else 'manual verification only'}."
                ),
            }
            return schema_cls.model_validate(payload)

        return schema_cls.model_validate({})
