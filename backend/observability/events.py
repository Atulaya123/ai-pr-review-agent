import logging
from uuid import UUID

from backend.database.models import AgentEvent
from backend.database.session import get_sessionmaker
from backend.models.enums import AgentType

logger = logging.getLogger(__name__)


async def emit_agent_event(
    review_id: UUID,
    agent: AgentType,
    event_type: str,
    *,
    model: str | None = None,
    tokens_in: int | None = None,
    tokens_out: int | None = None,
    cost_usd: float | None = None,
    latency_ms: int | None = None,
    outcome: str | None = None,
    confidence: float | None = None,
) -> None:
    """Write one row to the events spine. Never raises — observability must not
    be able to take down a review (it's cross-cutting, not on the critical path).
    """
    try:
        async with get_sessionmaker()() as session:
            session.add(
                AgentEvent(
                    review_id=review_id,
                    agent=agent.value,
                    event_type=event_type,
                    model=model,
                    tokens_in=tokens_in,
                    tokens_out=tokens_out,
                    cost_usd=cost_usd,
                    latency_ms=latency_ms,
                    outcome=outcome,
                    confidence=confidence,
                )
            )
            await session.commit()
    except Exception:
        logger.exception("failed to emit agent event review_id=%s agent=%s type=%s", review_id, agent, event_type)
