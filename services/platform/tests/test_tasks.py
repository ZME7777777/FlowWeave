from datetime import UTC, datetime, timedelta
from time import sleep

from sqlalchemy import select

from flowweave.modules.orchestration.application.service import _runtime_input_upload_handle
from flowweave.modules.tasks.application.service import (
    claim,
    enqueue,
    heartbeat,
    recover_expired,
    succeed,
)
from flowweave.shared.models import BackgroundTask, FlowDefinition, TaskState


def test_runtime_input_upload_uses_frozen_flow_run_generation_route(settings):
    from uuid import uuid4

    from flowweave.runtime.base import StartAttemptRequest
    from flowweave.runtime.openhands import OpenHandsRuntime

    conversation_id = str(uuid4())
    request = StartAttemptRequest(
        attempt_id="attempt-file-input",
        execution_key="attempt:attempt-file-input:start",
        node={},
        bindings=[],
        workspace_ref="workspace",
        conversation_id=conversation_id,
        runtime_sandbox_id="11111111-1111-4111-8111-111111111111",
        runtime_resource_name="flowweave-run-generation-7",
    )

    handle = _runtime_input_upload_handle(request)

    assert handle.job_id == "env-exec:flowweave-run-generation-7"
    assert handle.conversation_id == conversation_id
    assert handle.runtime_resource_id == request.runtime_sandbox_id
    assert handle.runtime_resource_name == request.runtime_resource_name
    assert (
        OpenHandsRuntime(settings)._base_url_for_handle(handle)
        == "http://flowweave-run-generation-7:8000"
    )


def _run_worker_until(worker, predicate, *, max_steps: int = 12) -> None:
    for _ in range(max_steps):
        if predicate():
            return
        assert worker._run_once_sync() is True
    assert predicate()


def _sandbox_desired_state(db_session_factory, sandbox_id: str) -> str | None:
    from flowweave.shared.models import ManagedSandbox

    with db_session_factory() as db:
        sandbox = db.get(ManagedSandbox, sandbox_id)
        return sandbox.desired_state if sandbox is not None else None


def test_task_lease_generation_fences_late_worker(db_session_factory):
    with db_session_factory() as db:
        task = enqueue(
            db,
            task_type="START_RUNTIME",
            aggregate_type="ATTEMPT",
            aggregate_id="attempt-1",
            idempotency_key="start:attempt-1",
        )
        db.commit()
        task_id = task.id
    with db_session_factory() as db:
        task, lease1 = claim(db, "worker-a", lease_seconds=30)
        assert task.id == task_id
        task.lease_until = datetime.now(UTC) - timedelta(seconds=1)
        db.commit()
    with db_session_factory() as db:
        assert recover_expired(db) == 1
    with db_session_factory() as db:
        task, lease2 = claim(db, "worker-b", lease_seconds=30)
        assert lease2.generation == lease1.generation + 1
    with db_session_factory() as db:
        assert heartbeat(db, lease1, lease_seconds=30) is False
        assert succeed(db, lease1) is False
    with db_session_factory() as db:
        assert succeed(db, lease2) is True
        assert db.get(BackgroundTask, task_id).state == TaskState.SUCCEEDED


def test_expired_lease_cannot_be_revived_by_heartbeat(db_session_factory):
    with db_session_factory() as db:
        task = enqueue(
            db,
            task_type="START_RUNTIME",
            aggregate_type="ATTEMPT",
            aggregate_id="expired-heartbeat",
            idempotency_key="expired-heartbeat",
        )
        db.commit()
        task_id = task.id
    with db_session_factory() as db:
        claimed = claim(db, "worker-a", lease_seconds=5)
        assert claimed is not None
        _task, lease = claimed
        row = db.get(BackgroundTask, task_id)
        assert row is not None
        row.lease_until = datetime.now(UTC) - timedelta(seconds=1)
        db.commit()
    with db_session_factory() as db:
        assert heartbeat(db, lease, lease_seconds=5) is False
        assert recover_expired(db) == 1


