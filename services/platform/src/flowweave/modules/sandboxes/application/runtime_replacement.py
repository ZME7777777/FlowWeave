from __future__ import annotations

import time
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from sqlalchemy import func, select
from sqlalchemy.engine import Connection, Engine
from sqlalchemy.orm import Session

from flowweave.modules.agent_sessions.public import AgentConversationBinding
from flowweave.modules.sandboxes.application.runtime_allocation import (
    resolve_runtime_secret,
)
from flowweave.modules.sandboxes.application.runtime_sessions import (
    acquire_runtime_replacement_lease,
    activate_runtime_replacement,
    attach_runtime_replacement_generation,
    ensure_runtime_generation,
    mark_runtime_generation_deleted,
    mark_runtime_generation_draining,
    mark_runtime_generation_stopped,
    next_runtime_generation_number,
    prepare_runtime_generation,
    record_runtime_replacement_failure,
    renew_runtime_replacement_lease,
)
from flowweave.modules.sandboxes.infrastructure.docker import (
    DockerSandboxProvider,
    backend_name,
)
from flowweave.modules.sandboxes.infrastructure.models import (
    FlowRunRuntime,
    ManagedSandbox,
    RuntimeGeneration,
)
from flowweave.modules.tasks.public import Lease, enqueue, lease_is_current
from flowweave.runtime.base import RuntimeConversationIdentity, RuntimeHandle
from flowweave.runtime.routing import runtime_for
from flowweave.shared.database import uid
from flowweave.shared.errors import DomainError
from flowweave.shared.settings import get_settings


@dataclass(frozen=True, slots=True)
class _ReplacementState:
    runtime_session_id: str
    source_generation_id: str
    target_generation_id: str
    source_runtime_id: str | None
    target_runtime_id: str
    replacement_lease_token: str
    replacement_lease_owner: str


_NON_RETRYABLE_RELOAD_ERRORS = {
    "RUNTIME_CONVERSATION_MISSING",
    "RUNTIME_CONVERSATION_ID_INVALID",
    "RUNTIME_CONVERSATION_IDENTITY_DRIFT",
    "RUNTIME_EVENT_IDENTITY_INVALID",
    "RUNTIME_EVENT_IDENTITY_DRIFT",
    "RUNTIME_RELOAD_IDENTITY_MISMATCH",
    "RUNTIME_WORKSPACE_IDENTITY_DRIFT",
    "RUNTIME_PERSISTENCE_IDENTITY_DRIFT",
    "RUNTIME_RELOAD_EMPTY",
}


def _control_engine(db: Session) -> Engine:
    bind = db.get_bind()
    return bind.engine if isinstance(bind, Connection) else bind


def enqueue_flow_run_runtime_replacement(
    db: Session,
    *,
    flow_run_id: str,
    failed_generation: int,
    reason: str,
    request_key: str | None = None,
) -> None:
    """Publish one idempotent recovery task for a failed active generation."""

    enqueue(
        db,
        task_type="REPLACE_FLOW_RUN_RUNTIME",
        aggregate_type="FLOW_RUN",
        aggregate_id=flow_run_id,
        idempotency_key=(
            f"runtime-replacement:{flow_run_id}:{failed_generation}"
            f"{f':{request_key}' if request_key else ''}"
        ),
        payload={
            "failed_generation": failed_generation,
            "reason": reason[:100],
        },
    )


def record_terminal_runtime_replacement_failure(
    db: Session,
    flow_run_id: str,
    error: str,
) -> None:
    """Make exhausted replacement retries explicitly diagnosable and non-routable."""

    session = db.scalar(
        select(FlowRunRuntime)
        .where(
            FlowRunRuntime.flow_run_id == flow_run_id,
            FlowRunRuntime.node_attempt_id.is_(None),
        )
        .with_for_update()
    )
    if (
        session is None
        or session.replacement_generation is None
        or session.status not in {"REPLACING", "RECONNECTING", "DEGRADED"}
    ):
        return
    session.status = "DEGRADED"
    session.replacement_lease_token = None
    session.replacement_lease_owner = None
    session.replacement_lease_until = None
    session.replacement_error_code = "RUNTIME_REPLACEMENT_EXHAUSTED"
    session.replacement_error_summary = error[:2000]
    session.row_version += 1
    session.updated_at = datetime.now(UTC)
    db.flush()


