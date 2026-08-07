from copy import deepcopy
from pathlib import Path

import pytest
from sqlalchemy import select

from flowweave.shared.errors import DomainError
from flowweave.shared.models import (
    ArtifactVersion,
    AttemptInputBinding,
    BackgroundTask,
    FlowDefinition,
    FlowRun,
    GateEvaluation,
    HumanAction,
    NodeAsset,
    NodeAttempt,
    NodeRun,
    RunEvent,
    RunSnapshot,
    TaskState,
)


def asset_payload(name="方案生成", skill=None):
    return {
        "name": name,
        "description": "产品驱动节点",
        "inputs": [{"field_key": "prd", "display_name": "需求", "data_type": "DOCUMENT"}],
        "outputs": [{"field_key": "design", "display_name": "方案", "data_type": "DOCUMENT"}],
        "executor": {
            "startup_prompt": "读取输入并生成方案",
            "context_prompt": "保留证据",
            "timeout_seconds": 120,
            "max_iterations": 20,
        },
        "capabilities": [skill] if skill else [],
        "default_skill_ref": skill["capability_key"] if skill else None,
    }


def create_asset(client, skill, name="方案生成"):
    response = client.post("/api/v1/node-assets", json=asset_payload(name, skill))
    assert response.status_code == 201, response.text
    return response.json()


def test_node_asset_can_be_saved_without_skill_or_default_skill(client):
    response = client.post("/api/v1/node-assets", json=asset_payload("无 Skill 节点"))
    assert response.status_code == 201, response.text
    assert response.json()["capabilities"] == []
    assert response.json()["default_skill_ref"] is None


def flow_payload(asset_id, name="需求到方案"):
    gates = [
        {
            "stage": "START",
            "position": 0,
            "gate_type": "PYTHON",
            "config": {
                "code": (
                    "result = {'decision': 'PASS', 'summary': '输入完整', "
                    "'reasons': [], 'evidence': [], 'details': {}}"
                )
            },
        },
        {
            "stage": "START",
            "position": 1,
            "gate_type": "JAVASCRIPT",
            "config": {
                "code": (
                    "return {decision: 'PASS', summary: '规则通过', "
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
                    "result = {'decision': 'PASS', 'summary': '输出完整', "
                    "'reasons': [], 'evidence': [], 'details': {}}"
                )
            },
        },
    ]
    return {
        "name": name,
        "description": "同一资产放置两次，映射显式产物",
        "default_entry_key": "design_a",
        "nodes": [
            {
                "instance_key": "design_a",
                "node_asset_id": asset_id,
                "alias": "首轮方案",
                "position_x": 120,
                "position_y": 180,
                "gates": gates,
            },
            {
                "instance_key": "design_b",
                "node_asset_id": asset_id,
                "alias": "复核方案",
                "position_x": 520,
                "position_y": 180,
                "gates": gates,
            },
        ],
        "edges": [
            {
                "source_instance_key": "design_a",
                "target_instance_key": "design_b",
                "position": 0,
                "mappings": [{"source_output_key": "design", "target_input_key": "prd"}],
            }
        ],
    }


def create_flow(client, asset_id):
    response = client.post("/api/v1/flows", json=flow_payload(asset_id))
    assert response.status_code == 201, response.text
    return response.json()


def test_public_reads_and_writes_require_no_human_token(public_client):
    assert public_client.get("/api/v1/node-assets").status_code == 200
    created = public_client.post("/api/v1/node-directories", json={"name": "公开写入"})
    assert created.status_code == 201, created.text
    assert created.json()["name"] == "公开写入"


