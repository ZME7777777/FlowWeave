"""Durable no-event Conversation response deadline.

OpenHands remains the authoritative owner of Conversation execution. This
module only schedules a bounded observation after a formal user event and, if
that exact event is still the active native leaf, asks OpenHands to pause. It
never manufactures a completion, replays a message, replaces a Runtime, or
automatically resumes a Conversation.
"""

from __future__ import annotations

import time
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from flowweave.modules.agent_sessions.infrastructure.models import AgentConversationBinding
from flowweave.modules.tasks.public import Lease, enqueue
from flowweave.runtime.base import RuntimeEvent, RuntimeHandle
from flowweave.runtime.dependencies import get_runtime
from flowweave.shared.errors import DomainError
from flowweave.shared.models import BackgroundTask, TaskState
from flowweave.shared.settings import get_settings

_RESPONSE_TIMEOUT_TASK = "PAUSE_AGENT_CONVERSATION_ON_TIMEOUT"
_PAUSE_CONFIRM_SECONDS = 5.0
_PAUSE_CONFIRM_INTERVAL_SECONDS = 0.25


def enqueue_response_timeout(
    db: Session, binding: AgentConversationBinding, user_event_id: str | None
) -> None:
    """Schedule one idempotent observation for a delivered user event."""

    if not user_event_id:
        return
    timeout_seconds = get_settings().agent_conversation_response_timeout_seconds
    enqueue(
        db,
        task_type=_RESPONSE_TIMEOUT_TASK,
        aggregate_type="AGENT_CONVERSATION",
        aggregate_id=binding.id,
        idempotency_key=f"pause-agent-conversation-timeout:{binding.id}:{user_event_id}",
        payload={
            "user_event_id": user_event_id,
            "timeout_seconds": timeout_seconds,
            "control_state": "TIMEOUT_WAITING",
        },
        available_at=datetime.now(UTC) + timedelta(seconds=timeout_seconds),
    )


def _mark_timeout_result(db: Session, lease: Lease, state: str) -> None:
    task = db.get(BackgroundTask, lease.task_id)
    if task is None:
        return
    payload = dict(task.payload_json or {})
    payload["control_state"] = state
    payload["updated_at"] = datetime.now(UTC).isoformat()
    task.payload_json = payload
    db.flush()


def _resolve_timeout_handle(
    db: Session, binding_id: str
) -> tuple[AgentConversationBinding, RuntimeHandle]:
    binding = db.scalar(
        select(AgentConversationBinding)
        .where(
            AgentConversationBinding.id == binding_id,
            AgentConversationBinding.lifecycle == "ACTIVE",
        )
        .with_for_update()
    )
    if binding is None:
        raise DomainError("AGENT_CONVERSATION_NOT_FOUND", "会话不存在或已删除", 404)
    if binding.host_kind == "AGENT_WORKSPACE":
        from flowweave.modules.agent_workspaces.application.conversations import (
            resolve_task_watchdog_runtime,
        )

        _workspace_id, resolved, handle, _generation, _runtime_id = resolve_task_watchdog_runtime(
            db, binding.id
        )
        return resolved, handle
    if binding.host_kind == "FLOW_NODE" and binding.flow_run_id:
        from flowweave.modules.agent_sessions.application.flow_node_locator import (
            active_runtime_handle,
        )

        return binding, active_runtime_handle(
            db,
            flow_run_id=binding.flow_run_id,
            openhands_conversation_id=binding.openhands_conversation_id,
            cursor=None,
            route_kind="COLLABORATION",
        )
    raise DomainError("RUNTIME_CONVERSATION_UNBOUND", "会话运行环境不可用", 409)


def _project_node_pause(db: Session, binding: AgentConversationBinding) -> None:
    if binding.host_kind != "FLOW_NODE" or not binding.node_attempt_id:
        return
    from flowweave.modules.agent_sessions.application.flow_node_conversations import (
        record_confirmed_native_pause,
    )

    record_confirmed_native_pause(db, binding=binding)


