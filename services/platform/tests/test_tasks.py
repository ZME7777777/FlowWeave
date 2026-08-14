import base64
import hashlib
import io
import json
import zipfile
from datetime import UTC, datetime, timedelta
from time import sleep

import pytest
from sqlalchemy import select

from flowweave.modules.tasks.application.service import (
    claim,
    enqueue,
    heartbeat,
    recover_expired,
    succeed,
)
from flowweave.shared.models import BackgroundTask, FlowDefinition, TaskState


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
            lark_root_folder_url=("https://example.feishu.cn/drive/folder/before-lost-lease-root"),
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


def _asset_payload(skill):
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
        "capabilities": [skill],
        "executor": {
            "startup_prompt": "生成方案",
            "context_prompt": "保留证据",
            "timeout_seconds": 120,
            "max_iterations": 20,
        },
    }


def _import_tool_policy(client, *, name: str, tools: list[str]) -> dict:
    document = {
        "name": name,
        "tools": [{"name": tool, "params": {}} for tool in tools],
    }
    validated = client.post(
        "/api/v1/capability-imports/validate",
        json={
            "capability_type": "TOOL_POLICY",
            "filename": f"{name}.json",
            "content_base64": base64.b64encode(
                json.dumps(document, sort_keys=True).encode()
            ).decode(),
        },
    )
    assert validated.status_code == 200, validated.text
    committed = client.post(
        "/api/v1/capability-imports",
        json={"import_token": validated.json()["import_token"]},
    )
    assert committed.status_code == 201, committed.text
    return committed.json()["capabilities"][0]


def _import_runtime_policy(client, *, capability_type: str, name: str, config: dict) -> dict:
    document = {"name": name, **config}
    validated = client.post(
        "/api/v1/capability-imports/validate",
        json={
            "capability_type": capability_type,
            "filename": f"{name}.json",
            "content_base64": base64.b64encode(
                json.dumps(document, sort_keys=True).encode()
            ).decode(),
        },
    )
    assert validated.status_code == 200, validated.text
    committed = client.post(
        "/api/v1/capability-imports",
        json={"import_token": validated.json()["import_token"]},
    )
    assert committed.status_code == 201, committed.text
    return committed.json()["capabilities"][0]


def _import_agent_definition(
    client, *, name: str, tools: list[str], system_prompt: str = "Review carefully."
) -> dict:
    document = {
        "name": name,
        "description": f"Governed sub-agent {name}",
        "model": "inherit",
        "tools": tools,
        "system_prompt": system_prompt,
        "when_to_use_examples": ["Review a proposed change"],
        "permission_mode": "never_confirm",
        "condenser": {"kind": "NoOpCondenser"},
    }
    validated = client.post(
        "/api/v1/capability-imports/validate",
        json={
            "capability_type": "AGENT_DEFINITION",
            "filename": f"{name}.json",
            "content_base64": base64.b64encode(
                json.dumps(document, sort_keys=True).encode()
            ).decode(),
        },
    )
    assert validated.status_code == 200, validated.text
    committed = client.post(
        "/api/v1/capability-imports",
        json={"import_token": validated.json()["import_token"]},
    )
    assert committed.status_code == 201, committed.text
    return committed.json()["capabilities"][0]


def _import_agent_profile(
    client, *, name: str, policies: dict[str, dict], max_iterations: int
) -> dict:
    document = {
        "name": name,
        "agent_kind": "OPENHANDS",
        "model": "inherit",
        "tool_policy_version_id": policies["TOOL_POLICY"]["capability_id"],
        "context_policy_version_id": policies["CONTEXT_POLICY"]["capability_id"],
        "memory_policy_version_id": policies["MEMORY_POLICY"]["capability_id"],
        "critic_policy_version_id": policies["CRITIC_POLICY"]["capability_id"],
        "confirmation_policy": "ALWAYS",
        "max_iterations": max_iterations,
    }
    validated = client.post(
        "/api/v1/capability-imports/validate",
        json={
            "capability_type": "AGENT_PROFILE",
            "filename": f"{name}.json",
            "content_base64": base64.b64encode(
                json.dumps(document, sort_keys=True).encode()
            ).decode(),
        },
    )
    assert validated.status_code == 200, validated.text
    committed = client.post(
        "/api/v1/capability-imports",
        json={"import_token": validated.json()["import_token"]},
    )
    assert committed.status_code == 201, committed.text
    return committed.json()["capabilities"][0]


