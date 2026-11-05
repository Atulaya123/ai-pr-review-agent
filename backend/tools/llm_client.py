import asyncio
import json
from abc import ABC, abstractmethod
from dataclasses import dataclass, replace
from uuid import UUID

from backend.core.config import Settings, get_settings
from backend.database.cost_ledger import CallPurpose, CostLedger


@dataclass
class LLMResponse:
    text: str
    tokens_in: int | None
    tokens_out: int | None
    cost_usd: float | None
    model: str
    cached_input_tokens: int | None = None
    raw_usage: dict[str, object] | None = None


class LLMClient(ABC):
    @abstractmethod
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
        """Call the model and return its raw text (expected to be a JSON array of findings)."""


def _integer_usage_value(value: object) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def _usage_fields(
    usage: object | None, *, input_name: str, output_name: str
) -> tuple[int | None, int | None, int | None, dict[str, object] | None]:
    if usage is None:
        return None, None, None, None

    input_tokens = _integer_usage_value(getattr(usage, input_name, None))
    output_tokens = _integer_usage_value(getattr(usage, output_name, None))
    cached_input_tokens = _integer_usage_value(getattr(usage, "cache_read_input_tokens", None))

    input_details = getattr(usage, "prompt_tokens_details", None)
    if input_details is not None:
        cached_input_tokens = _integer_usage_value(getattr(input_details, "cached_tokens", None))

    dump = getattr(usage, "model_dump", None)
    raw_value = dump(mode="json") if callable(dump) else None
    if isinstance(raw_value, dict):
        raw_usage: dict[str, object] = {
            key: value for key, value in raw_value.items() if isinstance(key, str)
        }
    else:
        raw_usage = {
            input_name: input_tokens,
            output_name: output_tokens,
            "cached_input_tokens": cached_input_tokens,
        }
    return input_tokens, output_tokens, cached_input_tokens, raw_usage


class OpenAILLMClient(LLMClient):
    def __init__(self, api_key: str):
        from openai import AsyncOpenAI

        self._client = AsyncOpenAI(api_key=api_key)

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
        resp = await self._client.chat.completions.create(
            model=model,
            messages=[{"role": "system", "content": system}, {"role": "user", "content": user}],
            response_format={"type": "json_object"},
            max_tokens=max_output_tokens,
        )
        tokens_in, tokens_out, cached_tokens, raw_usage = _usage_fields(
            resp.usage, input_name="prompt_tokens", output_name="completion_tokens"
        )
        return LLMResponse(
            text=resp.choices[0].message.content or "{}",
            tokens_in=tokens_in,
            tokens_out=tokens_out,
            cost_usd=None,
            model=model,
            cached_input_tokens=cached_tokens,
            raw_usage=raw_usage,
        )


class OllamaLLMClient(LLMClient):
    """Real local inference, zero cost, no API key — Ollama exposes an
    OpenAI-compatible endpoint, so this reuses the OpenAI SDK pointed at
    localhost instead of api.openai.com. Requires `ollama serve` running and
    the model already pulled (`ollama pull llama3.2:3b`)."""

    def __init__(self, base_url: str = "http://localhost:11434/v1"):
        from openai import AsyncOpenAI

        self._client = AsyncOpenAI(base_url=base_url, api_key="ollama")

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
        resp = await self._client.chat.completions.create(
            model=model,
            messages=[{"role": "system", "content": system}, {"role": "user", "content": user}],
            response_format={"type": "json_object"},
            max_tokens=max_output_tokens,
        )
        tokens_in, tokens_out, cached_tokens, raw_usage = _usage_fields(
            resp.usage, input_name="prompt_tokens", output_name="completion_tokens"
        )
        return LLMResponse(
            text=resp.choices[0].message.content or "{}",
            tokens_in=tokens_in,
            tokens_out=tokens_out,
            cost_usd=None,
            model=model,
            cached_input_tokens=cached_tokens,
            raw_usage=raw_usage,
        )


class GroqLLMClient(LLMClient):
    """Free hosted inference for the deployed instance — Groq's API is
    OpenAI-compatible, so this reuses the OpenAI SDK pointed at Groq instead
    of api.openai.com. Used where local Ollama can't run (free-tier hosting
    doesn't have the RAM for local model weights)."""

    def __init__(self, api_key: str):
        from openai import AsyncOpenAI

        self._client = AsyncOpenAI(base_url="https://api.groq.com/openai/v1", api_key=api_key)

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
        resp = await self._client.chat.completions.create(
            model=model,
            messages=[{"role": "system", "content": system}, {"role": "user", "content": user}],
            response_format={"type": "json_object"},
            max_tokens=max_output_tokens,
        )
        tokens_in, tokens_out, cached_tokens, raw_usage = _usage_fields(
            resp.usage, input_name="prompt_tokens", output_name="completion_tokens"
        )
        return LLMResponse(
            text=resp.choices[0].message.content or "{}",
            tokens_in=tokens_in,
            tokens_out=tokens_out,
            cost_usd=None,
            model=model,
            cached_input_tokens=cached_tokens,
            raw_usage=raw_usage,
        )


