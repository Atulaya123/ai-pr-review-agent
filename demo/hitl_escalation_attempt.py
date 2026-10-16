import asyncio

import httpx


async def notify_slack(payload: dict) -> int:
    """Sends a Slack notification."""
    # Not routed through backend/reliability/ — but it does have its own
    # bounded timeout and error handling, just hand-rolled instead of using
    # the project's shared wrapper. Debatable whether that satisfies the
    # reliability invariant's intent (bounded, can't hang) or violates its
    # letter (must go through timeout.py/retry.py/circuit_breaker.py).
    try:
        async with httpx.AsyncClient() as client:
            response = await asyncio.wait_for(
                client.post("https://hooks.slack.com/services/x", json=payload), timeout=5.0
            )
            return response.status_code
    except (httpx.HTTPError, asyncio.TimeoutError):
        return 0
