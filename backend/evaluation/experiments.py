import hashlib
import json
import os
import tempfile
import time
from collections.abc import Awaitable, Iterable
from dataclasses import dataclass
from pathlib import Path
from types import ModuleType
from typing import Callable
from unittest.mock import patch
from uuid import UUID

import mlflow
from pydantic import BaseModel, ConfigDict

from backend.agents.base_agent import resolve_model_for_provider
from backend.configuration.versions import build_behavior_manifest
from backend.core.config import Settings, get_settings
from backend.database.cost_ledger import CallPurpose
from backend.evaluation.replay import (
    ReplayFinding,
    ReviewReplayFixture,
    finding_snapshot,
)
from backend.models.enums import AgentType, ReviewOutcome
from backend.models.findings import Finding
from backend.prompts.registry import get_system_prompt, use_prompt_overrides
from backend.tools.llm_client import (
    LLMClient,
    LLMResponse,
    get_llm_client,
)

ClientFactory = Callable[[Settings], LLMClient]
RetrievalRunner = Callable[[ReviewReplayFixture, Settings], Awaitable[str]]


class ExperimentManifest(BaseModel):
    model_config = ConfigDict(frozen=True)

    name: str
    fixture_set_version: str
    fixture_names: tuple[str, ...]
    fixture_content_hash: str
    behavior_versions: dict[str, object]
    manifest_hash: str


class FindingScorecard(BaseModel):
    precision: float
    recall: float
    f1: float
    expected_findings: int
    predicted_findings: int
    matched_findings: int


class FixtureExperimentResult(BaseModel):
    name: str
    findings: list[dict[str, object]]
    expected_findings: list[dict[str, object]]
    outcome: ReviewOutcome
    overall_confidence: float
    latency_ms: float
    known_cost_usd: float
    unknown_cost_calls: int
    scorecard: FindingScorecard


class ConfigurationExperimentResult(BaseModel):
    manifest: ExperimentManifest
    cases: list[FixtureExperimentResult]
    mean_finding_f1: float
    escalation_rate: float
    mean_latency_ms: float
    known_cost_usd: float
    unknown_cost_calls: int
    coverage: float


class ExperimentComparison(BaseModel):
    baseline: ConfigurationExperimentResult
    candidate: ConfigurationExperimentResult
    fixture_changes: list[dict[str, object]]
    aggregate_deltas: dict[str, float]


@dataclass
class _RunUsage:
    known_cost_usd: float = 0.0
    unknown_cost_calls: int = 0


async def run_configuration_comparison(
    fixtures: list[ReviewReplayFixture],
    *,
    candidate_agent: AgentType | None = None,
    candidate_prompt: str | None = None,
    candidate_retrieval_mode: str | None = None,
    retrieval_runner: RetrievalRunner | None = None,
    settings: Settings | None = None,
    tracking_uri: str | None = None,
    experiment_name: str = "ai-pr-review-config-comparison",
    client_factory: ClientFactory | None = None,
) -> ExperimentComparison:
    """Run baseline and one-prompt candidate on identical fixtures, tracking both in MLflow."""
    if not fixtures:
        raise ValueError("configuration comparison requires at least one fixture")
    fixture_versions = {fixture.fixture_set_version for fixture in fixtures}
    if len(fixture_versions) != 1:
        raise ValueError("all comparison fixtures must have the same fixture-set version")

    active_settings = settings or get_settings()
    prompt_change = candidate_agent is not None and candidate_prompt is not None
    if prompt_change == (candidate_retrieval_mode is not None):
        raise ValueError("change exactly one prompt or retrieval mode")
    if (candidate_agent is None) != (candidate_prompt is None):
        raise ValueError("candidate prompt and agent must be supplied together")
    if candidate_retrieval_mode not in (None, "whole_document", "bounded_or"):
        raise ValueError("unsupported retrieval mode")
    if candidate_retrieval_mode is not None and retrieval_runner is None:
        raise ValueError("retrieval comparisons require an actual retrieval runner")
    candidate_settings = (
        active_settings.model_copy(update={"sparse_query_mode": candidate_retrieval_mode})
        if candidate_retrieval_mode
        else active_settings
    )
    model_identifier = resolve_model_for_provider(active_settings.llm_provider)
    factory = client_factory or get_llm_client
    fixture_set_version = next(iter(fixture_versions))
    baseline = await _run_configuration(
        fixtures,
        name="baseline",
        prompt_overrides={},
        settings=active_settings,
        model_identifier=model_identifier,
        client_factory=factory,
        tracking_uri=tracking_uri,
        experiment_name=experiment_name,
        fixture_set_version=fixture_set_version,
        retrieval_runner=retrieval_runner,
    )
    candidate = await _run_configuration(
        fixtures,
        name="candidate",
        prompt_overrides=(
            {candidate_agent.value: candidate_prompt}
            if candidate_agent is not None and candidate_prompt is not None
            else {}
        ),
        settings=candidate_settings,
        model_identifier=model_identifier,
        client_factory=factory,
        tracking_uri=tracking_uri,
        experiment_name=experiment_name,
        fixture_set_version=fixture_set_version,
        retrieval_runner=retrieval_runner,
    )
    comparison = _compare_results(baseline, candidate)
    _log_comparison(comparison, tracking_uri=tracking_uri, experiment_name=experiment_name)
    return comparison


