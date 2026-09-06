from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from sqlalchemy import Engine, create_engine, text
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.orm import sessionmaker

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
            max_overflow=0,
            pool_timeout=settings.database_pool_timeout_seconds,
            connect_args={"options": f"-c statement_timeout={settings.statement_timeout_ms}"},
        )
        self.sessions = async_sessionmaker(self.engine, expire_on_commit=False, autoflush=False)
        # Compatibility application services still combine synchronous SQLAlchemy
        # work with blocking Runtime HTTP calls. Give those reads their own bounded
        # pool so they can run outside the ASGI event loop without consuming or
        # overflowing the ordinary async request pool.
        self.blocking_engine: Engine = create_engine(
            settings.database_url,
            pool_pre_ping=True,
            pool_size=settings.blocking_pool_size,
            max_overflow=0,
            pool_timeout=settings.blocking_pool_timeout_seconds,
            connect_args={"options": f"-c statement_timeout={settings.statement_timeout_ms}"},
        )
        self.blocking_sessions = sessionmaker(
            self.blocking_engine, expire_on_commit=False, autoflush=False
        )

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
        await asyncio.to_thread(self.blocking_engine.dispose)
