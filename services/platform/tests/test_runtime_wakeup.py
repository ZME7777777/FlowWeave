from types import SimpleNamespace

from flowweave.modules.orchestration.application import service as orchestration_service
from flowweave.runtime.base import (
    RuntimeEventBatch,
    RuntimeInputReadiness,
    RuntimeResult,
    RuntimeWakeup,
)
from flowweave.runtime.dependencies import runtime_context
from flowweave.shared.models import AttemptState
from flowweave.shared.settings import Settings, settings_context


def test_runtime_wakeup_timeout_enqueues_bounded_rest_reconciliation(monkeypatch):
    """A quiet wake-up channel cannot leave a completed Runtime unobserved."""

    attempt = SimpleNamespace(
        id="attempt-1",
        state=AttemptState.EXECUTING,
        runtime_phase="RUNNING",
        conversation_id="conversation-1",
        state_version=7,
    )
    enqueued: list[dict[str, object]] = []
    ensured: list[str] = []

    class QuietRuntime:
        def wait_for_wakeup(self, *_args, **_kwargs):
            return RuntimeWakeup(channel="CONVERSATION", notified=False)

    def record_enqueue(_db, **kwargs):
        enqueued.append(kwargs)
        return SimpleNamespace(max_attempts=0)

    monkeypatch.setattr(orchestration_service, "_attempt", lambda *_args: attempt)
    monkeypatch.setattr(
        orchestration_service, "_active_attempt_runtime_handle", lambda *_args: SimpleNamespace()
    )
    monkeypatch.setattr(
        orchestration_service,
        "_ensure_attempt_runtime_for_native_observation",
        lambda _db, item: ensured.append(item.id),
    )
    monkeypatch.setattr(
        orchestration_service, "_release_worker_read_transaction", lambda *_args: None
    )
    monkeypatch.setattr(orchestration_service, "_require_current_lease", lambda *_args: None)
    monkeypatch.setattr(orchestration_service, "enqueue", record_enqueue)
    monkeypatch.setattr(
        orchestration_service, "_finish_transaction", lambda *_args, **_kwargs: None
    )

    with settings_context(Settings()), runtime_context(QuietRuntime()):
        orchestration_service.process_runtime_wakeup(
            None, "attempt-1", 3, SimpleNamespace(), commit=False
        )

    poll = next(item for item in enqueued if item["task_type"] == "POLL_RUNTIME")
    assert poll["idempotency_key"] == "poll-runtime-reconcile:attempt-1:v7:3"
    assert poll["payload"] == {"poll_no": 3}
    assert ensured == ["attempt-1"]


def test_native_running_event_recovers_an_end_blocked_native_conversation(monkeypatch):
    """A native continuation, rather than a UI action, restores the projection."""

    attempt = SimpleNamespace(
        id="attempt-1",
        node_run_id="node-run-1",
        state=AttemptState.END_BLOCKED,
        runtime_phase="COMPLETED",
        error_code="END_GATE_DELIVERY_FAILED",
        conversation_id="conversation-1",
        state_version=7,
    )
    resumed = SimpleNamespace(id="attempt-1", node_run_id="node-run-1", state_version=8)
    run = SimpleNamespace(id="run-1", state="WAITING_HUMAN")
    events: list[tuple[str, dict[str, object]]] = []
    wakeups: list[tuple[str, int]] = []
    ensured: list[str] = []

    class NativeRunningRuntime:
        def read_events(self, _handle):
            return RuntimeEventBatch(
                events=(), cursor="native-event", result=RuntimeResult(status="RUNNING")
            )

        def input_readiness(self, _handle):
            return RuntimeInputReadiness(ready=False, execution_status="running")

    monkeypatch.setattr(orchestration_service, "_attempt", lambda *_args: attempt)
    monkeypatch.setattr(
        orchestration_service,
        "_active_attempt_runtime_handle",
        lambda *_args: SimpleNamespace(cursor=None),
    )
    monkeypatch.setattr(
        orchestration_service,
        "_ensure_attempt_runtime_for_native_observation",
        lambda _db, item: ensured.append(item.id),
    )
    monkeypatch.setattr(
        orchestration_service, "_release_worker_read_transaction", lambda *_args: None
    )
    monkeypatch.setattr(orchestration_service, "_require_current_lease", lambda *_args: None)
    monkeypatch.setattr(
        orchestration_service, "_claim_runtime_phase", lambda *_args, **_kwargs: resumed
    )
    monkeypatch.setattr(
        orchestration_service,
        "_node_run",
        lambda *_args: SimpleNamespace(id="node-run-1", flow_run_id="run-1"),
    )
    monkeypatch.setattr(orchestration_service, "_run", lambda *_args: run)
    monkeypatch.setattr(
        orchestration_service,
        "_event",
        lambda _db, _run_id, event_type, payload, *_args: events.append((event_type, payload)),
    )
    monkeypatch.setattr(
        orchestration_service,
        "_dispatch_runtime_wakeup",
        lambda _db, item, wakeup_no: wakeups.append((item.id, wakeup_no)),
    )
    monkeypatch.setattr(
        orchestration_service, "_finish_transaction", lambda *_args, **_kwargs: None
    )

    with runtime_context(NativeRunningRuntime()):
        orchestration_service.process_poll_runtime(None, "attempt-1", 1, commit=False)

    assert run.state == "ACTIVE"
    assert events == [
        ("ATTEMPT_RESUMED", {"reason": "NATIVE_CONVERSATION_EVENT_AFTER_BLOCKED_PROJECTION"})
    ]
    assert wakeups == [("attempt-1", 1)]
    assert ensured == ["attempt-1"]


