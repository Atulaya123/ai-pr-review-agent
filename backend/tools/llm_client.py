import json
from abc import ABC, abstractmethod
from dataclasses import dataclass

from backend.core.config import Settings, get_settings


@dataclass
class LLMResponse:
    text: str
    tokens_in: int
    tokens_out: int
    cost_usd: float
    model: str


class LLMClient(ABC):
    @abstractmethod
    async def complete_json(self, *, system: str, user: str, model: str) -> LLMResponse:
        """Call the model and return its raw text (expected to be a JSON array of findings)."""


class OpenAILLMClient(LLMClient):
    def __init__(self, api_key: str):
        from openai import AsyncOpenAI

        self._client = AsyncOpenAI(api_key=api_key)

    async def complete_json(self, *, system: str, user: str, model: str) -> LLMResponse:
        resp = await self._client.chat.completions.create(
            model=model,
            messages=[{"role": "system", "content": system}, {"role": "user", "content": user}],
            response_format={"type": "json_object"},
        )
        usage = resp.usage
        tokens_in = usage.prompt_tokens if usage else 0
        tokens_out = usage.completion_tokens if usage else 0
        return LLMResponse(
            text=resp.choices[0].message.content or "{}",
            tokens_in=tokens_in,
            tokens_out=tokens_out,
            cost_usd=_estimate_cost(model, tokens_in, tokens_out),
            model=model,
        )


class AnthropicLLMClient(LLMClient):
    def __init__(self, api_key: str):
        from anthropic import AsyncAnthropic

        self._client = AsyncAnthropic(api_key=api_key)

    async def complete_json(self, *, system: str, user: str, model: str) -> LLMResponse:
        resp = await self._client.messages.create(
            model=model,
            max_tokens=2048,
            system=system,
            messages=[{"role": "user", "content": user}],
        )
        text = "".join(block.text for block in resp.content if block.type == "text")
        tokens_in = resp.usage.input_tokens
        tokens_out = resp.usage.output_tokens
        return LLMResponse(
            text=text,
            tokens_in=tokens_in,
            tokens_out=tokens_out,
            cost_usd=_estimate_cost(model, tokens_in, tokens_out),
            model=model,
        )


class MockLLMClient(LLMClient):
    """Deterministic canned responses — no network, no API key.

    Lets M1's demo command (`pytest backend/tests/test_e2e_review.py`) run with
    zero external credentials. Looks for known trigger substrings in the diff
    (e.g. "execute(" for a SQL-injection fixture) and returns a matching finding;
    otherwise returns an empty finding list.
    """

    async def complete_json(self, *, system: str, user: str, model: str) -> LLMResponse:
        findings = []
        if "security" in system.lower() and ("execute(" in user or "f\"select" in user.lower()):
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
            cost_usd=0.0,
            model="mock",
        )


_PRICE_PER_1K = {
    "gpt-4o": (0.005, 0.015),
    "gpt-4o-mini": (0.00015, 0.0006),
    "claude-opus-4-5": (0.015, 0.075),
    "claude-haiku-4-5": (0.001, 0.005),
}


def _estimate_cost(model: str, tokens_in: int, tokens_out: int) -> float:
    price_in, price_out = _PRICE_PER_1K.get(model, (0.0, 0.0))
    return (tokens_in / 1000) * price_in + (tokens_out / 1000) * price_out


def get_llm_client(settings: Settings | None = None) -> LLMClient:
    settings = settings or get_settings()
    if settings.llm_provider == "openai" and settings.openai_api_key:
        return OpenAILLMClient(settings.openai_api_key)
    if settings.llm_provider == "anthropic" and settings.anthropic_api_key:
        return AnthropicLLMClient(settings.anthropic_api_key)
    return MockLLMClient()
