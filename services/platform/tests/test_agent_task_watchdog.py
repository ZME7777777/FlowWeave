from __future__ import annotations

from dataclasses import asdict
from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any, cast

import pytest

from flowweave.modules.agent_workspaces.application import task_watchdog
from flowweave.runtime.base import RuntimeConversationIdentity, RuntimeEvent, RuntimeEventBatch


@pytest.fixture(autouse=True)
def database():
    """These watchdog state-machine tests deliberately require no database."""

    yield None


def _task_action() -> RuntimeEvent:
    return RuntimeEvent(
        cursor="task-action-1",
        event_type="TOOL_CALL",
        payload={
            "event_name": "TaskAction",
            "tool_call_id": "task-call-1",
            "runtime_task": {
                "phase": "REQUESTED",
                "action_event_id": "task-action-1",
                "tool_call_id": "task-call-1",
                "subagent_type": "reviewer",
            },
        },
    )


def _task_observation(*, action_id: str = "task-action-1") -> RuntimeEvent:
    return RuntimeEvent(
        cursor="task-observation-1",
        event_type="TOOL_RESULT",
        payload={
            "event_name": "TaskObservation",
            "action_id": action_id,
            "tool_call_id": "task-call-1",
            "runtime_task": {
                "phase": "COMPLETED",
                "action_event_id": action_id,
                "tool_call_id": "task-call-1",
                "subagent_type": "reviewer",
            },
        },
    )


def _task_error(*, tool_call_id: str = "task-call-1") -> RuntimeEvent:
    return RuntimeEvent(
        cursor="task-error-1",
        event_type="ERROR",
        payload={
            "event_name": "AgentErrorEvent",
            "tool_call_id": tool_call_id,
            "content": "Tool call interrupted before completion. The conversation was paused.",
        },
    )


def _payload() -> dict[str, Any]:
    return {
        "action_event_id": "task-action-1",
        "tool_call_id": "task-call-1",
        "identity_digest": "a" * 64,
        "failed_generation": 4,
        "expected_identity": asdict(_identity()),
    }


def _identity() -> RuntimeConversationIdentity:
    return RuntimeConversationIdentity(
        conversation_id="10000000-0000-4000-8000-000000000002",
        workspace_working_dir="/runtime/workspace/project",
        persistence_dir="/runtime/state/conversations/10000000000040008000000000000002",
        event_id="task-error-1",
        parent_id="task-action-1",
        action_id=None,
        tool_call_id="task-call-1",
    )


class _Runtime:
    def __init__(self, events: tuple[RuntimeEvent, ...]) -> None:
        self.events = events
        self.interrupts = 0
        self.runs = 0
        self.reloads: list[RuntimeConversationIdentity | None] = []

    def read_active_events(self, _handle: object) -> RuntimeEventBatch:
        return RuntimeEventBatch(events=self.events)

    def interrupt(self, _handle: object) -> None:
        self.interrupts += 1

    def run(self, _handle: object) -> None:
        self.runs += 1

    def reload_conversation(
        self, _handle: object, *, expected: RuntimeConversationIdentity | None = None
    ) -> RuntimeConversationIdentity:
        self.reloads.append(expected)
        return expected or _identity()


def _target(monkeypatch: Any, runtime: _Runtime, *, generation: int = 4) -> Any:
    binding = SimpleNamespace(last_connected_at=None, updated_at=None)
    monkeypatch.setattr(task_watchdog, "get_runtime", lambda: runtime)
    monkeypatch.setattr(
        task_watchdog.conversations,
        "resolve_task_watchdog_runtime",
        lambda _db, _binding_id: (
            "workspace-1",
            binding,
            object(),
            generation,
            "sandbox-1",
        ),
    )
    return binding


