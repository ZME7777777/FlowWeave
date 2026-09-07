from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

from flowweave.shared.domain.event_monitoring import build_activity_summary


def _event(cursor: str, event_type: str, timestamp: datetime, **payload: object) -> SimpleNamespace:
    return SimpleNamespace(
        cursor=cursor,
        event_type=event_type,
        payload={"timestamp": timestamp.isoformat(), **payload},
    )


def test_monitor_flags_stale_agent_and_active_subagent_without_mutating_events():
    observed_at = datetime(2026, 9, 8, 12, 0, tzinfo=UTC)
    task = {
        "phase": "REQUESTED",
        "action_event_id": "action-1",
        "tool_call_id": "tool-1",
        "subagent_type": "general-purpose",
    }
    events = [
        _event(
            "action-1",
            "TOOL_CALL",
            observed_at - timedelta(seconds=90),
            runtime_task=task,
        )
    ]

    result = build_activity_summary(events, now=observed_at, stale_after_seconds=60)

    assert result["possibly_stuck"] is True
    assert result["last_event_id"] == "action-1"
    assert result["active_subagents"] == [
        {
            "action_event_id": "action-1",
            "tool_call_id": "tool-1",
            "task_id": None,
            "subagent_type": "general-purpose",
            "status": "RUNNING",
            "last_event_id": "action-1",
            "last_event_type": "TOOL_CALL",
            "last_event_at": (observed_at - timedelta(seconds=90)).isoformat(),
            "seconds_since_event": 90,
            "possibly_stuck": True,
        }
    ]


def test_monitor_marks_completed_task_and_terminal_conversation_not_stuck():
    observed_at = datetime(2026, 9, 8, 12, 0, tzinfo=UTC)
    task = {
        "phase": "COMPLETED",
        "action_event_id": "action-1",
        "tool_call_id": "tool-1",
        "task_id": "task-1",
        "subagent_type": "general-purpose",
    }
    result = build_activity_summary(
        [_event("done", "COMPLETED", observed_at - timedelta(seconds=120), runtime_task=task)],
        now=observed_at,
        stale_after_seconds=60,
    )

    assert result["possibly_stuck"] is False
    assert result["active_subagents"] == []
    assert result["subagent_count"] == 1
