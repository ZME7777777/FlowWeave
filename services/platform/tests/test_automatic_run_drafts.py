from sqlalchemy import delete, select

from flowweave.modules.gates.public import GateResult
from flowweave.modules.orchestration.application import service as orchestration_service
from flowweave.shared.models import (
    BackgroundTask,
    FlowRun,
    FlowRunRuntime,
    FlowRunRuntimeAllocation,
    NodeAttempt,
    NodeRun,
    TaskState,
)


def _create_flow(client):
    asset = client.post(
        "/api/v1/node-assets",
        json={
            "name": "自动运行节点",
            "inputs": [
                {
                    "field_key": "source",
                    "display_name": "来源",
                    "data_type": "URL",
                }
            ],
            "outputs": [
                {
                    "field_key": "result",
                    "display_name": "结果",
                    "data_type": "URL",
                }
            ],
            "executor": {"startup_prompt": "处理当前节点"},
        },
    )
    assert asset.status_code == 201, asset.text
    flow = client.post(
        "/api/v1/flows",
        json={
            "name": "自动运行草稿流程",
            "nodes": [
                {"instance_key": "first", "node_asset_id": asset.json()["id"]},
                {"instance_key": "second", "node_asset_id": asset.json()["id"]},
            ],
            "edges": [{"source_instance_key": "first", "target_instance_key": "second"}],
            "port_mappings": [
                {
                    "source_instance_key": "first",
                    "source_output_key": "result",
                    "target_instance_key": "second",
                    "target_input_key": "source",
                }
            ],
        },
    )
    assert flow.status_code == 201, flow.text
    return flow.json()


def _node_plan(
    prompt: str,
    *,
    input_url: str | None = None,
    artifact_id: str | None = None,
    capability_version_ids: list[str] | None = None,
    model_provider_id: str | None = None,
    model_name: str | None = None,
):
    return {
        "startup_prompt": prompt,
        "agent_preset": {
            "capability_version_ids": capability_version_ids or [],
            "model_provider_id": model_provider_id,
            "model_name": model_name,
            "node_context_enabled": False,
        },
        "gates": [],
        "artifact_ids": {"source": artifact_id} if artifact_id else {},
        "input_urls": {"source": input_url} if input_url else {},
    }


def test_automatic_run_draft_freezes_snapshot_without_runtime_or_node_runs(
    client, db_session_factory
):
    flow = _create_flow(client)
    created = client.post(
        f"/api/v1/flows/{flow['id']}/automatic-runs",
        json={
            "name": "首个自动编排",
            "environment_version_id": client.environment_version_id,
            "start_node_key": "first",
            "node_plans": {
                "first": _node_plan("执行第一个节点", input_url="https://example.com/source")
            },
        },
    )
    assert created.status_code == 201, created.text
    draft = created.json()
    assert draft["run_mode"] == "AUTOMATIC"
    assert draft["state"] == "DRAFT"
    assert draft["node_runs"] == []
    assert draft["automation_plan"]["start_node_key"] == "first"
    assert draft["automation_plan"]["reachable_node_keys"] == ["first", "second"]
    assert draft["automation_plan"]["readiness"] == {
        "ready": False,
        "issues": [
            {
                "code": "NODE_PLAN_REQUIRED",
                "node_key": "second",
                "message": "请配置此节点的自动执行预设",
            }
        ],
    }
    assert draft["snapshots"][0]["definition"]["nodes"][0]["asset"]["name"] == "自动运行节点"

    with db_session_factory() as db:
        assert db.scalar(select(NodeRun).where(NodeRun.flow_run_id == draft["id"])) is None
        assert (
            db.scalar(
                select(FlowRunRuntimeAllocation).where(
                    FlowRunRuntimeAllocation.flow_run_id == draft["id"]
                )
            )
            is None
        )
        assert (
            db.scalar(select(FlowRunRuntime).where(FlowRunRuntime.flow_run_id == draft["id"]))
            is None
        )

    listed = client.get("/api/v1/flow-runs")
    assert listed.status_code == 200, listed.text
    summary = next(item for item in listed.json() if item["id"] == draft["id"])
    assert summary["run_mode"] == "AUTOMATIC"
    assert summary["runtime_status"] == "DRAFT"
    assert summary["runtime_write_available"] is False