def _require_task_lease(db: Session, lease: Lease) -> None:
    if not lease_is_current(db, lease):
        raise RuntimeError("task lease was lost during Runtime replacement")


def _replacement_owner(lease: Lease) -> str:
    return f"{lease.owner}:{lease.task_id}:{lease.generation}"[:200]


def _replacement_lease_seconds() -> int:
    settings = get_settings()
    return max(
        settings.task_lease_seconds * 2,
        settings.terminal_environment_start_timeout_seconds + 90,
    )


def _ensure_replacement_state(
    db: Session,
    *,
    flow_run_id: str,
    expected_generation: int,
    lease: Lease,
) -> _ReplacementState | None:
    settings = get_settings()
    owner = _replacement_owner(lease)
    engine = _control_engine(db)
    with engine.connect() as connection:
        lock_key = f"RUNTIME_REPLACEMENT:{flow_run_id}"
        lock_id = connection.scalar(select(func.hashtextextended(lock_key, 0)))
        if lock_id is None:
            raise RuntimeError("Could not derive the Runtime replacement lock")
        connection.scalar(select(func.pg_advisory_lock(lock_id)))
        connection.commit()
        try:
            with Session(bind=connection, expire_on_commit=False) as control_db:
                current_session = control_db.scalar(
                    select(FlowRunRuntime)
                    .where(
                        FlowRunRuntime.flow_run_id == flow_run_id,
                        FlowRunRuntime.node_attempt_id.is_(None),
                    )
                    .with_for_update()
                )
                if (
                    current_session is not None
                    and current_session.active_generation is not None
                    and current_session.active_generation > expected_generation
                ):
                    # The independent activation transaction committed but the
                    # task worker died before it could mark this task complete.
                    control_db.commit()
                    return None
                if (
                    current_session is None
                    or current_session.active_generation != expected_generation
                ):
                    raise DomainError(
                        "RUNTIME_REPLACEMENT_GENERATION_FENCED",
                        "The recovery task does not target the active generation",
                        409,
                        {
                            "flow_run_id": flow_run_id,
                            "expected_generation": expected_generation,
                        },
                    )
                session, replacement = acquire_runtime_replacement_lease(
                    control_db,
                    flow_run_id=flow_run_id,
                    owner=owner,
                    lease_seconds=_replacement_lease_seconds(),
                )
                source = control_db.scalar(
                    select(RuntimeGeneration)
                    .where(
                        RuntimeGeneration.runtime_session_id == session.id,
                        RuntimeGeneration.generation == replacement.source_generation,
                    )
                    .with_for_update()
                )
                if source is None:
                    raise DomainError(
                        "RUNTIME_REPLACEMENT_SOURCE_MISSING",
                        "The active Runtime generation audit record is unavailable",
                        409,
                        {"runtime_session_id": session.id},
                    )
                source_runtime = (
                    control_db.get(ManagedSandbox, source.managed_runtime_id)
                    if source.managed_runtime_id is not None
                    else None
                )
                target = (
                    control_db.scalar(
                        select(RuntimeGeneration)
                        .where(
                            RuntimeGeneration.runtime_session_id == session.id,
                            RuntimeGeneration.generation == session.replacement_generation,
                        )
                        .with_for_update()
                    )
                    if session.replacement_generation is not None
                    else None
                )
                target_runtime = (
                    control_db.get(ManagedSandbox, target.managed_runtime_id)
                    if target is not None and target.managed_runtime_id is not None
                    else None
                )
                if target is None:
                    if source_runtime is None:
                        raise DomainError(
                            "RUNTIME_REPLACEMENT_SOURCE_MISSING",
                            "The old Runtime disappeared before N+1 was allocated",
                            409,
                            {"runtime_session_id": session.id},
                        )
                    # The active-Runtime uniqueness constraint is keyed to
                    # desired state.  Fence the old provider before inserting
                    # N+1 so the durable replacement record can coexist with
                    # the still-running source container.  The container is
                    # deliberately drained later, after N+1 has prewarmed;
                    # this only prevents reconciliation from restarting the
                    # fenced writer in the interim.
                    source_runtime.desired_state = "DELETED"
                    source_runtime.observed_state = "DELETING"
                    source_runtime.next_reconcile_at = datetime.now(UTC) + timedelta(seconds=60)
                    control_db.flush()
                    generation_number = next_runtime_generation_number(
                        control_db,
                        session.id,
                        managed_generation_floor=int(
                            control_db.scalar(
                                select(func.coalesce(func.max(ManagedSandbox.generation), 0)).where(
                                    ManagedSandbox.kind == "AGENT_RUNTIME",
                                    ManagedSandbox.owner_type == "FLOW_RUN",
                                    ManagedSandbox.owner_id == flow_run_id,
                                )
                            )
                            or 0
                        ),
                    )
                    resource_id = uid()
                    created_at = datetime.now(UTC)
                    target_runtime = ManagedSandbox(
                        id=resource_id,
                        kind="AGENT_RUNTIME",
                        owner_type="FLOW_RUN",
                        owner_id=flow_run_id,
                        backend="docker",
                        backend_resource_name=backend_name(
                            resource_id,
                            owner_type="FLOW_RUN",
                            owner_id=flow_run_id,
                        ),
                        desired_state="RUNNING",
                        observed_state="CREATING",
                        generation=generation_number,
                        image_reference=session.runtime_image_digest,
                        runtime_allocation_id=session.workspace_allocation_id,
                        spec_json=dict(source_runtime.spec_json or {}),
                        last_activity_at=created_at,
                        idle_expires_at=None,
                        hard_expires_at=created_at
                        + timedelta(seconds=settings.sandbox_runtime_hard_ttl_seconds),
                        next_reconcile_at=created_at
                        + timedelta(seconds=settings.terminal_environment_start_timeout_seconds),
                    )
                    control_db.add(target_runtime)
                    target = ensure_runtime_generation(
                        control_db,
                        session=session,
                        generation=generation_number,
                        managed_runtime=target_runtime,
                    )
                    replacement = attach_runtime_replacement_generation(
                        control_db,
                        session=session,
                        generation=target,
                        replacement_lease_token=replacement.token,
                    )
                if target_runtime is None:
                    raise DomainError(
                        "RUNTIME_REPLACEMENT_TARGET_MISSING",
                        "The replacement Runtime provider record is unavailable",
                        409,
                        {"runtime_session_id": session.id},
                    )
                if (
                    target.runtime_image_digest != session.runtime_image_digest
                    or target_runtime.image_reference != session.runtime_image_digest
                    or target_runtime.runtime_allocation_id != session.workspace_allocation_id
                    or target_runtime.generation != target.generation
                    or target_runtime.owner_type != "FLOW_RUN"
                    or target_runtime.owner_id != flow_run_id
                ):
                    raise DomainError(
                        "RUNTIME_REPLACEMENT_TARGET_INVALID",
                        "The replacement Runtime no longer matches the frozen Session",
                        409,
                        {"runtime_session_id": session.id},
                    )
                control_db.commit()
                return _ReplacementState(
                    runtime_session_id=session.id,
                    source_generation_id=source.id,
                    target_generation_id=target.id,
                    source_runtime_id=source_runtime.id if source_runtime is not None else None,
                    target_runtime_id=target_runtime.id,
                    replacement_lease_token=replacement.token,
                    replacement_lease_owner=owner,
                )
        finally:
            if connection.in_transaction():
                connection.rollback()
            connection.scalar(select(func.pg_advisory_unlock(lock_id)))
            connection.commit()


