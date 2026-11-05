import logging
from datetime import datetime, timezone

from backend.agents.docs_agent import build_docs_agent
from backend.agents.quality_agent import build_quality_agent
from backend.agents.security_agent import build_security_agent
from backend.agents.test_agent import build_tests_agent
from backend.hitl.gate import apply_hitl_gate
from backend.integrations.github_client import GitHubClient
from backend.memory.context_retriever import get_retrieved_context
from backend.models.enums import AgentType
from backend.models.findings import Finding
from backend.observability.events import emit_agent_event
from backend.observability.logging import log_stage
from backend.observability.timing import elapsed_ms, monotonic_now_ns
from backend.orchestrator.state import ReviewState
from backend.security.injection_guard import sanitize_review_request
from backend.tools.llm_client import get_llm_client
from backend.verification.citations import is_safe_repository_path, verify_finding_citation

logger = logging.getLogger(__name__)


async def build_context(state: ReviewState) -> dict:
    log_stage("retrieval.started", request=state["request"], stage="retrieval")
    sanitized = sanitize_review_request(state["request"])
    report = sanitized.report
    await emit_agent_event(
        state["request"].review_id,
        AgentType.AGGREGATOR,
        "safety.assessment",
        outcome="applicable" if report.applicable else "not_applicable",
        payload={
            "injection_attempted": report.injection_attempted,
            "text_reached_prompt": report.text_reached_prompt,
            "neutralized": report.neutralized,
            "injection_attempt_count": report.injection_attempt_count,
            "prompt_leak_count": report.prompt_leak_count,
            "neutralized_count": report.neutralized_count,
            "detections": [
                {
                    "source": detection.source,
                    "pattern": detection.pattern,
                    "line_number": detection.line_number,
                    "action": detection.action,
                }
                for detection in report.detections
            ],
        },
    )
    context = await get_retrieved_context(sanitized.request)
    log_stage(
        "retrieval.completed",
        request=state["request"],
        stage="retrieval",
        outcome="context" if context else "empty",
    )
    return {"request": sanitized.request, "retrieved_context": context}


async def security_node(state: ReviewState) -> dict:
    agent = build_security_agent(get_llm_client())
    findings = await agent.review(state["request"], state["retrieved_context"])
    return {"security_findings": findings}


async def quality_node(state: ReviewState) -> dict:
    agent = build_quality_agent(get_llm_client())
    findings = await agent.review(state["request"], state["retrieved_context"])
    return {"quality_findings": findings}


async def tests_node(state: ReviewState) -> dict:
    agent = build_tests_agent(get_llm_client())
    findings = await agent.review(state["request"], state["retrieved_context"])
    return {"tests_findings": findings}


async def docs_node(state: ReviewState) -> dict:
    agent = build_docs_agent(get_llm_client())
    findings = await agent.review(state["request"], state["retrieved_context"])
    return {"docs_findings": findings}


def _dedup(findings: list[Finding]) -> list[Finding]:
    """Keep the highest-confidence finding when multiple specialists flag the
    same file+line (the "almost-right" defense from L8)."""
    best_by_location: dict[tuple[str, int, int], Finding] = {}
    for f in findings:
        key = (f.file_path, f.line_start, f.line_end)
        existing = best_by_location.get(key)
        if existing is None or f.confidence > existing.confidence:
            best_by_location[key] = f
    return list(best_by_location.values())


async def aggregate_node(state: ReviewState) -> dict:
    all_findings = [
        *state["security_findings"],
        *state["quality_findings"],
        *state["tests_findings"],
        *state["docs_findings"],
    ]
    merged = _dedup(all_findings)
    request = state["request"]
    diff_paths = {file.path for file in request.files}
    repository_files: dict[str, str] = {}
    repository_lookup_failures: dict[str, str] = {}
    if request.installation_id is not None:
        github = GitHubClient()
        for path in {finding.file_path for finding in merged} - diff_paths:
            if not is_safe_repository_path(path):
                continue
            try:
                contents = await github.fetch_file_contents(
                    request.repo, path, request.head_sha, request.installation_id
                )
            except Exception:
                logger.exception(
                    "citation source lookup failed for %s at %s",
                    request.repo,
                    path,
                )
                repository_lookup_failures[path] = "source unavailable"
            else:
                if contents is not None:
                    repository_files[path] = contents

    verified_findings: list[Finding] = []
    for finding in merged:
        verification = verify_finding_citation(
            finding, request.files, repository_files, repository_lookup_failures
        )
        log_stage(
            "finding.verified",
            request=request,
            stage="verification",
            finding_id=str(finding.id),
            outcome="verified" if verification.verified else "failed",
        )
        if verification.verified:
            verified_findings.append(finding)
            continue
        await emit_agent_event(
            request.review_id,
            AgentType.AGGREGATOR,
            "finding.verification_failed",
            confidence=finding.confidence,
            outcome="failed",
            payload={
                "finding_id": str(finding.id),
                "file_path": finding.file_path,
                "line_start": finding.line_start,
                "line_end": finding.line_end,
                "failures": list(verification.failures),
            },
        )
        verified_findings.append(finding.model_copy(update={"confidence": 0.0}))
    merged = verified_findings
    gate_started_ns = monotonic_now_ns()
    gate_started_at = datetime.now(timezone.utc)
    overall_confidence, outcome = apply_hitl_gate(merged)
    log_stage(
        "gate.decided",
        request=request,
        stage="gate",
        outcome=outcome.value,
        confidence=overall_confidence,
        finding_ids=[str(finding.id) for finding in merged],
    )
    await emit_agent_event(
        state["request"].review_id,
        AgentType.AGGREGATOR,
        "hitl.gate",
        latency_ms=elapsed_ms(gate_started_ns),
        outcome=outcome.value,
        payload={
            "started_at_utc": gate_started_at.isoformat(),
            "duration_clock": "monotonic",
        },
    )
    return {"findings": merged, "overall_confidence": overall_confidence, "outcome": outcome}
