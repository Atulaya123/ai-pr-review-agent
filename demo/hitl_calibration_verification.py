import asyncio

import httpx


async def notify_slack(payload: dict) -> int:
    """Sends a Slack notification."""
    # Not routed through backend/reliability/ — but it does have its own
    # bounded timeout and error handling, just hand-rolled instead of using
    # the project's shared wrapper. Same scenario as PR #11, which measured
    # min confidence 0.90 across its findings — re-run here to check whether
    # the newly calibrated HITL_CONFIDENCE_THRESHOLD (0.93) actually escalates
    # this now, rather than assuming the calibration works from the math alone.
    try:
        async with httpx.AsyncClient() as client:
            response = await asyncio.wait_for(
                client.post("https://hooks.slack.com/services/x", json=payload), timeout=5.0
            )
            return response.status_code
    except (httpx.HTTPError, asyncio.TimeoutError):
        return 0