def test_node_asset_delete_is_blocked_by_active_flow(client, skill_capability):
    asset = create_asset(client, skill_capability, "被流程引用节点")
    flow = create_flow(client, asset["id"])

    rejected = client.delete(f"/api/v1/node-assets/{asset['id']}")

    assert rejected.status_code == 409, rejected.text
    error = rejected.json()["error"]
    assert error["code"] == "NODE_ASSET_IN_USE"
    assert error["details"]["assets"] == [
        {
            "id": asset["id"],
            "name": asset["name"],
            "flows": [
                {
                    "id": flow["id"],
                    "name": flow["name"],
                    "reference_count": 2,
                }
            ],
        }
    ]
    assert any(item["id"] == asset["id"] for item in client.get("/api/v1/node-assets").json())
    persisted_flow = client.get(f"/api/v1/flows/{flow['id']}")
    assert persisted_flow.status_code == 200, persisted_flow.text
    assert {node["node_asset_id"] for node in persisted_flow.json()["nodes"]} == {asset["id"]}

    assert client.delete(f"/api/v1/flows/{flow['id']}").status_code == 204
    assert client.delete(f"/api/v1/node-assets/{asset['id']}").status_code == 204


def test_node_asset_bulk_delete_is_atomic_when_referenced(client, skill_capability):
    referenced = create_asset(client, skill_capability, "批删被引用节点")
    unreferenced = create_asset(client, skill_capability, "批删未引用节点")
    flow = create_flow(client, referenced["id"])

    rejected = client.request(
        "DELETE",
        "/api/v1/node-assets",
        json={"ids": [unreferenced["id"], referenced["id"]]},
    )

    assert rejected.status_code == 409, rejected.text
    error = rejected.json()["error"]
    assert error["code"] == "NODE_ASSET_IN_USE"
    assert error["details"]["flows"] == [
        {
            "id": flow["id"],
            "name": flow["name"],
            "reference_count": 2,
        }
    ]
    listed_ids = {item["id"] for item in client.get("/api/v1/node-assets").json()}
    assert {referenced["id"], unreferenced["id"]} <= listed_ids


def test_catalog_model_provider_and_optimistic_concurrency(client, skill_capability):
    directory = client.post(
        "/api/v1/node-directories", json={"name": "产品与需求", "position": 0}
    ).json()
    payload = asset_payload(skill=skill_capability)
    payload["directory_id"] = directory["id"]
    created = client.post("/api/v1/node-assets", json=payload).json()
    assert created["default_skill_ref"] == skill_capability["capability_key"]
    assert created["inputs"][0]["field_key"] == "prd"

    stale = deepcopy(payload)
    stale["row_version"] = 99
    conflict = client.put(f"/api/v1/node-assets/{created['id']}", json=stale)
    assert conflict.status_code == 409
    assert conflict.json()["error"]["code"] == "VERSION_CONFLICT"

    provider = client.post(
        "/api/v1/model-providers",
        json={
            "name": "OpenAI 内部网关",
            "base_url": "https://models.example.test/v1",
            "api_key": "secret-value-1234",
            "models": [
                {"model_name": "gpt-5", "enabled": True, "is_default": True},
                {"model_name": "o4-mini", "enabled": True, "is_default": False},
            ],
        },
    ).json()
    assert provider["has_api_key"] is True
    assert provider["api_key_hint"] == "••••1234"
    assert "secret-value" not in str(provider)


def test_model_provider_node_references_require_enabled_models(client, skill_capability):
    provider = client.post(
        "/api/v1/model-providers",
        json={
            "name": "受控模型服务",
            "base_url": "https://models.example.test/v1",
            "models": [
                {"model_name": "gpt-enabled", "enabled": True, "is_default": True},
                {"model_name": "gpt-disabled", "enabled": False, "is_default": False},
            ],
        },
    ).json()
    assert provider["available_for_nodes"] is True
    assert provider["reference_node_count"] == 0

    disabled = asset_payload("禁用模型节点", skill_capability)
    disabled["executor"]["model_provider_id"] = provider["id"]
    disabled["executor"]["model_name"] = "gpt-disabled"
    rejected = client.post("/api/v1/node-assets", json=disabled)
    assert rejected.status_code == 400
    assert rejected.json()["error"]["code"] == "INVALID_COMMAND"

    enabled = asset_payload("启用模型节点", skill_capability)
    enabled["executor"]["model_provider_id"] = provider["id"]
    enabled["executor"]["model_name"] = "gpt-enabled"
    asset = client.post("/api/v1/node-assets", json=enabled)
    assert asset.status_code == 201, asset.text
    asset = asset.json()

    listed = client.get("/api/v1/model-providers").json()[0]
    assert listed["reference_node_count"] == 1

    update = {
        "name": provider["name"],
        "base_url": provider["base_url"],
        "row_version": provider["row_version"],
        "models": [
            {"model_name": "gpt-enabled", "enabled": False, "is_default": False},
            {"model_name": "gpt-disabled", "enabled": True, "is_default": True},
        ],
    }
    conflict = client.put(f"/api/v1/model-providers/{provider['id']}", json=update)
    assert conflict.status_code == 409
    assert conflict.json()["error"]["code"] == "VERSION_CONFLICT"

    assert client.delete(f"/api/v1/node-assets/{asset['id']}").status_code == 204
    listed = client.get("/api/v1/model-providers").json()[0]
    assert listed["reference_node_count"] == 0