def test_worker_maintenance_recovers_a_lease_that_expires_after_startup(
    worker_container, db_session_factory
):
    """A restart just before expiry must not strand the task until another restart."""

    from flowweave.bootstrap.worker import TaskWorker

    worker = TaskWorker(worker_container)
    worker._recover_startup()
    with db_session_factory() as db:
        task = enqueue(
            db,
            task_type="PROVISION_AGENT_WORKSPACE_RUNTIME",
            aggregate_type="AGENT_WORKSPACE",
            aggregate_id="default-agent-workspace",
            idempotency_key="lease-expired-after-startup",
        )
        claimed = claim(db, "interrupted-worker", lease_seconds=5)
        assert claimed is not None
        task.lease_until = datetime.now(UTC) - timedelta(seconds=1)
        db.commit()
        task_id = task.id

    worker._run_sync(worker.run_maintenance())

    with db_session_factory() as db:
        recovered = db.get(BackgroundTask, task_id)
        assert recovered is not None
        assert recovered.state == TaskState.RETRY
        assert recovered.lease_owner is None
        assert recovered.lease_until is None
        assert recovered.last_error == "LEASE_EXPIRED"


def test_lease_heartbeat_prevents_recovery_and_second_claim(db_session_factory, settings):
    from flowweave.bootstrap.worker import LeaseHeartbeat

    with db_session_factory() as db:
        task = enqueue(
            db,
            task_type="START_RUNTIME",
            aggregate_type="ATTEMPT",
            aggregate_id="long-task",
            idempotency_key="long-task",
        )
        db.commit()
        task_id = task.id
    with db_session_factory() as db:
        claimed = claim(db, "worker-a", lease_seconds=2)
        assert claimed is not None
        _task, lease = claimed

    renewer = LeaseHeartbeat(
        settings,
        lease,
        interval_seconds=1,
        lease_seconds=2,
    )
    renewer.start()
    try:
        sleep(2.4)
        with db_session_factory() as db:
            assert recover_expired(db) == 0
            assert claim(db, "worker-b", lease_seconds=2) is None
            row = db.get(BackgroundTask, task_id)
            assert row is not None
            assert row.state == TaskState.RUNNING
            assert row.lease_owner == "worker-a"
        assert renewer.lost.is_set() is False
    finally:
        renewer.stop()
    with db_session_factory() as db:
        assert succeed(db, lease) is True


def test_late_worker_success_rolls_back_uncommitted_business_writes(db_session_factory):
    with db_session_factory() as db:
        flow = FlowDefinition(
            name="before-lost-lease",
        )
        db.add(flow)
        task = enqueue(
            db,
            task_type="START_RUNTIME",
            aggregate_type="ATTEMPT",
            aggregate_id="late-worker",
            idempotency_key="late-worker",
        )
        db.commit()
        flow_id = flow.id
        task_id = task.id
    with db_session_factory() as db:
        claimed = claim(db, "worker-a", lease_seconds=30)
        assert claimed is not None
        _task, stale_lease = claimed
        row = db.get(BackgroundTask, task_id)
        assert row is not None
        row.lease_until = datetime.now(UTC) - timedelta(seconds=1)
        db.commit()
    with db_session_factory() as db:
        assert recover_expired(db) == 1
        claimed = claim(db, "worker-b", lease_seconds=30)
        assert claimed is not None

    with db_session_factory() as late_db:
        flow = late_db.get(FlowDefinition, flow_id)
        assert flow is not None
        flow.name = "must-not-commit"
        assert succeed(late_db, stale_lease) is False

    with db_session_factory() as db:
        flow = db.get(FlowDefinition, flow_id)
        assert flow is not None
        assert flow.name == "before-lost-lease"


def test_idempotent_enqueue_returns_same_task(db_session_factory):
    with db_session_factory() as db:
        one = enqueue(
            db,
            task_type="CLEANUP_WORKSPACE",
            aggregate_type="ATTEMPT",
            aggregate_id="a",
            idempotency_key="cleanup:a",
        )
        db.commit()
        two = enqueue(
            db,
            task_type="CLEANUP_WORKSPACE",
            aggregate_type="ATTEMPT",
            aggregate_id="a",
            idempotency_key="cleanup:a",
        )
        assert one.id == two.id


def _asset_payload(_skill=None):
    """A Flow-owned node contract; Agent configuration lives on its session."""

    return {
        "name": "异步执行节点",
        "inputs": [
            {
                "field_key": "prd",
                "display_name": "需求",
                "data_type": "URL",
                "template_url": "https://example.feishu.cn/docx/prd-template",
            }
        ],
        "outputs": [
            {
                "field_key": "design",
                "display_name": "方案",
                "data_type": "URL",
                "template_url": "https://example.feishu.cn/docx/design-template",
            }
        ],
        "executor": {
            "startup_prompt": "生成方案",
            "context_prompt": "保留证据",
        },
    }


