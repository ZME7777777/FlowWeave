from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from sqlalchemy import delete, func, or_, select, update
from sqlalchemy.orm import Session

from flowweave.modules.sandboxes.application.runtime_allocation import (
    node_attempt_workspace_context,
)
from flowweave.modules.sandboxes.application.runtime_owner import runtime_owner_flow_run_id
from flowweave.modules.sandboxes.infrastructure.models import (
    FlowRunRuntime,
    FlowRunRuntimeAllocation,
    ManagedSandbox,
    RuntimeGeneration,
)
from flowweave.shared.database import uid
from flowweave.shared.errors import DomainError
from flowweave.shared.models import FlowRun, NodeAttempt, NodeRun


@dataclass(frozen=True, slots=True)
class RuntimeSessionFence:
    """Opaque command fence for one active physical generation."""

    runtime_session_id: str
    generation: int
    fence_token: str
    session_row_version: int
    generation_row_version: int


@dataclass(frozen=True, slots=True)
class ActiveRuntimeConnection:
    """Protected physical connection resolved from the active logical generation."""

    runtime_session_id: str
    flow_run_id: str
    managed_runtime_id: str
    resource_name: str
    generation: int
    runtime_fence: RuntimeSessionFence


@dataclass(frozen=True, slots=True)
class RuntimeReplacementLease:
    """Durable single-worker lease for one N -> N+1 replacement."""

    runtime_session_id: str
    flow_run_id: str
    source_generation: int
    target_generation: int | None
    token: str
    owner: str
    lease_until: datetime


def ensure_flow_run_runtime_session(
    db: Session,
    *,
    flow_run_id: str,
    environment_version_id: str,
    runtime_image_digest: str,
    workspace_allocation: FlowRunRuntimeAllocation,
) -> FlowRunRuntime:
    """Create or verify the immutable identity of one FlowRun Runtime Session."""

    if re.fullmatch(r"sha256:[0-9a-f]{64}", runtime_image_digest) is None:
        raise DomainError(
            "RUNTIME_SESSION_IMAGE_REQUIRED",
            "The Runtime Session requires an immutable Runtime Image digest",
            409,
            {"flow_run_id": flow_run_id},
        )
    owner_id = db.scalar(select(FlowRun.id).where(FlowRun.id == flow_run_id).with_for_update())
    if owner_id is None or workspace_allocation.flow_run_id != flow_run_id:
        raise DomainError(
            "RUNTIME_SESSION_OWNER_INVALID",
            "The Runtime Session does not match its FlowRun allocation",
            409,
            {"flow_run_id": flow_run_id},
        )
    item = db.scalar(
        select(FlowRunRuntime)
        .where(
            FlowRunRuntime.flow_run_id == flow_run_id,
            FlowRunRuntime.node_attempt_id.is_(None),
        )
        .with_for_update()
    )
    if item is None:
        item = FlowRunRuntime(
            id=uid(),
            flow_run_id=flow_run_id,
            environment_version_id=environment_version_id,
            runtime_image_digest=runtime_image_digest,
            workspace_allocation_id=workspace_allocation.id,
            status="STARTING",
            row_version=1,
        )
        db.add(item)
        db.flush()
        return item
    if (
        item.environment_version_id != environment_version_id
        or item.runtime_image_digest != runtime_image_digest
        or item.workspace_allocation_id != workspace_allocation.id
    ):
        raise DomainError(
            "RUNTIME_SESSION_SPEC_CONFLICT",
            "The FlowRun Runtime Session has a different immutable specification",
            409,
            {"runtime_session_id": item.id, "flow_run_id": flow_run_id},
        )
    if item.status == "DELETING":
        raise DomainError(
            "RUNTIME_SESSION_NOT_ACTIVE",
            "The FlowRun Runtime Session no longer accepts commands",
            409,
            {"runtime_session_id": item.id},
        )
    return item