def _create_model_provider(client, name: str) -> dict:
    response = client.post(
        "/api/v1/model-providers",
        json={
            "name": name,
            "base_url": "https://models.example.test/v1",
            "models": [{"model_name": "gpt-delete", "enabled": True, "is_default": True}],
        },
    )
    assert response.status_code == 201, response.text
    return response.json()


def test_model_provider_single_and_bulk_delete(client):
    first = _create_model_provider(client, "待单删模型服务")
    second = _create_model_provider(client, "待批删模型服务 A")
    third = _create_model_provider(client, "待批删模型服务 B")

    assert client.delete(f"/api/v1/model-providers/{first['id']}").status_code == 204
    assert {item["id"] for item in client.get("/api/v1/model-providers").json()} == {
        second["id"],
        third["id"],
    }

    deleted = client.request(
        "DELETE",
        "/api/v1/model-providers",
        json={"ids": [second["id"], third["id"]]},
    )
    assert deleted.status_code == 204, deleted.text
    assert client.get("/api/v1/model-providers").json() == []


def test_model_provider_bulk_delete_is_atomic_when_referenced(client, skill_capability):
    referenced = _create_model_provider(client, "被引用模型服务")
    unreferenced = _create_model_provider(client, "未引用模型服务")
    payload = asset_payload("引用模型服务节点", skill_capability)
    payload["executor"]["model_provider_id"] = referenced["id"]
    payload["executor"]["model_name"] = "gpt-delete"
    asset_response = client.post("/api/v1/node-assets", json=payload)
    assert asset_response.status_code == 201, asset_response.text
    asset = asset_response.json()

    rejected = client.request(
        "DELETE",
        "/api/v1/model-providers",
        json={"ids": [referenced["id"], unreferenced["id"]]},
    )
    assert rejected.status_code == 409, rejected.text
    error = rejected.json()["error"]
    assert error["code"] == "VERSION_CONFLICT"
    assert error["details"]["providers"] == [
        {
            "id": referenced["id"],
            "name": referenced["name"],
            "reference_node_count": 1,
        }
    ]
    assert {item["id"] for item in client.get("/api/v1/model-providers").json()} == {
        referenced["id"],
        unreferenced["id"],
    }

    assert client.delete(f"/api/v1/node-assets/{asset['id']}").status_code == 204
    assert client.delete(f"/api/v1/model-providers/{referenced['id']}").status_code == 204
    assert [item["id"] for item in client.get("/api/v1/model-providers").json()] == [
        unreferenced["id"]
    ]


@pytest.mark.asyncio
async def test_model_discovery_performs_network_io_outside_database_transaction(
    monkeypatch, client, container
):
    import httpx

    from flowweave.modules.model_providers.presentation import router as provider_router
    from flowweave.shared.application.transactions import mark_uow_owned

    response = client.post(
        "/api/v1/model-providers",
        json={
            "name": "短事务模型服务",
            "base_url": "https://models.example.test/v1",
            "models": [{"model_name": "gpt-probe", "enabled": True, "is_default": True}],
        },
    )
    assert response.status_code == 201, response.text
    provider = response.json()
    transaction_states: list[bool] = []

    async with container.database.sessions() as session:
        mark_uow_owned(session.sync_session)

        async def probe(
            _client: httpx.AsyncClient,
            _snapshot: provider_router.service.ProviderConnectionSnapshot,
        ) -> list[str]:
            transaction_states.append(session.in_transaction())
            return ["gpt-probe"]

        monkeypatch.setattr(provider_router, "discover_provider_models", probe)
        models = await provider_router._discover(provider["id"], session, container)

    assert models == ["gpt-probe"]
    assert transaction_states == [False]


