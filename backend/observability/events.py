import logging
from datetime import datetime, timezone
from uuid import UUID

from backend.database.models import AgentEvent
from backend.database.session import get_sessionmaker
from backend.configuration.versions import build_behavior_manifest
from backend.models.enums import AgentType

logger = logging.getLogger(__name__)


async def emit_agent_event(
    review_id: UUID,
    agent: AgentType | str,
    event_type: str,
    *,
    model: str | None = None,
    tokens_in: int | None = None,
    tokens_out: int | None = None,
    cost_usd: float | None = None,
    latency_ms: int | None = None,
    outcome: str | None = None,
    confidence: float | None = None,
    payload: dict[str, object] | None = None,
    occurred_at: datetime | None = None,
) -> None:
    """Write one row to the events spine. Never raises — observability must not
    be able to take down a review (it's cross-cutting, not on the critical path).
    """
    try:
        event_payload = dict(payload or {})
        event_payload["behavior_versions"] = build_behavior_manifest(
            agent=agent.value if isinstance(agent, AgentType) else agent,
            model=model,
        )
        async with get_sessionmaker()() as session:
            session.add(
                AgentEvent(
                    ts=occurred_at or datetime.now(timezone.utc),
                    review_id=review_id,
                    agent=agent.value if isinstance(agent, AgentType) else agent,
                    event_type=event_type,
                    model=model,
                    tokens_in=tokens_in,
                    tokens_out=tokens_out,
                    cost_usd=cost_usd,
                    latency_ms=latency_ms,
                    outcome=outcome,
                    confidence=confidence,
                    payload=event_payload,
                )
            )
            await session.commit()
    except Exception:
        logger.exception(
            "failed to emit agent event review_id=%s agent=%s type=%s", review_id, agent, event_type
        )