def _prepare_starting_attempt(worker_client, worker_container, _skill=None):
    """Create a plain Flow node and advance it through start confirmation."""

    from flowweave.bootstrap.worker import TaskWorker

    asset_response = worker_client.post("/api/v1/node-assets", json=_asset_payload())
    assert asset_response.status_code == 201, asset_response.text
    asset = asset_response.json()
    flow_response = worker_client.post(
        "/api/v1/flows",
        json={
            "name": "Worker 恢复流程",
            "environment_version_id": worker_client.environment_version_id,
            "default_entry_key": "design",
            "nodes": [{"instance_key": "design", "node_asset_id": asset["id"]}],
        },
    )
    assert flow_response.status_code == 201, flow_response.text
    flow = flow_response.json()
    started_response = worker_client.post(
        f"/api/v1/flows/{flow['id']}/runs",
        json={
            "environment_version_id": worker_client.environment_version_id,
            "flow_node_key": "design",
            "artifacts": [
                {
                    "field_key": "prd",
                    "artifact_type": "URL",
                    "uri": "https://example.feishu.cn/docx/recovery-input",
                }
            ],
        },
    )
    assert started_response.status_code == 201, started_response.text
    started = started_response.json()
    attempt_id = started["node_runs"][0]["attempts"][0]["id"]
    worker = TaskWorker(worker_container)
    assert worker._run_once_sync() is True
    assert worker._run_once_sync() is True
    ready = worker_client.get(f"/api/v1/flow-runs/{started['id']}").json()
    confirmed = worker_client.post(
        f"/api/v1/node-attempts/{attempt_id}/confirm-start",
        json={"expected_state_version": ready["node_runs"][0]["attempts"][0]["state_version"]},
        headers={"Idempotency-Key": f"recover-confirm:{attempt_id}"},
    )
    assert confirmed.status_code == 200, confirmed.text
    assert confirmed.json()["runtime_phase"] == "STARTING"
    return worker, started["id"], attempt_id


def test_worker_startup_recovers_deleted_runtime_delivery_and_advances_attempt(
    worker_client, db_session_factory, worker_container, worker_skill_capability
):
    from sqlalchemy import delete

    worker, run_id, attempt_id = _prepare_starting_attempt(
        worker_client, worker_container, worker_skill_capability
    )
    with db_session_factory() as db:
        db.execute(
            delete(BackgroundTask).where(
                BackgroundTask.aggregate_id == attempt_id,
                BackgroundTask.task_type == "START_RUNTIME",
            )
        )
        db.commit()

    worker._recover_startup()
    with db_session_factory() as db:
        recovered = db.scalar(
            select(BackgroundTask).where(
                BackgroundTask.aggregate_id == attempt_id,
                BackgroundTask.task_type == "START_RUNTIME",
            )
        )
        assert recovered is not None
        assert recovered.state == TaskState.PENDING
        assert recovered.idempotency_key.startswith("recovery:start_runtime:")

    assert worker._run_once_sync() is True
    detail = worker_client.get(f"/api/v1/flow-runs/{run_id}").json()
    attempt = detail["node_runs"][0]["attempts"][0]
    assert attempt["runtime_phase"] == "RUNNING"
    assert "runtime_job_id" not in attempt


def test_worker_startup_retries_terminal_recovery_delivery(
    worker_client, db_session_factory, worker_container, worker_skill_capability
):
    from sqlalchemy import delete

    worker, run_id, attempt_id = _prepare_starting_attempt(
        worker_client, worker_container, worker_skill_capability
    )
    with db_session_factory() as db:
        db.execute(
            delete(BackgroundTask).where(
                BackgroundTask.aggregate_id == attempt_id,
                BackgroundTask.task_type == "START_RUNTIME",
            )
        )
        db.commit()

    worker._recover_startup()
    with db_session_factory() as db:
        recovered = db.scalar(
            select(BackgroundTask).where(
                BackgroundTask.aggregate_id == attempt_id,
                BackgroundTask.task_type == "START_RUNTIME",
            )
        )
        assert recovered is not None
        recovered.state = TaskState.SUCCEEDED
        db.commit()
        recovery_id = recovered.id

    worker._recover_startup()
    with db_session_factory() as db:
        retried = db.get(BackgroundTask, recovery_id)
        assert retried is not None
        assert retried.state == TaskState.RETRY
        assert retried.last_error == "STARTUP_RECOVERY"

    assert worker._run_once_sync() is True
    detail = worker_client.get(f"/api/v1/flow-runs/{run_id}").json()
    assert detail["node_runs"][0]["attempts"][0]["runtime_phase"] == "RUNNING"