def test_automatic_run_draft_can_be_edited_but_not_manually_activated(client, db_session_factory):
    flow = _create_flow(client)
    created = client.post(
        f"/api/v1/flows/{flow['id']}/automatic-runs",
        json={
            "environment_version_id": client.environment_version_id,
            "start_node_key": "first",
            "node_plans": {"first": _node_plan("执行第一个节点")},
        },
    )
    assert created.status_code == 201, created.text
    draft = created.json()

    updated = client.put(
        f"/api/v1/automatic-runs/{draft['id']}",
        json={
            "expected_row_version": draft["row_version"],
            "name": "已补全自动编排",
            "start_node_key": "first",
            "node_plans": {
                "first": _node_plan("执行第一个节点", input_url="https://example.com/source"),
                "second": _node_plan("执行第二个节点"),
            },
        },
    )
    assert updated.status_code == 200, updated.text
    saved = updated.json()
    assert saved["row_version"] == draft["row_version"] + 1
    assert saved["name"] == "已补全自动编排"
    assert saved["automation_plan"]["readiness"] == {"ready": True, "issues": []}

    stale = client.put(
        f"/api/v1/automatic-runs/{draft['id']}",
        json={
            "expected_row_version": draft["row_version"],
            "start_node_key": "first",
            "node_plans": {},
        },
    )
    assert stale.status_code == 409, stale.text

    manual_start = client.post(
        f"/api/v1/flow-runs/{draft['id']}/nodes/first/runs",
        json={
            "startup_mode": "PROMPT",
            "agent_preset": {
                "capability_version_ids": [],
                "node_context_enabled": False,
            },
        },
    )
    assert manual_start.status_code == 409, manual_start.text
    sync_snapshot = client.post(
        f"/api/v1/flow-runs/{draft['id']}/sync-snapshot",
        json={"expected_active_version": 1},
    )
    assert sync_snapshot.status_code == 409, sync_snapshot.text
    complete = client.post(f"/api/v1/flow-runs/{draft['id']}/complete")
    assert complete.status_code == 409, complete.text
    with db_session_factory() as db:
        persisted = db.get(FlowRun, draft["id"])
        assert persisted is not None and persisted.run_mode == "AUTOMATIC"
        assert db.scalar(select(NodeRun).where(NodeRun.flow_run_id == draft["id"])) is None


def test_new_standard_flow_run_remains_manual(client):
    flow = _create_flow(client)
    created = client.post(
        f"/api/v1/flows/{flow['id']}/runs",
        json={"environment_version_id": client.environment_version_id},
    )
    assert created.status_code == 201, created.text
    assert created.json()["run_mode"] == "MANUAL"
    assert created.json()["automation_plan"] is None


