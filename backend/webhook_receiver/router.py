import logging

from fastapi import APIRouter, Depends, Header, HTTPException, Request
from sqlalchemy.ext.asyncio import AsyncSession

from backend.core.config import Settings, get_settings
from backend.core.exceptions import DuplicateDeliveryError, InvalidWebhookSignatureError
from backend.database.session import get_session
from backend.integrations.diff_parser import parse_unified_diff
from backend.integrations.github_client import GitHubClient
from backend.job_queue.arq_worker import get_redis_pool
from backend.models.webhook import PullRequestPayload
from backend.webhook_receiver.validator import validate_and_claim

logger = logging.getLogger(__name__)
router = APIRouter()

_HANDLED_ACTIONS = {"opened", "synchronize", "reopened"}


@router.post("/webhook")
async def receive_webhook(
    request: Request,
    x_hub_signature_256: str | None = Header(default=None),
    x_github_delivery: str | None = Header(default=None),
    x_github_event: str | None = Header(default=None),
    session: AsyncSession = Depends(get_session),
    settings: Settings = Depends(get_settings),
):
    body = await request.body()

    try:
        await validate_and_claim(
            session,
            body=body,
            signature_header=x_hub_signature_256,
            delivery_id=x_github_delivery,
            settings=settings,
        )
    except InvalidWebhookSignatureError as exc:
        raise HTTPException(status_code=401, detail=str(exc)) from exc
    except DuplicateDeliveryError:
        # already processed — ack with 200 so GitHub doesn't retry a redelivery loop
        return {"status": "duplicate_ignored"}

    if x_github_event != "pull_request":
        return {"status": "ignored", "reason": "not a pull_request event"}

    payload = PullRequestPayload.from_github_event(await request.json())
    if payload.action not in _HANDLED_ACTIONS:
        return {"status": "ignored", "reason": f"action {payload.action} not handled"}

    files: list[dict] = []
    if payload.installation_id is not None:
        try:
            diff_text = await GitHubClient(settings).fetch_pr_diff(
                payload.repo_full_name, payload.number, payload.installation_id
            )
            files = [f.model_dump() for f in parse_unified_diff(diff_text)]
        except Exception:
            logger.exception("failed to fetch diff for %s#%s, enqueuing with no files", payload.repo_full_name, payload.number)

    pool = await get_redis_pool()
    await pool.enqueue_job(
        "run_review_job",
        repo=payload.repo_full_name,
        pr_number=payload.number,
        installation_id=payload.installation_id,
        head_sha=payload.head_sha,
        files=files,
    )

    return {"status": "enqueued"}
