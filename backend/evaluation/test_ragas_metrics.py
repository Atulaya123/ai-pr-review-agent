import json
from uuid import UUID

import pytest

from backend.database.cost_ledger import CallPurpose
from backend.evaluation.ragas_metrics import EvaluationSample, evaluate_sample, metric_summary
from backend.models.enums import AgentType, Severity
from backend.models.findings import Finding
from backend.tools.llm_client import LLMClient, LLMResponse
from pathlib import Path
from backend.core.config import Settings
from backend.evaluation.experiments import run_configuration_comparison
from backend.evaluation.replay import load_replay_fixture
from backend.evaluation.test_experiments import PromptSensitiveClient
from mlflow.tracking import MlflowClient


class SchemaJudge(LLMClient):
    def __init__(self) -> None:
        self.purposes: list[str] = []

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
        self.purposes.append(purpose)
        if "StatementGeneratorOutput" in user:
            output: object = {"statements": ["Supported claim", "Unsupported claim"]}
        elif "NLIStatementOutput" in user:
            output = {
                "statements": [
                    {"statement": "Supported claim", "reason": "present", "verdict": 1},
                    {"statement": "Unsupported claim", "reason": "absent", "verdict": 0},
                ]
            }
        elif "ResponseRelevanceOutput" in user:
            output = {"question": "What is the issue?", "noncommittal": 0}
        elif "ContextRecallClassifications" in user:
            output = {
                "classifications": [
                    {"statement": "Supported claim", "reason": "present", "attributed": 1},
                    {"statement": "Unsupported claim", "reason": "absent", "attributed": 0},
                ]
            }
        elif '"verdict"' in user:
            output = {"reason": "Useful context", "verdict": 1}
        elif "faithfulness judge" in system:
            output = {"supported": True, "reasoning": "supported"}
        else:
            output = {"relevant": False, "reasoning": "irrelevant"}
        return LLMResponse(
            text=json.dumps(output), tokens_in=10, tokens_out=5, cost_usd=0.0, model=model
        )


async def test_real_ragas_algorithms_emit_all_scores_and_use_judge_purpose() -> None:
    judge = SchemaJudge()
    finding = Finding(
        agent_type=AgentType.QUALITY,
        severity=Severity.LOW,
        category="quality",
        summary="Claim",
        file_path="app.py",
        line_start=1,
        line_end=1,
        confidence=0.9,
        rationale="Supported and unsupported claims",
    )

    async def embed(text: str) -> list[float]:
        return [1.0, 0.0]

    scores = await evaluate_sample(
        EvaluationSample(
            name="two_claims",
            question="What is the issue?",
            response=finding.rationale,
            contexts=["Supported claim"],
            reference=finding.rationale,
            retrieved_symbols=["wrong", "right"],
            relevant_symbols={"right"},
            findings=[finding],
            diff_text="+code",
        ),
        client=judge,
        model="mock",
        embed=embed,
    )
    assert len(scores) == 8
    assert all(value.status == "measured" for value in scores.values())
    assert scores["ragas_faithfulness"].value == 0.5
    assert scores["ragas_context_recall"].value == 0.5
    assert scores["ragas_context_precision"].value == pytest.approx(1.0)
    assert scores["ragas_answer_relevancy"].value == pytest.approx(1.0)
    assert scores["mrr"].value == 0.5
    assert scores["judge_relevance"].value == 0
    assert set(judge.purposes) == {"judge"}


async def test_non_applicable_metrics_do_not_count_as_zero_or_pass() -> None:
    scores = await evaluate_sample(
        EvaluationSample(name="empty", question="Review", response="", contexts=[], diff_text=""),
        client=None,
        model="mock",
        embed=None,
    )
    assert all(value.status == "not_applicable" for value in scores.values())
    summary = metric_summary([scores])
    assert "ragas_faithfulness" not in summary
    assert summary["ragas_faithfulness_applicable_cases"] == 0
    assert summary["ragas_faithfulness_not_applicable_cases"] == 1


async def test_mlflow_logs_all_eight_evaluation_metrics_per_configuration(tmp_path: Path) -> None:
    fixture = load_replay_fixture(
        Path(__file__).parents[1] / "tests/fixtures/replays/known_good.json"
    ).model_copy(
        update={
            "retrieved_context": "Arithmetic helpers are supported.",
            "reference_answer": "Arithmetic helpers are supported.",
            "retrieved_symbols": ["helper"],
            "relevant_symbols": {"helper"},
        }
    )

    async def embed(text: str) -> list[float]:
        return [1.0, 0.0]

    uri = f"sqlite:///{tmp_path / 'all-metrics.db'}"
    comparison = await run_configuration_comparison(
        [fixture],
        candidate_agent=AgentType.DOCS,
        candidate_prompt="Different docs prompt",
        settings=Settings.model_construct(llm_provider="mock"),
        tracking_uri=uri,
        client_factory=lambda settings: PromptSensitiveClient(),
        judge_factory=lambda settings: SchemaJudge(),
        evaluation_embed=embed,
    )
    for configuration in (comparison.baseline, comparison.candidate):
        assert len(configuration.cases[0].evaluation_metrics) == 8
        assert all(
            metric.status == "measured"
            for metric in configuration.cases[0].evaluation_metrics.values()
        )
    runs = MlflowClient(tracking_uri=uri).search_runs(["1"])
    config_runs = [run for run in runs if run.data.params.get("configuration_name")]
    assert len(config_runs) == 2
    for run in config_runs:
        assert "ragas_context_precision" in run.data.metrics
        assert run.data.metrics["ragas_context_precision_applicable_cases"] == 1
