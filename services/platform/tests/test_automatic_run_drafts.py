import pytest
from sqlalchemy import select

from flowweave.modules.orchestration.application import service as orchestration_service
from flowweave.modules.tasks.application.service import claim, enqueue
from flowweave.shared.errors import DomainError
from flowweave.shared.models import (
    BackgroundTask,
    FlowRun,
    FlowRunRuntime,
    FlowRunRuntimeAllocation,
    HumanAction,
    NodeRun,
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


def test_ready_automatic_plan_freezes_without_runtime_or_execution_work(client, db_session_factory):
    flow = _create_flow(client)
    created = client.post(
        f"/api/v1/flows/{flow['id']}/automatic-runs",
        json={
            "environment_version_id": client.environment_version_id,
            "start_node_key": "first",
            "node_plans": {
                "first": _node_plan("执行第一个节点", input_url="https://example.com/source"),
                "second": _node_plan("执行第二个节点"),
            },
        },
    )
    assert created.status_code == 201, created.text
    draft = created.json()
    assert draft["automation_plan"]["readiness"] == {"ready": True, "issues": []}

    frozen = client.post(
        f"/api/v1/automatic-runs/{draft['id']}/start",
        json={"expected_row_version": draft["row_version"]},
        headers={"Idempotency-Key": "freeze-ready-plan"},
    )
    assert frozen.status_code == 200, frozen.text
    run = frozen.json()
    assert run["state"] == "ACTIVE"
    assert run["automation_plan"]["status"] == "FROZEN"
    assert run["node_runs"] == []

    replay = client.post(
        f"/api/v1/automatic-runs/{draft['id']}/start",
        json={"expected_row_version": draft["row_version"]},
        headers={"Idempotency-Key": "freeze-ready-plan"},
    )
    assert replay.status_code == 200, replay.text
    assert replay.json()["row_version"] == run["row_version"]
    mismatch = client.post(
        f"/api/v1/automatic-runs/{draft['id']}/start",
        json={"expected_row_version": run["row_version"]},
        headers={"Idempotency-Key": "freeze-ready-plan"},
    )
    assert mismatch.status_code == 409, mismatch.text

    with db_session_factory() as db:
        assert db.scalar(select(NodeRun).where(NodeRun.flow_run_id == run["id"])) is None
        assert (
            db.scalar(
                select(FlowRunRuntimeAllocation).where(
                    FlowRunRuntimeAllocation.flow_run_id == run["id"]
                )
            )
            is None
        )
        assert (
            db.scalar(select(FlowRunRuntime).where(FlowRunRuntime.flow_run_id == run["id"])) is None
        )
        assert (
            db.scalar(select(BackgroundTask).where(BackgroundTask.aggregate_id == run["id"]))
            is None
        )
        actions = list(db.scalars(select(HumanAction).where(HumanAction.flow_run_id == run["id"])))
        assert [item.action_type for item in actions] == ["FREEZE_AUTOMATIC_RUN"]

        task = enqueue(
            db,
            task_type="PROVISION_FLOW_RUN_RUNTIME",
            aggregate_type="FLOW_RUN",
            aggregate_id=run["id"],
            idempotency_key=f"misdelivered-provision:{run['id']}",
        )
        db.commit()
        claimed = claim(db, "fr121-guard", lease_seconds=30)
        assert claimed is not None and claimed[0].id == task.id
        with pytest.raises(DomainError, match="cannot provision a Runtime before scheduling"):
            orchestration_service.process_provision_flow_run_runtime(
                db, run["id"], claimed[1], commit=False
            )
        db.rollback()
        assert (
            db.scalar(
                select(FlowRunRuntimeAllocation).where(
                    FlowRunRuntimeAllocation.flow_run_id == run["id"]
                )
            )
            is None
        )


def test_automatic_plan_rejects_freeze_with_unmapped_required_input(client):
    flow = _create_flow(client)
    created = client.post(
        f"/api/v1/flows/{flow['id']}/automatic-runs",
        json={
            "environment_version_id": client.environment_version_id,
            "start_node_key": "first",
            "node_plans": {
                "first": _node_plan("缺少输入的起始节点"),
                "second": _node_plan("映射输入的下游节点"),
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
    response = client.post(
        f"/api/v1/automatic-runs/{draft['id']}/start",
        json={"expected_row_version": draft["row_version"]},
    )
    assert response.status_code == 422, response.text
    assert response.json()["error"]["code"] == "AUTOMATION_PLAN_NOT_READY"


def test_new_standard_flow_run_remains_manual(client):
    flow = _create_flow(client)
    created = client.post(
        f"/api/v1/flows/{flow['id']}/runs",
        json={"environment_version_id": client.environment_version_id},
    )
    assert created.status_code == 201, created.text
    assert created.json()["run_mode"] == "MANUAL"
    assert created.json()["automation_plan"] is None


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


def test_frozen_automatic_plan_copies_to_editable_draft_with_owned_artifact(
    client, db_session_factory
):
    flow = _create_flow(client)
    created = client.post(
        f"/api/v1/flows/{flow['id']}/automatic-runs",
        json={
            "name": "待冻结自动编排",
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
            "uri": "https://example.com/copied-source",
        },
    )
    assert artifact.status_code == 201, artifact.text
    configured = client.put(
        f"/api/v1/automatic-runs/{draft['id']}",
        json={
            "expected_row_version": draft["row_version"],
            "start_node_key": "first",
            "node_plans": {
                "first": _node_plan("执行第一个节点", artifact_id=artifact.json()["id"]),
                "second": _node_plan("执行第二个节点"),
            },
        },
    )
    assert configured.status_code == 200, configured.text
    frozen = client.post(
        f"/api/v1/automatic-runs/{draft['id']}/start",
        json={"expected_row_version": configured.json()["row_version"]},
        headers={"Idempotency-Key": "freeze-copy-source"},
    )
    assert frozen.status_code == 200, frozen.text

    copied = client.post(
        f"/api/v1/automatic-runs/{draft['id']}/copy",
        json={"name": "复制后的自动编排"},
    )
    assert copied.status_code == 201, copied.text
    copy = copied.json()
    assert copy["state"] == "DRAFT"
    assert copy["automation_plan"]["status"] == "DRAFT"
    assert copy["name"] == "复制后的自动编排"
    copied_artifact_id = copy["automation_plan"]["node_plans"]["first"]["artifact_ids"]["source"]
    assert copied_artifact_id != artifact.json()["id"]
    copied_artifact = next(item for item in copy["artifacts"] if item["id"] == copied_artifact_id)
    assert copied_artifact["flow_run_id"] == copy["id"]
    assert copied_artifact["uri"] == "https://example.com/copied-source"
    assert copy["node_runs"] == []

    with db_session_factory() as db:
        assert db.scalar(select(NodeRun).where(NodeRun.flow_run_id == copy["id"])) is None
        assert (
            db.scalar(
                select(FlowRunRuntimeAllocation).where(
                    FlowRunRuntimeAllocation.flow_run_id == copy["id"]
                )
            )
            is None
        )
