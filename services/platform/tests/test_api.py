from copy import deepcopy
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

import httpx
import pytest
from sqlalchemy import select

from flowweave.modules.agent_sessions.public import AgentConversationMessageAttachment
from flowweave.shared.errors import DomainError
from flowweave.shared.models import (
    AgentConversationBinding,
    AgentConversationCapability,
    AgentConversationCommand,
    ArtifactVersion,
    AttemptInputBinding,
    BackgroundTask,
    EnvironmentVersion,
    FlowDefinition,
    FlowRun,
    FlowRunRuntime,
    FlowRunRuntimeAllocation,
    GateEvaluation,
    HumanAction,
    NodeAsset,
    NodeAttempt,
    NodeRun,
    RunEvent,
    RunSnapshot,
    TaskState,
    TerminalEnvironment,
)


def asset_payload(name="方案生成", skill=None):
    del skill
    return {
        "name": name,
        "description": "产品驱动节点",
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
            "startup_prompt": "读取输入并生成方案",
            "context_prompt": "保留证据",
        },
    }


def create_asset(client, skill, name="方案生成"):
    response = client.post("/api/v1/node-assets", json=asset_payload(name, skill))
    assert response.status_code == 201, response.text
    return response.json()


def test_node_asset_can_be_saved_without_skill(client):
    response = client.post("/api/v1/node-assets", json=asset_payload("无 Skill 节点"))
    assert response.status_code == 201, response.text
    assert response.json()["executor"] == {
        "startup_prompt": "读取输入并生成方案",
        "context_prompt": "保留证据",
    }
    assert "capabilities" not in response.json()


def test_confirm_start_rejects_attempt_model_override(client):
    rejected = client.post(
        "/api/v1/node-attempts/missing-attempt/confirm-start",
        json={
            "expected_state_version": 1,
            "model_name": "model-explicit",
        },
        headers={"Idempotency-Key": "explicit-start-model"},
    )

    assert rejected.status_code == 422, rejected.text


def test_platform_owned_lark_oauth_endpoints_are_removed(client):
    assert client.post("/api/v1/oauth/lark/sessions", json={"scopes": []}).status_code == 404
    assert client.get("/api/v1/credential-connections").status_code == 404
    assert client.get("/api/v1/internal/credential-leases/opaque").status_code == 404


def test_node_asset_allows_optional_templates_and_creates_blank_output(client):
    payload = asset_payload("无模板节点")
    payload["inputs"][0]["template_url"] = ""
    payload["outputs"][0]["template_url"] = ""
    created = client.post("/api/v1/node-assets", json=payload)
    assert created.status_code == 201, created.text
    asset = created.json()
    assert asset["inputs"][0]["template_url"] == ""
    assert asset["outputs"][0]["template_url"] == ""

    flow = create_flow(client, asset["id"])
    started = client.post(
        f"/api/v1/flows/{flow['id']}/runs",
        json={
            "environment_version_id": client.environment_version_id,
            "flow_node_key": "design_a",
            "artifacts": [
                {
                    "field_key": "prd",
                    "artifact_type": "URL",
                    "uri": "https://example.feishu.cn/docx/input-without-template",
                }
            ],
        },
    )
    assert started.status_code == 201, started.text
    attempt = started.json()["node_runs"][0]["attempts"][0]
    confirmed = client.post(
        f"/api/v1/node-attempts/{attempt['id']}/confirm-start",
        json={"expected_state_version": attempt["state_version"]},
        headers={"Idempotency-Key": "blank-output-template"},
    )
    assert confirmed.status_code == 200, confirmed.text
    target = confirmed.json()["output_targets"]["design"]
    assert target["template_url"] == ""
    assert target["root_url"] == flow["lark_root_folder_url"]
    assert target["run_name"]
    assert "url" not in target


def flow_payload(asset_id, name="需求到方案", *, environment_version_id=None):
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
    payload = {
        "name": name,
        "description": "同一资产放置两次，映射显式产物",
        "lark_root_folder_url": "https://example.feishu.cn/drive/folder/flow-root",
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
            }
        ],
        "port_mappings": [
            {
                "source_instance_key": "design_a",
                "source_output_key": "design",
                "target_instance_key": "design_b",
                "target_input_key": "prd",
            }
        ],
    }
    if environment_version_id is not None:
        payload["environment_version_id"] = environment_version_id
    return payload


def create_flow(client, asset_id):
    response = client.post(
        "/api/v1/flows",
        json=flow_payload(asset_id, environment_version_id=client.environment_version_id),
    )
    assert response.status_code == 201, response.text
    return response.json()


def test_flow_template_does_not_bind_environment_and_each_run_freezes_its_selection(
    client, skill_capability, db_session_factory
):
    asset = create_asset(client, skill_capability, "多运行环境节点")
    flow = create_flow(client, asset["id"])
    assert "environment_version_id" not in flow

    missing = client.post(f"/api/v1/flows/{flow['id']}/runs", json={})
    assert missing.status_code == 422

    with db_session_factory() as db:
        first = db.get(EnvironmentVersion, client.environment_version_id)
        assert first is not None
        environment = TerminalEnvironment(
            name="second-run-environment",
            description="",
            base_image="python:3.13",
            base_image_digest="sha256:" + "3" * 64,
        )
        db.add(environment)
        db.flush()
        second = EnvironmentVersion(
            environment_id=environment.id,
            version_no=1,
            state="READY",
            base_image_reference="python@sha256:" + "3" * 64,
            base_image_digest="sha256:" + "3" * 64,
            image_reference="flowweave/environment-second:v1",
            image_digest="sha256:" + "4" * 64,
            manifest_json=deepcopy(first.manifest_json),
        )
        second.manifest_json["image_id"] = second.image_digest
        second.manifest_json["build"]["runtime_image_digest"] = second.image_digest
        db.add(second)
        db.commit()
        second_id = second.id

    first_run = client.post(
        f"/api/v1/flows/{flow['id']}/runs",
        json={"environment_version_id": client.environment_version_id},
    )
    second_run = client.post(
        f"/api/v1/flows/{flow['id']}/runs",
        json={"environment_version_id": second_id},
    )
    assert first_run.status_code == 201, first_run.text
    assert second_run.status_code == 201, second_run.text
    assert first_run.json()["environment_version_id"] == client.environment_version_id
    assert second_run.json()["environment_version_id"] == second_id


