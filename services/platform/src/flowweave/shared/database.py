from __future__ import annotations

from datetime import UTC, datetime
from typing import Any, ClassVar, cast
from uuid import uuid4

from sqlalchemy import Engine, String, and_, create_engine, event, or_, select, text
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

_SHARED_OWNER_TABLES = frozenset(
    {
        "capability_collection_items",
        "capability_collections",
        "capability_dependencies",
        "capability_imports",
        "capability_packages",
        "capability_validations",
        "capability_versions",
        "environment_setup_sessions",
        "environment_versions",
        "event_trigger_actions",
        "event_trigger_deliveries",
        "event_trigger_versions",
        "flow_definitions",
        "flow_edges",
        "flow_nodes",
        "flow_port_mappings",
        "gate_policies",
        "mcp_oauth_authorizations",
        "mcp_oauth_secret_audits",
        "mcp_oauth_secret_references",
        "memory_source_version_references",
        "memory_source_versions",
        "memory_sources",
        "node_assets",
        "node_context_capabilities",
        "node_directories",
        "node_executor_configs",
        "node_io_fields",
        "plugin_source_resolutions",
        "terminal_environments",
    }
)

_USER_ISOLATED_TABLES = frozenset(
    {
        "agent_conversation_bindings",
        "agent_conversation_capabilities",
        "agent_conversation_commands",
        "agent_conversation_message_attachments",
        "agent_attachment_uploads",
        "agent_attachment_upload_parts",
        "agent_sidebar_conversations",
        "agent_conversation_searches",
        "agent_conversation_search_hits",
        "agent_conversation_usage_buckets",
        "agent_work_directories",
        "agent_work_directory_paths",
        "agent_work_directory_versions",
        "agent_workspace_capabilities",
        "agent_workspace_preferences",
        "model_providers",
        "provider_models",
        "website_credentials",
        "flow_runs",
        "flow_run_schedules",
        "flow_run_schedule_occurrences",
        "run_snapshots",
        "node_runs",
        "node_attempts",
        "artifact_versions",
        "candidate_output_sets",
        "attempt_input_bindings",
        "gate_evaluations",
        "human_actions",
        "run_events",
        "runtime_confirmation_approvals",
        "flow_run_runtime_secret_references",
        "flow_run_runtime_allocations",
        "flow_run_runtimes",
        "runtime_generations",
    }
)


def _is_user_isolated_model(model: type[Any]) -> bool:
    table = getattr(model, "__table__", None)
    return table is not None and table.name in _USER_ISOLATED_TABLES


def _tenant_read_criterion(model: type[Any], user_id: str) -> Any:
    """Keep top-level FlowRun control-plane resources shared.

    A top-level FlowRun owns the shared container. Nested FlowRuns and all
    execution records remain private to the user that created them.
    """

    from flowweave.modules.runs.infrastructure.models import FlowRun
    from flowweave.modules.sandboxes.infrastructure.models import (
        FlowRunRuntime,
        FlowRunRuntimeAllocation,
    )
    from flowweave.modules.users.application.security import FLOWWEAVE_USER_ID

    table = model.__table__.name
    private = model.owner_user_id == user_id
    shared_owner = model.owner_user_id == FLOWWEAVE_USER_ID
    if table == "flow_runs":
        shared = model.parent_flow_run_id.is_(None)
    elif table == "run_snapshots":
        shared = model.flow_run_id.in_(
            select(FlowRun.id).where(
                FlowRun.parent_flow_run_id.is_(None),
                FlowRun.owner_user_id == FLOWWEAVE_USER_ID,
            )
        )
    elif table in {"flow_run_runtime_allocations", "flow_run_runtimes"}:
        shared = model.node_attempt_id.is_(None)
    elif table == "runtime_generations":
        shared = model.runtime_session_id.in_(
            select(FlowRunRuntime.id).where(
                FlowRunRuntime.node_attempt_id.is_(None),
                FlowRunRuntime.owner_user_id == FLOWWEAVE_USER_ID,
            )
        )
    elif table == "flow_run_runtime_secret_references":
        shared = model.id.in_(
            select(FlowRunRuntimeAllocation.secret_reference_id).where(
                FlowRunRuntimeAllocation.node_attempt_id.is_(None),
                FlowRunRuntimeAllocation.owner_user_id == FLOWWEAVE_USER_ID,
            )
        )
    else:
        return private
    return or_(private, and_(shared_owner, shared))