def test_cancelled_run_stops_started_runtime_through_worker(
    worker_client, worker_container, worker_skill_capability
):
    from flowweave.bootstrap.worker import TaskWorker
    from flowweave.runtime.base import RuntimeHandle

    asset = worker_client.post(
        "/api/v1/node-assets", json=_asset_payload(worker_skill_capability)
    ).json()
    flow = worker_client.post(
        "/api/v1/flows",
        json={
            "name": "取消 Runtime 流程",
            "environment_version_id": worker_client.environment_version_id,
            "default_entry_key": "design",
            "nodes": [{"instance_key": "design", "node_asset_id": asset["id"]}],
        },
    ).json()
    started = worker_client.post(
        f"/api/v1/flows/{flow['id']}/runs",
        json={
            "environment_version_id": worker_client.environment_version_id,
            "flow_node_key": "design",
            "artifacts": [
                {
                    "field_key": "prd",
                    "artifact_type": "URL",
                    "uri": "https://example.feishu.cn/docx/cancel-input",
                }
            ],
        },
    ).json()
    attempt_id = started["node_runs"][0]["attempts"][0]["id"]
    worker = TaskWorker(worker_container)
    assert worker._run_once_sync() is True  # readiness
    assert worker._run_once_sync() is True  # empty START gate stage
    ready = worker_client.get(f"/api/v1/flow-runs/{started['id']}").json()
    assert ready["node_runs"][0]["attempts"][0]["state"] == "WAITING_START_CONFIRMATION"
    worker_client.post(
        f"/api/v1/node-attempts/{attempt_id}/confirm-start",
        json={"expected_state_version": ready["node_runs"][0]["attempts"][0]["state_version"]},
        headers={"Idempotency-Key": "cancel-confirm"},
    )
    assert worker._run_once_sync() is True  # runtime start
    handle = RuntimeHandle(f"mock-job-{attempt_id}", f"mock-conversation-{attempt_id}", "1")

    cancelled = worker_client.post(
        f"/api/v1/flow-runs/{started['id']}/cancel",
        headers={"Idempotency-Key": "cancel-run"},
    ).json()
    assert cancelled["state"] == "CANCELLED"
    assert cancelled["node_runs"][0]["attempts"][0]["runtime_phase"] == "CANCELLING"

    _run_worker_until(
        worker,
        lambda: worker_client.get(f"/api/v1/flow-runs/{started['id']}").json()["node_runs"][0][
            "attempts"
        ][0]["runtime_phase"]
        == "CANCELLED",
    )
    final = worker_client.get(f"/api/v1/flow-runs/{started['id']}").json()
    assert final["node_runs"][0]["attempts"][0]["runtime_phase"] == "CANCELLED"
    assert worker_container.runtime.inspect(handle).status == "CANCELLED"