def ensure_node_attempt_runtime_session(
    db: Session,
    *,
    flow_run_id: str,
    node_attempt_id: str,
    environment_version_id: str,
    runtime_image_digest: str,
    workspace_allocation: FlowRunRuntimeAllocation,
) -> FlowRunRuntime:
    """Create or verify the immutable Runtime Session for one NodeAttempt."""

    if re.fullmatch(r"sha256:[0-9a-f]{64}", runtime_image_digest) is None:
        raise DomainError(
            "RUNTIME_SESSION_IMAGE_REQUIRED",
            "The Runtime Session requires an immutable Runtime Image digest",
            409,
            {"node_attempt_id": node_attempt_id},
        )
    attempt = db.get(NodeAttempt, node_attempt_id)
    node_run = db.get(NodeRun, attempt.node_run_id) if attempt else None
    if (
        node_run is None
        or node_run.flow_run_id != flow_run_id
        or workspace_allocation.flow_run_id != flow_run_id
        or workspace_allocation.node_attempt_id != node_attempt_id
    ):
        raise DomainError(
            "RUNTIME_SESSION_OWNER_INVALID",
            "The Runtime Session does not match its Node Attempt allocation",
            409,
            {"flow_run_id": flow_run_id, "node_attempt_id": node_attempt_id},
        )
    item = db.scalar(
        select(FlowRunRuntime)
        .where(FlowRunRuntime.node_attempt_id == node_attempt_id)
        .with_for_update()
    )
    # Provisioning records are committed independently so a controller failure
    # can be reconciled. A failed first generation whose allocation was rolled
    # back has no reusable immutable specification; discard only that exact
    # empty failure shape and let the caller create a fresh Attempt session.
    if item is not None and item.workspace_allocation_id != workspace_allocation.id:
        generations = list(
            db.scalars(
                select(RuntimeGeneration)
                .where(RuntimeGeneration.runtime_session_id == item.id)
                .with_for_update()
            )
        )
        if (
            item.status == "DEGRADED"
            and item.active_generation is None
            and item.replacement_generation is None
            and generations
            and all(generation.state == "FAILED" for generation in generations)
        ):
            db.execute(
                delete(RuntimeGeneration).where(RuntimeGeneration.runtime_session_id == item.id)
            )
            db.delete(item)
            db.flush()
            item = None
    if item is None:
        item = FlowRunRuntime(
            id=uid(),
            flow_run_id=flow_run_id,
            node_attempt_id=node_attempt_id,
            environment_version_id=environment_version_id,
            runtime_image_digest=runtime_image_digest,
            workspace_allocation_id=workspace_allocation.id,
            status="STARTING",
            row_version=1,
        )
        db.add(item)
        db.flush()
        return item
    if (
        item.flow_run_id != flow_run_id
        or item.environment_version_id != environment_version_id
        or item.runtime_image_digest != runtime_image_digest
        or item.workspace_allocation_id != workspace_allocation.id
    ):
        raise DomainError(
            "RUNTIME_SESSION_SPEC_CONFLICT",
            "The Node Attempt Runtime Session has a different immutable specification",
            409,
            {"runtime_session_id": item.id, "node_attempt_id": node_attempt_id},
        )
    if item.status == "DELETING":
        raise DomainError(
            "RUNTIME_SESSION_NOT_ACTIVE",
            "The Node Attempt Runtime Session no longer accepts commands",
            409,
            {"runtime_session_id": item.id},
        )
    return item


def next_runtime_generation_number(
    db: Session,
    runtime_session_id: str,
    *,
    managed_generation_floor: int = 0,
) -> int:
    """Allocate a monotonic number while the caller holds the Session owner lock."""

    highest = int(
        db.scalar(
            select(func.coalesce(func.max(RuntimeGeneration.generation), 0)).where(
                RuntimeGeneration.runtime_session_id == runtime_session_id
            )
        )
        or 0
    )
    return max(highest, managed_generation_floor) + 1


def ensure_runtime_generation(
    db: Session,
    *,
    session: FlowRunRuntime,
    generation: int,
    managed_runtime: ManagedSandbox,
) -> RuntimeGeneration:
    """Bind one physical provider record to one immutable Session generation."""

    owner_type = "FLOW_NODE_ATTEMPT" if session.node_attempt_id else "FLOW_RUN"
    owner_id = session.node_attempt_id or session.flow_run_id
    if (
        managed_runtime.owner_type != owner_type
        or managed_runtime.owner_id != owner_id
        or managed_runtime.runtime_allocation_id != session.workspace_allocation_id
        or managed_runtime.image_reference != session.runtime_image_digest
        or managed_runtime.generation != generation
    ):
        raise DomainError(
            "RUNTIME_GENERATION_SPEC_CONFLICT",
            "The physical Runtime does not match its logical Session generation",
            409,
            {"runtime_session_id": session.id, "managed_runtime_id": managed_runtime.id},
        )
    item = db.scalar(
        select(RuntimeGeneration)
        .where(RuntimeGeneration.managed_runtime_id == managed_runtime.id)
        .with_for_update()
    )
    if item is not None:
        if (
            item.runtime_session_id != session.id
            or item.generation != generation
            or item.runtime_image_digest != session.runtime_image_digest
        ):
            raise DomainError(
                "RUNTIME_GENERATION_SPEC_CONFLICT",
                "The physical Runtime is already bound to another generation",
                409,
                {"managed_runtime_id": managed_runtime.id},
            )
        return item
    collision = db.scalar(
        select(RuntimeGeneration.id).where(
            RuntimeGeneration.runtime_session_id == session.id,
            RuntimeGeneration.generation == generation,
        )
    )
    if collision is not None:
        raise DomainError(
            "RUNTIME_GENERATION_CONFLICT",
            "The Runtime generation number has already been allocated",
            409,
            {"runtime_session_id": session.id, "generation": generation},
        )
    if session.active_generation is not None and generation < session.active_generation:
        raise DomainError(
            "RUNTIME_GENERATION_STALE",
            "A stale Runtime generation cannot be attached to the active Session",
            409,
            {"runtime_session_id": session.id, "generation": generation},
        )
    item = RuntimeGeneration(
        id=uid(),
        runtime_session_id=session.id,
        generation=generation,
        managed_runtime_id=managed_runtime.id,
        runtime_image_digest=session.runtime_image_digest,
        state="PROVISIONING",
        fence_token=uid(),
        row_version=1,
        started_at=datetime.now(UTC),
    )
    db.add(item)
    db.flush()
    return item


