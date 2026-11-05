from pathlib import Path
from uuid import UUID

from backend.core.config import Settings
from backend.database.cost_ledger import CallPurpose
from backend.evaluation.experiments import run_configuration_comparison
from backend.evaluation.replay import load_replay_fixture
from backend.models.enums import AgentType
from backend.tools.llm_client import LLMClient, LLMResponse

_FIXTURE = Path(__file__).parents[1] / "tests" / "fixtures" / "replays" / "known_good.json"


class PromptSensitiveClient(LLMClient):
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
        if agent == "security" and "CANDIDATE_FIXTURE_BEHAVIOR" not in system:
            output = (
                '{"findings":[{"severity":"LOW","category":"style",'
                '"summary":"Unnecessary arithmetic helper","file_path":"utils.py",'
                '"line_start":2,"line_end":2,"confidence":0.9,'
                '"rationale":"This is a deliberately unsupported baseline finding."}]}'
            )
        else:
            output = '{"findings":[]}'
        return LLMResponse(
            text=output,
            tokens_in=10,
            tokens_out=5,
            cost_usd=0.001,
            model=model,
        )


async def test_mlflow_comparison_names_fixture_changes_and_versions_prompt(
    tmp_path: Path,
) -> None:
    fixture = load_replay_fixture(_FIXTURE)
    candidate_prompt = (
        Path(__file__).parents[1] / "prompts" / "templates" / "security.txt"
    ).read_text() + "\nCANDIDATE_FIXTURE_BEHAVIOR"

    comparison = await run_configuration_comparison(
        [fixture],
        candidate_agent=AgentType.SECURITY,
        candidate_prompt=candidate_prompt,
        settings=Settings(llm_provider="mock"),
        tracking_uri=f"sqlite:///{tmp_path / 'mlflow.db'}",
        client_factory=lambda settings: PromptSensitiveClient(),
    )

    assert comparison.baseline.manifest.manifest_hash != comparison.candidate.manifest.manifest_hash
    assert comparison.baseline.manifest.behavior_versions["prompt_library_hash"] != (
        comparison.candidate.manifest.behavior_versions["prompt_library_hash"]
    )
    assert comparison.fixture_changes[0]["fixture"] == "known_good"
    assert comparison.fixture_changes[0]["status"] == "improved"
    assert comparison.fixture_changes[0]["finding_changes"] == [
        {"summary": "Unnecessary arithmetic helper", "status": "improved"}
    ]
    assert comparison.aggregate_deltas["finding_f1"] > 0