def test_nested_automatic_records_are_scoped_and_share_parent_runtime(
    worker_client, worker_container, db_session_factory
):
    from flowweave.bootstrap.worker import TaskWorker

    flow = _create_flow(worker_client)
    parent_response = worker_client.post(
        f"/api/v1/flows/{flow['id']}/runs",
        json={"name": "父流程运行", "environment_version_id": worker_client.environment_version_id},
    )
    assert parent_response.status_code == 201, parent_response.text
    parent = parent_response.json()

    created_response = worker_client.post(
        f"/api/v1/flow-runs/{parent['id']}/automatic-runs",
        json={
            "name": "内部自动记录",
            "environment_version_id": worker_client.environment_version_id,
            "start_node_key": "first",
            "node_plans": {"first": _node_plan("先执行起点")},
        },
    )
    assert created_response.status_code == 201, created_response.text
    draft = created_response.json()
    assert draft["parent_flow_run_id"] == parent["id"]
    assert draft["automation_plan"]["readiness"]["ready"] is False

    outer = worker_client.get("/api/v1/flow-runs")
    assert outer.status_code == 200, outer.text
    outer_ids = {item["id"] for item in outer.json()}
    assert parent["id"] in outer_ids
    assert draft["id"] not in outer_ids

    nested = worker_client.get(f"/api/v1/flow-runs/{parent['id']}/automatic-runs")
    assert nested.status_code == 200, nested.text
    assert [item["id"] for item in nested.json()] == [draft["id"]]

    updated_response = worker_client.put(
        f"/api/v1/flow-runs/{parent['id']}/automatic-runs/{draft['id']}",
        json={
            "expected_row_version": draft["row_version"],
            "name": "已就绪自动记录",
            "start_node_key": "first",
            "node_plans": {
                "first": _node_plan(
                    "执行起点", input_url="https://example.com/nested-input"
                ),
                "second": _node_plan("执行下游"),
            },
        },
    )
    assert updated_response.status_code == 200, updated_response.text
    updated = updated_response.json()
    assert updated["automation_plan"]["readiness"] == {"ready": True, "issues": []}

    wrong_parent = worker_client.get(
        f"/api/v1/flow-runs/{draft['id']}/automatic-runs"
    )
    assert wrong_parent.status_code == 200, wrong_parent.text
    assert wrong_parent.json() == []
    cross_parent_update = worker_client.put(
        f"/api/v1/flow-runs/{draft['id']}/automatic-runs/{updated['id']}",
        json={
            "expected_row_version": updated["row_version"],
            "name": "越权更新",
            "start_node_key": "first",
            "node_plans": {
                "first": _node_plan(
                    "执行起点", input_url="https://example.com/nested-input"
                ),
                "second": _node_plan("执行下游"),
            },
        },
    )
    assert cross_parent_update.status_code == 404, cross_parent_update.text

    started_response = worker_client.post(
        f"/api/v1/flow-runs/{parent['id']}/automatic-runs/{updated['id']}/start",
        json={"expected_row_version": updated["row_version"]},
        headers={"Idempotency-Key": "start-nested-record"},
    )
    assert started_response.status_code == 200, started_response.text
    assert started_response.json()["state"] == "ACTIVE"

    worker = TaskWorker(worker_container)
    assert worker._run_once_sync() is True
    detail = worker_client.get(f"/api/v1/flow-runs/{updated['id']}").json()
    assert [item["flow_node_snapshot_key"] for item in detail["node_runs"]] == ["first"]

    with db_session_factory() as db:
        parent_runtime = db.scalar(
            select(FlowRunRuntime).where(FlowRunRuntime.flow_run_id == parent["id"])
        )
        parent_allocation = db.scalar(
            select(FlowRunRuntimeAllocation).where(
                FlowRunRuntimeAllocation.flow_run_id == parent["id"]
            )
        )
        child_runtime = db.scalar(
            select(FlowRunRuntime).where(FlowRunRuntime.flow_run_id == updated["id"])
        )
        child_allocation = db.scalar(
            select(FlowRunRuntimeAllocation).where(
                FlowRunRuntimeAllocation.flow_run_id == updated["id"]
            )
        )
        assert parent_runtime is not None
        assert parent_allocation is not None
        assert child_runtime is None
        assert child_allocation is None

    disposable_response = worker_client.post(
        f"/api/v1/flow-runs/{parent['id']}/automatic-runs",
        json={
            "name": "待删除记录",
            "environment_version_id": worker_client.environment_version_id,
            "start_node_key": "first",
            "node_plans": {},
        },
    )
    assert disposable_response.status_code == 201, disposable_response.text
    disposable = disposable_response.json()
    deleted = worker_client.delete(
        f"/api/v1/flow-runs/{parent['id']}/automatic-runs/{disposable['id']}"
    )
    assert deleted.status_code == 204, deleted.text
    assert worker_client.get(f"/api/v1/flow-runs/{parent['id']}").status_code == 200
    with db_session_factory() as db:
        assert db.get(FlowRun, disposable["id"]) is None
        assert db.scalar(
            select(FlowRunRuntime).where(FlowRunRuntime.flow_run_id == parent["id"])
        ) is not None


def test_automatic_run_draft_rejects_unknown_frozen_nodes(client):
    flow = _create_flow(client)
    unknown_start = client.post(
        f"/api/v1/flows/{flow['id']}/automatic-runs",
        json={
            "environment_version_id": client.environment_version_id,
            "start_node_key": "missing",
            "node_plans": {},
        },
    )
    assert unknown_start.status_code == 404, unknown_start.text

    unknown_plan = client.post(
        f"/api/v1/flows/{flow['id']}/automatic-runs",
        json={
            "environment_version_id": client.environment_version_id,
            "start_node_key": "first",
            "node_plans": {"missing": _node_plan("非法节点")},
        },
    )
    assert unknown_plan.status_code == 422, unknown_plan.text