def test_cancel_attempt_stops_only_current_node_runtime(
    worker_client, worker_container, worker_skill_capability
):
    from flowweave.bootstrap.worker import TaskWorker
    from flowweave.runtime.base import RuntimeHandle

    asset = worker_client.post(
        "/api/v1/node-assets", json=_asset_payload(worker_skill_capability)
    ).json()
    flow = worker_client.post(
        "/api/v1/flows",
        json={
            "name": "停止当前节点 Runtime",
            "environment_version_id": worker_client.environment_version_id,
            "default_entry_key": "design",
            "nodes": [{"instance_key": "design", "node_asset_id": asset["id"]}],
        },
    ).json()
    started = worker_client.post(
        f"/api/v1/flows/{flow['id']}/runs",
        json={
            "environment_version_id": worker_client.environment_version_id,
            "flow_node_key": "design",
            "artifacts": [
                {
                    "field_key": "prd",
                    "artifact_type": "URL",
                    "uri": "https://example.feishu.cn/docx/cancel-attempt-input",
                }
            ],
        },
    ).json()
    attempt_id = started["node_runs"][0]["attempts"][0]["id"]
    worker = TaskWorker(worker_container)
    assert worker._run_once_sync() is True
    assert worker._run_once_sync() is True
    ready = worker_client.get(f"/api/v1/flow-runs/{started['id']}").json()
    confirmed = worker_client.post(
        f"/api/v1/node-attempts/{attempt_id}/confirm-start",
        json={"expected_state_version": ready["node_runs"][0]["attempts"][0]["state_version"]},
        headers={"Idempotency-Key": "cancel-attempt-confirm"},
    )
    assert confirmed.status_code == 200, confirmed.text
    assert worker._run_once_sync() is True
    running = worker_client.get(f"/api/v1/flow-runs/{started['id']}").json()
    attempt = running["node_runs"][0]["attempts"][0]
    handle = RuntimeHandle(f"mock-job-{attempt_id}", f"mock-conversation-{attempt_id}", "1")

    cancelled = worker_client.post(
        f"/api/v1/node-attempts/{attempt_id}/cancel",
        json={"expected_state_version": attempt["state_version"]},
        headers={"Idempotency-Key": "cancel-only-current-attempt"},
    )
    assert cancelled.status_code == 200, cancelled.text
    assert cancelled.json()["state"] == "CANCELLED"
    assert cancelled.json()["runtime_phase"] == "CANCELLING"
    after_command = worker_client.get(f"/api/v1/flow-runs/{started['id']}").json()
    assert after_command["state"] != "CANCELLED"
    assert after_command["node_runs"][0]["state"] == "CANCELLED"

    _run_worker_until(
        worker,
        lambda: worker_client.get(f"/api/v1/flow-runs/{started['id']}").json()["node_runs"][0][
            "attempts"
        ][0]["runtime_phase"]
        == "CANCELLED",
    )
    final_attempt = worker_client.get(f"/api/v1/flow-runs/{started['id']}").json()["node_runs"][0][
        "attempts"
    ][0]
    assert final_attempt["runtime_phase"] == "CANCELLED"
    assert worker_container.runtime.inspect(handle).status == "CANCELLED"


def test_worker_rolls_back_business_result_when_task_success_is_fenced(
    monkeypatch, worker_client, db_session_factory, worker_container, worker_skill_capability
):
    """Business writes and task success share one transaction after handler execution."""

    from sqlalchemy import func

    from flowweave.bootstrap import worker as worker_module
    from flowweave.bootstrap.worker import TaskWorker
    from flowweave.shared.models import NodeAttempt, RunEvent

    asset = worker_client.post(
        "/api/v1/node-assets", json=_asset_payload(worker_skill_capability)
    ).json()
    flow = worker_client.post(
        "/api/v1/flows",
        json={
            "name": "Worker 原子提交流程",
            "environment_version_id": worker_client.environment_version_id,
            "default_entry_key": "design",
            "nodes": [{"instance_key": "design", "node_asset_id": asset["id"]}],
        },
    ).json()
    started = worker_client.post(
        f"/api/v1/flows/{flow['id']}/runs",
        json={
            "environment_version_id": worker_client.environment_version_id,
            "flow_node_key": "design",
            "artifacts": [
                {
                    "field_key": "prd",
                    "artifact_type": "URL",
                    "uri": "https://example.feishu.cn/docx/atomic-input",
                }
            ],
        },
    ).json()
    attempt_id = started["node_runs"][0]["attempts"][0]["id"]

    # Simulate lease fencing between a successful handler flush and task completion.
    monkeypatch.setattr(worker_module, "succeed", lambda *_args, **_kwargs: False)
    assert TaskWorker(worker_container)._run_once_sync() is True

    with db_session_factory() as db:
        attempt = db.get(NodeAttempt, attempt_id)
        assert attempt is not None
        assert attempt.state == "WAITING_INPUT"
        assert (
            db.scalar(
                select(func.count(RunEvent.cursor)).where(
                    RunEvent.attempt_id == attempt_id,
                    RunEvent.event_type == "READINESS_EVALUATED",
                )
            )
            == 0
        )
        assert (
            db.scalar(
                select(func.count(BackgroundTask.id)).where(
                    BackgroundTask.aggregate_id == attempt_id,
                    BackgroundTask.task_type == "RUN_GATE_POLICY",
                )
            )
            == 0
        )