def activate_runtime_generation(
    db: Session,
    *,
    session: FlowRunRuntime,
    generation: RuntimeGeneration,
    instance_id: str,
) -> RuntimeSessionFence:
    """CAS-activate an initial generation without allowing two active writers."""

    if (
        session.active_generation == generation.generation
        and session.status == "ACTIVE"
        and generation.state == "READY"
        and generation.instance_id == instance_id
    ):
        return _fence(session, generation)
    if session.active_generation not in {None, generation.generation}:
        raise DomainError(
            "RUNTIME_GENERATION_FENCED",
            "Another Runtime generation is already active",
            409,
            {
                "runtime_session_id": session.id,
                "active_generation": session.active_generation,
            },
        )
    now = datetime.now(UTC)
    expected_generation_version = generation.row_version
    generation_result = db.execute(
        update(RuntimeGeneration)
        .where(
            RuntimeGeneration.id == generation.id,
            RuntimeGeneration.runtime_session_id == session.id,
            RuntimeGeneration.generation == generation.generation,
            RuntimeGeneration.fence_token == generation.fence_token,
            RuntimeGeneration.row_version == expected_generation_version,
            RuntimeGeneration.state.in_(("PROVISIONING", "READY", "STOPPED")),
        )
        .values(
            # READY is a physical lifecycle fact. The Session's
            # active_generation pointer remains the only active-writer truth.
            state="READY",
            instance_id=instance_id,
            ready_at=now,
            stopped_at=None,
            failure_code=None,
            failure_summary=None,
            row_version=RuntimeGeneration.row_version + 1,
            updated_at=now,
        )
    )
    if generation_result.rowcount != 1:
        raise DomainError(
            "RUNTIME_GENERATION_FENCED",
            "The Runtime generation activation command is stale",
            409,
            {"runtime_session_id": session.id, "generation": generation.generation},
        )
    expected_session_version = session.row_version
    session_result = db.execute(
        update(FlowRunRuntime)
        .where(
            FlowRunRuntime.id == session.id,
            FlowRunRuntime.row_version == expected_session_version,
            or_(
                FlowRunRuntime.active_generation.is_(None),
                FlowRunRuntime.active_generation == generation.generation,
            ),
            FlowRunRuntime.status.in_(("STARTING", "STOPPED", "DEGRADED", "RECONNECTING")),
        )
        .values(
            active_generation=generation.generation,
            status="ACTIVE",
            stopped_at=None,
            row_version=FlowRunRuntime.row_version + 1,
            updated_at=now,
        )
    )
    if session_result.rowcount != 1:
        raise DomainError(
            "RUNTIME_SESSION_FENCED",
            "The Runtime Session activation command is stale",
            409,
            {"runtime_session_id": session.id},
        )
    db.flush()
    db.refresh(session)
    db.refresh(generation)
    return _fence(session, generation)


def prepare_runtime_generation(
    db: Session,
    *,
    session: FlowRunRuntime,
    generation: RuntimeGeneration,
    instance_id: str,
    replacement_lease_token: str,
) -> None:
    """Record that N+1 is healthy without making it routable."""

    _require_replacement_lease(session, replacement_lease_token)
    if session.replacement_generation != generation.generation:
        raise DomainError(
            "RUNTIME_REPLACEMENT_TARGET_FENCED",
            "The prepared Runtime is not the Session replacement target",
            409,
            {"runtime_session_id": session.id, "generation": generation.generation},
        )
    if (
        generation.state == "READY"
        and generation.instance_id == instance_id
        and generation.runtime_image_digest == session.runtime_image_digest
    ):
        return
    now = datetime.now(UTC)
    result = db.execute(
        update(RuntimeGeneration)
        .where(
            RuntimeGeneration.id == generation.id,
            RuntimeGeneration.runtime_session_id == session.id,
            RuntimeGeneration.generation == generation.generation,
            RuntimeGeneration.fence_token == generation.fence_token,
            RuntimeGeneration.row_version == generation.row_version,
            RuntimeGeneration.state.in_(("PROVISIONING", "READY", "STOPPED")),
        )
        .values(
            state="READY",
            instance_id=instance_id,
            ready_at=now,
            stopped_at=None,
            failure_code=None,
            failure_summary=None,
            row_version=RuntimeGeneration.row_version + 1,
            updated_at=now,
        )
    )
    if result.rowcount != 1:
        raise DomainError(
            "RUNTIME_GENERATION_FENCED",
            "The Runtime generation prepare command is stale",
            409,
            {"runtime_session_id": session.id, "generation": generation.generation},
        )
    db.flush()
    db.refresh(generation)