def test_task_outcome_matches_only_formal_action_and_tool_identities() -> None:
    assert (
        task_watchdog.task_outcome(
            (_task_action(), _task_observation(action_id="other-action")),
            action_event_id="task-action-1",
            tool_call_id="task-call-1",
        )
        == "PENDING"
    )
    assert (
        task_watchdog.task_outcome(
            (_task_action(), _task_error(tool_call_id="other-call")),
            action_event_id="task-action-1",
            tool_call_id="task-call-1",
        )
        == "PENDING"
    )
    assert (
        task_watchdog.task_outcome(
            (_task_action(), _task_observation()),
            action_event_id="task-action-1",
            tool_call_id="task-call-1",
        )
        == "OBSERVATION"
    )
    assert (
        task_watchdog.task_outcome(
            (_task_action(), _task_error()),
            action_event_id="task-action-1",
            tool_call_id="task-call-1",
        )
        == "AGENT_ERROR"
    )
    assert (
        task_watchdog.task_outcome(
            (_task_observation(),),
            action_event_id="abandoned-action",
            tool_call_id="abandoned-call",
        )
        == "INACTIVE"
    )
    assert (
        task_watchdog.task_outcome(
            (_task_error(),),
            action_event_id="task-action-1",
            tool_call_id="task-call-1",
        )
        == "INACTIVE"
    )


def test_watchdog_interrupts_pending_task_and_schedules_formal_confirmation(
    monkeypatch: Any,
) -> None:
    runtime = _Runtime((_task_action(),))
    _target(monkeypatch, runtime)
    scheduled: list[dict[str, Any]] = []
    monkeypatch.setattr(
        task_watchdog, "_enqueue_confirmation", lambda _db, **kwargs: scheduled.append(kwargs)
    )

    task_watchdog.process_task_timeout_watchdog(
        cast(Any, object()), "binding-1", _payload(), cast(Any, None)
    )

    assert runtime.interrupts == 1
    assert scheduled == [
        {
            "binding_id": "binding-1",
            "action_event_id": "task-action-1",
            "tool_call_id": "task-call-1",
            "digest": "a" * 64,
            "failed_generation": 4,
            "expected": _identity(),
            "resume_parent": True,
        }
    ]


@pytest.mark.parametrize(
    "state", [task_watchdog.TaskState.PENDING, task_watchdog.TaskState.RUNNING]
)
def test_manual_interrupt_cancels_deadline_and_never_auto_resumes_parent(
    monkeypatch: Any,
    state: task_watchdog.TaskState,
) -> None:
    watchdog = SimpleNamespace(
        task_type=task_watchdog.WATCH_TASK_TYPE,
        state=state,
        lease_owner="worker-1",
        lease_until=datetime.now(UTC),
        payload_json={},
    )
    confirmation = SimpleNamespace(
        task_type=task_watchdog.CONFIRM_TASK_TYPE,
        state=state,
        lease_owner="worker-1",
        lease_until=datetime.now(UTC),
        payload_json={"resume_parent": True},
    )
    resume = SimpleNamespace(
        task_type=task_watchdog.RESUME_TASK_TYPE,
        state=state,
        lease_owner="worker-1",
        lease_until=datetime.now(UTC),
        payload_json={"resume_parent": True},
    )
    db = SimpleNamespace(scalars=lambda _query: (watchdog, confirmation, resume))
    confirmations: list[dict[str, Any]] = []
    monkeypatch.setattr(task_watchdog, "current_user_id", lambda: "user-1")
    monkeypatch.setattr(
        task_watchdog,
        "_enqueue_confirmation",
        lambda _db, **kwargs: confirmations.append(kwargs),
    )

    task_watchdog.prepare_manual_interrupt(
        cast(Any, db),
        SimpleNamespace(id="binding-1"),
        (_task_action(),),
        generation=4,
        expected=_identity(),
    )

    assert watchdog.state == task_watchdog.TaskState.SUCCEEDED
    assert watchdog.lease_owner is None
    assert watchdog.lease_until is None
    assert confirmation.payload_json["resume_parent"] is False
    assert resume.payload_json["resume_parent"] is False
    assert confirmations[0]["resume_parent"] is False
    assert confirmations[0]["expected"] == _identity()


def test_manual_interrupt_fences_claimed_watchdog_before_runtime_action(monkeypatch: Any) -> None:
    runtime = _Runtime((_task_action(),))
    _target(monkeypatch, runtime)
    monkeypatch.setattr(task_watchdog, "lease_is_current", lambda _db, _lease: False)

    task_watchdog.process_task_timeout_watchdog(
        cast(Any, object()),
        "binding-1",
        _payload(),
        cast(Any, SimpleNamespace(task_id="watch-1", owner="worker-1", generation=2)),
    )

    assert runtime.interrupts == 0


