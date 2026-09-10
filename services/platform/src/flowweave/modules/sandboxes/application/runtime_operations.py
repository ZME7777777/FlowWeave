from __future__ import annotations

import re
from pathlib import Path, PurePosixPath
from typing import Any

from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from flowweave.modules.sandboxes.application.runtime_owner import runtime_owner_flow_run_id
from flowweave.modules.sandboxes.application.runtime_replacement import (
    enqueue_flow_run_runtime_replacement,
)
from flowweave.modules.sandboxes.infrastructure.docker import (
    DockerResourceUsage,
    DockerSandboxProvider,
)
from flowweave.modules.sandboxes.infrastructure.models import (
    FlowRunRuntime,
    ManagedSandbox,
    RuntimeGeneration,
)
from flowweave.modules.tasks.public import enqueue
from flowweave.shared.application.transactions import finish
from flowweave.shared.database import now
from flowweave.shared.errors import DomainError, conflict, not_found
from flowweave.shared.models import (
    AttemptState,
    FlowRun,
    NodeAttempt,
    NodeRun,
    RunEvent,
)
from flowweave.shared.settings import get_settings

_FLOW_RUN_ALLOCATION_RELATIVE = re.compile(
    r"\.flow-run-runtimes/[0-9a-f]{32}/"
    r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}"
)


def _retention_policy() -> dict[str, Any]:
    return {
        "mode": "FLOW_RUN_LIFETIME",
        "workspace_preserved_during_replacement": True,
        "physical_delete_operation": "DELETE_FLOW_RUN",
    }


def _host_project_mount_path(spec: dict[str, Any], host_root: Path) -> str | None:
    """Return the exact FlowRun project bind source only for a canonical allocation."""

    relative_value = spec.get("runtime_allocation_relative")
    if not isinstance(relative_value, str) or not _FLOW_RUN_ALLOCATION_RELATIVE.fullmatch(
        relative_value
    ):
        return None
    relative = PurePosixPath(relative_value)
    if not host_root.is_absolute() or relative.is_absolute():
        return None
    return str(host_root.joinpath(*relative.parts, "workspace", "project"))


def _active_resource_summary(
    resource: ManagedSandbox | None,
    generation: RuntimeGeneration | None,
    usage: DockerResourceUsage | None,
) -> dict[str, Any] | None:
    """Return only the active container's safe operational identity and limits."""

    if (
        resource is None
        or generation is None
        or generation.state != "READY"
        or resource.observed_state != "RUNNING"
        or not resource.backend_resource_id
        or usage is None
    ):
        return None
    settings = get_settings()
    spec = resource.spec_json or {}
    host_project_mount_path = _host_project_mount_path(
        spec, Path(settings.runtime_host_workspace_root)
    )
    return {
        "generation": generation.generation,
        "container_id": resource.backend_resource_id.removeprefix("sha256:")[:12],
        "image_reference": resource.image_reference,
        "created_at": resource.created_at.isoformat(),
        "cpu_limit": str(spec.get("cpu_limit") or settings.terminal_environment_cpus),
        "memory_limit": str(spec.get("memory_limit") or settings.terminal_environment_memory),
        "storage_limit": usage.storage_limit,
        "cpu_usage_percent": usage.cpu_usage_percent,
        "memory_usage_bytes": usage.memory_usage_bytes,
        "storage_usage_bytes": usage.storage_usage_bytes,
        "host_project_mount_path": host_project_mount_path,
    }