def acquire_runtime_replacement_lease(
    db: Session,
    *,
    flow_run_id: str,
    owner: str,
    lease_seconds: int,
) -> tuple[FlowRunRuntime, RuntimeReplacementLease]:
    """Freeze routing and acquire or take over the durable replacement lease."""

    if not owner or len(owner) > 200 or lease_seconds < 30:
        raise ValueError("Invalid Runtime replacement lease")
    now = datetime.now(UTC)
    session = db.scalar(
        select(FlowRunRuntime)
        .where(
            FlowRunRuntime.flow_run_id == flow_run_id,
            FlowRunRuntime.node_attempt_id.is_(None),
        )
        .with_for_update()
    )
    if session is None or session.active_generation is None:
        raise DomainError(
            "RUNTIME_SESSION_NOT_ACTIVE",
            "The FlowRun has no active generation to replace",
            409,
            {"flow_run_id": flow_run_id},
        )
    if session.status not in {"ACTIVE", "REPLACING", "RECONNECTING", "DEGRADED"}:
        raise DomainError(
            "RUNTIME_REPLACEMENT_NOT_ALLOWED",
            "The Runtime Session does not allow replacement",
            409,
            {"runtime_session_id": session.id, "status": session.status},
        )
    if (
        session.replacement_lease_token is not None
        and session.replacement_lease_until is not None
        and session.replacement_lease_until > now
        and session.replacement_lease_owner != owner
    ):
        raise DomainError(
            "RUNTIME_REPLACEMENT_LEASE_HELD",
            "Another worker owns the Runtime replacement lease",
            409,
            {"runtime_session_id": session.id},
        )
    token = (
        session.replacement_lease_token
        if session.replacement_lease_owner == owner and session.replacement_lease_token is not None
        else uid()
    )
    lease_until = now + timedelta(seconds=lease_seconds)
    session.status = "REPLACING"
    session.replacement_lease_token = token
    session.replacement_lease_owner = owner
    session.replacement_lease_until = lease_until
    session.replacement_started_at = session.replacement_started_at or now
    session.replacement_error_code = None
    session.replacement_error_summary = None
    session.row_version += 1
    session.updated_at = now
    db.flush()
    return session, _replacement_lease(session)


def attach_runtime_replacement_generation(
    db: Session,
    *,
    session: FlowRunRuntime,
    generation: RuntimeGeneration,
    replacement_lease_token: str,
) -> RuntimeReplacementLease:
    """Attach the one durable N+1 target, idempotently across worker restarts."""

    _require_replacement_lease(session, replacement_lease_token)
    if generation.runtime_session_id != session.id or generation.generation <= int(
        session.active_generation or 0
    ):
        raise DomainError(
            "RUNTIME_REPLACEMENT_TARGET_INVALID",
            "The replacement generation does not follow the active generation",
            409,
            {"runtime_session_id": session.id, "generation": generation.generation},
        )
    if session.replacement_generation not in {None, generation.generation}:
        raise DomainError(
            "RUNTIME_REPLACEMENT_TARGET_FENCED",
            "The Runtime Session already has another replacement target",
            409,
            {"runtime_session_id": session.id},
        )
    if session.replacement_generation is None:
        session.replacement_generation = generation.generation
        session.row_version += 1
        session.updated_at = datetime.now(UTC)
        db.flush()
    return _replacement_lease(session)