def test_flow_run_can_start_empty_and_activate_any_node_later(
    client, skill_capability, db_session_factory, settings, monkeypatch
):
    from flowweave.modules.conversations.application import locator
    from flowweave.modules.conversations.application import service as conversation_service

    asset = create_asset(client, skill_capability)
    flow = create_flow(client, asset["id"])

    created = client.post(
        f"/api/v1/flows/{flow['id']}/runs",
        json={
            "name": "空任务运行",
            "environment_version_id": client.environment_version_id,
        },
    )

    assert created.status_code == 201, created.text
    run = created.json()
    assert run["node_runs"] == []
    assert run["artifacts"] == []
    assert run["lark_folder_token"] is None
    assert run["lark_folder_url"] is None
    with db_session_factory() as db:
        runtime_session_id = db.scalar(
            select(FlowRunRuntime.id).where(FlowRunRuntime.flow_run_id == run["id"])
        )
    monkeypatch.setattr(
        conversation_service.sandboxes,
        "active_flow_run_runtime_connection",
        lambda _db, *, flow_run_id: SimpleNamespace(
            runtime_session_id=runtime_session_id,
            flow_run_id=flow_run_id,
            managed_runtime_id="mock-runtime",
            resource_name="mock-runtime",
            generation=1,
        ),
    )
    monkeypatch.setattr(
        locator.sandboxes,
        "active_flow_run_runtime_connection",
        lambda _db, *, flow_run_id: SimpleNamespace(
            runtime_session_id=runtime_session_id,
            flow_run_id=flow_run_id,
            managed_runtime_id="mock-runtime",
            resource_name="mock-runtime",
            generation=1,
        ),
    )
    assert client.get(f"/api/v1/flow-runs/{run['id']}/conversations").json() == []
    missing_node = client.post(
        f"/api/v1/flow-runs/{run['id']}/conversations",
        json={"title": "首个会话"},
        headers={"Idempotency-Key": "empty-run-first-conversation"},
    )
    assert missing_node.status_code == 422, missing_node.text
    errors = missing_node.json()["error"]["details"]["errors"]
    assert errors[0]["loc"][-1] == "node_attempt_id"
    artifact = client.post(
        f"/api/v1/flow-runs/{run['id']}/artifacts",
        json={
            "field_key": "prd",
            "artifact_type": "URL",
            "uri": "https://example.feishu.cn/docx/selected-node-input",
        },
    ).json()
    activated = client.post(
        f"/api/v1/flow-runs/{run['id']}/nodes/design_b/runs",
        json={"artifact_ids": {"prd": artifact["id"]}},
    )
    assert activated.status_code == 201, activated.text
    node_run = activated.json()
    attempt = node_run["attempts"][0]
    assert attempt["state"] == "WAITING_START_CONFIRMATION"
    scope = f"/api/v1/flow-runs/{run['id']}/node-attempts/{attempt['id']}/agent-sessions"
    assert client.get(scope).json() == []
    conversation = client.post(
        scope,
        json={"title": "首个会话"},
        headers={"Idempotency-Key": "selected-node-first-conversation"},
    )
    assert conversation.status_code == 201, conversation.text
    assert conversation.json()["id"]
    listed = client.get(scope)
    assert listed.status_code == 200, listed.text
    assert [item["id"] for item in listed.json()] == [conversation.json()["id"]]
    with db_session_factory() as db:
        allocation = db.scalar(
            select(FlowRunRuntimeAllocation).where(
                FlowRunRuntimeAllocation.flow_run_id == run["id"]
            )
        )
        assert allocation is not None
        capabilities = settings.workspace_root / allocation.relative_root / "capabilities"
        # The provider mount is read-only; the control plane parent remains
        # writable so rootless bind mounts can publish and roll back bundles.
        assert capabilities.stat().st_mode & 0o777 == 0o700
    assert node_run["flow_node_snapshot_key"] == "design_b"


def test_human_can_start_same_node_as_independent_runs(client, skill_capability):
    asset = create_asset(client, skill_capability)
    flow = create_flow(client, asset["id"])
    run = client.post(
        f"/api/v1/flows/{flow['id']}/runs",
        json={
            "name": "重复人工启动",
            "environment_version_id": client.environment_version_id,
        },
    ).json()
    artifact = client.post(
        f"/api/v1/flow-runs/{run['id']}/artifacts",
        json={
            "field_key": "prd",
            "artifact_type": "URL",
            "uri": "https://example.feishu.cn/docx/repeated-start-input",
        },
    ).json()

    first = client.post(
        f"/api/v1/flow-runs/{run['id']}/nodes/design_a/runs",
        json={"artifact_ids": {"prd": artifact["id"]}},
    )
    assert first.status_code == 201, first.text
    first_attempt = first.json()["attempts"][0]
    started = client.post(
        f"/api/v1/node-attempts/{first_attempt['id']}/confirm-start",
        json={
            "expected_state_version": first_attempt["state_version"],
            "startup_mode": "PROMPT",
            "prompt": "执行第一条并行记录",
        },
        headers={"Idempotency-Key": "start-first-independent-node-run"},
    )
    assert started.status_code == 200, started.text
    assert started.json()["state"] == "WAITING_ACCEPTANCE"

    second = client.post(
        f"/api/v1/flow-runs/{run['id']}/nodes/design_a/runs",
        json={"artifact_ids": {"prd": artifact["id"]}},
    )

    assert second.status_code == 201, second.text
    assert first.json()["id"] != second.json()["id"]
    assert first.json()["sequence_no"] == 1
    assert second.json()["sequence_no"] == 2
    detail = client.get(f"/api/v1/flow-runs/{run['id']}").json()
    matching = [
        item for item in detail["node_runs"] if item["flow_node_snapshot_key"] == "design_a"
    ]
    assert len(matching) == 2
    assert all(item["attempts"][0]["attempt_no"] == 1 for item in matching)


