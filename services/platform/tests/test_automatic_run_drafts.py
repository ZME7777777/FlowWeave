import base64
from datetime import timedelta

from sqlalchemy import delete, select

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


def _create_flow(client, *, context_prompt: str = ""):
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
            "executor": {
                "startup_prompt": "处理当前节点",
                "context_prompt": context_prompt,
            },
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


def _create_fanout_flow(client):
    asset = client.post(
        "/api/v1/node-assets",
        json={
            "name": "自动扇出节点",
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
            "name": "自动运行扇出流程",
            "nodes": [
                {"instance_key": "first", "node_asset_id": asset.json()["id"]},
                {"instance_key": "second", "node_asset_id": asset.json()["id"]},
                {"instance_key": "third", "node_asset_id": asset.json()["id"]},
            ],
            "edges": [
                {"source_instance_key": "first", "target_instance_key": "second"},
                {"source_instance_key": "first", "target_instance_key": "third"},
            ],
            "port_mappings": [
                {
                    "source_instance_key": "first",
                    "source_output_key": "result",
                    "target_instance_key": "second",
                    "target_input_key": "source",
                },
                {
                    "source_instance_key": "first",
                    "source_output_key": "result",
                    "target_instance_key": "third",
                    "target_input_key": "source",
                },
            ],
        },
    )
    assert flow.status_code == 201, flow.text
    return flow.json()


def _automatic_model_provider_id(client) -> str:
    existing = getattr(client, "automatic_model_provider_id", None)
    if isinstance(existing, str):
        return existing
    provider = client.post(
        "/api/v1/model-providers",
        json={
            "name": "自动运行测试模型",
            "base_url": "https://models.example.test/v1",
            "models": [{"model_name": "gpt-auto", "enabled": True, "is_default": True}],
        },
    )
    assert provider.status_code == 201, provider.text
    provider_id = str(provider.json()["id"])
    client.automatic_model_provider_id = provider_id
    return provider_id


def _node_plan(
    client,
    prompt: str,
    *,
    input_url: str | None = None,
    artifact_id: str | None = None,
    capability_version_ids: list[str] | None = None,
    model_provider_id: str | None = None,
    model_name: str | None = None,
    node_context_enabled: bool = False,
    node_context_prompt: str | None = None,
):
    if not model_provider_id or not model_name:
        model_provider_id = _automatic_model_provider_id(client)
        model_name = "gpt-auto"
    preset = {
        "capability_version_ids": capability_version_ids or [],
        "model_provider_id": model_provider_id,
        "model_name": model_name,
        "node_context_enabled": node_context_enabled,
    }
    if node_context_prompt is not None:
        preset["node_context_prompt"] = node_context_prompt
    return {
        "startup_prompt": prompt,
        "agent_preset": preset,
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
                "first": _node_plan(
                    client, "执行第一个节点", input_url="https://example.com/source"
                )
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
            "node_plans": {"first": _node_plan(client, "执行第一个节点")},
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
                "first": _node_plan(
                    client, "执行第一个节点", input_url="https://example.com/source"
                ),
                "second": _node_plan(client, "执行第二个节点"),
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
            "node_plans": {"first": _node_plan(worker_client, "先执行起点")},
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
                    worker_client, "执行起点", input_url="https://example.com/nested-input"
                ),
                "second": _node_plan(worker_client, "执行下游"),
            },
        },
    )
    assert updated_response.status_code == 200, updated_response.text
    updated = updated_response.json()
    assert updated["automation_plan"]["readiness"] == {"ready": True, "issues": []}

    copied_response = worker_client.post(
        f"/api/v1/flow-runs/{parent['id']}/automatic-runs/{updated['id']}/copy",
        json={"name": "命名自动副本"},
    )
    assert copied_response.status_code == 201, copied_response.text
    copied = copied_response.json()
    assert copied["id"] != updated["id"]
    assert copied["parent_flow_run_id"] == parent["id"]
    assert copied["state"] == "DRAFT"
    assert copied["name"] == "命名自动副本"
    assert copied["node_runs"] == []
    assert copied["artifacts"] == []
    assert copied["automation_plan"]["status"] == "DRAFT"
    assert copied["automation_plan"]["node_plans"] == updated["automation_plan"]["node_plans"]
    nested_after_copy = worker_client.get(f"/api/v1/flow-runs/{parent['id']}/automatic-runs")
    assert nested_after_copy.status_code == 200, nested_after_copy.text
    assert {item["id"] for item in nested_after_copy.json()} == {updated["id"], copied["id"]}

    wrong_parent = worker_client.get(f"/api/v1/flow-runs/{draft['id']}/automatic-runs")
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
                    worker_client, "执行起点", input_url="https://example.com/nested-input"
                ),
                "second": _node_plan(worker_client, "执行下游"),
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
    assert deleted.status_code == 202, deleted.text
    for _ in range(8):
        with db_session_factory() as db:
            if db.get(FlowRun, disposable["id"]) is None:
                break
        assert worker._run_once_sync() is True
    assert worker_client.get(f"/api/v1/flow-runs/{parent['id']}").status_code == 200
    with db_session_factory() as db:
        assert db.get(FlowRun, disposable["id"]) is None
        assert (
            db.scalar(select(FlowRunRuntime).where(FlowRunRuntime.flow_run_id == parent["id"]))
            is not None
        )