def renew_runtime_replacement_lease(
    db: Session,
    *,
    runtime_session_id: str,
    replacement_lease_token: str,
    owner: str,
    lease_seconds: int,
) -> RuntimeReplacementLease:
    """Renew only the currently owned replacement lease."""

    now = datetime.now(UTC)
    result = db.execute(
        update(FlowRunRuntime)
        .where(
            FlowRunRuntime.id == runtime_session_id,
            FlowRunRuntime.replacement_lease_token == replacement_lease_token,
            FlowRunRuntime.replacement_lease_owner == owner,
            FlowRunRuntime.replacement_lease_until >= now,
            FlowRunRuntime.status.in_(("REPLACING", "RECONNECTING", "DEGRADED")),
        )
        .values(
            replacement_lease_until=now + timedelta(seconds=lease_seconds),
            row_version=FlowRunRuntime.row_version + 1,
            updated_at=now,
        )
    )
    if result.rowcount != 1:
        raise DomainError(
            "RUNTIME_REPLACEMENT_LEASE_LOST",
            "The Runtime replacement lease is stale",
            409,
            {"runtime_session_id": runtime_session_id},
        )
    db.flush()
    session = db.get(FlowRunRuntime, runtime_session_id)
    if session is None:
        raise RuntimeError("Runtime Session disappeared after replacement lease renewal")
    db.refresh(session)
    return _replacement_lease(session)


def mark_runtime_generation_draining(
    db: Session,
    *,
    session: FlowRunRuntime,
    generation: RuntimeGeneration,
    replacement_lease_token: str,
) -> None:
    """Fence the source generation lifecycle before physical drain."""

    _require_replacement_lease(session, replacement_lease_token)
    if generation.generation != session.active_generation:
        raise DomainError(
            "RUNTIME_GENERATION_FENCED",
            "Only the frozen source generation can be drained",
            409,
            {"runtime_session_id": session.id, "generation": generation.generation},
        )
    if generation.state in {"DRAINING", "STOPPED", "DELETED"}:
        return
    _transition_generation(
        db,
        generation,
        from_states=("READY",),
        state="DRAINING",
        draining_at=datetime.now(UTC),
    )


def mark_runtime_generation_stopped(
    db: Session,
    *,
    session: FlowRunRuntime,
    generation: RuntimeGeneration,
    replacement_lease_token: str,
    graceful: bool,
) -> None:
    """Persist that the old writer is physically stopped or absent."""

    _require_replacement_lease(session, replacement_lease_token)
    if generation.generation != session.active_generation:
        raise DomainError(
            "RUNTIME_GENERATION_FENCED",
            "Only the frozen source generation can be stopped",
            409,
            {"runtime_session_id": session.id, "generation": generation.generation},
        )
    if generation.state in {"STOPPED", "DELETED"}:
        return
    now = datetime.now(UTC)
    _transition_generation(
        db,
        generation,
        from_states=("READY", "DRAINING"),
        state="STOPPED",
        stopped_at=now,
    )
    session.replacement_not_before = None if graceful else now + timedelta(seconds=45)
    session.row_version += 1
    session.updated_at = now
    db.flush()


def mark_runtime_generation_deleted(
    db: Session,
    *,
    session: FlowRunRuntime,
    generation: RuntimeGeneration,
    replacement_lease_token: str,
) -> None:
    """Retain the generation audit row after its physical provider is gone."""

    _require_replacement_lease(session, replacement_lease_token)
    if generation.generation != session.active_generation:
        raise DomainError(
            "RUNTIME_GENERATION_FENCED",
            "Only the frozen source generation can be deleted",
            409,
            {"runtime_session_id": session.id, "generation": generation.generation},
        )
    if generation.state == "DELETED":
        return
    _transition_generation(
        db,
        generation,
        from_states=("STOPPED",),
        state="DELETED",
        stopped_at=generation.stopped_at or datetime.now(UTC),
    )
    generation.deleted_at = datetime.now(UTC)
    generation.row_version += 1
    generation.updated_at = datetime.now(UTC)
    db.flush()


def activate_runtime_replacement(
    db: Session,
    *,
    session: FlowRunRuntime,
    source: RuntimeGeneration,
    target: RuntimeGeneration,
    replacement_lease_token: str,
) -> RuntimeSessionFence:
    """CAS-switch routing only after the old writer stopped and N+1 reloaded."""

    _require_replacement_lease(session, replacement_lease_token)
    if (
        session.active_generation != source.generation
        or session.replacement_generation != target.generation
        or source.state not in {"STOPPED", "DELETED"}
        or target.state != "READY"
        or not target.instance_id
    ):
        raise DomainError(
            "RUNTIME_REPLACEMENT_NOT_READY",
            "The Runtime replacement cannot be activated before drain and reload",
            409,
            {"runtime_session_id": session.id},
        )
    now = datetime.now(UTC)
    expected_version = session.row_version
    result = db.execute(
        update(FlowRunRuntime)
        .where(
            FlowRunRuntime.id == session.id,
            FlowRunRuntime.row_version == expected_version,
            FlowRunRuntime.active_generation == source.generation,
            FlowRunRuntime.replacement_generation == target.generation,
            FlowRunRuntime.replacement_lease_token == replacement_lease_token,
            FlowRunRuntime.status.in_(("REPLACING", "RECONNECTING", "DEGRADED")),
        )
        .values(
            active_generation=target.generation,
            replacement_generation=None,
            replacement_lease_token=None,
            replacement_lease_owner=None,
            replacement_lease_until=None,
            replacement_started_at=None,
            replacement_not_before=None,
            replacement_error_code=None,
            replacement_error_summary=None,
            status="ACTIVE",
            row_version=FlowRunRuntime.row_version + 1,
            updated_at=now,
        )
    )
    if result.rowcount != 1:
        raise DomainError(
            "RUNTIME_SESSION_FENCED",
            "The Runtime replacement activation command is stale",
            409,
            {"runtime_session_id": session.id},
        )
    db.flush()
    db.refresh(session)
    db.refresh(target)
    return _fence(session, target)


