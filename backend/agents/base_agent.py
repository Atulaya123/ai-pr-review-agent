import json
import time
from uuid import UUID

from backend.models.enums import AgentType, Severity
from backend.models.findings import Finding
from backend.models.review import ReviewRequest
from backend.observability.events import emit_agent_event
from backend.prompts.registry import get_system_prompt
from backend.reliability.circuit_breaker import CircuitBreaker
from backend.reliability.retry import retry_with_backoff
from backend.reliability.timeout import with_timeout
from backend.security.injection_guard import detect_injection_attempt, fence_untrusted_content
from backend.core.config import get_settings
from backend.tools.llm_client import LLMClient

_LLM_MODEL_BY_PROVIDER = {
    "openai": "gpt-4o-mini",
    "anthropic": "claude-haiku-4-5",
    "ollama": "llama3.2:3b",
    "mock": "mock",
}

# one breaker per process is enough for M1; M2 can key this per-provider if needed
_breaker = CircuitBreaker()


class SpecialistAgent:
    """Shared shape for all four specialists (security/quality/tests/docs).

    Each specialist differs only in agent_type (which selects its system prompt)
    — the retrieval call, LLM call, parsing, and event emission are identical.
    """

    def __init__(self, agent_type: AgentType, llm_client: LLMClient, model: str | None = None):
        self.agent_type = agent_type
        self.llm_client = llm_client
        self.model = model or _LLM_MODEL_BY_PROVIDER.get(get_settings().llm_provider, "mock")

    def _build_user_prompt(self, request: ReviewRequest, retrieved_context: str) -> str:
        diff_text = "\n".join(f"--- {f.path} ---\n{f.patch}" for f in request.files)
        parts = [fence_untrusted_content("diff", diff_text)]
        if retrieved_context:
            parts.append(fence_untrusted_content("codebase_context", retrieved_context))
        return "\n\n".join(parts)

    async def review(self, request: ReviewRequest, retrieved_context: str = "") -> list[Finding]:
        span_start = time.monotonic()
        await emit_agent_event(request.review_id, self.agent_type, "span.start")

        system = get_system_prompt(self.agent_type.value)
        user = self._build_user_prompt(request, retrieved_context)

        injection_hits = detect_injection_attempt(user)
        if injection_hits and self.agent_type == AgentType.SECURITY:
            # the security specialist itself flags injection attempts in the diff
            pass  # surfaced as part of its own findings via the LLM's judgment

        async def _call():
            return await with_timeout(self.llm_client.complete_json(system=system, user=user, model=self.model))

        response = await _breaker.call(lambda: retry_with_backoff(_call, max_attempts=3))

        await emit_agent_event(
            request.review_id,
            self.agent_type,
            "llm.call",
            model=response.model,
            tokens_in=response.tokens_in,
            tokens_out=response.tokens_out,
            cost_usd=response.cost_usd,
            latency_ms=int((time.monotonic() - span_start) * 1000),
        )

        findings = self._parse_findings(response.text, request)
        await emit_agent_event(request.review_id, self.agent_type, "span.end")
        return findings

    def _parse_findings(self, raw_text: str, request: ReviewRequest) -> list[Finding]:
        try:
            data = json.loads(raw_text)
        except json.JSONDecodeError:
            return []

        findings = []
        for item in data.get("findings", []):
            try:
                findings.append(
                    Finding(
                        agent_type=self.agent_type,
                        severity=Severity(item["severity"]),
                        category=item["category"],
                        summary=item["summary"],
                        file_path=item["file_path"],
                        line_start=item["line_start"],
                        line_end=item["line_end"],
                        suggestion=item.get("suggestion"),
                        confidence=item["confidence"],
                        rationale=item["rationale"],
                    )
                )
            except (KeyError, ValueError):
                continue  # a malformed finding from the LLM should not sink the whole review
        return findings
