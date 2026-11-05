from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from types import SimpleNamespace
from uuid import uuid4

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.agents import base_agent
from backend.agents.base_agent import resolve_model_for_provider
from backend.core.config import Settings
from backend.database.cost_ledger import CostLedger
from backend.core.workflow_engine import get_workflow_engine
from backend.database.models import AgentEvent, ModelCall, PriceVersion
from backend.database.pricing import seed_initial_prices
from backend.memory import embedder
from backend.database.repository import (
    get_latest_review,
    get_posted_review,
    get_review,
    save_review_result,
)
from backend.memory import context_retriever
from backend.models.enums import ReviewOutcome, Severity
from backend.models.review import ReviewRequest, ReviewResult
from backend.tests.fixtures.vulnerable_diff import CLEAN_DIFF_FILES, VULNERABLE_DIFF_FILES
from backend.tools.llm_client import BudgetedLLMClient, MockLLMClient
from backend.database.session import get_sessionmaker
from backend.orchestrator import nodes
from backend.observability.safety_metrics import load_safety_metrics
from backend.tests.fixtures.injection_attempts import injection_review_request

INITIAL_STATE = {
    "retrieved_context": "",
    "security_findings": [],
    "quality_findings": [],
    "tests_findings": [],
    "docs_findings": [],
    "findings": [],
    "overall_confidence": 1.0,
    "outcome": ReviewOutcome.APPROVED,
}


async def _seed_mock_price(session: AsyncSession) -> None:
    session.add(
        PriceVersion(
            provider="mock",
            model="mock",
            input_rate_per_million=Decimal("0"),
            cached_input_rate_per_million=Decimal("0"),
            output_rate_per_million=Decimal("0"),
            currency="USD",
            effective_from=date.today(),
            source="Deterministic local MockLLMClient; no provider-billed token usage.",
        )
    )
    await session.commit()


async def test_pipeline_flags_sql_injection_and_blocks(db_session: AsyncSession) -> None:
    """M1 demo: a fixture diff with a known SQL-injection pattern goes in, and
    the aggregator comes back with a CRITICAL finding + CRITICAL_BLOCK outcome
    — end to end through the real LangGraph fan-out, using the mock LLM client
    (no API keys needed)."""
    await _seed_mock_price(db_session)
    request = ReviewRequest(
        repo="acme/demo", pr_number=1, head_sha="abc123", files=VULNERABLE_DIFF_FILES
    )
    engine = get_workflow_engine()

    final_state = await engine.run(str(request.review_id), {"request": request, **INITIAL_STATE})

    assert len(final_state["findings"]) == 1
    finding = final_state["findings"][0]
    assert finding.severity == Severity.CRITICAL
    assert finding.category == "injection"
    assert final_state["outcome"] == ReviewOutcome.CRITICAL_BLOCK


async def test_pipeline_clean_diff_approves(db_session: AsyncSession) -> None:
    await _seed_mock_price(db_session)
    request = ReviewRequest(
        repo="acme/demo", pr_number=2, head_sha="def456", files=CLEAN_DIFF_FILES
    )
    engine = get_workflow_engine()

    final_state = await engine.run(str(request.review_id), {"request": request, **INITIAL_STATE})

    assert final_state["findings"] == []
    assert final_state["outcome"] == ReviewOutcome.APPROVED


