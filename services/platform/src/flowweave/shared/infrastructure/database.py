from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from sqlalchemy import text
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from flowweave.bootstrap.settings import Settings
from flowweave.shared.application.uow import SqlAlchemyUnitOfWork


class Database:
    """Async PostgreSQL resources owned by a process container."""

    def __init__(self, settings: Settings) -> None:
        if not settings.database_url.startswith("postgresql+psycopg://"):
            raise ValueError("FlowWeave supports PostgreSQL through psycopg only")
        self.engine: AsyncEngine = create_async_engine(
            settings.database_url,
            pool_pre_ping=True,
            pool_size=settings.pool_size,
            connect_args={"options": f"-c statement_timeout={settings.statement_timeout_ms}"},
        )
        self.sessions = async_sessionmaker(self.engine, expire_on_commit=False, autoflush=False)

    def uow(self) -> SqlAlchemyUnitOfWork:
        return SqlAlchemyUnitOfWork(self.sessions)

    @asynccontextmanager
    async def session(self) -> AsyncIterator[AsyncSession]:
        async with self.sessions() as session:
            yield session

    async def ping(self) -> None:
        async with self.session() as session:
            await session.execute(text("SELECT 1"))

    async def dispose(self) -> None:
        await self.engine.dispose()
