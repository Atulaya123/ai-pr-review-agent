from datetime import date
from decimal import Decimal
from types import SimpleNamespace
from uuid import UUID

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.core.config import Settings
from backend.core.exceptions import BudgetExceededError, PricingUnavailableError
from backend.database.cost_ledger import CallPurpose, CostLedger
from backend.database.models import ModelCall, PriceVersion
from backend.database.session import get_sessionmaker
from backend.reliability.retry import retry_with_backoff
from backend.tools.llm_client import BudgetedLLMClient, LLMClient, LLMResponse, _usage_fields

_RATE = Decimal("5")


class _FakeLLMClient(LLMClient):
    def __init__(self, response: LLMResponse) -> None:
        self.response = response
        self.calls = 0
        self.output_limits: list[int] = []

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
        self.calls += 1
        self.output_limits.append(max_output_tokens)
        return self.response


class _FailOnceLLMClient(_FakeLLMClient):
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
        self.calls += 1
        self.output_limits.append(max_output_tokens)
        if self.calls == 1:
            raise RuntimeError("simulated transient provider failure")
        return self.response


class _ProviderUsage:
    def __init__(
        self,
        *,
        input_tokens: int,
        output_tokens: int,
        cached_input_tokens: int,
        raw: dict[str, object],
    ) -> None:
        self.prompt_tokens = input_tokens
        self.completion_tokens = output_tokens
        self.input_tokens = input_tokens
        self.output_tokens = output_tokens
        self.prompt_tokens_details = SimpleNamespace(cached_tokens=cached_input_tokens)
        self.cache_read_input_tokens = cached_input_tokens
        self._raw = raw

    def model_dump(self, *, mode: str) -> dict[str, object]:
        return self._raw


def _known_usage_response() -> LLMResponse:
    return LLMResponse(
        text='{"findings": []}',
        tokens_in=1000,
        tokens_out=2000,
        cost_usd=None,
        model="budget-model",
        cached_input_tokens=500,
        raw_usage={
            "prompt_tokens": 1000,
            "completion_tokens": 2000,
            "prompt_tokens_details": {"cached_tokens": 500},
        },
    )


async def _seed_price(session: AsyncSession, provider: str, model: str) -> None:
    session.add(
        PriceVersion(
            provider=provider,
            model=model,
            input_rate_per_million=_RATE,
            cached_input_rate_per_million=Decimal("1"),
            output_rate_per_million=_RATE,
            currency="USD",
            effective_from=date.today(),
            source="Test fixture rate.",
        )
    )
    await session.commit()


async def test_daily_budget_refuses_dispatch_when_reserved_and_spent_would_exceed(
    db_session: AsyncSession,
) -> None:
    await _seed_price(db_session, "budget-test", "budget-model")
    settings = Settings(daily_budget_usd=0.02)
    delegate = _FakeLLMClient(_known_usage_response())
    client = BudgetedLLMClient(
        delegate,
        provider="budget-test",
        settings=settings,
        ledger=CostLedger(settings, get_sessionmaker()),
    )

    result = await client.complete_json(system="s", user="u", model="budget-model")
    assert result.cost_usd == pytest.approx(0.013)
    with pytest.raises(BudgetExceededError):
        await client.complete_json(system="s", user="u", model="budget-model")

    calls = (
        (
            await db_session.execute(
                select(ModelCall).where(
                    ModelCall.provider == "budget-test",
                    ModelCall.model == "budget-model",
                )
            )
        )
        .scalars()
        .all()
    )
    assert len(calls) == 1
    assert delegate.calls == 1
    assert calls[0].cost_usd == Decimal("0.013000000000")
    assert calls[0].price_version_id is not None
    assert delegate.output_limits == [settings.llm_max_output_tokens]


