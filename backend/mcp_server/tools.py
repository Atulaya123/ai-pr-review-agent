"""Plain, directly-testable implementations behind the MCP tools registered
in server.py. Kept separate from the @mcp.tool() decorators so tests call
these functions directly instead of driving them through an MCP client.

Both are the Phase 1 read-only tools: retrieve_context costs one embedding
call, get_findings is a pure DB read. The expensive tool (review_diff, real
LLM cost per call) is Phase 2, gated behind OAuth — not implemented yet.
"""

from typing import Any

from backend.database.repository import get_latest_review
from backend.database.session import get_sessionmaker
from backend.memory.embedder import embed_text
from backend.memory.tiger_client import hybrid_search_chunks


async def retrieve_context_impl(repo: str, query: str, top_k: int = 5) -> str:
    """Hybrid retrieval (pgvector/DiskANN cosine + Postgres full-text, fused
    via Reciprocal Rank Fusion) over this repo's ingested code_chunks — the
    same underlying primitive get_retrieved_context uses for a diff, exposed
    here for an arbitrary text query instead.
    """
    embedding = await embed_text(query)
    chunks = await hybrid_search_chunks(repo, embedding, query, top_k=top_k)
    if not chunks:
        return f"No chunks ingested for {repo}, or no match for this query."
    return "\n\n---\n\n".join(chunks)


async def get_findings_impl(repo: str, pr_number: int) -> dict[str, Any]:
    """The latest recorded review for a PR — read-only, zero LLM cost."""
    async with get_sessionmaker()() as session:
        record = await get_latest_review(session, repo, pr_number)
        if record is None:
            return {"repo": repo, "pr_number": pr_number, "found": False}
        return {
            "repo": repo,
            "pr_number": pr_number,
            "found": True,
            "review_id": str(record.id),
            "head_sha": record.head_sha,
            "outcome": record.outcome,
            "overall_confidence": record.overall_confidence,
            "posted": record.posted,
            "findings": [
                {
                    "agent_type": f.agent_type,
                    "severity": f.severity,
                    "category": f.category,
                    "summary": f.summary,
                    "file_path": f.file_path,
                    "confidence": f.confidence,
                }
                for f in record.findings
            ],
        }