def test_model_provider_connection_test_reports_result_and_persists_failure(
    monkeypatch, client
):
    from flowweave.modules.model_providers.presentation import router as provider_router
    from flowweave.shared.errors import DomainError

    provider = _create_model_provider(client, "连接测试模型服务")

    async def successful_probe(*_args):
        return ["gpt-one", "gpt-two"]

    monkeypatch.setattr(provider_router, "_discover", successful_probe)
    connected = client.post(f"/api/v1/model-providers/{provider['id']}/test")
    assert connected.status_code == 200, connected.text
    assert connected.json() == {"connection_state": "CONNECTED", "model_count": 2}
    assert client.get("/api/v1/model-providers").json()[0]["connection_state"] == "CONNECTED"

    async def failed_probe(*_args):
        raise DomainError("EXECUTOR_UNAVAILABLE", "model discovery failed", 503)

    monkeypatch.setattr(provider_router, "_discover", failed_probe)
    failed = client.post(f"/api/v1/model-providers/{provider['id']}/test")
    assert failed.status_code == 503, failed.text
    assert failed.json()["error"]["code"] == "EXECUTOR_UNAVAILABLE"
    assert client.get("/api/v1/model-providers").json()[0]["connection_state"] == "FAILED"


def test_full_product_run_attempt_revision_snapshot_and_lineage(client, skill_capability):
    asset = create_asset(client, skill_capability)
    flow = create_flow(client, asset["id"])
    started = client.post(
        f"/api/v1/flows/{flow['id']}/runs",
        json={
            "name": "Run #验收",
            "flow_node_key": "design_a",
            "artifacts": [
                {
                    "field_key": "prd",
                    "artifact_type": "DOCUMENT",
                    "inline_content": "需求文档 v1",
                }
            ],
        },
    )
    assert started.status_code == 201, started.text
    run = started.json()
    first = run["node_runs"][0]
    attempt1 = first["attempts"][0]
    assert attempt1["state"] == "WAITING_START_CONFIRMATION"
    assert [x["policy_position"] for x in attempt1["gate_evaluations"]] == [0, 1]
    assert attempt1["input_bindings"][0]["artifact_version_id"] == run["artifacts"][0]["id"]

    summary = client.get("/api/v1/flow-runs").json()[0]
    assert summary["active_snapshot_version"] == 1
    assert summary["current_node_key"] == "design_a"
    assert summary["current_node_name"] == "首轮方案"
    assert summary["current_attempt_state"] == "WAITING_START_CONFIRMATION"
    assert summary["has_pending_action"] is True
    assert summary["progress"] == {"accepted": 0, "terminal": 0, "active": 1}
    assert summary["updated_at"] >= summary["started_at"]

    execution = client.post(
        f"/api/v1/node-attempts/{attempt1['id']}/confirm-start",
        json={"expected_state_version": attempt1["state_version"]},
        headers={"Idempotency-Key": "confirm-first"},
    )
    assert execution.status_code == 200, execution.text
    attempt1 = execution.json()
    assert attempt1["state"] == "WAITING_ACCEPTANCE"
    assert attempt1["artifacts"][0]["field_key"] == "design"
    assert [x["stage"] for x in attempt1["gate_evaluations"]] == ["END", "START", "START"]

    rejected = client.post(
        f"/api/v1/node-attempts/{attempt1['id']}/reject",
        json={
            "reason": "补充恢复策略",
            "copy_input_bindings": True,
            "expected_state_version": attempt1["state_version"],
        },
        headers={"Idempotency-Key": "reject-first"},
    )
    assert rejected.status_code == 200, rejected.text
    attempt2 = rejected.json()
    assert attempt2["attempt_no"] == 2
    assert attempt2["state"] == "WAITING_START_CONFIRMATION"
    assert attempt2["snapshot_id"] == attempt1["snapshot_id"]
    assert (
        attempt2["input_bindings"][0]["artifact_version_id"]
        == attempt1["input_bindings"][0]["artifact_version_id"]
    )

    # Edit definition, then sync creates v2 without changing existing attempts.
    changed = flow_payload(asset["id"])
    changed["row_version"] = flow["row_version"]
    changed["description"] = "同步后的流程定义"
    updated = client.put(f"/api/v1/flows/{flow['id']}", json=changed)
    assert updated.status_code == 200, updated.text
    synced = client.post(
        f"/api/v1/flow-runs/{run['id']}/sync-snapshot",
        json={"expected_active_version": 1},
        headers={"Idempotency-Key": "sync-v2"},
    ).json()
    assert synced["active_snapshot_version"] == 2
    preserved = next(x for x in synced["node_runs"][0]["attempts"] if x["attempt_no"] == 1)
    assert preserved["snapshot_id"] == attempt1["snapshot_id"]

    second_execution = client.post(
        f"/api/v1/node-attempts/{attempt2['id']}/confirm-start",
        json={"expected_state_version": attempt2["state_version"]},
        headers={"Idempotency-Key": "confirm-second"},
    ).json()
    assert second_execution["state"] == "WAITING_ACCEPTANCE"
    accepted = client.post(
        f"/api/v1/node-attempts/{attempt2['id']}/accept",
        json={"expected_state_version": second_execution["state_version"]},
        headers={"Idempotency-Key": "accept-second"},
    )
    assert accepted.status_code == 200, accepted.text
    run = accepted.json()
    assert len(run["node_runs"]) == 2
    downstream = run["node_runs"][1]
    assert downstream["flow_node_snapshot_key"] == "design_b"
    assert downstream["attempts"][0]["state"] == "WAITING_START_CONFIRMATION"
    assert downstream["attempts"][0]["snapshot_id"] == synced["active_snapshot_id"]
    assert (
        downstream["attempts"][0]["input_bindings"][0]["artifact_version_id"]
        == second_execution["artifacts"][0]["id"]
    )

    events = client.get(f"/api/v1/flow-runs/{run['id']}/event-history").json()
    assert [x["cursor"] for x in events] == sorted(x["cursor"] for x in events)
    assert {"SNAPSHOT_SYNCED", "NODE_RUN_ACCEPTED", "ARTIFACT_VERSION_CREATED"} <= {
        x["event_type"] for x in events
    }


