from __future__ import annotations

import json
import logging
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
        "gemini-3.8-flash",
        "gemini-3.7-flash",
        "gemini-3.5-flash",
    )

    def __init__(
        self,
        api_key: str = "",
        fast_model: str = "gemini-3.8-flash",
        pro_model: str = "gemini-3.8-flash",
        min_interval_sec: float = 4.5,
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

        self._client_instance = genai.Client(api_key=self.api_key)
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
                if _is_model_not_found_error(exc) and idx < len(candidates) - 1:
                    logger.warning("Gemini model %s returned 404; trying next fallback.", candidate)
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
            payload = {
                "overall_score": overall,
                "topic_scores": {
                    "OOP Design & Patterns": oop_score,
                    "Automated Testing": test_score,
                    "Code Quality & Error Handling": clean_score,
                    "Architecture Modularity": arch_score,
                },
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
