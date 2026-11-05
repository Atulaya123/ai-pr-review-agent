from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone
from decimal import Decimal
from math import ceil
from typing import Literal
from uuid import UUID, uuid4

from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from backend.core.exceptions import BudgetExceededError, PricingUnavailableError
from backend.core.config import Settings, get_settings
from backend.database.models import ModelCall, PriceVersion
from backend.database.session import get_sessionmaker

CallPurpose = Literal["agent", "judge"]
_MILLION = Decimal(1_000_000)
_BUDGET_LOCK_KEY = 731_946_201


@dataclass(frozen=True)
class CostReservation:
    call_id: UUID
    price_version_id: UUID
    estimated_input_tokens: int
    estimated_output_tokens: int
    reserved_cost_usd: Decimal


@dataclass(frozen=True)
class DailyCostSummary:
    purpose: CallPurpose
    known_cost_usd: Decimal
    unknown_reserved_cost_usd: Decimal
    unknown_calls: int

    @property
    def enforced_cost_usd(self) -> Decimal:
        return self.known_cost_usd + self.unknown_reserved_cost_usd


def estimate_token_count(text_value: str) -> int:
    """Conservative local estimate; provider-reported usage replaces it later."""
    return max(1, ceil(len(text_value) / 3))


def _call_cost(
    price: PriceVersion,
    tokens_in: int,
    tokens_out: int,
    cached_input_tokens: int,
) -> Decimal:
    if cached_input_tokens < 0 or cached_input_tokens > tokens_in:
        raise ValueError("cached input token count is outside the input token range")
    uncached_tokens = tokens_in - cached_input_tokens
    return (
        Decimal(uncached_tokens) * price.input_rate_per_million
        + Decimal(cached_input_tokens) * price.cached_input_rate_per_million
        + Decimal(tokens_out) * price.output_rate_per_million
    ) / _MILLION


def _utc_day_bounds(now: datetime) -> tuple[datetime, datetime]:
    start = datetime.combine(now.date(), time.min, tzinfo=timezone.utc)
    return start, start + timedelta(days=1)


