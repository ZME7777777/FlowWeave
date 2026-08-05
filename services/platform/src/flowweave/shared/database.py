from __future__ import annotations

from datetime import UTC, datetime
from uuid import uuid4

from sqlalchemy import Engine, create_engine
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker

from flowweave.bootstrap.settings import Settings


class Base(DeclarativeBase):
    """Shared declarative registry; mappings are owned by module infrastructure packages."""


def uid() -> str:
    return str(uuid4())


def now() -> datetime:
    return datetime.now(UTC)


def create_sync_engine(settings: Settings) -> Engine:
    """Build an explicitly-owned compatibility engine for modules still being migrated.

    New application code uses ``Database.uow()``. This factory exists only so the
    old synchronous handlers can be removed module-by-module without retaining a
    process-global engine.
    """

    if not settings.database_url.startswith("postgresql+psycopg://"):
        raise ValueError("FlowWeave supports PostgreSQL through psycopg only")
    return create_engine(
        settings.database_url,
        pool_pre_ping=True,
        pool_size=settings.pool_size,
        connect_args={"options": f"-c statement_timeout={settings.statement_timeout_ms}"},
    )


def create_sync_session_factory(settings: Settings) -> sessionmaker[Session]:
    return sessionmaker(create_sync_engine(settings), expire_on_commit=False)