def runtime_overview(db: Session, flow_run_id: str) -> dict[str, Any]:
    """Return logical Runtime health without exposing a physical connection."""

    run = db.get(FlowRun, flow_run_id)
    if run is None:
        raise not_found("flow_run", flow_run_id)
    owner_id = runtime_owner_flow_run_id(db, flow_run_id)
    session = db.scalar(
        select(FlowRunRuntime).where(
            FlowRunRuntime.flow_run_id == owner_id,
            FlowRunRuntime.node_attempt_id.is_(None),
        )
    )
    if session is None:
        return {
            "flow_run_id": flow_run_id,
            "runtime_session_id": None,
            "status": "ARCHIVED",
            "connection_state": "ARCHIVED",
            "active_generation": None,
            "replacement_generation": None,
            "session_row_version": None,
            "write_available": False,
            "read_only": True,
            "rerun_required": True,
            "diagnostic_code": "LEGACY_RUNTIME_INCOMPATIBLE",
            "diagnostic_summary": (
                "Historical Runtime data is intentionally incompatible; rerun the Flow."
            ),
            "generations": [],
            "retention": _retention_policy(),
        }
    generations = list(
        db.scalars(
            select(RuntimeGeneration)
            .where(RuntimeGeneration.runtime_session_id == session.id)
            .order_by(RuntimeGeneration.generation.desc())
        )
    )
    connection_state = (
        session.status
        if session.status in {"RECONNECTING", "DEGRADED"}
        else "READY"
        if session.status == "ACTIVE"
        else session.status
    )
    return {
        "flow_run_id": flow_run_id,
        "runtime_session_id": session.id,
        "status": session.status,
        "connection_state": connection_state,
        "active_generation": session.active_generation,
        "replacement_generation": session.replacement_generation,
        "session_row_version": session.row_version,
        "write_available": session.status == "ACTIVE",
        "read_only": session.status in {"STOPPED", "DELETING"},
        "rerun_required": False,
        "diagnostic_code": session.replacement_error_code,
        "diagnostic_summary": session.replacement_error_summary,
        "generations": [
            {
                "generation": item.generation,
                "state": item.state,
                "row_version": item.row_version,
                "failure_code": item.failure_code,
                "failure_summary": item.failure_summary,
                "started_at": item.started_at.isoformat() if item.started_at else None,
                "ready_at": item.ready_at.isoformat() if item.ready_at else None,
                "draining_at": item.draining_at.isoformat() if item.draining_at else None,
                "stopped_at": item.stopped_at.isoformat() if item.stopped_at else None,
                "deleted_at": item.deleted_at.isoformat() if item.deleted_at else None,
            }
            for item in generations
        ],
        "retention": _retention_policy(),
    }


def runtime_readiness_by_flow_run(
    db: Session, flow_run_ids: list[str]
) -> dict[str, dict[str, Any]]:
    """Return the list-safe Runtime entry state for known FlowRuns.

    The run list must be able to distinguish a newly persisted FlowRun whose
    single Runtime is still being provisioned from one that is ready to open.
    This deliberately exposes only logical lifecycle data, never a generation
    connection or any physical container detail.
    """

    if not flow_run_ids:
        return {}
    sessions = list(
        db.scalars(
            select(FlowRunRuntime).where(
                FlowRunRuntime.flow_run_id.in_(flow_run_ids),
                FlowRunRuntime.node_attempt_id.is_(None),
            )
        )
    )
    session_ids = [item.id for item in sessions]
    generations = (
        list(
            db.scalars(
                select(RuntimeGeneration).where(
                    RuntimeGeneration.runtime_session_id.in_(session_ids)
                )
            )
        )
        if session_ids
        else []
    )
    active_generations = {
        (item.runtime_session_id, item.generation): item for item in generations
    }
    managed_ids = [item.managed_runtime_id for item in generations if item.managed_runtime_id]
    resources = (
        list(db.scalars(select(ManagedSandbox).where(ManagedSandbox.id.in_(managed_ids))))
        if managed_ids
        else []
    )
    resources_by_id = {item.id: item for item in resources}
    provider = DockerSandboxProvider(get_settings())
    readiness: dict[str, dict[str, Any]] = {}
    for item in sessions:
        active_generation = (
            active_generations.get((item.id, item.active_generation))
            if item.status == "ACTIVE"
            else None
        )
        resource = (
            resources_by_id.get(active_generation.managed_runtime_id)
            if active_generation and active_generation.managed_runtime_id
            else None
        )
        try:
            usage = (
                provider.usage(resource.backend_resource_name, resource.id)
                if resource is not None
                else None
            )
        except DomainError:
            usage = None
        readiness[item.flow_run_id] = {
            "status": item.status,
            "write_available": item.status == "ACTIVE",
            "message": item.replacement_error_summary,
            "updated_at": item.updated_at,
            "resource": _active_resource_summary(resource, active_generation, usage),
        }
    return readiness