def test_any_node_can_start_without_upstream_completion(client, skill_capability):
    asset = create_asset(client, skill_capability)
    flow = create_flow(client, asset["id"])
    run = client.post(
        f"/api/v1/flows/{flow['id']}/runs",
        json={"flow_node_key": "design_a"},
    ).json()
    manual = client.post(
        f"/api/v1/flow-runs/{run['id']}/artifacts",
        json={
            "field_key": "prd",
            "artifact_type": "DOCUMENT",
            "inline_content": "人工为任意节点提供的输入",
        },
    ).json()
    activated = client.post(
        f"/api/v1/flow-runs/{run['id']}/nodes/design_b/runs",
        json={"artifact_ids": {"prd": manual["id"]}},
    )
    assert activated.status_code == 201, activated.text
    assert activated.json()["attempts"][0]["state"] == "WAITING_START_CONFIRMATION"
    assert run["node_runs"][0]["state"] == "ACTIVE"


def test_artifact_content_preview_download_and_storage_boundary(client, skill_capability):
    asset = create_asset(client, skill_capability, "产物内容节点")
    flow = create_flow(client, asset["id"])
    run = client.post(
        f"/api/v1/flows/{flow['id']}/runs",
        json={"flow_node_key": "design_a"},
    ).json()
    inline = client.post(
        f"/api/v1/flow-runs/{run['id']}/artifacts",
        json={
            "field_key": "notes",
            "artifact_type": "TEXT",
            "inline_content": "可预览内容",
            "metadata": {"filename": "notes.md"},
        },
    ).json()
    preview = client.get(f"/api/v1/artifact-versions/{inline['id']}/content")
    assert preview.status_code == 200
    assert preview.text == "可预览内容"
    assert preview.headers["content-disposition"].startswith("inline")
    download = client.get(f"/api/v1/artifact-versions/{inline['id']}/content?download=true")
    assert download.headers["content-disposition"].startswith("attachment")
    assert "notes.md" in download.headers["content-disposition"]