class AnthropicLLMClient(LLMClient):
    def __init__(self, api_key: str):
        from anthropic import AsyncAnthropic

        self._client = AsyncAnthropic(api_key=api_key)

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
        resp = await self._client.messages.create(
            model=model,
            max_tokens=max_output_tokens,
            system=system,
            messages=[{"role": "user", "content": user}],
        )
        text = "".join(block.text for block in resp.content if block.type == "text")
        tokens_in, tokens_out, cached_tokens, raw_usage = _usage_fields(
            resp.usage, input_name="input_tokens", output_name="output_tokens"
        )
        return LLMResponse(
            text=text,
            tokens_in=tokens_in,
            tokens_out=tokens_out,
            cost_usd=None,
            model=model,
            cached_input_tokens=cached_tokens,
            raw_usage=raw_usage,
        )


class MockLLMClient(LLMClient):
    """Deterministic canned responses — no network, no API key.

    Lets M1's demo command (`pytest backend/tests/test_e2e_review.py`) run with
    zero external credentials. Looks for known trigger substrings in the diff
    (e.g. "execute(" for a SQL-injection fixture) and returns a matching finding;
    otherwise returns an empty finding list.
    """

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
        system_lower = system.lower()

        if "faithfulness judge" in system_lower:
            # Deterministic stand-in for "does the claim trace back to context":
            # no retrieved context means nothing to be faithful to.
            supported = "(no context retrieved)" not in user
            return LLMResponse(
                text=json.dumps(
                    {"supported": supported, "reasoning": "mock: context-presence heuristic"}
                ),
                tokens_in=0,
                tokens_out=0,
                cost_usd=None,
                model="mock",
                raw_usage={"prompt_tokens": 0, "completion_tokens": 0},
            )

        if "relevance judge" in system_lower:
            relevant = "UNRELATED_TO_DIFF" not in user
            return LLMResponse(
                text=json.dumps({"relevant": relevant, "reasoning": "mock: marker heuristic"}),
                tokens_in=0,
                tokens_out=0,
                cost_usd=None,
                model="mock",
                raw_usage={"prompt_tokens": 0, "completion_tokens": 0},
            )

        findings = []
        if "security" in system_lower and ("execute(" in user or 'f"select' in user.lower()):
            findings.append(
                {
                    "severity": "CRITICAL",
                    "category": "injection",
                    "summary": "Possible SQL injection via unparameterized query",
                    "file_path": "app.py",
                    "line_start": 1,
                    "line_end": 1,
                    "suggestion": "Use parameterized queries instead of string formatting.",
                    "confidence": 0.92,
                    "rationale": "User-controlled input is concatenated directly into a SQL string.",
                }
            )
        return LLMResponse(
            text=json.dumps({"findings": findings}),
            tokens_in=0,
            tokens_out=0,
            cost_usd=None,
            model="mock",
            raw_usage={"prompt_tokens": 0, "completion_tokens": 0},
        )


class BudgetedLLMClient(LLMClient):
    def __init__(
        self,
        delegate: LLMClient,
        *,
        provider: str,
        settings: Settings,
        ledger: CostLedger | None = None,
    ) -> None:
        self._delegate = delegate
        self._provider = provider
        self._settings = settings
        self._ledger = ledger or CostLedger(settings)

    async def complete_json(
        self,
        *,
        system: str,
        user: str,
        model: str,
        max_output_tokens: int | None = None,
        purpose: CallPurpose = "agent",
        review_id: UUID | None = None,
        agent: str | None = None,
    ) -> LLMResponse:
        output_limit = (
            self._settings.llm_max_output_tokens if max_output_tokens is None else max_output_tokens
        )
        if output_limit <= 0:
            raise ValueError("max_output_tokens must be greater than zero")
        reservation = await self._ledger.reserve(
            provider=self._provider,
            model=model,
            system=system,
            user=user,
            purpose=purpose,
            max_output_tokens=output_limit,
            review_id=review_id,
            agent=agent,
        )
        try:
            response = await self._delegate.complete_json(
                system=system,
                user=user,
                model=model,
                max_output_tokens=output_limit,
                purpose=purpose,
                review_id=review_id,
                agent=agent,
            )
        except Exception:
            await self._ledger.reconcile(
                reservation,
                tokens_in=None,
                tokens_out=None,
                cached_input_tokens=None,
                raw_usage=None,
                failed=True,
            )
            raise
        except asyncio.CancelledError:
            await asyncio.shield(
                self._ledger.reconcile(
                    reservation,
                    tokens_in=None,
                    tokens_out=None,
                    cached_input_tokens=None,
                    raw_usage=None,
                    failed=True,
                )
            )
            raise

        cost = await self._ledger.reconcile(
            reservation,
            tokens_in=response.tokens_in,
            tokens_out=response.tokens_out,
            cached_input_tokens=response.cached_input_tokens,
            raw_usage=response.raw_usage,
        )
        return replace(response, cost_usd=float(cost) if cost is not None else None)


def get_llm_client(settings: Settings | None = None) -> LLMClient:
    settings = settings or get_settings()
    provider = settings.llm_provider
    client: LLMClient
    if settings.llm_provider == "openai" and settings.openai_api_key:
        client = OpenAILLMClient(settings.openai_api_key)
    elif settings.llm_provider == "anthropic" and settings.anthropic_api_key:
        client = AnthropicLLMClient(settings.anthropic_api_key)
    elif settings.llm_provider == "ollama":
        client = OllamaLLMClient()
    elif settings.llm_provider == "groq" and settings.groq_api_key:
        client = GroqLLMClient(settings.groq_api_key)
    else:
        provider = "mock"
        client = MockLLMClient()
    return BudgetedLLMClient(client, provider=provider, settings=settings)
