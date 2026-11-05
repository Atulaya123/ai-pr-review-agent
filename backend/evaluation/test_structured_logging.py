import json
import logging
from pathlib import Path

import pytest

from backend.agents.base_agent import SpecialistAgent
from backend.core.exceptions import StageExecutionError
from backend.evaluation.replay import load_replay_fixture, run_replay_fixture
from backend.models.enums import AgentType
from backend.models.review import ReviewRequest
from backend.observability.logging import ReviewJSONFormatter
from backend.tools.llm_client import MockLLMClient


async def test_replay_path_is_reconstructable_from_json_logs_without_langsmith(
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.INFO, logger="backend.review_trace")
    fixture = load_replay_fixture(
        Path(__file__).parents[1] / "tests/fixtures/replays/known_hallucinated.json"
    )
    result = await run_replay_fixture(fixture)
    formatter = ReviewJSONFormatter()
    rows = [
        json.loads(formatter.format(record))
        for record in caplog.records
        if hasattr(record, "review_event")
    ]
    events = [row["event"] for row in rows]
    assert events[:4] == [
        "review.enqueued",
        "worker.started",
        "retrieval.started",
        "retrieval.completed",
    ]
    assert events.count("model.started") == events.count("model.completed") == 4
    assert events[-1] == "gate.decided"
    finding = result.findings[0]
    path = [row for row in rows if row.get("finding_id") == str(finding.id)]
    assert [row["event"] for row in path] == ["finding.proposed", "finding.verified"]
    assert path[-1]["outcome"] == "failed"
    assert rows[-1]["finding_ids"] == [str(finding.id)]
    assert all(row["trace_id"] == str(fixture.request.trace_id) for row in rows)
    assert all(row["attempt_id"] == str(fixture.request.attempt_id) for row in rows)
    assert all(row["behavior_versions"]["prompt_library_hash"] for row in rows)


def test_formatter_redacts_at_boundary_even_for_unreviewed_call_sites() -> None:
    record = logging.LogRecord(
        "backend.new_module",
        logging.ERROR,
        "",
        1,
        "raw model output %s",
        ("private diff and credentials",),
        (ValueError, ValueError("sensitive exception text"), None),
    )
    record.review_fields = {
        "raw_output": "attacker output",
        "diff": "full diff",
        "outcome": "Bearer very-secret-token",
        "api_key": "gsk_supersecret",
    }
    rendered = ReviewJSONFormatter().format(record)
    assert "private diff" not in rendered
    assert "attacker output" not in rendered
    assert "very-secret-token" not in rendered
    assert "gsk_supersecret" not in rendered
    assert "sensitive exception text" not in rendered
    assert json.loads(rendered)["outcome"] == "[redacted]"


async def test_model_exception_preserves_stage_and_cause_without_secret_message(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    agent = SpecialistAgent(AgentType.SECURITY, MockLLMClient(), model="mock")

    async def fail(request: ReviewRequest, retrieved_context: str) -> list[object]:
        raise ValueError("secret provider output")

    monkeypatch.setattr(agent, "_review_chunk", fail)
    request = ReviewRequest(repo="owner/repo", pr_number=1, head_sha="a", files=[])
    with pytest.raises(StageExecutionError) as error:
        await agent.review(request)
    assert error.value.stage == "model"
    assert error.value.cause_type == "ValueError"
    assert isinstance(error.value.__cause__, ValueError)
    assert "secret provider output" not in str(error.value)