def record_runtime_replacement_failure(
    db: Session,
    *,
    session: FlowRunRuntime,
    replacement_lease_token: str,
    error_code: str,
    error_summary: str,
    retryable: bool,
) -> None:
    """Keep routing frozen and preserve the one N+1 target for diagnosis/retry."""

    _require_replacement_lease(session, replacement_lease_token, require_current=False)
    now = datetime.now(UTC)
    session.status = "RECONNECTING" if retryable else "DEGRADED"
    session.replacement_error_code = error_code[:100]
    session.replacement_error_summary = error_summary[:2000]
    session.replacement_lease_token = None
    session.replacement_lease_owner = None
    session.replacement_lease_until = None
    session.row_version += 1
    session.updated_at = now
    db.flush()


def fail_runtime_generation(
    db: Session,
    *,
    session: FlowRunRuntime,
    generation: RuntimeGeneration,
    failure_code: str,
    failure_summary: str,
) -> None:
    """Record a redacted provisioning failure behind both row-version fences."""

    now = datetime.now(UTC)
    generation_result = db.execute(
        update(RuntimeGeneration)
        .where(
            RuntimeGeneration.id == generation.id,
            RuntimeGeneration.fence_token == generation.fence_token,
            RuntimeGeneration.row_version == generation.row_version,
            RuntimeGeneration.state.in_(("PROVISIONING", "READY")),
        )
        .values(
            state="FAILED",
            failure_code=failure_code[:100],
            failure_summary=failure_summary[:2000],
            stopped_at=now,
            row_version=RuntimeGeneration.row_version + 1,
            updated_at=now,
        )
    )
    if generation_result.rowcount != 1:
        raise DomainError(
            "RUNTIME_GENERATION_FENCED",
            "The Runtime generation failure command is stale",
            409,
            {"runtime_session_id": session.id, "generation": generation.generation},
        )
    session_result = db.execute(
        update(FlowRunRuntime)
        .where(
            FlowRunRuntime.id == session.id,
            FlowRunRuntime.row_version == session.row_version,
            or_(
                FlowRunRuntime.active_generation.is_(None),
                FlowRunRuntime.active_generation == generation.generation,
            ),
        )
        .values(
            active_generation=None,
            status="DEGRADED",
            row_version=FlowRunRuntime.row_version + 1,
            updated_at=now,
        )
    )
    if session_result.rowcount != 1:
        raise DomainError(
            "RUNTIME_SESSION_FENCED",
            "The Runtime Session failure command is stale",
            409,
            {"runtime_session_id": session.id},
        )
    db.flush()


def assert_active_runtime_fence(
    db: Session,
    *,
    runtime_session_id: str,
    generation: int,
    fence_token: str,
    session_row_version: int,
    generation_row_version: int,
) -> RuntimeSessionFence:
    """Fail closed when a command targets an inactive or superseded generation."""

    match = db.execute(
        select(FlowRunRuntime, RuntimeGeneration)
        .join(
            RuntimeGeneration,
            (RuntimeGeneration.runtime_session_id == FlowRunRuntime.id)
            & (RuntimeGeneration.generation == FlowRunRuntime.active_generation),
        )
        .where(
            FlowRunRuntime.id == runtime_session_id,
            FlowRunRuntime.status == "ACTIVE",
            FlowRunRuntime.active_generation == generation,
            FlowRunRuntime.row_version == session_row_version,
            RuntimeGeneration.fence_token == fence_token,
            RuntimeGeneration.row_version == generation_row_version,
            RuntimeGeneration.state == "READY",
        )
    ).one_or_none()
    if match is None:
        raise DomainError(
            "RUNTIME_COMMAND_FENCED",
            "The Runtime command targets a stale generation",
            409,
            {"runtime_session_id": runtime_session_id, "generation": generation},
        )
    session, item = match
    return _fence(session, item)