def _renew(control_db: Session, state: _ReplacementState) -> FlowRunRuntime:
    renew_runtime_replacement_lease(
        control_db,
        runtime_session_id=state.runtime_session_id,
        replacement_lease_token=state.replacement_lease_token,
        owner=state.replacement_lease_owner,
        lease_seconds=_replacement_lease_seconds(),
    )
    session = control_db.get(FlowRunRuntime, state.runtime_session_id)
    if session is None:
        raise RuntimeError("Runtime Session disappeared during replacement")
    control_db.refresh(session)
    return session


def _handle(resource_name: str, conversation_id: str) -> RuntimeHandle:
    return RuntimeHandle(
        job_id=f"env-chat:{resource_name}",
        conversation_id=conversation_id,
        runtime_resource_name=resource_name,
    )


def _probe_identity(
    *,
    resource_name: str,
    conversation_id: str,
    expected: RuntimeConversationIdentity | None,
) -> RuntimeConversationIdentity:
    handle = _handle(resource_name, conversation_id)
    identity = runtime_for("openhands", handle).reload_conversation(
        handle,
        expected=expected,
    )
    if identity.event_id is None:
        raise DomainError(
            "RUNTIME_RELOAD_EMPTY",
            "An empty OpenHands Conversation cannot prove replacement recovery",
            409,
            {"conversation_id": conversation_id},
        )
    return identity