def _is_shared_flowrun_item(session: Session, item: Any) -> bool:
    """Return whether an ORM row belongs to the shared top-level FlowRun."""

    from flowweave.modules.runs.infrastructure.models import FlowRun
    from flowweave.modules.sandboxes.infrastructure.models import (
        FlowRunRuntime,
        FlowRunRuntimeAllocation,
    )

    table = item.__table__.name
    if table == "flow_runs":
        return item.parent_flow_run_id is None
    if table == "run_snapshots":
        run = next(
            (
                candidate
                for candidate in session.new
                if isinstance(candidate, FlowRun) and candidate.id == item.flow_run_id
            ),
            None,
        )
        if run is None:
            with session.no_autoflush:
                run = session.get(FlowRun, item.flow_run_id)
        return run is not None and run.parent_flow_run_id is None
    if table in {"flow_run_runtime_allocations", "flow_run_runtimes"}:
        return item.node_attempt_id is None
    if table == "runtime_generations":
        runtime = next(
            (
                candidate
                for candidate in session.new
                if isinstance(candidate, FlowRunRuntime) and candidate.id == item.runtime_session_id
            ),
            None,
        )
        if runtime is None:
            with session.no_autoflush:
                runtime = session.get(FlowRunRuntime, item.runtime_session_id)
        return runtime is not None and runtime.node_attempt_id is None
    if table == "flow_run_runtime_secret_references":
        allocation = next(
            (
                candidate
                for candidate in session.new
                if getattr(getattr(candidate, "__table__", None), "name", None)
                == "flow_run_runtime_allocations"
                and candidate.secret_reference_id == item.id
            ),
            None,
        )
        if allocation is None:
            with session.no_autoflush:
                allocation = session.scalar(
                    select(FlowRunRuntimeAllocation).where(
                        FlowRunRuntimeAllocation.secret_reference_id == item.id
                    )
                )
        return allocation is not None and allocation.node_attempt_id is None
    return False


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
            # Shared catalog/flow definitions keep one stable owner without
            # replacing the request tenant or bypassing private-resource reads.
            default=lambda: (
                FLOWWEAVE_USER_ID
                if getattr(cls, "__tablename__", None) in _SHARED_OWNER_TABLES
                else current_user_id(default=FLOWWEAVE_USER_ID)
            ),
        )


def uid() -> str:
    return str(uuid4())


def now() -> datetime:
    return datetime.now(UTC)


@event.listens_for(Session, "after_begin")
def _bind_tenant_context(  # pyright: ignore[reportUnusedFunction]
    session: Session, _transaction: object, connection: object
) -> None:
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
def _enforce_tenant_writes(  # pyright: ignore[reportUnusedFunction]
    session: Session, _flush_context: object, _instances: object
) -> None:
    """Assign ownership and reject cross-user ORM writes before SQL is emitted."""

    from flowweave.modules.users.application.security import (
        FLOWWEAVE_USER_ID,
        current_user_id,
        tenant_filter_bypassed,
    )

    user_id = current_user_id(default=FLOWWEAVE_USER_ID)
    for item in session.new:
        item_type = cast(type[Any], type(item))
        if _is_user_isolated_model(item_type) and _is_shared_flowrun_item(session, item):
            item.owner_user_id = FLOWWEAVE_USER_ID
    if tenant_filter_bypassed():
        return
    for item in session.new:
        item_type = cast(type[Any], type(item))
        if _is_user_isolated_model(item_type):
            if _is_shared_flowrun_item(session, item):
                continue
            tenant_item = item  # keep the dynamic ownership mixin local to persistence
            owner = tenant_item.owner_user_id  # type: ignore[attr-defined]
            if owner in {None, ""}:
                tenant_item.owner_user_id = user_id  # type: ignore[attr-defined]
            elif owner != user_id:
                raise RuntimeError("Cross-user record creation is forbidden")
    for item in session.dirty.union(session.deleted):
        item_type = cast(type[Any], type(item))
        if (
            _is_user_isolated_model(item_type)
            and not _is_shared_flowrun_item(session, item)
            and item.owner_user_id != user_id  # type: ignore[attr-defined]
        ):
            raise RuntimeError("Cross-user record mutation is forbidden")


@event.listens_for(Session, "do_orm_execute")
def _enforce_tenant_reads(  # pyright: ignore[reportUnusedFunction]
    execute_state: Any,
) -> None:
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
                    _tenant_read_criterion(model, user_id),
                    include_aliases=True,
                )
            )
        execute_state.statement = statement
        return
    if getattr(execute_state, "is_update", False) or getattr(execute_state, "is_delete", False):
        mapper = execute_state.bind_arguments.get("mapper")
        if mapper is not None and _is_user_isolated_model(mapper.class_):
            execute_state.statement = execute_state.statement.where(
                _tenant_read_criterion(mapper.class_, user_id)
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
        max_overflow=settings.pool_max_overflow,
        pool_timeout=settings.database_pool_timeout_seconds,
        connect_args={"options": f"-c statement_timeout={settings.statement_timeout_ms}"},
    )


def create_sync_session_factory(settings: Settings) -> sessionmaker[Session]:
    return sessionmaker(create_sync_engine(settings), expire_on_commit=False)
