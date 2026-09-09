from datetime import UTC, datetime
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
    StartAttemptRequest,
)
from flowweave.runtime.dependencies import runtime_context
from flowweave.shared.errors import DomainError
from flowweave.shared.models import AttemptState
from flowweave.shared.schemas import GateRetryWithProviderWrite
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


def test_gate_sidecar_uses_its_node_attempt_runtime_and_workspace(monkeypatch):
    """Gate Conversations share the owning node Attempt's Runtime and root."""

    attempt = SimpleNamespace(id="attempt-1", snapshot_id="snapshot-1")
    node_run = SimpleNamespace(id="node-run-1", flow_run_id="nested-run-1")
    run = SimpleNamespace(id="nested-run-1", environment_version_id="environment-version-1")
    snapshot = SimpleNamespace(
        environment_version_id="environment-version-1", runtime_manifest_hash="manifest-1"
    )
    environment = SimpleNamespace(
        id="environment-version-1",
        environment_id="environment-1",
        version_no=1,
        image_digest="sha256:image",
    )
    connection = SimpleNamespace(
        runtime_session_id="runtime-session-1",
        managed_runtime_id="managed-runtime-1",
        resource_name="flow-run-runtime-1",
    )
    captured: dict[str, object] = {}

    monkeypatch.setattr(orchestration_service, "_run", lambda *_args: run)
    monkeypatch.setattr(orchestration_service, "_snapshot", lambda *_args: snapshot)
    monkeypatch.setattr(
        orchestration_service, "lock_referenceable_version", lambda *_args: environment
    )
    def node_sidecar_connection(*_args, **kwargs):
        captured["connection_flow_run_id"] = kwargs["flow_run_id"]
        captured["connection_node_attempt_id"] = kwargs["node_attempt_id"]
        return connection

    monkeypatch.setattr(orchestration_service, "_node_sidecar_connection", node_sidecar_connection)
    monkeypatch.setattr(
        orchestration_service.sandboxes,
        "node_attempt_workspace_context",
        lambda *_args, **_kwargs: SimpleNamespace(
            attempt_owned=True,
            runtime_mount_root="/runtime/workspace/node-record",
        ),
    )
    monkeypatch.setattr(
        orchestration_service.agent_sessions,
        "resolve_session_config",
        lambda *_args, **_kwargs: SimpleNamespace(),
    )

    def reserve_binding(*_args, **kwargs):
        captured["binding_working_directory"] = kwargs["working_directory"]
        return SimpleNamespace(
            id="binding-1",
            openhands_conversation_id="conversation-1",
            working_directory=kwargs["working_directory"],
        )

    monkeypatch.setattr(
        orchestration_service.agent_sessions, "reserve_flow_node_binding", reserve_binding
    )
    monkeypatch.setattr(
        orchestration_service.agent_sessions,
        "provider_for_config",
        lambda *_args: SimpleNamespace(),
    )
    monkeypatch.setattr(
        orchestration_service.agent_sessions, "build_agent_spec", lambda *_args, **kwargs: kwargs
    )
    monkeypatch.setattr(
        orchestration_service.sandboxes,
        "node_attempt_capability_path",
        lambda *_args: "/host/node-capabilities",
    )
    monkeypatch.setattr(
        orchestration_service.sandboxes,
        "openhands_flow_run_capability_path",
        lambda *_args: "/runtime/capabilities",
    )

    def build_request(*_args, **kwargs):
        captured["workspace_ref"] = kwargs["workspace_ref"]
        captured["node_attempt_id"] = kwargs["node_attempt_id"]
        return StartAttemptRequest(
            attempt_id="binding-1",
            execution_key="gate-sidecar:attempt-1:gate-1:1",
            node={},
            bindings=[],
            workspace_ref=kwargs["workspace_ref"],
            conversation_id="conversation-1",
        )

    monkeypatch.setattr(orchestration_service, "build_runtime_request", build_request)

    plan = orchestration_service._prepare_gate_plan(
        None,
        attempt=attempt,
        node_run=node_run,
        policy={
            "id": "gate-1",
            "gate_type": "PROMPT",
            "agent_preset": {"model_provider_id": "provider-1", "model_name": "model-1"},
        },
        context={},
        execution_no=1,
    )

    assert captured == {
        "binding_working_directory": "/runtime/workspace/node-record",
        "connection_flow_run_id": "nested-run-1",
        "connection_node_attempt_id": "attempt-1",
        "workspace_ref": "/runtime/workspace/node-record",
        "node_attempt_id": "attempt-1",
    }
    assert plan.sidecar_request is not None
    assert plan.sidecar_request.workspace_root == "/runtime/workspace/node-record"


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