async def _run_configuration(
    fixtures: list[ReviewReplayFixture],
    *,
    name: str,
    prompt_overrides: dict[str, str],
    settings: Settings,
    model_identifier: str,
    client_factory: ClientFactory,
    tracking_uri: str | None,
    experiment_name: str,
    fixture_set_version: str,
    retrieval_runner: RetrievalRunner | None,
) -> ConfigurationExperimentResult:
    _configure_mlflow(mlflow, tracking_uri, experiment_name)
    with use_prompt_overrides(prompt_overrides):
        behavior_versions = build_behavior_manifest(settings=settings, model=model_identifier)
        prompt_texts = {
            agent_name: get_system_prompt(agent_name)
            for agent_name in ("security", "quality", "tests", "docs")
        }
    manifest_payload: dict[str, object] = {
        "name": name,
        "fixture_set_version": fixture_set_version,
        "fixture_names": tuple(fixture.name for fixture in fixtures),
        "behavior_versions": behavior_versions,
        "fixture_content_hash": _digest([fixture.model_dump(mode="json") for fixture in fixtures]),
    }
    manifest_hash = _digest(manifest_payload)
    manifest = ExperimentManifest(
        name=name,
        fixture_set_version=fixture_set_version,
        fixture_names=tuple(fixture.name for fixture in fixtures),
        fixture_content_hash=str(manifest_payload["fixture_content_hash"]),
        behavior_versions=behavior_versions,
        manifest_hash=manifest_hash,
    )
    usage = _RunUsage()
    case_results: list[FixtureExperimentResult] = []

    with mlflow.start_run(run_name=name):
        mlflow.log_params(
            {
                "configuration_name": name,
                "fixture_set_version": fixture_set_version,
                "fixture_count": len(fixtures),
                "model_provider": settings.llm_provider,
                "model_identifier": model_identifier,
                "prompt_library_hash": str(behavior_versions["prompt_library_hash"]),
                "configuration_hash": manifest_hash,
                "candidate_agent": next(iter(prompt_overrides), "none"),
                "candidate_prompt_hash": (
                    hashlib.sha256(
                        next(iter(prompt_overrides.values())).encode("utf-8")
                    ).hexdigest()
                    if prompt_overrides
                    else "none"
                ),
            }
        )
        mlflow.log_dict(manifest.model_dump(mode="json"), "manifest.json")
        mlflow.log_dict(prompt_texts, "prompts.json")
        with tempfile.TemporaryDirectory(prefix="aipr-experiment-") as temp_dir:
            for fixture in fixtures:
                case_result = await _run_fixture(
                    fixture,
                    settings=settings,
                    model_identifier=model_identifier,
                    prompt_overrides=prompt_overrides,
                    client_factory=client_factory,
                    usage=usage,
                    retrieval_runner=retrieval_runner,
                )
                case_results.append(case_result)
                artifact_path = Path(temp_dir) / f"{fixture.name}.json"
                artifact_path.write_text(case_result.model_dump_json(indent=2))
            mlflow.log_artifacts(temp_dir, artifact_path="fixtures")

        mean_f1 = _mean(case.scorecard.f1 for case in case_results)
        escalation_rate = sum(
            case.outcome == ReviewOutcome.ESCALATED for case in case_results
        ) / len(fixtures)
        mean_latency_ms = _mean(case.latency_ms for case in case_results)
        mlflow.log_metrics(
            {
                "finding_f1": mean_f1,
                "escalation_rate": escalation_rate,
                "mean_latency_ms": mean_latency_ms,
                "known_cost_usd": usage.known_cost_usd,
                "unknown_cost_calls": float(usage.unknown_cost_calls),
                "coverage": len(case_results) / len(fixtures),
            }
        )

    return ConfigurationExperimentResult(
        manifest=manifest,
        cases=case_results,
        mean_finding_f1=mean_f1,
        escalation_rate=escalation_rate,
        mean_latency_ms=mean_latency_ms,
        known_cost_usd=usage.known_cost_usd,
        unknown_cost_calls=usage.unknown_cost_calls,
        coverage=len(case_results) / len(fixtures),
    )


