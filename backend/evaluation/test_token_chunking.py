import json
from pathlib import Path
from uuid import UUID

import pytest

from backend.agents import base_agent
from backend.agents.base_agent import SpecialistAgent
from backend.core.config import Settings
from backend.core.exceptions import TokenBudgetExceededError
from backend.database.cost_ledger import CallPurpose
from backend.models.enums import AgentType, ReviewOutcome
from backend.models.review import DiffFile, ReviewRequest
from backend.orchestrator import nodes
from backend.orchestrator.graph import build_review_graph
from backend.prompts.registry import get_system_prompt, prompt_library_hash
from backend.reliability import token_budget
from backend.reliability.token_budget import chunk_review_request, estimate_call_tokens
from backend.security.injection_guard import sanitize_review_request
from backend.tools.llm_client import LLMClient, LLMResponse

_PR14 = Path(__file__).parents[1] / "tests" / "fixtures" / "pr14_diff.json"


class _TokenCeilingClient(LLMClient):
    def __init__(self) -> None:
        self.calls: list[tuple[str | None, str]] = []

    async def complete_json(
        self,
        *,
        system: str,
        user: str,
        model: str,
        max_output_tokens: int = 2048,
        purpose: CallPurpose = "agent",
        review_id: UUID | None = None,
        agent: str | None = None,
    ) -> LLMResponse:
        if estimate_call_tokens(system, user, max_output_tokens) > 8000:
            raise TokenBudgetExceededError("Recorded token-ceiling failure")
        self.calls.append((agent, user))
        response = {
            "findings": [
                {
                    "severity": "LOW",
                    "category": "quality",
                    "summary": "Repeated recorded finding",
                    "file_path": "backend/api/main.py",
                    "line_start": 1,
                    "line_end": 1,
                    "confidence": 0.95,
                    "rationale": "Synthetic response to exercise cross-chunk deduplication.",
                }
            ]
        }
        return LLMResponse(
            text=json.dumps(response), tokens_in=None, tokens_out=None, cost_usd=None, model=model
        )


async def test_pr14_diff_replays_all_specialists_under_groq_limit_and_deduplicates(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Original PR patches, synthetic responses: no live provider or Redis calls."""
    fixture = json.loads(_PR14.read_text())
    assert fixture["prompt_library_hash"] == prompt_library_hash()
    request = ReviewRequest(
        repo="Atulaya123/ai-pr-review-agent",
        pr_number=14,
        head_sha=fixture["source_commit"],
        files=[DiffFile.model_validate(file) for file in fixture["files"]],
    )
    client = _TokenCeilingClient()
    settings = Settings.model_construct(llm_provider="groq")
    events: list[tuple[str, dict[str, object]]] = []
    reservations: list[int] = []

    async def event(review_id: UUID, agent: object, event_type: str, **kwargs: object) -> None:
        events.append((event_type, kwargs))

    async def retrieval(request: ReviewRequest) -> str:
        return ""

    async def capacity(*, tokens: int, token_limit: int, model: str, redis_url: str) -> None:
        assert tokens <= token_limit
        reservations.append(tokens)

    monkeypatch.setattr(base_agent, "get_settings", lambda: settings)
    monkeypatch.setattr(base_agent, "wait_for_groq_capacity", capacity)
    monkeypatch.setattr(base_agent, "emit_agent_event", event)
    monkeypatch.setattr(nodes, "get_llm_client", lambda: client)
    monkeypatch.setattr(nodes, "get_retrieved_context", retrieval)
    monkeypatch.setattr(nodes, "emit_agent_event", event)

    agent = SpecialistAgent(AgentType.SECURITY, client, model=fixture["model_identifier"])
    with pytest.raises(TokenBudgetExceededError):
        await client.complete_json(
            system=get_system_prompt("security"),
            user=agent._build_user_prompt(request, ""),
            model=fixture["model_identifier"],
        )

    state = (
        await build_review_graph()
        .compile()
        .ainvoke(
            {
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
        )
    )
    assert len(client.calls) == len(reservations) == 8
    for specialist in ("security", "quality", "tests", "docs"):
        prompts = [user for agent_name, user in client.calls if agent_name == specialist]
        assert len(prompts) == 2
        for file in sanitize_review_request(request).request.files:
            assert sum(f"--- {file.path} ---" in prompt for prompt in prompts) == 1
            if file.patch:
                assert sum(file.patch in prompt for prompt in prompts) == 1
    assert len(state["findings"]) == 1
    chunk_events = [details for name, details in events if name == "review.chunking"]
    assert len(chunk_events) == 4
    for details in chunk_events:
        payload = details["payload"]
        assert isinstance(payload, dict)
        assert payload["chunk_count"] == 2
        assert payload["token_counts_estimated"] is True


def test_oversized_file_is_not_split_or_silently_dropped() -> None:
    request = ReviewRequest(
        repo="owner/repo",
        pr_number=1,
        head_sha="abc",
        files=[DiffFile(path="huge.py", patch="x" * 100)],
    )
    with pytest.raises(TokenBudgetExceededError, match="no file was split"):
        chunk_review_request(
            request,
            estimate=lambda chunk: sum(len(file.patch) for file in chunk.files),
            token_limit=50,
        )


def test_small_request_preserves_unchunked_behavior() -> None:
    request = ReviewRequest(
        repo="owner/repo", pr_number=1, head_sha="abc", files=[DiffFile(path="small.py", patch="x")]
    )
    assert chunk_review_request(request, estimate=lambda chunk: 1, token_limit=50) == [request]


async def test_shared_groq_capacity_waits_for_expiry_before_admission(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    waits: list[float] = []

    class FakeRedis:
        def __init__(self) -> None:
            self.replies = iter([60000, 0])
            self.closed = False

        async def eval(self, *args: object) -> int:
            return next(self.replies)

        async def aclose(self) -> None:
            self.closed = True

    redis = FakeRedis()

    async def sleep(delay: float) -> None:
        waits.append(delay)

    monkeypatch.setattr(token_budget.Redis, "from_url", lambda url: redis)
    monkeypatch.setattr(token_budget.asyncio, "sleep", sleep)
    await token_budget.wait_for_groq_capacity(
        tokens=4000, token_limit=8000, model="test-model", redis_url="redis://unused"
    )
    assert waits == [60.0]
    assert redis.closed