def test_resource_deletion_preserves_history_and_hard_deletes_run_dependencies(
    client, skill_capability, db_session_factory, settings, container
):
    asset = create_asset(client, skill_capability, "删除语义节点")
    flow = create_flow(client, asset["id"])
    large_content = "x" * (settings.inline_artifact_limit + 1)
    started = client.post(
        f"/api/v1/flows/{flow['id']}/runs",
        json={
            "name": "待删除运行",
            "flow_node_key": "design_a",
            "artifacts": [
                {
                    "field_key": "prd",
                    "artifact_type": "DOCUMENT",
                    "inline_content": large_content,
                }
            ],
        },
    )
    assert started.status_code == 201, started.text
    run = started.json()
    attempt = run["node_runs"][0]["attempts"][0]
    artifact = next(item for item in run["artifacts"] if item["field_key"] == "prd")
    assert artifact["storage_key"]
    assert container.artifact_store.exists(artifact["storage_key"])

    workspace = Path(attempt["workspace_ref"])
    workspace.mkdir(parents=True, exist_ok=True)
    (workspace / "execution.log").write_text("temporary workspace")
    task_id = "delete-run-task"
    with db_session_factory() as db:
        db.add(
            BackgroundTask(
                id=task_id,
                task_type="POLL_RUNTIME",
                aggregate_type="ATTEMPT",
                aggregate_id=attempt["id"],
                idempotency_key=f"delete-run-task:{attempt['id']}",
            )
        )
        db.commit()

    assert client.delete(f"/api/v1/flows/{flow['id']}").status_code == 204
    assert client.delete(f"/api/v1/node-assets/{asset['id']}").status_code == 204
    assert all(item["id"] != flow["id"] for item in client.get("/api/v1/flows").json())
    assert all(item["id"] != asset["id"] for item in client.get("/api/v1/node-assets").json())

    summaries = client.get("/api/v1/flow-runs").json()
    summary = next(item for item in summaries if item["id"] == run["id"])
    assert summary["flow_name"] == flow["name"]
    assert summary["flow_row_version"] == flow["row_version"]
    assert client.get(f"/api/v1/flow-runs/{run['id']}").status_code == 200

    deleted = client.delete(f"/api/v1/flow-runs/{run['id']}")
    assert deleted.status_code == 204, deleted.text
    assert client.get(f"/api/v1/flow-runs/{run['id']}").status_code == 404
    assert all(item["id"] != run["id"] for item in client.get("/api/v1/flow-runs").json())
    assert not container.artifact_store.exists(artifact["storage_key"])
    assert not workspace.exists()

    with db_session_factory() as db:
        assert db.get(FlowDefinition, flow["id"]).deleted_at is not None
        assert db.get(NodeAsset, asset["id"]).deleted_at is not None
        assert db.get(FlowRun, run["id"]) is None
        assert db.get(BackgroundTask, task_id) is None
        assert (
            db.scalars(select(RunSnapshot).where(RunSnapshot.flow_run_id == run["id"])).all() == []
        )
        assert db.scalars(select(NodeRun).where(NodeRun.flow_run_id == run["id"])).all() == []
        assert (
            db.scalars(
                select(ArtifactVersion).where(ArtifactVersion.flow_run_id == run["id"])
            ).all()
            == []
        )
        assert (
            db.scalars(select(HumanAction).where(HumanAction.flow_run_id == run["id"])).all() == []
        )
        assert db.scalars(select(RunEvent).where(RunEvent.flow_run_id == run["id"])).all() == []
        assert db.get(NodeAttempt, attempt["id"]) is None
        assert (
            db.scalars(
                select(AttemptInputBinding).where(AttemptInputBinding.attempt_id == attempt["id"])
            ).all()
            == []
        )
        assert (
            db.scalars(
                select(GateEvaluation).where(GateEvaluation.attempt_id == attempt["id"])
            ).all()
            == []
        )


