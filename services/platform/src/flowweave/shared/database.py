from __future__ import annotations

from datetime import UTC, datetime
from typing import Any, ClassVar
from uuid import uuid4

from sqlalchemy import Engine, String, create_engine, event, text
from sqlalchemy.orm import (
    DeclarativeBase,
    Mapped,
    Session,
    declared_attr,
    mapped_column,
    sessionmaker,
    with_loader_criteria,
)

from flowweave.bootstrap.settings import Settings

_USER_ISOLATED_TABLES = frozenset(
    {
        "agent_conversation_bindings",
        "agent_conversation_capabilities",
        "agent_conversation_commands",
        "agent_conversation_message_attachments",
        "agent_work_directories",
        "agent_work_directory_paths",
        "agent_work_directory_versions",
        "agent_workspace_capabilities",
        "agent_workspace_preferences",
    }
)


def _is_user_isolated_model(model: type[Any]) -> bool:
    table = getattr(model, "__table__", None)
    return table is not None and table.name in _USER_ISOLATED_TABLES


class Base(DeclarativeBase):
    """Shared declarative registry; mappings are owned by module infrastructure packages."""

    __tenant_scoped__: ClassVar[bool] = True

    @declared_attr
    def owner_user_id(cls) -> Mapped[str]:
        from flowweave.modules.users.application.security import (
            FLOWWEAVE_USER_ID,
            current_user_id,
        )

        return mapped_column(
            String(36),
            nullable=False,
            index=True,
            default=lambda: current_user_id(default=FLOWWEAVE_USER_ID),
        )


def uid() -> str:
    return str(uuid4())


def now() -> datetime:
    return datetime.now(UTC)


@event.listens_for(Session, "after_begin")
def _bind_tenant_context(session: Session, _transaction: object, connection: object) -> None:
    """Set transaction-local PostgreSQL RLS identity for every ORM session."""

    from flowweave.modules.users.application.security import (
        FLOWWEAVE_USER_ID,
        current_user_id,
        tenant_filter_bypassed,
    )
    connection.execute(  # type: ignore[attr-defined]
        text(
            "SELECT set_config('flowweave.user_id', :user_id, true), "
            "set_config('flowweave.bypass', :bypass, true)"
        ),
        {
            "user_id": current_user_id(default=FLOWWEAVE_USER_ID),
            "bypass": "on" if tenant_filter_bypassed() else "off",
        },
    )


@event.listens_for(Session, "before_flush")
def _enforce_tenant_writes(session: Session, _flush_context: object, _instances: object) -> None:
    """Assign ownership and reject cross-user ORM writes before SQL is emitted."""

    from flowweave.modules.users.application.security import (
        FLOWWEAVE_USER_ID,
        current_user_id,
        tenant_filter_bypassed,
    )

    if tenant_filter_bypassed():
        return
    user_id = current_user_id(default=FLOWWEAVE_USER_ID)
    for item in session.new:
        if _is_user_isolated_model(type(item)):
            tenant_item = item  # keep the dynamic ownership mixin local to persistence
            owner = tenant_item.owner_user_id  # type: ignore[attr-defined]
            if owner in {None, ""}:
                tenant_item.owner_user_id = user_id  # type: ignore[attr-defined]
            elif owner != user_id:
                raise RuntimeError("Cross-user record creation is forbidden")
    for item in session.dirty.union(session.deleted):
        if (
            _is_user_isolated_model(type(item))
            and item.owner_user_id != user_id  # type: ignore[attr-defined]
        ):
            raise RuntimeError("Cross-user record mutation is forbidden")


@event.listens_for(Session, "do_orm_execute")
def _enforce_tenant_reads(execute_state: Any) -> None:
    """Apply tenant criteria even when PostgreSQL is reached through its owner role."""

    from flowweave.modules.users.application.security import (
        FLOWWEAVE_USER_ID,
        current_user_id,
        tenant_filter_bypassed,
    )

    if tenant_filter_bypassed():
        return
    user_id = current_user_id(default=FLOWWEAVE_USER_ID)
    if getattr(execute_state, "is_select", False):
        statement = execute_state.statement
        for mapper in Base.registry.mappers:
            model = mapper.class_
            if not _is_user_isolated_model(model):
                continue
            statement = statement.options(
                with_loader_criteria(
                    model,
                    model.owner_user_id == user_id,
                    include_aliases=True,
                )
            )
        execute_state.statement = statement
        return
    if getattr(execute_state, "is_update", False) or getattr(
        execute_state, "is_delete", False
    ):
        mapper = execute_state.bind_arguments.get("mapper")
        if mapper is not None and _is_user_isolated_model(mapper.class_):
            execute_state.statement = execute_state.statement.where(
                mapper.class_.owner_user_id == user_id
            )


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
