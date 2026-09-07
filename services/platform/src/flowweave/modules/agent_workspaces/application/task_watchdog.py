"""Durable wall-clock watchdog for native OpenHands Task executions.

OpenHands 1.44.0 owns Task execution and Conversation history. It exposes no
per-child cancellation or Task wall-clock timeout, so this module only watches
formal event identities, requests the native parent interrupt, and uses the
existing Agent Workspace generation replacement to physically remove a
possibly residual Task worker before resuming the same Conversation.
"""

from __future__ import annotations

import hashlib
from dataclasses import asdict
from datetime import UTC, datetime, timedelta
from typing import Any, Literal, cast

from sqlalchemy import select
from sqlalchemy.orm import Session

from flowweave.modules.agent_sessions.application import conversations
from flowweave.modules.agent_sessions.infrastructure.models import AgentConversationBinding
from flowweave.modules.agent_workspaces.application.service import (
    mark_agent_workspace_runtime_lost,
)
from flowweave.modules.tasks.public import Lease, enqueue, lease_is_current
from flowweave.modules.users.application.security import current_user_id
from flowweave.runtime.base import RuntimeConversationIdentity, RuntimeEvent, RuntimeHandle
from flowweave.runtime.dependencies import get_runtime
from flowweave.shared.database import now
from flowweave.shared.errors import DomainError
from flowweave.shared.models import BackgroundTask, TaskState
from flowweave.shared.settings import get_settings

WATCH_TASK_TYPE = "WATCH_AGENT_TASK_TIMEOUT"
CONFIRM_TASK_TYPE = "CONFIRM_AGENT_TASK_TIMEOUT"
RESUME_TASK_TYPE = "RESUME_AGENT_TASK_TIMEOUT"

TaskOutcome = Literal["PENDING", "INACTIVE", "OBSERVATION", "AGENT_ERROR"]


def _lease_still_owned(db: Session, lease: Lease | None) -> bool:
    """Fence a claimed watchdog after a concurrent manual interrupt."""

    return lease is None or lease_is_current(db, lease)


def _identity_digest(binding_id: str, action_event_id: str, tool_call_id: str) -> str:
    value = f"{binding_id}\0{action_event_id}\0{tool_call_id}".encode()
    return hashlib.sha256(value).hexdigest()


def _task_key(kind: Literal["watch", "confirm", "resume"], digest: str) -> str:
    return f"agent-task-timeout-{kind}:{digest}"


def _requested_at(event: RuntimeEvent) -> datetime:
    raw = event.payload.get("timestamp")
    if isinstance(raw, str) and raw:
        try:
            parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
        except ValueError:
            parsed = None
        if parsed is not None:
            return parsed.replace(tzinfo=UTC) if parsed.tzinfo is None else parsed.astimezone(UTC)
    # Event timestamps are formal OpenHands fields. Historical events that do
    # not carry one get a conservative deadline starting at first observation;
    # their relationship is still derived only from action/tool identities.
    return datetime.now(UTC)


def task_outcome(
    events: tuple[RuntimeEvent, ...], *, action_event_id: str, tool_call_id: str
) -> TaskOutcome:
    """Classify one Task only through formal action/tool identities."""

    matching_action = False
    matching_error = False
    for event in events:
        runtime_task = event.payload.get("runtime_task")
        if isinstance(runtime_task, dict):
            task = cast(dict[str, Any], runtime_task)
            if (
                task.get("phase") == "REQUESTED"
                and task.get("action_event_id") == action_event_id
                and task.get("tool_call_id") == tool_call_id
            ):
                matching_action = True
            if (
                task.get("phase") in {"COMPLETED", "ERROR"}
                and task.get("action_event_id") == action_event_id
                and task.get("tool_call_id") == tool_call_id
            ):
                return "OBSERVATION"
        if (
            event.event_type == "ERROR"
            and event.payload.get("event_name") == "AgentErrorEvent"
            and event.payload.get("tool_call_id") == tool_call_id
        ):
            matching_error = True
    if matching_action and matching_error:
        return "AGENT_ERROR"
    return "PENDING" if matching_action else "INACTIVE"