def test_flow_run_rejects_ready_version_of_deleted_environment(
    client, db_session_factory, skill_capability
):
    asset = create_asset(client, skill_capability, name="软删环境运行节点")
    with db_session_factory() as db:
        environment = TerminalEnvironment(
            name="已删除环境不可创建运行",
            description="",
            base_image="flowweave-openhands-runtime:1",
            base_image_digest="sha256:" + "5" * 64,
        )
        db.add(environment)
        db.flush()
        version = EnvironmentVersion(
            environment_id=environment.id,
            version_no=1,
            state="READY",
            base_image_reference=environment.base_image,
            base_image_digest=environment.base_image_digest,
            image_reference="flowweave/environment-deleted-run:v1",
            image_digest="sha256:" + "6" * 64,
            manifest_json={},
        )
        db.add(version)
        db.flush()
        version_id = version.id
        db.delete(environment)
        db.commit()

    saved = client.post(
        "/api/v1/flows",
        json=flow_payload(asset["id"], environment_version_id=version_id),
    )
    assert saved.status_code == 201, saved.text
    response = client.post(
        f"/api/v1/flows/{saved.json()['id']}/runs",
        json={"environment_version_id": version_id},
    )
    assert response.status_code == 422, response.text
    assert response.json()["error"]["code"] == "RUN_ENVIRONMENT_VERSION_INVALID"


def test_flow_run_rejects_ready_environment_without_runtime_provenance(
    client, db_session_factory, skill_capability
):
    asset = create_asset(client, skill_capability, name="不兼容环境运行节点")
    with db_session_factory() as db:
        environment = TerminalEnvironment(
            name="旧环境不可创建运行",
            description="",
            base_image="flowweave-openhands-runtime:1",
            base_image_digest="sha256:" + "7" * 64,
        )
        db.add(environment)
        db.flush()
        version = EnvironmentVersion(
            environment_id=environment.id,
            version_no=1,
            state="READY",
            base_image_reference=environment.base_image,
            base_image_digest=environment.base_image_digest,
            image_reference="flowweave/environment-legacy-run:v1",
            image_digest="sha256:" + "7" * 64,
            manifest_json={"schema_version": 1},
        )
        db.add(version)
        db.commit()
        environment_id = environment.id
        version_id = version.id

    listed = client.get(f"/api/v1/terminal-environments/{environment_id}").json()["versions"][0]
    assert listed["runtime_compatible"] is False
    assert listed["runtime_incompatibility_reason"]

    saved = client.post(
        "/api/v1/flows",
        json=flow_payload(asset["id"], environment_version_id=version_id),
    )
    assert saved.status_code == 201, saved.text
    response = client.post(
        f"/api/v1/flows/{saved.json()['id']}/runs",
        json={"environment_version_id": version_id},
    )
    assert response.status_code == 409, response.text
    assert response.json()["error"]["code"] == "ENVIRONMENT_RUNTIME_INCOMPATIBLE"


def test_port_mappings_support_branching_and_merge_into_one_node_run(client):
    def create_graph_asset(name, inputs, outputs):
        response = client.post(
            "/api/v1/node-assets",
            json={
                "name": name,
                "inputs": inputs,
                "outputs": outputs,
                "executor": {"startup_prompt": f"执行{name}"},
            },
        )
        assert response.status_code == 201, response.text
        return response.json()

    source = create_graph_asset(
        "分支源节点",
        [],
        [
            {
                "field_key": "result",
                "display_name": "结果",
                "data_type": "URL",
                "template_url": "https://example.feishu.cn/docx/result-template",
            }
        ],
    )
    merge = create_graph_asset(
        "汇聚节点",
        [
            {
                "field_key": "left",
                "display_name": "左输入",
                "data_type": "URL",
                "template_url": "https://example.feishu.cn/docx/left-template",
            },
            {
                "field_key": "right",
                "display_name": "右输入",
                "data_type": "URL",
                "template_url": "https://example.feishu.cn/docx/right-template",
            },
        ],
        [],
    )
    branch = create_graph_asset(
        "分支目标节点",
        [
            {
                "field_key": "input",
                "display_name": "输入",
                "data_type": "URL",
                "template_url": "https://example.feishu.cn/docx/input-template",
            }
        ],
        [],
    )
    created_flow = client.post(
        "/api/v1/flows",
        json={
            "name": "分支与汇聚流程",
            "environment_version_id": client.environment_version_id,
            "lark_root_folder_url": "https://example.feishu.cn/drive/folder/branch-root",
            "nodes": [
                {"instance_key": "source_a", "node_asset_id": source["id"]},
                {"instance_key": "source_b", "node_asset_id": source["id"]},
                {"instance_key": "merge", "node_asset_id": merge["id"]},
                {"instance_key": "branch", "node_asset_id": branch["id"]},
            ],
            "edges": [
                {"source_instance_key": "source_a", "target_instance_key": "merge"},
                {"source_instance_key": "source_b", "target_instance_key": "merge"},
                {"source_instance_key": "source_a", "target_instance_key": "branch"},
            ],
            "port_mappings": [
                {
                    "source_instance_key": "source_a",
                    "source_output_key": "result",
                    "target_instance_key": "merge",
                    "target_input_key": "left",
                },
                {
                    "source_instance_key": "source_b",
                    "source_output_key": "result",
                    "target_instance_key": "merge",
                    "target_input_key": "right",
                },
                {
                    "source_instance_key": "source_a",
                    "source_output_key": "result",
                    "target_instance_key": "branch",
                    "target_input_key": "input",
                },
            ],
        },
    )
    assert created_flow.status_code == 201, created_flow.text
    flow = created_flow.json()
    run = client.post(
        f"/api/v1/flows/{flow['id']}/runs",
        json={"environment_version_id": client.environment_version_id},
    ).json()

    source_attempts = {}
    for node_key in ("source_a", "source_b"):
        activated = client.post(
            f"/api/v1/flow-runs/{run['id']}/nodes/{node_key}/runs",
            json={"artifact_ids": {}},
        )
        assert activated.status_code == 201, activated.text
        source_attempts[node_key] = activated.json()["attempts"][0]

    def execute_and_accept(node_key):
        attempt = source_attempts[node_key]
        execution = client.post(
            f"/api/v1/node-attempts/{attempt['id']}/confirm-start",
            json={
                "expected_state_version": attempt["state_version"],
                "startup_mode": "PROMPT",
                "prompt": f"执行 {node_key}",
            },
            headers={"Idempotency-Key": f"execute-{node_key}"},
        )
        assert execution.status_code == 200, execution.text
        executed = execution.json()
        assert executed["state"] == "WAITING_ACCEPTANCE"
        accepted = client.post(
            f"/api/v1/node-attempts/{attempt['id']}/accept",
            json={"expected_state_version": executed["state_version"]},
            headers={"Idempotency-Key": f"accept-{node_key}"},
        )
        assert accepted.status_code == 200, accepted.text
        return executed, accepted.json()

    source_a_execution, after_source_a = execute_and_accept("source_a")
    merge_run = next(
        item for item in after_source_a["node_runs"] if item["flow_node_snapshot_key"] == "merge"
    )
    branch_run = next(
        item for item in after_source_a["node_runs"] if item["flow_node_snapshot_key"] == "branch"
    )
    assert merge_run["attempts"][0]["state"] == "WAITING_INPUT"
    assert [item["input_field_key"] for item in merge_run["attempts"][0]["input_bindings"]] == [
        "left"
    ]
    assert branch_run["attempts"][0]["state"] == "WAITING_START_CONFIRMATION"
    assert (
        branch_run["attempts"][0]["input_bindings"][0]["artifact_version_id"]
        == source_a_execution["artifacts"][0]["id"]
    )

    source_b_execution, after_source_b = execute_and_accept("source_b")
    merge_runs = [
        item for item in after_source_b["node_runs"] if item["flow_node_snapshot_key"] == "merge"
    ]
    assert len(merge_runs) == 1
    merge_attempt = merge_runs[0]["attempts"][0]
    assert merge_attempt["state"] == "WAITING_START_CONFIRMATION"
    assert {
        item["input_field_key"]: item["artifact_version_id"]
        for item in merge_attempt["input_bindings"]
    } == {
        "left": source_a_execution["artifacts"][0]["id"],
        "right": source_b_execution["artifacts"][0]["id"],
    }


