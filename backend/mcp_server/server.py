"""Exposes this project's own capabilities as MCP tools/resources, so
another agent can call retrieve_context / get_findings over Streamable HTTP
instead of only this repo's own LangGraph pipeline being able to use them.

Phase 1 only: the two read-only tools, gated by a shared secret (see
auth.py). review_diff (the LLM-cost tool) is deliberately not exposed yet —
Phase 2 gates it behind OAuth 2.1 + audience-bound GitHub token validation,
since daily_budget_usd still isn't enforced and an open endpoint able to
trigger the full 4-specialist LangGraph fan-out is an uncapped bill.
"""

import logging

from mcp.server.mcpserver import MCPServer
from mcp.server.transport_security import TransportSecuritySettings
from starlette.types import ASGIApp

from backend.core.config import Settings
from backend.mcp_server.auth import SharedSecretMiddleware
from backend.mcp_server.tools import get_findings_impl, retrieve_context_impl
from scripts.ingest_docs import CHUNKS

logger = logging.getLogger(__name__)


def build_mcp_server() -> MCPServer:
    mcp = MCPServer(name="aipr-review-agent")

    @mcp.tool()
    async def retrieve_context(repo: str, query: str, top_k: int = 5) -> str:
        """Hybrid retrieval (pgvector/DiskANN cosine + Postgres full-text,
        fused via Reciprocal Rank Fusion) over this repo's ingested
        code_chunks. Read-only, costs one embedding call."""
        return await retrieve_context_impl(repo, query, top_k)

    @mcp.tool()
    async def get_findings(repo: str, pr_number: int) -> dict:
        """The latest recorded review for a PR — outcome, confidence, and
        findings. Read-only, zero LLM cost."""
        return await get_findings_impl(repo, pr_number)

    # The same six architecture invariants scripts/ingest_docs.py embeds
    # into code_chunks, exposed as MCP resources too — lets a client read
    # the project's own rules directly instead of only reaching them via a
    # similarity search that might not surface them for a given query.
    #
    # A static URI's handler must take zero parameters (the SDK treats
    # handler params as {...} URI template variables and rejects a mismatch)
    # — so binding `content` per loop iteration needs a real closure via a
    # factory, not a default-argument trick on the handler itself.
    def _make_invariant_reader(text: str):
        def _read() -> str:
            return text

        return _read

    for _path, symbol, content in CHUNKS:
        mcp.resource(f"architecture://{symbol}", name=symbol, mime_type="text/plain")(
            _make_invariant_reader(content)
        )

    return mcp


def build_mcp(settings: Settings) -> tuple[ASGIApp, MCPServer] | None:
    """None means "don't mount" — main.py skips app.mount() entirely rather
    than expose /mcp with no auth. Fails closed: an unconfigured secret must
    never mean open access, not even in a dev/demo environment, since one of
    these tools already touches paid-tier-shaped cost (embeddings) and the
    Phase 2 tool will touch real LLM cost directly.

    Returns both the secured ASGI app to mount AND the underlying MCPServer,
    because a Mount does NOT propagate the parent ASGI app's lifespan into a
    mounted sub-app — streamable_http_app()'s own
    lifespan=lambda app: session_manager.run() (which initializes the
    session manager's task group) never runs unless the *parent* FastAPI
    app's own lifespan explicitly enters mcp.session_manager.run() too. This
    isn't a theoretical concern: without that, every real request to /mcp
    500s with "RuntimeError: Task group is not initialized" — confirmed
    against a real running uvicorn instance, not assumed from the mount
    working for a plain Starlette route.
    """
    if not settings.mcp_shared_secret:
        logger.warning("MCP_SHARED_SECRET not set — /mcp will not be mounted")
        return None
    mcp = build_mcp_server()
    inner = mcp.streamable_http_app(
        streamable_http_path="/",
        transport_security=TransportSecuritySettings(
            allowed_hosts=settings.mcp_allowed_hosts_list, allowed_origins=[]
        ),
    )
    return SharedSecretMiddleware(inner, settings.mcp_shared_secret), mcp