def observe_task_watchdogs(
    db: Session,
    binding: AgentConversationBinding,
    events: tuple[RuntimeEvent, ...],
) -> None:
    """Schedule one idempotent deadline for every unmatched native TaskAction."""

    timeout_seconds = get_settings().agent_task_timeout_seconds
    for event in events:
        runtime_task = event.payload.get("runtime_task")
        if not isinstance(runtime_task, dict):
            continue
        task = cast(dict[str, Any], runtime_task)
        if task.get("phase") != "REQUESTED":
            continue
        action_event_id = str(task.get("action_event_id") or "")
        tool_call_id = str(task.get("tool_call_id") or "")
        if not action_event_id or not tool_call_id:
            raise DomainError(
                "RUNTIME_EVENT_IDENTITY_INVALID",
                "OpenHands TaskAction is missing a formal event identity",
                502,
            )
        if (
            task_outcome(
                events,
                action_event_id=action_event_id,
                tool_call_id=tool_call_id,
            )
            != "PENDING"
        ):
            continue
        requested_at = _requested_at(event)
        digest = _identity_digest(binding.id, action_event_id, tool_call_id)
        watchdog = enqueue(
            db,
            task_type=WATCH_TASK_TYPE,
            aggregate_type="AGENT_CONVERSATION",
            aggregate_id=binding.id,
            idempotency_key=_task_key("watch", digest),
            payload={
                "action_event_id": action_event_id,
                "tool_call_id": tool_call_id,
                "requested_at": requested_at.isoformat(),
                "timeout_seconds": timeout_seconds,
                "identity_digest": digest,
            },
            available_at=requested_at + timedelta(seconds=timeout_seconds),
        )
        watchdog.max_attempts = max(watchdog.max_attempts, 20)


def observe_task_watchdogs_from_runtime(
    db: Session,
    binding: AgentConversationBinding,
    handle: RuntimeHandle,
) -> None:
    """Observe native Task events immediately after a message is accepted."""

    events = get_runtime().read_active_events(handle).events
    observe_task_watchdogs(db, binding, events)


def _payload_identity(payload: dict[str, Any]) -> tuple[str, str, str]:
    action_event_id = payload.get("action_event_id")
    tool_call_id = payload.get("tool_call_id")
    digest = payload.get("identity_digest")
    values = (action_event_id, tool_call_id, digest)
    if not all(isinstance(value, str) and value for value in values):
        raise DomainError("AGENT_TASK_WATCHDOG_INVALID", "子智能体超时任务身份无效", 409)
    return cast(str, action_event_id), cast(str, tool_call_id), cast(str, digest)


def _enqueue_confirmation(
    db: Session,
    *,
    binding_id: str,
    action_event_id: str,
    tool_call_id: str,
    digest: str,
    failed_generation: int,
    resume_parent: bool,
) -> None:
    task = enqueue(
        db,
        task_type=CONFIRM_TASK_TYPE,
        aggregate_type="AGENT_CONVERSATION",
        aggregate_id=binding_id,
        idempotency_key=_task_key("confirm", digest),
        payload={
            "action_event_id": action_event_id,
            "tool_call_id": tool_call_id,
            "identity_digest": digest,
            "failed_generation": failed_generation,
            "resume_parent": resume_parent,
        },
        available_at=datetime.now(UTC) + timedelta(seconds=1),
    )
    if not resume_parent:
        # A user interrupt wins a race with an automatic timeout confirmation:
        # recovery may hard-stop the Task, but must not restart its parent.
        task.payload_json = {**dict(task.payload_json or {}), "resume_parent": False}
    task.max_attempts = max(task.max_attempts, 20)


def prepare_manual_interrupt(
    db: Session,
    binding: AgentConversationBinding,
    events: tuple[RuntimeEvent, ...],
    *,
    generation: int,
) -> None:
    """Cancel automatic deadlines and isolate manually interrupted Tasks."""

    owner_user_id = current_user_id()
    for event in events:
        runtime_task = event.payload.get("runtime_task")
        if not isinstance(runtime_task, dict):
            continue
        task_projection = cast(dict[str, Any], runtime_task)
        if task_projection.get("phase") != "REQUESTED":
            continue
        action_event_id = str(task_projection.get("action_event_id") or "")
        tool_call_id = str(task_projection.get("tool_call_id") or "")
        if not action_event_id or not tool_call_id:
            continue
        if (
            task_outcome(
                events,
                action_event_id=action_event_id,
                tool_call_id=tool_call_id,
            )
            != "PENDING"
        ):
            continue
        digest = _identity_digest(binding.id, action_event_id, tool_call_id)
        watchdog = db.scalar(
            select(BackgroundTask)
            .where(
                BackgroundTask.owner_user_id == owner_user_id,
                BackgroundTask.idempotency_key == _task_key("watch", digest),
            )
            .with_for_update()
        )
        if watchdog is not None and watchdog.state in {
            TaskState.PENDING,
            TaskState.RETRY,
            TaskState.RUNNING,
        }:
            watchdog.state = TaskState.SUCCEEDED
            watchdog.lease_owner = None
            watchdog.lease_until = None
        _enqueue_confirmation(
            db,
            binding_id=binding.id,
            action_event_id=action_event_id,
            tool_call_id=tool_call_id,
            digest=digest,
            failed_generation=generation,
            resume_parent=False,
        )


