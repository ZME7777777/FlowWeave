"""Durable delivery of native OpenHands manual condensation requests."""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session

from flowweave.modules.agent_sessions.application.conversation_diagnostics import (
    log_conversation_diagnostic,
)
from flowweave.modules.agent_sessions.application.usage_reconciliation import resolve_handle
from flowweave.modules.tasks.public import Lease, enqueue, lease_is_current
from flowweave.runtime.dependencies import get_runtime
from flowweave.shared.errors import DomainError
from flowweave.shared.models import BackgroundTask, TaskState

_CONDENSATION_TASK_FAILURE_REASONS = frozenset(
    {
        "runtime_rate_limited",
        "runtime_service_unavailable",
        "runtime_auth_failed",
        "runtime_request_rejected",
        "runtime_response_invalid",
        "runtime_timeout_unknown",
        "runtime_connection_unknown",
        "runtime_unavailable_unknown",
        "runtime_unknown",
    }
)


def condensation_task_failure_reason(error: str | None) -> str | None:
    """Return the safe, stable reason persisted for a failed condensation task.

    Background-task errors must never expose Runtime response bodies, provider
    details, URLs, or credentials.  Only explicit FlowWeave error codes are
    projected to the browser.
    """

    if not error:
        return None
    code = error.partition(":")[0]
    if not code.startswith("CONDENSATION_"):
        return None
    reason = code.removeprefix("CONDENSATION_").lower()
    return reason if reason in _CONDENSATION_TASK_FAILURE_REASONS else None


def _condensation_runtime_failure(exc: DomainError) -> DomainError:
    """Map a Runtime transport result to a safe condensation-specific failure."""

    if exc.code == "OPENHANDS_REQUEST_FAILED":
        status_code = exc.details.get("status_code")
        if status_code == 429:
            reason = "RUNTIME_RATE_LIMITED"
        elif status_code in {401, 403}:
            reason = "RUNTIME_AUTH_FAILED"
        elif isinstance(status_code, int) and status_code >= 500:
            reason = "RUNTIME_SERVICE_UNAVAILABLE"
        else:
            reason = "RUNTIME_REQUEST_REJECTED"
        return DomainError(
            f"CONDENSATION_{reason}",
            "OpenHands rejected the context-condensation request",
            exc.status,
            {"outcome_unknown": False},
        )
    if exc.code == "OPENHANDS_RESPONSE_INVALID":
        return DomainError(
            "CONDENSATION_RUNTIME_RESPONSE_INVALID",
            "OpenHands returned an invalid context-condensation response",
            exc.status,
            {"outcome_unknown": False},
        )
    if exc.code == "EXECUTOR_UNAVAILABLE":
        transport_failure = exc.details.get("transport_failure")
        reason = (
            "RUNTIME_TIMEOUT_UNKNOWN"
            if transport_failure == "timeout"
            else "RUNTIME_CONNECTION_UNKNOWN"
            if transport_failure == "connection"
            else "RUNTIME_UNAVAILABLE_UNKNOWN"
        )
        return DomainError(
            f"CONDENSATION_{reason}",
            "The context-condensation request outcome is unknown",
            exc.status,
            {"outcome_unknown": True},
        )
    return DomainError(
        "CONDENSATION_RUNTIME_UNKNOWN",
        "The context-condensation request failed unexpectedly",
        exc.status,
        {"outcome_unknown": bool(exc.details.get("outcome_unknown"))},
    )


def enqueue_manual_condensation(
    db: Session, *, binding_id: str, idempotency_key: str
) -> BackgroundTask:
    """Create or reuse the one active condensation task for a binding."""

    active = db.scalar(
        select(BackgroundTask)
        .where(
            BackgroundTask.task_type == "CONDENSE_AGENT_CONVERSATION",
            BackgroundTask.aggregate_id == binding_id,
            BackgroundTask.state.in_([TaskState.PENDING, TaskState.RUNNING]),
        )
        .order_by(BackgroundTask.created_at.desc())
    )
    if active is not None:
        return active
    task = enqueue(
        db,
        task_type="CONDENSE_AGENT_CONVERSATION",
        aggregate_type="AGENT_CONVERSATION",
        aggregate_id=binding_id,
        idempotency_key=idempotency_key,
    )
    task.max_attempts = 1
    db.flush()
    return task


def process_manual_condensation(
    db: Session, binding_id: str, _payload: dict[str, object], lease: Lease
) -> None:
    """Run one accepted native condensation without retaining a DB transaction."""

    if not lease_is_current(db, lease):
        raise RuntimeError("task lease was lost before manual condensation")
    handle = resolve_handle(db, binding_id)
    db.rollback()
    runtime = get_runtime()
    if not runtime.can_accept_input(handle):
        raise DomainError(
            "AGENT_CONVERSATION_BUSY",
            "请在当前回复完成或暂停后压缩上下文",
            409,
        )
    log_conversation_diagnostic(
        operation="condensation_requested",
        host_kind="agent_workspace"
        if handle.job_id.startswith("agent-workspace:")
        else "flow_node",
        binding_id=binding_id,
        outcome="started",
        force=True,
    )
    try:
        runtime.condense(handle)
    except DomainError as exc:
        classified = _condensation_runtime_failure(exc)
        log_conversation_diagnostic(
            operation="condensation_completed",
            host_kind="agent_workspace"
            if handle.job_id.startswith("agent-workspace:")
            else "flow_node",
            binding_id=binding_id,
            outcome="error",
            error_kind=classified.code,
            force=True,
        )
        raise classified from exc
    log_conversation_diagnostic(
        operation="condensation_completed",
        host_kind="agent_workspace"
        if handle.job_id.startswith("agent-workspace:")
        else "flow_node",
        binding_id=binding_id,
        outcome="accepted",
        force=True,
    )


__all__ = (
    "condensation_task_failure_reason",
    "enqueue_manual_condensation",
    "process_manual_condensation",
)
