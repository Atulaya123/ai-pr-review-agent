import json
import logging
import time
from datetime import datetime, timezone
from uuid import UUID

from backend.models.enums import AgentType, Severity
from backend.models.findings import Finding
from backend.models.review import ReviewRequest
from backend.observability.events import emit_agent_event
from backend.prompts.registry import get_system_prompt
from backend.reliability.circuit_breaker import CircuitBreaker
from backend.reliability.retry import retry_with_backoff
from backend.reliability.timeout import LLM_TIMEOUT_S, with_timeout
from backend.reliability.token_budget import (
    chunk_review_request,
    estimate_call_tokens,
    wait_for_groq_capacity,
)
from backend.security.injection_guard import fence_untrusted_content, sanitize_review_request
from backend.core.config import get_settings
from backend.tools.llm_client import LLMClient, LLMResponse

_LLM_MODEL_BY_PROVIDER = {
    "openai": "gpt-4o-mini",
    "anthropic": "claude-haiku-4-5",
    "ollama": "qwen2.5:14b-instruct",
    # Groq deprecated llama-3.3-70b-versatile 2026-08-16 (announced 2026-06-17);
    # this is Groq's own recommended replacement, not an OpenAI-routed call —
    # Groq just names some hosted models with an "openai/" prefix.
    "groq": "openai/gpt-oss-120b",
    "mock": "mock",
}


def resolve_model_for_provider(provider: str) -> str:
    """Shared with backend/evaluation/ so the eval harness judges with the same
    model a real review would actually use for the active LLM_PROVIDER."""
    return _LLM_MODEL_BY_PROVIDER.get(provider, "mock")