def test_late_poll_result_is_discarded_after_concurrent_cancel(
    worker_client, db_session_factory, worker_container, worker_skill_capability
):
    """A runtime result cannot overwrite an Attempt changed while I/O was in flight."""

    from concurrent.futures import ThreadPoolExecutor
    from threading import Event

    from sqlalchemy import func

    from flowweave.bootstrap.worker import TaskWorker
    from flowweave.modules.orchestration.application.service import process_poll_runtime
    from flowweave.runtime.base import (
        RuntimeEvent,
        RuntimeEventBatch,
        RuntimeHandle,
        RuntimeResult,
        StartAttemptRequest,
    )
    from flowweave.runtime.dependencies import runtime_context
    from flowweave.shared.models import ArtifactVersion, BackgroundTask, RunEvent

    asset = worker_client.post(
        "/api/v1/node-assets", json=_asset_payload(worker_skill_capability)
    ).json()
    flow = worker_client.post(
        "/api/v1/flows",
        json={
            "name": "Runtime CAS 迟到结果流程",
            "environment_version_id": worker_client.environment_version_id,
            "default_entry_key": "design",
            "nodes": [{"instance_key": "design", "node_asset_id": asset["id"]}],
        },
    ).json()
    started = worker_client.post(
        f"/api/v1/flows/{flow['id']}/runs",
        json={
            "environment_version_id": worker_client.environment_version_id,
            "flow_node_key": "design",
            "artifacts": [
                {
                    "field_key": "prd",
                    "artifact_type": "URL",
                    "uri": "https://example.feishu.cn/docx/runtime-cas-input",
                }
            ],
        },
    ).json()
    run_id = started["id"]
    attempt_id = started["node_runs"][0]["attempts"][0]["id"]
    worker = TaskWorker(worker_container)
    assert worker._run_once_sync() is True  # readiness
    assert worker._run_once_sync() is True  # START gates
    ready = worker_client.get(f"/api/v1/flow-runs/{run_id}").json()
    ready_attempt = ready["node_runs"][0]["attempts"][0]
    worker_client.post(
        f"/api/v1/node-attempts/{attempt_id}/confirm-start",
        json={"expected_state_version": ready_attempt["state_version"]},
        headers={"Idempotency-Key": "runtime-cas-confirm"},
    )
    assert worker._run_once_sync() is True  # START_RUNTIME; leaves POLL_RUNTIME pending

    entered = Event()
    release = Event()

    class BlockingTerminalRuntime:
        def start(self, _request: StartAttemptRequest) -> RuntimeHandle:
            raise AssertionError("runtime is already running")

        def read_events(self, _handle: RuntimeHandle) -> RuntimeEventBatch:
            entered.set()
            assert release.wait(timeout=5)
            return RuntimeEventBatch(
                events=(RuntimeEvent("late-1", "COMPLETED", {"reason": "late"}),),
                cursor="late-1",
                result=RuntimeResult(
                    status="COMPLETED",
                    outputs={"design": ("DOCUMENT", "must be discarded")},
                    cursor="late-1",
                ),
            )

        def inspect(self, _handle: RuntimeHandle) -> RuntimeResult:
            raise AssertionError("terminal event batch must not inspect")

        def resume(self, _handle: RuntimeHandle, _content: str) -> RuntimeResult:
            raise AssertionError("not used")

        def cancel(self, _handle: RuntimeHandle) -> None:
            return None

    def poll() -> None:
        from flowweave.shared.artifact_store import artifact_store_context
        from flowweave.shared.settings import settings_context

        with (
            runtime_context(BlockingTerminalRuntime()),
            settings_context(worker_container.settings),
            artifact_store_context(worker_container.artifact_store),
            db_session_factory() as db,
        ):
            process_poll_runtime(db, attempt_id, 1)

    with db_session_factory() as db:
        artifacts_before = db.scalar(
            select(func.count(ArtifactVersion.id)).where(
                ArtifactVersion.producer_attempt_id == attempt_id
            )
        )
        completed_events_before = db.scalar(
            select(func.count(RunEvent.cursor)).where(
                RunEvent.attempt_id == attempt_id,
                RunEvent.event_type == "RUNTIME_EVENT_COMPLETED",
            )
        )

    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(poll)
        assert entered.wait(timeout=5)
        cancelled = worker_client.post(
            f"/api/v1/flow-runs/{run_id}/cancel",
            headers={"Idempotency-Key": "runtime-cas-cancel"},
        )
        assert cancelled.status_code == 200, cancelled.text
        release.set()
        future.result(timeout=5)

    detail = worker_client.get(f"/api/v1/flow-runs/{run_id}").json()
    attempt = detail["node_runs"][0]["attempts"][0]
    assert attempt["state"] == "CANCELLED"
    assert attempt["runtime_phase"] == "CANCELLING"
    assert "runtime_cursor" not in attempt

    with db_session_factory() as db:
        assert (
            db.scalar(
                select(func.count(ArtifactVersion.id)).where(
                    ArtifactVersion.producer_attempt_id == attempt_id
                )
            )
            == artifacts_before
        )
        assert (
            db.scalar(
                select(func.count(RunEvent.cursor)).where(
                    RunEvent.attempt_id == attempt_id,
                    RunEvent.event_type == "RUNTIME_EVENT_COMPLETED",
                )
            )
            == completed_events_before
        )
        assert (
            db.scalar(
                select(func.count(BackgroundTask.id)).where(
                    BackgroundTask.aggregate_id == attempt_id,
                    BackgroundTask.task_type == "RUN_GATE_POLICY",
                    BackgroundTask.payload_json["stage"].as_string() == "END",
                )
            )
            == 0
        )