def test_automatic_run_draft_freezes_artifact_and_capability_references(client, skill_capability):
    flow = _create_flow(client)
    provider = client.post(
        "/api/v1/model-providers",
        json={
            "name": "自动编排模型",
            "base_url": "https://models.example.test/v1",
            "models": [{"model_name": "gpt-auto", "enabled": True, "is_default": True}],
        },
    )
    assert provider.status_code == 201, provider.text
    created = client.post(
        f"/api/v1/flows/{flow['id']}/automatic-runs",
        json={
            "environment_version_id": client.environment_version_id,
            "start_node_key": "first",
            "node_plans": {},
        },
    )
    assert created.status_code == 201, created.text
    draft = created.json()
    artifact = client.post(
        f"/api/v1/flow-runs/{draft['id']}/nodes/first/input-artifacts",
        json={
            "field_key": "source",
            "artifact_type": "URL",
            "uri": "https://example.com/frozen-source",
        },
    )
    assert artifact.status_code == 201, artifact.text

    updated = client.put(
        f"/api/v1/automatic-runs/{draft['id']}",
        json={
            "expected_row_version": draft["row_version"],
            "start_node_key": "first",
            "node_plans": {
                "first": _node_plan(
                    "执行第一个节点",
                    artifact_id=artifact.json()["id"],
                    capability_version_ids=[skill_capability["capability_id"]],
                    model_provider_id=provider.json()["id"],
                    model_name="gpt-auto",
                )
            },
        },
    )
    assert updated.status_code == 200, updated.text
    frozen = updated.json()["automation_plan"]["node_plans"]["first"]
    assert frozen["artifact_ids"] == {"source": artifact.json()["id"]}
    assert frozen["agent_preset"]["capability_version_ids"] == [skill_capability["capability_id"]]

    artifact_delete = client.delete(
        f"/api/v1/flow-runs/{draft['id']}/artifacts/{artifact.json()['id']}"
    )
    assert artifact_delete.status_code == 409, artifact_delete.text
    capability_delete = client.delete(f"/api/v1/capabilities/{skill_capability['capability_id']}")
    assert capability_delete.status_code == 409, capability_delete.text
    provider_delete = client.delete(f"/api/v1/model-providers/{provider.json()['id']}")
    assert provider_delete.status_code == 409, provider_delete.text


def test_automatic_run_starts_ready_plan_and_completes_frozen_chain(
    worker_client, worker_container, db_session_factory
):
    from flowweave.bootstrap.worker import TaskWorker

    flow = _create_flow(worker_client)
    created = worker_client.post(
        f"/api/v1/flows/{flow['id']}/automatic-runs",
        json={
            "environment_version_id": worker_client.environment_version_id,
            "start_node_key": "first",
            "node_plans": {
                "first": _node_plan("自动执行第一个节点", input_url="https://example.com/input"),
                # `second.source` is supplied exclusively through the frozen
                # first.result -> second.source port mapping.
                "second": _node_plan("自动执行第二个节点"),
            },
        },
    )
    assert created.status_code == 201, created.text
    draft = created.json()
    assert draft["automation_plan"]["readiness"] == {"ready": True, "issues": []}

    started = worker_client.post(
        f"/api/v1/automatic-runs/{draft['id']}/start",
        json={"expected_row_version": draft["row_version"]},
        headers={"Idempotency-Key": "start-automatic-chain"},
    )
    assert started.status_code == 200, started.text
    run = started.json()
    assert run["state"] == "ACTIVE"
    assert run["automation_plan"]["status"] == "FROZEN"
    assert run["node_runs"] == []
    with db_session_factory() as db:
        assert (
            db.scalar(select(FlowRunRuntime).where(FlowRunRuntime.flow_run_id == run["id"])) is None
        )
        tasks = list(
            db.scalars(
                select(BackgroundTask).where(
                    BackgroundTask.aggregate_id == run["id"],
                    BackgroundTask.task_type == "START_AUTOMATIC_RUN",
                )
            )
        )
        assert len(tasks) == 1
    worker = TaskWorker(worker_container)
    for _ in range(24):
        run = worker_client.get(f"/api/v1/flow-runs/{run['id']}").json()
        if run["state"] == "COMPLETED":
            break
        assert worker._run_once_sync() is True
    assert run["state"] == "COMPLETED"
    assert run["automation_plan"]["status"] == "FROZEN"
    assert [item["flow_node_snapshot_key"] for item in run["node_runs"]] == ["first", "second"]
    assert all(item["state"] == "ACCEPTED" for item in run["node_runs"])
    first, second = run["node_runs"]
    assert first["attempts"][0]["state"] == "ACCEPTED"
    assert second["attempts"][0]["state"] == "ACCEPTED"
    first_output = first["attempts"][0]["artifacts"][0]["id"]
    bindings = second["attempts"][0]["input_bindings"]
    assert len(bindings) == 1
    binding_summary = {
        key: bindings[0][key]
        for key in ("input_field_key", "artifact_version_id", "binding_source")
    }
    assert binding_summary == {
        "input_field_key": "source",
        "artifact_version_id": first_output,
        "binding_source": "AUTOMATIC_PORT_MAPPING",
    }

    events = worker_client.get(f"/api/v1/flow-runs/{run['id']}/event-history").json()
    event_types = {event["event_type"] for event in events}
    assert {
        "AUTOMATIC_RUN_STARTED",
        "AUTOMATIC_ATTEMPT_STARTED",
        "AUTOMATIC_NODE_ACCEPTED",
        "AUTOMATIC_DOWNSTREAM_AVAILABLE",
    } <= event_types

    # Starting is one-way; stale and repeated starts cannot create additional work.
    again = worker_client.post(
        f"/api/v1/automatic-runs/{draft['id']}/start",
        json={"expected_row_version": draft["row_version"]},
    )
    assert again.status_code == 409, again.text