def test_schedule_occurrence_stays_in_original_flow_run_as_continuous_record(
    worker_client, worker_container
):
    from flowweave.bootstrap.worker import TaskWorker

    flow = _create_flow(worker_client)
    parent = worker_client.post(
        f"/api/v1/flows/{flow['id']}/runs",
        json={
            "name": "定时任务所属 FlowRun",
            "environment_version_id": worker_client.environment_version_id,
        },
    ).json()
    source = worker_client.post(
        f"/api/v1/flow-runs/{parent['id']}/automatic-runs",
        json={
            "name": "定时连续运行母版",
            "environment_version_id": worker_client.environment_version_id,
            "start_node_key": "first",
            "node_plans": {
                "first": _node_plan(
                    worker_client, "执行起点", input_url="https://example.com/scheduled-input"
                ),
                "second": _node_plan(worker_client, "执行下游"),
            },
        },
    ).json()
    assert source["automation_plan"]["readiness"] == {"ready": True, "issues": []}

    created = worker_client.post(
        "/api/v1/flow-run-schedules",
        json={
            "name": "每小时检查",
            "source_flow_run_id": source["id"],
            "cron_expression": "0 * * * *",
        },
    )
    assert created.status_code == 201, created.text
    schedule = created.json()
    triggered = worker_client.post(f"/api/v1/flow-run-schedules/{schedule['id']}/trigger")
    assert triggered.status_code == 202, triggered.text

    worker = TaskWorker(worker_container)
    occurrences = None
    for _ in range(8):
        assert worker._run_once_sync() is True
        occurrences = worker_client.get(
            f"/api/v1/flow-run-schedules/{schedule['id']}/occurrences?page=1&page_size=10"
        )
        if occurrences.json()["items"][0]["flow_run"] is not None:
            break
    assert occurrences is not None
    assert occurrences.status_code == 200, occurrences.text
    record = occurrences.json()["items"][0]["flow_run"]
    assert record["run_mode"] == "AUTOMATIC"
    assert record["parent_flow_run_id"] == parent["id"]
    assert record["schedule_id"] == schedule["id"]
    assert record["schedule_name"] == "每小时检查"
    nested = worker_client.get(f"/api/v1/flow-runs/{parent['id']}/automatic-runs").json()
    assert record["id"] in {item["id"] for item in nested}
    assert record["id"] not in {
        item["id"] for item in worker_client.get("/api/v1/flow-runs").json()
    }


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
            "node_plans": {"missing": _node_plan(client, "非法节点")},
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
                    client,
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
                "first": _node_plan(
                    worker_client, "自动执行第一个节点", input_url="https://example.com/input"
                ),
                # `second.source` is supplied exclusively through the frozen
                # first.result -> second.source port mapping.
                "second": _node_plan(worker_client, "自动执行第二个节点"),
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


def _import_context_capability(client) -> dict:
    validated = client.post(
        "/api/v1/capability-imports/validate",
        json={
            "capability_type": "CONTEXT",
            "filename": "automatic-context.md",
            "content_base64": base64.b64encode("自动运行冻结 Context".encode()).decode(),
        },
    )
    assert validated.status_code == 200, validated.text
    imported = client.post(
        "/api/v1/capability-imports",
        json={"import_token": validated.json()["import_token"]},
    )
    assert imported.status_code == 201, imported.text
    return imported.json()["capabilities"][0]


