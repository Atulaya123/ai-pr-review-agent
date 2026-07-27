"""M1: create the truth-lane tables via SQLAlchemy metadata against whatever
Postgres-compatible database TIGER_DATABASE_URL points at (local Postgres or
Tiger Cloud both work — this doesn't touch vector/hypertable extensions).

Run with: python -m scripts.init_db
"""

import asyncio

from backend.database.models import Base
from backend.database.session import get_engine


async def main() -> None:
    engine = get_engine()
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    print("tables created")


if __name__ == "__main__":
    asyncio.run(main())
