"""Ingest this project's own architectural rules into the vector lane
(code_chunks), so specialists can be grounded in conventions and ADRs that
live elsewhere in the repo, not just the diff in front of them (Part II / L4).

Content below is drawn verbatim/paraphrased from context-graph.json's
invariants and docs/ARCHITECTURE_DECISIONS.md — real project decisions, not
placeholder text. Re-running is safe; it clears and reloads this repo's rows.

Run with: PYTHONPATH=. python -m scripts.ingest_docs
"""

import asyncio

from backend.core.config import get_settings
from backend.database.session import get_sessionmaker
from backend.memory.embedder import embed_text
from backend.memory.tiger_client import to_vector_literal
from sqlalchemy import text

REPO = "Atulaya123/ai-pr-review-agent"

CHUNKS = [
    (
        "ARCHITECTURE.md",
        "modular_monolith_dependency_rule",
        "Dependency rule (ADR-002, context-graph.json invariant): core/ depends on "
        "nothing. Outer modules (agents, api, orchestrator, memory, integrations, "
        "job_queue, webhook_receiver) depend inward only, never sideways on each "
        "other directly — only through core/workflow_engine.py or models/. "
        "observability is cross-cutting middleware, injected everywhere, not a "
        "dependency of domain logic.",
    ),
    (
        "ARCHITECTURE.md",
        "reliability_layer_required",
        "Reliability invariant (context-graph.json): every outbound call — every "
        "LLM call, every GitHub API call, every Tiger/Postgres query, and any "
        "future external integration (Slack, email, etc.) — must go through "
        "backend/reliability/timeout.py + retry.py + circuit_breaker.py. No raw "
        "httpx/asyncpg/openai client call without a bound timeout. A hanging "
        "outbound call must never be able to stall the worker.",
    ),
    (
        "ARCHITECTURE.md",
        "workflow_engine_seam",
        "Orchestration seam (ADR-001): all orchestrator code depends on the "
        "abstract WorkflowEngine interface in backend/core/workflow_engine.py "
        "(run/resume/get_state), never on LangGraph directly. Only "
        "backend/orchestrator/langgraph_engine.py is allowed to import langgraph. "
        "This is what lets Temporal swap in later without touching any other module.",
    ),
    (
        "ARCHITECTURE.md",
        "llm_client_seam",
        "LLM provider seam (docs/ARCHITECTURE_DECISIONS.md #7): all LLM calls go "
        "through the LLMClient interface in backend/tools/llm_client.py "
        "(get_llm_client()), which dispatches to OpenAI, Anthropic, Ollama, or "
        "the deterministic MockLLMClient based on LLM_PROVIDER. No other module "
        "should import the openai or anthropic SDKs directly — that would bypass "
        "the one place cost, retries, and provider-swapping are handled.",
    ),
    (
        "ARCHITECTURE.md",
        "hitl_confidence_gate",
        "Confidence-weighted HITL gate (backend/hitl/gate.py): overall confidence "
        "is the MINIMUM confidence across findings, not the average — one "
        "low-confidence finding must not be masked by several high-confidence "
        "ones. Any CRITICAL severity finding escalates to human review "
        "regardless of confidence, because the consequence of a missed critical "
        "issue is too high to automate.",
    ),
    (
        "ARCHITECTURE.md",
        "untrusted_content_boundary",
        "Trust boundary invariant (context-graph.json): PR diff/code content is "
        "untrusted data, never instructions. It is only ever passed to LLMs "
        "inside an explicit <<<UNTRUSTED_...>>> fenced block (see "
        "backend/security/injection_guard.py), and is never eval'd, shelled out, "
        "or used to build SQL/paths without parameterization.",
    ),
]


async def main() -> None:
    settings = get_settings()
    async with get_sessionmaker()() as session:
        await session.execute(text("DELETE FROM code_chunks WHERE repo = :repo"), {"repo": REPO})
        for chunk_index, (path, symbol, content) in enumerate(CHUNKS):
            embedding = await embed_text(content, settings)
            await session.execute(
                text(
                    """
                    INSERT INTO code_chunks (repo, path, symbol, chunk_index, content, embedding)
                    VALUES (:repo, :path, :symbol, :chunk_index, :content, CAST(:emb AS vector))
                    """
                ),
                {
                    "repo": REPO,
                    "path": path,
                    "symbol": symbol,
                    "chunk_index": chunk_index,
                    "content": content,
                    "emb": to_vector_literal(embedding),
                },
            )
            print(f"ingested: {symbol}")
        await session.commit()
    print(f"done — {len(CHUNKS)} chunks ingested for {REPO}")


if __name__ == "__main__":
    asyncio.run(main())