def _import_plugin(client, *, name: str) -> dict:
    archive_bytes = io.BytesIO()
    with zipfile.ZipFile(archive_bytes, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        archive.writestr(
            f"{name}/.plugin/plugin.json",
            json.dumps(
                {
                    "name": name,
                    "version": "1.0.0",
                    "description": "Governed frozen Plugin",
                },
                sort_keys=True,
            ),
        )
        archive.writestr(
            f"{name}/commands/review.md",
            "---\ndescription: Review a change\nallowed-tools: [terminal]\n---\nReview it.\n",
        )
    validated = client.post(
        "/api/v1/capability-imports/validate",
        json={
            "capability_type": "PLUGIN",
            "filename": f"{name}.zip",
            "content_base64": base64.b64encode(archive_bytes.getvalue()).decode(),
        },
    )
    assert validated.status_code == 200, validated.text
    committed = client.post(
        "/api/v1/capability-imports",
        json={"import_token": validated.json()["import_token"]},
    )
    assert committed.status_code == 201, committed.text
    return committed.json()["capabilities"][0]


def test_worker_executes_readiness_gates_and_runtime_tasks(
    worker_client, db_session_factory, worker_container, worker_skill_capability
):
    from flowweave.bootstrap.worker import TaskWorker

    asset = worker_client.post(
        "/api/v1/node-assets", json=_asset_payload(worker_skill_capability)
    ).json()
    flow = worker_client.post(
        "/api/v1/flows",
        json={
            "name": "异步执行流程",
            "lark_root_folder_url": "https://example.feishu.cn/drive/folder/task-root",
            "default_entry_key": "design",
            "nodes": [
                {
                    "instance_key": "design",
                    "node_asset_id": asset["id"],
                    "gates": [
                        {
                            "stage": "START",
                            "position": 0,
                            "gate_type": "JAVASCRIPT",
                            "config": {
                                "code": (
                                    "return {decision: 'PASS', summary: '输入可用', "
                                    "reasons: [], evidence: [], details: {}};"
                                )
                            },
                        },
                        {
                            "stage": "END",
                            "position": 0,
                            "gate_type": "PYTHON",
                            "config": {
                                "code": (
                                    "result = {'decision': 'PASS', 'summary': '输出可用', "
                                    "'reasons': [], 'evidence': [], 'details': {}}"
                                )
                            },
                        },
                    ],
                }
            ],
        },
    ).json()
    started = worker_client.post(
        f"/api/v1/flows/{flow['id']}/runs",
        json={
            "flow_node_key": "design",
            "artifacts": [
                {
                    "field_key": "prd",
                    "artifact_type": "URL",
                    "uri": "https://example.feishu.cn/docx/async-input",
                }
            ],
        },
    ).json()
    attempt_id = started["node_runs"][0]["attempts"][0]["id"]
    assert started["node_runs"][0]["attempts"][0]["state"] == "WAITING_INPUT"
    worker = TaskWorker(worker_container)

    assert worker._run_once_sync() is True  # readiness
    assert worker._run_once_sync() is True  # START gates
    ready = worker_client.get(f"/api/v1/flow-runs/{started['id']}").json()
    assert ready["node_runs"][0]["attempts"][0]["state"] == "WAITING_START_CONFIRMATION"

    queued = worker_client.post(
        f"/api/v1/node-attempts/{attempt_id}/confirm-start",
        json={"expected_state_version": ready["node_runs"][0]["attempts"][0]["state_version"]},
        headers={"Idempotency-Key": "async-confirm"},
    ).json()
    assert queued["state"] == "EXECUTING"
    assert queued["runtime_phase"] == "STARTING"
    assert queued["runtime_job_id"] is None

    assert worker._run_once_sync() is True  # runtime start
    _run_worker_until(
        worker,
        lambda: worker_client.get(f"/api/v1/flow-runs/{started['id']}").json()["node_runs"][0][
            "attempts"
        ][0]["state"]
        == "WAITING_ACCEPTANCE",
    )
    finished = worker_client.get(f"/api/v1/flow-runs/{started['id']}").json()
    attempt = finished["node_runs"][0]["attempts"][0]
    assert attempt["state"] == "WAITING_ACCEPTANCE"
    assert attempt["runtime_phase"] == "COMPLETED"
    assert attempt["runtime_job_id"].startswith("mock-job-")
    assert attempt["artifacts"][0]["field_key"] == "design"

    with db_session_factory() as db:
        types = list(
            db.scalars(select(BackgroundTask.task_type).order_by(BackgroundTask.created_at))
        )
        states = list(db.scalars(select(BackgroundTask.state)))
    assert types[:4] == [
        "CLEANUP_CAPABILITY_IMPORT",
        "EVALUATE_READINESS",
        "RUN_GATE_POLICY",
        "START_RUNTIME",
    ]
    assert {"POLL_RUNTIME", "WAIT_RUNTIME_WAKEUP", "RUN_GATE_POLICY"}.issubset(types)
    assert states.count(TaskState.SUCCEEDED) >= 5
    assert states.count(TaskState.PENDING) == 1


def _prepare_starting_attempt(
    worker_client, worker_container, skill, *, extra_capabilities: list[dict] | None = None
):
    from flowweave.bootstrap.worker import TaskWorker

    asset_payload = _asset_payload(skill)
    if extra_capabilities:
        asset_payload["name"] = "异步执行节点（Profile）"
    asset_payload["capabilities"].extend(extra_capabilities or [])
    asset_response = worker_client.post("/api/v1/node-assets", json=asset_payload)
    assert asset_response.status_code == 201, asset_response.text
    asset = asset_response.json()
    flow = worker_client.post(
        "/api/v1/flows",
        json={
            "name": "Worker 恢复流程",
            "lark_root_folder_url": "https://example.feishu.cn/drive/folder/task-root",
            "default_entry_key": "design",
            "nodes": [{"instance_key": "design", "node_asset_id": asset["id"]}],
        },
    ).json()
    started = worker_client.post(
        f"/api/v1/flows/{flow['id']}/runs",
        json={
            "flow_node_key": "design",
            "artifacts": [
                {
                    "field_key": "prd",
                    "artifact_type": "URL",
                    "uri": "https://example.feishu.cn/docx/recovery-input",
                }
            ],
        },
    ).json()
    attempt_id = started["node_runs"][0]["attempts"][0]["id"]
    worker = TaskWorker(worker_container)
    assert worker._run_once_sync() is True  # readiness
    assert worker._run_once_sync() is True  # empty START gates
    ready = worker_client.get(f"/api/v1/flow-runs/{started['id']}").json()
    confirmed = worker_client.post(
        f"/api/v1/node-attempts/{attempt_id}/confirm-start",
        json={"expected_state_version": ready["node_runs"][0]["attempts"][0]["state_version"]},
        headers={"Idempotency-Key": f"recover-confirm:{attempt_id}"},
    ).json()
    assert confirmed["runtime_phase"] == "STARTING"
    return worker, started["id"], attempt_id


def test_start_runtime_uses_frozen_agent_definition_after_live_node_change(
    worker_client, db_session_factory, worker_container, worker_skill_capability
):
    from flowweave.runtime.base import StartAttemptRequest
    from flowweave.runtime.mock import MockRuntime

    policy = _import_tool_policy(
        worker_client, name="native-task-policy", tools=["task_tool_set", "terminal"]
    )
    definition = _import_agent_definition(worker_client, name="reviewer", tools=["terminal"])
    worker, run_id, _attempt_id = _prepare_starting_attempt(
        worker_client,
        worker_container,
        worker_skill_capability,
        extra_capabilities=[policy, definition],
    )

    # The Run Snapshot is already frozen. Remove the definition from the live
    # node to prove START_RUNTIME consumes only the immutable manifest.
    run = worker_client.get(f"/api/v1/flow-runs/{run_id}").json()
    snapshot = run["snapshots"][0]
    frozen_types = [
        item["capability_type"]
        for item in snapshot["definition"]["nodes"][0]["asset"]["capabilities"]
    ]
    assert "AGENT_DEFINITION" in frozen_types
    frozen_definitions = snapshot["runtime_manifest"]["nodes"]["design"]["agent_spec"][
        "agent_definitions"
    ]
    assert [item["capability_key"] for item in frozen_definitions] == ["reviewer"]
    asset_id = run["snapshots"][0]["definition"]["nodes"][0]["asset"]["id"]
    live_asset = worker_client.get(f"/api/v1/node-assets/{asset_id}").json()
    replacement = _asset_payload(worker_skill_capability)
    replacement["capabilities"].append(policy)
    replacement["row_version"] = live_asset["row_version"]
    updated = worker_client.put(f"/api/v1/node-assets/{asset_id}", json=replacement)
    assert updated.status_code == 200, updated.text

    captured: list[StartAttemptRequest] = []

    class CapturingRuntime(MockRuntime):
        def start(self, request: StartAttemptRequest):
            captured.append(request)
            return super().start(request)

    previous_runtime = worker_container.runtime
    worker_container.runtime = CapturingRuntime()
    try:
        assert worker._run_once_sync() is True
    finally:
        worker_container.runtime = previous_runtime

    with db_session_factory() as db:
        start_task = db.scalar(
            select(BackgroundTask).where(
                BackgroundTask.aggregate_id == _attempt_id,
                BackgroundTask.task_type == "START_RUNTIME",
            )
        )
        assert start_task is not None
        assert len(captured) == 1, start_task.last_error
    assert [item.name for item in captured[0].agent_spec.agent_definitions] == ["reviewer"]
    frozen = captured[0].agent_spec.agent_definitions[0]
    assert frozen.tools == ("terminal",)
    assert frozen.permission_mode == "never_confirm"
    assert frozen.system_prompt == "Review carefully."


def test_start_runtime_uses_frozen_plugin_after_live_node_change(
    worker_client, db_session_factory, worker_container, worker_skill_capability
):
    from flowweave.runtime.base import StartAttemptRequest
    from flowweave.runtime.mock import MockRuntime

    plugin = _import_plugin(worker_client, name="review-plugin")
    worker, run_id, attempt_id = _prepare_starting_attempt(
        worker_client,
        worker_container,
        worker_skill_capability,
        extra_capabilities=[plugin],
    )

    run = worker_client.get(f"/api/v1/flow-runs/{run_id}").json()
    snapshot = run["snapshots"][0]
    frozen_plugins = snapshot["runtime_manifest"]["nodes"]["design"]["agent_spec"]["plugins"]
    assert [item["capability_key"] for item in frozen_plugins] == ["review-plugin"]
    assert frozen_plugins[0]["capability_version_id"] == plugin["capability_id"]
    assert frozen_plugins[0]["runtime_config"]["file_hashes"]

    asset_id = snapshot["definition"]["nodes"][0]["asset"]["id"]
    live_asset = worker_client.get(f"/api/v1/node-assets/{asset_id}").json()
    replacement = _asset_payload(worker_skill_capability)
    replacement["row_version"] = live_asset["row_version"]
    updated = worker_client.put(f"/api/v1/node-assets/{asset_id}", json=replacement)
    assert updated.status_code == 200, updated.text
    assert all(item["capability_type"] != "PLUGIN" for item in updated.json()["capabilities"])

    captured: list[StartAttemptRequest] = []

    class CapturingRuntime(MockRuntime):
        def start(self, request: StartAttemptRequest):
            captured.append(request)
            return super().start(request)

    previous_runtime = worker_container.runtime
    worker_container.runtime = CapturingRuntime()
    try:
        assert worker._run_once_sync() is True
    finally:
        worker_container.runtime = previous_runtime

    with db_session_factory() as db:
        start_task = db.scalar(
            select(BackgroundTask).where(
                BackgroundTask.aggregate_id == attempt_id,
                BackgroundTask.task_type == "START_RUNTIME",
            )
        )
        assert start_task is not None
        assert len(captured) == 1, start_task.last_error
    assert [item.name for item in captured[0].agent_spec.plugins] == ["review-plugin"]
    frozen = captured[0].agent_spec.plugins[0]
    assert frozen.content_hash == plugin["normalized_config"]["content_hash"]
    assert plugin["capability_id"] in frozen.source
    assert frozen.source.startswith("/runtime/capabilities/nodes/")


def test_start_runtime_rejects_plugin_drift_with_rehashed_manifest(
    worker_client, db_session_factory, worker_container, worker_skill_capability
):
    from copy import deepcopy

    from flowweave.runtime.base import StartAttemptRequest
    from flowweave.shared.models import NodeAttempt, RunSnapshot

    plugin = _import_plugin(worker_client, name="fenced-plugin")
    worker, _run_id, attempt_id = _prepare_starting_attempt(
        worker_client,
        worker_container,
        worker_skill_capability,
        extra_capabilities=[plugin],
    )
    with db_session_factory() as db:
        attempt = db.get(NodeAttempt, attempt_id)
        assert attempt is not None
        snapshot = db.get(RunSnapshot, attempt.snapshot_id)
        assert snapshot is not None
        manifest = deepcopy(snapshot.runtime_manifest_json)
        frozen = manifest["nodes"]["design"]["agent_spec"]["plugins"][0]
        frozen["runtime_config"]["file_hashes"]["commands/review.md"] = "0" * 64
        # Recompute the outer checksum to prove the immutable capability digest,
        # rather than only the manifest envelope, fences Plugin configuration.
        snapshot.runtime_manifest_json = manifest
        snapshot.runtime_manifest_hash = hashlib.sha256(
            json.dumps(
                manifest,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            ).encode()
        ).hexdigest()
        db.commit()

    runtime_called = False

    class RejectUnexpectedRuntime:
        def start(self, _request: StartAttemptRequest):
            nonlocal runtime_called
            runtime_called = True
            raise AssertionError("Runtime must not receive a drifted Plugin")

    previous_runtime = worker_container.runtime
    worker_container.runtime = RejectUnexpectedRuntime()
    try:
        assert worker._run_once_sync() is True
    finally:
        worker_container.runtime = previous_runtime

    assert runtime_called is False
    with db_session_factory() as db:
        task = db.scalar(
            select(BackgroundTask).where(
                BackgroundTask.aggregate_id == attempt_id,
                BackgroundTask.task_type == "START_RUNTIME",
            )
        )
        assert task is not None
        assert task.state == TaskState.RETRY
        assert "SNAPSHOT_MANIFEST_INVALID" in str(task.last_error)


def test_start_runtime_rejects_agent_definition_drift_with_rehashed_manifest(
    worker_client, db_session_factory, worker_container, worker_skill_capability
):
    from copy import deepcopy

    from flowweave.runtime.base import StartAttemptRequest
    from flowweave.shared.models import NodeAttempt, RunSnapshot

    policy = _import_tool_policy(
        worker_client, name="native-task-fenced", tools=["task_tool_set", "terminal"]
    )
    definition = _import_agent_definition(worker_client, name="fenced-reviewer", tools=["terminal"])
    worker, _run_id, attempt_id = _prepare_starting_attempt(
        worker_client,
        worker_container,
        worker_skill_capability,
        extra_capabilities=[policy, definition],
    )
    with db_session_factory() as db:
        attempt = db.get(NodeAttempt, attempt_id)
        assert attempt is not None
        snapshot = db.get(RunSnapshot, attempt.snapshot_id)
        assert snapshot is not None
        manifest = deepcopy(snapshot.runtime_manifest_json)
        frozen = manifest["nodes"]["design"]["agent_spec"]["agent_definitions"][0]
        frozen["runtime_config"]["system_prompt"] = "Tampered instructions"
        snapshot.runtime_manifest_json = manifest
        snapshot.runtime_manifest_hash = hashlib.sha256(
            json.dumps(
                manifest,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            ).encode()
        ).hexdigest()
        db.commit()

    runtime_called = False

    class RejectUnexpectedRuntime:
        def start(self, _request: StartAttemptRequest):
            nonlocal runtime_called
            runtime_called = True
            raise AssertionError("Runtime must not receive a drifted Agent Definition")

    previous_runtime = worker_container.runtime
    worker_container.runtime = RejectUnexpectedRuntime()
    try:
        assert worker._run_once_sync() is True
    finally:
        worker_container.runtime = previous_runtime

    assert runtime_called is False
    with db_session_factory() as db:
        task = db.scalar(
            select(BackgroundTask).where(
                BackgroundTask.aggregate_id == attempt_id,
                BackgroundTask.task_type == "START_RUNTIME",
            )
        )
        assert task is not None
        assert task.state == TaskState.RETRY
        assert "SNAPSHOT_MANIFEST_INVALID" in str(task.last_error)


def test_start_runtime_consumes_frozen_snapshot_capability_manifest(
    worker_client, worker_container, worker_skill_capability
):
    from flowweave.runtime.base import StartAttemptRequest
    from flowweave.runtime.mock import MockRuntime

    worker, run_id, _attempt_id = _prepare_starting_attempt(
        worker_client, worker_container, worker_skill_capability
    )
    run = worker_client.get(f"/api/v1/flow-runs/{run_id}").json()
    asset_id = run["snapshots"][0]["definition"]["nodes"][0]["asset"]["id"]
    source = worker_client.get(
        f"/api/v1/capabilities/{worker_skill_capability['capability_id']}/source"
    ).json()
    revised = worker_client.put(
        f"/api/v1/capabilities/{worker_skill_capability['capability_id']}/source",
        json={"content": source["content"] + "\nNew immutable revision\n"},
    )
    assert revised.status_code == 200, revised.text
    revised_capability = revised.json()
    asset = worker_client.get(f"/api/v1/node-assets/{asset_id}").json()
    replacement = _asset_payload(
        {
            "capability_id": revised_capability["id"],
            "capability_type": revised_capability["capability_type"],
            "capability_key": revised_capability["capability_key"],
        }
    )
    replacement["row_version"] = asset["row_version"]
    updated = worker_client.put(f"/api/v1/node-assets/{asset_id}", json=replacement)
    assert updated.status_code == 200, updated.text

    captured: list[StartAttemptRequest] = []

    class CapturingRuntime(MockRuntime):
        def start(self, request: StartAttemptRequest):
            captured.append(request)
            return super().start(request)

    previous_runtime = worker_container.runtime
    worker_container.runtime = CapturingRuntime()
    try:
        assert worker._run_once_sync() is True
    finally:
        worker_container.runtime = previous_runtime

    assert len(captured) == 1
    capability = captured[0].node["asset"]["capabilities"][0]
    assert capability["capability_id"] == worker_skill_capability["capability_id"]
    assert (
        capability["normalized_config"]["digest"]
        == worker_skill_capability["normalized_config"]["digest"]
    )
    assert capability["capability_id"] != revised_capability["id"]


def test_start_runtime_consumes_frozen_runtime_agent_spec_tool_policy(
    worker_client, worker_container, worker_skill_capability
):
    from flowweave.runtime.base import StartAttemptRequest
    from flowweave.runtime.mock import MockRuntime

    terminal_policy = _import_tool_policy(worker_client, name="terminal-only", tools=["terminal"])
    payload = _asset_payload(worker_skill_capability)
    payload["capabilities"].append(terminal_policy)
    asset = worker_client.post("/api/v1/node-assets", json=payload)
    assert asset.status_code == 201, asset.text
    flow = worker_client.post(
        "/api/v1/flows",
        json={
            "name": "Frozen Agent Spec flow",
            "lark_root_folder_url": "https://example.feishu.cn/drive/folder/spec-root",
            "default_entry_key": "design",
            "nodes": [{"instance_key": "design", "node_asset_id": asset.json()["id"]}],
        },
    )
    assert flow.status_code == 201, flow.text
    started = worker_client.post(
        f"/api/v1/flows/{flow.json()['id']}/runs",
        json={
            "flow_node_key": "design",
            "artifacts": [
                {
                    "field_key": "prd",
                    "artifact_type": "URL",
                    "uri": "https://example.feishu.cn/docx/spec-input",
                }
            ],
        },
    )
    assert started.status_code == 201, started.text
    attempt_id = started.json()["node_runs"][0]["attempts"][0]["id"]

    # Change the live node after the Run Snapshot exists. The Runtime must still
    # receive the terminal-only policy frozen by that Snapshot.
    all_tools = _import_tool_policy(
        worker_client,
        name="all-default-tools",
        tools=["terminal", "file_editor", "task_tracker"],
    )
    current = worker_client.get(f"/api/v1/node-assets/{asset.json()['id']}").json()
    replacement = _asset_payload(worker_skill_capability)
    replacement["row_version"] = current["row_version"]
    replacement["capabilities"].append(all_tools)
    updated = worker_client.put(f"/api/v1/node-assets/{asset.json()['id']}", json=replacement)
    assert updated.status_code == 200, updated.text

    from flowweave.bootstrap.worker import TaskWorker

    worker = TaskWorker(worker_container)
    assert worker._run_once_sync() is True
    assert worker._run_once_sync() is True
    ready = worker_client.get(f"/api/v1/flow-runs/{started.json()['id']}").json()
    confirmed = worker_client.post(
        f"/api/v1/node-attempts/{attempt_id}/confirm-start",
        json={"expected_state_version": ready["node_runs"][0]["attempts"][0]["state_version"]},
        headers={"Idempotency-Key": f"frozen-spec:{attempt_id}"},
    )
    assert confirmed.status_code == 200, confirmed.text

    captured: list[StartAttemptRequest] = []

    class CapturingRuntime(MockRuntime):
        def start(self, request: StartAttemptRequest):
            captured.append(request)
            return super().start(request)

    previous_runtime = worker_container.runtime
    worker_container.runtime = CapturingRuntime()
    try:
        assert worker._run_once_sync() is True
    finally:
        worker_container.runtime = previous_runtime

    assert len(captured) == 1
    assert [tool.name for tool in captured[0].agent_spec.tools] == ["terminal"]
    assert (
        captured[0].node["runtime_agent_spec"]["tool_policy"]["capability_version_id"]
        == terminal_policy["capability_id"]
    )


def test_start_runtime_consumes_frozen_context_and_critic_policy_versions(
    worker_client, worker_container, worker_skill_capability
):
    from flowweave.runtime.base import StartAttemptRequest
    from flowweave.runtime.mock import MockRuntime

    context_policy = _import_runtime_policy(
        worker_client,
        capability_type="CONTEXT_POLICY",
        name="reviewed-context",
        config={
            "system_message_suffix": "Apply the reviewed organization policy.",
            "user_message_suffix": "Use only frozen capabilities.",
            "disabled_skills": ["ambient-unreviewed"],
        },
    )
    critic_policy = _import_runtime_policy(
        worker_client,
        capability_type="CRITIC_POLICY",
        name="reviewed-critic",
        config={
            "enabled": True,
            "mode": "ALL_ACTIONS",
            "threshold": 0.8,
            "max_refinement_iterations": 2,
        },
    )
    worker, _run_id, _attempt_id = _prepare_starting_attempt(
        worker_client,
        worker_container,
        worker_skill_capability,
        extra_capabilities=[context_policy, critic_policy],
    )
    captured: list[StartAttemptRequest] = []

    class CapturingRuntime(MockRuntime):
        def start(self, request: StartAttemptRequest):
            captured.append(request)
            return super().start(request)

    previous_runtime = worker_container.runtime
    worker_container.runtime = CapturingRuntime()
    try:
        assert worker._run_once_sync() is True
    finally:
        worker_container.runtime = previous_runtime

    assert len(captured) == 1
    request = captured[0]
    assert request.agent_spec.agent_context.system_message_suffix == (
        "Apply the reviewed organization policy."
    )
    assert request.agent_spec.agent_context.user_message_suffix == ("Use only frozen capabilities.")
    assert request.agent_spec.agent_context.disabled_skills == ("ambient-unreviewed",)
    assert request.agent_spec.critic is not None
    assert request.agent_spec.critic.mode == "all_actions"
    assert request.agent_spec.critic.success_threshold == 0.8
    assert request.agent_spec.critic.max_iterations == 2
    frozen_spec = request.node["runtime_agent_spec"]
    assert (
        frozen_spec["context_policy"]["capability_version_id"] == (context_policy["capability_id"])
    )
    assert frozen_spec["critic_policy"]["capability_version_id"] == (critic_policy["capability_id"])


def test_memory_policy_rejects_unverified_source_reference(worker_client):
    from uuid import uuid4

    document = {
        "name": "unverified-memory",
        "enabled": True,
        "scopes": ["ATTEMPT"],
        "source_refs": [{"reference_id": str(uuid4()), "digest": "a" * 64}],
        "retention_days": 30,
        "require_review": True,
        "sensitive_data_scan": True,
        "replay_mode": "FROZEN",
    }
    validated = worker_client.post(
        "/api/v1/capability-imports/validate",
        json={
            "capability_type": "MEMORY_POLICY",
            "filename": "unverified-memory.json",
            "content_base64": base64.b64encode(
                json.dumps(document, sort_keys=True).encode()
            ).decode(),
        },
    )
    assert validated.status_code == 200, validated.text

    rejected = worker_client.post(
        "/api/v1/capability-imports",
        json={"import_token": validated.json()["import_token"]},
    )

    assert rejected.status_code == 422, rejected.text
    assert rejected.json()["error"]["code"] == "MEMORY_SOURCE_REFERENCE_INVALID"


def test_node_accepts_enabled_memory_with_governed_source(
    worker_client, db_session_factory, worker_skill_capability
):
    created = worker_client.post(
        "/api/v1/memory-sources",
        json={
            "source_key": "task-runtime-memory",
            "display_name": "Task runtime memory",
            "owner_id": "memory-owner",
            "scope": "PROJECT",
            "scope_key": "task-project",
            "content": "# Governed memory\n\nUse the frozen project standard.\n",
        },
    )
    assert created.status_code == 201, created.text
    source = created.json()
    version = source["latest_version"]
    reviewed = worker_client.post(
        f"/api/v1/memory-sources/{source['id']}/versions/{version['id']}/review",
        json={"expected_governance_version": 1, "decision": "APPROVE"},
        headers={"X-Actor-ID": "memory-reviewer"},
    )
    assert reviewed.status_code == 200, reviewed.text
    scanned = worker_client.post(
        f"/api/v1/memory-sources/{source['id']}/versions/{version['id']}/scan",
        json={"expected_governance_version": reviewed.json()["governance_version"]},
    )
    assert scanned.status_code == 200, scanned.text
    activated = worker_client.post(
        f"/api/v1/memory-sources/{source['id']}/versions/{version['id']}/activate",
        json={
            "expected_governance_version": scanned.json()["governance_version"],
            "retention_days": 30,
        },
    )
    assert activated.status_code == 200, activated.text
    active = activated.json()
    memory_policy = _import_runtime_policy(
        worker_client,
        capability_type="MEMORY_POLICY",
        name="reviewed-memory",
        config={
            "enabled": True,
            "scopes": ["ATTEMPT"],
            "source_refs": [{"reference_id": active["id"], "digest": active["digest"]}],
            "retention_days": 30,
            "require_review": True,
            "sensitive_data_scan": True,
            "replay_mode": "FROZEN",
        },
    )
    payload = _asset_payload(worker_skill_capability)
    payload["capabilities"].append(memory_policy)
    accepted = worker_client.post(
        "/api/v1/node-assets",
        json=payload,
    )

    assert accepted.status_code == 201, accepted.text

    flow = worker_client.post(
        "/api/v1/flows",
        json={
            "name": "Governed Memory Snapshot",
            "lark_root_folder_url": "https://example.feishu.cn/drive/folder/memory-root",
            "default_entry_key": "design",
            "nodes": [{"instance_key": "design", "node_asset_id": accepted.json()["id"]}],
        },
    )
    assert flow.status_code == 201, flow.text
    started = worker_client.post(f"/api/v1/flows/{flow.json()['id']}/runs", json={})
    assert started.status_code == 201, started.text

    from flowweave.shared.models import MemorySourceVersionReference

    with db_session_factory() as db:
        hold = db.scalar(
            select(MemorySourceVersionReference).where(
                MemorySourceVersionReference.memory_source_version_id == active["id"],
                MemorySourceVersionReference.reference_kind == "RUN_SNAPSHOT",
                MemorySourceVersionReference.reference_id == started.json()["active_snapshot_id"],
            )
        )
        assert hold is not None


def test_start_runtime_rejects_tampered_snapshot_manifest(
    worker_client, db_session_factory, worker_container, worker_skill_capability
):
    from copy import deepcopy

    from flowweave.runtime.base import StartAttemptRequest
    from flowweave.shared.models import NodeAttempt, RunSnapshot

    worker, _run_id, attempt_id = _prepare_starting_attempt(
        worker_client, worker_container, worker_skill_capability
    )
    with db_session_factory() as db:
        attempt = db.get(NodeAttempt, attempt_id)
        assert attempt is not None
        snapshot = db.get(RunSnapshot, attempt.snapshot_id)
        assert snapshot is not None
        manifest = deepcopy(snapshot.runtime_manifest_json)
        manifest["nodes"]["design"]["capabilities"][0]["digest"] = "0" * 64
        snapshot.runtime_manifest_json = manifest
        db.commit()

    runtime_called = False

    class RejectUnexpectedRuntime:
        def start(self, _request: StartAttemptRequest):
            nonlocal runtime_called
            runtime_called = True
            raise AssertionError("Runtime must not receive a tampered manifest")

    previous_runtime = worker_container.runtime
    worker_container.runtime = RejectUnexpectedRuntime()
    try:
        assert worker._run_once_sync() is True
    finally:
        worker_container.runtime = previous_runtime

    assert runtime_called is False
    with db_session_factory() as db:
        task = db.scalar(
            select(BackgroundTask).where(
                BackgroundTask.aggregate_id == attempt_id,
                BackgroundTask.task_type == "START_RUNTIME",
            )
        )
        assert task is not None
        assert task.state == TaskState.RETRY
        assert "SNAPSHOT_MANIFEST_INVALID" in str(task.last_error)


def test_start_runtime_rejects_tool_policy_drift_even_with_rehashed_manifest(
    worker_client, db_session_factory, worker_container, worker_skill_capability
):
    from copy import deepcopy

    from flowweave.runtime.base import StartAttemptRequest
    from flowweave.shared.models import NodeAttempt, RunSnapshot

    worker, _run_id, attempt_id = _prepare_starting_attempt(
        worker_client, worker_container, worker_skill_capability
    )
    with db_session_factory() as db:
        attempt = db.get(NodeAttempt, attempt_id)
        assert attempt is not None
        snapshot = db.get(RunSnapshot, attempt.snapshot_id)
        assert snapshot is not None
        manifest = deepcopy(snapshot.runtime_manifest_json)
        policy = manifest["nodes"]["design"]["agent_spec"]["tool_policy"]
        policy["runtime_config"]["tools"] = [{"name": "terminal", "params": {}}]
        # Simulate an attacker who can also rewrite the outer checksum. The
        # immutable policy digest/config identity must still fence the Runtime.
        snapshot.runtime_manifest_json = manifest
        snapshot.runtime_manifest_hash = hashlib.sha256(
            json.dumps(
                manifest,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            ).encode()
        ).hexdigest()
        db.commit()

    runtime_called = False

    class RejectUnexpectedRuntime:
        def start(self, _request: StartAttemptRequest):
            nonlocal runtime_called
            runtime_called = True
            raise AssertionError("Runtime must not receive a drifted Tool Policy")

    previous_runtime = worker_container.runtime
    worker_container.runtime = RejectUnexpectedRuntime()
    try:
        assert worker._run_once_sync() is True
    finally:
        worker_container.runtime = previous_runtime

    assert runtime_called is False
    with db_session_factory() as db:
        task = db.scalar(
            select(BackgroundTask).where(
                BackgroundTask.aggregate_id == attempt_id,
                BackgroundTask.task_type == "START_RUNTIME",
            )
        )
        assert task is not None
        assert task.state == TaskState.RETRY
        assert "SNAPSHOT_MANIFEST_INVALID" in str(task.last_error)


def test_start_runtime_rejects_profile_spec_drift_with_rehashed_manifest(
    worker_client, db_session_factory, worker_container, worker_skill_capability
):
    from copy import deepcopy

    from flowweave.runtime.base import StartAttemptRequest
    from flowweave.shared.models import NodeAttempt, RunSnapshot

    baseline = worker_client.post(
        "/api/v1/node-assets", json=_asset_payload(worker_skill_capability)
    )
    assert baseline.status_code == 201, baseline.text
    policies = {
        item["capability_type"]: item
        for item in baseline.json()["capabilities"]
        if item["capability_type"].endswith("_POLICY")
    }
    profile = _import_agent_profile(
        worker_client, name="fenced-profile", policies=policies, max_iterations=20
    )
    worker, _run_id, attempt_id = _prepare_starting_attempt(
        worker_client,
        worker_container,
        worker_skill_capability,
        extra_capabilities=[profile],
    )
    with db_session_factory() as db:
        attempt = db.get(NodeAttempt, attempt_id)
        assert attempt is not None
        snapshot = db.get(RunSnapshot, attempt.snapshot_id)
        assert snapshot is not None
        manifest = deepcopy(snapshot.runtime_manifest_json)
        spec = manifest["nodes"]["design"]["agent_spec"]
        assert spec["agent_profile"]["capability_version_id"] == profile["capability_id"]
        spec["confirmation_policy"] = "NEVER"
        snapshot.runtime_manifest_json = manifest
        snapshot.runtime_manifest_hash = hashlib.sha256(
            json.dumps(manifest, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest()
        db.commit()

    runtime_called = False

    class RejectUnexpectedRuntime:
        def start(self, _request: StartAttemptRequest):
            nonlocal runtime_called
            runtime_called = True
            raise AssertionError("Runtime must not receive a Profile-drifted Agent Spec")

    previous_runtime = worker_container.runtime
    worker_container.runtime = RejectUnexpectedRuntime()
    try:
        assert worker._run_once_sync() is True
    finally:
        worker_container.runtime = previous_runtime

    assert runtime_called is False
    with db_session_factory() as db:
        task = db.scalar(
            select(BackgroundTask).where(
                BackgroundTask.aggregate_id == attempt_id,
                BackgroundTask.task_type == "START_RUNTIME",
            )
        )
        assert task is not None
        assert task.state == TaskState.RETRY
        assert "SNAPSHOT_MANIFEST_INVALID" in str(task.last_error)


def test_start_runtime_carries_frozen_profile_provenance(
    worker_client, db_session_factory, worker_container, worker_skill_capability
):
    from flowweave.runtime.base import StartAttemptRequest
    from flowweave.runtime.mock import MockRuntime

    baseline = worker_client.post(
        "/api/v1/node-assets", json=_asset_payload(worker_skill_capability)
    )
    assert baseline.status_code == 201, baseline.text
    policies = {
        item["capability_type"]: item
        for item in baseline.json()["capabilities"]
        if item["capability_type"].endswith("_POLICY")
    }
    profile = _import_agent_profile(
        worker_client, name="observable-profile", policies=policies, max_iterations=20
    )
    worker, _run_id, attempt_id = _prepare_starting_attempt(
        worker_client,
        worker_container,
        worker_skill_capability,
        extra_capabilities=[profile],
    )
    captured: list[StartAttemptRequest] = []

    class CapturingRuntime(MockRuntime):
        def start(self, request: StartAttemptRequest):
            captured.append(request)
            return super().start(request)

    previous_runtime = worker_container.runtime
    worker_container.runtime = CapturingRuntime()
    try:
        assert worker._run_once_sync() is True
    finally:
        worker_container.runtime = previous_runtime

    with db_session_factory() as db:
        task = db.scalar(
            select(BackgroundTask).where(
                BackgroundTask.aggregate_id == attempt_id,
                BackgroundTask.task_type == "START_RUNTIME",
            )
        )
        assert task is not None
        assert len(captured) == 1, task.last_error
    frozen = captured[0].agent_spec.agent_profile
    assert frozen is not None
    assert frozen.capability_version_id == profile["capability_id"]
    assert frozen.capability_key == "observable-profile"
    assert frozen.digest == profile["normalized_config"]["digest"]
    assert frozen.content_hash == profile["normalized_config"]["content_hash"]


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
    assert attempt["runtime_job_id"].startswith("mock-job-")


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
            "lark_root_folder_url": "https://example.feishu.cn/drive/folder/task-root",
            "default_entry_key": "design",
            "nodes": [{"instance_key": "design", "node_asset_id": asset["id"]}],
        },
    ).json()
    started = worker_client.post(
        f"/api/v1/flows/{flow['id']}/runs",
        json={
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
    running = worker_client.get(f"/api/v1/flow-runs/{started['id']}").json()
    attempt = running["node_runs"][0]["attempts"][0]
    assert attempt["runtime_adapter"] == "mock"
    handle = RuntimeHandle(
        attempt["runtime_job_id"], attempt["conversation_id"], attempt["runtime_cursor"]
    )

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
            "lark_root_folder_url": "https://example.feishu.cn/drive/folder/task-root",
            "default_entry_key": "design",
            "nodes": [{"instance_key": "design", "node_asset_id": asset["id"]}],
        },
    ).json()
    started = worker_client.post(
        f"/api/v1/flows/{flow['id']}/runs",
        json={
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
    handle = RuntimeHandle(
        attempt["runtime_job_id"], attempt["conversation_id"], attempt["runtime_cursor"]
    )

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


def test_shared_runtime_parent_interrupt_does_not_claim_inflight_task_cancelled(
    worker_client, db_session_factory, worker_container, worker_skill_capability
):
    from flowweave.shared.models import (
        AgentConversation,
        BackgroundTask,
        NodeAttempt,
        RunEvent,
        RuntimeSubagentTask,
    )

    worker, run_id, attempt_id = _prepare_starting_attempt(
        worker_client, worker_container, worker_skill_capability
    )
    assert worker._run_once_sync() is True  # START_RUNTIME
    with db_session_factory() as db:
        attempt = db.get(NodeAttempt, attempt_id)
        conversation = db.scalar(
            select(AgentConversation).where(
                AgentConversation.attempt_id == attempt_id,
                AgentConversation.kind == "AUTO",
            )
        )
        assert attempt is not None
        assert conversation is not None
        db.add(
            RuntimeSubagentTask(
                attempt_id=attempt_id,
                conversation_id=conversation.id,
                action_event_id="cancel-shared-task-action",
                action_cursor="cancel-shared-task-action",
                tool_call_id="cancel-shared-tool-call",
                subagent_type="reviewer",
                state="REQUESTED",
            )
        )
        db.commit()

    current = worker_client.get(f"/api/v1/flow-runs/{run_id}").json()
    attempt = current["node_runs"][0]["attempts"][0]
    cancelled = worker_client.post(
        f"/api/v1/node-attempts/{attempt_id}/cancel",
        json={"expected_state_version": attempt["state_version"]},
        headers={"Idempotency-Key": "cancel-shared-inflight-task"},
    )
    assert cancelled.status_code == 200, cancelled.text
    _run_worker_until(
        worker,
        lambda: worker_client.get(f"/api/v1/flow-runs/{run_id}").json()["node_runs"][0]["attempts"][
            0
        ]["runtime_phase"]
        == "CANCEL_FAILED",
    )

    final = worker_client.get(f"/api/v1/flow-runs/{run_id}").json()
    final_attempt = final["node_runs"][0]["attempts"][0]
    assert final_attempt["runtime_phase"] == "CANCEL_FAILED"
    assert final_attempt["error_code"] == "RUNTIME_TASK_CANCEL_UNCONFIRMED"
    with db_session_factory() as db:
        cancel_task = db.scalar(
            select(BackgroundTask).where(
                BackgroundTask.aggregate_id == attempt_id,
                BackgroundTask.task_type == "CANCEL_RUNTIME",
            )
        )
        assert cancel_task is not None
        assert cancel_task.max_attempts == 20
        assert cancel_task.state == TaskState.SUCCEEDED
        audit = db.scalar(
            select(RunEvent).where(
                RunEvent.attempt_id == attempt_id,
                RunEvent.event_type == "RUNTIME_SUBAGENT_CANCEL_UNCONFIRMED",
            )
        )
        assert audit is not None
        assert audit.payload_json["control_scope"] == "PARENT_CONVERSATION_INTERRUPT"
        assert audit.payload_json["pending_tasks"] == [
            {
                "runtime_subagent_task_id": audit.payload_json["pending_tasks"][0][
                    "runtime_subagent_task_id"
                ],
                "conversation_id": audit.payload_json["pending_tasks"][0]["conversation_id"],
                "action_event_id": "cancel-shared-task-action",
                "tool_call_id": "cancel-shared-tool-call",
            }
        ]


def test_shared_runtime_cancel_recovery_waits_for_late_formal_task_usage(
    worker_client, db_session_factory, worker_container, worker_skill_capability
):
    from flowweave.runtime.base import (
        RuntimeEvent,
        RuntimeEventBatch,
        RuntimeTaskUsageSnapshot,
    )
    from flowweave.shared.models import (
        AgentConversation,
        NodeAttempt,
        RuntimeSubagentTask,
        RuntimeSubagentTaskUsage,
    )

    worker, run_id, attempt_id = _prepare_starting_attempt(
        worker_client, worker_container, worker_skill_capability
    )
    assert worker._run_once_sync() is True  # START_RUNTIME
    with db_session_factory() as db:
        attempt = db.get(NodeAttempt, attempt_id)
        conversation = db.scalar(
            select(AgentConversation).where(
                AgentConversation.attempt_id == attempt_id,
                AgentConversation.kind == "AUTO",
            )
        )
        assert attempt is not None
        assert conversation is not None
        db.add(
            RuntimeSubagentTask(
                attempt_id=attempt_id,
                conversation_id=conversation.id,
                action_event_id="late-cancel-task-action",
                action_cursor="late-cancel-task-action",
                tool_call_id="late-cancel-tool-call",
                subagent_type="reviewer",
                state="REQUESTED",
            )
        )
        db.commit()

    current = worker_client.get(f"/api/v1/flow-runs/{run_id}").json()
    attempt = current["node_runs"][0]["attempts"][0]
    cancelled = worker_client.post(
        f"/api/v1/node-attempts/{attempt_id}/cancel",
        json={"expected_state_version": attempt["state_version"]},
        headers={"Idempotency-Key": "cancel-before-late-task-observation"},
    )
    assert cancelled.status_code == 200, cancelled.text
    _run_worker_until(
        worker,
        lambda: worker_client.get(f"/api/v1/flow-runs/{run_id}").json()["node_runs"][0]["attempts"][
            0
        ]["runtime_phase"]
        == "CANCEL_FAILED",
    )

    failed = worker_client.get(f"/api/v1/flow-runs/{run_id}").json()["node_runs"][0]["attempts"][0]
    assert failed["runtime_phase"] == "CANCEL_FAILED"
    assert failed["runtime_cancel_recovery_modes"] == ["RECONCILE_PARENT"]

    previous_runtime = worker_container.runtime

    class LateObservationRuntime:
        calls = 0

        def read_events(self, _handle):
            self.calls += 1
            usage = (
                ()
                if self.calls == 1
                else (
                    RuntimeTaskUsageSnapshot(
                        task_id="task_late_cancel_recovery",
                        source_cursor="late-cancel-task-stats",
                        digest="d" * 64,
                        model_name="openai/test-model",
                        accumulated_cost=0.2,
                        prompt_tokens=20,
                        completion_tokens=8,
                        cache_read_tokens=0,
                        cache_write_tokens=0,
                        reasoning_tokens=0,
                        context_window=4096,
                        per_turn_tokens=28,
                    ),
                )
            )
            return RuntimeEventBatch(
                events=(
                    RuntimeEvent(
                        "late-cancel-task-observation",
                        "TOOL_RESULT",
                        {
                            "content": "review completed before cancellation recovery",
                            "runtime_task": {
                                "phase": "COMPLETED",
                                "action_event_id": "late-cancel-task-action",
                                "observation_event_id": "late-cancel-task-observation",
                                "tool_call_id": "late-cancel-tool-call",
                                "task_id": "task_late_cancel_recovery",
                                "subagent_type": "reviewer",
                                "status": "completed",
                            },
                        },
                    ),
                ),
                cursor=(
                    "late-cancel-task-observation" if self.calls == 1 else "late-cancel-task-stats"
                ),
                task_usage=usage,
            )

        def cancel(self, _handle):
            return None

    worker_container.runtime = LateObservationRuntime()
    try:
        recovery = worker_client.post(
            f"/api/v1/node-attempts/{attempt_id}/retry-runtime-cancel",
            json={
                "expected_state_version": failed["state_version"],
                "mode": "RECONCILE_PARENT",
            },
            headers={"Idempotency-Key": "reconcile-late-task-observation"},
        )
        assert recovery.status_code == 200, recovery.text
        assert recovery.json()["runtime_phase"] == "CANCELLING"
        with db_session_factory() as db:
            recovery_delivery = db.scalar(
                select(BackgroundTask).where(
                    BackgroundTask.aggregate_id == attempt_id,
                    BackgroundTask.task_type == "CANCEL_RUNTIME",
                    BackgroundTask.state == TaskState.PENDING,
                    BackgroundTask.payload_json["recovery_mode"].as_string() == "RECONCILE_PARENT",
                )
            )
            assert recovery_delivery is not None
            for stale_delivery in db.scalars(
                select(BackgroundTask).where(
                    BackgroundTask.id != recovery_delivery.id,
                    BackgroundTask.state.in_([TaskState.PENDING, TaskState.RETRY]),
                )
            ):
                stale_delivery.state = TaskState.SUCCEEDED
            recovery_delivery.available_at = datetime.now(UTC)
            db.commit()
        assert worker._run_once_sync() is True
        waiting = worker_client.get(f"/api/v1/flow-runs/{run_id}").json()["node_runs"][0][
            "attempts"
        ][0]
        assert waiting["runtime_phase"] == "CANCELLING"
        with db_session_factory() as db:
            usage_recovery = db.scalar(
                select(BackgroundTask).where(
                    BackgroundTask.aggregate_id == attempt_id,
                    BackgroundTask.task_type == "CANCEL_RUNTIME",
                    BackgroundTask.idempotency_key.like("cancel-task-usage-recovery:%"),
                )
            )
            assert usage_recovery is not None
            assert usage_recovery.state == TaskState.PENDING
            assert usage_recovery.payload_json == {
                "recovery_mode": "RECONCILE_PARENT",
                "sandbox_ids": [],
            }
            usage_recovery.available_at = datetime.now(UTC)
            db.commit()
        assert worker._run_once_sync() is True
    finally:
        worker_container.runtime = previous_runtime

    final = worker_client.get(f"/api/v1/flow-runs/{run_id}").json()["node_runs"][0]["attempts"][0]
    assert final["runtime_phase"] == "CANCELLED"
    assert final["error_code"] is None
    assert final["runtime_cancel_recovery_modes"] == []
    with db_session_factory() as db:
        projected = db.scalar(
            select(RuntimeSubagentTask).where(
                RuntimeSubagentTask.attempt_id == attempt_id,
                RuntimeSubagentTask.action_event_id == "late-cancel-task-action",
            )
        )
        assert projected is not None
        assert projected.state == "COMPLETED"
        assert projected.observation_event_id == "late-cancel-task-observation"
        assert projected.runtime_task_id == "task_late_cancel_recovery"
        usage = db.scalar(
            select(RuntimeSubagentTaskUsage).where(
                RuntimeSubagentTaskUsage.runtime_subagent_task_id == projected.id
            )
        )
        assert usage is not None
        assert usage.runtime_task_id == "task_late_cancel_recovery"
        assert float(usage.accumulated_cost_usd) == 0.2


def test_managed_runtime_cancel_cleanup_mode_survives_worker_restart(
    worker_client, db_session_factory, worker_container, worker_skill_capability
):
    from uuid import uuid4

    from sqlalchemy import delete, update

    from flowweave.modules.tasks.application.handlers import record_terminal_failure
    from flowweave.shared.models import (
        AgentConversation,
        ManagedSandbox,
        NodeAttempt,
        RunEvent,
        RuntimeSubagentTask,
    )

    worker, run_id, attempt_id = _prepare_starting_attempt(
        worker_client, worker_container, worker_skill_capability
    )
    assert worker._run_once_sync() is True  # START_RUNTIME
    sandbox_id = str(uuid4())
    with db_session_factory() as db:
        attempt = db.get(NodeAttempt, attempt_id)
        conversation = db.scalar(
            select(AgentConversation).where(
                AgentConversation.attempt_id == attempt_id,
                AgentConversation.kind == "AUTO",
            )
        )
        assert attempt is not None
        assert conversation is not None
        db.add(
            ManagedSandbox(
                id=sandbox_id,
                kind="AGENT_RUNTIME",
                owner_type="ATTEMPT",
                owner_id=attempt_id,
                backend="docker",
                backend_resource_name=f"runtime-recovery-{attempt_id}",
                image_reference="flowweave-openhands-runtime:1",
                observed_state="RUNNING",
                desired_state="RUNNING",
                hard_expires_at=datetime.now(UTC) + timedelta(hours=1),
            )
        )
        db.flush()
        attempt.runtime_sandbox_id = sandbox_id
        conversation.runtime_sandbox_id = sandbox_id
        db.add(
            RuntimeSubagentTask(
                attempt_id=attempt_id,
                conversation_id=conversation.id,
                action_event_id="restart-cleanup-task-action",
                action_cursor="restart-cleanup-task-action",
                tool_call_id="restart-cleanup-tool-call",
                subagent_type="reviewer",
                state="REQUESTED",
            )
        )
        db.commit()

    current = worker_client.get(f"/api/v1/flow-runs/{run_id}").json()
    attempt = current["node_runs"][0]["attempts"][0]
    cancelled = worker_client.post(
        f"/api/v1/node-attempts/{attempt_id}/cancel",
        json={"expected_state_version": attempt["state_version"]},
        headers={"Idempotency-Key": "cancel-before-managed-recovery"},
    )
    assert cancelled.status_code == 200, cancelled.text

    with db_session_factory() as db:
        cancel_task = db.scalar(
            select(BackgroundTask).where(
                BackgroundTask.aggregate_id == attempt_id,
                BackgroundTask.task_type == "CANCEL_RUNTIME",
            )
        )
        assert cancel_task is not None
        cancel_task.state = TaskState.DEAD
        db.flush()
        record_terminal_failure(db, cancel_task.id, "Runtime cancellation delivery failed")
        db.commit()

    failed = worker_client.get(f"/api/v1/flow-runs/{run_id}").json()["node_runs"][0]["attempts"][0]
    assert failed["runtime_phase"] == "CANCEL_FAILED"
    assert failed["runtime_cancel_recovery_modes"] == [
        "RECONCILE_PARENT",
        "DELETE_MANAGED_RUNTIME",
    ]
    recovery = worker_client.post(
        f"/api/v1/node-attempts/{attempt_id}/retry-runtime-cancel",
        json={
            "expected_state_version": failed["state_version"],
            "mode": "DELETE_MANAGED_RUNTIME",
        },
        headers={"Idempotency-Key": "delete-managed-runtime-after-cancel-failure"},
    )
    assert recovery.status_code == 200, recovery.text
    assert recovery.json()["runtime_phase"] == "CANCELLING"

    with db_session_factory() as db:
        db.execute(
            delete(BackgroundTask).where(
                BackgroundTask.aggregate_id == attempt_id,
                BackgroundTask.task_type == "CANCEL_RUNTIME",
            )
        )
        db.execute(
            update(BackgroundTask)
            .where(
                BackgroundTask.aggregate_id == attempt_id,
                BackgroundTask.task_type == "POLL_RUNTIME",
            )
            .values(state=TaskState.SUCCEEDED)
        )
        sandbox = db.get(ManagedSandbox, sandbox_id)
        assert sandbox is not None
        assert sandbox.desired_state == "DELETED"
        db.commit()

    worker._recover_startup()
    with db_session_factory() as db:
        recovered = db.scalar(
            select(BackgroundTask).where(
                BackgroundTask.aggregate_id == attempt_id,
                BackgroundTask.task_type == "CANCEL_RUNTIME",
                BackgroundTask.state == TaskState.PENDING,
            )
        )
        assert recovered is not None
        assert recovered.payload_json == {
            "recovery_mode": "DELETE_MANAGED_RUNTIME",
            "sandbox_ids": [sandbox_id],
        }
        assert recovered.max_attempts == 20
        sandbox = db.get(ManagedSandbox, sandbox_id)
        assert sandbox is not None
        db.delete(sandbox)
        recovered.available_at = datetime.now(UTC)
        db.commit()

    _run_worker_until(
        worker,
        lambda: worker_client.get(f"/api/v1/flow-runs/{run_id}").json()["node_runs"][0]["attempts"][
            0
        ]["runtime_phase"]
        == "CANCELLED",
    )
    final = worker_client.get(f"/api/v1/flow-runs/{run_id}").json()["node_runs"][0]["attempts"][0]
    assert final["runtime_phase"] == "CANCELLED"
    assert final["error_code"] is None
    with db_session_factory() as db:
        audit = db.scalar(
            select(RunEvent).where(
                RunEvent.attempt_id == attempt_id,
                RunEvent.event_type == "RUNTIME_SUBAGENT_EXECUTION_STOPPED_BY_SANDBOX_DELETION",
            )
        )
        assert audit is not None
        assert audit.payload_json["control_scope"] == "MANAGED_RUNTIME"
        assert audit.payload_json["sandbox_ids"] == [sandbox_id]


def test_managed_runtime_inflight_task_waits_for_physical_sandbox_deletion(
    worker_client, db_session_factory, worker_container, worker_skill_capability
):
    from flowweave.shared.models import (
        AgentConversation,
        BackgroundTask,
        ManagedSandbox,
        NodeAttempt,
        RunEvent,
        RuntimeSubagentTask,
    )

    worker, run_id, attempt_id = _prepare_starting_attempt(
        worker_client, worker_container, worker_skill_capability
    )
    assert worker._run_once_sync() is True  # START_RUNTIME
    from uuid import uuid4

    sandbox_id = str(uuid4())
    with db_session_factory() as db:
        attempt = db.get(NodeAttempt, attempt_id)
        conversation = db.scalar(
            select(AgentConversation).where(
                AgentConversation.attempt_id == attempt_id,
                AgentConversation.kind == "AUTO",
            )
        )
        assert attempt is not None
        assert conversation is not None
        sandbox = ManagedSandbox(
            id=sandbox_id,
            kind="AGENT_RUNTIME",
            owner_type="ATTEMPT",
            owner_id=attempt_id,
            backend="docker",
            backend_resource_name=f"runtime-{attempt_id}",
            image_reference="flowweave-openhands-runtime:1",
            observed_state="RUNNING",
            desired_state="RUNNING",
            hard_expires_at=datetime.now(UTC) + timedelta(hours=1),
        )
        db.add(sandbox)
        db.flush()
        attempt.runtime_sandbox_id = sandbox_id
        conversation.runtime_sandbox_id = sandbox_id
        db.add(
            RuntimeSubagentTask(
                attempt_id=attempt_id,
                conversation_id=conversation.id,
                action_event_id="cancel-managed-task-action",
                action_cursor="cancel-managed-task-action",
                tool_call_id="cancel-managed-tool-call",
                subagent_type="reviewer",
                state="REQUESTED",
            )
        )
        db.commit()

    current = worker_client.get(f"/api/v1/flow-runs/{run_id}").json()
    attempt = current["node_runs"][0]["attempts"][0]
    cancelled = worker_client.post(
        f"/api/v1/node-attempts/{attempt_id}/cancel",
        json={"expected_state_version": attempt["state_version"]},
        headers={"Idempotency-Key": "cancel-managed-inflight-task"},
    )
    assert cancelled.status_code == 200, cancelled.text
    _run_worker_until(
        worker,
        lambda: (
            worker_client.get(f"/api/v1/flow-runs/{run_id}").json()["node_runs"][0]["attempts"][0][
                "runtime_phase"
            ]
            == "CANCELLING"
            and _sandbox_desired_state(db_session_factory, sandbox_id) == "DELETED"
        ),
    )

    with db_session_factory() as db:
        attempt_row = db.get(NodeAttempt, attempt_id)
        sandbox = db.get(ManagedSandbox, sandbox_id)
        cancel_task = db.scalar(
            select(BackgroundTask).where(
                BackgroundTask.aggregate_id == attempt_id,
                BackgroundTask.task_type == "CANCEL_RUNTIME",
            )
        )
        assert attempt_row is not None
        assert sandbox is not None
        assert cancel_task is not None
        assert attempt_row.runtime_phase == "CANCELLING"
        assert sandbox.desired_state == "DELETED"
        assert cancel_task.state == TaskState.RETRY
        db.delete(sandbox)
        cancel_task.available_at = datetime.now(UTC)
        db.commit()

    assert worker._run_once_sync() is True  # deletion is now authoritative
    final = worker_client.get(f"/api/v1/flow-runs/{run_id}").json()
    assert final["node_runs"][0]["attempts"][0]["runtime_phase"] == "CANCELLED"
    with db_session_factory() as db:
        audit = db.scalar(
            select(RunEvent).where(
                RunEvent.attempt_id == attempt_id,
                RunEvent.event_type == "RUNTIME_SUBAGENT_EXECUTION_STOPPED_BY_SANDBOX_DELETION",
            )
        )
        assert audit is not None
        assert audit.payload_json["control_scope"] == "MANAGED_RUNTIME"
        assert audit.payload_json["sandbox_ids"] == [sandbox_id]
        assert audit.payload_json["pending_tasks"][0]["action_event_id"] == (
            "cancel-managed-task-action"
        )


def test_terminal_runtime_event_batch_skips_inspect_and_persists_events(
    worker_client, db_session_factory, worker_container, worker_skill_capability
):
    from flowweave.bootstrap.worker import TaskWorker
    from flowweave.runtime.base import (
        RuntimeEvent,
        RuntimeEventBatch,
        RuntimeHandle,
        RuntimeResult,
        StartAttemptRequest,
    )

    class EventTerminalRuntime:
        def start(self, _request: StartAttemptRequest) -> RuntimeHandle:
            raise AssertionError("runtime was already started by the previous adapter")

        def read_events(self, _handle: RuntimeHandle) -> RuntimeEventBatch:
            result = RuntimeResult(
                status="COMPLETED",
                outputs={
                    "design": (
                        "URL",
                        "https://example.feishu.cn/docx/event-result",
                    )
                },
                cursor="event-2",
            )
            return RuntimeEventBatch(
                events=(
                    RuntimeEvent("event-1", "MESSAGE", {"content": "working"}),
                    RuntimeEvent("event-2", "COMPLETED", {"reason": "done"}),
                ),
                cursor="event-2",
                result=result,
            )

        def inspect(self, _handle: RuntimeHandle) -> RuntimeResult:
            raise AssertionError("terminal event batches must not fall back to inspect")

        def resume(self, _handle: RuntimeHandle, _content: str) -> RuntimeResult:
            raise AssertionError("not used")

        def cancel(self, _handle: RuntimeHandle) -> None:
            raise AssertionError("not used")

    asset = worker_client.post(
        "/api/v1/node-assets", json=_asset_payload(worker_skill_capability)
    ).json()
    flow = worker_client.post(
        "/api/v1/flows",
        json={
            "name": "Runtime 事件流程",
            "lark_root_folder_url": "https://example.feishu.cn/drive/folder/task-root",
            "default_entry_key": "design",
            "nodes": [{"instance_key": "design", "node_asset_id": asset["id"]}],
        },
    ).json()
    started = worker_client.post(
        f"/api/v1/flows/{flow['id']}/runs",
        json={
            "flow_node_key": "design",
            "artifacts": [
                {
                    "field_key": "prd",
                    "artifact_type": "URL",
                    "uri": "https://example.feishu.cn/docx/event-input",
                }
            ],
        },
    ).json()
    attempt_id = started["node_runs"][0]["attempts"][0]["id"]
    worker = TaskWorker(worker_container)
    assert worker._run_once_sync() is True  # readiness
    assert worker._run_once_sync() is True  # empty START gates
    ready = worker_client.get(f"/api/v1/flow-runs/{started['id']}").json()
    worker_client.post(
        f"/api/v1/node-attempts/{attempt_id}/confirm-start",
        json={"expected_state_version": ready["node_runs"][0]["attempts"][0]["state_version"]},
        headers={"Idempotency-Key": "event-confirm"},
    )
    assert worker._run_once_sync() is True  # runtime start via MockRuntime

    previous_runtime = worker_container.runtime
    worker_container.runtime = EventTerminalRuntime()
    try:
        assert worker._run_once_sync() is True  # terminal event batch
    finally:
        worker_container.runtime = previous_runtime

    detail = worker_client.get(f"/api/v1/flow-runs/{started['id']}").json()
    attempt = detail["node_runs"][0]["attempts"][0]
    assert attempt["runtime_cursor"] == "event-2"
    assert attempt["runtime_phase"] == "COMPLETED"
    assert attempt["artifacts"][0]["inline_content"] is None
    assert attempt["artifacts"][0]["uri"].startswith("https://example.feishu.cn/docx/")
    history = worker_client.get(f"/api/v1/flow-runs/{started['id']}/event-history").json()
    runtime_events = [item for item in history if item["event_type"].startswith("RUNTIME_EVENT_")]
    assert [item["event_type"] for item in runtime_events] == [
        "RUNTIME_EVENT_MESSAGE",
        "RUNTIME_EVENT_COMPLETED",
    ]
    assert [item["payload"]["runtime_cursor"] for item in runtime_events] == [
        "event-1",
        "event-2",
    ]


@pytest.mark.parametrize(
    ("task_phase", "task_status", "parent_status", "expected_attempt_state"),
    [
        ("COMPLETED", "completed", "COMPLETED", "WAITING_ACCEPTANCE"),
        ("ERROR", "error", "FAILED", "END_BLOCKED"),
    ],
)
def test_terminal_task_waits_for_late_stats_before_closing_attempt(
    worker_client,
    db_session_factory,
    worker_container,
    worker_skill_capability,
    settings,
    task_phase,
    task_status,
    parent_status,
    expected_attempt_state,
):
    from flowweave.modules.orchestration.application.service import process_poll_runtime
    from flowweave.runtime.base import (
        RuntimeEvent,
        RuntimeEventBatch,
        RuntimeHandle,
        RuntimeResult,
        RuntimeTaskUsageSnapshot,
    )
    from flowweave.runtime.dependencies import runtime_context
    from flowweave.shared.models import (
        NodeAttempt,
        RunEvent,
        RuntimeSubagentTask,
        RuntimeSubagentTaskUsage,
    )
    from flowweave.shared.settings import settings_context

    worker, _run_id, attempt_id = _prepare_starting_attempt(
        worker_client, worker_container, worker_skill_capability
    )
    assert worker._run_once_sync() is True  # START_RUNTIME
    task_id = f"task_late_stats_{task_status}"
    observation_cursor = f"observation-{task_status}"
    calls = 0

    class RacingTaskStatsRuntime:
        def read_events(self, _handle: RuntimeHandle) -> RuntimeEventBatch:
            nonlocal calls
            calls += 1
            result = RuntimeResult(
                status=parent_status,
                outputs={"design": ("URL", "https://example.feishu.cn/docx/task-output")},
                cursor=f"terminal-{task_status}",
                error="parent failed" if parent_status == "FAILED" else None,
            )
            if calls == 1:
                return RuntimeEventBatch(
                    events=(
                        RuntimeEvent(
                            observation_cursor,
                            "TOOL_RESULT",
                            {
                                "content": "child result",
                                "runtime_task": {
                                    "phase": task_phase,
                                    "action_event_id": f"action-{task_status}",
                                    "observation_event_id": observation_cursor,
                                    "task_id": task_id,
                                    "subagent_type": "reviewer",
                                    "status": task_status,
                                },
                            },
                        ),
                    ),
                    cursor=observation_cursor,
                    result=result,
                )
            return RuntimeEventBatch(
                cursor=f"stats-{task_status}",
                result=result,
                task_usage=(
                    RuntimeTaskUsageSnapshot(
                        task_id=task_id,
                        source_cursor=f"stats-{task_status}",
                        digest=("a" if task_status == "completed" else "b") * 64,
                        model_name="openai/test-model",
                        accumulated_cost=0.25,
                        prompt_tokens=25,
                        completion_tokens=10,
                        cache_read_tokens=0,
                        cache_write_tokens=0,
                        reasoning_tokens=0,
                        context_window=4096,
                        per_turn_tokens=35,
                    ),
                ),
            )

        def inspect(self, _handle: RuntimeHandle) -> RuntimeResult:
            raise AssertionError("event batch already carries the terminal result")

    racing_runtime = RacingTaskStatsRuntime()
    with (
        runtime_context(racing_runtime),
        settings_context(settings),
        db_session_factory() as db,
    ):
        process_poll_runtime(db, attempt_id, 1)
    with db_session_factory() as db:
        attempt = db.get(NodeAttempt, attempt_id)
        assert attempt is not None
        assert attempt.state == "EXECUTING"
        assert attempt.runtime_phase == "RUNNING"
        assert db.scalar(select(RuntimeSubagentTaskUsage.id)) is None
        assert db.scalar(select(RuntimeSubagentTask.id)) is not None
        pending = list(
            db.scalars(
                select(RunEvent).where(
                    RunEvent.attempt_id == attempt_id,
                    RunEvent.event_type == "RUNTIME_SUBAGENT_USAGE_RECOVERY_PENDING",
                )
            )
        )
        assert len(pending) == 1
        assert pending[0].payload_json["runtime_task_ids"] == [task_id]

    with runtime_context(racing_runtime), settings_context(settings), db_session_factory() as db:
        process_poll_runtime(db, attempt_id, 2, task_usage_recovery_no=1)
    with db_session_factory() as db:
        attempt = db.get(NodeAttempt, attempt_id)
        usage = db.scalar(select(RuntimeSubagentTaskUsage))
        assert attempt is not None
        assert usage is not None
        assert attempt.state == expected_attempt_state
        assert attempt.runtime_phase == parent_status
        assert usage.runtime_task_id == task_id
        assert float(usage.accumulated_cost_usd) == 0.25
        assert usage.usage_version == 1
        assert (
            db.scalar(
                select(RuntimeSubagentTask.id).where(
                    RuntimeSubagentTask.conversation_id == usage.conversation_id,
                    RuntimeSubagentTask.runtime_task_id == task_id,
                )
            )
            == usage.runtime_subagent_task_id
        )


def test_terminal_task_usage_recovery_exhaustion_fails_closed(
    worker_client, db_session_factory, worker_container, worker_skill_capability, settings
):
    from flowweave.modules.orchestration.application.service import process_poll_runtime
    from flowweave.runtime.base import RuntimeEvent, RuntimeEventBatch, RuntimeHandle, RuntimeResult
    from flowweave.runtime.dependencies import runtime_context
    from flowweave.shared.models import NodeAttempt, RunEvent
    from flowweave.shared.settings import settings_context

    worker, _run_id, attempt_id = _prepare_starting_attempt(
        worker_client, worker_container, worker_skill_capability
    )
    assert worker._run_once_sync() is True  # START_RUNTIME

    class MissingTaskStatsRuntime:
        def read_events(self, _handle: RuntimeHandle) -> RuntimeEventBatch:
            return RuntimeEventBatch(
                events=(
                    RuntimeEvent(
                        "missing-stats-observation",
                        "TOOL_RESULT",
                        {
                            "runtime_task": {
                                "phase": "COMPLETED",
                                "action_event_id": "missing-stats-action",
                                "observation_event_id": "missing-stats-observation",
                                "task_id": "task_missing_stats",
                                "subagent_type": "reviewer",
                                "status": "completed",
                            }
                        },
                    ),
                ),
                cursor="missing-stats-observation",
                result=RuntimeResult(
                    status="COMPLETED",
                    outputs={
                        "design": ("URL", "https://example.feishu.cn/docx/missing-stats-output")
                    },
                    cursor="missing-stats-observation",
                ),
            )

        def inspect(self, _handle: RuntimeHandle) -> RuntimeResult:
            raise AssertionError("event batch already carries the terminal result")

    with (
        runtime_context(MissingTaskStatsRuntime()),
        settings_context(settings),
        db_session_factory() as db,
    ):
        process_poll_runtime(
            db,
            attempt_id,
            settings.runtime_task_usage_visibility_max_polls,
            task_usage_recovery_no=settings.runtime_task_usage_visibility_max_polls - 1,
        )
    with db_session_factory() as db:
        attempt = db.get(NodeAttempt, attempt_id)
        assert attempt is not None
        assert attempt.state == "END_BLOCKED"
        assert attempt.runtime_phase == "FAILED"
        assert attempt.error_code == "RUNTIME_TASK_USAGE_UNAVAILABLE"
        exhausted = list(
            db.scalars(
                select(RunEvent).where(
                    RunEvent.attempt_id == attempt_id,
                    RunEvent.event_type == "RUNTIME_SUBAGENT_USAGE_RECOVERY_EXHAUSTED",
                )
            )
        )
        assert len(exhausted) == 1
        assert exhausted[0].payload_json["runtime_task_ids"] == ["task_missing_stats"]


def test_terminal_task_stats_visible_before_observation_waits_for_formal_identity(
    worker_client,
    db_session_factory,
    worker_container,
    worker_skill_capability,
    settings,
):
    from flowweave.modules.orchestration.application.service import process_poll_runtime
    from flowweave.runtime.base import (
        RuntimeEvent,
        RuntimeEventBatch,
        RuntimeHandle,
        RuntimeResult,
        RuntimeTaskUsageSnapshot,
    )
    from flowweave.runtime.dependencies import runtime_context
    from flowweave.shared.models import NodeAttempt, RuntimeSubagentTaskUsage
    from flowweave.shared.settings import settings_context

    worker, _run_id, attempt_id = _prepare_starting_attempt(
        worker_client, worker_container, worker_skill_capability
    )
    assert worker._run_once_sync() is True  # START_RUNTIME
    calls = 0
    usage = RuntimeTaskUsageSnapshot(
        task_id="task_stats_first",
        source_cursor="stats-first",
        digest="c" * 64,
        model_name="openai/test-model",
        accumulated_cost=0.1,
        prompt_tokens=10,
        completion_tokens=5,
        cache_read_tokens=0,
        cache_write_tokens=0,
        reasoning_tokens=0,
        context_window=4096,
        per_turn_tokens=15,
    )

    class StatsFirstRuntime:
        def read_events(self, _handle: RuntimeHandle) -> RuntimeEventBatch:
            nonlocal calls
            calls += 1
            events = (
                ()
                if calls == 1
                else (
                    RuntimeEvent(
                        "stats-first-observation",
                        "TOOL_RESULT",
                        {
                            "runtime_task": {
                                "phase": "COMPLETED",
                                "action_event_id": "stats-first-action",
                                "observation_event_id": "stats-first-observation",
                                "task_id": usage.task_id,
                                "subagent_type": "reviewer",
                                "status": "completed",
                            }
                        },
                    ),
                )
            )
            return RuntimeEventBatch(
                events=events,
                cursor=f"stats-first-{calls}",
                result=RuntimeResult(
                    status="COMPLETED",
                    outputs={"design": ("URL", "https://example.feishu.cn/docx/stats-first")},
                    cursor=f"stats-first-{calls}",
                ),
                task_usage=(usage,),
            )

        def inspect(self, _handle: RuntimeHandle) -> RuntimeResult:
            raise AssertionError("event batch already carries the terminal result")

    runtime = StatsFirstRuntime()
    with runtime_context(runtime), settings_context(settings), db_session_factory() as db:
        process_poll_runtime(db, attempt_id, 1)
    with db_session_factory() as db:
        attempt = db.get(NodeAttempt, attempt_id)
        assert attempt is not None
        assert attempt.state == "EXECUTING"
        assert db.scalar(select(RuntimeSubagentTaskUsage.id)) is None

    with runtime_context(runtime), settings_context(settings), db_session_factory() as db:
        process_poll_runtime(db, attempt_id, 2, task_usage_recovery_no=1)
    with db_session_factory() as db:
        attempt = db.get(NodeAttempt, attempt_id)
        projected = db.scalar(select(RuntimeSubagentTaskUsage))
        assert attempt is not None
        assert attempt.state == "WAITING_ACCEPTANCE"
        assert projected is not None
        assert projected.runtime_task_id == usage.task_id
        assert projected.usage_version == 1


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
            "lark_root_folder_url": "https://example.feishu.cn/drive/folder/task-root",
            "default_entry_key": "design",
            "nodes": [{"instance_key": "design", "node_asset_id": asset["id"]}],
        },
    ).json()
    started = worker_client.post(
        f"/api/v1/flows/{flow['id']}/runs",
        json={
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
            "lark_root_folder_url": "https://example.feishu.cn/drive/folder/task-root",
            "default_entry_key": "design",
            "nodes": [{"instance_key": "design", "node_asset_id": asset["id"]}],
        },
    ).json()
    started = worker_client.post(
        f"/api/v1/flows/{flow['id']}/runs",
        json={
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
    assert attempt["runtime_cursor"] != "late-1"

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
            "lark_root_folder_url": "https://example.feishu.cn/drive/folder/task-root",
            "default_entry_key": "design",
            "nodes": [{"instance_key": "design", "node_asset_id": asset["id"]}],
        },
    ).json()
    started = worker_client.post(
        f"/api/v1/flows/{flow['id']}/runs",
        json={
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
    worker_client, db_session_factory, worker_container, worker_skill_capability
):
    """Gate I/O holds no DB transaction and CAS discards a concurrently stale result."""

    from concurrent.futures import ThreadPoolExecutor
    from threading import Event

    from sqlalchemy import func, update

    from flowweave.bootstrap.worker import TaskWorker
    from flowweave.modules.orchestration.public import process_gate_stage
    from flowweave.modules.tasks.application.service import claim, succeed
    from flowweave.shared.application.sandbox import SandboxExecution
    from flowweave.shared.models import GateEvaluation, NodeAttempt, RunEvent
    from flowweave.shared.sandbox import sandbox_context

    asset = worker_client.post(
        "/api/v1/node-assets", json=_asset_payload(worker_skill_capability)
    ).json()
    flow = worker_client.post(
        "/api/v1/flows",
        json={
            "name": "Gate CAS 短事务流程",
            "lark_root_folder_url": "https://example.feishu.cn/drive/folder/task-root",
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
                                "code": (
                                    "result = {'decision': 'PASS', 'summary': 'ok', "
                                    "'reasons': [], 'evidence': [], 'details': {}}"
                                )
                            },
                        }
                    ],
                }
            ],
        },
    ).json()
    started = worker_client.post(
        f"/api/v1/flows/{flow['id']}/runs",
        json={
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

        class BlockingSandbox:
            def execute(self, _language, _code, _context, _timeout_seconds):
                transaction_states.append(db.in_transaction())
                entered.set()
                assert release.wait(timeout=5)
                return SandboxExecution(
                    "COMPLETED",
                    result={
                        "decision": "PASS",
                        "summary": "late pass",
                        "reasons": [],
                        "evidence": [],
                        "details": {},
                    },
                )

        def execute_gate() -> None:
            with sandbox_context(BlockingSandbox()):
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