def process_response_timeout(
    db: Session, binding_id: str, payload: dict[str, Any], lease: Lease
) -> None:
    """Pause only an unchanged, still-running native Conversation."""

    user_event_id = payload.get("user_event_id")
    if not isinstance(user_event_id, str) or not user_event_id:
        raise DomainError("AGENT_RESPONSE_TIMEOUT_INVALID", "会话超时任务缺少用户事件标识", 409)
    binding, handle = _resolve_timeout_handle(db, binding_id)
    runtime = get_runtime()
    if runtime.read_active_events(handle).cursor != user_event_id:
        _mark_timeout_result(db, lease, "TIMEOUT_SUPERSEDED")
        return
    readiness = runtime.input_readiness(handle)
    if readiness.execution_status.lower() not in {"running", "executing"}:
        _mark_timeout_result(db, lease, "TIMEOUT_SUPERSEDED")
        return
    _mark_timeout_result(db, lease, "TIMEOUT_PAUSING")
    runtime.interrupt(handle)
    deadline = time.monotonic() + _PAUSE_CONFIRM_SECONDS
    while time.monotonic() < deadline:
        readiness = runtime.input_readiness(handle)
        if readiness.execution_status.lower() == "paused":
            _project_node_pause(db, binding)
            _mark_timeout_result(db, lease, "TIMEOUT_PAUSED")
            return
        if readiness.execution_status.lower() not in {
            "running",
            "executing",
            "stopping",
            "pausing",
        }:
            _mark_timeout_result(db, lease, "TIMEOUT_SUPERSEDED")
            return
        time.sleep(_PAUSE_CONFIRM_INTERVAL_SECONDS)
    raise DomainError("AGENT_RESPONSE_PAUSE_UNCONFIRMED", "OpenHands 暂停状态未确认", 503)


def task_control_projection(db: Session, binding_id: str) -> list[dict[str, Any]]:
    """Expose a pause reason, never a wall-clock countdown."""

    tasks = db.scalars(
        select(BackgroundTask)
        .where(
            BackgroundTask.aggregate_id == binding_id,
            BackgroundTask.task_type == _RESPONSE_TIMEOUT_TASK,
        )
        .order_by(BackgroundTask.created_at.desc())
    ).all()
    snapshots: list[dict[str, Any]] = []
    for task in tasks:
        payload = dict(task.payload_json or {})
        user_event_id = payload.get("user_event_id")
        if not isinstance(user_event_id, str) or not user_event_id:
            continue
        control_state = str(payload.get("control_state") or "TIMEOUT_WAITING")
        if task.state == TaskState.RUNNING:
            control_state = "TIMEOUT_PAUSING"
        elif task.state == TaskState.DEAD:
            control_state = "TIMEOUT_PAUSE_FAILED"
        snapshots.append(
            {
                "action_event_id": user_event_id,
                "tool_call_id": "",
                "identity_digest": user_event_id,
                "task_type": task.task_type,
                "state": task.state,
                "control_state": control_state,
                "requested_at": task.created_at.isoformat(),
                "deadline_at": task.available_at.isoformat(),
                "updated_at": payload.get("updated_at") or task.updated_at.isoformat(),
                "attempts": task.attempts,
                "last_error": task.last_error,
            }
        )
    return snapshots


def observe_task_watchdogs(
    _db: Session,
    _binding: AgentConversationBinding,
    _events: tuple[RuntimeEvent, ...],
) -> None:
    """Compatibility no-op for the retired Task/child-Agent watchdog."""


def observe_task_watchdogs_from_runtime(
    _db: Session,
    _binding: AgentConversationBinding,
    _handle: RuntimeHandle,
) -> None:
    """Compatibility no-op for the retired Task/child-Agent watchdog."""


__all__ = (
    "enqueue_response_timeout",
    "observe_task_watchdogs",
    "observe_task_watchdogs_from_runtime",
    "process_response_timeout",
    "task_control_projection",
)
