from __future__ import annotations

from collections.abc import Callable
from typing import Any, cast

from sqlalchemy.orm import Session

from flowweave.modules.agent_workspaces.application.service import process_agent_workspace_runtime
from flowweave.modules.catalog.public import (
    build_capability_dependencies,
    cleanup_capability_import,
    expire_plugin_source,
    fail_plugin_source_resolution,
    resolve_plugin_source,
)
from flowweave.modules.environments import public as environments
from flowweave.modules.orchestration import public as orchestration
from flowweave.modules.sandboxes import public as sandboxes
from flowweave.modules.tasks.public import Lease, lease_is_current
from flowweave.shared.models import BackgroundTask, TaskState

Handler = Callable[[Session, str, dict[str, Any], Lease], None]


def _readiness(db: Session, aggregate_id: str, _payload: dict[str, Any], _lease: Lease) -> None:
    orchestration.process_readiness(db, aggregate_id, commit=False)


def _gates(db: Session, aggregate_id: str, payload: dict[str, Any], lease: Lease) -> None:
    orchestration.process_gate_stage(db, aggregate_id, str(payload["stage"]), lease, commit=False)


def _start_runtime(db: Session, aggregate_id: str, _payload: dict[str, Any], lease: Lease) -> None:
    orchestration.process_start_runtime(db, aggregate_id, lease, commit=False)


def _provision_flow_run_runtime(
    db: Session, aggregate_id: str, _payload: dict[str, Any], lease: Lease
) -> None:
    orchestration.process_provision_flow_run_runtime(db, aggregate_id, lease, commit=False)


def _provision_agent_workspace_runtime(
    db: Session, aggregate_id: str, _payload: dict[str, Any], _lease: Lease
) -> None:
    process_agent_workspace_runtime(db, aggregate_id)


def _poll_runtime(db: Session, aggregate_id: str, payload: dict[str, Any], lease: Lease) -> None:
    orchestration.process_poll_runtime(
        db,
        aggregate_id,
        int(payload.get("poll_no", 1)),
        lease,
        commit=False,
    )


def _wait_runtime_wakeup(
    db: Session, aggregate_id: str, payload: dict[str, Any], lease: Lease
) -> None:
    orchestration.process_runtime_wakeup(
        db,
        aggregate_id,
        int(payload.get("wakeup_no", 1)),
        lease,
        backoff_no=int(payload.get("backoff_no", 0)),
        commit=False,
    )


def _resume_runtime(db: Session, aggregate_id: str, payload: dict[str, Any], lease: Lease) -> None:
    orchestration.process_resume_runtime(
        db, aggregate_id, str(payload["action_id"]), lease, commit=False
    )


def _respond_runtime_confirmation(
    db: Session, aggregate_id: str, _payload: dict[str, Any], lease: Lease
) -> None:
    orchestration.process_runtime_confirmation(db, aggregate_id, lease, commit=False)


def _cancel_runtime(db: Session, aggregate_id: str, payload: dict[str, Any], lease: Lease) -> None:
    raw_mode = payload.get("recovery_mode")
    raw_sandbox_ids = payload.get("sandbox_ids")
    sandbox_ids = (
        tuple(
            str(item)
            for item in cast(list[object], raw_sandbox_ids)
            if isinstance(item, str) and item
        )
        if isinstance(raw_sandbox_ids, list)
        else ()
    )
    orchestration.process_cancel_runtime(
        db,
        aggregate_id,
        lease,
        recovery_mode=str(raw_mode) if raw_mode is not None else None,
        sandbox_ids=sandbox_ids,
        commit=False,
    )


def _cleanup_setup_container(
    db: Session, aggregate_id: str, payload: dict[str, Any], lease: Lease
) -> None:
    environments.process_cleanup_setup_container(db, aggregate_id, payload, lease, commit=False)


def _cleanup_environment_image(
    db: Session, _aggregate_id: str, payload: dict[str, Any], lease: Lease
) -> None:
    environments.process_cleanup_environment_image(db, payload, lease, commit=False)


