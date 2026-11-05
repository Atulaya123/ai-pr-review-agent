import logging
from datetime import datetime, timezone

from backend.models.enums import AgentType
from backend.memory.embedder import embed_text
from backend.memory.tiger_client import hybrid_search_chunks
from backend.models.review import ReviewRequest
from backend.observability.events import emit_agent_event
from backend.observability.timing import elapsed_ms, monotonic_now_ns

logger = logging.getLogger(__name__)


async def get_retrieved_context(request: ReviewRequest, top_k: int = 5) -> str:
    """Grounds each specialist in the codebase's own conventions and ADRs (Part
    II / L4 grounding) — an ungrounded reviewer only sees the diff and cannot
    know it violates a decision recorded elsewhere in the repo.

    Embeds the diff, then does a hybrid search over code_chunks (vector cosine
    + content_tsv full-text, fused via Reciprocal Rank Fusion — see
    hybrid_search_chunks) for this repo, and returns the top-k fused chunks as
    context. Ingestion is a separate step (scripts/ingest_docs.py) — if
    nothing has been ingested yet, this returns "" and specialists fall back
    to reasoning over the diff alone, same as M1. Retrieval failure must never
    sink a review, so any error here is logged and swallowed rather than raised.
    """
    diff_text = "\n".join(f.patch for f in request.files)
    started_ns = monotonic_now_ns()
    started_at = datetime.now(timezone.utc)
    try:
        embedding = await embed_text(diff_text)
        chunks = await hybrid_search_chunks(request.repo, embedding, diff_text, top_k=top_k)
    except Exception:
        logger.exception("retrieval failed for %s, falling back to no context", request.repo)
        await emit_agent_event(
            request.review_id,
            AgentType.AGGREGATOR,
            "retrieval.complete",
            latency_ms=elapsed_ms(started_ns),
            outcome="fallback",
            payload={"started_at_utc": started_at.isoformat(), "duration_clock": "monotonic"},
        )
        return ""
    await emit_agent_event(
        request.review_id,
        AgentType.AGGREGATOR,
        "retrieval.complete",
        latency_ms=elapsed_ms(started_ns),
        outcome="success",
        payload={
            "result_count": len(chunks),
            "started_at_utc": started_at.isoformat(),
            "duration_clock": "monotonic",
        },
    )
    return "\n\n---\n\n".join(chunks)
