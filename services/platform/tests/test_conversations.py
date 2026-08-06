from __future__ import annotations

from sqlalchemy import delete, select

from flowweave.bootstrap.worker import TaskWorker
from flowweave.shared.models import BackgroundTask


def _asset_payload(skill: dict[str, object]) -> dict[str, object]:
    return {
        "name": "Agent 协作节点",
        "inputs": [{"field_key": "prd", "display_name": "需求", "data_type": "DOCUMENT"}],
        "outputs": [{"field_key": "design", "display_name": "方案", "data_type": "DOCUMENT"}],
        "capabilities": [skill],
        "default_skill_ref": skill["capability_key"],
        "executor": {
            "startup_prompt": "生成方案",
            "context_prompt": "保留证据",
            "timeout_seconds": 120,
            "max_iterations": 20,
        },
    }


def _create_run(api_client, skill: dict[str, object]) -> tuple[str, str]:
    asset_response = api_client.post("/api/v1/node-assets", json=_asset_payload(skill))
    assert asset_response.status_code == 201, asset_response.text
    asset = asset_response.json()
    flow_response = api_client.post(
        "/api/v1/flows",
        json={
            "name": "Agent 协作流程",
            "default_entry_key": "design",
            "nodes": [{"instance_key": "design", "node_asset_id": asset["id"]}],
        },
    )
    assert flow_response.status_code == 201, flow_response.text
    flow = flow_response.json()
    run_response = api_client.post(
        f"/api/v1/flows/{flow['id']}/runs",
        json={
            "artifacts": [
                {
                    "field_key": "prd",
                    "artifact_type": "DOCUMENT",
                    "inline_content": "会话测试输入",
                }
            ]
        },
    )
    assert run_response.status_code == 201, run_response.text
    run = run_response.json()
    return run["id"], run["node_runs"][0]["attempts"][0]["id"]


def test_auto_conversation_records_runtime_result_and_becomes_read_only(client, skill_capability):
    run_id, attempt_id = _create_run(client, skill_capability)
    run = client.get(f"/api/v1/flow-runs/{run_id}").json()
    attempt = run["node_runs"][0]["attempts"][0]

    before_start = client.post(
        f"/api/v1/node-attempts/{attempt_id}/conversations",
        json={"expected_attempt_state_version": attempt["state_version"]},
        headers={"Idempotency-Key": "conversation-before-start"},
    )
    assert before_start.status_code == 409
    assert before_start.json()["error"]["code"] == "ATTEMPT_NOT_STARTED"

    execution = client.post(
        f"/api/v1/node-attempts/{attempt_id}/confirm-start",
        json={"expected_state_version": attempt["state_version"]},
        headers={"Idempotency-Key": "conversation-auto-start"},
    )
    assert execution.status_code == 200, execution.text
    attempt = execution.json()
    assert attempt["state"] == "WAITING_ACCEPTANCE"

    conversations = client.get(f"/api/v1/node-attempts/{attempt_id}/conversations").json()
    assert len(conversations) == 1
    automatic = conversations[0]
    assert automatic["kind"] == "AUTO"
    assert automatic["state"] == "IDLE"
    messages = client.get(f"/api/v1/agent-conversations/{automatic['id']}/messages").json()
    assert [message["source"] for message in messages] == ["PROGRAM", "AGENT"]
    assert [message["sequence_no"] for message in messages] == [1, 2]
    assert messages[1]["delivery_state"] == "DELIVERED"
    assert "Mock output" in messages[1]["content"]["parts"][0]["text"]

    accepted = client.post(
        f"/api/v1/node-attempts/{attempt_id}/accept",
        json={"expected_state_version": attempt["state_version"]},
        headers={"Idempotency-Key": "conversation-auto-accept"},
    )
    assert accepted.status_code == 200, accepted.text
    automatic = client.get(f"/api/v1/agent-conversations/{automatic['id']}").json()
    assert automatic["state"] == "READ_ONLY"


def test_human_conversation_recovers_creation_and_sends_idempotently(
    worker_client,
    db_session_factory,
    worker_container,
    worker_skill_capability,
):
    run_id, attempt_id = _create_run(worker_client, worker_skill_capability)
    worker = TaskWorker(worker_container)
    assert worker._run_once_sync() is True  # readiness
    assert worker._run_once_sync() is True  # empty START gates
    run = worker_client.get(f"/api/v1/flow-runs/{run_id}").json()
    attempt = run["node_runs"][0]["attempts"][0]
    confirmed = worker_client.post(
        f"/api/v1/node-attempts/{attempt_id}/confirm-start",
        json={"expected_state_version": attempt["state_version"]},
        headers={"Idempotency-Key": "conversation-human-start"},
    )
    assert confirmed.status_code == 200, confirmed.text
    assert worker._run_once_sync() is True  # START_RUNTIME
    assert worker._run_once_sync() is True  # POLL_RUNTIME
    assert worker._run_once_sync() is True  # empty END gates
    run = worker_client.get(f"/api/v1/flow-runs/{run_id}").json()
    attempt = run["node_runs"][0]["attempts"][0]
    assert attempt["state"] == "WAITING_ACCEPTANCE"

    created = worker_client.post(
        f"/api/v1/node-attempts/{attempt_id}/conversations",
        json={
            "title": "架构复核",
            "expected_attempt_state_version": attempt["state_version"],
        },
        headers={"Idempotency-Key": "create-human-conversation"},
    )
    assert created.status_code == 202, created.text
    conversation = created.json()
    assert conversation["state"] == "CREATING"

    with db_session_factory() as db:
        db.execute(
            delete(BackgroundTask).where(
                BackgroundTask.aggregate_id == conversation["id"],
                BackgroundTask.task_type == "CREATE_CONVERSATION",
            )
        )
        db.commit()
    worker._recover_startup()
    with db_session_factory() as db:
        recovered = db.scalar(
            select(BackgroundTask).where(
                BackgroundTask.aggregate_id == conversation["id"],
                BackgroundTask.task_type == "CREATE_CONVERSATION",
            )
        )
        assert recovered is not None
        assert recovered.idempotency_key.startswith("recovery:create-conversation:")
    assert worker._run_once_sync() is True

    conversation = worker_client.get(f"/api/v1/agent-conversations/{conversation['id']}").json()
    assert conversation["state"] == "IDLE"
    payload = {
        "client_message_id": "client-message-1",
        "content": [{"type": "text", "text": "请复核异常处理。"}],
        "delivery_mode": "QUEUE_AFTER_TURN",
        "expected_conversation_version": conversation["state_version"],
    }
    headers = {"Idempotency-Key": "send-human-message"}
    first = worker_client.post(
        f"/api/v1/agent-conversations/{conversation['id']}/messages",
        json=payload,
        headers=headers,
    )
    assert first.status_code == 202, first.text
    duplicate = worker_client.post(
        f"/api/v1/agent-conversations/{conversation['id']}/messages",
        json=payload,
        headers=headers,
    )
    assert duplicate.status_code == 202, duplicate.text
    assert duplicate.json()["id"] == first.json()["id"]

    assert worker._run_once_sync() is True  # DELIVER_CONVERSATION_MESSAGE
    messages = worker_client.get(
        f"/api/v1/agent-conversations/{conversation['id']}/messages"
    ).json()
    assert [message["source"] for message in messages] == ["PROGRAM", "HUMAN", "AGENT"]
    assert [message["sequence_no"] for message in messages] == [1, 2, 3]
    assert messages[1]["delivery_state"] == "DELIVERED"
    assert "Mock response" in messages[2]["content"]["parts"][0]["text"]
