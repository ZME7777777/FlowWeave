from __future__ import annotations

import base64
from pathlib import Path
from uuid import uuid4

from sqlalchemy import delete, select

from flowweave.bootstrap.worker import TaskWorker
from flowweave.modules.conversations.application.service import recover_conversation_tasks
from flowweave.runtime.base import StartAttemptRequest
from flowweave.runtime.mock import MockRuntime
from flowweave.shared.models import (
    AgentConversation,
    BackgroundTask,
    EnvironmentVersion,
    NodeAttempt,
    TerminalEnvironment,
)


def _asset_payload(skill: dict[str, object] | None) -> dict[str, object]:
    return {
        "name": "Agent 协作节点",
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
        "capabilities": [skill] if skill else [],
        "executor": {
            "startup_prompt": "生成方案",
            "context_prompt": "保留证据",
            "timeout_seconds": 120,
            "max_iterations": 20,
        },
    }


def _create_run(api_client, skill: dict[str, object] | None) -> tuple[str, str]:
    asset_response = api_client.post("/api/v1/node-assets", json=_asset_payload(skill))
    assert asset_response.status_code == 201, asset_response.text
    asset = asset_response.json()
    flow_response = api_client.post(
        "/api/v1/flows",
        json={
            "name": "Agent 协作流程",
            "lark_root_folder_url": "https://example.feishu.cn/drive/folder/test-root",
            "default_entry_key": "design",
            "nodes": [{"instance_key": "design", "node_asset_id": asset["id"]}],
        },
    )
    assert flow_response.status_code == 201, flow_response.text
    flow = flow_response.json()
    run_response = api_client.post(
        f"/api/v1/flows/{flow['id']}/runs",
        json={
            "flow_node_key": "design",
            "artifacts": [
                {
                    "field_key": "prd",
                    "artifact_type": "URL",
                    "uri": "https://example.feishu.cn/docx/conversation-input",
                }
            ],
        },
    )
    assert run_response.status_code == 201, run_response.text
    run = run_response.json()
    return run["id"], run["node_runs"][0]["attempts"][0]["id"]


