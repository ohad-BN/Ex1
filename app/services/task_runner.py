from __future__ import annotations

import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from typing import Any, Callable


class AsyncTaskManager:
    """Bounded background thread pool for CPU/IO-heavy jobs (SCRFD CV, Whisper, LLM).

    Keeps Gunicorn Socket.IO threads non-blocking during live classroom sessions.
    """

    def __init__(self, max_workers: int = 2) -> None:
        self._max_workers = max(1, max_workers)
        self._pool = ThreadPoolExecutor(
            max_workers=self._max_workers, thread_name_prefix="classpulse-worker"
        )
        self._lock = threading.Lock()
        self._jobs: dict[str, dict[str, Any]] = {}

    def submit(
        self,
        job_name: str,
        fn: Callable[..., Any],
        *args: Any,
        **kwargs: Any,
    ) -> str:
        """Enqueue a callable for background execution and return its job_id."""
        job_id = uuid.uuid4().hex
        now = time.time()
        with self._lock:
            self._jobs[job_id] = {
                "job_id": job_id,
                "job_name": job_name,
                "status": "PENDING",
                "created_at": now,
                "started_at": None,
                "finished_at": None,
                "result": None,
                "error": None,
            }

        def _wrapped() -> Any:
            with self._lock:
                self._jobs[job_id]["status"] = "RUNNING"
                self._jobs[job_id]["started_at"] = time.time()
            try:
                result = fn(*args, **kwargs)
                with self._lock:
                    self._jobs[job_id]["status"] = "DONE"
                    self._jobs[job_id]["result"] = result
                    self._jobs[job_id]["finished_at"] = time.time()
                return result
            except Exception as exc:  # noqa: BLE001
                with self._lock:
                    self._jobs[job_id]["status"] = "FAILED"
                    self._jobs[job_id]["error"] = f"{type(exc).__name__}: {exc}"
                    self._jobs[job_id]["finished_at"] = time.time()
                return None

        self._pool.submit(_wrapped)
        return job_id

    def get_status(self, job_id: str) -> dict[str, Any] | None:
        """Return a thread-safe snapshot of a tracked job."""
        with self._lock:
            job = self._jobs.get(job_id)
            return dict(job) if job is not None else None

    def list_jobs(self) -> list[dict[str, Any]]:
        """Return all tracked jobs ordered by creation time descending."""
        with self._lock:
            return sorted(
                (dict(v) for v in self._jobs.values()),
                key=lambda item: item["created_at"],
                reverse=True,
            )

    def shutdown(self, wait: bool = False) -> None:
        """Cleanly shut down the underlying ThreadPoolExecutor."""
        self._pool.shutdown(wait=wait)