def _enqueue_resume(
    db: Session,
    *,
    binding_id: str,
    action_event_id: str,
    tool_call_id: str,
    digest: str,
    failed_generation: int,
    expected: RuntimeConversationIdentity,
    resume_parent: bool,
) -> None:
    task = enqueue(
        db,
        task_type=RESUME_TASK_TYPE,
        aggregate_type="AGENT_CONVERSATION",
        aggregate_id=binding_id,
        idempotency_key=_task_key("resume", digest),
        payload={
            "action_event_id": action_event_id,
            "tool_call_id": tool_call_id,
            "identity_digest": digest,
            "failed_generation": failed_generation,
            "expected_identity": asdict(expected),
            "resume_parent": resume_parent,
        },
        available_at=datetime.now(UTC) + timedelta(seconds=1),
    )
    task.max_attempts = max(task.max_attempts, 20)


def _begin_generation_replacement(
    db: Session,
    *,
    binding_id: str,
    workspace_id: str,
    runtime_handle: RuntimeHandle,
    generation: int,
    sandbox_id: str,
    action_event_id: str,
    tool_call_id: str,
    digest: str,
    resume_parent: bool,
) -> None:
    expected = get_runtime().reload_conversation(runtime_handle)
    mark_agent_workspace_runtime_lost(
        db,
        workspace_id,
        sandbox_id,
        failure_code="AGENT_TASK_TIMEOUT",
        failure_summary="A timed-out native Task is being isolated by Runtime replacement",
    )
    _enqueue_resume(
        db,
        binding_id=binding_id,
        action_event_id=action_event_id,
        tool_call_id=tool_call_id,
        digest=digest,
        failed_generation=generation,
        expected=expected,
        resume_parent=resume_parent,
    )


def process_task_timeout_watchdog(
    db: Session, binding_id: str, payload: dict[str, Any], _lease: Lease
) -> None:
    """Interrupt an overdue Task, or isolate a previously interrupted one."""

    if not _lease_still_owned(db, _lease):
        return
    action_event_id, tool_call_id, digest = _payload_identity(payload)
    workspace_id, _binding, handle, generation, sandbox_id = (
        conversations.resolve_task_watchdog_runtime(db, binding_id)
    )
    events = get_runtime().read_active_events(handle).events
    outcome = task_outcome(events, action_event_id=action_event_id, tool_call_id=tool_call_id)
    if outcome in {"INACTIVE", "OBSERVATION"}:
        return
    if outcome == "AGENT_ERROR":
        _begin_generation_replacement(
            db,
            binding_id=binding_id,
            workspace_id=workspace_id,
            runtime_handle=handle,
            generation=generation,
            sandbox_id=sandbox_id,
            action_event_id=action_event_id,
            tool_call_id=tool_call_id,
            digest=digest,
            resume_parent=True,
        )
        return
    get_runtime().interrupt(handle)
    _enqueue_confirmation(
        db,
        binding_id=binding_id,
        action_event_id=action_event_id,
        tool_call_id=tool_call_id,
        digest=digest,
        failed_generation=generation,
        resume_parent=True,
    )