def request_runtime_replacement(
    db: Session,
    flow_run_id: str,
    *,
    expected_generation: int,
    expected_session_row_version: int,
) -> dict[str, Any]:
    """Fence writes immediately and enqueue one generation-scoped replacement."""

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
    if (
        session.active_generation != expected_generation
        or session.row_version != expected_session_row_version
    ):
        raise conflict(
            "Runtime Session was modified",
            expected_generation=expected_generation,
            actual_generation=session.active_generation,
            expected_session_row_version=expected_session_row_version,
            actual_session_row_version=session.row_version,
        )
    if session.status not in {"ACTIVE", "DEGRADED"}:
        raise DomainError(
            "RUNTIME_REPLACEMENT_NOT_ALLOWED",
            "The Runtime Session does not allow an operator replacement",
            409,
            {"runtime_session_id": session.id, "status": session.status},
        )
    session.status = "RECONNECTING"
    session.replacement_error_code = None
    session.replacement_error_summary = None
    session.row_version += 1
    session.updated_at = now()
    enqueue_flow_run_runtime_replacement(
        db,
        flow_run_id=flow_run_id,
        failed_generation=expected_generation,
        reason="OPERATOR_REQUEST",
        request_key=f"operator-{expected_session_row_version}",
    )
    finish(db)
    return runtime_overview(db, flow_run_id)


def request_runtime_pause(
    db: Session,
    flow_run_id: str,
    *,
    expected_generation: int,
    expected_session_row_version: int,
) -> dict[str, Any]:
    """Fence a FlowRun and ask the Worker to gracefully stop its container.

    The Workspace and OpenHands persistence stay allocated; only compute is
    reclaimed. A Worker task is used because drain performs Docker I/O.
    """

    owner_id = runtime_owner_flow_run_id(db, flow_run_id)
    session = db.scalar(
        select(FlowRunRuntime)
        .where(
            FlowRunRuntime.flow_run_id == owner_id,
            FlowRunRuntime.node_attempt_id.is_(None),
        )
        .with_for_update()
    )
    if session is None or session.active_generation is None:
        raise DomainError(
            "RUNTIME_SESSION_NOT_ACTIVE",
            "The FlowRun has no active generation to pause",
            409,
            {"flow_run_id": flow_run_id},
        )
    if (
        session.active_generation != expected_generation
        or session.row_version != expected_session_row_version
    ):
        raise conflict(
            "Runtime Session was modified",
            expected_generation=expected_generation,
            actual_generation=session.active_generation,
            expected_session_row_version=expected_session_row_version,
            actual_session_row_version=session.row_version,
        )
    if session.status != "ACTIVE":
        raise DomainError(
            "RUNTIME_PAUSE_NOT_ALLOWED",
            "Only an active Runtime Session can be paused",
            409,
            {"runtime_session_id": session.id, "status": session.status},
        )
    generation = db.scalar(
        select(RuntimeGeneration).where(
            RuntimeGeneration.runtime_session_id == session.id,
            RuntimeGeneration.generation == session.active_generation,
            RuntimeGeneration.state == "READY",
        )
    )
    if generation is None or generation.managed_runtime_id is None:
        raise DomainError(
            "RUNTIME_GENERATION_NOT_ACTIVE",
            "The active Runtime generation has no managed container",
            409,
            {"runtime_session_id": session.id},
        )
    resource = db.scalar(
        select(ManagedSandbox)
        .where(ManagedSandbox.id == generation.managed_runtime_id)
        .with_for_update()
    )
    if resource is None or resource.desired_state != "RUNNING":
        raise DomainError(
            "RUNTIME_PAUSE_NOT_ALLOWED",
            "The active Runtime container is unavailable for pause",
            409,
            {"runtime_session_id": session.id},
        )
    _pause_active_attempts(db, runtime_owner_id=owner_id)
    session.status = "STOPPED"
    session.stopped_at = now()
    session.row_version += 1
    session.updated_at = now()
    resource.desired_state = "STOPPED"
    resource.next_reconcile_at = now()
    enqueue(
        db,
        task_type="PAUSE_FLOW_RUN_RUNTIME",
        aggregate_type="FLOW_RUN",
        aggregate_id=owner_id,
        idempotency_key=f"pause-flow-run-runtime:{owner_id}:{expected_session_row_version}",
        payload={"generation": expected_generation},
    )
    finish(db)
    return runtime_overview(db, flow_run_id)