def test_manual_confirmation_overrides_existing_auto_resume(monkeypatch: Any) -> None:
    task = SimpleNamespace(
        payload_json={"resume_parent": True},
        max_attempts=3,
    )
    monkeypatch.setattr(task_watchdog, "enqueue", lambda _db, **_kwargs: task)

    task_watchdog._enqueue_confirmation(
        cast(Any, object()),
        binding_id="binding-1",
        action_event_id="task-action-1",
        tool_call_id="task-call-1",
        digest="a" * 64,
        failed_generation=4,
        expected=_identity(),
        resume_parent=False,
    )

    assert task.payload_json["resume_parent"] is False
    assert task.payload_json["expected_identity"] == asdict(_identity())
    assert task.max_attempts == 20


def test_observation_registers_one_deterministic_wall_clock_deadline(monkeypatch: Any) -> None:
    event = _task_action()
    event.payload["timestamp"] = "2026-09-07T10:00:00Z"
    enqueued: list[dict[str, Any]] = []
    task = SimpleNamespace(max_attempts=3)
    monkeypatch.setattr(
        task_watchdog,
        "get_settings",
        lambda: SimpleNamespace(agent_task_timeout_seconds=600),
    )
    monkeypatch.setattr(
        task_watchdog,
        "enqueue",
        lambda _db, **kwargs: (enqueued.append(kwargs), task)[1],
    )

    task_watchdog.observe_task_watchdogs(
        cast(Any, object()), SimpleNamespace(id="binding-1"), (event,)
    )

    assert enqueued[0]["available_at"] == datetime(2026, 9, 7, 10, 10, tzinfo=UTC)
    assert enqueued[0]["idempotency_key"].startswith("agent-task-timeout-watch:")
    assert len(enqueued[0]["idempotency_key"]) < 180
    assert task.max_attempts == 20


def test_natural_completion_wins_interrupt_race_without_replacement(monkeypatch: Any) -> None:
    runtime = _Runtime((_task_action(), _task_observation()))
    _target(monkeypatch, runtime)
    monkeypatch.setattr(
        task_watchdog,
        "_begin_generation_replacement",
        lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("unexpected replacement")),
    )

    task_watchdog.process_task_timeout_confirmation(
        cast(Any, object()), "binding-1", _payload(), cast(Any, None)
    )

    assert runtime.runs == 1


def test_confirmed_timeout_replaces_generation_then_same_id_reload_resumes_parent(
    monkeypatch: Any,
) -> None:
    runtime = _Runtime((_task_action(), _task_error()))
    binding = _target(monkeypatch, runtime)
    lost: list[dict[str, Any]] = []
    resumes: list[dict[str, Any]] = []
    monkeypatch.setattr(
        task_watchdog,
        "mark_agent_workspace_runtime_lost",
        lambda _db, workspace_id, sandbox_id, **kwargs: lost.append(
            {"workspace_id": workspace_id, "sandbox_id": sandbox_id, **kwargs}
        ),
    )
    monkeypatch.setattr(
        task_watchdog,
        "_enqueue_resume",
        lambda _db, **kwargs: resumes.append(kwargs),
    )

    task_watchdog.process_task_timeout_confirmation(
        cast(Any, object()), "binding-1", _payload(), cast(Any, None)
    )

    assert lost == [
        {
            "workspace_id": "workspace-1",
            "sandbox_id": "sandbox-1",
            "failure_code": "AGENT_TASK_TIMEOUT",
            "failure_summary": ("A timed-out native Task is being isolated by Runtime replacement"),
        }
    ]
    assert resumes[0]["failed_generation"] == 4
    assert resumes[0]["resume_parent"] is True

    expected = resumes[0]["expected"]
    resume_payload = {
        **_payload(),
        "expected_identity": asdict(expected),
        "resume_parent": True,
    }
    resumed_binding = _target(monkeypatch, runtime, generation=5)
    task_watchdog.process_task_timeout_resume(
        cast(Any, SimpleNamespace(flush=lambda: None)),
        "binding-1",
        resume_payload,
        cast(Any, None),
    )

    assert runtime.reloads[-1] == expected
    assert runtime.runs == 1
    assert binding.last_connected_at is None
    assert resumed_binding.last_connected_at is not None