def test_automatic_run_rejects_start_when_required_unmapped_input_is_missing(client):
    flow = _create_flow(client)
    created = client.post(
        f"/api/v1/flows/{flow['id']}/automatic-runs",
        json={
            "environment_version_id": client.environment_version_id,
            "start_node_key": "first",
            "node_plans": {
                "first": _node_plan("缺输入的起点"),
                "second": _node_plan("下游"),
            },
        },
    )
    assert created.status_code == 201, created.text
    draft = created.json()
    assert draft["automation_plan"]["readiness"] == {
        "ready": False,
        "issues": [
            {
                "code": "NODE_INPUT_REQUIRED",
                "node_key": "first",
                "message": "请配置未映射输入：source",
            }
        ],
    }
    start = client.post(
        f"/api/v1/automatic-runs/{draft['id']}/start",
        json={"expected_row_version": draft["row_version"]},
    )
    assert start.status_code == 422, start.text
    assert start.json()["error"]["code"] == "AUTOMATION_PLAN_NOT_READY"


def _started_automatic_attempt(worker_client, worker_container):
    from flowweave.bootstrap.worker import TaskWorker

    flow = _create_flow(worker_client)
    created = worker_client.post(
        f"/api/v1/flows/{flow['id']}/automatic-runs",
        json={
            "environment_version_id": worker_client.environment_version_id,
            "start_node_key": "first",
            "node_plans": {
                "first": _node_plan("自动执行第一个节点", input_url="https://example.com/input"),
                "second": _node_plan("自动执行第二个节点"),
            },
        },
    ).json()
    started = worker_client.post(
        f"/api/v1/automatic-runs/{created['id']}/start",
        json={"expected_row_version": created["row_version"]},
        headers={"Idempotency-Key": f"start:{created['id']}"},
    ).json()
    worker = TaskWorker(worker_container)
    assert worker._run_once_sync() is True
    detail = worker_client.get(f"/api/v1/flow-runs/{started['id']}").json()
    return worker, detail["id"], detail["node_runs"][0]["attempts"][0]["id"]


def test_automatic_transition_rejects_unauthorized_agent_selection(
    worker_client, worker_container, db_session_factory, monkeypatch
):
    worker, run_id, attempt_id = _started_automatic_attempt(worker_client, worker_container)
    for _ in range(12):
        detail = worker_client.get(f"/api/v1/flow-runs/{run_id}").json()
        if detail["node_runs"][0]["attempts"][0]["state"] == "WAITING_ACCEPTANCE":
            break
        assert worker._run_once_sync() is True
    else:
        raise AssertionError("automatic attempt did not reach transition decision")

    monkeypatch.setattr(
        orchestration_service,
        "execute_gate_plan",
        lambda _plan, _context: GateResult(
            "PASS",
            "unauthorized",
            [],
            [],
            {"selected_node_keys": ["outside-frozen-topology"]},
        ),
    )
    assert worker._run_once_sync() is True

    detail = worker_client.get(f"/api/v1/flow-runs/{run_id}").json()
    assert detail["state"] == "WAITING_HUMAN"
    assert [item["flow_node_snapshot_key"] for item in detail["node_runs"]] == ["first"]
    attempt = detail["node_runs"][0]["attempts"][0]
    assert attempt["id"] == attempt_id
    assert attempt["state"] == "END_BLOCKED"
    assert attempt["error_code"] == "AUTOMATIC_TRANSITION_INVALID"
    with db_session_factory() as db:
        assert (
            db.scalar(
                select(NodeRun.id).where(
                    NodeRun.flow_run_id == run_id,
                    NodeRun.flow_node_snapshot_key == "second",
                )
            )
            is None
        )


