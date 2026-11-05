from datetime import date
from decimal import Decimal

from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from backend.database.models import PriceVersion

_INITIAL_EFFECTIVE_DATE = date(2026, 11, 4)
_INITIAL_PRICES = (
    {
        "provider": "groq",
        "model": "openai/gpt-oss-120b",
        "input_rate_per_million": Decimal("0.15"),
        "cached_input_rate_per_million": Decimal("0.075"),
        "output_rate_per_million": Decimal("0.60"),
        "source": "https://console.groq.com/docs/model/openai/gpt-oss-120b",
    },
    {
        "provider": "mock",
        "model": "mock",
        "input_rate_per_million": Decimal("0"),
        "cached_input_rate_per_million": Decimal("0"),
        "output_rate_per_million": Decimal("0"),
        "source": "Deterministic local MockLLMClient; no provider-billed token usage.",
    },
    {
        "provider": "ollama",
        "model": "qwen2.5:14b-instruct",
        "input_rate_per_million": Decimal("0"),
        "cached_input_rate_per_million": Decimal("0"),
        "output_rate_per_million": Decimal("0"),
        "source": "Local Ollama inference; no provider-billed token usage.",
    },
    {
        "provider": "gemini",
        "model": "gemini-embedding-001",
        "input_rate_per_million": Decimal("0"),
        "cached_input_rate_per_million": Decimal("0"),
        "output_rate_per_million": Decimal("0"),
        "source": "Gemini API free tier; embedding use is currently within the free-tier allowance.",
    },
)


async def seed_initial_prices(session: AsyncSession) -> None:
    rows = [
        {**price, "currency": "USD", "effective_from": _INITIAL_EFFECTIVE_DATE}
        for price in _INITIAL_PRICES
    ]
    statement = (
        pg_insert(PriceVersion)
        .values(rows)
        .on_conflict_do_nothing(index_elements=["provider", "model", "effective_from"])
    )
    await session.execute(statement)
    await session.commit()