def test_hard_delete_is_not_blocked_when_runtime_cleanup_is_unavailable(
    client, skill_capability, db_session_factory, monkeypatch
):
    asset = create_asset(client, skill_capability, "运行时清理容错节点")
    flow = create_flow(client, asset["id"])
    run = client.post(
        f"/api/v1/flows/{flow['id']}/runs",
        json={"flow_node_key": "design_a"},
    ).json()
    attempt = run["node_runs"][0]["attempts"][0]
    cancelled = client.post(f"/api/v1/flow-runs/{run['id']}/cancel")
    assert cancelled.status_code == 200, cancelled.text

    with db_session_factory() as db:
        stored_attempt = db.get(NodeAttempt, attempt["id"])
        assert stored_attempt is not None
        stored_attempt.runtime_job_id = "unavailable-runtime-job"
        stored_attempt.conversation_id = "unavailable-runtime-conversation"
        stored_attempt.runtime_adapter = "mock"
        db.commit()

    cleanup_calls = []

    def unavailable(handle):
        cleanup_calls.append(handle)
        raise DomainError("EXECUTOR_UNAVAILABLE", "Runtime is unavailable", 503)

    monkeypatch.setattr(client.app.state.container.runtime, "cancel", unavailable)
    deleted = client.delete(f"/api/v1/flow-runs/{run['id']}")

    assert deleted.status_code == 204, deleted.text
    assert len(cleanup_calls) == 1
    assert cleanup_calls[0].conversation_id == "unavailable-runtime-conversation"
    assert client.get(f"/api/v1/flow-runs/{run['id']}").status_code == 404


def test_failed_runtime_cancel_is_visible_and_can_be_retried(
    client, skill_capability, db_session_factory
):
    from flowweave.modules.tasks.application.handlers import record_terminal_failure

    asset = create_asset(client, skill_capability, "取消失败重试节点")
    flow = create_flow(client, asset["id"])
    run = client.post(
        f"/api/v1/flows/{flow['id']}/runs",
        json={"flow_node_key": "design_a"},
    ).json()
    attempt = run["node_runs"][0]["attempts"][0]
    cancelled = client.post(f"/api/v1/flow-runs/{run['id']}/cancel").json()
    version = cancelled["node_runs"][0]["attempts"][0]["state_version"]

    with db_session_factory() as db:
        stored_attempt = db.get(NodeAttempt, attempt["id"])
        assert stored_attempt is not None
        stored_attempt.runtime_phase = "CANCELLING"
        task = BackgroundTask(
            task_type="CANCEL_RUNTIME",
            aggregate_type="ATTEMPT",
            aggregate_id=stored_attempt.id,
            idempotency_key=f"failed-cancel:{stored_attempt.id}",
            state=TaskState.DEAD,
            attempts=3,
            max_attempts=3,
        )
        db.add(task)
        db.flush()
        record_terminal_failure(db, task.id, "OpenHands unavailable")
        db.commit()

    failed = client.get(f"/api/v1/flow-runs/{run['id']}").json()
    failed_attempt = failed["node_runs"][0]["attempts"][0]
    assert failed_attempt["runtime_phase"] == "CANCEL_FAILED"
    assert failed_attempt["error_code"] == "EXECUTOR_CANCEL_FAILED"
    assert failed_attempt["state_version"] == version + 1

    retried = client.post(
        f"/api/v1/node-attempts/{attempt['id']}/retry-runtime-cancel",
        json={"expected_state_version": failed_attempt["state_version"]},
        headers={"Idempotency-Key": "retry-failed-runtime-cancel"},
    )
    assert retried.status_code == 200, retried.text
    assert retried.json()["runtime_phase"] == "CANCELLING"
    assert retried.json()["error_code"] is None
    with db_session_factory() as db:
        retry_task = db.scalar(
            select(BackgroundTask).where(
                BackgroundTask.aggregate_id == attempt["id"],
                BackgroundTask.task_type == "CANCEL_RUNTIME",
                BackgroundTask.state == TaskState.PENDING,
            )
        )
        assert retry_task is not None