def test_run_environment_is_used_by_execution_and_collaboration_runtime(
    worker_client, db_session_factory, worker_container
):
    digest = "sha256:" + "e" * 64
    with db_session_factory() as db:
        environment = TerminalEnvironment(
            id=str(uuid4()),
            name="运行级环境",
            description="",
            base_image="flowweave-openhands-runtime:1",
        )
        db.add(environment)
        db.flush()
        version = EnvironmentVersion(
            id=str(uuid4()),
            environment_id=environment.id,
            version_no=1,
            state="READY",
            image_reference="flowweave/test-environment:v1",
            image_digest=digest,
            manifest_json={},
        )
        db.add(version)
        db.commit()
        version_id = version.id

    asset = worker_client.post("/api/v1/node-assets", json=_asset_payload(None)).json()
    flow = worker_client.post(
        "/api/v1/flows",
        json={
            "name": "运行环境传递流程",
            "lark_root_folder_url": "https://example.feishu.cn/drive/folder/test-root",
            "nodes": [{"instance_key": "design", "node_asset_id": asset["id"]}],
        },
    ).json()
    started = worker_client.post(
        f"/api/v1/flows/{flow['id']}/runs",
        json={
            "environment_version_id": version_id,
            "flow_node_key": "design",
            "artifacts": [
                {
                    "field_key": "prd",
                    "artifact_type": "URL",
                    "uri": "https://example.feishu.cn/docx/environment-input",
                }
            ],
        },
    )
    assert started.status_code == 201, started.text
    run = started.json()
    attempt_id = run["node_runs"][0]["attempts"][0]["id"]

    class CapturingRuntime(MockRuntime):
        def __init__(self) -> None:
            super().__init__()
            self.execution_requests: list[StartAttemptRequest] = []
            self.collaboration_requests: list[StartAttemptRequest] = []

        def start(self, request: StartAttemptRequest):
            self.execution_requests.append(request)
            return super().start(request)

        def create_conversation(self, request: StartAttemptRequest):
            self.collaboration_requests.append(request)
            return super().create_conversation(request)

    runtime = CapturingRuntime()
    previous_runtime = worker_container.runtime
    worker_container.runtime = runtime
    try:
        worker = TaskWorker(worker_container)
        assert worker._run_once_sync() is True  # readiness
        assert worker._run_once_sync() is True  # empty START gates
        ready = worker_client.get(f"/api/v1/flow-runs/{run['id']}").json()
        attempt = ready["node_runs"][0]["attempts"][0]
        confirmed = worker_client.post(
            f"/api/v1/node-attempts/{attempt_id}/confirm-start",
            json={"expected_state_version": attempt["state_version"]},
            headers={"Idempotency-Key": "environment-runtime-start"},
        )
        assert confirmed.status_code == 200, confirmed.text
        assert worker._run_once_sync() is True  # START_RUNTIME
        assert runtime.execution_requests[0].environment_image == digest
        assert worker._run_once_sync() is True  # POLL_RUNTIME
        assert worker._run_once_sync() is True  # empty END gates

        current = worker_client.get(f"/api/v1/flow-runs/{run['id']}").json()
        attempt = current["node_runs"][0]["attempts"][0]
        conversation = worker_client.post(
            f"/api/v1/node-attempts/{attempt_id}/conversations",
            json={
                "title": "镜像传递验证",
                "expected_attempt_state_version": attempt["state_version"],
            },
            headers={"Idempotency-Key": "environment-collaboration-start"},
        )
        assert conversation.status_code == 202, conversation.text
        assert worker._run_once_sync() is True  # CREATE_CONVERSATION
        assert runtime.collaboration_requests[0].environment_image == digest
    finally:
        worker_container.runtime = previous_runtime