async def _run_fixture(
    fixture: ReviewReplayFixture,
    *,
    settings: Settings,
    model_identifier: str,
    prompt_overrides: dict[str, str],
    client_factory: ClientFactory,
    usage: _RunUsage,
    retrieval_runner: RetrievalRunner | None,
) -> FixtureExperimentResult:
    if fixture.request.installation_id is not None:
        raise ValueError("experiments cannot perform GitHub repository lookups")

    class _UsageTrackingClient(LLMClient):
        def __init__(self, client: LLMClient) -> None:
            self._client = client

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
            response = await self._client.complete_json(
                system=system,
                user=user,
                model=model,
                max_output_tokens=max_output_tokens,
                purpose=purpose,
                review_id=review_id,
                agent=agent,
            )
            if response.cost_usd is None:
                usage.unknown_cost_calls += 1
            else:
                usage.known_cost_usd += response.cost_usd
            return response

    client = _UsageTrackingClient(client_factory(settings))

    async def fixed_retrieval(request: object) -> str:
        return (
            await retrieval_runner(fixture, settings)
            if retrieval_runner is not None
            else fixture.retrieved_context
        )

    async def no_event_writes(*args: object, **kwargs: object) -> None:
        return None

    known_cost_before = usage.known_cost_usd
    unknown_calls_before = usage.unknown_cost_calls
    started_ns = time.monotonic_ns()
    with (
        patch.dict(
            os.environ,
            {
                "LANGCHAIN_TRACING": "",
                "LANGCHAIN_HANDLER": "",
                "LANGCHAIN_TRACING_V2": "",
                "LANGSMITH_TRACING": "false",
            },
        ),
        use_prompt_overrides(prompt_overrides),
        patch("backend.orchestrator.nodes.get_llm_client", return_value=client),
        patch("backend.orchestrator.nodes.get_retrieved_context", fixed_retrieval),
        patch("backend.orchestrator.nodes.emit_agent_event", no_event_writes),
        patch("backend.agents.base_agent.emit_agent_event", no_event_writes),
        patch("backend.agents.base_agent.get_settings", return_value=settings),
        patch(
            "backend.agents.base_agent.resolve_model_for_provider",
            return_value=model_identifier,
        ),
        patch("backend.orchestrator.nodes.GitHubClient"),
    ):
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
            }
        )
    latency_ms = (time.monotonic_ns() - started_ns) / 1_000_000
    predicted = [finding_snapshot(finding) for finding in state["findings"]]
    scorecard = _score_findings(fixture.expected_findings, state["findings"])
    return FixtureExperimentResult(
        name=fixture.name,
        findings=predicted,
        expected_findings=[
            finding.model_dump(mode="json") for finding in fixture.expected_findings
        ],
        outcome=state["outcome"],
        overall_confidence=state["overall_confidence"],
        latency_ms=latency_ms,
        known_cost_usd=usage.known_cost_usd - known_cost_before,
        unknown_cost_calls=usage.unknown_cost_calls - unknown_calls_before,
        scorecard=scorecard,
    )


def _score_findings(expected: list[ReplayFinding], predicted: list[Finding]) -> FindingScorecard:
    def key(item: ReplayFinding | Finding) -> tuple[str, str, str, int, int]:
        return (
            item.agent_type.value,
            item.category,
            item.file_path,
            item.line_start,
            item.line_end,
        )

    expected_keys = {key(item) for item in expected}
    predicted_keys = {key(item) for item in predicted}
    matched = len(expected_keys & predicted_keys)
    precision = matched / len(predicted_keys) if predicted_keys else float(not expected_keys)
    recall = matched / len(expected_keys) if expected_keys else 1.0
    f1 = 2 * precision * recall / (precision + recall) if precision + recall else 0.0
    return FindingScorecard(
        precision=precision,
        recall=recall,
        f1=f1,
        expected_findings=len(expected_keys),
        predicted_findings=len(predicted_keys),
        matched_findings=matched,
    )