async def test_missing_usage_is_recorded_as_unknown_not_zero(db_session: AsyncSession) -> None:
    await _seed_price(db_session, "unknown-test", "budget-model")
    settings = Settings(daily_budget_usd=1.0)
    delegate = _FakeLLMClient(
        LLMResponse(
            text='{"findings": []}',
            tokens_in=None,
            tokens_out=None,
            cost_usd=None,
            model="budget-model",
        )
    )
    client = BudgetedLLMClient(
        delegate,
        provider="unknown-test",
        settings=settings,
        ledger=CostLedger(settings, get_sessionmaker()),
    )

    response = await client.complete_json(system="s", user="u", model="budget-model")
    call = (
        await db_session.execute(
            select(ModelCall).where(
                ModelCall.provider == "unknown-test",
                ModelCall.model == "budget-model",
            )
        )
    ).scalar_one()
    summary = await CostLedger(settings, get_sessionmaker()).daily_summary("agent")

    assert response.cost_usd is None
    assert call.cost_usd is None
    assert call.status == "unknown"
    assert call.raw_usage is None
    assert call.input_tokens_estimated is True
    assert call.output_tokens_estimated is True
    assert call.reserved_cost_usd > 0
    assert summary.known_cost_usd == 0
    assert summary.unknown_calls == 1
    assert summary.unknown_reserved_cost_usd == call.reserved_cost_usd


async def test_judge_budget_is_separate_from_agent_budget(db_session: AsyncSession) -> None:
    await _seed_price(db_session, "separate-test", "budget-model")
    settings = Settings(daily_budget_usd=0, daily_judge_budget_usd=0.02)
    delegate = _FakeLLMClient(_known_usage_response())
    client = BudgetedLLMClient(
        delegate,
        provider="separate-test",
        settings=settings,
        ledger=CostLedger(settings, get_sessionmaker()),
    )

    result = await client.complete_json(system="s", user="u", model="budget-model", purpose="judge")
    assert result.cost_usd == pytest.approx(0.013)
    with pytest.raises(BudgetExceededError):
        await client.complete_json(system="s", user="u", model="budget-model")
    assert delegate.calls == 1


async def test_each_retry_attempt_has_its_own_reservation(db_session: AsyncSession) -> None:
    await _seed_price(db_session, "retry-test", "budget-model")
    settings = Settings(daily_budget_usd=0.1)
    delegate = _FailOnceLLMClient(_known_usage_response())
    client = BudgetedLLMClient(
        delegate,
        provider="retry-test",
        settings=settings,
        ledger=CostLedger(settings, get_sessionmaker()),
    )

    response = await retry_with_backoff(
        lambda: client.complete_json(system="s", user="u", model="budget-model"),
        max_attempts=2,
        base_delay_s=0,
    )

    calls = (
        (
            await db_session.execute(
                select(ModelCall).where(
                    ModelCall.provider == "retry-test",
                    ModelCall.model == "budget-model",
                )
            )
        )
        .scalars()
        .all()
    )
    summary = await CostLedger(settings, get_sessionmaker()).daily_summary("agent")

    assert response.cost_usd == pytest.approx(0.013)
    assert delegate.calls == 2
    assert {call.status for call in calls} == {"failed", "completed"}
    assert len(calls) == 2
    assert summary.unknown_calls == 1
    assert summary.known_cost_usd == Decimal("0.013000000000")


def test_provider_usage_normalization_preserves_raw_payload_and_cached_tokens() -> None:
    raw: dict[str, object] = {
        "prompt_tokens": 10,
        "completion_tokens": 5,
        "prompt_tokens_details": {"cached_tokens": 3},
    }

    tokens_in, tokens_out, cached_tokens, raw_usage = _usage_fields(
        _ProviderUsage(input_tokens=10, output_tokens=5, cached_input_tokens=3, raw=raw),
        input_name="prompt_tokens",
        output_name="completion_tokens",
    )

    assert (tokens_in, tokens_out, cached_tokens) == (10, 5, 3)
    assert raw_usage == raw


def test_absent_provider_usage_remains_unknown() -> None:
    assert _usage_fields(None, input_name="prompt_tokens", output_name="completion_tokens") == (
        None,
        None,
        None,
        None,
    )


async def test_unpriced_model_is_refused_before_dispatch(db_session: AsyncSession) -> None:
    delegate = _FakeLLMClient(_known_usage_response())
    settings = Settings(daily_budget_usd=1.0)
    client = BudgetedLLMClient(
        delegate,
        provider="unpriced-test",
        settings=settings,
        ledger=CostLedger(settings, get_sessionmaker()),
    )

    with pytest.raises(PricingUnavailableError):
        await client.complete_json(system="s", user="u", model="unpriced-model")
    assert delegate.calls == 0
