import asyncio
from collections.abc import Awaitable, Callable
from typing import TypeVar

T = TypeVar("T")

DEFAULT_TIMEOUT_S = 20.0
# Local CPU inference (Ollama) genuinely needs more headroom than a network
# call — a 7B model reasoning over a diff + retrieved context can take well
# past 20s on a laptop. Hosted providers (OpenAI/Anthropic) are typically much
# faster, but there's no harm giving them the same ceiling.
LLM_TIMEOUT_S = 90.0


async def with_timeout(coro: Awaitable[T], *, timeout_s: float = DEFAULT_TIMEOUT_S) -> T:
    """Bound any outbound call. Raises asyncio.TimeoutError on expiry."""
    return await asyncio.wait_for(coro, timeout=timeout_s)
