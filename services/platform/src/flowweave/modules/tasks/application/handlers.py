from __future__ import annotations

from collections.abc import Callable
from typing import Any

from sqlalchemy.orm import Session

from flowweave.modules.catalog.public import (
    build_capability_dependencies,
    cleanup_capability_import,
)
from flowweave.modules.conversations import public as conversations
from flowweave.modules.orchestration import public as orchestration
from flowweave.modules.tasks.public import Lease, lease_is_current
from flowweave.shared.models import BackgroundTask, TaskState

Handler = Callable[[Session, str, dict[str, Any], Lease], None]


def _readiness(db: Session, aggregate_id: str, _payload: dict[str, Any], _lease: Lease) -> None:
    orchestration.process_readiness(db, aggregate_id, commit=False)


def _gates(db: Session, aggregate_id: str, payload: dict[str, Any], lease: Lease) -> None:
    orchestration.process_gate_stage(db, aggregate_id, str(payload["stage"]), lease, commit=False)


def _start_runtime(db: Session, aggregate_id: str, _payload: dict[str, Any], lease: Lease) -> None:
    orchestration.process_start_runtime(db, aggregate_id, lease, commit=False)


def _poll_runtime(db: Session, aggregate_id: str, payload: dict[str, Any], lease: Lease) -> None:
    orchestration.process_poll_runtime(
        db, aggregate_id, int(payload.get("poll_no", 1)), lease, commit=False
    )


def _resume_runtime(db: Session, aggregate_id: str, payload: dict[str, Any], lease: Lease) -> None:
    orchestration.process_resume_runtime(
        db, aggregate_id, str(payload["action_id"]), lease, commit=False
    )


def _cancel_runtime(db: Session, aggregate_id: str, _payload: dict[str, Any], lease: Lease) -> None:
    orchestration.process_cancel_runtime(db, aggregate_id, lease, commit=False)


def _create_conversation(
    db: Session, aggregate_id: str, _payload: dict[str, Any], lease: Lease
) -> None:
    conversations.process_create_conversation(db, aggregate_id, lease, commit=False)


def _deliver_conversation_message(
    db: Session, aggregate_id: str, _payload: dict[str, Any], lease: Lease
) -> None:
    conversations.process_deliver_message(db, aggregate_id, lease, commit=False)


def _poll_conversation(
    db: Session, aggregate_id: str, payload: dict[str, Any], lease: Lease
) -> None:
    conversations.process_poll_conversation(
        db, aggregate_id, int(payload.get("poll_no", 1)), lease, commit=False
    )


def _cleanup_capability_import(
    db: Session, aggregate_id: str, _payload: dict[str, Any], _lease: Lease
) -> None:
    cleanup_capability_import(db, aggregate_id)


def _build_capability_dependencies(
    db: Session, aggregate_id: str, payload: dict[str, Any], _lease: Lease
) -> None:
    build_capability_dependencies(db, aggregate_id, int(payload["position"]))


HANDLERS: dict[str, Handler] = {
    "EVALUATE_READINESS": _readiness,
    "RUN_GATE_POLICY": _gates,
    "START_RUNTIME": _start_runtime,
    "POLL_RUNTIME": _poll_runtime,
    "RESUME_RUNTIME": _resume_runtime,
    "CANCEL_RUNTIME": _cancel_runtime,
    "CREATE_CONVERSATION": _create_conversation,
    "DELIVER_CONVERSATION_MESSAGE": _deliver_conversation_message,
    "POLL_CONVERSATION": _poll_conversation,
    "CLEANUP_CAPABILITY_IMPORT": _cleanup_capability_import,
    "BUILD_CAPABILITY_DEPENDENCIES": _build_capability_dependencies,
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
