from types import SimpleNamespace

import pytest

from flowweave.modules.gates.public import GateResult
from flowweave.modules.orchestration.application import service as orchestration_service
from flowweave.runtime.base import (
    RuntimeEventBatch,
    RuntimeHandle,
    RuntimeInputReadiness,
    RuntimeResult,
    RuntimeWakeup,
)
from flowweave.runtime.dependencies import runtime_context
from flowweave.shared.errors import DomainError
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
        def read_active_events(self, _handle):
            return RuntimeEventBatch(
                events=(), cursor="native-event", result=RuntimeResult(status="RUNNING")
            )

        def read_events(self, _handle):
            raise AssertionError("execution polling must use the active native HEAD")

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


def test_native_completion_after_runtime_failure_reenters_artifact_projection(monkeypatch):
    """A later FinishAction must not be discarded just because it is already finished."""

    attempt = SimpleNamespace(
        id="attempt-1",
        node_run_id="node-run-1",
        state=AttemptState.END_BLOCKED,
        runtime_phase="FAILED",
        error_code="RUNTIME_FAILED",
        error_detail="interrupted tool",
        conversation_id="conversation-1",
        state_version=7,
    )
    resumed = SimpleNamespace(
        id="attempt-1",
        node_run_id="node-run-1",
        state_version=8,
        output_targets_json={"report": {"artifact_type": "FILE"}},
    )
    run = SimpleNamespace(id="run-1", state="WAITING_HUMAN")
    events: list[tuple[str, dict[str, object]]] = []
    applied: list[tuple[object, RuntimeResult, object]] = []

    class NativeCompletedRuntime:
        def read_active_events(self, _handle):
            return RuntimeEventBatch(
                events=(),
                cursor="finish-2",
                result=RuntimeResult(
                    status="COMPLETED",
                    outputs={"report": ("FILE", "/runtime/workspace/report.md")},
                ),
            )

        def read_events(self, _handle):
            raise AssertionError("execution polling must use the active native HEAD")

        def input_readiness(self, _handle):
            return RuntimeInputReadiness(ready=True, execution_status="finished")

    monkeypatch.setattr(orchestration_service, "_attempt", lambda *_args: attempt)
    monkeypatch.setattr(
        orchestration_service,
        "_active_attempt_runtime_handle",
        lambda *_args: SimpleNamespace(cursor=None),
    )
    monkeypatch.setattr(
        orchestration_service, "_ensure_attempt_runtime_for_native_observation", lambda *_args: None
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
        orchestration_service, "_prepare_runtime_outputs", lambda *_args: ["prepared"]
    )
    monkeypatch.setattr(
        orchestration_service,
        "_apply_runtime_result",
        lambda _db, item, result, **kwargs: applied.append(
            (item, result, kwargs["prepared_outputs"])
        ),
    )

    with runtime_context(NativeCompletedRuntime()):
        orchestration_service.process_poll_runtime(None, "attempt-1", 1, commit=False)

    assert run.state == "ACTIVE"
    assert events == [("ATTEMPT_RESUMED", {"reason": "NATIVE_COMPLETION_AFTER_BLOCKED_PROJECTION"})]
    assert applied == [
        (
            resumed,
            RuntimeResult(
                status="COMPLETED",
                outputs={"report": ("FILE", "/runtime/workspace/report.md")},
                cursor="finish-2",
            ),
            ["prepared"],
        )
    ]


