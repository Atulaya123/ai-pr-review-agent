from sqlalchemy.ext.asyncio import AsyncSession

from backend.core.config import Settings
from backend.core.exceptions import InvalidWebhookSignatureError
from backend.database.repository import claim_delivery
from backend.security.webhook_verify import verify_github_signature


async def validate_and_claim(
    session: AsyncSession,
    *,
    body: bytes,
    signature_header: str | None,
    delivery_id: str | None,
    settings: Settings,
) -> None:
    """Ingress gate: signature, size, idempotency — in that order, cheapest first."""
    if len(body) > settings.webhook_max_body_bytes:
        raise InvalidWebhookSignatureError("payload too large")

    if not settings.github_webhook_secret:
        raise RuntimeError("GITHUB_WEBHOOK_SECRET not configured")
    verify_github_signature(body, signature_header, settings.github_webhook_secret)

    if not delivery_id:
        raise InvalidWebhookSignatureError("missing X-GitHub-Delivery header")
    await claim_delivery(session, delivery_id)