def test_worker_runtime_io_runs_without_database_transaction(
    worker_client, db_session_factory, worker_container, worker_skill_capability
):
    """Worker freezes runtime input, ends the read transaction, then performs external I/O."""

    from flowweave.bootstrap.worker import TaskWorker
    from flowweave.modules.orchestration.public import process_poll_runtime
    from flowweave.modules.tasks.application.service import claim, succeed
    from flowweave.runtime.base import (
        RuntimeEventBatch,
        RuntimeHandle,
        RuntimeResult,
        StartAttemptRequest,
    )
    from flowweave.runtime.dependencies import runtime_context
    from flowweave.shared.settings import settings_context

    asset = worker_client.post(
        "/api/v1/node-assets", json=_asset_payload(worker_skill_capability)
    ).json()
    flow = worker_client.post(
        "/api/v1/flows",
        json={
            "name": "Runtime 短事务流程",
            "environment_version_id": worker_client.environment_version_id,
            "default_entry_key": "design",
            "nodes": [{"instance_key": "design", "node_asset_id": asset["id"]}],
        },
    ).json()
    started = worker_client.post(
        f"/api/v1/flows/{flow['id']}/runs",
        json={
            "environment_version_id": worker_client.environment_version_id,
            "flow_node_key": "design",
            "artifacts": [
                {
                    "field_key": "prd",
                    "artifact_type": "URL",
                    "uri": "https://example.feishu.cn/docx/short-transaction-input",
                }
            ],
        },
    ).json()
    attempt_id = started["node_runs"][0]["attempts"][0]["id"]
    worker = TaskWorker(worker_container)
    assert worker._run_once_sync() is True  # readiness
    assert worker._run_once_sync() is True  # START gates
    ready = worker_client.get(f"/api/v1/flow-runs/{started['id']}").json()
    worker_client.post(
        f"/api/v1/node-attempts/{attempt_id}/confirm-start",
        json={"expected_state_version": ready["node_runs"][0]["attempts"][0]["state_version"]},
        headers={"Idempotency-Key": "runtime-short-transaction-confirm"},
    )
    assert worker._run_once_sync() is True  # START_RUNTIME creates POLL_RUNTIME

    with db_session_factory() as claim_db:
        claimed = claim(claim_db, "io-boundary-worker", lease_seconds=30)
    assert claimed is not None
    task, lease = claimed
    assert task.task_type == "POLL_RUNTIME"

    transaction_states: list[bool] = []

    with db_session_factory() as db:

        class InspectingRuntime:
            def start(self, _request: StartAttemptRequest) -> RuntimeHandle:
                raise AssertionError("runtime is already running")

            def read_events(self, handle: RuntimeHandle) -> RuntimeEventBatch:
                transaction_states.append(db.in_transaction())
                return RuntimeEventBatch(
                    events=(),
                    cursor=handle.cursor,
                    result=RuntimeResult(status="RUNNING", cursor=handle.cursor),
                )

            def inspect(self, _handle: RuntimeHandle) -> RuntimeResult:
                raise AssertionError("event batch already contains a result")

            def resume(self, _handle: RuntimeHandle, _content: str) -> RuntimeResult:
                raise AssertionError("not used")

            def cancel(self, _handle: RuntimeHandle) -> None:
                raise AssertionError("not used")

        with (
            settings_context(worker_container.settings),
            runtime_context(InspectingRuntime()),
        ):
            process_poll_runtime(db, attempt_id, 1, lease, commit=False)
        assert succeed(db, lease, commit=False) is True
        db.commit()

    assert transaction_states == [False]
    with db_session_factory() as db:
        persisted = db.get(BackgroundTask, task.id)
        assert persisted is not None
        assert persisted.state == TaskState.SUCCEEDED
        next_poll = db.scalar(
            select(BackgroundTask).where(
                BackgroundTask.aggregate_id == attempt_id,
                BackgroundTask.task_type == "POLL_RUNTIME",
                BackgroundTask.state == TaskState.PENDING,
            )
        )
        assert next_poll is not None