def test_repeated_native_completion_after_gate_block_does_not_replay_outputs(monkeypatch):
    """The same formal terminal event must not become another Artifact version."""

    attempt = SimpleNamespace(
        id="attempt-1",
        state=AttemptState.END_BLOCKED,
        runtime_phase="COMPLETED",
        conversation_id="conversation-1",
        state_version=7,
    )
    claims: list[object] = []
    prepared: list[object] = []

    class RepeatedCompletionRuntime:
        def read_active_events(self, _handle):
            return RuntimeEventBatch(
                events=(),
                cursor="finish-1",
                result=RuntimeResult(
                    status="COMPLETED",
                    outputs={"report": ("FILE", "/runtime/workspace/report.md")},
                ),
            )

        def input_readiness(self, _handle):
            return RuntimeInputReadiness(ready=True, execution_status="finished")

    monkeypatch.setattr(orchestration_service, "_attempt", lambda *_args: attempt)
    monkeypatch.setattr(
        orchestration_service,
        "_active_attempt_runtime_handle",
        lambda *_args: SimpleNamespace(cursor=None),
    )
    monkeypatch.setattr(
        orchestration_service, "_ensure_attempt_runtime_for_native_observation", lambda *_args: None
    )
    monkeypatch.setattr(
        orchestration_service, "_release_worker_read_transaction", lambda *_args: None
    )
    monkeypatch.setattr(orchestration_service, "_require_current_lease", lambda *_args: None)
    monkeypatch.setattr(orchestration_service, "_completion_already_projected", lambda *_args: True)
    monkeypatch.setattr(
        orchestration_service, "_claim_runtime_phase", lambda *_args, **_kwargs: claims.append(True)
    )
    monkeypatch.setattr(
        orchestration_service, "_prepare_runtime_outputs", lambda *_args: prepared.append(True)
    )
    monkeypatch.setattr(
        orchestration_service, "_finish_transaction", lambda *_args, **_kwargs: None
    )

    with runtime_context(RepeatedCompletionRuntime()):
        orchestration_service.process_poll_runtime(None, "attempt-1", 2, commit=False)

    assert claims == []
    assert prepared == []


def test_historical_gate_without_id_is_a_controlled_configuration_error(monkeypatch):
    attempt = SimpleNamespace(
        id="attempt-1",
        node_run_id="node-run-1",
        snapshot_id="snapshot-1",
        gate_policies_json=[{"stage": "END", "position": 0, "enabled": True}],
    )

    monkeypatch.setattr(
        orchestration_service,
        "_node_run",
        lambda *_args: SimpleNamespace(flow_node_snapshot_key="node"),
    )
    monkeypatch.setattr(orchestration_service, "_snapshot", lambda *_args: {})
    monkeypatch.setattr(orchestration_service, "_node", lambda *_args: {})
    monkeypatch.setattr(orchestration_service, "_gate_context", lambda *_args: {})

    with pytest.raises(DomainError) as exc_info:
        orchestration_service._prepare_gate_stage(None, attempt, "END")

    assert exc_info.value.code == "GATE_POLICY_ID_MISSING"


def test_runtime_output_registration_reuses_the_same_formal_completion(monkeypatch):
    payload = SimpleNamespace(field_key="report", artifact_type="URL")
    prepared = SimpleNamespace(
        payload=payload,
        storage_key="artifacts/versions/duplicate",
        content_hash="same-content",
        byte_size=12,
    )
    existing = SimpleNamespace(content_hash="same-content", artifact_type="URL")
    deleted: list[object] = []

    class Db:
        def scalar(self, _statement):
            return existing

    monkeypatch.setattr(
        orchestration_service, "discard_prepared_artifacts", lambda items: deleted.extend(items)
    )

    item = orchestration_service._register_artifact(
        Db(),
        "run-1",
        prepared,
        source="RUNTIME",
        attempt_id="attempt-1",
        runtime_completion_event_id="formal-finish-1",
    )

    assert item is existing
    assert deleted == [prepared]


def test_runtime_output_growth_limit_blocks_before_registering_more_artifacts(monkeypatch):
    attempt = SimpleNamespace(
        id="attempt-1",
        node_run_id="node-run-1",
        state=AttemptState.EXECUTING,
        runtime_phase="RUNNING",
        state_version=7,
    )
    node_run = SimpleNamespace(id="node-run-1", flow_run_id="run-1")
    run = SimpleNamespace(id="run-1", state="ACTIVE")
    prepared = [SimpleNamespace(payload=SimpleNamespace(field_key="report"))]
    events: list[tuple[str, dict[str, object]]] = []
    deleted: list[object] = []

    class Db:
        def scalar(self, _statement):
            return 4

    monkeypatch.setattr(orchestration_service, "_node_run", lambda *_args: node_run)
    monkeypatch.setattr(orchestration_service, "_run", lambda *_args: run)
    monkeypatch.setattr(
        orchestration_service, "discard_prepared_artifacts", lambda items: deleted.extend(items)
    )
    monkeypatch.setattr(
        orchestration_service,
        "_event",
        lambda _db, _run_id, event_type, payload, *_args: events.append((event_type, payload)),
    )

    with settings_context(Settings(runtime_output_version_limit=4)):
        violations = orchestration_service._runtime_output_growth_violations(
            Db(), attempt, prepared
        )

    assert violations == [{"field_key": "report", "existing_versions": 4, "limit": 4}]

    orchestration_service._block_runtime_output_growth(None, attempt, prepared, violations)

    assert deleted == prepared
    assert attempt.state == AttemptState.END_BLOCKED
    assert attempt.runtime_phase == "FAILED"
    assert attempt.error_code == "RUNTIME_OUTPUT_VERSION_LIMIT_EXCEEDED"
    assert attempt.state_version == 8
    assert run.state == "WAITING_HUMAN"
    assert events == [
        (
            "RUNTIME_OUTPUT_VERSION_GROWTH_BLOCKED",
            {"violations": [{"field_key": "report", "existing_versions": 4, "limit": 4}]},
        )
    ]


