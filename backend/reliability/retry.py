import asyncio
import logging
import random
from collections.abc import Awaitable, Callable
from typing import TypeVar

T = TypeVar("T")

logger = logging.getLogger(__name__)


async def retry_with_backoff(
    fn: Callable[[], Awaitable[T]],
    *,
    max_attempts: int = 3,
    base_delay_s: float = 0.5,
    exceptions: tuple[type[Exception], ...] = (Exception,),
) -> T:
    """Retry an async call with exponential backoff + jitter.

    Used for the tool/API-timeout failure mode: an LLM provider or the GitHub
    API stalling should not hang or fail the whole review outright.
    """
    last_exc: Exception | None = None
    for attempt in range(1, max_attempts + 1):
        try:
            return await fn()
        except exceptions as exc:  # noqa: PERF203 - retry loop is intentional
            last_exc = exc
            if attempt == max_attempts:
                break
            delay = base_delay_s * (2 ** (attempt - 1)) + random.uniform(0, base_delay_s)
            logger.warning("attempt %s/%s failed: %s, retrying in %.2fs", attempt, max_attempts, exc, delay)
            await asyncio.sleep(delay)
    assert last_exc is not None
    raise last_exc
