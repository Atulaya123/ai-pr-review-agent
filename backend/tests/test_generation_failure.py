import json
from unittest.mock import AsyncMock
from uuid import UUID

import pytest
from pydantic import ValidationError
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.agents.base_agent import SpecialistAgent
from backend.core.exceptions import ModelResponseParseError
from backend.core.workflow_engine import get_workflow_engine
from backend.database.models import AgentEvent, PRReviewRecord
from backend.job_queue.arq_worker import run_review_job
from backend.models.enums import AgentType, ReviewOutcome
from backend.models.review import ReviewFailure, ReviewRequest, ReviewResult
from backend.orchestrator import nodes
from backend.tests.fixtures.vulnerable_diff import CLEAN_DIFF_FILES
from backend.tools.llm_client import LLMClient, LLMResponse, MockLLMClient


async def _empty_context(request: ReviewRequest) -> str:
    return ""


def _request(pr_number: int) -> ReviewRequest:
    return ReviewRequest(
        repo="acme/demo",
        pr_number=pr_number,
        installation_id=123,
        head_sha=f"generation-{pr_number}",
        files=CLEAN_DIFF_FILES,
    )


async def _run(request: ReviewRequest, github: AsyncMock) -> str:
    return await run_review_job(
        {"github_client": github, "workflow_engine": get_workflow_engine()},
        repo=request.repo,
        pr_number=request.pr_number,
        installation_id=request.installation_id,
        head_sha=request.head_sha,
        files=[file.model_dump() for file in request.files],
        review_id=str(request.review_id),
        attempt_id=str(request.attempt_id),
        trace_id=str(request.trace_id),
    )


async def test_malformed_generation_fails_explicitly_and_never_posts_or_records_a_clean_review(
    db_session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    request = _request(81)
    github = AsyncMock()
    llm = AsyncMock(spec=LLMClient)
    llm.complete_json.return_value = LLMResponse(
        text="malformed secret response",
        model="mock",
        tokens_in=None,
        tokens_out=None,
        cost_usd=None,
    )
    monkeypatch.setattr(nodes, "get_llm_client", lambda: llm)
    monkeypatch.setattr(nodes, "get_retrieved_context", _empty_context)

    with pytest.raises(ModelResponseParseError) as error:
        await _run(request, github)
    assert error.value.stage == "model.parse"
    github.post_review.assert_not_called()
    github.request_human_review.assert_not_called()
    assert await db_session.get(PRReviewRecord, request.review_id) is None
    events = (
        (
            await db_session.execute(
                select(AgentEvent).where(AgentEvent.review_id == request.review_id)
            )
        )
        .scalars()
        .all()
    )
    failed = next(event for event in events if event.event_type == "review.failed")
    assert failed.outcome == ReviewOutcome.FAILED.value
    assert failed.payload is not None
    failure = ReviewFailure.model_validate(failed.payload)
    assert failure.stage == "model.parse" and failure.attempt_id == request.attempt_id
    assert any(event.event_type == "generation.failed" for event in events)
    assert not any(event.event_type in ("review.complete", "hitl.gate") for event in events)
    assert "malformed secret response" not in json.dumps(failed.payload)
    with pytest.raises(ValidationError):
        ReviewResult(
            review_id=request.review_id,
            findings=[],
            overall_confidence=1.0,
            outcome=failure.outcome,
        )


async def test_valid_clean_generation_still_records_and_posts_a_distinct_clean_verdict(
    db_session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    request = _request(82)
    github = AsyncMock()
    llm = AsyncMock(spec=LLMClient)
    llm.complete_json.return_value = LLMResponse(
        text='{"findings":[]}',
        model="mock",
        tokens_in=None,
        tokens_out=None,
        cost_usd=None,
    )
    monkeypatch.setattr(nodes, "get_llm_client", lambda: llm)
    monkeypatch.setattr(nodes, "get_retrieved_context", _empty_context)

    review_id = await _run(request, github)
    record = await db_session.get(PRReviewRecord, UUID(review_id))
    assert record is not None and record.outcome == ReviewOutcome.APPROVED.value
    assert record.findings == [] and record.posted
    github.post_review.assert_awaited_once()
    github.request_human_review.assert_not_called()
    events = (
        (
            await db_session.execute(
                select(AgentEvent).where(AgentEvent.review_id == request.review_id)
            )
        )
        .scalars()
        .all()
    )
    completed = next(event for event in events if event.event_type == "review.complete")
    assert completed.outcome == ReviewOutcome.APPROVED.value
    assert any(
        event.event_type == "generation.complete" and event.outcome == "clean" for event in events
    )
    assert not any(event.event_type.endswith(".failed") for event in events)


def test_invalid_envelopes_and_findings_never_become_empty_or_partial_success() -> None:
    agent = SpecialistAgent(AgentType.SECURITY, MockLLMClient(), model="mock")
    request = _request(83)
    valid = {
        "severity": "HIGH",
        "category": "test",
        "summary": "Fixture finding",
        "file_path": "utils.py",
        "line_start": 1,
        "line_end": 1,
        "confidence": 0.99,
        "rationale": "Fixture rationale",
    }
    responses = [
        {},
        [],
        {"findings": None},
        {"findings": [None]},
        {"findings": [{}]},
        {"findings": [{**valid, "confidence": 2.0}]},
        {"findings": [valid, {}]},
        {"findings": [{**valid, "file_path": []}]},
    ]
    for response in responses:
        with pytest.raises(ModelResponseParseError):
            agent._parse_findings(json.dumps(response), request)
