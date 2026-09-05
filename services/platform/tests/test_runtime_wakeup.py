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