def test_inspect_completion_does_not_replay_old_outputs_after_gate_block(monkeypatch):
    """An inspect-only terminal snapshot is not a new native completion."""

    attempt = SimpleNamespace(
        id="attempt-1",
        state=AttemptState.END_BLOCKED,
        runtime_phase="COMPLETED",
        conversation_id="conversation-1",
        state_version=7,
    )
    claims: list[object] = []

    class HistoricalCompletionRuntime:
        def read_events(self, _handle):
            return RuntimeEventBatch(events=(), cursor="finish-1", result=None)

        def inspect(self, _handle):
            return RuntimeResult(
                status="COMPLETED",
                outputs={"report": ("FILE", "/runtime/workspace/report.md")},
            )

        def input_readiness(self, _handle):
            return RuntimeInputReadiness(ready=True, execution_status="finished")

    monkeypatch.setattr(orchestration_service, "_attempt", lambda *_args: attempt)
    monkeypatch.setattr(
        orchestration_service,
        "_active_attempt_runtime_handle",
        lambda *_args: RuntimeHandle(job_id="job-1", conversation_id="conversation-1", cursor=None),
    )
    monkeypatch.setattr(
        orchestration_service,
        "_ensure_attempt_runtime_for_native_observation",
        lambda *_args: None,
    )
    monkeypatch.setattr(
        orchestration_service, "_release_worker_read_transaction", lambda *_args: None
    )
    monkeypatch.setattr(orchestration_service, "_require_current_lease", lambda *_args: None)
    monkeypatch.setattr(
        orchestration_service,
        "_claim_runtime_phase",
        lambda *_args, **_kwargs: claims.append(True),
    )
    monkeypatch.setattr(
        orchestration_service, "_finish_transaction", lambda *_args, **_kwargs: None
    )

    with runtime_context(HistoricalCompletionRuntime()):
        orchestration_service.process_poll_runtime(None, "attempt-1", 1, commit=False)

    assert claims == []


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