def test_public_reads_and_writes_require_no_human_token(public_client):
    assert public_client.get("/api/v1/node-assets").status_code == 200
    created = public_client.post("/api/v1/node-directories", json={"name": "公开写入"})
    assert created.status_code == 201, created.text
    assert created.json()["name"] == "公开写入"


def test_flow_name_conflict_is_explicit_and_deleted_name_can_be_reused(
    client, skill_capability, db_session_factory
):
    asset = create_asset(client, skill_capability, "流程重名节点")
    original = create_flow(client, asset["id"])

    duplicate = client.post(
        "/api/v1/flows",
        json=flow_payload(
            asset["id"],
            name=original["name"],
            environment_version_id=client.environment_version_id,
        ),
    )
    assert duplicate.status_code == 409, duplicate.text
    assert duplicate.json()["error"]["code"] == "FLOW_NAME_CONFLICT"
    assert "流程名称" in duplicate.json()["error"]["message"]

    assert client.delete(f"/api/v1/flows/{original['id']}").status_code == 204
    with db_session_factory() as db:
        assert db.get(FlowDefinition, original["id"]) is None

    recreated = client.post(
        "/api/v1/flows",
        json=flow_payload(
            asset["id"],
            name=original["name"],
            environment_version_id=client.environment_version_id,
        ),
    )
    assert recreated.status_code == 201, recreated.text


def test_flow_hard_delete_is_blocked_until_runs_are_deleted(
    client, skill_capability, db_session_factory
):
    asset = create_asset(client, skill_capability, "有关联运行流程节点")
    flow = create_flow(client, asset["id"])
    run = client.post(
        f"/api/v1/flows/{flow['id']}/runs",
        json={"environment_version_id": client.environment_version_id},
    ).json()

    blocked = client.delete(f"/api/v1/flows/{flow['id']}")
    assert blocked.status_code == 409, blocked.text
    assert blocked.json()["error"]["code"] == "FLOW_IN_USE"
    assert blocked.json()["error"]["details"]["run_ids"] == [run["id"]]

    assert client.delete(f"/api/v1/flow-runs/{run['id']}").status_code == 204
    assert client.delete(f"/api/v1/flows/{flow['id']}").status_code == 204
    with db_session_factory() as db:
        assert db.get(FlowDefinition, flow["id"]) is None


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
            "relation": "FLOW_NODE",
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


def test_deleted_node_asset_name_can_be_reused_in_the_same_directory(
    client, skill_capability, db_session_factory
):
    directory = client.post(
        "/api/v1/node-directories",
        json={"name": "技术方案设计", "position": 0},
    ).json()
    payload = asset_payload("需求拆分", skill_capability)
    payload["directory_id"] = directory["id"]

    original = client.post("/api/v1/node-assets", json=payload)
    assert original.status_code == 201, original.text

    duplicate = client.post("/api/v1/node-assets", json=payload)
    assert duplicate.status_code == 409, duplicate.text
    assert duplicate.json()["error"]["code"] == "NODE_ASSET_NAME_CONFLICT"
    assert duplicate.json()["error"]["message"] == ("当前目录已存在同名节点资产，请使用其他名称。")

    original_id = original.json()["id"]
    assert client.delete(f"/api/v1/node-assets/{original_id}").status_code == 204

    recreated = client.post("/api/v1/node-assets", json=payload)
    assert recreated.status_code == 201, recreated.text
    assert recreated.json()["id"] != original_id
    with db_session_factory() as db:
        assert db.get(NodeAsset, original_id) is None


def test_node_asset_bulk_delete_deletes_unreferenced_and_reports_blocked(client, skill_capability):
    referenced = create_asset(client, skill_capability, "批删被引用节点")
    unreferenced = create_asset(client, skill_capability, "批删未引用节点")
    flow = create_flow(client, referenced["id"])

    deleted = client.request(
        "DELETE",
        "/api/v1/node-assets",
        json={"ids": [unreferenced["id"], referenced["id"]]},
    )

    assert deleted.status_code == 200, deleted.text
    assert deleted.json() == {
        "deleted_ids": [unreferenced["id"]],
        "blocked": [
            {
                "id": referenced["id"],
                "name": referenced["name"],
                "relation": "FLOW_NODE",
                "flows": [
                    {
                        "id": flow["id"],
                        "name": flow["name"],
                        "reference_count": 2,
                    }
                ],
            }
        ],
    }
    listed_ids = {item["id"] for item in client.get("/api/v1/node-assets").json()}
    assert referenced["id"] in listed_ids
    assert unreferenced["id"] not in listed_ids


