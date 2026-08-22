from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import UTC, datetime

from sqlalchemy import delete, func, or_, select, update
from sqlalchemy.orm import Session

from flowweave.modules.runs.infrastructure.models import FlowRun
from flowweave.modules.sandboxes.infrastructure.models import (
    FlowRunRuntime,
    FlowRunRuntimeAllocation,
    ManagedSandbox,
    RuntimeGeneration,
)
from flowweave.shared.database import uid
from flowweave.shared.errors import DomainError


@dataclass(frozen=True, slots=True)
class RuntimeSessionFence:
    """Opaque command fence for one active physical generation."""

    runtime_session_id: str
    generation: int
    fence_token: str
    session_row_version: int
    generation_row_version: int


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
    owner_id = db.scalar(
        select(FlowRun.id).where(FlowRun.id == flow_run_id).with_for_update()
    )
    if owner_id is None or workspace_allocation.flow_run_id != flow_run_id:
        raise DomainError(
            "RUNTIME_SESSION_OWNER_INVALID",
            "The Runtime Session does not match its FlowRun allocation",
            409,
            {"flow_run_id": flow_run_id},
        )
    item = db.scalar(
        select(FlowRunRuntime)
        .where(FlowRunRuntime.flow_run_id == flow_run_id)
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
    if item.status in {"STOPPED", "DELETING"}:
        raise DomainError(
            "RUNTIME_SESSION_NOT_ACTIVE",
            "The FlowRun Runtime Session no longer accepts commands",
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

    if (
        managed_runtime.owner_type != "FLOW_RUN"
        or managed_runtime.owner_id != session.flow_run_id
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
            RuntimeGeneration.state.in_(("PROVISIONING", "READY")),
        )
        .values(
            # READY is a physical lifecycle fact. The Session's
            # active_generation pointer remains the only active-writer truth.
            state="READY",
            instance_id=instance_id,
            ready_at=now,
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
            FlowRunRuntime.active_generation.is_(None),
            FlowRunRuntime.status.in_(("STARTING", "DEGRADED", "RECONNECTING")),
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


def delete_flow_run_runtime_session(db: Session, flow_run_id: str) -> None:
    """Remove logical Runtime records after all physical generations are gone."""

    session = db.scalar(
        select(FlowRunRuntime)
        .where(FlowRunRuntime.flow_run_id == flow_run_id)
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
    session.status = "DELETING"
    session.row_version += 1
    session.updated_at = datetime.now(UTC)
    db.flush()
    db.execute(
        delete(RuntimeGeneration).where(
            RuntimeGeneration.runtime_session_id == session.id
        )
    )
    db.delete(session)
    db.flush()


def _fence(
    session: FlowRunRuntime, generation: RuntimeGeneration
) -> RuntimeSessionFence:
    return RuntimeSessionFence(
        runtime_session_id=session.id,
        generation=generation.generation,
        fence_token=generation.fence_token,
        session_row_version=session.row_version,
        generation_row_version=generation.row_version,
    )


__all__ = (
    "RuntimeSessionFence",
    "activate_runtime_generation",
    "assert_active_runtime_fence",
    "delete_flow_run_runtime_session",
    "ensure_flow_run_runtime_session",
    "ensure_runtime_generation",
    "fail_runtime_generation",
    "next_runtime_generation_number",
)