def test_automatic_repairs_fork_from_the_latest_failed_conversation(monkeypatch):
    """Each repair branches from the prior repair, preserving every history node."""

    attempt = SimpleNamespace(
        id="attempt-1",
        state=AttemptState.END_BLOCKED,
        state_version=8,
        error_code=None,
        conversation_id="original-conversation",
    )
    node_run = SimpleNamespace(id="node-run-1", flow_run_id="run-1")
    run = SimpleNamespace(id="run-1", run_mode="AUTOMATIC", state="ACTIVE")
    source_conversation_ids: list[str] = []
    fork_count = 0

    class NativeRuntime:
        def can_accept_input(self, _handle):
            return True

        def reload_conversation(self, _handle):
            return SimpleNamespace(event_id="completed-event")

    def source_binding(_db, *, openhands_conversation_id, **_kwargs):
        source_conversation_ids.append(openhands_conversation_id)
        return SimpleNamespace(
            id=f"binding-{openhands_conversation_id}",
            openhands_conversation_id=openhands_conversation_id,
        )

    def fork(_db, **_kwargs):
        nonlocal fork_count
        fork_count += 1
        return {
            "id": f"binding-repair-{fork_count}",
            "openhands_conversation_id": f"repair-{fork_count}",
        }

    monkeypatch.setattr(
        orchestration_service.agent_sessions.flow_node_locator,
        "conversation_binding",
        source_binding,
    )
    monkeypatch.setattr(
        orchestration_service,
        "_active_attempt_runtime_handle",
        lambda *_args: SimpleNamespace(),
    )
    monkeypatch.setattr(
        orchestration_service, "_gate_remediation_prompt", lambda *_args: ("请修订输出", ["gate-1"])
    )
    monkeypatch.setattr(
        orchestration_service,
        "_automatic_gate_remediation_round",
        lambda *_args: fork_count + 1,
    )
    monkeypatch.setattr(
        orchestration_service.agent_sessions.flow_node_conversations,
        "fork_node_conversation",
        fork,
    )
    monkeypatch.setattr(
        orchestration_service.agent_sessions.flow_node_conversations,
        "send_node_message",
        lambda *_args, **_kwargs: None,
    )
    monkeypatch.setattr(orchestration_service, "_event", lambda *_args, **_kwargs: None)

    with runtime_context(NativeRuntime()):
        orchestration_service._remediate_gate_failure(
            None,
            attempt,
            node_run,
            run,
            expected_state_version=8,
            idempotency_key="repair-1",
            automatic=True,
        )
        orchestration_service._remediate_gate_failure(
            None,
            attempt,
            node_run,
            run,
            expected_state_version=8,
            idempotency_key="repair-2",
            automatic=True,
        )

    assert source_conversation_ids == ["original-conversation", "repair-1"]
    assert attempt.conversation_id == "repair-2"


def test_gate_remediation_prompt_only_contains_actionable_output_corrections(monkeypatch):
    """A repair Conversation receives missing-output details, not process narration."""

    attempt = SimpleNamespace(
        id="attempt-1", state=AttemptState.END_BLOCKED, snapshot_id="snapshot-1"
    )
    node_run = SimpleNamespace(flow_node_snapshot_key="node-1")
    evaluation = SimpleNamespace(
        id="evaluation-1",
        result_json={
            "summary": "平台门禁未通过",
            "reasons": ["缺少 report.md 文件", "输出类型应为 FILE"],
        },
    )

    class Db:
        def scalar(self, _statement):
            return 1

        def scalars(self, _statement):
            return [evaluation]

    monkeypatch.setattr(orchestration_service, "_snapshot", lambda *_args: {})
    monkeypatch.setattr(
        orchestration_service,
        "_node",
        lambda *_args: {
            "asset": {
                "outputs": [{"field_key": "report", "display_name": "报告", "data_type": "FILE"}]
            }
        },
    )

    prompt, evaluation_ids = orchestration_service._gate_remediation_prompt(Db(), attempt, node_run)

    assert evaluation_ids == ["evaluation-1"]
    assert "缺少 report.md 文件" in prompt
    assert "输出类型应为 FILE" in prompt
    assert "报告（FILE）" in prompt
    assert "平台" not in prompt
    assert "门禁" not in prompt
    assert "FlowWeave" not in prompt
    assert "Fork" not in prompt


def test_third_automatic_end_gate_failure_forks_a_third_repair_conversation(monkeypatch):
    """The third failed gate round is still repaired from its latest Conversation."""

    attempt = SimpleNamespace(id="attempt-1", node_run_id="node-run-1", state_version=8)
    node_run = SimpleNamespace(id="node-run-1", flow_run_id="run-1")
    run = SimpleNamespace(id="run-1", run_mode="AUTOMATIC", state="ACTIVE")
    events: list[tuple[str, dict[str, object]]] = []
    remediation_calls: list[dict[str, object]] = []

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
        lambda *_args, **kwargs: remediation_calls.append(kwargs),
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
    assert run.state == "ACTIVE"
    assert remediation_calls == [
        {
            "expected_state_version": 8,
            "idempotency_key": "automatic-gate-remediation:attempt-1:round3",
            "automatic": True,
        }
    ]
    assert events == [
        ("GATE_STAGE_FINISHED", {"stage": "END", "state": AttemptState.END_BLOCKED}),
    ]


