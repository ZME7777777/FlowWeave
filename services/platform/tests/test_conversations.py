from __future__ import annotations

import base64
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import uuid4

from sqlalchemy import delete, func, select

from flowweave.bootstrap.worker import TaskWorker
from flowweave.modules.conversations.application.service import (
    _append,
    _append_runtime_payload,
    _apply_conversation_result,
    list_subagents,
    recover_conversation_tasks,
    terminal_resource_details,
)
from flowweave.modules.conversations.domain.enums import (
    DeliveryMode,
    DeliveryState,
    MessageSource,
    MessageType,
)
from flowweave.runtime.base import RuntimeHandle, RuntimeResult, StartAttemptRequest
from flowweave.runtime.mock import MockRuntime
from flowweave.shared.models import (
    AgentConversation,
    AgentMessage,
    BackgroundTask,
    EnvironmentVersion,
    ManagedSandbox,
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


def test_human_conversation_ignores_intermediate_agent_message(client, db_session_factory):
    run_id, attempt_id = _create_run(client, None)
    run = client.get(f"/api/v1/flow-runs/{run_id}").json()
    attempt = run["node_runs"][0]["attempts"][0]
    created = client.post(
        f"/api/v1/node-attempts/{attempt_id}/conversations",
        json={"expected_attempt_state_version": attempt["state_version"]},
        headers={"Idempotency-Key": "ignore-intermediate-agent-message"},
    )
    assert created.status_code == 202, created.text

    with db_session_factory() as db:
        conversation = db.get(AgentConversation, created.json()["id"])
        assert conversation is not None
        before = db.scalar(
            select(func.count())
            .select_from(AgentMessage)
            .where(AgentMessage.conversation_id == conversation.id)
        )
        _append_runtime_payload(
            db,
            conversation,
            cursor="intermediate-message-1",
            event_type="MESSAGE",
            payload={"source": "agent", "content": "已就绪"},
        )
        after = db.scalar(
            select(func.count())
            .select_from(AgentMessage)
            .where(AgentMessage.conversation_id == conversation.id)
        )
        assert after == before


def test_conversation_fork_only_accepts_agent_reply_and_copies_history(client, db_session_factory):
    run_id, attempt_id = _create_run(client, None)
    run = client.get(f"/api/v1/flow-runs/{run_id}").json()
    attempt = run["node_runs"][0]["attempts"][0]
    created = client.post(
        f"/api/v1/node-attempts/{attempt_id}/conversations",
        json={"expected_attempt_state_version": attempt["state_version"]},
        headers={"Idempotency-Key": "fork-source-conversation"},
    )
    assert created.status_code == 202, created.text

    with db_session_factory() as db:
        source = db.get(AgentConversation, created.json()["id"])
        assert source is not None
        source.state = "IDLE"
        _append(
            db,
            source,
            source=MessageSource.HUMAN,
            message_type=MessageType.TEXT,
            content={"parts": [{"type": "text", "text": "第一问"}]},
            delivery_state=DeliveryState.DELIVERED,
            delivery_mode=DeliveryMode.QUEUE_AFTER_TURN,
        )
        first_agent = _append(
            db,
            source,
            source=MessageSource.AGENT,
            message_type=MessageType.TEXT,
            content={
                "presentation": "final",
                "parts": [{"type": "text", "text": "第一答"}],
            },
            delivery_state=DeliveryState.DELIVERED,
        )
        second_human = _append(
            db,
            source,
            source=MessageSource.HUMAN,
            message_type=MessageType.TEXT,
            content={"parts": [{"type": "text", "text": "原始第二问"}]},
            delivery_state=DeliveryState.DELIVERED,
            delivery_mode=DeliveryMode.QUEUE_AFTER_TURN,
        )
        source.state_version += 1
        version = source.state_version
        db.commit()

    forked = client.post(
        f"/api/v1/agent-messages/{first_agent.id}/fork",
        json={"expected_conversation_version": version},
        headers={"Idempotency-Key": "fork-at-first-answer"},
    )
    assert forked.status_code == 202, forked.text
    fork_messages = client.get(f"/api/v1/agent-conversations/{forked.json()['id']}/messages").json()
    assert [message["source"] for message in fork_messages] == [
        "PROGRAM",
        "HUMAN",
        "AGENT",
    ]
    assert [message["content"]["parts"][0]["text"] for message in fork_messages] == [
        "已从既有会话创建上下文分支。",
        "第一问",
        "第一答",
    ]
    assert forked.json()["context_baseline"]["fork"]["history"] == [
        {"role": "user", "content": "第一问"},
        {"role": "assistant", "content": "第一答"},
    ]

    rejected = client.post(
        f"/api/v1/agent-messages/{second_human.id}/fork",
        json={"expected_conversation_version": version},
        headers={"Idempotency-Key": "reject-human-message-fork"},
    )
    assert rejected.status_code == 409, rejected.text
    assert rejected.json()["error"]["code"] == "MESSAGE_NOT_FORKABLE"


def test_revise_stopped_turn_rebuilds_same_conversation_and_supersedes_old_chain(
    client, db_session_factory
):
    run_id, attempt_id = _create_run(client, None)
    run = client.get(f"/api/v1/flow-runs/{run_id}").json()
    attempt = run["node_runs"][0]["attempts"][0]
    created = client.post(
        f"/api/v1/node-attempts/{attempt_id}/conversations",
        json={"expected_attempt_state_version": attempt["state_version"]},
        headers={"Idempotency-Key": "revise-source-conversation"},
    )
    assert created.status_code == 202, created.text
    conversation_id = created.json()["id"]

    with db_session_factory() as db:
        conversation = db.get(AgentConversation, conversation_id)
        assert conversation is not None
        conversation.state = "IDLE"
        first_human = _append(
            db,
            conversation,
            source=MessageSource.HUMAN,
            message_type=MessageType.TEXT,
            content={"parts": [{"type": "text", "text": "第一问"}]},
            delivery_state=DeliveryState.DELIVERED,
            delivery_mode=DeliveryMode.QUEUE_AFTER_TURN,
        )
        _append(
            db,
            conversation,
            source=MessageSource.AGENT,
            message_type=MessageType.TEXT,
            content={"presentation": "final", "parts": [{"type": "text", "text": "第一答"}]},
            delivery_state=DeliveryState.DELIVERED,
        )
        stopped_human = _append(
            db,
            conversation,
            source=MessageSource.HUMAN,
            message_type=MessageType.TEXT,
            content={
                "presentation": "chat",
                "parts": [{"type": "text", "text": "原始第二问"}],
                "capability_refs": [{"capability_type": "SKILL", "capability_key": "kept-skill"}],
            },
            delivery_state=DeliveryState.DELIVERED,
            delivery_mode=DeliveryMode.QUEUE_AFTER_TURN,
        )
        stale_agent = _append(
            db,
            conversation,
            source=MessageSource.AGENT,
            message_type=MessageType.TEXT,
            content={"parts": [{"type": "text", "text": "不应继续使用的旧回复"}]},
            delivery_state=DeliveryState.DELIVERED,
        )
        conversation.context_baseline_json = {
            **conversation.context_baseline_json,
            "stopped_turn": {"editable_message_id": stopped_human.id},
        }
        conversation.state_version += 1
        version = conversation.state_version
        db.commit()

    earlier_rejected = client.post(
        f"/api/v1/agent-messages/{first_human.id}/revise",
        json={"expected_conversation_version": version, "text": "不能编辑第一问"},
        headers={"Idempotency-Key": "reject-earlier-revision"},
    )
    assert earlier_rejected.status_code == 409, earlier_rejected.text
    assert earlier_rejected.json()["error"]["code"] == "MESSAGE_NOT_REVISABLE"

    revised = client.post(
        f"/api/v1/agent-messages/{stopped_human.id}/revise",
        json={"expected_conversation_version": version, "text": "修改后的第二问"},
        headers={"Idempotency-Key": "revise-stopped-turn"},
    )
    assert revised.status_code == 202, revised.text
    assert revised.json()["id"] == conversation_id
    assert revised.json()["state"] == "CREATING"
    assert revised.json()["editable_message_id"] is None
    assert revised.json()["context_baseline"]["fork"]["history"] == [
        {"role": "user", "content": "第一问"},
        {"role": "assistant", "content": "第一答"},
    ]

    visible = client.get(f"/api/v1/agent-conversations/{conversation_id}/messages").json()
    assert [message["content"].get("parts", [{}])[0].get("text") for message in visible] == [
        (
            "当前 Attempt 的快照、输入绑定、候选能力与产物已挂载；"
            "本会话不包含其他会话消息，也不继承自动执行的启动任务。"
            "Agent 将根据本会话中的消息动态选择能力。"
        ),
        "第一问",
        "第一答",
        "修改后的第二问",
    ]
    assert visible[-1]["delivery_state"] == "QUEUED"
    assert visible[-1]["content"]["capability_refs"] == [
        {"capability_type": "SKILL", "capability_key": "kept-skill"}
    ]

    with db_session_factory() as db:
        old_human = db.get(AgentMessage, stopped_human.id)
        old_agent = db.get(AgentMessage, stale_agent.id)
        assert old_human is not None and old_human.content_json["superseded"] is True
        assert old_agent is not None and old_agent.content_json["superseded"] is True
        recreate = db.scalar(
            select(BackgroundTask).where(
                BackgroundTask.aggregate_id == conversation_id,
                BackgroundTask.task_type == "CREATE_CONVERSATION",
                BackgroundTask.state == "PENDING",
            )
        )
        assert recreate is not None


def test_conversation_result_projection_is_idempotent(client, db_session_factory):
    run_id, attempt_id = _create_run(client, None)
    run = client.get(f"/api/v1/flow-runs/{run_id}").json()
    attempt = run["node_runs"][0]["attempts"][0]
    created = client.post(
        f"/api/v1/node-attempts/{attempt_id}/conversations",
        json={"expected_attempt_state_version": attempt["state_version"]},
        headers={"Idempotency-Key": "idempotent-result-projection"},
    )
    assert created.status_code == 202, created.text

    runtime_event_id = "delivery-result:poll:current-finish:current-finish"
    result = RuntimeResult(
        status="COMPLETED",
        final_message="本轮完成",
        cursor="current-finish",
    )
    with db_session_factory() as db:
        conversation = db.get(AgentConversation, created.json()["id"])
        assert conversation is not None
        conversation.state = "GENERATING"
        _apply_conversation_result(db, conversation, result, message_id="poll:current-finish")
        db.flush()
        conversation.state = "GENERATING"
        _apply_conversation_result(db, conversation, result, message_id="poll:current-finish")
        db.flush()

        projected = db.scalars(
            select(AgentMessage).where(
                AgentMessage.conversation_id == conversation.id,
                AgentMessage.runtime_event_id == runtime_event_id,
            )
        ).all()
        assert len(projected) == 1
        assert conversation.state == "IDLE"


def test_human_conversation_delegates_and_resumes_after_all_children(client, db_session_factory):
    run_id, attempt_id = _create_run(client, None)
    run = client.get(f"/api/v1/flow-runs/{run_id}").json()
    attempt = run["node_runs"][0]["attempts"][0]
    created = client.post(
        f"/api/v1/node-attempts/{attempt_id}/conversations",
        json={"expected_attempt_state_version": attempt["state_version"]},
        headers={"Idempotency-Key": "delegate-parent"},
    )
    assert created.status_code == 202, created.text

    envelope = (
        '{"flowweave":{"action":"delegate","tasks":['
        '{"title":"后端检查","instruction":"检查后端状态机"},'
        '{"title":"前端检查","instruction":"检查前端侧栏"}'
        "]}}"
    )
    with db_session_factory() as db:
        parent = db.get(AgentConversation, created.json()["id"])
        assert parent is not None
        parent.state = "GENERATING"
        _apply_conversation_result(
            db,
            parent,
            RuntimeResult(status="COMPLETED", final_message=envelope, cursor="delegate-1"),
            message_id="delegate-message",
        )
        db.flush()

        children = list_subagents(db, parent.id)
        assert parent.state == "WAITING_SUBAGENTS"
        assert [item["title"] for item in children] == ["后端检查", "前端检查"]
        assert all(item["kind"] == "SUBAGENT" for item in children)
        assert all(item["parent_conversation_id"] == parent.id for item in children)
        assert all(item["state"] == "CREATING" for item in children)

        child_rows = [db.get(AgentConversation, item["id"]) for item in children]
        assert all(item is not None for item in child_rows)
        for index, child in enumerate(child_rows):
            assert child is not None
            child.state = "GENERATING"
            _apply_conversation_result(
                db,
                child,
                RuntimeResult(
                    status="COMPLETED",
                    final_message=f"子任务 {index + 1} 结果",
                    cursor=f"child-{index + 1}",
                ),
                message_id=f"child-message-{index + 1}",
            )
            db.flush()

        db.refresh(parent)
        assert parent.state == "GENERATING"
        result_message = db.scalar(
            select(AgentMessage).where(
                AgentMessage.conversation_id == parent.id,
                AgentMessage.client_message_id.like("subagent-results:%"),
            )
        )
        assert result_message is not None
        assert result_message.delivery_state == "DELIVERING"
        result_text = result_message.content_json["parts"][0]["text"]
        assert "子任务 1 结果" in result_text
        assert "子任务 2 结果" in result_text
        delivery = db.scalar(
            select(BackgroundTask).where(
                BackgroundTask.aggregate_id == result_message.id,
                BackgroundTask.task_type == "DELIVER_CONVERSATION_MESSAGE",
            )
        )
        assert delivery is not None


def test_dead_conversation_poll_projects_visible_failure(client, db_session_factory):
    from flowweave.modules.tasks.application.handlers import record_terminal_failure

    run_id, attempt_id = _create_run(client, None)
    run = client.get(f"/api/v1/flow-runs/{run_id}").json()
    attempt = run["node_runs"][0]["attempts"][0]
    created = client.post(
        f"/api/v1/node-attempts/{attempt_id}/conversations",
        json={"expected_attempt_state_version": attempt["state_version"]},
        headers={"Idempotency-Key": "dead-poll-visible-failure"},
    )
    assert created.status_code == 202, created.text

    with db_session_factory() as db:
        conversation = db.get(AgentConversation, created.json()["id"])
        assert conversation is not None
        conversation.state = "GENERATING"
        conversation.runtime_job_id = "env-chat:test-runtime"
        conversation.runtime_conversation_id = "runtime-conversation-dead-poll"
        task = BackgroundTask(
            task_type="POLL_CONVERSATION",
            aggregate_type="CONVERSATION",
            aggregate_id=conversation.id,
            idempotency_key=f"dead-poll:{conversation.id}",
            state="DEAD",
            attempts=3,
            max_attempts=3,
        )
        db.add(task)
        db.flush()
        record_terminal_failure(db, task.id, "OpenHands temporarily unavailable")
        db.commit()

    failed = client.get(f"/api/v1/agent-conversations/{created.json()['id']}").json()
    assert failed["state"] == "FAILED"
    messages = client.get(f"/api/v1/agent-conversations/{created.json()['id']}/messages").json()
    assert messages[-1]["message_type"] == "ERROR"
    assert messages[-1]["content"]["error"]["code"] == "RUNTIME_POLL_FAILED"


def test_conversation_recovery_requeues_missing_poll_with_extended_retries(
    client, db_session_factory
):
    run_id, attempt_id = _create_run(client, None)
    run = client.get(f"/api/v1/flow-runs/{run_id}").json()
    attempt = run["node_runs"][0]["attempts"][0]
    created = client.post(
        f"/api/v1/node-attempts/{attempt_id}/conversations",
        json={"expected_attempt_state_version": attempt["state_version"]},
        headers={"Idempotency-Key": "recover-missing-poll"},
    )
    assert created.status_code == 202, created.text

    with db_session_factory() as db:
        conversation = db.get(AgentConversation, created.json()["id"])
        assert conversation is not None
        conversation.state = "GENERATING"
        conversation.runtime_job_id = "env-chat:test-runtime"
        conversation.runtime_conversation_id = "runtime-conversation-recover-poll"
        db.flush()
        assert recover_conversation_tasks(db) == 1
        recovered = db.scalar(
            select(BackgroundTask).where(
                BackgroundTask.aggregate_id == conversation.id,
                BackgroundTask.task_type == "POLL_CONVERSATION",
                BackgroundTask.state == "PENDING",
            )
        )
        assert recovered is not None
        assert recovered.max_attempts == 10
        db.rollback()


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
        automatic = worker_client.get(f"/api/v1/node-attempts/{attempt_id}/conversations").json()[0]
        assert automatic["kind"] == "AUTO"
        assert automatic["connection_status"]["phase"] == "WAITING_WORKER"
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
        binding = runtime.collaboration_requests[0].bindings[0]
        assert binding["display_name"] == "需求"
        assert binding["template_url"] == "https://example.feishu.cn/docx/prd-template"
        assert binding["artifact"]["uri"] == ("https://example.feishu.cn/docx/environment-input")
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
    assert messages[1]["content"]["parts"][0]["text"] == (
        "https://example.feishu.cn/docx/mock-docx-design"
    )

    # AUTO terminal attachment resolves the Attempt-owned sandbox even for
    # rows created before runtime_sandbox_id was projected to the conversation.
    with db_session_factory() as db:
        runtime_conversation = db.get(AgentConversation, automatic["id"])
        runtime_attempt = db.get(NodeAttempt, attempt_id)
        assert runtime_conversation is not None
        assert runtime_attempt is not None
        sandbox_id = str(uuid4())
        current_time = datetime.now(UTC)
        db.add(
            ManagedSandbox(
                id=sandbox_id,
                kind="AGENT_RUNTIME",
                owner_type="ATTEMPT",
                owner_id=attempt_id,
                backend="docker",
                backend_resource_name=f"fw-sbx-{sandbox_id.replace('-', '')}",
                image_reference="runtime:test",
                spec_json={"environment_id": "11111111-1111-4111-8111-111111111111"},
                hard_expires_at=current_time + timedelta(hours=1),
                next_reconcile_at=current_time,
            )
        )
        db.flush()
        runtime_conversation.runtime_job_id = "env-exec:fw-sbx-auto-terminal"
        runtime_conversation.runtime_sandbox_id = None
        runtime_attempt.runtime_sandbox_id = sandbox_id
        db.flush()
        assert terminal_resource_details(db, runtime_conversation.id) == (
            "fw-sbx-auto-terminal",
            sandbox_id,
            "11111111-1111-4111-8111-111111111111",
        )
        db.rollback()

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

    node_workspace = workspace.parents[3]
    shared_image = node_workspace / "files" / "lark-readonly-auth.png"
    shared_image.parent.mkdir(parents=True, exist_ok=True)
    shared_image.write_bytes(b"\x89PNG\r\n\x1a\nflowweave-shared-node-image")
    shared_runtime_path = Path("/workspaces") / shared_image.resolve().relative_to(
        settings.workspace_root.resolve()
    )
    shared_media = client.get(
        f"/api/v1/agent-messages/{messages[1]['id']}/workspace-image",
        params={"source": str(shared_runtime_path)},
    )
    assert shared_media.status_code == 200, shared_media.text
    assert shared_media.headers["content-type"] == "image/png"
    assert shared_media.content == shared_image.read_bytes()

    other_node_image = (
        settings.workspace_root.resolve() / "nodes" / "other-node" / "files" / "private.png"
    )
    other_node_image.parent.mkdir(parents=True, exist_ok=True)
    other_node_image.write_bytes(b"not available to this node")
    blocked_other_node = client.get(
        f"/api/v1/agent-messages/{messages[1]['id']}/workspace-image",
        params={
            "source": str(
                Path("/workspaces")
                / other_node_image.resolve().relative_to(settings.workspace_root.resolve())
            )
        },
    )
    assert blocked_other_node.status_code == 404

    blocked_relative_escape = client.get(
        f"/api/v1/agent-messages/{messages[1]['id']}/workspace-image",
        params={"source": "../../../../files/lark-readonly-auth.png"},
    )
    assert blocked_relative_escape.status_code == 404

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
    assert conversation["connection_status"]["phase"] == "WAITING_WORKER"

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
    assert conversation["connection_status"]["phase"] == "READY"
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
    runtime_handle = RuntimeHandle(
        conversation["runtime_job_id"], conversation["runtime_conversation_id"]
    )
    stopping = worker_client.post(
        f"/api/v1/agent-conversations/{conversation['id']}/stop",
        json={"expected_conversation_version": conversation["state_version"]},
        headers={"Idempotency-Key": "stop-human-conversation-turn"},
    )
    assert stopping.status_code == 202, stopping.text
    assert stopping.json()["state"] == "STOPPING"
    with db_session_factory() as db:
        stop_task = db.scalar(
            select(BackgroundTask).where(
                BackgroundTask.aggregate_id == conversation["id"],
                BackgroundTask.task_type == "STOP_CONVERSATION_RUNTIME",
            )
        )
        assert stop_task is not None
    assert worker._run_once_sync() is True
    stopped = worker_client.get(f"/api/v1/agent-conversations/{conversation['id']}").json()
    assert stopped["state"] == "IDLE"
    assert worker_container.runtime.inspect(runtime_handle).status == "CANCELLED"

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
        recreate_task = db.scalar(
            select(BackgroundTask).where(
                BackgroundTask.aggregate_id == conversation["id"],
                BackgroundTask.task_type == "CREATE_CONVERSATION",
                BackgroundTask.state == "PENDING",
            )
        )
        assert recreate_task is not None
        assert recreate_task.idempotency_key.startswith("recreate-conversation:")
    assert worker._run_once_sync() is True  # recreate Runtime stopped above
    with db_session_factory() as db:
        retry_task = db.scalar(
            select(BackgroundTask).where(
                BackgroundTask.aggregate_id == queued_message["id"],
                BackgroundTask.task_type == "DELIVER_CONVERSATION_MESSAGE",
                BackgroundTask.state == "PENDING",
            )
        )
        assert retry_task is not None
        assert retry_task.idempotency_key.startswith("deliver-conversation-message:")
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
    runtime_handle = RuntimeHandle(
        recovered["runtime_job_id"],
        recovered["runtime_conversation_id"],
    )
    current_run = worker_client.get(f"/api/v1/flow-runs/{run_id}").json()
    current_attempt = current_run["node_runs"][0]["attempts"][0]
    accepted = worker_client.post(
        f"/api/v1/node-attempts/{attempt_id}/accept",
        json={"expected_state_version": current_attempt["state_version"]},
        headers={"Idempotency-Key": "accept-and-clean-human-runtime"},
    )
    assert accepted.status_code == 200, accepted.text
    with db_session_factory() as db:
        cleanup = db.scalar(
            select(BackgroundTask).where(
                BackgroundTask.aggregate_id == conversation["id"],
                BackgroundTask.task_type == "CLEANUP_CONVERSATION_RUNTIME",
            )
        )
        assert cleanup is not None
    assert worker._run_once_sync() is True
    cleaned = worker_client.get(f"/api/v1/agent-conversations/{conversation['id']}").json()
    assert cleaned["state"] == "READ_ONLY"
    assert cleaned["runtime_job_id"] is None
    assert cleaned["runtime_conversation_id"] is None
    assert worker_container.runtime.inspect(runtime_handle).status == "CANCELLED"

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