def test_automatic_end_gate_failure_stops_for_human_review_without_fork(monkeypatch):
    """A valid Gate FAIL never silently creates a repair Conversation."""

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
            "AUTOMATIC_GATE_REVIEW_REQUIRED",
            {"stage": "END", "state": AttemptState.END_BLOCKED, "decision": "FAIL"},
        ),
    ]


def test_automatic_gate_pass_dispatches_the_durable_advance(monkeypatch):
    """A successful END gate progresses through the durable worker boundary."""

    attempt = SimpleNamespace(id="attempt-1", node_run_id="node-run-1", state_version=8)
    node_run = SimpleNamespace(id="node-run-1", flow_run_id="run-1")
    run = SimpleNamespace(id="run-1", run_mode="AUTOMATIC", state="ACTIVE")
    advances: list[str] = []
    events: list[tuple[str, dict[str, object]]] = []
    prepared = SimpleNamespace(
        policy={"id": "gate-1", "position": 0},
        execution_no=1,
        plan=SimpleNamespace(sidecar_binding_id=None),
    )
    result = GateResult("PASS", "输出符合要求", [], [], {})

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
        "_dispatch_automatic_advance",
        lambda _db, item: advances.append(item.id),
    )

    orchestration_service._record_gate_results(
        Db(),
        attempt,
        "END",
        {},
        [(prepared, result)],
        AttemptState.WAITING_ACCEPTANCE,
    )

    assert attempt.state == AttemptState.WAITING_ACCEPTANCE
    assert advances == ["attempt-1"]
    assert events == [
        ("GATE_STAGE_FINISHED", {"stage": "END", "state": AttemptState.WAITING_ACCEPTANCE})
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


def test_retry_gate_with_provider_preserves_old_evaluation_and_updates_next_policy(monkeypatch):
    attempt = SimpleNamespace(
        id="attempt-1",
        node_run_id="node-run-1",
        state=AttemptState.END_BLOCKED,
        gate_policies_json=[
            {
                "id": "gate-1",
                "stage": "END",
                "agent_preset": {
                    "model_provider_id": "provider-old",
                    "model_name": "old-model",
                    "reasoning_effort": "low",
                },
            }
        ],
    )
    evaluation = SimpleNamespace(
        id="evaluation-1", attempt_id=attempt.id, decision="ERROR", stage="END",
        policy_snapshot_key="gate-1",
    )
    events: list[tuple[str, dict[str, object]]] = []

    class Db:
        def get(self, _model, value):
            return evaluation if value == evaluation.id else None

    monkeypatch.setattr(orchestration_service, "_attempt", lambda *_args: attempt)
    monkeypatch.setattr(
        orchestration_service,
        "_node_run",
        lambda *_args: SimpleNamespace(id="node-run-1", flow_run_id="run-1"),
    )
    monkeypatch.setattr(orchestration_service, "_run", lambda *_args: SimpleNamespace(id="run-1"))
    monkeypatch.setattr(
        orchestration_service.agent_sessions,
        "resolve_session_config",
        lambda *_args, **_kwargs: SimpleNamespace(
            model_provider_id="provider-new", model_name="new-model", reasoning_effort="high"
        ),
    )
    monkeypatch.setattr(
        orchestration_service,
        "_event",
        lambda _db, _run_id, event_type, payload, *_args: events.append((event_type, payload)),
    )
    monkeypatch.setattr(
        orchestration_service,
        "retry_gates",
        lambda _db, attempt_id, _payload: {"id": attempt_id, "state": "END_GATES"},
    )

    result = orchestration_service.retry_gate_with_provider(
        Db(),
        attempt.id,
        GateRetryWithProviderWrite(
            expected_state_version=3,
            evaluation_id=evaluation.id,
            agent_preset={
                "model_provider_id": "provider-new",
                "model_name": "new-model",
                "reasoning_effort": "high",
            },
            prompt="修订后的判定提示词",
            code="assert True",
        ),
    )

    assert result == {"id": attempt.id, "state": "END_GATES"}
    assert attempt.gate_policies_json[0]["agent_preset"] == {
        "model_provider_id": "provider-new",
        "model_name": "new-model",
        "reasoning_effort": "high",
    }
    assert attempt.gate_policies_json[0]["config"] == {
        "prompt": "修订后的判定提示词", "code": "assert True"
    }
    assert events == [
        (
            "GATE_RETRY_CONFIGURED",
            {
                "evaluation_id": "evaluation-1",
                "stage": "END",
                "policy_snapshot_key": "gate-1",
                "previous_model_provider_id": "provider-old",
                "previous_model_name": "old-model",
                "model_provider_id": "provider-new",
                "model_name": "new-model",
                "reasoning_effort": "high",
                "prompt_changed": True,
                "script_changed": True,
            },
        )
    ]


def test_automatic_run_summary_never_contains_execution_history():
    """The polling DTO stays bounded as a run's Artifacts and attempts grow."""

    run = SimpleNamespace(
        id="automatic-1",
        parent_flow_run_id="parent-1",
        run_no=3,
        name="每日检查",
        state="ACTIVE",
        row_version=6,
        schedule_id="schedule-1",
        schedule_occurrence_id="occurrence-1",
        started_at=datetime(2026, 9, 8, 9, 0, tzinfo=UTC),
        finished_at=None,
        automation_plan_json={
            "start_node_key": "collect",
            "reachable_node_keys": ["collect", "review"],
            "node_plans": {"collect": {}, "review": {}},
            "readiness": {"ready": False, "issues": [{"code": "INPUT_MISSING"}]},
        },
    )
    schedule = SimpleNamespace(name="工作日 09:00")

    summary = orchestration_service._automatic_run_summary(
        run,
        schedule,
        {"ACCEPTED": 1, "FAILED": 1, "ACTIVE": 2},
    )

    assert summary["plan"] == {
        "start_node_key": "collect",
        "reachable_node_count": 2,
        "configured_node_count": 2,
        "readiness": {"ready": False, "issue_count": 1},
    }
    assert summary["progress"] == {"node_runs": 4, "accepted": 1, "terminal": 2, "active": 2}
    assert {
        "artifacts",
        "snapshots",
        "node_runs",
        "gate_evaluations",
        "automation_plan",
    }.isdisjoint(summary)


def test_lightweight_run_detail_skips_artifact_queries(monkeypatch):
    """Automatic-record detail must not re-read historical Artifact rows."""

    run = SimpleNamespace(
        id="automatic-1",
        flow_definition_id="flow-1",
        run_no=2,
        name="连续记录",
        run_mode="AUTOMATIC",
        automation_plan_json={},
        parent_flow_run_id="parent-1",
        schedule_id=None,
        schedule_occurrence_id=None,
        state="COMPLETED",
        row_version=4,
        completion_mode=None,
        environment_version_id=None,
        lark_folder_token=None,
        lark_folder_url=None,
        active_snapshot_id=None,
        started_at=datetime(2026, 9, 8, 9, 0, tzinfo=UTC),
        finished_at=datetime(2026, 9, 8, 9, 1, tzinfo=UTC),
    )
    scalar_calls = 0

    class Db:
        def get(self, *_args):
            return None

        def scalars(self, _statement):
            nonlocal scalar_calls
            scalar_calls += 1
            return ()

    monkeypatch.setattr(orchestration_service, "_run", lambda *_args: run)

    detail = orchestration_service.run_detail(Db(), run.id, include_artifacts=False)

    assert detail["artifacts"] == []
    assert detail["node_runs"] == []
    # Snapshots and NodeRuns are the only list projections. A third query
    # would be the prohibited run-level Artifact history read.
    assert scalar_calls == 2


def test_duplicate_runtime_artifact_audit_is_read_only_and_reports_references(monkeypatch):
    """Historical duplicate candidates remain a confirmation-only report."""

    run = SimpleNamespace(
        id="automatic-1",
        automation_plan_json={"node_plans": {"review": {"artifact_ids": {"report": "a-2"}}}},
    )
    responses = iter(
        [
            [{"attempt_id": "attempt-1", "field_key": "report", "content_hash": "h-1"}],
            [
                {
                    "id": "a-1",
                    "producer_attempt_id": "attempt-1",
                    "field_key": "report",
                    "version_no": 1,
                    "content_hash": "h-1",
                    "artifact_type": "FILE",
                    "byte_size": 12,
                    "mime_type": "text/plain",
                    "runtime_completion_event_id": "finish-1",
                    "created_at": datetime(2026, 9, 8, 9, 0, tzinfo=UTC),
                },
                {
                    "id": "a-2",
                    "producer_attempt_id": "attempt-1",
                    "field_key": "report",
                    "version_no": 2,
                    "content_hash": "h-1",
                    "artifact_type": "FILE",
                    "byte_size": 12,
                    "mime_type": "text/plain",
                    "runtime_completion_event_id": "finish-1",
                    "created_at": datetime(2026, 9, 8, 9, 1, tzinfo=UTC),
                },
            ],
            [
                {
                    "artifact_version_id": "a-2",
                    "attempt_id": "consumer-1",
                    "input_field_key": "source",
                    "binding_source": "PORT_MAPPING",
                }
            ],
            [
                {
                    "id": "attempt-1",
                    "node_run_id": "node-run-1",
                    "workspace_ref": "/workspace/attempt-1",
                }
            ],
            [{"node_attempt_id": "attempt-1", "count": 1}],
        ]
    )

    class Result:
        def __init__(self, rows):
            self.rows = rows

        def mappings(self):
            return self.rows

    class Db:
        def scalar(self, _statement):
            return 1

        def execute(self, _statement):
            return Result(next(responses))

    monkeypatch.setattr(orchestration_service, "nested_automatic_run", lambda *_args: run)

    report = orchestration_service.duplicate_runtime_artifact_audit(
        Db(), "parent-1", run.id, page=1, page_size=20
    )

    assert report["read_only"] is True
    assert report["cleanup"]["state"] == "CONFIRMATION_REQUIRED"
    item = report["items"][0]
    assert item["evidence"] == {
        "kind": "FORMAL_COMPLETION_ID_REPLAY",
        "runtime_completion_event_ids": ["finish-1"],
    }
    assert item["artifacts"][1]["input_references"] == [
        {
            "consumer_attempt_id": "consumer-1",
            "input_field_key": "source",
            "binding_source": "PORT_MAPPING",
        }
    ]
    assert item["cleanup"]["proposed_action"] == "NO_ACTION_IN_THIS_RELEASE"
    assert item["cleanup"]["plan_reference_artifact_ids"] == ["a-2"]
    assert "storage_key" not in item["artifacts"][0]
    assert "inline_content" not in item["artifacts"][0]
