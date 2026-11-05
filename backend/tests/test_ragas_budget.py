from langchain_core.prompt_values import StringPromptValue
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.core.config import Settings
from backend.database.cost_ledger import CostLedger
from backend.database.models import ModelCall
from backend.database.pricing import seed_initial_prices
from backend.database.session import get_sessionmaker
from backend.evaluation.ragas_metrics import LedgerRagasLLM
from backend.tools.llm_client import BudgetedLLMClient, MockLLMClient


async def test_ragas_generations_are_separate_judge_ledger_calls(db_session: AsyncSession) -> None:
    await seed_initial_prices(db_session)
    settings = Settings(llm_provider="mock")
    client = BudgetedLLMClient(
        MockLLMClient(),
        provider="mock",
        settings=settings,
        ledger=CostLedger(settings, get_sessionmaker()),
    )
    await LedgerRagasLLM(client, "mock").agenerate_text(StringPromptValue(text="Judge"), n=2)
    calls = (await db_session.execute(select(ModelCall))).scalars().all()
    assert len(calls) == 2
    assert all(call.purpose == "judge" for call in calls)
    assert all(call.status == "completed" for call in calls)