def test_automatic_attempt_self_heals_legacy_human_wait_and_projects_frozen_context(
    worker_client,
    worker_container,
    db_session_factory,
    worker_skill_capability,
    monkeypatch,
):
    from flowweave.bootstrap.worker import TaskWorker

    context_capability = _import_context_capability(worker_client)
    flow = _create_flow(worker_client, context_prompt="节点快照中的专属上下文")
    created = worker_client.post(
        f"/api/v1/flows/{flow['id']}/automatic-runs",
        json={
            "environment_version_id": worker_client.environment_version_id,
            "start_node_key": "first",
            "node_plans": {
                "first": _node_plan(
                    worker_client,
                    "启动受损自动节点",
                    input_url="https://example.com/input",
                    capability_version_ids=[
                        worker_skill_capability["capability_id"],
                        context_capability["capability_id"],
                    ],
                    node_context_enabled=True,
                    node_context_prompt="",
                ),
                "second": _node_plan(worker_client, "后继节点"),
            },
        },
    ).json()
    started = worker_client.post(
        f"/api/v1/automatic-runs/{created['id']}/start",
        json={"expected_row_version": created["row_version"]},
        headers={"Idempotency-Key": f"self-heal:{created['id']}"},
    ).json()
    worker = TaskWorker(worker_container)
    for _ in range(6):
        assert worker._run_once_sync() is True
        detail = worker_client.get(f"/api/v1/flow-runs/{started['id']}").json()
        projected = detail["node_runs"][0]["attempts"][0]
        if projected["state"] == "WAITING_START_CONFIRMATION":
            break
    else:
        raise AssertionError("automatic attempt did not reach start handoff")

    assert detail["state"] == "ACTIVE"
    assert projected["context_ids"] == ["__node_context_prompt__"]
    assert projected["agent_preset"]["node_context_prompt"] == "节点快照中的专属上下文"
    capabilities = {
        item["capability_type"]: item for item in projected["frozen_agent_capabilities"]
    }
    assert capabilities["SKILL"]["capability_key"] == worker_skill_capability["capability_key"]
    assert "text" not in capabilities["SKILL"]
    assert capabilities["CONTEXT"]["text"] == "自动运行冻结 Context"
    assert projected["frozen_session_contexts"] == [
        {
            "id": context_capability["capability_id"],
            "capability_key": "automatic-context",
            "digest": context_capability["normalized_config"]["digest"],
            "text": "自动运行冻结 Context",
        }
    ]
    assert projected["automatic_progress"]["stage"] == "START_HANDOFF"

    attempt_id = projected["id"]
    with db_session_factory() as db:
        run = db.get(FlowRun, started["id"])
        attempt = db.get(NodeAttempt, attempt_id)
        assert run is not None and attempt is not None
        run.state = "WAITING_HUMAN"
        attempt.context_ids_json = []
        attempt.agent_preset_json = {
            **attempt.agent_preset_json,
            "node_context_prompt": "",
        }
        db.commit()

    confirmed: list[str] = []

    def confirm_without_runtime(db, observed_attempt_id, payload, idempotency_key):
        del payload, idempotency_key
        repaired = db.get(NodeAttempt, observed_attempt_id)
        assert repaired is not None
        repaired.state = "EXECUTING"
        repaired.runtime_phase = "STARTING"
        repaired.state_version += 1
        repaired_run = db.get(FlowRun, started["id"])
        assert repaired_run is not None
        repaired_run.state = "ACTIVE"
        confirmed.append(observed_attempt_id)
        return {}

    monkeypatch.setattr(orchestration_service, "confirm_start", confirm_without_runtime)
    # The durable start handler now encounters the historical bad aggregate
    # state. Stub only the Runtime handoff after the handler guard so this
    # regression stays focused on orchestration self-healing.
    with db_session_factory() as db:
        orchestration_service.process_start_automatic_attempt(db, attempt_id)
    healed = worker_client.get(f"/api/v1/flow-runs/{started['id']}").json()
    healed_attempt = healed["node_runs"][0]["attempts"][0]
    assert healed["state"] == "ACTIVE"
    assert healed_attempt["state"] == "EXECUTING"
    assert healed_attempt["context_ids"] == ["__node_context_prompt__"]
    assert healed_attempt["agent_preset"]["node_context_prompt"] == ("节点快照中的专属上下文")
    assert confirmed == [attempt_id]
    with db_session_factory() as db:
        orchestration_service.process_start_automatic_attempt(db, attempt_id)
        db.commit()
    assert confirmed == [attempt_id]