def test_automatic_end_gate_forks_and_sends_the_latest_gate_report(monkeypatch):
    """A failed automatic END gate repairs on a native child Conversation."""

    attempt = SimpleNamespace(
        id="attempt-1",
        state=AttemptState.END_BLOCKED,
        state_version=8,
        error_code=None,
        conversation_id="source-conversation",
    )
    node_run = SimpleNamespace(id="node-run-1", flow_run_id="run-1")
    run = SimpleNamespace(id="run-1", run_mode="AUTOMATIC", state="ACTIVE")
    source = SimpleNamespace(id="binding-source", openhands_conversation_id="source-conversation")
    sent: list[dict[str, object]] = []
    events: list[tuple[str, dict[str, object]]] = []

    class NativeRuntime:
        def can_accept_input(self, _handle):
            return True

        def reload_conversation(self, _handle):
            return SimpleNamespace(event_id="completed-event")

    monkeypatch.setattr(
        orchestration_service.agent_sessions.flow_node_locator,
        "conversation_binding",
        lambda *_args, **_kwargs: source,
    )
    monkeypatch.setattr(
        orchestration_service,
        "_active_attempt_runtime_handle",
        lambda *_args: SimpleNamespace(),
    )
    monkeypatch.setattr(
        orchestration_service, "_gate_remediation_prompt", lambda *_args: ("门禁结果", ["gate-1"])
    )
    monkeypatch.setattr(
        orchestration_service, "_automatic_gate_remediation_round", lambda *_args: 1
    )
    monkeypatch.setattr(
        orchestration_service.agent_sessions.flow_node_conversations,
        "fork_node_conversation",
        lambda *_args, **kwargs: {
            "id": "binding-target",
            "openhands_conversation_id": "forked-conversation",
            "requested_event_id": kwargs["event_id"],
        },
    )
    monkeypatch.setattr(
        orchestration_service.agent_sessions.flow_node_conversations,
        "send_node_message",
        lambda *_args, **kwargs: sent.append(kwargs),
    )
    monkeypatch.setattr(
        orchestration_service,
        "_event",
        lambda _db, _run_id, event_type, payload, *_args: events.append((event_type, payload)),
    )

    with runtime_context(NativeRuntime()):
        orchestration_service._remediate_gate_failure(
            None,
            attempt,
            node_run,
            run,
            expected_state_version=8,
            idempotency_key="automatic-gate-remediation:attempt-1:round1",
            automatic=True,
        )

    assert attempt.conversation_id == "forked-conversation"
    assert sent == [
        {
            "flow_run_id": "run-1",
            "attempt_id": "attempt-1",
            "binding_id": "binding-target",
            "content": "门禁结果",
        }
    ]
    assert events == [
        (
            "GATE_REMEDIATION_FORKED",
            {
                "automatic": True,
                "source_attempt_id": "attempt-1",
                "source_conversation_id": "source-conversation",
                "target_conversation_id": "forked-conversation",
                "fork_event_id": "completed-event",
                "failed_gate_evaluation_ids": ["gate-1"],
                "failed_gate_round": 1,
            },
        )
    ]


def test_third_automatic_end_gate_failure_waits_for_human(monkeypatch):
    """The third failed gate round is retained, but never auto-messages again."""

    attempt = SimpleNamespace(id="attempt-1", node_run_id="node-run-1", state_version=8)
    node_run = SimpleNamespace(id="node-run-1", flow_run_id="run-1")
    run = SimpleNamespace(id="run-1", run_mode="AUTOMATIC", state="ACTIVE")
    events: list[tuple[str, dict[str, object]]] = []

    class Db:
        def add(self, _item):
            pass

    monkeypatch.setattr(orchestration_service, "_node_run", lambda *_args: node_run)
    monkeypatch.setattr(orchestration_service, "_run", lambda *_args: run)
    monkeypatch.setattr(
        orchestration_service, "_automatic_gate_remediation_round", lambda *_args: 3
    )
    monkeypatch.setattr(
        orchestration_service,
        "_remediate_gate_failure",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("must not auto-remediate")),
    )
    monkeypatch.setattr(
        orchestration_service,
        "_event",
        lambda _db, _run_id, event_type, payload, *_args: events.append((event_type, payload)),
    )

    orchestration_service._record_gate_results(
        Db(),
        attempt,
        "END",
        {},
        [],
        AttemptState.END_BLOCKED,
    )

    assert attempt.state == AttemptState.END_BLOCKED
    assert run.state == "WAITING_HUMAN"
    assert events == [
        ("GATE_STAGE_FINISHED", {"stage": "END", "state": AttemptState.END_BLOCKED}),
        (
            "AUTOMATIC_GATE_REMEDIATION_LIMIT_REACHED",
            {"stage": "END", "max_failed_rounds": 3},
        ),
        (
            "AUTOMATIC_GATE_REVIEW_REQUIRED",
            {"stage": "END", "state": AttemptState.END_BLOCKED},
        ),
    ]