def _record_failure(
    engine: Engine,
    state: _ReplacementState,
    exc: DomainError,
) -> None:
    with Session(bind=engine, expire_on_commit=False) as control_db:
        session = control_db.get(FlowRunRuntime, state.runtime_session_id)
        if session is None or session.replacement_lease_token != state.replacement_lease_token:
            control_db.rollback()
            return
        record_runtime_replacement_failure(
            control_db,
            session=session,
            replacement_lease_token=state.replacement_lease_token,
            error_code=exc.code,
            error_summary="Runtime replacement failed; inspect protected diagnostics",
            retryable=exc.code not in _NON_RETRYABLE_RELOAD_ERRORS,
        )
        control_db.commit()


def process_flow_run_runtime_replacement(
    db: Session,
    flow_run_id: str,
    expected_generation: int,
    lease: Lease,
    *,
    commit: bool = True,
) -> None:
    """Idempotently replace N with one preallocated N+1 and original-ID reload."""

    _require_task_lease(db, lease)
    # Every durable replacement mutation uses independent control Sessions.
    # Discard the handler's read-only lease transaction before Docker/HTTP I/O.
    if db.in_transaction():
        db.rollback()
    state = _ensure_replacement_state(
        db,
        flow_run_id=flow_run_id,
        expected_generation=expected_generation,
        lease=lease,
    )
    if state is None:
        return
    engine = _control_engine(db)
    provider = DockerSandboxProvider(get_settings())
    try:
        with Session(bind=engine, expire_on_commit=False) as control_db:
            session = _renew(control_db, state)
            target = control_db.get(RuntimeGeneration, state.target_generation_id)
            target_runtime = control_db.get(ManagedSandbox, state.target_runtime_id)
            if target is None or target_runtime is None:
                raise DomainError(
                    "RUNTIME_REPLACEMENT_TARGET_MISSING",
                    "The replacement Runtime provider record is unavailable",
                    409,
                )
            runtime_secret = resolve_runtime_secret(control_db, session.workspace_allocation_id)
            control_db.commit()

        observation = provider.ensure_running(
            target_runtime,
            runtime_secret_key=runtime_secret,
        )
        _require_task_lease(db, lease)
        with Session(bind=engine, expire_on_commit=False) as control_db:
            session = _renew(control_db, state)
            target = control_db.get(RuntimeGeneration, state.target_generation_id)
            target_runtime = control_db.get(ManagedSandbox, state.target_runtime_id)
            if target is None or target_runtime is None:
                raise DomainError(
                    "RUNTIME_REPLACEMENT_TARGET_MISSING",
                    "The replacement Runtime disappeared after prewarm",
                    409,
                )
            target_runtime.backend_resource_id = observation.resource_identifier
            target_runtime.observed_state = observation.state
            target_runtime.last_error_code = None
            target_runtime.last_error_detail = None
            target_runtime.next_reconcile_at = datetime.now(UTC) + timedelta(
                seconds=get_settings().sandbox_reconcile_seconds
            )
            prepare_runtime_generation(
                control_db,
                session=session,
                generation=target,
                instance_id=observation.resource_identifier,
                replacement_lease_token=state.replacement_lease_token,
            )
            binding = control_db.scalar(
                select(AgentConversationBinding)
                .where(
                    AgentConversationBinding.host_kind == "FLOW_NODE",
                    AgentConversationBinding.runtime_session_id == session.id,
                    AgentConversationBinding.lifecycle == "ACTIVE",
                )
                .order_by(AgentConversationBinding.created_at)
                .limit(1)
            )
            source = control_db.get(RuntimeGeneration, state.source_generation_id)
            source_runtime = (
                control_db.get(ManagedSandbox, state.source_runtime_id)
                if state.source_runtime_id is not None
                else None
            )
            control_db.commit()

        expected_identity: RuntimeConversationIdentity | None = None
        if source is None:
            raise DomainError(
                "RUNTIME_REPLACEMENT_SOURCE_MISSING",
                "The source generation audit record is unavailable",
                409,
            )
        if binding is not None and source_runtime is not None and source.state == "READY":
            try:
                expected_identity = _probe_identity(
                    resource_name=source_runtime.backend_resource_name,
                    conversation_id=binding.openhands_conversation_id,
                    expected=None,
                )
            except DomainError as exc:
                if exc.code in _NON_RETRYABLE_RELOAD_ERRORS:
                    raise

        graceful = False
        if source.state not in {"STOPPED", "DELETED"}:
            with Session(bind=engine, expire_on_commit=False) as control_db:
                session = _renew(control_db, state)
                source = control_db.get(RuntimeGeneration, state.source_generation_id)
                if source is None:
                    raise DomainError(
                        "RUNTIME_REPLACEMENT_SOURCE_MISSING",
                        "The source generation audit record is unavailable",
                        409,
                    )
                mark_runtime_generation_draining(
                    control_db,
                    session=session,
                    generation=source,
                    replacement_lease_token=state.replacement_lease_token,
                )
                current_source_runtime = (
                    control_db.get(ManagedSandbox, state.source_runtime_id)
                    if state.source_runtime_id is not None
                    else None
                )
                if current_source_runtime is not None:
                    # Deletion intent is monotonic: reconciliation may finish
                    # cleanup, but it must never restart the fenced writer.
                    current_source_runtime.desired_state = "DELETED"
                    current_source_runtime.observed_state = "DELETING"
                    current_source_runtime.next_reconcile_at = datetime.now(UTC) + timedelta(
                        seconds=60
                    )
                control_db.commit()
            if source_runtime is not None:
                drain_result = provider.drain(source_runtime)
                graceful = drain_result.graceful
            _require_task_lease(db, lease)
            with Session(bind=engine, expire_on_commit=False) as control_db:
                session = _renew(control_db, state)
                source = control_db.get(RuntimeGeneration, state.source_generation_id)
                if source is None:
                    raise DomainError(
                        "RUNTIME_REPLACEMENT_SOURCE_MISSING",
                        "The source generation audit record is unavailable",
                        409,
                    )
                mark_runtime_generation_stopped(
                    control_db,
                    session=session,
                    generation=source,
                    replacement_lease_token=state.replacement_lease_token,
                    graceful=graceful,
                )
                not_before = session.replacement_not_before
                control_db.commit()
        else:
            with Session(bind=engine, expire_on_commit=False) as control_db:
                session = _renew(control_db, state)
                not_before = session.replacement_not_before
                control_db.commit()

        while not_before is not None and datetime.now(UTC) < not_before:
            time.sleep(min(1.0, max((not_before - datetime.now(UTC)).total_seconds(), 0.0)))
            _require_task_lease(db, lease)

        if binding is not None:
            _probe_identity(
                resource_name=target_runtime.backend_resource_name,
                conversation_id=binding.openhands_conversation_id,
                expected=expected_identity,
            )

        if source_runtime is not None:
            provider.delete(source_runtime)
        _require_task_lease(db, lease)
        with Session(bind=engine, expire_on_commit=False) as control_db:
            session = _renew(control_db, state)
            source = control_db.get(RuntimeGeneration, state.source_generation_id)
            target = control_db.get(RuntimeGeneration, state.target_generation_id)
            source_runtime = (
                control_db.get(ManagedSandbox, state.source_runtime_id)
                if state.source_runtime_id is not None
                else None
            )
            if source is None or target is None:
                raise DomainError(
                    "RUNTIME_REPLACEMENT_GENERATION_MISSING",
                    "The replacement generation audit record is unavailable",
                    409,
                )
            mark_runtime_generation_deleted(
                control_db,
                session=session,
                generation=source,
                replacement_lease_token=state.replacement_lease_token,
            )
            if source_runtime is not None:
                control_db.delete(source_runtime)
                control_db.flush()
                source.managed_runtime_id = None
            activate_runtime_replacement(
                control_db,
                session=session,
                source=source,
                target=target,
                replacement_lease_token=state.replacement_lease_token,
            )
            control_db.commit()
    except DomainError as exc:
        _record_failure(engine, state, exc)
        raise
    if commit:
        db.commit()
    else:
        db.flush()


__all__ = (
    "enqueue_flow_run_runtime_replacement",
    "process_flow_run_runtime_replacement",
    "record_terminal_runtime_replacement_failure",
)