def test_automatic_attempt_delivery_recovery_covers_every_scheduler_stage(
    worker_client, worker_container, db_session_factory
):
    _worker, _run_id, attempt_id = _started_automatic_attempt(worker_client, worker_container)
    stages = [
        ("WAITING_INPUT", "EVALUATE_READINESS", {}),
        ("START_GATES", "RUN_GATE_POLICY", {"stage": "START"}),
        ("WAITING_START_CONFIRMATION", "START_AUTOMATIC_ATTEMPT", {}),
        ("END_GATES", "RUN_GATE_POLICY", {"stage": "END"}),
        ("WAITING_ACCEPTANCE", "ADVANCE_AUTOMATIC_ATTEMPT", {}),
    ]
    for index, (state, task_type, payload) in enumerate(stages, start=1):
        with db_session_factory() as db:
            db.execute(delete(BackgroundTask).where(BackgroundTask.aggregate_id == attempt_id))
            attempt = db.get(NodeAttempt, attempt_id)
            assert attempt is not None
            attempt.state = state
            attempt.state_version = 100 + index
            attempt.error_code = None
            attempt.error_detail = None
            db.commit()

        with db_session_factory() as db:
            assert orchestration_service.recover_runtime_tasks(db) >= 1

        with db_session_factory() as db:
            recovered = db.scalar(
                select(BackgroundTask).where(
                    BackgroundTask.aggregate_id == attempt_id,
                    BackgroundTask.task_type == task_type,
                    BackgroundTask.state == TaskState.PENDING,
                )
            )
            assert recovered is not None
            assert recovered.payload_json == payload


def test_automatic_terminal_task_failure_becomes_visible_block(
    worker_client, worker_container, db_session_factory
):
    _worker, run_id, attempt_id = _started_automatic_attempt(worker_client, worker_container)
    with db_session_factory() as db:
        attempt = db.get(NodeAttempt, attempt_id)
        assert attempt is not None
        attempt.state = "WAITING_START_CONFIRMATION"
        orchestration_service.record_automatic_task_failure(
            db, attempt_id, "START_AUTOMATIC_ATTEMPT", {}, "scheduler exhausted retries"
        )
        db.commit()

    detail = worker_client.get(f"/api/v1/flow-runs/{run_id}").json()
    assert detail["state"] == "WAITING_HUMAN"
    attempt = detail["node_runs"][0]["attempts"][0]
    assert attempt["state"] == "START_BLOCKED"
    assert attempt["error_code"] == "AUTOMATIC_START_DELIVERY_FAILED"
    assert attempt["error_detail"] == "scheduler exhausted retries"


def test_automatic_runtime_delivery_failure_cannot_be_retried_as_a_gate(
    worker_client, worker_container, db_session_factory
):
    _worker, run_id, attempt_id = _started_automatic_attempt(worker_client, worker_container)
    with db_session_factory() as db:
        attempt = db.get(NodeAttempt, attempt_id)
        assert attempt is not None
        attempt.state = "EXECUTING"
        attempt.runtime_phase = "RUNNING"
        orchestration_service.record_automatic_task_failure(
            db, attempt_id, "POLL_RUNTIME", {}, "runtime delivery exhausted retries"
        )
        db.commit()

    detail = worker_client.get(f"/api/v1/flow-runs/{run_id}").json()
    attempt = detail["node_runs"][0]["attempts"][0]
    assert attempt["state"] == "END_BLOCKED"
    assert attempt["error_code"] == "AUTOMATIC_RUNTIME_DELIVERY_FAILED"

    retried = worker_client.post(
        f"/api/v1/node-attempts/{attempt_id}/retry-gates",
        json={"expected_state_version": attempt["state_version"]},
    )
    assert retried.status_code == 409, retried.text
    assert retried.json()["error"]["details"]["error_code"] == ("AUTOMATIC_RUNTIME_DELIVERY_FAILED")
