import asyncio
import hashlib
from collections.abc import Callable

from redis.asyncio import Redis

from backend.core.exceptions import TokenBudgetExceededError
from backend.models.review import DiffFile, ReviewRequest


def estimate_call_tokens(system: str, user: str, max_output_tokens: int) -> int:
    """Conservative byte-based estimate, including output and message overhead."""
    return (len(system.encode()) + len(user.encode()) + 2) // 3 + 128 + max_output_tokens


def chunk_review_request(
    request: ReviewRequest,
    *,
    estimate: Callable[[ReviewRequest], int],
    token_limit: int,
) -> list[ReviewRequest]:
    if estimate(request) <= token_limit:
        return [request]
    chunks: list[ReviewRequest] = []
    current: list[DiffFile] = []
    for file in request.files:
        single = request.model_copy(update={"files": [file]})
        if estimate(single) > token_limit:
            raise TokenBudgetExceededError(
                f"Whole-file review for {file.path!r} exceeds the token limit; "
                "no file was split or silently omitted."
            )
        combined = request.model_copy(update={"files": [*current, file]})
        if current and estimate(combined) > token_limit:
            chunks.append(request.model_copy(update={"files": current}))
            current = []
        current = [*current, file]
    if current:
        chunks.append(request.model_copy(update={"files": current}))
    if not chunks:
        raise TokenBudgetExceededError("Prompt overhead exceeds the token limit.")
    return chunks


# Reservations expire only after a full quiet minute. This is conservative:
# refreshing the TTL on each admission prevents overlapping batches from
# exceeding a rolling-minute ceiling.
_RESERVE_TOKENS = """
local used = tonumber(redis.call('GET', KEYS[1]) or '0')
local amount = tonumber(ARGV[1])
local limit = tonumber(ARGV[2])
if used + amount > limit then
    return redis.call('PTTL', KEYS[1])
end
redis.call('INCRBY', KEYS[1], amount)
redis.call('PEXPIRE', KEYS[1], 60000)
return 0
"""


async def wait_for_groq_capacity(
    *, tokens: int, token_limit: int, model: str, redis_url: str
) -> None:
    if tokens > token_limit:
        raise TokenBudgetExceededError("Estimated call exceeds Groq's per-minute token limit.")
    key = "aipr:groq:tokens:" + hashlib.sha256(model.encode()).hexdigest()
    redis = Redis.from_url(redis_url)
    try:
        while True:
            delay_ms = int(await redis.eval(_RESERVE_TOKENS, 1, key, tokens, token_limit))
            if delay_ms == 0:
                return
            await asyncio.sleep(max(delay_ms, 1) / 1000)
    finally:
        await redis.aclose()
