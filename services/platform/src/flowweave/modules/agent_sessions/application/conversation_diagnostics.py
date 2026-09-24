from __future__ import annotations

import json
import logging
import threading
import time
from collections import Counter, OrderedDict
from typing import Any

from flowweave.runtime.base import RuntimeEventBatch, RuntimeInputReadiness, RuntimeResult

_logger = logging.getLogger("flowweave.agent_conversation_diagnostics")
_HEARTBEAT_SECONDS = 60.0
_MAX_TRACKED_SNAPSHOTS = 2048
_ALLOWED_TRIGGERS = frozenset(
    {"scheduled", "foreground", "message_complete", "stream_closed", "stream_reconnect"}
)
_snapshot_lock = threading.Lock()
_snapshots: OrderedDict[tuple[str, str, str, str | None], tuple[str, float]] = OrderedDict()


def _message_source(payload: dict[str, Any]) -> str | None:
    source = payload.get("source")
    return str(source).lower() if source is not None else None


def _snapshot_fields(
    *,
    operation: str,
    host_kind: str,
    binding_id: str,
    batch: RuntimeEventBatch | None,
    readiness: RuntimeInputReadiness | dict[str, Any] | None,
    result: RuntimeResult | None,
    request_cursor: str | None,
    history_cursor: str | None,
    workspace_id: str | None,
    flow_run_id: str | None,
    attempt_id: str | None,
    trigger: str | None,
    outcome: str,
    error_kind: str | None,
) -> dict[str, Any]:
    events = batch.events if batch is not None else ()
    event_types = Counter(event.event_type for event in events)
    user_messages = [
        event.cursor
        for event in events
        if event.event_type == "MESSAGE" and _message_source(event.payload) in {"user", "human"}
    ]
    assistant_messages = [
        event.cursor
        for event in events
        if event.event_type == "MESSAGE"
        and _message_source(event.payload) in {"agent", "assistant", "ai"}
    ]
    effective_readiness = readiness or (batch.readiness if batch is not None else None)
    if isinstance(effective_readiness, RuntimeInputReadiness):
        ready = effective_readiness.ready
        execution_status = effective_readiness.execution_status
    elif effective_readiness is not None:
        ready = effective_readiness.get("ready")
        execution_status = effective_readiness.get("execution_status")
    else:
        ready = None
        execution_status = None
    effective_result = result or (batch.result if batch is not None else None)
    safe_trigger = trigger if trigger in _ALLOWED_TRIGGERS else None
    request_mode = (
        "history" if history_cursor else "incremental" if request_cursor else "latest"
    )
    return {
        "operation": operation,
        "trigger": safe_trigger,
        "outcome": outcome,
        "error_kind": error_kind,
        "host_kind": host_kind,
        "binding_id": str(binding_id),
        "workspace_id": str(workspace_id) if workspace_id is not None else None,
        "flow_run_id": str(flow_run_id) if flow_run_id is not None else None,
        "attempt_id": str(attempt_id) if attempt_id is not None else None,
        "request_mode": request_mode,
        "request_cursor": request_cursor,
        "request_history_cursor": history_cursor,
        "ready": ready,
        "execution_status": execution_status,
        "event_count": len(events),
        "event_types": dict(sorted(event_types.items())),
        "latest_event_id": events[-1].cursor if events else None,
        "latest_event_type": events[-1].event_type if events else None,
        "user_message_count": len(user_messages),
        "latest_user_message_id": user_messages[-1] if user_messages else None,
        "assistant_message_count": len(assistant_messages),
        "latest_assistant_message_id": assistant_messages[-1] if assistant_messages else None,
        "next_cursor": batch.cursor if batch is not None else result.cursor if result else None,
        "history_cursor": batch.history_cursor if batch is not None else None,
        "result_status": effective_result.status if effective_result is not None else None,
        "completion_event_id": (
            effective_result.completion_event_id if effective_result is not None else None
        ),
        "completion_event_kind": (
            effective_result.completion_event_kind if effective_result is not None else None
        ),
    }


def log_conversation_diagnostic(
    *,
    operation: str,
    host_kind: str,
    binding_id: str,
    batch: RuntimeEventBatch | None = None,
    readiness: RuntimeInputReadiness | dict[str, Any] | None = None,
    result: RuntimeResult | None = None,
    request_cursor: str | None = None,
    history_cursor: str | None = None,
    workspace_id: str | None = None,
    flow_run_id: str | None = None,
    attempt_id: str | None = None,
    trigger: str | None = None,
    outcome: str = "ok",
    error_kind: str | None = None,
    force: bool = False,
) -> None:
    """Log only lifecycle metadata needed to diagnose delayed final replies."""

    fields = _snapshot_fields(
        operation=operation,
        host_kind=host_kind,
        binding_id=binding_id,
        batch=batch,
        readiness=readiness,
        result=result,
        request_cursor=request_cursor,
        history_cursor=history_cursor,
        workspace_id=workspace_id,
        flow_run_id=flow_run_id,
        attempt_id=attempt_id,
        trigger=trigger,
        outcome=outcome,
        error_kind=error_kind,
    )
    serialized = json.dumps(fields, ensure_ascii=True, separators=(",", ":"), sort_keys=True)
    key = (host_kind, binding_id, operation, fields["trigger"])
    current_time = time.monotonic()
    with _snapshot_lock:
        previous = _snapshots.get(key)
        if not force and previous is not None:
            previous_snapshot, previous_time = previous
            unchanged = previous_snapshot == serialized
            within_heartbeat = current_time - previous_time < _HEARTBEAT_SECONDS
            if unchanged and within_heartbeat:
                return
        _snapshots[key] = (serialized, current_time)
        _snapshots.move_to_end(key)
        while len(_snapshots) > _MAX_TRACKED_SNAPSHOTS:
            _snapshots.popitem(last=False)
    _logger.info("agent_conversation_diagnostic %s", serialized)
