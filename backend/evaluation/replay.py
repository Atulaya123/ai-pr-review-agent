import hashlib
import json
import os
from contextlib import ExitStack
from dataclasses import dataclass
from pathlib import Path
from unittest.mock import patch
from uuid import UUID

from pydantic import BaseModel

from backend.agents.base_agent import SpecialistAgent
from backend.core.config import Settings
from backend.database.cost_ledger import CallPurpose
from backend.models.enums import AgentType, ReviewOutcome, Severity
from backend.models.findings import Finding
from backend.models.review import ReviewRequest
from backend.prompts.registry import get_system_prompt
from backend.security.injection_guard import sanitize_review_request
from backend.observability.logging import review_log_context, log_stage
from backend.configuration.runtime_controls import RuntimeControls
from backend.tools.llm_client import LLMClient, LLMResponse

REPLAY_AGENTS = (
    AgentType.SECURITY,
    AgentType.QUALITY,
    AgentType.TESTS,
    AgentType.DOCS,
)


class RecordedModelResponse(BaseModel):
    text: str
    model: str
    tokens_in: int | None
    tokens_out: int | None
    raw_usage: dict[str, object] | None = None


class ReplayFinding(BaseModel):
    agent_type: AgentType
    severity: Severity
    category: str
    summary: str
    file_path: str
    line_start: int
    line_end: int
    evidence: str | None = None
    suggestion: str | None = None
    confidence: float
    rationale: str


class ReviewReplayFixture(BaseModel):
    fixture_set_version: str
    provenance: str
    name: str
    provider: str
    model_identifier: str
    prompt_hash: str
    request: ReviewRequest
    retrieved_context: str
    model_responses: dict[AgentType, RecordedModelResponse]
    expected_findings: list[ReplayFinding]
    expected_overall_confidence: float
    expected_outcome: ReviewOutcome
    evaluation_question: str | None = None
    reference_answer: str | None = None
    retrieved_symbols: list[str] = []
    relevant_symbols: set[str] = set()


@dataclass(frozen=True)
class ReplayResult:
    findings: list[Finding]
    overall_confidence: float
    outcome: ReviewOutcome
    model_calls: int


def load_replay_fixture(path: Path) -> ReviewReplayFixture:
    return ReviewReplayFixture.model_validate_json(path.read_text())


def replay_prompt_hash(fixture: ReviewReplayFixture) -> str:
    """Hash the exact system and user prompts for all four recorded calls."""
    request = sanitize_review_request(fixture.request).request
    prompts: list[dict[str, str]] = []
    for agent_type in REPLAY_AGENTS:
        agent = SpecialistAgent(agent_type, _UnusedLLMClient(), model=fixture.model_identifier)
        prompts.append(
            {
                "agent": agent_type.value,
                "system": get_system_prompt(agent_type.value),
                "user": agent._build_user_prompt(request, fixture.retrieved_context),
            }
        )
    serialized = json.dumps(prompts, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(serialized.encode("utf-8")).hexdigest()


async def run_replay_fixture(fixture: ReviewReplayFixture) -> ReplayResult:
    """Run the actual review graph with recorded responses and no external I/O."""
    if fixture.request.installation_id is not None:
        raise ValueError("provider-free replays cannot perform GitHub repository lookups")
    current_hash = replay_prompt_hash(fixture)
    if current_hash != fixture.prompt_hash:
        raise ValueError(
            f"replay {fixture.name!r} prompt hash mismatch: "
            f"recorded {fixture.prompt_hash}, current {current_hash}"
        )
    missing_agents = set(REPLAY_AGENTS) - fixture.model_responses.keys()
    if missing_agents:
        raise ValueError(
            f"replay is missing recorded responses for: {sorted(a.value for a in missing_agents)}"
        )

    client = _RecordedLLMClient(fixture.model_responses)

    async def no_external_retrieval(request: ReviewRequest) -> str:
        return fixture.retrieved_context

    async def no_event_writes(*args: object, **kwargs: object) -> None:
        return None

    with ExitStack() as stack:
        stack.enter_context(review_log_context(fixture.request))
        log_stage("review.enqueued", stage="enqueue")
        log_stage("worker.started", stage="pickup")

        async def replay_controls() -> RuntimeControls:
            return RuntimeControls()

        stack.enter_context(
            patch("backend.orchestrator.nodes.get_runtime_controls", replay_controls)
        )
        stack.enter_context(
            patch.dict(
                os.environ,
                {
                    "LANGCHAIN_TRACING": "",
                    "LANGCHAIN_HANDLER": "",
                    "LANGCHAIN_TRACING_V2": "",
                    "LANGSMITH_TRACING": "false",
                },
            )
        )
        stack.enter_context(patch("backend.orchestrator.nodes.get_llm_client", return_value=client))
        stack.enter_context(
            patch(
                "backend.agents.base_agent.get_settings",
                return_value=Settings(llm_provider=fixture.provider),
            )
        )
        stack.enter_context(
            patch("backend.orchestrator.nodes.get_retrieved_context", no_external_retrieval)
        )
        stack.enter_context(patch("backend.orchestrator.nodes.emit_agent_event", no_event_writes))
        stack.enter_context(patch("backend.agents.base_agent.emit_agent_event", no_event_writes))
        stack.enter_context(
            patch("backend.agents.base_agent.wait_for_groq_capacity", no_event_writes)
        )

        from backend.orchestrator.graph import build_review_graph

        graph = build_review_graph().compile()
        state = await graph.ainvoke(
            {
                "request": fixture.request,
                "retrieved_context": "",
                "security_findings": [],
                "quality_findings": [],
                "tests_findings": [],
                "docs_findings": [],
                "findings": [],
                "overall_confidence": 1.0,
                "outcome": ReviewOutcome.APPROVED,
            },
            config={"run_id": fixture.request.trace_id},
        )

    return ReplayResult(
        findings=state["findings"],
        overall_confidence=state["overall_confidence"],
        outcome=state["outcome"],
        model_calls=client.call_count,
    )


def finding_snapshot(finding: Finding) -> dict[str, object]:
    return finding.model_dump(mode="json", exclude={"id"})


class _RecordedLLMClient(LLMClient):
    def __init__(self, responses: dict[AgentType, RecordedModelResponse]) -> None:
        self._responses = responses
        self.call_count = 0

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
        if agent is None:
            raise ValueError("replay model calls must include the specialist agent")
        try:
            response = self._responses[AgentType(agent)]
        except (KeyError, ValueError) as exc:
            raise ValueError(f"no recorded response for agent {agent!r}") from exc
        if model != response.model:
            raise ValueError(
                f"replay model mismatch for {agent}: expected {response.model}, received {model}"
            )
        self.call_count += 1
        return LLMResponse(
            text=response.text,
            tokens_in=response.tokens_in,
            tokens_out=response.tokens_out,
            cost_usd=0.0,
            model=response.model,
            raw_usage=response.raw_usage,
        )


class _UnusedLLMClient(LLMClient):
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
        raise AssertionError("prompt hashing must not call a model")