def _pause_active_attempts(db: Session, *, runtime_owner_id: str) -> None:
    """Persist the business pause before the shared Runtime is drained.

    Nested automatic FlowRuns deliberately share their parent's Runtime, so a
    Runtime pause must cover the owner's unfinished Attempts and every nested
    record that executes inside it. Terminal audit facts remain immutable. The
    Runtime fence established by the caller prevents any later command from
    continuing these Attempts on the old generation.
    """

    attempts = list(
        db.scalars(
            select(NodeAttempt)
            .join(NodeRun, NodeRun.id == NodeAttempt.node_run_id)
            .join(FlowRun, FlowRun.id == NodeRun.flow_run_id)
            .where(
                NodeAttempt.state.not_in(
                    (
                        AttemptState.ACCEPTED,
                        AttemptState.REJECTED,
                        AttemptState.CANCELLED,
                        AttemptState.PAUSED,
                    )
                ),
                or_(
                    FlowRun.id == runtime_owner_id,
                    FlowRun.parent_flow_run_id == runtime_owner_id,
                ),
            )
            .with_for_update()
        )
    )
    for attempt in attempts:
        node_run = db.get(NodeRun, attempt.node_run_id)
        if node_run is None:
            raise DomainError(
                "RUNTIME_ATTEMPT_OWNER_INVALID",
                "An active Attempt has no owning NodeRun",
                409,
                {"attempt_id": attempt.id},
            )
        attempt.state = AttemptState.PAUSED
        attempt.runtime_phase = "PAUSED"
        attempt.state_version += 1
        db.add(
            RunEvent(
                flow_run_id=node_run.flow_run_id,
                node_run_id=node_run.id,
                attempt_id=attempt.id,
                event_type="ATTEMPT_PAUSED",
                payload_json={"reason": "FLOW_RUN_RUNTIME_PAUSED"},
            )
        )


def request_runtime_resume(
    db: Session,
    flow_run_id: str,
    *,
    expected_generation: int,
    expected_session_row_version: int,
) -> dict[str, Any]:
    """Resume a paused FlowRun Runtime using its retained container and state."""

    owner_id = runtime_owner_flow_run_id(db, flow_run_id)
    session = db.scalar(
        select(FlowRunRuntime)
        .where(
            FlowRunRuntime.flow_run_id == owner_id,
            FlowRunRuntime.node_attempt_id.is_(None),
        )
        .with_for_update()
    )
    if session is None or session.active_generation is None:
        raise DomainError(
            "RUNTIME_SESSION_NOT_STOPPED",
            "The FlowRun has no paused Runtime Session to start",
            409,
            {"flow_run_id": flow_run_id},
        )
    if (
        session.active_generation != expected_generation
        or session.row_version != expected_session_row_version
    ):
        raise conflict(
            "Runtime Session was modified",
            expected_generation=expected_generation,
            actual_generation=session.active_generation,
            expected_session_row_version=expected_session_row_version,
            actual_session_row_version=session.row_version,
        )
    if session.status != "STOPPED":
        raise DomainError(
            "RUNTIME_RESUME_NOT_ALLOWED",
            "Only a paused Runtime Session can be started",
            409,
            {"runtime_session_id": session.id, "status": session.status},
        )
    generation = db.scalar(
        select(RuntimeGeneration).where(
            RuntimeGeneration.runtime_session_id == session.id,
            RuntimeGeneration.generation == session.active_generation,
            RuntimeGeneration.state.in_(("READY", "STOPPED")),
        )
    )
    if generation is None or generation.managed_runtime_id is None:
        raise DomainError(
            "RUNTIME_GENERATION_NOT_STOPPED",
            "The paused Runtime generation cannot be resumed",
            409,
            {"runtime_session_id": session.id},
        )
    resource = db.scalar(
        select(ManagedSandbox)
        .where(ManagedSandbox.id == generation.managed_runtime_id)
        .with_for_update()
    )
    if resource is None or resource.desired_state != "STOPPED":
        raise DomainError(
            "RUNTIME_RESUME_NOT_ALLOWED",
            "The paused Runtime container is unavailable for start",
            409,
            {"runtime_session_id": session.id},
        )
    session.status = "STARTING"
    session.stopped_at = None
    session.row_version += 1
    session.updated_at = now()
    resource.desired_state = "RUNNING"
    resource.observed_state = "PENDING"
    resource.next_reconcile_at = now()
    enqueue(
        db,
        task_type="PROVISION_FLOW_RUN_RUNTIME",
        aggregate_type="FLOW_RUN",
        aggregate_id=owner_id,
        idempotency_key=f"resume-flow-run-runtime:{owner_id}:{expected_session_row_version}",
    )
    finish(db)
    return runtime_overview(db, flow_run_id)


__all__ = (
    "request_runtime_pause",
    "request_runtime_replacement",
    "request_runtime_resume",
    "runtime_overview",
    "runtime_readiness_by_flow_run",
)
