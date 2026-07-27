import asyncio
from collections.abc import Awaitable, Callable
from typing import TypeVar

T = TypeVar("T")

DEFAULT_TIMEOUT_S = 20.0


async def with_timeout(coro: Awaitable[T], *, timeout_s: float = DEFAULT_TIMEOUT_S) -> T:
    """Bound any outbound call. Raises asyncio.TimeoutError on expiry."""
    return await asyncio.wait_for(coro, timeout=timeout_s)