def active_flow_run_runtime_connection(db: Session, *, flow_run_id: str) -> ActiveRuntimeConnection:
    """Resolve the only routable Agent Server generation for a FlowRun."""
    owner_id = runtime_owner_flow_run_id(db, flow_run_id)

    match = db.execute(
        select(FlowRunRuntime, RuntimeGeneration, ManagedSandbox)
        .join(
            RuntimeGeneration,
            (RuntimeGeneration.runtime_session_id == FlowRunRuntime.id)
            & (RuntimeGeneration.generation == FlowRunRuntime.active_generation),
        )
        .join(ManagedSandbox, ManagedSandbox.id == RuntimeGeneration.managed_runtime_id)
        .where(
            FlowRunRuntime.flow_run_id == owner_id,
            FlowRunRuntime.node_attempt_id.is_(None),
            FlowRunRuntime.status == "ACTIVE",
            RuntimeGeneration.state == "READY",
            ManagedSandbox.kind == "AGENT_RUNTIME",
            ManagedSandbox.owner_type == "FLOW_RUN",
            ManagedSandbox.owner_id == owner_id,
            ManagedSandbox.desired_state == "RUNNING",
            ManagedSandbox.observed_state == "RUNNING",
        )
    ).one_or_none()
    if match is None:
        raise DomainError(
            "RUNTIME_SESSION_NOT_ACTIVE",
            "The FlowRun has no active Agent Server generation",
            409,
            {"flow_run_id": flow_run_id},
        )
    session, generation, managed_runtime = match
    return ActiveRuntimeConnection(
        runtime_session_id=session.id,
        flow_run_id=session.flow_run_id,
        managed_runtime_id=managed_runtime.id,
        resource_name=managed_runtime.backend_resource_name,
        generation=generation.generation,
        runtime_fence=_fence(session, generation),
    )


def active_node_attempt_runtime_connection(
    db: Session, *, flow_run_id: str, node_attempt_id: str
) -> ActiveRuntimeConnection:
    """Resolve the only routable Agent Server generation for one NodeAttempt."""

    match = db.execute(
        select(FlowRunRuntime, RuntimeGeneration, ManagedSandbox)
        .join(
            RuntimeGeneration,
            (RuntimeGeneration.runtime_session_id == FlowRunRuntime.id)
            & (RuntimeGeneration.generation == FlowRunRuntime.active_generation),
        )
        .join(ManagedSandbox, ManagedSandbox.id == RuntimeGeneration.managed_runtime_id)
        .where(
            FlowRunRuntime.flow_run_id == flow_run_id,
            FlowRunRuntime.node_attempt_id == node_attempt_id,
            FlowRunRuntime.status == "ACTIVE",
            RuntimeGeneration.state == "READY",
            ManagedSandbox.kind == "AGENT_RUNTIME",
            ManagedSandbox.owner_type == "FLOW_NODE_ATTEMPT",
            ManagedSandbox.owner_id == node_attempt_id,
            ManagedSandbox.desired_state == "RUNNING",
            ManagedSandbox.observed_state == "RUNNING",
        )
    ).one_or_none()
    if match is None:
        raise DomainError(
            "RUNTIME_SESSION_NOT_ACTIVE",
            "The Node Attempt has no active Agent Server generation",
            409,
            {"flow_run_id": flow_run_id, "node_attempt_id": node_attempt_id},
        )
    session, generation, managed_runtime = match
    return ActiveRuntimeConnection(
        runtime_session_id=session.id,
        flow_run_id=session.flow_run_id,
        managed_runtime_id=managed_runtime.id,
        resource_name=managed_runtime.backend_resource_name,
        generation=generation.generation,
        runtime_fence=_fence(session, generation),
    )


def active_node_runtime_connection(
    db: Session, *, flow_run_id: str, node_attempt_id: str
) -> ActiveRuntimeConnection:
    """Route new Attempt-owned workspaces and untouched legacy Attempts correctly."""

    context = node_attempt_workspace_context(
        db, flow_run_id=flow_run_id, node_attempt_id=node_attempt_id
    )
    if context.attempt_owned:
        return active_node_attempt_runtime_connection(
            db, flow_run_id=flow_run_id, node_attempt_id=node_attempt_id
        )
    return active_flow_run_runtime_connection(db, flow_run_id=flow_run_id)