def test_fourth_automatic_end_gate_failure_stops_automation_for_human_override(monkeypatch):
    """After three repair Conversations, the node remains available to a user."""

    attempt = SimpleNamespace(
        id="attempt-1",
        node_run_id="node-run-1",
        state_version=8,
        error_code=None,
        error_detail=None,
    )
    node_run = SimpleNamespace(id="node-run-1", flow_run_id="run-1", state="ACTIVE")
    run = SimpleNamespace(
        id="run-1", run_mode="AUTOMATIC", state="ACTIVE", row_version=4, finished_at=None
    )
    events: list[tuple[str, dict[str, object]]] = []

    class Db:
        def add(self, _item):
            pass

    monkeypatch.setattr(orchestration_service, "_node_run", lambda *_args: node_run)
    monkeypatch.setattr(orchestration_service, "_run", lambda *_args: run)
    monkeypatch.setattr(
        orchestration_service, "_automatic_gate_remediation_round", lambda *_args: 4
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
    assert attempt.error_code == "AUTOMATIC_OUTPUT_REMEDIATION_EXHAUSTED"
    assert attempt.error_detail == "节点输出连续 3 次修订后仍不符合要求，连续运行已停止。"
    assert attempt.state_version == 9
    assert node_run.state == "FAILED"
    assert run.state == "WAITING_HUMAN"
    assert run.row_version == 5
    assert run.finished_at is None
    assert events == [
        ("GATE_STAGE_FINISHED", {"stage": "END", "state": AttemptState.END_BLOCKED}),
        (
            "AUTOMATIC_OUTPUT_REMEDIATION_EXHAUSTED",
            {
                "max_remediation_rounds": 3,
                "error_code": "AUTOMATIC_OUTPUT_REMEDIATION_EXHAUSTED",
            },
        ),
        (
            "AUTOMATIC_OUTPUT_REMEDIATION_REQUIRES_HUMAN",
            {"reason": "AUTOMATIC_OUTPUT_REMEDIATION_EXHAUSTED"},
        ),
    ]


def test_automatic_gate_execution_error_stops_without_output_remediation(monkeypatch):
    """A technical gate error is not evidence that the output contract failed."""

    attempt = SimpleNamespace(id="attempt-1", node_run_id="node-run-1", state_version=8)
    node_run = SimpleNamespace(id="node-run-1", flow_run_id="run-1")
    run = SimpleNamespace(id="run-1", run_mode="AUTOMATIC", state="ACTIVE")
    events: list[tuple[str, dict[str, object]]] = []
    remediation_calls: list[object] = []
    prepared = SimpleNamespace(
        policy={"id": "gate-1", "position": 0},
        execution_no=1,
        plan=SimpleNamespace(sidecar_binding_id=None),
    )
    result = GateResult(
        "ERROR",
        "Gate Agent configuration is unavailable",
        ["Gate Agent configuration is unavailable"],
        [],
        {},
        error_code="GATE_CONFIG_INVALID",
    )

    class Db:
        def add(self, _item):
            pass

    monkeypatch.setattr(orchestration_service, "_node_run", lambda *_args: node_run)
    monkeypatch.setattr(orchestration_service, "_run", lambda *_args: run)
    monkeypatch.setattr(
        orchestration_service,
        "_event",
        lambda _db, _run_id, event_type, payload, *_args: events.append((event_type, payload)),
    )
    monkeypatch.setattr(
        orchestration_service,
        "_remediate_gate_failure",
        lambda *_args, **_kwargs: remediation_calls.append(True),
    )

    orchestration_service._record_gate_results(
        Db(),
        attempt,
        "END",
        {},
        [(prepared, result)],
        AttemptState.END_BLOCKED,
    )

    assert attempt.state == AttemptState.END_BLOCKED
    assert attempt.error_code == "AUTOMATIC_GATE_EXECUTION_FAILED"
    assert attempt.error_detail == "自动完成门禁执行失败，请检查门禁配置或稍后重试。"
    assert attempt.state_version == 9
    assert run.state == "WAITING_HUMAN"
    assert remediation_calls == []
    assert events == [
        ("GATE_STAGE_FINISHED", {"stage": "END", "state": AttemptState.END_BLOCKED}),
        (
            "AUTOMATIC_GATE_EXECUTION_FAILED",
            {"stage": "END", "gate_error_codes": ["GATE_CONFIG_INVALID"]},
        ),
    ]
