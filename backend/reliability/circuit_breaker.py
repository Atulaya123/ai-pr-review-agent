import time
from collections.abc import Awaitable, Callable
from typing import TypeVar

from backend.core.exceptions import CircuitOpenError

T = TypeVar("T")


class CircuitBreaker:
    """Per-dependency circuit breaker (e.g. one per LLM provider, one for GitHub API).

    Guards against the "orchestration deadlock" / cascading-failure mode: once a
    dependency has failed `failure_threshold` times in a row, fail fast for
    `reset_after_s` instead of piling up more timeouts on a dead service.
    """

    def __init__(self, failure_threshold: int = 5, reset_after_s: float = 30.0):
        self.failure_threshold = failure_threshold
        self.reset_after_s = reset_after_s
        self._failure_count = 0
        self._opened_at: float | None = None

    @property
    def is_open(self) -> bool:
        if self._opened_at is None:
            return False
        if time.monotonic() - self._opened_at >= self.reset_after_s:
            # half-open: allow one probe through
            return False
        return True

    async def call(self, fn: Callable[[], Awaitable[T]]) -> T:
        if self.is_open:
            raise CircuitOpenError("circuit open, failing fast")
        try:
            result = await fn()
        except Exception:
            self._failure_count += 1
            if self._failure_count >= self.failure_threshold:
                self._opened_at = time.monotonic()
            raise
        else:
            self._failure_count = 0
            self._opened_at = None
            return result