def process_task_timeout_confirmation(
    db: Session, binding_id: str, payload: dict[str, Any], _lease: Lease
) -> None:
    """Wait for the formal interrupt error before killing the old generation."""

    if not _lease_still_owned(db, _lease):
        return
    action_event_id, tool_call_id, digest = _payload_identity(payload)
    failed_generation = int(payload.get("failed_generation") or 0)
    resume_parent = payload.get("resume_parent") is not False
    workspace_id, _binding, handle, generation, sandbox_id = (
        conversations.resolve_task_watchdog_runtime(db, binding_id)
    )
    if generation < failed_generation:
        raise DomainError(
            "AGENT_TASK_TIMEOUT_GENERATION_DRIFT",
            "子智能体超时恢复期间 Runtime generation 已变化",
            503,
        )
    events = get_runtime().read_active_events(handle).events
    outcome = task_outcome(events, action_event_id=action_event_id, tool_call_id=tool_call_id)
    if outcome in {"INACTIVE", "OBSERVATION"}:
        # Natural completion won the interrupt race. The parent may have been
        # paused by the accepted interrupt, so restart only through native run.
        if resume_parent:
            get_runtime().run(handle)
        return
    if outcome == "PENDING":
        get_runtime().interrupt(handle)
        raise DomainError(
            "AGENT_TASK_INTERRUPT_PENDING",
            "OpenHands 尚未持久化子智能体中断结果",
            503,
        )
    if generation > failed_generation:
        # A concurrent timeout in the shared Agent Workspace already removed
        # this physical Task worker with the old generation.
        if resume_parent:
            get_runtime().run(handle)
        return
    _begin_generation_replacement(
        db,
        binding_id=binding_id,
        workspace_id=workspace_id,
        runtime_handle=handle,
        generation=generation,
        sandbox_id=sandbox_id,
        action_event_id=action_event_id,
        tool_call_id=tool_call_id,
        digest=digest,
        resume_parent=resume_parent,
    )


def _expected_identity(value: object) -> RuntimeConversationIdentity:
    if not isinstance(value, dict):
        raise DomainError("AGENT_TASK_WATCHDOG_INVALID", "子智能体恢复身份无效", 409)
    data = cast(dict[str, object], value)

    def required(field: str) -> str:
        item = data.get(field)
        if not isinstance(item, str) or not item:
            raise DomainError("AGENT_TASK_WATCHDOG_INVALID", "子智能体恢复身份无效", 409)
        return item

    def optional(field: str) -> str | None:
        item = data.get(field)
        if item is None:
            return None
        if not isinstance(item, str) or not item:
            raise DomainError("AGENT_TASK_WATCHDOG_INVALID", "子智能体恢复身份无效", 409)
        return item

    return RuntimeConversationIdentity(
        conversation_id=required("conversation_id"),
        workspace_working_dir=required("workspace_working_dir"),
        persistence_dir=required("persistence_dir"),
        event_id=optional("event_id"),
        parent_id=optional("parent_id"),
        action_id=optional("action_id"),
        tool_call_id=optional("tool_call_id"),
    )


def process_task_timeout_resume(
    db: Session, binding_id: str, payload: dict[str, Any], _lease: Lease
) -> None:
    """Verify same-ID reload on N+1, then let the parent consume Task failure."""

    if not _lease_still_owned(db, _lease):
        return
    action_event_id, tool_call_id, _digest = _payload_identity(payload)
    failed_generation = int(payload.get("failed_generation") or 0)
    expected = _expected_identity(payload.get("expected_identity"))
    _workspace_id, binding, handle, generation, _sandbox_id = (
        conversations.resolve_task_watchdog_runtime(db, binding_id)
    )
    if generation <= failed_generation:
        raise DomainError(
            "AGENT_TASK_TIMEOUT_RECOVERY_PENDING",
            "子智能体超时后的 Runtime 正在恢复",
            503,
        )
    runtime = get_runtime()
    try:
        runtime.reload_conversation(handle, expected=expected)
    except DomainError as exc:
        if exc.code != "RUNTIME_RELOAD_IDENTITY_MISMATCH":
            raise
        # The run request is idempotent at OpenHands' execution-status
        # boundary. If a worker died after that external request, the leaf may
        # already have advanced; retain the formal Task error check below and
        # never create or resend a user message.
    events = runtime.read_active_events(handle).events
    outcome = task_outcome(events, action_event_id=action_event_id, tool_call_id=tool_call_id)
    if outcome in {"INACTIVE", "OBSERVATION"}:
        return
    if outcome != "AGENT_ERROR":
        raise DomainError(
            "AGENT_TASK_TIMEOUT_ERROR_MISSING",
            "原生子智能体失败事件未通过恢复校验",
            409,
        )
    if payload.get("resume_parent") is True:
        runtime.run(handle)
        activity_at = now()
        binding.last_connected_at = activity_at
        binding.updated_at = activity_at
    db.flush()


__all__ = (
    "CONFIRM_TASK_TYPE",
    "RESUME_TASK_TYPE",
    "WATCH_TASK_TYPE",
    "observe_task_watchdogs",
    "observe_task_watchdogs_from_runtime",
    "prepare_manual_interrupt",
    "process_task_timeout_confirmation",
    "process_task_timeout_resume",
    "process_task_timeout_watchdog",
    "task_outcome",
)
