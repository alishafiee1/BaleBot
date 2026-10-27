from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.pool import StaticPool

from balebot.models import Base


def make_engine(url: str) -> AsyncEngine:
    in_memory = url == "sqlite+aiosqlite://" or ":memory:" in url
    if in_memory:
        # one shared connection, otherwise every connection sees its own empty database
        return create_async_engine(url, poolclass=StaticPool)
    return create_async_engine(url)


def make_sessionmaker(engine: AsyncEngine) -> async_sessionmaker[AsyncSession]:
    return async_sessionmaker(engine, expire_on_commit=False)


async def create_all(engine: AsyncEngine) -> None:
    """Dev/test bootstrap. Production schema changes need migrations (Alembic, phase 2)."""
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
