from uuid import uuid4

import pytest
from httpx import ASGITransport, AsyncClient
from starlette.applications import Starlette
from starlette.responses import PlainTextResponse
from starlette.routing import Route

from backend.core.config import Settings
from backend.database.repository import save_review_result
from backend.mcp_server.auth import SharedSecretMiddleware
from backend.mcp_server.server import build_mcp, build_mcp_server
from backend.mcp_server.tools import get_findings_impl
from backend.models.enums import ReviewOutcome
from backend.models.review import ReviewResult
from scripts.ingest_docs import CHUNKS


def _inner_app() -> Starlette:
    async def ok(request):
        return PlainTextResponse("ok")

    return Starlette(routes=[Route("/", ok)])


async def test_shared_secret_rejects_missing_auth():
    app = SharedSecretMiddleware(_inner_app(), secret="s3cr3t")
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        resp = await client.get("/")
    assert resp.status_code == 401


async def test_shared_secret_rejects_wrong_token():
    app = SharedSecretMiddleware(_inner_app(), secret="s3cr3t")
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        resp = await client.get("/", headers={"authorization": "Bearer wrong"})
    assert resp.status_code == 401


async def test_shared_secret_passes_correct_token():
    app = SharedSecretMiddleware(_inner_app(), secret="s3cr3t")
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        resp = await client.get("/", headers={"authorization": "Bearer s3cr3t"})
    assert resp.status_code == 200
    assert resp.text == "ok"


def test_build_mcp_fails_closed_without_secret():
    """No MCP_SHARED_SECRET must mean no /mcp at all, not an open endpoint —
    review_diff (Phase 2) will trigger real LLM cost with daily_budget_usd
    still unenforced."""
    settings = Settings(mcp_shared_secret=None)
    assert build_mcp(settings) is None


def test_build_mcp_returns_app_and_server_with_secret():
    settings = Settings(mcp_shared_secret="s3cr3t")
    result = build_mcp(settings)
    assert result is not None
    mcp_app, mcp = result
    assert mcp_app is not None
    assert mcp is not None


async def test_mcp_session_manager_lifespan_required_for_real_requests():
    """Regression test for two real bugs found building this, both only
    visible by actually driving the real HTTP protocol end to end:

    1. Mounting the MCP sub-app alone does NOT propagate the parent app's
       ASGI lifespan into it, so streamable_http_app()'s internal
       session_manager.run() never runs and every real request 500s with
       "RuntimeError: Task group is not initialized" — confirmed against a
       running uvicorn instance before this test existed.
    2. The mcp SDK's DNS-rebinding Host-header check defaults to an empty
       allowed_hosts list even though protection is enabled, which rejects
       every request with 421 regardless of who's asking — including the
       real deployed hostname in production — unless allowed_hosts is set
       explicitly (see Settings.mcp_allowed_hosts).

    This drives the exact wiring create_app() uses (entering
    mcp.session_manager.run() in the parent app's own lifespan, plus a
    configured host allowlist) through a full, real MCP initialize handshake
    — not just a raw tool call — and asserts the registered tool actually
    comes back, not just that nothing crashed.
    """
    settings = Settings(mcp_shared_secret="s3cr3t", mcp_allowed_hosts="testserver")
    result = build_mcp(settings)
    assert result is not None
    mcp_app, mcp = result

    app = Starlette()
    app.mount("/mcp", mcp_app)

    headers = {
        "authorization": "Bearer s3cr3t",
        "Accept": "application/json, text/event-stream",
        "Content-Type": "application/json",
    }
    async with mcp.session_manager.run():
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://testserver") as client:
            init = await client.post(
                "/mcp/",
                json={
                    "jsonrpc": "2.0",
                    "id": 1,
                    "method": "initialize",
                    "params": {
                        "protocolVersion": "2025-06-18",
                        "capabilities": {},
                        "clientInfo": {"name": "test-client", "version": "0"},
                    },
                },
                headers=headers,
            )
            assert init.status_code == 200
            session_id = init.headers["mcp-session-id"]
            session_headers = {**headers, "mcp-session-id": session_id}

            initialized = await client.post(
                "/mcp/",
                json={"jsonrpc": "2.0", "method": "notifications/initialized"},
                headers=session_headers,
            )
            assert initialized.status_code == 202

            tools_resp = await client.post(
                "/mcp/",
                json={"jsonrpc": "2.0", "id": 2, "method": "tools/list"},
                headers=session_headers,
            )
    assert tools_resp.status_code == 200
    assert "retrieve_context" in tools_resp.text
    assert "get_findings" in tools_resp.text


async def test_mcp_server_registers_tools_and_resources_cleanly():
    """Sanity check that tool/resource registration doesn't blow up at
    schema-generation time, and that the resource-registration loop actually
    binds distinct content per invariant rather than all six closing over
    the same (last) loop value."""
    mcp = build_mcp_server()

    tools = await mcp.list_tools()
    assert {t.name for t in tools} == {"retrieve_context", "get_findings"}

    resources = await mcp.list_resources()
    assert len(resources) == len(CHUNKS)

    _path0, symbol0, content0 = CHUNKS[0]
    _path1, symbol1, content1 = CHUNKS[1]
    read0 = list(await mcp.read_resource(f"architecture://{symbol0}"))[0].content
    read1 = list(await mcp.read_resource(f"architecture://{symbol1}"))[0].content
    assert read0 == content0
    assert read1 == content1
    assert read0 != read1


@pytest.mark.usefixtures("db_session")
async def test_get_findings_impl_reports_latest_review(db_session):
    result = ReviewResult(
        review_id=uuid4(), findings=[], overall_confidence=0.95, outcome=ReviewOutcome.APPROVED, posted=True
    )
    await save_review_result(db_session, "acme/demo", 42, "sha-mcp-test", result)

    found = await get_findings_impl("acme/demo", 42)
    assert found["found"] is True
    assert found["review_id"] == str(result.review_id)
    assert found["outcome"] == "approved"
    assert found["head_sha"] == "sha-mcp-test"


@pytest.mark.usefixtures("db_session")
async def test_get_findings_impl_reports_not_found_for_unknown_pr():
    found = await get_findings_impl("acme/demo", 999999)
    assert found == {"repo": "acme/demo", "pr_number": 999999, "found": False}