def test_worker_gate_io_is_transaction_free_and_late_result_is_discarded(
    worker_client, db_session_factory, worker_container, worker_skill_capability, monkeypatch
):
    """Gate I/O holds no DB transaction and CAS discards a concurrently stale result."""

    from concurrent.futures import ThreadPoolExecutor
    from threading import Event

    from sqlalchemy import func, update

    from flowweave.bootstrap.worker import TaskWorker
    from flowweave.modules.gates.public import GateResult
    from flowweave.modules.orchestration import application as orchestration_application
    from flowweave.modules.orchestration.public import process_gate_stage
    from flowweave.modules.tasks.application.service import claim, succeed
    from flowweave.shared.models import GateEvaluation, NodeAttempt, RunEvent

    asset = worker_client.post(
        "/api/v1/node-assets", json=_asset_payload(worker_skill_capability)
    ).json()
    flow = worker_client.post(
        "/api/v1/flows",
        json={
            "name": "Gate CAS 短事务流程",
            "environment_version_id": worker_client.environment_version_id,
            "default_entry_key": "design",
            "nodes": [
                {
                    "instance_key": "design",
                    "node_asset_id": asset["id"],
                    "gates": [
                        {
                            "stage": "START",
                            "position": 0,
                            "gate_type": "PYTHON",
                            "config": {
                                "prompt": "评估输出是否符合门禁要求",
                                "code": (
                                    "result = {'decision': 'PASS', 'summary': 'ok', "
                                    "'reasons': [], 'evidence': [], 'details': {}}"
                                ),
                            },
                            "agent_preset": {},
                        }
                    ],
                }
            ],
        },
    ).json()
    started = worker_client.post(
        f"/api/v1/flows/{flow['id']}/runs",
        json={
            "environment_version_id": worker_client.environment_version_id,
            "flow_node_key": "design",
            "artifacts": [
                {
                    "field_key": "prd",
                    "artifact_type": "URL",
                    "uri": "https://example.feishu.cn/docx/gate-cas-input",
                }
            ],
        },
    ).json()
    attempt_id = started["node_runs"][0]["attempts"][0]["id"]
    worker = TaskWorker(worker_container)
    assert worker._run_once_sync() is True  # readiness; queues START gates

    with db_session_factory() as claim_db:
        claimed = claim(claim_db, "gate-io-boundary-worker", lease_seconds=30)
    assert claimed is not None
    task, lease = claimed
    assert task.task_type == "RUN_GATE_POLICY"

    entered = Event()
    release = Event()
    transaction_states: list[bool] = []

    with db_session_factory() as db:

        def execute_frozen_gate(_db, _context, prepared):
            transaction_states.append(db.in_transaction())
            entered.set()
            assert release.wait(timeout=5)
            result = GateResult("PASS", "late pass", [], [], {})
            return [(item, result) for item in prepared], False

        monkeypatch.setattr(
            orchestration_application.service, "_execute_gate_stage", execute_frozen_gate
        )

        def execute_gate() -> None:
            process_gate_stage(db, attempt_id, "START", lease, commit=False)

        with ThreadPoolExecutor(max_workers=1) as pool:
            future = pool.submit(execute_gate)
            assert entered.wait(timeout=5)
            with db_session_factory() as concurrent_db:
                changed = concurrent_db.execute(
                    update(NodeAttempt)
                    .where(NodeAttempt.id == attempt_id)
                    .values(state_version=NodeAttempt.state_version + 1)
                )
                assert changed.rowcount == 1
                concurrent_db.commit()
            release.set()
            future.result(timeout=5)

        # The delivery may complete as a stale no-op, but no business result is persisted.
        assert succeed(db, lease, commit=False) is True
        db.commit()

    assert transaction_states == [False]
    with db_session_factory() as db:
        attempt = db.get(NodeAttempt, attempt_id)
        assert attempt is not None
        assert attempt.state == "START_GATES"
        assert (
            db.scalar(
                select(func.count(GateEvaluation.id)).where(GateEvaluation.attempt_id == attempt_id)
            )
            == 0
        )
        assert (
            db.scalar(
                select(func.count(RunEvent.cursor)).where(
                    RunEvent.attempt_id == attempt_id,
                    RunEvent.event_type == "GATE_STAGE_FINISHED",
                )
            )
            == 0
        )
