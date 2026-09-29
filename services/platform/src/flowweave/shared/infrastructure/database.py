from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import cast

from sqlalchemy import Engine, create_engine, text
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import QueuePool

from flowweave.bootstrap.settings import Settings
from flowweave.shared.application.uow import SqlAlchemyUnitOfWork


class Database:
    """Async PostgreSQL resources owned by a process container."""

    def __init__(
        self,
        settings: Settings,
        *,
        poll_pool_size: int = 0,
        auxiliary_pool_size: int = 0,
        admin_pool_size: int = 0,
        hydration_pool_size: int = 0,
        message_pool_size: int = 0,
    ) -> None:
        if not settings.database_url.startswith("postgresql+psycopg://"):
            raise ValueError("FlowWeave supports PostgreSQL through psycopg only")
        self.engine: AsyncEngine = create_async_engine(
            settings.database_url,
            pool_pre_ping=True,
            pool_size=settings.pool_size,
            max_overflow=settings.pool_max_overflow,
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
            pool_size=settings.blocking_pool_size - hydration_pool_size - message_pool_size,
            max_overflow=settings.pool_max_overflow,
            pool_timeout=settings.blocking_pool_timeout_seconds,
            connect_args={"options": f"-c statement_timeout={settings.statement_timeout_ms}"},
        )
        self.blocking_sessions = sessionmaker(
            self.blocking_engine, expire_on_commit=False, autoflush=False
        )
        self.hydration_engine: Engine | None = None
        self.hydration_sessions: sessionmaker[Session] | None = None
        if hydration_pool_size:
            self.hydration_engine = create_engine(
                settings.database_url,
                pool_pre_ping=True,
                pool_size=hydration_pool_size,
                max_overflow=0,
                pool_timeout=settings.blocking_pool_timeout_seconds,
                connect_args={"options": f"-c statement_timeout={settings.statement_timeout_ms}"},
            )
            self.hydration_sessions = sessionmaker(
                self.hydration_engine, expire_on_commit=False, autoflush=False
            )
        # Sending a message is the latency-sensitive user action. The API can
        # reserve one of its existing blocking SQL connections for this path so
        # a lifecycle mutation cannot consume all dispatch capacity.
        self.message_engine: Engine | None = None
        self.message_sessions: sessionmaker[Session] | None = None
        if message_pool_size:
            self.message_engine = create_engine(
                settings.database_url,
                pool_pre_ping=True,
                pool_size=message_pool_size,
                max_overflow=0,
                pool_timeout=settings.blocking_pool_timeout_seconds,
                connect_args={"options": f"-c statement_timeout={settings.statement_timeout_ms}"},
            )
            self.message_sessions = sessionmaker(
                self.message_engine, expire_on_commit=False, autoflush=False
            )
        # Optional background tasks can wait on model providers, package
        # registries or controller builds. Only Worker processes allocate this
        # small separate SQL pool, preserving ordinary delivery connections for
        # Runtime progression and recovery.
        self.auxiliary_engine: Engine | None = None
        self.auxiliary_sessions: sessionmaker[Session] | None = None
        if auxiliary_pool_size:
            self.auxiliary_engine = create_engine(
                settings.database_url,
                pool_pre_ping=True,
                pool_size=auxiliary_pool_size,
                max_overflow=0,
                pool_timeout=settings.blocking_pool_timeout_seconds,
                connect_args={"options": f"-c statement_timeout={settings.statement_timeout_ms}"},
            )
            self.auxiliary_sessions = sessionmaker(
                self.auxiliary_engine, expire_on_commit=False, autoflush=False
            )
        # Admin diagnostics are operator-triggered, potentially slow Runtime
        # reads. Give API processes a separate, tiny pool so diagnostics cannot
        # exhaust request, hydration, history or Worker auxiliary capacity.
        self.admin_engine: Engine | None = None
        self.admin_sessions: sessionmaker[Session] | None = None
        if admin_pool_size:
            self.admin_engine = create_engine(
                settings.database_url,
                pool_pre_ping=True,
                pool_size=admin_pool_size,
                max_overflow=0,
                pool_timeout=settings.blocking_pool_timeout_seconds,
                connect_args={"options": f"-c statement_timeout={settings.statement_timeout_ms}"},
            )
            self.admin_sessions = sessionmaker(
                self.admin_engine, expire_on_commit=False, autoflush=False
            )
        # Polling formal OpenHands state can remain blocked while a Runtime is
        # unhealthy. Only the Worker owns this dedicated pool; API processes
        # must not allocate an otherwise unused poll connection budget.
        self.poll_engine: Engine | None = None
        self.poll_sessions: sessionmaker[Session] | None = None
        if poll_pool_size:
            self.poll_engine = create_engine(
                settings.database_url,
                pool_pre_ping=True,
                pool_size=poll_pool_size,
                max_overflow=0,
                pool_timeout=settings.blocking_pool_timeout_seconds,
                connect_args={"options": f"-c statement_timeout={settings.statement_timeout_ms}"},
            )
            self.poll_sessions = sessionmaker(
                self.poll_engine, expire_on_commit=False, autoflush=False
            )
        # Background history reads can take several seconds against a large
        # OpenHands conversation. They must never reserve the same database
        # connection budget as the interactive state/readiness path.
        self.history_engine: Engine = create_engine(
            settings.database_url,
            pool_pre_ping=True,
            pool_size=settings.history_read_pool_size,
            max_overflow=0,
            pool_timeout=settings.blocking_pool_timeout_seconds,
            connect_args={"options": f"-c statement_timeout={settings.statement_timeout_ms}"},
        )
        self.history_sessions = sessionmaker(
            self.history_engine, expire_on_commit=False, autoflush=False
        )
        # Runtime control commands must remain available when every ordinary
        # read worker is blocked on an unhealthy Agent Server.  A separate
        # single-connection lane prevents read saturation from denying the
        # interrupt/recovery operation that can release those resources.
        self.control_engine: Engine = create_engine(
            settings.database_url,
            pool_pre_ping=True,
            pool_size=1,
            max_overflow=settings.pool_max_overflow,
            pool_timeout=settings.blocking_pool_timeout_seconds,
            connect_args={"options": f"-c statement_timeout={settings.statement_timeout_ms}"},
        )
        self.control_sessions = sessionmaker(
            self.control_engine, expire_on_commit=False, autoflush=False
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
        if self.hydration_engine is not None:
            await asyncio.to_thread(self.hydration_engine.dispose)
        if self.message_engine is not None:
            await asyncio.to_thread(self.message_engine.dispose)
        if self.auxiliary_engine is not None:
            await asyncio.to_thread(self.auxiliary_engine.dispose)
        if self.admin_engine is not None:
            await asyncio.to_thread(self.admin_engine.dispose)
        if self.poll_engine is not None:
            await asyncio.to_thread(self.poll_engine.dispose)
        await asyncio.to_thread(self.history_engine.dispose)
        await asyncio.to_thread(self.control_engine.dispose)

    def pool_metrics(self) -> dict[str, dict[str, int]]:
        pools = {
            "async": cast(QueuePool, self.engine.sync_engine.pool),
            "blocking": cast(QueuePool, self.blocking_engine.pool),
            **(
                {"hydration": cast(QueuePool, self.hydration_engine.pool)}
                if self.hydration_engine is not None
                else {}
            ),
            **(
                {"message": cast(QueuePool, self.message_engine.pool)}
                if self.message_engine is not None
                else {}
            ),
            **(
                {"auxiliary": cast(QueuePool, self.auxiliary_engine.pool)}
                if self.auxiliary_engine is not None
                else {}
            ),
            **(
                {"admin": cast(QueuePool, self.admin_engine.pool)}
                if self.admin_engine is not None
                else {}
            ),
            **(
                {"poll": cast(QueuePool, self.poll_engine.pool)}
                if self.poll_engine is not None
                else {}
            ),
            "history": cast(QueuePool, self.history_engine.pool),
            "control": cast(QueuePool, self.control_engine.pool),
        }
        return {
            name: {
                "size": pool.size(),
                "checked_out": pool.checkedout(),
                "overflow": pool.overflow(),
            }
            for name, pool in pools.items()
        }
