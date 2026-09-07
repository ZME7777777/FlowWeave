"""Read-only activity summaries for OpenHands conversations.

The monitor never changes a Conversation, Attempt, Runtime, or task.  It only
projects formal event identities and wall-clock gaps so a user can decide
whether an Agent or child Task may be stuck.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any


def _timestamp(value: object) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed.replace(tzinfo=UTC) if parsed.tzinfo is None else parsed.astimezone(UTC)


def _event_at(event: Any) -> datetime | None:
    payload = getattr(event, "payload", None)
    return _timestamp(payload.get("timestamp")) if isinstance(payload, dict) else None


def _task_key(task: dict[str, Any]) -> str:
    return str(task.get("action_event_id") or task.get("task_id") or task.get("tool_call_id") or "")


def build_activity_summary(
    events: tuple[Any, ...] | list[Any],
    *,
    now: datetime | None = None,
    stale_after_seconds: int = 60,
) -> dict[str, Any]:
    """Return a bounded, identity-based activity projection for the UI."""

    observed_at = (now or datetime.now(UTC)).astimezone(UTC)
    latest = max(
        ((event, _event_at(event)) for event in events),
        key=lambda item: item[1] or datetime.min.replace(tzinfo=UTC),
        default=(None, None),
    )
    latest_event, latest_at = latest
    subagents: dict[str, dict[str, Any]] = {}
    for event in events:
        payload = getattr(event, "payload", None)
        task = payload.get("runtime_task") if isinstance(payload, dict) else None
        if not isinstance(task, dict):
            continue
        key = _task_key(task)
        if not key:
            continue
        item = subagents.setdefault(
            key,
            {
                "action_event_id": task.get("action_event_id") or key,
                "tool_call_id": task.get("tool_call_id"),
                "task_id": task.get("task_id"),
                "subagent_type": task.get("subagent_type") or "general-purpose",
                "status": "RUNNING",
                "last_event_id": getattr(event, "cursor", None),
                "last_event_type": getattr(event, "event_type", "UNKNOWN"),
                "last_event_at": None,
            },
        )
        event_at = _event_at(event)
        raw_last_event_at = item["last_event_at"]
        last_event_at = (
            _timestamp(raw_last_event_at) if isinstance(raw_last_event_at, str) else None
        )
        if event_at is not None and (last_event_at is None or event_at > last_event_at):
            item["last_event_at"] = event_at.isoformat()
            item["last_event_id"] = getattr(event, "cursor", None)
            item["last_event_type"] = getattr(event, "event_type", "UNKNOWN")
        phase = str(task.get("phase") or "")
        if phase == "COMPLETED":
            item["status"] = "COMPLETED"
        elif phase == "ERROR":
            item["status"] = "ERROR"
        for field in ("task_id", "tool_call_id"):
            if task.get(field):
                item[field] = task[field]

    def age(at: datetime | None) -> int | None:
        return max(0, int((observed_at - at).total_seconds())) if at else None

    latest_age = age(latest_at)
    active = [item for item in subagents.values() if item["status"] == "RUNNING"]
    for item in active:
        item["seconds_since_event"] = age(_timestamp(item["last_event_at"]))
        item["possibly_stuck"] = (
            item["seconds_since_event"] is not None
            and item["seconds_since_event"] >= stale_after_seconds
        )
    terminal = getattr(latest_event, "event_type", None) in {
        "COMPLETED",
        "ERROR",
        "HUMAN_INPUT_REQUIRED",
    }
    return {
        "last_event_id": getattr(latest_event, "cursor", None),
        "last_event_type": getattr(latest_event, "event_type", None),
        "last_event_at": latest_at.isoformat() if latest_at else None,
        "seconds_since_event": latest_age,
        "stale_after_seconds": stale_after_seconds,
        "possibly_stuck": bool(
            not terminal and latest_age is not None and latest_age >= stale_after_seconds
        ),
        "active_subagents": active,
        "subagent_count": len(subagents),
    }


__all__ = ("build_activity_summary",)