def _cleanup_environment_credentials(
    db: Session, aggregate_id: str, _payload: dict[str, Any], lease: Lease
) -> None:
    environments.process_cleanup_environment_credentials(db, aggregate_id, lease, commit=False)


def _replace_flow_run_runtime(
    db: Session, aggregate_id: str, payload: dict[str, Any], lease: Lease
) -> None:
    sandboxes.process_flow_run_runtime_replacement(
        db,
        aggregate_id,
        int(payload["failed_generation"]),
        lease,
        commit=False,
    )


def _cleanup_capability_import(
    db: Session, aggregate_id: str, _payload: dict[str, Any], _lease: Lease
) -> None:
    cleanup_capability_import(db, aggregate_id)


def _build_capability_dependencies(
    db: Session, aggregate_id: str, payload: dict[str, Any], _lease: Lease
) -> None:
    build_capability_dependencies(db, aggregate_id, int(payload["position"]))


def _resolve_plugin_source(
    db: Session, aggregate_id: str, _payload: dict[str, Any], _lease: Lease
) -> None:
    resolve_plugin_source(db, aggregate_id)


def _expire_plugin_source(
    db: Session, aggregate_id: str, _payload: dict[str, Any], _lease: Lease
) -> None:
    expire_plugin_source(db, aggregate_id)


HANDLERS: dict[str, Handler] = {
    "EVALUATE_READINESS": _readiness,
    "RUN_GATE_POLICY": _gates,
    "START_RUNTIME": _start_runtime,
    "PROVISION_FLOW_RUN_RUNTIME": _provision_flow_run_runtime,
    "PROVISION_AGENT_WORKSPACE_RUNTIME": _provision_agent_workspace_runtime,
    "POLL_RUNTIME": _poll_runtime,
    "WAIT_RUNTIME_WAKEUP": _wait_runtime_wakeup,
    "RESUME_RUNTIME": _resume_runtime,
    "RESPOND_RUNTIME_CONFIRMATION": _respond_runtime_confirmation,
    "CANCEL_RUNTIME": _cancel_runtime,
    "CLEANUP_SETUP_CONTAINER": _cleanup_setup_container,
    "CLEANUP_ENVIRONMENT_IMAGE": _cleanup_environment_image,
    "CLEANUP_ENVIRONMENT_CREDENTIALS": _cleanup_environment_credentials,
    "REPLACE_FLOW_RUN_RUNTIME": _replace_flow_run_runtime,
    "CLEANUP_CAPABILITY_IMPORT": _cleanup_capability_import,
    "BUILD_CAPABILITY_DEPENDENCIES": _build_capability_dependencies,
    "RESOLVE_PLUGIN_SOURCE": _resolve_plugin_source,
    "EXPIRE_PLUGIN_SOURCE": _expire_plugin_source,
}


def handle(db: Session, task: BackgroundTask, lease: Lease) -> None:
    if not lease_is_current(db, lease):
        raise RuntimeError("task lease was lost before handler execution")
    handler = HANDLERS.get(task.task_type)
    if handler is None:
        raise ValueError(f"Unknown task type: {task.task_type}")
    handler(db, task.aggregate_id, dict(task.payload_json or {}), lease)


def record_terminal_failure(db: Session, task_id: str, error: str) -> None:
    task = db.get(BackgroundTask, task_id)
    if task is None or task.state != TaskState.DEAD:
        return
    if task.task_type == "CANCEL_RUNTIME":
        orchestration.record_runtime_task_failure(db, task.aggregate_id, error, terminal=True)
    elif task.task_type == "REPLACE_FLOW_RUN_RUNTIME":
        sandboxes.record_terminal_runtime_replacement_failure(db, task.aggregate_id, error)
    elif task.task_type == "RESOLVE_PLUGIN_SOURCE":
        fail_plugin_source_resolution(db, task.aggregate_id, error)
