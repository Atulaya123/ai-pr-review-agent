"""Generation-quality metrics for specialist findings, via LLM-as-judge.

Distinct from backend/hitl/gate.py: the HITL gate only reads a finding's
*self-reported* confidence — a specialist can be confidently wrong. These
functions check a finding's claim against an external source (the retrieved
context, or the diff itself), the same way a human reviewer would ask "where
does it say that?" instead of just trusting the tone of the answer.

Goes through the same LLMClient seam as everything else (backend/tools/
llm_client.py) — MockLLMClient handles these prompts deterministically, so
this stays free and hermetic in the test suite, same philosophy as the rest
of M1.
"""

import json

from backend.models.findings import Finding
from backend.tools.llm_client import LLMClient

FAITHFULNESS_SYSTEM_PROMPT = (
    "You are a strict faithfulness judge for a PR-review finding. Given a "
    "finding's rationale and the codebase context it was supposed to be "
    "grounded in, decide whether the rationale's claim is actually supported "
    "by that context. If no context was retrieved, the claim cannot be "
    "grounded and must be marked unsupported. Respond with JSON only: "
    '{"supported": true|false, "reasoning": "..."}.'
)

RELEVANCE_SYSTEM_PROMPT = (
    "You are a strict relevance judge for a PR-review finding. Given a diff "
    "and a finding raised against it, decide whether the finding actually "
    "addresses something present in that diff, rather than an unrelated or "
    'hallucinated concern. Respond with JSON only: {"relevant": true|false, '
    '"reasoning": "..."}.'
)


async def score_faithfulness(
    finding: Finding, context: str, llm_client: LLMClient, *, model: str
) -> tuple[bool, str]:
    user = f"CONTEXT:\n{context or '(no context retrieved)'}\n\nFINDING RATIONALE:\n{finding.rationale}"
    response = await llm_client.complete_json(system=FAITHFULNESS_SYSTEM_PROMPT, user=user, model=model)
    verdict = json.loads(response.text)
    return bool(verdict.get("supported", False)), verdict.get("reasoning", "")


async def score_relevance(
    finding: Finding, diff_text: str, llm_client: LLMClient, *, model: str
) -> tuple[bool, str]:
    user = (
        f"DIFF:\n{diff_text}\n\nFINDING:\n{finding.summary} "
        f"({finding.file_path}:{finding.line_start})\nRATIONALE:\n{finding.rationale}"
    )
    response = await llm_client.complete_json(system=RELEVANCE_SYSTEM_PROMPT, user=user, model=model)
    verdict = json.loads(response.text)
    return bool(verdict.get("relevant", False)), verdict.get("reasoning", "")


def pass_rate(verdicts: list[bool]) -> float:
    """Shared aggregation for both faithfulness and relevance verdict lists."""
    if not verdicts:
        return 1.0
    return sum(verdicts) / len(verdicts)