def test_catalog_model_provider_and_optimistic_concurrency(client, skill_capability):
    directory = client.post(
        "/api/v1/node-directories", json={"name": "产品与需求", "position": 0}
    ).json()
    payload = asset_payload(skill=skill_capability)
    payload["directory_id"] = directory["id"]
    created = client.post("/api/v1/node-assets", json=payload).json()
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


def test_models_frozen_by_agent_conversations_cannot_be_disabled(client, db_session_factory):
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

    binding_id = str(uuid4())
    with db_session_factory() as db:
        db.add(
            AgentConversationBinding(
                id=binding_id,
                runtime_session_id=str(uuid4()),
                host_kind="FLOW_NODE",
                host_id=str(uuid4()),
                conversation_scope_id=str(uuid4()),
                model_provider_id=provider["id"],
                model_name="gpt-enabled",
                openhands_conversation_id=str(uuid4()),
                display_title="冻结模型会话",
                create_idempotency_key=f"test-binding:{binding_id}",
            )
        )
        db.commit()

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

    with db_session_factory() as db:
        binding = db.get(AgentConversationBinding, binding_id)
        assert binding is not None
        db.delete(binding)
        db.commit()
    listed = client.get("/api/v1/model-providers").json()[0]
    assert listed["reference_node_count"] == 0


def _create_model_provider(client, name: str) -> dict:
    response = client.post(
        "/api/v1/model-providers",
        json={
            "name": name,
            "base_url": "https://models.example.test/v1",
            "api_key": "secret-key",
            "models": [{"model_name": "gpt-delete", "enabled": True, "is_default": True}],
        },
    )
    assert response.status_code == 201, response.text
    return response.json()


def test_api_key_runtime_provider_preserves_reasoning_effort(client, db_session_factory, settings):
    provider = client.post(
        "/api/v1/model-providers",
        json={
            "name": "推理强度运行时供应商",
            "base_url": "https://models.example.test/v1",
            "api_key": "secret-key",
            "models": [
                {
                    "model_name": "gpt-reasoning",
                    "enabled": True,
                    "is_default": True,
                }
            ],
        },
    )
    assert provider.status_code == 201, provider.text

    from flowweave.runtime.request import runtime_provider
    from flowweave.shared.models import ProviderModel
    from flowweave.shared.settings import settings_context

    node = {
        "asset": {
            "executor": {
                "model_provider_id": provider.json()["id"],
            }
        }
    }
    with settings_context(settings), db_session_factory() as db:
        model = db.scalar(
            select(ProviderModel).where(
                ProviderModel.provider_id == provider.json()["id"],
                ProviderModel.model_name == "gpt-reasoning",
            )
        )
        assert model is not None
        model.supported_reasoning_efforts = ["medium", "high"]
        db.commit()
        selected = runtime_provider(db, node, "gpt-reasoning", "high")

    assert selected.reasoning_effort == "high"


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
    assert deleted.status_code == 200, deleted.text
    assert deleted.json() == {
        "deleted_ids": [second["id"], third["id"]],
        "blocked": [],
    }
    assert client.get("/api/v1/model-providers").json() == []


def test_model_provider_bulk_delete_deletes_unreferenced_and_reports_blocked(
    client, db_session_factory
):
    referenced = _create_model_provider(client, "被引用模型服务")
    unreferenced = _create_model_provider(client, "未引用模型服务")
    binding_id = str(uuid4())
    with db_session_factory() as db:
        db.add(
            AgentConversationBinding(
                id=binding_id,
                runtime_session_id=str(uuid4()),
                host_kind="FLOW_NODE",
                host_id=str(uuid4()),
                conversation_scope_id=str(uuid4()),
                model_provider_id=referenced["id"],
                model_name="gpt-delete",
                openhands_conversation_id=str(uuid4()),
                display_title="冻结供应商会话",
                create_idempotency_key=f"test-binding:{binding_id}",
            )
        )
        db.commit()

    deleted = client.request(
        "DELETE",
        "/api/v1/model-providers",
        json={"ids": [referenced["id"], unreferenced["id"]]},
    )
    assert deleted.status_code == 200, deleted.text
    assert deleted.json() == {
        "deleted_ids": [unreferenced["id"]],
        "blocked": [
            {
                "id": referenced["id"],
                "name": referenced["name"],
                "relation": "AGENT_CONFIGURATION",
                "nodes": [{"id": binding_id, "name": "冻结供应商会话"}],
            }
        ],
    }
    assert {item["id"] for item in client.get("/api/v1/model-providers").json()} == {
        referenced["id"],
    }

    with db_session_factory() as db:
        binding = db.get(AgentConversationBinding, binding_id)
        assert binding is not None
        db.delete(binding)
        db.commit()
    assert client.delete(f"/api/v1/model-providers/{referenced['id']}").status_code == 204
    assert client.get("/api/v1/model-providers").json() == []


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


def test_model_provider_connection_test_reports_result_and_persists_failure(monkeypatch, client):
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


def test_model_provider_preview_discovery_uses_unsaved_connection_without_persisting(
    monkeypatch, client
):
    from flowweave.modules.model_providers.presentation import router as provider_router

    captured = {}

    async def discover(_client, snapshot):
        captured["snapshot"] = snapshot
        return ["gpt-alpha", "gpt-beta"]

    monkeypatch.setattr(provider_router, "discover_provider_models", discover)
    response = client.post(
        "/api/v1/model-providers/discover-models",
        json={
            "base_url": "https://preview.example.test/v1/",
            "api_key": "preview-secret",
        },
    )

    assert response.status_code == 200, response.text
    assert response.json() == {"models": ["gpt-alpha", "gpt-beta"]}
    snapshot = captured["snapshot"]
    assert snapshot.base_url == "https://preview.example.test/v1"
    assert snapshot.headers["Authorization"] == "Bearer preview-secret"
    assert client.get("/api/v1/model-providers").json() == []
    assert "preview-secret" not in response.text


