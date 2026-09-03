from types import SimpleNamespace

from flowweave.modules.orchestration.application import service as orchestration_service
from flowweave.runtime.base import RuntimeWakeup
from flowweave.runtime.dependencies import runtime_context
from flowweave.shared.models import AttemptState
from flowweave.shared.settings import Settings, settings_context


def test_runtime_wakeup_timeout_enqueues_bounded_rest_reconciliation(monkeypatch):
    """A quiet wake-up channel cannot leave a completed Runtime unobserved."""

    attempt = SimpleNamespace(
        id="attempt-1", state=AttemptState.EXECUTING, runtime_phase="RUNNING", state_version=7
    )
    enqueued: list[dict[str, object]] = []

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
