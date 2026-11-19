import json
from typing import Any
from unittest.mock import AsyncMock
from uuid import UUID

import pytest
from pydantic import ValidationError
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.agents.base_agent import SpecialistAgent
from backend.configuration.runtime_controls import RuntimeControls
from backend.core.exceptions import ModelResponseParseError
from backend.core.workflow_engine import WorkflowEngine, get_workflow_engine
from backend.database.models import AgentEvent, PRReviewRecord
from backend.job_queue import arq_worker
from backend.job_queue.arq_worker import run_review_job
from backend.models.enums import AgentType, ReviewOutcome
from backend.models.review import ReviewFailure, ReviewRequest, ReviewResult
from backend.orchestrator import nodes
from backend.orchestrator.graph import build_review_graph
from backend.tests.fixtures.vulnerable_diff import CLEAN_DIFF_FILES
from backend.tools.llm_client import LLMClient, LLMResponse, MockLLMClient


async def _empty_context(request: ReviewRequest) -> str:
    return ""


class _GraphWithoutCheckpoint(WorkflowEngine):
    async def run(self, workflow_id: str, input: dict[str, Any]) -> dict[str, Any]:
        graph = build_review_graph().compile()
        return await graph.ainvoke(input, config={"configurable": {"thread_id": workflow_id}})

    async def resume(self, workflow_id: str, state: dict[str, Any]) -> dict[str, Any]:
        return await self.run(workflow_id, state)

    async def get_state(self, workflow_id: str) -> dict[str, Any] | None:
        return None


def _request(pr_number: int) -> ReviewRequest:
    return ReviewRequest(
        repo="acme/demo",
        pr_number=pr_number,
        installation_id=123,
        head_sha=f"generation-{pr_number}",
        files=CLEAN_DIFF_FILES,
    )


async def _run(
    request: ReviewRequest, github: AsyncMock, engine: WorkflowEngine | None = None
) -> str:
    return await run_review_job(
        {"github_client": github, "workflow_engine": engine or get_workflow_engine()},
        repo=request.repo,
        pr_number=request.pr_number,
        installation_id=request.installation_id,
        head_sha=request.head_sha,
        files=[file.model_dump() for file in request.files],
        review_id=str(request.review_id),
        attempt_id=str(request.attempt_id),
        trace_id=str(request.trace_id),
    )


@pytest.mark.parametrize("posting_disabled", [False, True])
async def test_malformed_generation_fails_required_check_and_never_records_clean_review(
    db_session: AsyncSession,
    _clear_test_checkpoints: None,
    monkeypatch: pytest.MonkeyPatch,
    posting_disabled: bool,
) -> None:
    request = _request(81)
    github = AsyncMock()
    github.set_review_check.return_value = "check-run-81"
    llm = AsyncMock(spec=LLMClient)
    llm.complete_json.return_value = LLMResponse(
        text="malformed secret response",
        model="mock",
        tokens_in=None,
        tokens_out=None,
        cost_usd=None,
    )

    async def controls() -> RuntimeControls:
        return RuntimeControls(posting_disabled=posting_disabled)

    monkeypatch.setattr(nodes, "get_llm_client", lambda: llm)
    monkeypatch.setattr(nodes, "get_retrieved_context", _empty_context)
    monkeypatch.setattr(nodes, "get_runtime_controls", controls)
    monkeypatch.setattr(arq_worker, "get_runtime_controls", controls)

    with pytest.raises(ModelResponseParseError) as error:
        await _run(request, github)
    assert error.value.stage == "model.parse"
    github.set_review_check.assert_awaited_once_with(
        request.repo,
        request.pr_number,
        request.head_sha,
        123,
        conclusion="failure",
        summary="The review failed at `model.parse` and produced no valid verdict. Retry the review before merging.",
    )
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
    check = next(event for event in events if event.event_type == "review.check_completed")
    assert check.outcome == "failure"
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
    _clear_test_checkpoints: None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    request = _request(82)
    github = AsyncMock()
    github.set_review_check.return_value = "check-run-82"
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
    github.set_review_check.assert_awaited_once_with(
        request.repo,
        request.pr_number,
        request.head_sha,
        123,
        conclusion="success",
        summary=(
            "The review completed with a valid parsed response. This check signals "
            "completion, not finding severity or review outcome."
        ),
    )
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


async def test_wrong_shape_json_fails_explicitly_and_never_posts_or_records(
    db_session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    request = _request(84).model_copy(update={"installation_id": None})
    github = AsyncMock()
    llm = AsyncMock(spec=LLMClient)
    llm.complete_json.return_value = LLMResponse(
        text='{"findings":[{"severity":"HIGH"}]}',
        model="mock",
        tokens_in=None,
        tokens_out=None,
        cost_usd=None,
    )

    async def default_controls() -> RuntimeControls:
        return RuntimeControls()

    monkeypatch.setattr(nodes, "get_llm_client", lambda: llm)
    monkeypatch.setattr(nodes, "get_retrieved_context", _empty_context)
    monkeypatch.setattr(nodes, "get_runtime_controls", default_controls)

    with pytest.raises(ModelResponseParseError) as error:
        await _run(request, github, engine=_GraphWithoutCheckpoint())

    assert error.value.stage == "model.parse"
    github.set_review_check.assert_not_called()
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
    assert failure.stage == "model.parse"
    assert failure.cause_type == "KeyError"
    assert any(event.event_type == "generation.failed" for event in events)
    unavailable = next(event for event in events if event.event_type == "review.check_unavailable")
    assert unavailable.outcome == "missing_installation_id"
    assert not any(event.event_type in ("review.complete", "hitl.gate") for event in events)


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