def test_model_provider_preview_discovery_can_reuse_saved_api_key(monkeypatch, client):
    from flowweave.modules.model_providers.presentation import router as provider_router

    provider = _create_model_provider(client, "复用凭据模型服务")
    captured = {}

    async def discover(_client, snapshot):
        captured["snapshot"] = snapshot
        return ["gpt-reused"]

    monkeypatch.setattr(provider_router, "discover_provider_models", discover)
    response = client.post(
        "/api/v1/model-providers/discover-models",
        json={
            "base_url": "https://changed.example.test/v1",
            "provider_id": provider["id"],
        },
    )

    assert response.status_code == 200, response.text
    snapshot = captured["snapshot"]
    assert snapshot.base_url == "https://changed.example.test/v1"
    assert snapshot.headers["Authorization"] == "Bearer secret-key"


def test_codex_oauth_device_flow_encrypts_tokens_and_never_returns_them(
    monkeypatch, client, db_session_factory
):
    from flowweave.modules.model_providers.infrastructure.codex_oauth import (
        CodexModelProfile,
        DeviceAuthorization,
        OAuthTokens,
    )
    from flowweave.modules.model_providers.presentation import router as provider_router
    from flowweave.shared.models import ModelProvider

    created = client.post(
        "/api/v1/model-providers",
        json={
            "name": "Codex 订阅",
            "auth_type": "CODEX_OAUTH",
            "base_url": "",
            "models": [],
        },
    )
    assert created.status_code == 201, created.text
    provider = created.json()
    assert provider["auth_type"] == "CODEX_OAUTH"
    assert provider["oauth_connected"] is False

    async def start_device(_client):
        return DeviceAuthorization(
            device_auth_id="internal-device-id",
            user_code="ABCD-EFGH",
            interval=5,
            expires_at=datetime.now(UTC) + timedelta(minutes=15),
        )

    monkeypatch.setattr(provider_router, "request_device_authorization", start_device)
    started = client.post(f"/api/v1/model-providers/{provider['id']}/oauth/device/start")
    assert started.status_code == 200, started.text
    assert started.json()["user_code"] == "ABCD-EFGH"
    assert "internal-device-id" not in started.text

    async def finish_device(_client, device_auth_id, user_code):
        assert device_auth_id == "internal-device-id"
        assert user_code == "ABCD-EFGH"
        return OAuthTokens(
            access_token="secret-access-token",
            refresh_token="secret-refresh-token",
            expires_at=datetime.now(UTC) + timedelta(hours=1),
            account_id="account-123",
            email="developer@example.test",
        )

    monkeypatch.setattr(provider_router, "poll_device_authorization", finish_device)

    async def discover_account_models(*_args):
        return [
            CodexModelProfile("gpt-5.4", "medium", ("low", "medium", "high")),
            CodexModelProfile("gpt-5.6-sol", "high", ("low", "medium", "high", "xhigh")),
            CodexModelProfile("gpt-5.6-terra", "medium", ("low", "medium", "high")),
        ]

    monkeypatch.setattr(provider_router, "discover_codex_model_profiles", discover_account_models)
    completed = client.post(f"/api/v1/model-providers/{provider['id']}/oauth/device/poll")
    assert completed.status_code == 200, completed.text
    assert completed.json() == {
        "state": "CONNECTED",
        "connected": True,
        "account_email": "developer@example.test",
        "model_count": 3,
        "model_sync_error": None,
    }
    assert "secret-access-token" not in completed.text
    assert "secret-refresh-token" not in completed.text

    listed = client.get("/api/v1/model-providers").json()[0]
    assert listed["oauth_connected"] is True
    assert listed["available_for_nodes"] is True
    assert listed["available_for_prompt_gates"] is False
    assert [model["model_name"] for model in listed["models"]] == [
        "gpt-5.4",
        "gpt-5.6-sol",
        "gpt-5.6-terra",
    ]
    sol = next(model for model in listed["models"] if model["model_name"] == "gpt-5.6-sol")
    assert sol["default_reasoning_effort"] == "high"
    assert sol["supported_reasoning_efforts"] == ["low", "medium", "high", "xhigh"]
    assert "secret-access-token" not in str(listed)
    assert "secret-refresh-token" not in str(listed)

    with db_session_factory() as db:
        stored = db.get(ModelProvider, provider["id"])
        assert stored is not None
        assert stored.encrypted_oauth_access_token != b"secret-access-token"
        assert stored.encrypted_oauth_refresh_token != b"secret-refresh-token"

    monkeypatch.setattr(provider_router, "_discover_codex", discover_account_models)
    discovered = client.post(f"/api/v1/model-providers/{provider['id']}/discover-models")
    assert discovered.status_code == 200, discovered.text
    discovered_payload = discovered.json()
    assert discovered_payload["models"] == [
        "gpt-5.4",
        "gpt-5.6-sol",
        "gpt-5.6-terra",
    ]
    assert discovered_payload["provider"]["row_version"] > listed["row_version"]

    tested = client.post(f"/api/v1/model-providers/{provider['id']}/test")
    assert tested.status_code == 200, tested.text
    assert tested.json() == {"connection_state": "CONNECTED", "model_count": 3}