def delete_flow_run_runtime_session(db: Session, flow_run_id: str) -> None:
    """Remove logical Runtime records after all physical generations are gone."""

    session = db.scalar(
        select(FlowRunRuntime)
        .where(
            FlowRunRuntime.flow_run_id == flow_run_id,
            FlowRunRuntime.node_attempt_id.is_(None),
        )
        .with_for_update()
    )
    if session is None:
        return
    linked_runtime_id = db.scalar(
        select(RuntimeGeneration.managed_runtime_id)
        .where(
            RuntimeGeneration.runtime_session_id == session.id,
            RuntimeGeneration.managed_runtime_id.is_not(None),
        )
        .limit(1)
    )
    if linked_runtime_id is not None:
        raise DomainError(
            "FLOW_RUN_RUNTIME_DELETE_PROTECTED",
            "The Runtime Session is still referenced by managed compute",
            409,
            {"flow_run_id": flow_run_id, "managed_runtime_id": linked_runtime_id},
        )
    session.active_generation = None
    session.replacement_generation = None
    session.replacement_lease_token = None
    session.replacement_lease_owner = None
    session.replacement_lease_until = None
    session.status = "DELETING"
    session.row_version += 1
    session.updated_at = datetime.now(UTC)
    db.flush()
    db.execute(delete(RuntimeGeneration).where(RuntimeGeneration.runtime_session_id == session.id))
    db.delete(session)
    db.flush()


def _fence(session: FlowRunRuntime, generation: RuntimeGeneration) -> RuntimeSessionFence:
    return RuntimeSessionFence(
        runtime_session_id=session.id,
        generation=generation.generation,
        fence_token=generation.fence_token,
        session_row_version=session.row_version,
        generation_row_version=generation.row_version,
    )


def _replacement_lease(session: FlowRunRuntime) -> RuntimeReplacementLease:
    if (
        session.active_generation is None
        or session.replacement_lease_token is None
        or session.replacement_lease_owner is None
        or session.replacement_lease_until is None
    ):
        raise RuntimeError("Runtime replacement lease is incomplete")
    return RuntimeReplacementLease(
        runtime_session_id=session.id,
        flow_run_id=session.flow_run_id,
        source_generation=session.active_generation,
        target_generation=session.replacement_generation,
        token=session.replacement_lease_token,
        owner=session.replacement_lease_owner,
        lease_until=session.replacement_lease_until,
    )


def _require_replacement_lease(
    session: FlowRunRuntime,
    token: str,
    *,
    require_current: bool = True,
) -> None:
    if (
        session.replacement_lease_token != token
        or session.status not in {"REPLACING", "RECONNECTING", "DEGRADED"}
        or (
            require_current
            and (
                session.replacement_lease_until is None
                or session.replacement_lease_until < datetime.now(UTC)
            )
        )
    ):
        raise DomainError(
            "RUNTIME_REPLACEMENT_LEASE_LOST",
            "The Runtime replacement lease is stale",
            409,
            {"runtime_session_id": session.id},
        )


def _transition_generation(
    db: Session,
    generation: RuntimeGeneration,
    *,
    from_states: tuple[str, ...],
    state: str,
    draining_at: datetime | None = None,
    stopped_at: datetime | None = None,
) -> None:
    result = db.execute(
        update(RuntimeGeneration)
        .where(
            RuntimeGeneration.id == generation.id,
            RuntimeGeneration.fence_token == generation.fence_token,
            RuntimeGeneration.row_version == generation.row_version,
            RuntimeGeneration.state.in_(from_states),
        )
        .values(
            state=state,
            draining_at=draining_at if draining_at is not None else generation.draining_at,
            stopped_at=stopped_at if stopped_at is not None else generation.stopped_at,
            row_version=RuntimeGeneration.row_version + 1,
            updated_at=datetime.now(UTC),
        )
    )
    if result.rowcount != 1:
        raise DomainError(
            "RUNTIME_GENERATION_FENCED",
            "The Runtime generation lifecycle command is stale",
            409,
            {
                "runtime_session_id": generation.runtime_session_id,
                "generation": generation.generation,
            },
        )
    db.flush()
    db.refresh(generation)


__all__ = (
    "ActiveRuntimeConnection",
    "RuntimeReplacementLease",
    "RuntimeSessionFence",
    "acquire_runtime_replacement_lease",
    "active_flow_run_runtime_connection",
    "active_node_attempt_runtime_connection",
    "active_node_runtime_connection",
    "activate_runtime_generation",
    "activate_runtime_replacement",
    "attach_runtime_replacement_generation",
    "assert_active_runtime_fence",
    "delete_flow_run_runtime_session",
    "ensure_flow_run_runtime_session",
    "ensure_node_attempt_runtime_session",
    "ensure_runtime_generation",
    "fail_runtime_generation",
    "mark_runtime_generation_draining",
    "mark_runtime_generation_deleted",
    "mark_runtime_generation_stopped",
    "next_runtime_generation_number",
    "prepare_runtime_generation",
    "record_runtime_replacement_failure",
    "renew_runtime_replacement_lease",
)