def _compare_results(
    baseline: ConfigurationExperimentResult,
    candidate: ConfigurationExperimentResult,
) -> ExperimentComparison:
    baseline_by_name = {case.name: case for case in baseline.cases}
    candidate_by_name = {case.name: case for case in candidate.cases}
    if baseline_by_name.keys() != candidate_by_name.keys():
        raise ValueError("baseline and candidate must contain the same fixture cases")

    changes: list[dict[str, object]] = []
    for name in sorted(baseline_by_name):
        before = baseline_by_name[name]
        after = candidate_by_name[name]
        delta = after.scorecard.f1 - before.scorecard.f1
        expected = {
            (
                str(finding["agent_type"]),
                str(finding["category"]),
                str(finding["file_path"]),
                _int_field(finding["line_start"]),
                _int_field(finding["line_end"]),
            )
            for finding in before.expected_findings
        }
        before_by_key = {_finding_key(finding): finding for finding in before.findings}
        after_by_key = {_finding_key(finding): finding for finding in after.findings}
        finding_changes: list[dict[str, str]] = []
        for finding_key in sorted(before_by_key.keys() | after_by_key.keys()):
            was_present = finding_key in before_by_key
            is_present = finding_key in after_by_key
            if was_present and is_present:
                status = "unchanged"
                finding = after_by_key[finding_key]
            elif was_present:
                status = "regressed" if finding_key in expected else "improved"
                finding = before_by_key[finding_key]
            else:
                status = "improved" if finding_key in expected else "regressed"
                finding = after_by_key[finding_key]
            finding_changes.append({"summary": str(finding["summary"]), "status": status})
        changes.append(
            {
                "fixture": name,
                "status": "improved" if delta > 0 else "regressed" if delta < 0 else "unchanged",
                "finding_f1_before": before.scorecard.f1,
                "finding_f1_after": after.scorecard.f1,
                "outcome_before": before.outcome.value,
                "outcome_after": after.outcome.value,
                "finding_changes": finding_changes,
                "findings_before": before.findings,
                "findings_after": after.findings,
            }
        )
    deltas = {
        "mean_confidence": _mean(case.overall_confidence for case in candidate.cases)
        - _mean(case.overall_confidence for case in baseline.cases),
        "finding_f1": candidate.mean_finding_f1 - baseline.mean_finding_f1,
        "escalation_rate": candidate.escalation_rate - baseline.escalation_rate,
        "mean_latency_ms": candidate.mean_latency_ms - baseline.mean_latency_ms,
        "known_cost_usd": candidate.known_cost_usd - baseline.known_cost_usd,
        "unknown_cost_calls": float(candidate.unknown_cost_calls - baseline.unknown_cost_calls),
        "coverage": candidate.coverage - baseline.coverage,
    }
    return ExperimentComparison(
        baseline=baseline,
        candidate=candidate,
        fixture_changes=changes,
        aggregate_deltas=deltas,
    )


def _finding_key(finding: dict[str, object]) -> tuple[str, str, str, int, int]:
    return (
        str(finding["agent_type"]),
        str(finding["category"]),
        str(finding["file_path"]),
        _int_field(finding["line_start"]),
        _int_field(finding["line_end"]),
    )


def _int_field(value: object) -> int:
    if isinstance(value, int) and not isinstance(value, bool):
        return value
    raise ValueError(f"expected integer finding field, got {value!r}")


def _log_comparison(
    comparison: ExperimentComparison,
    *,
    tracking_uri: str | None,
    experiment_name: str,
) -> None:
    _configure_mlflow(mlflow, tracking_uri, experiment_name)
    with mlflow.start_run(run_name="configuration-comparison"):
        mlflow.log_params(
            {
                "baseline_manifest_hash": comparison.baseline.manifest.manifest_hash,
                "candidate_manifest_hash": comparison.candidate.manifest.manifest_hash,
                "fixture_set_version": comparison.baseline.manifest.fixture_set_version,
            }
        )
        mlflow.log_metrics(comparison.aggregate_deltas)
        mlflow.log_dict(
            {
                "fixture_changes": comparison.fixture_changes,
                "aggregate_deltas": comparison.aggregate_deltas,
                "scorecards": {
                    "baseline": _scorecard(comparison.baseline),
                    "candidate": _scorecard(comparison.candidate),
                },
            },
            "comparison.json",
        )


def _scorecard(result: ConfigurationExperimentResult) -> dict[str, float]:
    return {
        "finding_f1": result.mean_finding_f1,
        "escalation_rate": result.escalation_rate,
        "mean_latency_ms": result.mean_latency_ms,
        "known_cost_usd": result.known_cost_usd,
        "unknown_cost_calls": float(result.unknown_cost_calls),
        "coverage": result.coverage,
    }


def _configure_mlflow(mlflow: ModuleType, tracking_uri: str | None, experiment_name: str) -> None:
    if tracking_uri:
        mlflow.set_tracking_uri(tracking_uri)
    mlflow.set_experiment(experiment_name)


def _digest(value: object) -> str:
    encoded = json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def _mean(values: Iterable[float]) -> float:
    numbers = list(values)
    return sum(numbers) / len(numbers) if numbers else 0.0
