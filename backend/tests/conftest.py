import os

# Tests must never run against the same database as a live demo/dev session —
# the db_session fixture below drops all tables on teardown, and a shell with
# TIGER_DATABASE_URL exported (e.g. for local scripts/run_eval.py runs against
# the real Tiger Cloud instance) previously leaked straight through: setdefault()
# only guards the .env file, not an OS env var already in the process's
# environment, and pydantic-settings prefers OS env vars over .env either way.
# Force it unconditionally instead — nothing in this test suite should ever be
# able to read a real database URL. Must happen before
# backend.core.config.get_settings() is ever called (it's lru_cached on first
# call, so this must happen at import time here).
os.environ["TIGER_DATABASE_URL"] = "postgresql+asyncpg://postgres:postgres@localhost:5544/aipr_review_test"

# Same leak, same fix: a shell with LLM_PROVIDER=ollama (or any real provider)
# exported previously bypassed this and made test_e2e_review.py's assertions
# non-deterministic against a real model's output instead of MockLLMClient's
# canned responses. Force it unconditionally.
os.environ["LLM_PROVIDER"] = "mock"

import pytest_asyncio
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

from backend.core.config import get_settings
from backend.database.models import Base
from backend.database.session import get_engine, get_sessionmaker


async def _ensure_test_database_exists(test_db_url: str) -> None:
    db_name = test_db_url.rsplit("/", 1)[1].split("?")[0]
    admin_url = test_db_url.rsplit("/", 1)[0] + "/postgres"
    admin_engine = create_async_engine(admin_url, isolation_level="AUTOCOMMIT")
    try:
        async with admin_engine.connect() as conn:
            exists = await conn.scalar(text("SELECT 1 FROM pg_database WHERE datname = :name"), {"name": db_name})
            if not exists:
                await conn.execute(text(f'CREATE DATABASE "{db_name}"'))
    finally:
        await admin_engine.dispose()


@pytest_asyncio.fixture(scope="session", autouse=True)
async def _test_database_exists():
    await _ensure_test_database_exists(get_settings().tiger_database_url)


@pytest_asyncio.fixture
async def db_session():
    engine = get_engine()
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    async with get_sessionmaker()() as session:
        yield session

    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)