async def test_pipeline_uses_deployed_groq_and_gemini_configuration(
    db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Run the graph using Render's provider/model configuration without
    contacting paid providers or the deployed database."""
    await seed_initial_prices(db_session)
    settings = Settings(
        llm_provider="groq",
        embedding_provider="gemini",
        embedding_model="gemini-embedding-001",
        gemini_api_key="test-gemini-key",
        daily_budget_usd=5.0,
    )
    model = resolve_model_for_provider(settings.llm_provider)
    llm_client = BudgetedLLMClient(
        MockLLMClient(),
        provider="groq",
        settings=settings,
        ledger=CostLedger(settings, get_sessionmaker()),
    )
    monkeypatch.setattr(nodes, "get_llm_client", lambda: llm_client)
    monkeypatch.setattr(base_agent, "get_settings", lambda: settings)
    gemini_models: list[str] = []

    class _FakeGeminiClient:
        def __init__(self, *, api_key: str) -> None:
            assert api_key == "test-gemini-key"
            self.aio = SimpleNamespace(models=self)

        async def embed_content(self, *, model: str, contents: str, config: object) -> object:
            gemini_models.append(model)
            return SimpleNamespace(embeddings=[SimpleNamespace(values=[0.0] * 768)])

    from google import genai

    monkeypatch.setattr(genai, "Client", _FakeGeminiClient)
    monkeypatch.setattr(
        context_retriever,
        "embed_text",
        lambda text: embedder.embed_text(text, settings),
    )
    monkeypatch.setattr(context_retriever, "hybrid_search_chunks", _empty_retrieval)

    request = ReviewRequest(
        repo="acme/demo", pr_number=10, head_sha="deployed-config", files=VULNERABLE_DIFF_FILES
    )
    final_state = await get_workflow_engine().run(
        str(request.review_id), {"request": request, **INITIAL_STATE}
    )

    assert model == "openai/gpt-oss-120b"
    assert settings.embedding_provider == "gemini"
    assert gemini_models == ["gemini-embedding-001"]
    assert final_state["outcome"] == ReviewOutcome.CRITICAL_BLOCK
    gemini_price = (
        await db_session.execute(
            select(PriceVersion).where(
                PriceVersion.provider == "gemini",
                PriceVersion.model == "gemini-embedding-001",
            )
        )
    ).scalar_one()
    assert gemini_price.currency == "USD"
    assert gemini_price.input_rate_per_million == 0
    assert "free tier" in gemini_price.source.lower()
    calls = (
        (
            await db_session.execute(
                select(ModelCall).where(
                    ModelCall.review_id == request.review_id,
                    ModelCall.provider == "groq",
                    ModelCall.model == model,
                )
            )
        )
        .scalars()
        .all()
    )
    assert len(calls) == 4
    assert all(call.cost_usd == 0 for call in calls)
    events = (
        (
            await db_session.execute(
                select(AgentEvent).where(AgentEvent.review_id == request.review_id)
            )
        )
        .scalars()
        .all()
    )
    assert all(
        event.payload is not None and "behavior_versions" in event.payload for event in events
    )
    behavior_versions = next(
        event.payload["behavior_versions"]
        for event in events
        if event.event_type == "llm.call" and event.payload is not None
    )
    assert behavior_versions["active_prompt_hash"]
    assert behavior_versions["tool_schema_hash"]
    assert behavior_versions["agent_graph_hash"]
    assert behavior_versions["guardrail_rules_hash"]
    event_types = {event.event_type for event in events}
    assert {"retrieval.complete", "llm.call", "hitl.gate"} <= event_types
    timed_events = [
        event
        for event in events
        if event.event_type in {"retrieval.complete", "llm.call", "hitl.gate"}
    ]
    assert timed_events
    assert all(event.latency_ms is not None for event in timed_events)
    assert all(event.ts.tzinfo is not None for event in timed_events)


async def test_injection_review_persists_separate_safety_metrics(db_session: AsyncSession) -> None:
    await _seed_mock_price(db_session)
    request = injection_review_request()
    final_state = await get_workflow_engine().run(
        str(request.review_id), {"request": request, **INITIAL_STATE}
    )
    assert final_state["request"].files[2].path == request.files[2].path

    metrics = await load_safety_metrics(
        db_session,
        start=datetime.now(timezone.utc) - timedelta(minutes=1),
        end=datetime.now(timezone.utc) + timedelta(minutes=1),
    )

    assert metrics.applicable_runs == 1
    assert metrics.not_applicable_runs == 0
    assert metrics.injection_attempt_runs == 1
    assert metrics.prompt_leak_runs == 0
    assert metrics.injection_attempt_rate == 1.0
    assert metrics.prompt_leak_rate == 0.0
    assert metrics.neutralization_rate == 1.0
    event = (
        await db_session.execute(
            select(AgentEvent).where(
                AgentEvent.review_id == request.review_id,
                AgentEvent.event_type == "safety.assessment",
            )
        )
    ).scalar_one()
    assert event.outcome == "applicable"
    assert event.payload is not None
    assert event.payload["injection_attempted"] is True
    assert event.payload["text_reached_prompt"] is False
    assert event.payload["neutralized"] is True


async def _empty_retrieval(
    repo: str, embedding: list[float], query_text: str, top_k: int
) -> list[str]:
    return []


@pytest.mark.usefixtures("db_session")
async def test_review_result_persists_and_is_readable(db_session):
    result = ReviewResult(
        review_id=uuid4(),
        findings=[],
        overall_confidence=1.0,
        outcome=ReviewOutcome.APPROVED,
        posted=True,
    )
    await save_review_result(db_session, "acme/demo", 3, "ghi789", result)

    fetched = await get_review(db_session, result.review_id)
    assert fetched is not None
    assert fetched.repo == "acme/demo"
    assert fetched.outcome == "approved"


@pytest.mark.usefixtures("db_session")
async def test_get_posted_review_finds_only_matching_posted_commit(db_session):
    """The idempotency check run_review_job relies on: a retry after a
    successful GitHub post must find that prior record, but a not-yet-posted
    record or a different commit on the same PR must not match."""
    posted = ReviewResult(
        review_id=uuid4(),
        findings=[],
        overall_confidence=1.0,
        outcome=ReviewOutcome.APPROVED,
        posted=True,
    )
    await save_review_result(db_session, "acme/demo", 4, "sha-posted", posted)

    unposted = ReviewResult(
        review_id=uuid4(),
        findings=[],
        overall_confidence=1.0,
        outcome=ReviewOutcome.APPROVED,
        posted=False,
    )
    await save_review_result(db_session, "acme/demo", 4, "sha-unposted", unposted)

    found = await get_posted_review(db_session, "acme/demo", 4, "sha-posted")
    assert found is not None
    assert found.id == posted.review_id

    assert await get_posted_review(db_session, "acme/demo", 4, "sha-unposted") is None
    assert await get_posted_review(db_session, "acme/demo", 4, "sha-never-seen") is None


@pytest.mark.usefixtures("db_session")
async def test_get_latest_review_ignores_posted_status_and_picks_most_recent(db_session):
    """Backs the MCP get_findings tool: unlike get_posted_review, this must
    return an escalated-but-not-posted review too — and the newest one for a
    PR that's been reviewed more than once."""
    older = ReviewResult(
        review_id=uuid4(),
        findings=[],
        overall_confidence=0.7,
        outcome=ReviewOutcome.ESCALATED,
        posted=False,
    )
    await save_review_result(db_session, "acme/demo", 5, "sha-old", older)

    newer = ReviewResult(
        review_id=uuid4(),
        findings=[],
        overall_confidence=0.98,
        outcome=ReviewOutcome.APPROVED,
        posted=True,
    )
    await save_review_result(db_session, "acme/demo", 5, "sha-new", newer)

    found = await get_latest_review(db_session, "acme/demo", 5)
    assert found is not None
    assert found.id == newer.review_id

    assert await get_latest_review(db_session, "acme/demo", 999999) is None
