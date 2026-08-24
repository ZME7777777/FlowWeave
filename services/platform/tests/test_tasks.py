import base64
import hashlib
import io
import json
import zipfile
from datetime import UTC, datetime, timedelta
from time import sleep

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
            "environment_version_id": worker_client.environment_version_id,
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
    assert "runtime_job_id" not in queued

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
    assert "runtime_job_id" not in attempt
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
            "environment_version_id": worker_client.environment_version_id,
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
    assert frozen.source.startswith("/runtime/capabilities/")
    assert "/nodes/" in frozen.source


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
            "environment_version_id": worker_client.environment_version_id,
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
            "environment_version_id": worker_client.environment_version_id,
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
            "environment_version_id": worker_client.environment_version_id,
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
            "environment_version_id": worker_client.environment_version_id,
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