class CostLedger:
    def __init__(
        self,
        settings: Settings | None = None,
        sessionmaker: async_sessionmaker[AsyncSession] | None = None,
    ) -> None:
        self._settings = settings or get_settings()
        self._sessionmaker = sessionmaker or get_sessionmaker()

    async def reserve(
        self,
        *,
        provider: str,
        model: str,
        system: str,
        user: str,
        purpose: CallPurpose,
        max_output_tokens: int,
        review_id: UUID | None,
        agent: str | None,
    ) -> CostReservation:
        now = datetime.now(timezone.utc)
        estimated_input_tokens = estimate_token_count(system + user)
        estimated_output_tokens = max_output_tokens
        start, end = _utc_day_bounds(now)

        async with self._sessionmaker() as session:
            async with session.begin():
                await session.execute(
                    text("SELECT pg_advisory_xact_lock(:key)"), {"key": _BUDGET_LOCK_KEY}
                )
                price = await self._effective_price(session, provider, model, now.date())
                if price is None:
                    raise PricingUnavailableError(
                        f"no effective USD price configured for provider={provider!r}, model={model!r}"
                    )
                if price.currency != "USD":
                    raise PricingUnavailableError(
                        f"daily USD budget cannot account for price currency {price.currency!r}"
                    )

                reserved_cost = _call_cost(
                    price, estimated_input_tokens, estimated_output_tokens, cached_input_tokens=0
                )
                budget = self._budget_for(purpose)
                known_cost = await session.scalar(
                    select(func.coalesce(func.sum(ModelCall.cost_usd), 0)).where(
                        ModelCall.created_at >= start,
                        ModelCall.created_at < end,
                        ModelCall.purpose == purpose,
                        ModelCall.cost_usd.is_not(None),
                    )
                )
                unknown_reserved = await session.scalar(
                    select(func.coalesce(func.sum(ModelCall.reserved_cost_usd), 0)).where(
                        ModelCall.created_at >= start,
                        ModelCall.created_at < end,
                        ModelCall.purpose == purpose,
                        ModelCall.cost_usd.is_(None),
                    )
                )
                current_cost = Decimal(known_cost or 0) + Decimal(unknown_reserved or 0)
                if current_cost + reserved_cost > budget:
                    raise BudgetExceededError(
                        f"{purpose} daily budget exceeded: "
                        f"{current_cost} USD spent/reserved + {reserved_cost} USD estimated "
                        f"> {budget} USD"
                    )

                call_id = uuid4()
                session.add(
                    ModelCall(
                        id=call_id,
                        created_at=now,
                        provider=provider,
                        model=model,
                        price_version_id=price.id,
                        purpose=purpose,
                        review_id=review_id,
                        agent=agent,
                        tokens_in=estimated_input_tokens,
                        tokens_out=estimated_output_tokens,
                        cached_input_tokens=0,
                        input_tokens_estimated=True,
                        output_tokens_estimated=True,
                        reserved_cost_usd=reserved_cost,
                        status="reserved",
                    )
                )
            return CostReservation(
                call_id=call_id,
                price_version_id=price.id,
                estimated_input_tokens=estimated_input_tokens,
                estimated_output_tokens=estimated_output_tokens,
                reserved_cost_usd=reserved_cost,
            )

    async def reconcile(
        self,
        reservation: CostReservation,
        *,
        tokens_in: int | None,
        tokens_out: int | None,
        cached_input_tokens: int | None,
        raw_usage: dict[str, object] | None,
        failed: bool = False,
    ) -> Decimal | None:
        async with self._sessionmaker() as session:
            async with session.begin():
                call = await session.get(ModelCall, reservation.call_id)
                if call is None:
                    raise RuntimeError(f"reserved model call {reservation.call_id} disappeared")
                price = await session.get(PriceVersion, call.price_version_id)
                if price is None:
                    raise RuntimeError(f"price version {call.price_version_id} disappeared")

                call.tokens_in = (
                    tokens_in if tokens_in is not None else reservation.estimated_input_tokens
                )
                call.tokens_out = (
                    tokens_out if tokens_out is not None else reservation.estimated_output_tokens
                )
                call.input_tokens_estimated = tokens_in is None
                call.output_tokens_estimated = tokens_out is None
                call.cached_input_tokens = cached_input_tokens
                call.raw_usage = raw_usage
                if failed:
                    call.status = "failed"
                    return None

                if raw_usage is None or tokens_in is None or tokens_out is None:
                    call.status = "unknown"
                    call.cost_usd = None
                    return None

                effective_cached_tokens = cached_input_tokens or 0
                try:
                    actual_cost = _call_cost(price, tokens_in, tokens_out, effective_cached_tokens)
                except ValueError:
                    call.status = "unknown"
                    call.cost_usd = None
                    return None

                call.cost_usd = actual_cost
                call.status = "completed"
                return actual_cost

    async def daily_summary(
        self, purpose: CallPurpose, *, now: datetime | None = None
    ) -> DailyCostSummary:
        current = now or datetime.now(timezone.utc)
        start, end = _utc_day_bounds(current)
        async with self._sessionmaker() as session:
            known_cost = await session.scalar(
                select(func.coalesce(func.sum(ModelCall.cost_usd), 0)).where(
                    ModelCall.created_at >= start,
                    ModelCall.created_at < end,
                    ModelCall.purpose == purpose,
                    ModelCall.cost_usd.is_not(None),
                )
            )
            unknown_cost = await session.scalar(
                select(func.coalesce(func.sum(ModelCall.reserved_cost_usd), 0)).where(
                    ModelCall.created_at >= start,
                    ModelCall.created_at < end,
                    ModelCall.purpose == purpose,
                    ModelCall.cost_usd.is_(None),
                )
            )
            unknown_calls = await session.scalar(
                select(func.count(ModelCall.id)).where(
                    ModelCall.created_at >= start,
                    ModelCall.created_at < end,
                    ModelCall.purpose == purpose,
                    ModelCall.cost_usd.is_(None),
                )
            )
        return DailyCostSummary(
            purpose=purpose,
            known_cost_usd=Decimal(known_cost or 0),
            unknown_reserved_cost_usd=Decimal(unknown_cost or 0),
            unknown_calls=int(unknown_calls or 0),
        )

    async def _effective_price(
        self, session: AsyncSession, provider: str, model: str, effective_date: date
    ) -> PriceVersion | None:
        statement = (
            select(PriceVersion)
            .where(
                PriceVersion.provider == provider,
                PriceVersion.model == model,
                PriceVersion.effective_from <= effective_date,
            )
            .order_by(PriceVersion.effective_from.desc())
            .limit(1)
        )
        return (await session.execute(statement)).scalar_one_or_none()

    def _budget_for(self, purpose: CallPurpose) -> Decimal:
        amount = (
            self._settings.daily_judge_budget_usd
            if purpose == "judge"
            else self._settings.daily_budget_usd
        )
        return Decimal(str(amount))