logger = logging.getLogger(__name__)

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
        self.model = model or resolve_model_for_provider(get_settings().llm_provider)

    def _build_user_prompt(self, request: ReviewRequest, retrieved_context: str) -> str:
        sanitized = sanitize_review_request(request)
        diff_text = "\n".join(
            f"--- {path} ---\n{file.patch}"
            for path, file in zip(sanitized.prompt_paths, sanitized.request.files)
        )
        parts = [fence_untrusted_content("diff", diff_text)]
        if sanitized.request.commit_messages:
            parts.append(
                fence_untrusted_content(
                    "commit_messages",
                    "\n\n".join(sanitized.request.commit_messages),
                )
            )
        if retrieved_context:
            parts.append(fence_untrusted_content("codebase_context", retrieved_context))
        return "\n\n".join(parts)

    async def review(self, request: ReviewRequest, retrieved_context: str = "") -> list[Finding]:
        await emit_agent_event(request.review_id, self.agent_type, "span.start")
        settings = get_settings()
        system = get_system_prompt(self.agent_type.value)
        chunks = [request]
        if settings.llm_provider == "groq":
            chunks = chunk_review_request(
                request,
                estimate=lambda chunk: estimate_call_tokens(
                    system,
                    self._build_user_prompt(chunk, retrieved_context),
                    settings.llm_max_output_tokens,
                ),
                token_limit=settings.groq_tokens_per_minute,
            )
            await emit_agent_event(
                request.review_id,
                self.agent_type,
                "review.chunking",
                outcome="chunked" if len(chunks) > 1 else "unchunked",
                payload={
                    "chunk_count": len(chunks),
                    "file_count": len(request.files),
                    "token_counts_estimated": True,
                    "estimated_call_tokens": [
                        estimate_call_tokens(
                            system,
                            self._build_user_prompt(chunk, retrieved_context),
                            settings.llm_max_output_tokens,
                        )
                        for chunk in chunks
                    ],
                },
            )
        best: dict[tuple[str, int, int], Finding] = {}
        for chunk in chunks:
            for finding in await self._review_chunk(chunk, retrieved_context):
                key = (finding.file_path, finding.line_start, finding.line_end)
                previous = best.get(key)
                if previous is None or finding.confidence > previous.confidence:
                    best[key] = finding
        await emit_agent_event(request.review_id, self.agent_type, "span.end")
        return list(best.values())

    async def _review_chunk(self, request: ReviewRequest, retrieved_context: str) -> list[Finding]:
        system = get_system_prompt(self.agent_type.value)
        user = self._build_user_prompt(request, retrieved_context)
        settings = get_settings()

        async def _call() -> LLMResponse:
            if settings.llm_provider == "groq":
                wait_started_ns = time.monotonic_ns()
                await wait_for_groq_capacity(
                    tokens=estimate_call_tokens(system, user, settings.llm_max_output_tokens),
                    token_limit=settings.groq_tokens_per_minute,
                    model=self.model,
                    redis_url=settings.redis_url,
                )
                await emit_agent_event(
                    request.review_id,
                    self.agent_type,
                    "llm.rate_limit_wait",
                    latency_ms=int((time.monotonic_ns() - wait_started_ns) / 1_000_000),
                    payload={"duration_clock": "monotonic", "token_counts_estimated": True},
                )
            call_start_ns = time.monotonic_ns()
            call_started_at = datetime.now(timezone.utc)
            try:
                response = await with_timeout(
                    self.llm_client.complete_json(
                        system=system,
                        user=user,
                        model=self.model,
                        max_output_tokens=settings.llm_max_output_tokens,
                        review_id=request.review_id,
                        agent=self.agent_type.value,
                    ),
                    timeout_s=LLM_TIMEOUT_S,
                )
            except Exception:
                await emit_agent_event(
                    request.review_id,
                    self.agent_type,
                    "llm.call",
                    model=self.model,
                    latency_ms=int((time.monotonic_ns() - call_start_ns) / 1_000_000),
                    outcome="error",
                    payload={
                        "started_at_utc": call_started_at.isoformat(),
                        "duration_clock": "monotonic",
                    },
                )
                raise
            await emit_agent_event(
                request.review_id,
                self.agent_type,
                "llm.call",
                model=response.model,
                tokens_in=response.tokens_in,
                tokens_out=response.tokens_out,
                cost_usd=response.cost_usd,
                latency_ms=int((time.monotonic_ns() - call_start_ns) / 1_000_000),
                outcome="success",
                payload={
                    "started_at_utc": call_started_at.isoformat(),
                    "duration_clock": "monotonic",
                },
            )
            return response

        response = await _breaker.call(lambda: retry_with_backoff(_call, max_attempts=3))

        return self._parse_findings(response.text, request)

    def _parse_findings(self, raw_text: str, request: ReviewRequest) -> list[Finding]:
        try:
            data = json.loads(raw_text)
        except json.JSONDecodeError:
            logger.warning(
                "%s agent returned non-JSON response for review %s, treating as zero findings: %r",
                self.agent_type.value,
                request.review_id,
                raw_text[:500],
            )
            return []

        safe_file_paths = sanitize_review_request(request).prompt_paths
        path_map = {safe_path: file.path for safe_path, file in zip(safe_file_paths, request.files)}
        findings = []
        for item in data.get("findings", []):
            try:
                file_path = item["file_path"]
                findings.append(
                    Finding(
                        agent_type=self.agent_type,
                        severity=Severity(item["severity"]),
                        category=item["category"],
                        summary=item["summary"],
                        file_path=path_map.get(file_path, file_path),
                        line_start=item["line_start"],
                        line_end=item["line_end"],
                        evidence=item.get("evidence"),
                        suggestion=item.get("suggestion"),
                        confidence=item["confidence"],
                        rationale=item["rationale"],
                    )
                )
            except (KeyError, ValueError) as exc:
                logger.warning(
                    "%s agent produced a malformed finding for review %s, dropping it: %s — %r",
                    self.agent_type.value,
                    request.review_id,
                    exc,
                    item,
                )
                continue  # a malformed finding from the LLM should not sink the whole review
        return findings
