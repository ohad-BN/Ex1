from __future__ import annotations

import threading
import time
from collections import defaultdict, deque


class SlidingWindowRateLimiter:
    """Thread-safe in-memory sliding-window rate limiter for auth and AI routes."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._buckets: dict[str, deque[float]] = defaultdict(deque)

    def allow(
        self,
        key: str,
        limit: int,
        window_seconds: float,
        now_ts: float | None = None,
    ) -> tuple[bool, int]:
        """Record an attempt if within budget; return (allowed, retry_after_sec)."""
        if limit <= 0 or window_seconds <= 0:
            return True, 0

        now = now_ts if now_ts is not None else time.monotonic()
        cutoff = now - window_seconds

        with self._lock:
            q = self._buckets[key]
            while q and q[0] <= cutoff:
                q.popleft()
            if len(q) >= limit:
                retry_after = max(1, int(window_seconds - (now - q[0])))
                return False, retry_after
            q.append(now)
            return True, 0

    def reset(self, prefix: str | None = None) -> None:
        with self._lock:
            if prefix is None:
                self._buckets.clear()
            else:
                for k in list(self._buckets.keys()):
                    if k.startswith(prefix):
                        self._buckets.pop(k, None)