@pytest.mark.asyncio
async def test_codex_model_discovery_uses_account_headers_and_parses_slugs():
    from flowweave.modules.model_providers.infrastructure.codex_oauth import (
        discover_codex_models,
    )

    captured: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["url"] = str(request.url)
        captured["authorization"] = request.headers.get("Authorization")
        captured["account_id"] = request.headers.get("ChatGPT-Account-ID")
        captured["originator"] = request.headers.get("Originator")
        return httpx.Response(
            200,
            json={
                "models": [
                    {"slug": "gpt-5.6-sol"},
                    {"id": "gpt-5.4"},
                    {"slug": "codex-auto-review"},
                    {"name": "gpt-5.5"},
                    {"slug": "gpt-5.6-sol"},
                    {"slug": "future-codex-model"},
                    {"display_name": "ignored-without-id"},
                ]
            },
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as upstream:
        models = await discover_codex_models(upstream, "access-token", "account-123")

    assert models == ["gpt-5.4", "gpt-5.5", "gpt-5.6-sol"]
    assert captured["url"] == (
        "https://chatgpt.com/backend-api/codex/models?client_version=0.144.1"
    )
    assert captured["authorization"] == "Bearer access-token"
    assert captured["account_id"] == "account-123"
    assert captured["originator"] == "codex_cli_rs"


def test_api_key_provider_rejects_codex_oauth_endpoints(client):
    provider = _create_model_provider(client, "普通 API Key 服务")
    status = client.get(f"/api/v1/model-providers/{provider['id']}/oauth/status")
    assert status.status_code == 409
    disconnected = client.delete(f"/api/v1/model-providers/{provider['id']}/oauth")
    assert disconnected.status_code == 409


def test_full_product_run_attempt_revision_snapshot_and_lineage(client, skill_capability):
    asset = create_asset(client, skill_capability)
    flow = create_flow(client, asset["id"])
    started = client.post(
        f"/api/v1/flows/{flow['id']}/runs",
        json={
            "environment_version_id": client.environment_version_id,
            "name": "Run #验收",
            "flow_node_key": "design_a",
            "artifacts": [
                {
                    "field_key": "prd",
                    "artifact_type": "URL",
                    "uri": "https://example.feishu.cn/docx/prd-v1",
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
        json={
            "expected_state_version": attempt1["state_version"],
            "startup_mode": "PROMPT",
            "prompt": "生成首轮方案",
        },
        headers={"Idempotency-Key": "confirm-first"},
    )
    assert execution.status_code == 200, execution.text
    attempt1 = execution.json()
    assert attempt1["state"] == "WAITING_ACCEPTANCE"
    assert attempt1["startup_mode"] == "PROMPT"
    assert attempt1["startup_capability_key"] is None
    assert attempt1["artifacts"][0]["field_key"] == "design"
    assert attempt1["artifacts"][0]["artifact_type"] == "URL"
    assert attempt1["artifacts"][0]["uri"] == ("https://example.feishu.cn/docx/mock-docx-design")
    assert "url" not in attempt1["output_targets"]["design"]
    assert attempt1["artifacts"][0]["inline_content"] is None
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
    changed = flow_payload(asset["id"], environment_version_id=client.environment_version_id)
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
        json={"environment_version_id": client.environment_version_id},
    ).json()
    manual = client.post(
        f"/api/v1/flow-runs/{run['id']}/artifacts",
        json={
            "field_key": "prd",
            "artifact_type": "URL",
            "uri": "https://example.feishu.cn/docx/manual-input",
        },
    ).json()
    activated = client.post(
        f"/api/v1/flow-runs/{run['id']}/nodes/design_b/runs",
        json={"artifact_ids": {"prd": manual["id"]}},
    )
    assert activated.status_code == 201, activated.text
    assert activated.json()["attempts"][0]["state"] == "WAITING_START_CONFIRMATION"
    assert activated.json()["flow_node_snapshot_key"] == "design_b"
    assert run["node_runs"] == []


def test_node_input_bindings_reject_unknown_ports_and_other_run_artifacts(client, skill_capability):
    asset = create_asset(client, skill_capability)
    flow = create_flow(client, asset["id"])
    first_run = client.post(
        f"/api/v1/flows/{flow['id']}/runs",
        json={"environment_version_id": client.environment_version_id},
    ).json()
    second_run = client.post(
        f"/api/v1/flows/{flow['id']}/runs",
        json={"environment_version_id": client.environment_version_id},
    ).json()
    artifact = client.post(
        f"/api/v1/flow-runs/{first_run['id']}/artifacts",
        json={
            "field_key": "prd",
            "artifact_type": "URL",
            "uri": "https://example.feishu.cn/docx/strict-input",
        },
    ).json()

    unknown = client.post(
        f"/api/v1/flow-runs/{first_run['id']}/nodes/design_a/runs",
        json={"artifact_ids": {"unknown": artifact["id"]}},
    )
    assert unknown.status_code == 422, unknown.text
    assert unknown.json()["error"]["code"] == "INPUT_BINDING_INVALID"
    assert unknown.json()["error"]["details"]["fields"] == ["unknown"]

    cross_run = client.post(
        f"/api/v1/flow-runs/{second_run['id']}/nodes/design_a/runs",
        json={"artifact_ids": {"prd": artifact["id"]}},
    )
    assert cross_run.status_code == 422, cross_run.text
    assert cross_run.json()["error"]["code"] == "INPUT_BINDING_INVALID"
    assert cross_run.json()["error"]["details"]["field"] == "prd"


def test_lark_artifact_content_redirects_to_external_document(client, skill_capability):
    asset = create_asset(client, skill_capability, "产物内容节点")
    flow = create_flow(client, asset["id"])
    run = client.post(
        f"/api/v1/flows/{flow['id']}/runs",
        json={"environment_version_id": client.environment_version_id},
    ).json()
    external = client.post(
        f"/api/v1/flow-runs/{run['id']}/artifacts",
        json={
            "field_key": "notes",
            "artifact_type": "URL",
            "uri": "https://example.feishu.cn/docx/manual-notes",
        },
    ).json()
    preview = client.get(f"/api/v1/artifact-versions/{external['id']}/content")
    assert preview.status_code == 409
    assert preview.json()["error"]["code"] == "ARTIFACT_EXTERNAL"
    assert preview.json()["error"]["details"]["uri"] == external["uri"]


def test_human_artifact_can_be_named_and_removed_until_bound(client, skill_capability):
    asset = create_asset(client, skill_capability, "人工文档管理节点")
    flow = create_flow(client, asset["id"])
    run = client.post(
        f"/api/v1/flows/{flow['id']}/runs",
        json={"environment_version_id": client.environment_version_id},
    ).json()
    first = client.post(
        f"/api/v1/flow-runs/{run['id']}/artifacts",
        json={
            "field_key": "prd",
            "uri": "https://example.feishu.cn/docx/named-input",
            "metadata": {"display_name": "支付改版需求文档", "source": "HUMAN_INPUT"},
        },
    )
    assert first.status_code == 201, first.text
    artifact = first.json()
    assert artifact["metadata"]["display_name"] == "支付改版需求文档"
    removed = client.delete(f"/api/v1/flow-runs/{run['id']}/artifacts/{artifact['id']}")
    assert removed.status_code == 204, removed.text
    assert artifact["id"] not in {
        item["id"] for item in client.get(f"/api/v1/flow-runs/{run['id']}").json()["artifacts"]
    }

    bound = client.post(
        f"/api/v1/flow-runs/{run['id']}/artifacts",
        json={"field_key": "prd", "uri": "https://example.feishu.cn/docx/bound-input"},
    ).json()
    activated = client.post(
        f"/api/v1/flow-runs/{run['id']}/nodes/design_a/runs",
        json={"artifact_ids": {"prd": bound["id"]}},
    )
    assert activated.status_code == 201, activated.text
    blocked = client.delete(f"/api/v1/flow-runs/{run['id']}/artifacts/{bound['id']}")
    assert blocked.status_code == 409, blocked.text
    assert blocked.json()["error"]["code"] == "ARTIFACT_DELETE_BLOCKED"
    assert blocked.json()["error"]["details"]["binding_count"] == 1


def test_resource_deletion_preserves_history_and_hard_deletes_run_dependencies(
    client, skill_capability, db_session_factory, settings, container
):
    asset = create_asset(client, skill_capability, "删除语义节点")
    flow = create_flow(client, asset["id"])
    started = client.post(
        f"/api/v1/flows/{flow['id']}/runs",
        json={
            "environment_version_id": client.environment_version_id,
            "name": "待删除运行",
            "flow_node_key": "design_a",
            "artifacts": [
                {
                    "field_key": "prd",
                    "artifact_type": "URL",
                    "uri": "https://example.feishu.cn/docx/deletion-input",
                }
            ],
        },
    )
    assert started.status_code == 201, started.text
    run = started.json()
    attempt = run["node_runs"][0]["attempts"][0]
    artifact = next(item for item in run["artifacts"] if item["field_key"] == "prd")
    assert artifact["storage_key"] is None
    assert artifact["uri"] == "https://example.feishu.cn/docx/deletion-input"

    workspace = Path(attempt["workspace_ref"])
    workspace.mkdir(parents=True, exist_ok=True)
    (workspace / "execution.log").write_text("temporary workspace")
    task_id = "delete-run-task"
    with db_session_factory() as db:
        runtime_session_id = db.scalar(
            select(FlowRunRuntime.id).where(FlowRunRuntime.flow_run_id == run["id"])
        )
        assert runtime_session_id is not None
        conversation = AgentConversationBinding(
            runtime_session_id=runtime_session_id,
            host_kind="FLOW_NODE",
            host_id=run["id"],
            conversation_scope_id=run["id"],
            flow_run_id=run["id"],
            node_run_id=attempt["node_run_id"],
            node_attempt_id=attempt["id"],
            working_directory="/runtime/workspace/project",
            openhands_conversation_id=str(uuid4()),
            lifecycle="ACTIVE",
            create_idempotency_key=f"delete-run-binding:{attempt['id']}",
        )
        db.add(conversation)
        db.flush()
        conversation_id = conversation.id
        db.add_all(
            [
                AgentConversationCapability(
                    binding_id=conversation_id,
                    capability_version_id=skill_capability["capability_id"],
                    capability_type=skill_capability["capability_type"],
                    capability_key=skill_capability["capability_key"],
                    digest="d" * 64,
                    position=0,
                ),
                AgentConversationMessageAttachment(
                    binding_id=conversation_id,
                    event_id="deletion-test-event",
                    content="删除时清理",
                    filename="deletion-test.txt",
                    mime_type="text/plain",
                    byte_size=18,
                    path="/runtime/workspace/project/deletion-test.txt",
                ),
                AgentConversationCommand(
                    workspace_id=None,
                    host_kind="FLOW_NODE",
                    host_id=run["id"],
                    binding_id=conversation_id,
                    command_type="RENAME",
                    idempotency_key=f"delete-run-command:{conversation_id}",
                    state="SUCCEEDED",
                    attempt_count=1,
                ),
            ]
        )
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

    blocked_flow = client.delete(f"/api/v1/flows/{flow['id']}")
    assert blocked_flow.status_code == 409, blocked_flow.text
    assert blocked_flow.json()["error"]["code"] == "FLOW_IN_USE"
    assert blocked_flow.json()["error"]["details"]["run_ids"] == [run["id"]]
    assert client.delete(f"/api/v1/node-assets/{asset['id']}").status_code == 409

    deleted = client.delete(f"/api/v1/flow-runs/{run['id']}")
    assert deleted.status_code == 204, deleted.text
    assert client.get(f"/api/v1/flow-runs/{run['id']}").status_code == 404
    assert all(item["id"] != run["id"] for item in client.get("/api/v1/flow-runs").json())
    assert not workspace.exists()
    assert client.delete(f"/api/v1/flows/{flow['id']}").status_code == 204
    assert client.delete(f"/api/v1/node-assets/{asset['id']}").status_code == 204

    with db_session_factory() as db:
        assert db.get(FlowDefinition, flow["id"]) is None
        assert db.get(NodeAsset, asset["id"]) is None
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
        assert db.get(AgentConversationBinding, conversation_id) is None
        assert (
            db.scalars(
                select(AgentConversationCapability).where(
                    AgentConversationCapability.binding_id == conversation_id
                )
            ).all()
            == []
        )
        assert (
            db.scalars(
                select(AgentConversationMessageAttachment).where(
                    AgentConversationMessageAttachment.binding_id == conversation_id
                )
            ).all()
            == []
        )
        assert (
            db.scalars(
                select(AgentConversationCommand).where(
                    AgentConversationCommand.binding_id == conversation_id
                )
            ).all()
            == []
        )
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


def test_hard_delete_uses_flow_run_runtime_lifecycle_not_attempt_cancel(
    client, skill_capability, db_session_factory, monkeypatch
):
    asset = create_asset(client, skill_capability, "运行时清理容错节点")
    flow = create_flow(client, asset["id"])
    run = client.post(
        f"/api/v1/flows/{flow['id']}/runs",
        json={
            "flow_node_key": "design_a",
            "environment_version_id": client.environment_version_id,
        },
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
    assert cleanup_calls == []
    assert client.get(f"/api/v1/flow-runs/{run['id']}").status_code == 404


def test_failed_runtime_cancel_is_visible_and_can_be_retried(
    client, skill_capability, db_session_factory
):
    from flowweave.modules.tasks.application.handlers import record_terminal_failure

    asset = create_asset(client, skill_capability, "取消失败重试节点")
    flow = create_flow(client, asset["id"])
    run = client.post(
        f"/api/v1/flows/{flow['id']}/runs",
        json={
            "flow_node_key": "design_a",
            "environment_version_id": client.environment_version_id,
        },
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