def test_auto_conversation_records_runtime_result_and_becomes_read_only(
    client, skill_capability, settings, db_session_factory
):
    # This scenario verifies automatic conversation projection, not Skill loading.
    run_id, attempt_id = _create_run(client, None)
    run = client.get(f"/api/v1/flow-runs/{run_id}").json()
    attempt = run["node_runs"][0]["attempts"][0]

    before_start = client.post(
        f"/api/v1/node-attempts/{attempt_id}/conversations",
        json={"expected_attempt_state_version": attempt["state_version"]},
        headers={"Idempotency-Key": "conversation-before-start"},
    )
    assert before_start.status_code == 202, before_start.text
    assert before_start.json()["kind"] == "HUMAN_CREATED"
    assert (
        client.delete(f"/api/v1/agent-conversations/{before_start.json()['id']}").status_code == 204
    )

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

    workspace = Path(attempt["workspace_ref"])
    workspace.mkdir(parents=True, exist_ok=True)
    image = workspace / "lark-config-qrcode.png"
    image.write_bytes(b"\x89PNG\r\n\x1a\nflowweave-test")
    runtime_path = Path("/workspaces") / image.resolve().relative_to(
        settings.workspace_root.resolve()
    )
    media = client.get(
        f"/api/v1/agent-messages/{messages[1]['id']}/workspace-image",
        params={"source": f"file://{runtime_path}"},
    )
    assert media.status_code == 200, media.text
    assert media.headers["content-type"] == "image/png"
    assert media.content == image.read_bytes()

    outside = settings.workspace_root.resolve() / "outside.png"
    outside.write_bytes(b"not available to this attempt")
    blocked = client.get(
        f"/api/v1/agent-messages/{messages[1]['id']}/workspace-image",
        params={"source": f"file:///workspaces/{outside.name}"},
    )
    assert blocked.status_code == 404

    with db_session_factory() as db:
        runtime_conversation = db.get(AgentConversation, automatic["id"])
        runtime_attempt = db.get(NodeAttempt, attempt_id)
        assert runtime_conversation is not None
        assert runtime_attempt is not None
        runtime_conversation.state = "GENERATING"
        runtime_conversation.state_version += 1
        runtime_attempt.state = "EXECUTING"
        db.commit()
    automatic = client.get(f"/api/v1/agent-conversations/{automatic['id']}").json()
    queued = client.post(
        f"/api/v1/agent-conversations/{automatic['id']}/messages",
        json={
            "client_message_id": "auto-message-waiting-for-turn",
            "content": [{"type": "text", "text": "当前回合结束后继续。"}],
            "delivery_mode": "QUEUE_AFTER_TURN",
            "expected_conversation_version": automatic["state_version"],
        },
        headers={"Idempotency-Key": "auto-message-waiting-for-turn"},
    )
    assert queued.status_code == 202, queued.text
    with db_session_factory() as db:
        assert recover_conversation_tasks(db) == 0
        assert (
            db.scalar(
                select(BackgroundTask).where(
                    BackgroundTask.aggregate_id == queued.json()["id"],
                    BackgroundTask.task_type == "DELIVER_CONVERSATION_MESSAGE",
                )
            )
            is None
        )
        runtime_attempt = db.get(NodeAttempt, attempt_id)
        assert runtime_attempt is not None
        runtime_attempt.state = "WAITING_ACCEPTANCE"
        db.commit()
    with db_session_factory() as db:
        assert recover_conversation_tasks(db) == 1
        recovered_delivery = db.scalar(
            select(BackgroundTask).where(
                BackgroundTask.aggregate_id == queued.json()["id"],
                BackgroundTask.task_type == "DELIVER_CONVERSATION_MESSAGE",
            )
        )
        assert recovered_delivery is not None
        assert recovered_delivery.idempotency_key.startswith(
            "recovery:deliver-conversation-message:"
        )
        db.execute(delete(BackgroundTask).where(BackgroundTask.id == recovered_delivery.id))
        db.commit()
    cancelled = client.post(
        f"/api/v1/agent-messages/{queued.json()['id']}/cancel-queued",
        headers={"Idempotency-Key": "cancel-auto-recovery-test-message"},
    )
    assert cancelled.status_code == 202, cancelled.text

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
    unavailable = worker_client.post(
        f"/api/v1/agent-conversations/{conversation['id']}/messages",
        json={
            "client_message_id": "client-message-unavailable-capability",
            "content": [{"type": "text", "text": "$missing 执行任务"}],
            "capability_refs": [{"capability_type": "MCP", "capability_key": "missing"}],
            "delivery_mode": "QUEUE_AFTER_TURN",
            "expected_conversation_version": conversation["state_version"],
        },
        headers={"Idempotency-Key": "send-unavailable-capability"},
    )
    assert unavailable.status_code == 422, unavailable.text
    assert unavailable.json()["error"]["code"] == "CAPABILITY_NOT_AVAILABLE"
    payload = {
        "client_message_id": "client-message-1",
        "content": [{"type": "text", "text": "$test-skill 请复核异常处理。"}],
        "capability_refs": [{"capability_type": "SKILL", "capability_key": "test-skill"}],
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
    assert messages[1]["content"]["capability_refs"] == [
        {"capability_type": "SKILL", "capability_key": "test-skill"}
    ]
    assert "Mock response" in messages[2]["content"]["parts"][0]["text"]
    assert 'Skill "test-skill"' in messages[2]["content"]["parts"][0]["text"]

    with db_session_factory() as db:
        generating = db.get(AgentConversation, conversation["id"])
        assert generating is not None
        generating.state = "GENERATING"
        generating.state_version += 1
        db.commit()
    conversation = worker_client.get(f"/api/v1/agent-conversations/{conversation['id']}").json()
    queued = worker_client.post(
        f"/api/v1/agent-conversations/{conversation['id']}/messages",
        json={
            "client_message_id": "client-message-steer",
            "content": [{"type": "text", "text": "优先检查并发安全。"}],
            "delivery_mode": "QUEUE_AFTER_TURN",
            "expected_conversation_version": conversation["state_version"],
        },
        headers={"Idempotency-Key": "queue-steer-message"},
    )
    assert queued.status_code == 202, queued.text
    queued_message = queued.json()
    assert queued_message["delivery_state"] == "QUEUED"
    assert queued_message["content"]["presentation"] == "queued"
    assert queued_message["conversation_state_version"] == conversation["state_version"] + 1

    retried = worker_client.post(
        f"/api/v1/agent-messages/{queued_message['id']}/retry",
        headers={"Idempotency-Key": "retry-stuck-queued-message"},
    )
    assert retried.status_code == 202, retried.text
    queued_message = retried.json()
    with db_session_factory() as db:
        retry_task = db.scalar(
            select(BackgroundTask).where(
                BackgroundTask.aggregate_id == queued_message["id"],
                BackgroundTask.task_type == "DELIVER_CONVERSATION_MESSAGE",
            )
        )
        assert retry_task is not None
        assert retry_task.idempotency_key.startswith("retry-conversation-message:")
        db.delete(retry_task)
        db.commit()

    cancelled = worker_client.post(
        f"/api/v1/agent-messages/{queued_message['id']}/cancel-queued",
        headers={"Idempotency-Key": "cancel-queued-message"},
    )
    assert cancelled.status_code == 202, cancelled.text
    assert cancelled.json()["delivery_state"] == "CANCELLED"
    assert cancelled.json()["content"]["presentation"] == "cancelled-queue"

    conversation = worker_client.get(f"/api/v1/agent-conversations/{conversation['id']}").json()
    queued = worker_client.post(
        f"/api/v1/agent-conversations/{conversation['id']}/messages",
        json={
            "client_message_id": "client-message-steer-after-cancel",
            "content": [{"type": "text", "text": "优先检查并发安全。"}],
            "delivery_mode": "QUEUE_AFTER_TURN",
            "expected_conversation_version": conversation["state_version"],
        },
        headers={"Idempotency-Key": "queue-steer-message-after-cancel"},
    )
    assert queued.status_code == 202, queued.text
    queued_message = queued.json()
    assert queued_message["delivery_state"] == "QUEUED"
    assert queued_message["content"]["presentation"] == "queued"

    steered = worker_client.post(
        f"/api/v1/agent-messages/{queued_message['id']}/steer",
        headers={"Idempotency-Key": "steer-now"},
    )
    assert steered.status_code == 202, steered.text
    assert steered.json()["delivery_state"] == "DELIVERING"
    assert steered.json()["delivery_mode"] == "INTERRUPT_AND_RESUME"
    assert steered.json()["content"]["presentation"] == "chat"
    assert worker._run_once_sync() is True

    messages = worker_client.get(
        f"/api/v1/agent-conversations/{conversation['id']}/messages"
    ).json()
    cancelled_message = next(
        message for message in messages if message["id"] == cancelled.json()["id"]
    )
    assert cancelled_message["delivery_state"] == "CANCELLED"
    assert cancelled_message["content"]["presentation"] == "cancelled-queue"
    assert messages[-2]["source"] == "HUMAN"
    assert messages[-2]["delivery_state"] == "DELIVERED"
    assert messages[-2]["content"]["presentation"] == "chat"
    assert messages[-1]["source"] == "AGENT"
    assert "优先检查并发安全" in messages[-1]["content"]["parts"][0]["text"]

    conversation = worker_client.get(f"/api/v1/agent-conversations/{conversation['id']}").json()
    attachment_bytes = b"\x89PNG\r\n\x1a\nflowweave-chat-image"
    attached = worker_client.post(
        f"/api/v1/agent-conversations/{conversation['id']}/messages",
        json={
            "client_message_id": "client-message-attachment",
            "content": [
                {"type": "text", "text": "参考 https://example.com/spec 和附件。"},
                {
                    "type": "attachment",
                    "filename": "界面截图.png",
                    "mime_type": "image/png",
                    "content_base64": base64.b64encode(attachment_bytes).decode(),
                },
            ],
            "delivery_mode": "QUEUE_AFTER_TURN",
            "expected_conversation_version": conversation["state_version"],
        },
        headers={"Idempotency-Key": "send-chat-attachment"},
    )
    assert attached.status_code == 202, attached.text
    attachment_message = attached.json()
    attachment = attachment_message["content"]["parts"][1]
    assert attachment["filename"] == "界面截图.png"
    assert attachment["mime_type"] == "image/png"
    assert "storage_path" not in attachment
    downloaded = worker_client.get(
        f"/api/v1/agent-messages/{attachment_message['id']}/attachments/"
        f"{attachment['attachment_id']}"
    )
    assert downloaded.status_code == 200
    assert downloaded.content == attachment_bytes
    assert worker._run_once_sync() is True
    messages = worker_client.get(
        f"/api/v1/agent-conversations/{conversation['id']}/messages"
    ).json()
    assert "界面截图.png" in messages[-1]["content"]["parts"][0]["text"]
    assert "1 image(s)" in messages[-1]["content"]["parts"][0]["text"]

    with db_session_factory() as db:
        failed = db.get(AgentConversation, conversation["id"])
        assert failed is not None
        failed.state = "FAILED"
        failed.state_version += 1
        db.commit()
    conversation = worker_client.get(f"/api/v1/agent-conversations/{conversation['id']}").json()
    recovery = worker_client.post(
        f"/api/v1/agent-conversations/{conversation['id']}/messages",
        json={
            "client_message_id": "client-message-recover",
            "content": [{"type": "text", "text": "从失败状态继续。"}],
            "delivery_mode": "QUEUE_AFTER_TURN",
            "expected_conversation_version": conversation["state_version"],
        },
        headers={"Idempotency-Key": "send-after-conversation-failed"},
    )
    assert recovery.status_code == 202, recovery.text
    with db_session_factory() as db:
        db.add(
            BackgroundTask(
                task_type="DELIVER_CONVERSATION_MESSAGE",
                aggregate_type="MESSAGE",
                aggregate_id=recovery.json()["id"],
                idempotency_key=f"deliver-conversation-message:{recovery.json()['id']}",
                state="SUCCEEDED",
            )
        )
        db.commit()
    assert worker._run_once_sync() is True  # recreate missing Runtime conversation
    with db_session_factory() as db:
        scheduled = db.scalar(
            select(BackgroundTask).where(
                BackgroundTask.aggregate_id == recovery.json()["id"],
                BackgroundTask.task_type == "DELIVER_CONVERSATION_MESSAGE",
                BackgroundTask.state == "PENDING",
            )
        )
        assert scheduled is not None
        assert scheduled.idempotency_key.startswith(
            f"deliver-conversation-message:{recovery.json()['id']}:v"
        )
    assert worker._run_once_sync() is True  # deliver queued message
    recovered = worker_client.get(f"/api/v1/agent-conversations/{conversation['id']}").json()
    assert recovered["state"] == "IDLE"

    automatic = next(
        item
        for item in worker_client.get(f"/api/v1/node-attempts/{attempt_id}/conversations").json()
        if item["kind"] == "AUTO"
    )
    blocked_delete = worker_client.delete(f"/api/v1/agent-conversations/{automatic['id']}")
    assert blocked_delete.status_code == 409
    deleted = worker_client.delete(f"/api/v1/agent-conversations/{conversation['id']}")
    assert deleted.status_code == 204, deleted.text
    remaining = worker_client.get(f"/api/v1/node-attempts/{attempt_id}/conversations").json()
    assert all(item["id"] != conversation["id"] for item in remaining)
