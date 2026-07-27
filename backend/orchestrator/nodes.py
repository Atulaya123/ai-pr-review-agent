from backend.agents.docs_agent import build_docs_agent
from backend.agents.quality_agent import build_quality_agent
from backend.agents.security_agent import build_security_agent
from backend.agents.test_agent import build_tests_agent
from backend.hitl.gate import apply_hitl_gate
from backend.memory.context_retriever import get_retrieved_context
from backend.models.findings import Finding
from backend.orchestrator.state import ReviewState
from backend.tools.llm_client import get_llm_client


async def build_context(state: ReviewState) -> dict:
    context = await get_retrieved_context(state["request"])
    return {"retrieved_context": context}


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
    overall_confidence, outcome = apply_hitl_gate(merged)
    return {"findings": merged, "overall_confidence": overall_confidence, "outcome": outcome}
