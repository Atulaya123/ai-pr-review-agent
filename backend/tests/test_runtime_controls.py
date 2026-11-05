from unittest.mock import AsyncMock
from uuid import UUID

import pytest
from pydantic import ValidationError
from redis.asyncio import Redis
from redis.exceptions import ConnectionError
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.configuration import runtime_controls
from backend.configuration.runtime_controls import (
    CONTROL_KEY,
    get_runtime_controls,
    update_runtime_controls,
)
from backend.core.config import get_settings
from backend.database.models import HITLReview
from backend.database.repository import get_review
from backend.job_queue.arq_worker import run_review_job
from backend.models.enums import AgentType, ReviewOutcome, Severity
from backend.models.findings import Finding
from backend.orchestrator import nodes
from backend.orchestrator.state import ReviewState
from backend.tests.fixtures.vulnerable_diff import VULNERABLE_DIFF_FILES


def _finding() -> Finding:
    return Finding(
        agent_type=AgentType.SECURITY,
        severity=Severity.HIGH,
        category="test",
        summary="Fixture finding",
        file_path=VULNERABLE_DIFF_FILES[0].path,
        line_start=11,
        line_end=11,
        confidence=0.99,
        rationale="Fixture rationale",
    )


async def test_kill_switch_reread_after_review_suppresses_both_github_paths_and_keeps_records(
    db_session: AsyncSession,
) -> None:
    github = AsyncMock()
    engine = AsyncMock()
    for outcome in (ReviewOutcome.REQUEST_CHANGES, ReviewOutcome.ESCALATED):
        await update_runtime_controls({"posting_disabled": False})

        async def review(workflow_id: str, state: ReviewState) -> dict[str, object]:
            await update_runtime_controls({"posting_disabled": True})
            return {"findings": [_finding()], "overall_confidence": 0.99, "outcome": outcome}

        engine.run.side_effect = review
        review_id = await run_review_job(
            {"github_client": github, "workflow_engine": engine},
            repo="acme/demo",
            pr_number=71,
            installation_id=123,
            head_sha=f"kill-{outcome.value}",
            files=[f.model_dump() for f in VULNERABLE_DIFF_FILES],
        )
        record = await get_review(db_session, UUID(review_id))
        assert record is not None and not record.posted and len(record.findings) == 1
        assert record.outcome == outcome.value
        if outcome == ReviewOutcome.ESCALATED:
            queued = await db_session.scalar(
                select(HITLReview).where(HITLReview.review_id == UUID(review_id))
            )
            assert queued is not None and queued.status == "pending"
    assert engine.run.await_count == 2
    github.post_review.assert_not_called()
    github.request_human_review.assert_not_called()


async def test_force_hitl_routes_verified_high_confidence_finding_to_queue_without_redeployment(
    db_session: AsyncSession,
) -> None:
    engine = AsyncMock()
    github = AsyncMock()

    async def review(workflow_id: str, state: ReviewState) -> dict[str, object]:
        decision = await nodes.aggregate_node({**state, "security_findings": [_finding()]})
        await update_runtime_controls({"force_hitl": False})
        return decision

    engine.run.side_effect = review
    for forced in (False, True):
        await update_runtime_controls({"force_hitl": forced})
        review_id = await run_review_job(
            {"github_client": github, "workflow_engine": engine},
            repo="acme/demo",
            pr_number=72,
            installation_id=123,
            head_sha=f"force-{forced}",
            files=[f.model_dump() for f in VULNERABLE_DIFF_FILES],
        )
        record = await get_review(db_session, UUID(review_id))
        assert record is not None and record.overall_confidence == 0.99
        assert record.outcome == (
            ReviewOutcome.ESCALATED.value if forced else ReviewOutcome.REQUEST_CHANGES.value
        )
        queued = await db_session.scalar(
            select(HITLReview).where(HITLReview.review_id == UUID(review_id))
        )
        assert (queued is not None) == forced
        if queued is not None:
            assert queued.reason == "runtime force-HITL enabled"
    github.post_review.assert_awaited_once()
    github.request_human_review.assert_awaited_once()


@pytest.mark.parametrize("specialist", ["security", "quality", "tests", "docs"])
async def test_disable_specialist_skips_dispatch_and_can_be_reenabled_in_same_process(
    specialist: str,
    db_session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from backend.models.review import ReviewRequest

    agent = AsyncMock()
    finding = _finding()
    agent.review.return_value = [finding]
    factory = "build_tests_agent" if specialist == "tests" else f"build_{specialist}_agent"
    monkeypatch.setattr(nodes, factory, lambda client: agent)
    monkeypatch.setattr(nodes, "get_llm_client", lambda: object())
    request = ReviewRequest(repo="acme/demo", pr_number=73, head_sha="disable", files=[])
    state: ReviewState = {
        "request": request,
        "retrieved_context": "",
        "security_findings": [],
        "quality_findings": [],
        "tests_findings": [],
        "docs_findings": [],
        "findings": [],
        "overall_confidence": 1.0,
        "outcome": ReviewOutcome.APPROVED,
    }
    node = getattr(nodes, f"{specialist}_node")
    await update_runtime_controls({"disabled_specialist": specialist})
    assert await node(state) == {f"{specialist}_findings": []}
    agent.review.assert_not_called()
    await update_runtime_controls({"disabled_specialist": None})
    assert await node(state) == {f"{specialist}_findings": [finding]}
    agent.review.assert_awaited_once()


async def test_invalid_controls_fail_closed_and_invalid_updates_do_not_replace_valid_state() -> (
    None
):
    redis = Redis.from_url(get_settings().redis_url)
    try:
        await update_runtime_controls({"posting_disabled": True})
        with pytest.raises(ValidationError):
            await update_runtime_controls({"disabled_specialist": "aggregator"})
        assert (await get_runtime_controls()).posting_disabled
        await redis.set(CONTROL_KEY, '{"posting_disabled":"false"}')
        controls = await get_runtime_controls()
        assert controls.posting_disabled and controls.force_hitl
    finally:
        await redis.aclose()


async def test_unreachable_control_store_fails_closed(monkeypatch: pytest.MonkeyPatch) -> None:
    redis = AsyncMock()
    redis.get.side_effect = ConnectionError("credentials must not be logged")
    monkeypatch.setattr(runtime_controls, "_redis", lambda: redis)
    controls = await get_runtime_controls()
    assert controls.posting_disabled and controls.force_hitl
    redis.aclose.assert_awaited_once()