def test_automatic_progress_projects_retry_and_succeeded_noop_without_raw_error(
    worker_client, worker_container, db_session_factory
):
    _worker, run_id, attempt_id = _started_automatic_attempt(worker_client, worker_container)
    with db_session_factory() as db:
        attempt = db.get(NodeAttempt, attempt_id)
        run = db.get(FlowRun, run_id)
        assert attempt is not None and run is not None
        attempt.state = "WAITING_START_CONFIRMATION"
        run.state = "ACTIVE"
        db.execute(delete(BackgroundTask).where(BackgroundTask.aggregate_id == attempt_id))
        db.commit()

    with db_session_factory() as db:
        assert orchestration_service.recover_runtime_tasks(db) >= 1
        task = db.scalar(
            select(BackgroundTask).where(
                BackgroundTask.aggregate_id == attempt_id,
                BackgroundTask.task_type == "START_AUTOMATIC_ATTEMPT",
            )
        )
        attempt = db.get(NodeAttempt, attempt_id)
        assert task is not None and attempt is not None
        task.state = TaskState.RETRY
        task.attempts = 2
        task.max_attempts = 5
        task.last_error = "provider secret detail must not escape"
        task.available_at = attempt.updated_at + timedelta(minutes=1)
        task.updated_at = attempt.updated_at + timedelta(seconds=1)
        db.commit()

    retry = worker_client.get(f"/api/v1/flow-runs/{run_id}").json()["node_runs"][0]["attempts"][0][
        "automatic_progress"
    ]
    assert retry["stage"] == "START_HANDOFF"
    assert retry["task_state"] == "RETRY"
    assert retry["attempts"] == 2
    assert retry["max_attempts"] == 5
    assert retry["next_retry_at"] is not None
    assert retry["task_error"] == "后台任务执行失败，平台将按重试策略继续处理。"
    assert "secret" not in str(retry)

    with db_session_factory() as db:
        task = db.scalar(
            select(BackgroundTask).where(
                BackgroundTask.aggregate_id == attempt_id,
                BackgroundTask.task_type == "START_AUTOMATIC_ATTEMPT",
            )
        )
        assert task is not None
        task.state = TaskState.SUCCEEDED
        task.last_error = None
        db.commit()

    succeeded = worker_client.get(f"/api/v1/flow-runs/{run_id}").json()["node_runs"][0]["attempts"][
        0
    ]["automatic_progress"]
    assert succeeded["task_state"] == "SUCCEEDED"
    assert succeeded["needs_attention"] is True


def test_automatic_run_rejects_start_when_required_unmapped_input_is_missing(client):
    flow = _create_flow(client)
    created = client.post(
        f"/api/v1/flows/{flow['id']}/automatic-runs",
        json={
            "environment_version_id": client.environment_version_id,
            "start_node_key": "first",
            "node_plans": {
                "first": _node_plan(client, "缺输入的起点"),
                "second": _node_plan(client, "下游"),
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


def _started_automatic_attempt(worker_client, worker_container, *, fanout: bool = False):
    from flowweave.bootstrap.worker import TaskWorker

    flow = _create_fanout_flow(worker_client) if fanout else _create_flow(worker_client)
    node_plans = {
        "first": _node_plan(
            worker_client, "自动执行第一个节点", input_url="https://example.com/input"
        ),
        "second": _node_plan(worker_client, "自动执行第二个节点"),
    }
    if fanout:
        node_plans["third"] = _node_plan(worker_client, "自动执行第三个节点")
    created = worker_client.post(
        f"/api/v1/flows/{flow['id']}/automatic-runs",
        json={
            "environment_version_id": worker_client.environment_version_id,
            "start_node_key": "first",
            "node_plans": node_plans,
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


def test_automatic_start_handoff_is_recomputed_as_machine_driven(
    worker_client, worker_container, db_session_factory
):
    _worker, run_id, attempt_id = _started_automatic_attempt(worker_client, worker_container)
    with db_session_factory() as db:
        run = db.get(FlowRun, run_id)
        attempt = db.get(NodeAttempt, attempt_id)
        assert run is not None and attempt is not None
        run.state = "WAITING_HUMAN"
        attempt.state = "WAITING_START_CONFIRMATION"
        orchestration_service._recompute_run(db, run)
        assert run.state == "ACTIVE"


def test_automatic_transition_fans_out_without_a_gate_agent(
    worker_client, worker_container, monkeypatch
):
    worker, run_id, attempt_id = _started_automatic_attempt(
        worker_client, worker_container, fanout=True
    )
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
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("automatic transitions must not invoke a Gate Agent")
        ),
    )
    assert worker._run_once_sync() is True

    detail = worker_client.get(f"/api/v1/flow-runs/{run_id}").json()
    node_runs = {item["flow_node_snapshot_key"]: item for item in detail["node_runs"]}
    assert set(node_runs) == {"first", "second", "third"}
    assert node_runs["first"]["attempts"][0]["id"] == attempt_id
    first_output = node_runs["first"]["attempts"][0]["artifacts"][0]["id"]
    for node_key in ("second", "third"):
        bindings = node_runs[node_key]["attempts"][0]["input_bindings"]
        assert [
            {
                key: bindings[0][key]
                for key in ("input_field_key", "artifact_version_id", "binding_source")
            }
        ] == [
            {
                "input_field_key": "source",
                "artifact_version_id": first_output,
                "binding_source": "AUTOMATIC_PORT_MAPPING",
            }
        ]


def test_automatic_transition_with_one_successor_does_not_require_gate_agent(
    worker_client, worker_container, monkeypatch
):
    worker, run_id, _attempt_id = _started_automatic_attempt(worker_client, worker_container)
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
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("single-successor transitions must not invoke a Gate Agent")
        ),
    )
    assert worker._run_once_sync() is True
    detail = worker_client.get(f"/api/v1/flow-runs/{run_id}").json()
    assert [item["flow_node_snapshot_key"] for item in detail["node_runs"]] == [
        "first",
        "second",
    ]


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
