import logging

from backend.memory.embedder import embed_text
from backend.memory.tiger_client import query_similar_chunks
from backend.models.review import ReviewRequest

logger = logging.getLogger(__name__)


async def get_retrieved_context(request: ReviewRequest, top_k: int = 5) -> str:
    """Grounds each specialist in the codebase's own conventions and ADRs (Part
    II / L4 grounding) — an ungrounded reviewer only sees the diff and cannot
    know it violates a decision recorded elsewhere in the repo.

    Embeds the diff, does a cosine-similarity search over code_chunks for this
    repo, and returns the top-k nearest chunks as context. Ingestion is a
    separate step (scripts/ingest_docs.py) — if nothing has been ingested yet,
    this returns "" and specialists fall back to reasoning over the diff alone,
    same as M1. Retrieval failure must never sink a review, so any error here
    is logged and swallowed rather than raised.
    """
    diff_text = "\n".join(f.patch for f in request.files)
    try:
        embedding = await embed_text(diff_text)
        chunks = await query_similar_chunks(request.repo, embedding, top_k=top_k)
    except Exception:
        logger.exception("retrieval failed for %s, falling back to no context", request.repo)
        return ""
    return "\n\n---\n\n".join(chunks)
