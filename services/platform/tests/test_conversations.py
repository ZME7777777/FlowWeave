from __future__ import annotations

import base64
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import uuid4

import pytest
from sqlalchemy import delete, func, select

from flowweave.bootstrap.worker import TaskWorker
from flowweave.modules.conversations.application.service import (
    _append,
    _append_runtime_payload,
    _apply_conversation_result,
    _runtime_message_payload,
    list_subagents,
    process_conversation_condensation,
    process_poll_conversation,
    project_runtime_task_usage,
    recover_conversation_tasks,
    terminal_resource_details,
)
from flowweave.modules.conversations.domain.enums import (
    ConversationState,
    DeliveryMode,
    DeliveryState,
    MessageSource,
    MessageType,
)
from flowweave.modules.conversations.infrastructure.models import RuntimeGoalCommand
from flowweave.modules.tasks.application.service import Lease
from flowweave.runtime.base import (
    RuntimeEvent,
    RuntimeEventBatch,
    RuntimeHandle,
    RuntimeResult,
    RuntimeTaskUsageSnapshot,
    StartAttemptRequest,
)
from flowweave.runtime.contract import OPENHANDS_PACKAGE_VERSIONS
from flowweave.runtime.dependencies import runtime_context
from flowweave.runtime.mock import MockRuntime
from flowweave.shared.domain.tool_policy import OPENHANDS_SOURCE_COMMIT
from flowweave.shared.errors import DomainError
from flowweave.shared.models import (
    AgentConversation,
    AgentMessage,
    BackgroundTask,
    EnvironmentVersion,
    ManagedSandbox,
    NodeAttempt,
    RunEvent,
    RuntimeCondensation,
    RuntimeCondensationCommand,
    RuntimeSubagentTask,
    RuntimeSubagentTaskUsage,
    TaskState,
    TerminalEnvironment,
)
from flowweave.shared.schemas import ConversationAskAgentWrite, ConversationGoalWrite
from flowweave.shared.settings import settings_context


def _run_worker_until(worker: TaskWorker, predicate, *, max_steps: int = 12) -> None:
    for _ in range(max_steps):
        if predicate():
            return
        assert worker._run_once_sync() is True
    assert predicate()


def test_runtime_message_payload_uses_native_skill_trigger_without_prompt_directive():
    content = {
        "parts": [{"type": "text", "text": "$test-skill review this"}],
        "capability_refs": [{"capability_type": "SKILL", "capability_key": "test-skill"}],
    }

    rendered, image_urls = _runtime_message_payload(content)

    assert rendered == "$test-skill review this"
    assert rendered.count("$test-skill") == 1
    assert "先读取并遵循" not in rendered
    assert image_urls == ()


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


def test_runtime_message_payload_maps_selected_skill_to_one_native_keyword_trigger(settings):
    selected = {
        "parts": [{"type": "text", "text": "请复核异常处理。"}],
        "capability_refs": [{"capability_type": "SKILL", "capability_key": "test-skill"}],
    }
    already_triggered = {
        **selected,
        "parts": [{"type": "text", "text": "$test-skill 请复核异常处理。"}],
    }

    with settings_context(settings):
        selected_text, selected_images = _runtime_message_payload(selected)
        existing_text, existing_images = _runtime_message_payload(already_triggered)

    assert selected_text == "$test-skill\n\n请复核异常处理。"
    assert existing_text == "$test-skill 请复核异常处理。"
    assert selected_images == existing_images == ()
    assert "先读取并遵循" not in selected_text


def _create_run(
    api_client,
    skill: dict[str, object] | None,
    *,
    extra_capabilities: list[dict[str, object]] | None = None,
) -> tuple[str, str]:
    payload = _asset_payload(skill)
    payload["capabilities"] = [skill] if skill else []
    payload["capabilities"].extend(extra_capabilities or [])
    asset_response = api_client.post("/api/v1/node-assets", json=payload)
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


def _import_task_capabilities(client) -> list[dict[str, object]]:
    def publish(capability_type: str, name: str, document: dict[str, object]):
        import json

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

    policy = publish(
        "TOOL_POLICY",
        "usage-tools",
        {"name": "usage-tools", "tools": [{"name": "task_tool_set"}, {"name": "terminal"}]},
    )
    definition = publish(
        "AGENT_DEFINITION",
        "reviewer",
        {
            "name": "reviewer",
            "description": "Governed usage reviewer",
            "model": "inherit",
            "tools": ["terminal"],
            "system_prompt": "Review safely.",
            "when_to_use_examples": ["Review a change"],
            "permission_mode": "never_confirm",
            "max_budget_per_run": 0.12,
            "condenser": {"kind": "NoOpCondenser"},
        },
    )
    return [policy, definition]


def _prepare_manual_condensation(client, db_session_factory):
    run_id, attempt_id = _create_run(client, None)
    run = client.get(f"/api/v1/flow-runs/{run_id}").json()
    attempt = run["node_runs"][0]["attempts"][0]
    created = client.post(
        f"/api/v1/node-attempts/{attempt_id}/conversations",
        json={"expected_attempt_state_version": attempt["state_version"]},
        headers={"Idempotency-Key": f"manual-condensation-conversation-{attempt_id}"},
    )
    assert created.status_code == 202, created.text
    conversation_id = created.json()["id"]
    frozen = {
        "kind": "LLM_SUMMARIZING",
        "model_provider_id": "provider-1",
        "model_name": "summary-model",
        "max_size": 80,
        "max_tokens": None,
        "keep_first": 2,
        "minimum_progress": 0.1,
        "hard_context_reset_max_retries": 5,
        "hard_context_reset_context_scaling": 0.8,
    }
    with db_session_factory() as db:
        conversation = db.get(AgentConversation, conversation_id)
        attempt_row = db.get(NodeAttempt, attempt_id)
        assert conversation is not None and attempt_row is not None
        attempt_row.condenser_config_json = frozen
        conversation.context_baseline_json = {
            **conversation.context_baseline_json,
            "condenser": frozen,
        }
        conversation.state = ConversationState.IDLE
        conversation.state_version += 1
        conversation.runtime_adapter = "mock"
        conversation.runtime_job_id = f"mock-job-{conversation_id}"
        conversation.runtime_conversation_id = f"mock-runtime-{conversation_id}"
        conversation.runtime_cursor = "event-0"
        db.execute(
            delete(BackgroundTask).where(
                BackgroundTask.aggregate_id == conversation_id,
                BackgroundTask.task_type == "CREATE_CONVERSATION",
            )
        )
        version = conversation.state_version
        db.commit()

    requested = client.post(
        f"/api/v1/agent-conversations/{conversation_id}/condense",
        json={"expected_conversation_version": version},
        headers={"Idempotency-Key": f"manual-condensation-{conversation_id}"},
    )
    assert requested.status_code == 202, requested.text
    command_id = requested.json()["id"]
    with db_session_factory() as db:
        task = db.scalar(
            select(BackgroundTask).where(
                BackgroundTask.aggregate_id == command_id,
                BackgroundTask.task_type == "CONDENSE_CONVERSATION",
            )
        )
        assert task is not None
        assert task.max_attempts == 20
        task.state = TaskState.RUNNING
        task.lease_owner = "condensation-test"
        task.lease_until = datetime.now(UTC) + timedelta(minutes=5)
        task.lease_generation += 1
        task.attempts += 1
        lease = Lease(task.id, task.lease_owner, task.lease_generation)
        db.commit()
    return conversation_id, command_id, lease


def _ready_human_conversation(client, db_session_factory) -> tuple[str, int]:
    run_id, attempt_id = _create_run(client, None)
    attempt = client.get(f"/api/v1/flow-runs/{run_id}").json()["node_runs"][0]["attempts"][0]
    created = client.post(
        f"/api/v1/node-attempts/{attempt_id}/conversations",
        json={"expected_attempt_state_version": attempt["state_version"]},
        headers={"Idempotency-Key": f"governance-conversation-{uuid4()}"},
    )
    assert created.status_code == 202, created.text
    conversation_id = created.json()["id"]
    with db_session_factory() as db:
        conversation = db.get(AgentConversation, conversation_id)
        assert conversation is not None
        conversation.state = ConversationState.IDLE
        conversation.state_version += 1
        conversation.runtime_adapter = "mock"
        conversation.runtime_job_id = f"mock-job-{conversation_id}"
        conversation.runtime_conversation_id = f"mock-runtime-{conversation_id}"
        conversation.runtime_cursor = "event-0"
        db.execute(
            delete(BackgroundTask).where(
                BackgroundTask.aggregate_id == conversation_id,
                BackgroundTask.task_type == "CREATE_CONVERSATION",
            )
        )
        version = conversation.state_version
        db.commit()
    return conversation_id, version


def test_goal_and_ask_agent_require_authenticated_actor(client, db_session_factory):
    conversation_id, version = _ready_human_conversation(client, db_session_factory)
    with db_session_factory() as db:
        from flowweave.modules.conversations.application.service import (
            request_ask_agent,
            request_goal_command,
        )

        with pytest.raises(DomainError) as goal_error:
            request_goal_command(
                db,
                conversation_id,
                ConversationGoalWrite(
                    expected_conversation_version=version,
                    action="START",
                    objective="Review the current result",
                ),
                f"goal-no-actor-{uuid4()}",
                None,
            )
        assert goal_error.value.code == "GOAL_ACTOR_REQUIRED"
        with pytest.raises(DomainError) as diagnostic_error:
            request_ask_agent(
                db,
                conversation_id,
                ConversationAskAgentWrite(question="What remains?"),
                f"ask-no-actor-{uuid4()}",
                None,
            )
        assert diagnostic_error.value.code == "DIAGNOSTIC_ACTOR_REQUIRED"


def test_message_send_rejects_active_goal(client, db_session_factory):
    from flowweave.modules.conversations.application.service import send_message
    from flowweave.shared.schemas import MessageSendWrite, TextPartWrite

    conversation_id, version = _ready_human_conversation(client, db_session_factory)
    with db_session_factory() as db:
        conversation = db.get(AgentConversation, conversation_id)
        assert conversation is not None and conversation.runtime_conversation_id is not None
        db.add(
            RuntimeGoalCommand(
                attempt_id=conversation.attempt_id,
                conversation_id=conversation.id,
                runtime_conversation_id=conversation.runtime_conversation_id,
                action="START",
                objective="Review the current result",
                max_iterations=2,
                state="RUNNING",
                idempotency_key=f"active-goal-{uuid4()}",
                requested_by="actor-1",
            )
        )
        db.flush()
        with pytest.raises(DomainError) as raised:
            send_message(
                db,
                conversation_id,
                MessageSendWrite(
                    expected_conversation_version=version,
                    client_message_id=str(uuid4()),
                    content=[TextPartWrite(type="text", text="continue")],
                ),
                f"message-during-goal-{uuid4()}",
                "actor-1",
            )
        assert raised.value.code == "CONVERSATION_GOAL_ACTIVE"


class _ControlledCondensationRuntime(MockRuntime):
    def __init__(self, *, visible_after_post: str, fail_after_post: bool = False) -> None:
        super().__init__()
        self.visible = "none"
        self.visible_after_post = visible_after_post
        self.fail_after_post = fail_after_post
        self.condense_calls = 0

    @staticmethod
    def _request_event() -> RuntimeEvent:
        return RuntimeEvent(
            "condense-request-1",
            "CONDENSATION_REQUESTED",
            {"event_name": "CondensationRequest"},
        )

    @staticmethod
    def _completion_event() -> RuntimeEvent:
        return RuntimeEvent(
            "condensation-1",
            "CONDENSATION_COMPLETED",
            {
                "event_name": "Condensation",
                "forgotten_event_ids": ["event-2", "event-3"],
                "summary": "Earlier work was summarized.",
                "summary_offset": 2,
                "llm_response_id": "response-1",
            },
        )

    def read_events(self, handle: RuntimeHandle) -> RuntimeEventBatch:
        if self.visible == "none":
            return RuntimeEventBatch(cursor=handle.cursor)
        events = []
        if self.visible != "completion_only":
            events.append(self._request_event())
        if self.visible == "two_requests":
            events.append(
                RuntimeEvent(
                    "condense-request-2",
                    "CONDENSATION_REQUESTED",
                    {"event_name": "CondensationRequest"},
                )
            )
        if self.visible in {"complete", "completion_only", "two_requests"}:
            events.append(self._completion_event())
        return RuntimeEventBatch(events=tuple(events), cursor=events[-1].cursor)

    def condense(self, handle: RuntimeHandle) -> RuntimeResult:
        self.condense_calls += 1
        self.visible = self.visible_after_post
        if self.fail_after_post:
            raise RuntimeError("response lost after native condensation")
        return RuntimeResult(status="RUNNING", cursor=handle.cursor)


def _renew_condensation_lease(db_session_factory, lease: Lease) -> Lease:
    with db_session_factory() as db:
        task = db.get(BackgroundTask, lease.task_id)
        assert task is not None
        task.state = TaskState.RUNNING
        task.lease_owner = "condensation-retry"
        task.lease_until = datetime.now(UTC) + timedelta(minutes=5)
        task.lease_generation += 1
        task.attempts += 1
        renewed = Lease(task.id, task.lease_owner, task.lease_generation)
        db.commit()
        return renewed


def _assert_condensation_succeeded(db_session_factory, conversation_id: str, command_id: str):
    with db_session_factory() as db:
        command = db.get(RuntimeCondensationCommand, command_id)
        conversation = db.get(AgentConversation, conversation_id)
        assert command is not None and conversation is not None
        assert command.state == "SUCCEEDED"
        assert command.request_event_id == "condense-request-1"
        assert command.completion_event_id == "condensation-1"
        assert conversation.state == ConversationState.IDLE
        assert (
            db.scalar(
                select(func.count())
                .select_from(RuntimeCondensation)
                .where(RuntimeCondensation.conversation_id == conversation_id)
            )
            == 2
        )


def test_condensation_completion_is_projected_idempotently(client, db_session_factory):
    run_id, attempt_id = _create_run(client, None)
    run = client.get(f"/api/v1/flow-runs/{run_id}").json()
    attempt = run["node_runs"][0]["attempts"][0]
    created = client.post(
        f"/api/v1/node-attempts/{attempt_id}/conversations",
        json={"expected_attempt_state_version": attempt["state_version"]},
        headers={"Idempotency-Key": "condensation-projection"},
    )
    assert created.status_code == 202, created.text

    payload = {
        "forgotten_event_ids": ["event-3", "event-2", "event-2"],
        "summary": "Earlier work was summarized.",
        "summary_offset": 2,
        "llm_response_id": "response-1",
    }
    with db_session_factory() as db:
        conversation = db.get(AgentConversation, created.json()["id"])
        assert conversation is not None
        for _ in range(2):
            _append_runtime_payload(
                db,
                conversation,
                cursor="condensation-1",
                event_type="CONDENSATION_COMPLETED",
                payload=payload,
            )
        db.flush()

        rows = list(
            db.scalars(
                select(RuntimeCondensation).where(
                    RuntimeCondensation.conversation_id == conversation.id
                )
            )
        )
        assert len(rows) == 1
        assert rows[0].runtime_event_id == "condensation-1"
        assert rows[0].forgotten_event_ids_json == ["event-2", "event-3"]
        assert rows[0].summary == "Earlier work was summarized."
        assert rows[0].summary_offset == 2
        assert rows[0].llm_response_id == "response-1"
        audits = list(
            db.scalars(
                select(RunEvent).where(
                    RunEvent.attempt_id == attempt_id,
                    RunEvent.event_type == "CONVERSATION_CONDENSATION_COMPLETED",
                )
            )
        )
        assert len(audits) == 1
        assert audits[0].payload_json["forgotten_event_count"] == 2


def test_manual_condensation_projects_native_events_once(client, db_session_factory, settings):
    conversation_id, command_id, lease = _prepare_manual_condensation(client, db_session_factory)
    runtime = _ControlledCondensationRuntime(visible_after_post="complete")
    with (
        settings_context(settings),
        runtime_context(runtime),
        db_session_factory() as db,
    ):
        process_conversation_condensation(db, command_id, lease)

    assert runtime.condense_calls == 1
    _assert_condensation_succeeded(db_session_factory, conversation_id, command_id)


def test_manual_condensation_reconciles_after_response_loss_without_second_post(
    client, db_session_factory, settings
):
    conversation_id, command_id, lease = _prepare_manual_condensation(client, db_session_factory)
    runtime = _ControlledCondensationRuntime(visible_after_post="complete", fail_after_post=True)
    with (
        settings_context(settings),
        runtime_context(runtime),
        db_session_factory() as db,
    ):
        with pytest.raises(RuntimeError, match="response lost"):
            process_conversation_condensation(db, command_id, lease)

    runtime.fail_after_post = False
    renewed = _renew_condensation_lease(db_session_factory, lease)
    with (
        settings_context(settings),
        runtime_context(runtime),
        db_session_factory() as db,
    ):
        process_conversation_condensation(db, command_id, renewed)

    assert runtime.condense_calls == 1
    _assert_condensation_succeeded(db_session_factory, conversation_id, command_id)


def test_manual_condensation_waits_for_completion_without_second_post(
    client, db_session_factory, settings
):
    conversation_id, command_id, lease = _prepare_manual_condensation(client, db_session_factory)
    runtime = _ControlledCondensationRuntime(visible_after_post="request")
    with (
        settings_context(settings),
        runtime_context(runtime),
        db_session_factory() as db,
    ):
        with pytest.raises(DomainError) as pending:
            process_conversation_condensation(db, command_id, lease)
    assert pending.value.code == "RUNTIME_CONDENSATION_PENDING"

    runtime.visible = "complete"
    renewed = _renew_condensation_lease(db_session_factory, lease)
    with (
        settings_context(settings),
        runtime_context(runtime),
        db_session_factory() as db,
    ):
        process_conversation_condensation(db, command_id, renewed)

    assert runtime.condense_calls == 1
    _assert_condensation_succeeded(db_session_factory, conversation_id, command_id)


@pytest.mark.parametrize("visible", ["completion_only", "two_requests"])
def test_manual_condensation_fails_closed_on_unattributable_native_events(
    client, db_session_factory, settings, visible
):
    _conversation_id, command_id, lease = _prepare_manual_condensation(client, db_session_factory)
    runtime = _ControlledCondensationRuntime(visible_after_post="complete")
    runtime.visible = visible
    with (
        settings_context(settings),
        runtime_context(runtime),
        db_session_factory() as db,
    ):
        with pytest.raises(DomainError) as drifted:
            process_conversation_condensation(db, command_id, lease)

    assert drifted.value.code == "RUNTIME_CONDENSATION_DRIFTED"
    assert runtime.condense_calls == 0


def test_human_conversation_projects_intermediate_agent_message_as_progress(
    client, db_session_factory
):
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
            cursor="initialization-message",
            event_type="MESSAGE",
            payload={"source": "agent", "content": "已就绪"},
        )
        after_initialization = db.scalar(
            select(func.count())
            .select_from(AgentMessage)
            .where(AgentMessage.conversation_id == conversation.id)
        )
        assert after_initialization == before

        human = _append(
            db,
            conversation,
            source=MessageSource.HUMAN,
            message_type=MessageType.TEXT,
            content={"parts": [{"type": "text", "text": "开始处理"}]},
            delivery_state=DeliveryState.DELIVERED,
        )
        _append_runtime_payload(
            db,
            conversation,
            cursor="intermediate-message-1",
            event_type="MESSAGE",
            payload={"source": "agent", "content": "我先检查相关文件。"},
        )
        progress = db.scalar(
            select(AgentMessage).where(
                AgentMessage.conversation_id == conversation.id,
                AgentMessage.runtime_event_id == "intermediate-message-1",
            )
        )
        assert progress is not None
        assert progress.message_type == MessageType.TEXT
        assert progress.content_json["presentation"] == "progress"
        assert progress.content_json["turn_message_id"] == human.id
        assert progress.content_json["parts"][0]["text"] == "我先检查相关文件。"

        _append_runtime_payload(
            db,
            conversation,
            cursor="tool-call-1",
            event_type="TOOL_CALL",
            payload={
                "event_name": "CommandAction",
                "content": "读取需求文档",
                "details": {"command": "read requirements"},
            },
        )
        _append_runtime_payload(
            db,
            conversation,
            cursor="tool-result-1",
            event_type="TOOL_RESULT",
            payload={
                "event_name": "CommandObservation",
                "content": "读取完成",
                "details": {"command": "read requirements"},
            },
        )
        _append_runtime_payload(
            db,
            conversation,
            cursor="intermediate-message-2",
            event_type="MESSAGE",
            payload={"source": "agent", "content": "需求已读取，继续核对画板。"},
        )
        _append_runtime_payload(
            db,
            conversation,
            cursor="tool-call-2",
            event_type="TOOL_CALL",
            payload={
                "event_name": "CommandAction",
                "content": "读取画板",
                "details": {"command": "read whiteboard"},
            },
        )
        _append_runtime_payload(
            db,
            conversation,
            cursor="tool-result-2",
            event_type="TOOL_RESULT",
            payload={
                "event_name": "CommandObservation",
                "content": "读取完成",
                "details": {"command": "read whiteboard"},
            },
        )
        _append_runtime_payload(
            db,
            conversation,
            cursor="intermediate-message-3",
            event_type="MESSAGE",
            payload={"source": "agent", "content": "核对完成。"},
        )
        _apply_conversation_result(
            db,
            conversation,
            RuntimeResult(
                status="COMPLETED",
                final_message="核对完成。",
                cursor="finish-1",
            ),
            message_id="human-turn-1",
        )
        db.flush()

        turn_messages = list(
            db.scalars(
                select(AgentMessage)
                .where(
                    AgentMessage.conversation_id == conversation.id,
                    AgentMessage.sequence_no > human.sequence_no,
                )
                .order_by(AgentMessage.sequence_no)
            )
        )
        visible = [
            item for item in turn_messages if item.content_json.get("superseded") is not True
        ]
        assert [item.message_type for item in visible] == [
            MessageType.TEXT,
            MessageType.TOOL_CALL,
            MessageType.TOOL_RESULT,
            MessageType.TEXT,
            MessageType.TOOL_CALL,
            MessageType.TOOL_RESULT,
            MessageType.TEXT,
        ]
        assert [
            item.content_json["parts"][0]["text"]
            for item in visible
            if item.message_type == MessageType.TEXT
        ] == [
            "我先检查相关文件。",
            "需求已读取，继续核对画板。",
            "核对完成。",
        ]
        duplicate_progress = next(
            item for item in turn_messages if item.runtime_event_id == "intermediate-message-3"
        )
        assert duplicate_progress.content_json["superseded_by_final"] is True


def test_native_task_events_are_persisted_as_structured_runtime_facts(client, db_session_factory):
    from flowweave.runtime.openhands import OpenHandsRuntime

    run_id, attempt_id = _create_run(client, None)
    run = client.get(f"/api/v1/flow-runs/{run_id}").json()
    attempt = run["node_runs"][0]["attempts"][0]
    created = client.post(
        f"/api/v1/node-attempts/{attempt_id}/conversations",
        json={"expected_attempt_state_version": attempt["state_version"]},
        headers={"Idempotency-Key": "native-task-runtime-facts"},
    )
    assert created.status_code == 202, created.text
    conversation_id = created.json()["id"]

    requested = OpenHandsRuntime._event_payload(
        {
            "id": "native-task-action-1",
            "kind": "ActionEvent",
            "source": "agent",
            "tool_name": "task_tool_set",
            "tool_call_id": "call-native-task-1",
            "llm_response_id": "response-native-task-1",
            "action": {
                "kind": "TaskAction",
                "description": "Review patch",
                "prompt": "Review the patch without exposing secrets.",
                "subagent_type": "reviewer",
                "resume": None,
            },
        }
    )
    completed = OpenHandsRuntime._event_payload(
        {
            "id": "native-task-observation-1",
            "kind": "ObservationEvent",
            "source": "environment",
            "tool_name": "task_tool_set",
            "tool_call_id": "call-native-task-1",
            "action_id": "native-task-action-1",
            "observation": {
                "kind": "TaskObservation",
                "content": [{"type": "text", "text": "Review passed."}],
                "is_error": False,
                "task_id": "task_00000001",
                "subagent": "reviewer",
                "status": "completed",
            },
        }
    )

    with db_session_factory() as db:
        conversation = db.get(AgentConversation, conversation_id)
        assert conversation is not None
        _append_runtime_payload(
            db,
            conversation,
            cursor="native-task-requested-1",
            event_type="TOOL_CALL",
            payload=requested,
        )
        _append_runtime_payload(
            db,
            conversation,
            cursor="native-task-completed-1",
            event_type="TOOL_RESULT",
            payload=completed,
        )
        db.commit()

    messages = client.get(f"/api/v1/agent-conversations/{conversation_id}/messages").json()
    task_messages = [
        item for item in messages if item["message_type"] in {"TOOL_CALL", "TOOL_RESULT"}
    ]
    assert [item["content"]["tool"]["runtime_task"] for item in task_messages] == [
        {
            "phase": "REQUESTED",
            "action_event_id": "native-task-action-1",
            "tool_call_id": "call-native-task-1",
            "llm_response_id": "response-native-task-1",
            "subagent_type": "reviewer",
            "description": "Review patch",
            "resume_task_id": None,
        },
        {
            "phase": "COMPLETED",
            "action_event_id": "native-task-action-1",
            "observation_event_id": "native-task-observation-1",
            "tool_call_id": "call-native-task-1",
            "task_id": "task_00000001",
            "subagent_type": "reviewer",
            "status": "completed",
        },
    ]
    tasks = client.get(f"/api/v1/agent-conversations/{conversation_id}/subagents").json()
    assert len(tasks) == 1
    assert tasks[0] | {"created_at": None, "updated_at": None, "completed_at": None} == {
        **tasks[0],
        "created_at": None,
        "updated_at": None,
        "completed_at": None,
    }
    assert {
        key: tasks[0][key]
        for key in (
            "action_event_id",
            "tool_call_id",
            "llm_response_id",
            "observation_event_id",
            "runtime_task_id",
            "subagent_type",
            "description",
            "state",
            "native_status",
            "result",
            "error_detail",
        )
    } == {
        "action_event_id": "native-task-action-1",
        "tool_call_id": "call-native-task-1",
        "llm_response_id": "response-native-task-1",
        "observation_event_id": "native-task-observation-1",
        "runtime_task_id": "task_00000001",
        "subagent_type": "reviewer",
        "description": "Review patch",
        "state": "COMPLETED",
        "native_status": "completed",
        "result": "Review passed.",
        "error_detail": None,
    }


def test_native_task_projection_handles_observation_before_action_and_replay(
    client, db_session_factory
):
    run_id, attempt_id = _create_run(client, None)
    attempt = client.get(f"/api/v1/flow-runs/{run_id}").json()["node_runs"][0]["attempts"][0]
    created = client.post(
        f"/api/v1/node-attempts/{attempt_id}/conversations",
        json={"expected_attempt_state_version": attempt["state_version"]},
        headers={"Idempotency-Key": "native-task-out-of-order"},
    )
    conversation_id = created.json()["id"]
    observation = {
        "content": "Review failed safely.",
        "runtime_task": {
            "phase": "ERROR",
            "action_event_id": "task-action-out-of-order",
            "observation_event_id": "task-observation-out-of-order",
            "tool_call_id": "call-out-of-order",
            "task_id": "task_00000002",
            "subagent_type": "reviewer",
            "status": "error",
        },
    }
    action = {
        "runtime_task": {
            "phase": "REQUESTED",
            "action_event_id": "task-action-out-of-order",
            "tool_call_id": "call-out-of-order",
            "llm_response_id": "response-out-of-order",
            "subagent_type": "reviewer",
            "description": "Review safely",
            "resume_task_id": None,
        }
    }
    with db_session_factory() as db:
        conversation = db.get(AgentConversation, conversation_id)
        assert conversation is not None
        _append_runtime_payload(
            db,
            conversation,
            cursor="observation-first",
            event_type="TOOL_RESULT",
            payload=observation,
        )
        _append_runtime_payload(
            db, conversation, cursor="action-second", event_type="TOOL_CALL", payload=action
        )
        _append_runtime_payload(
            db,
            conversation,
            cursor="observation-first",
            event_type="TOOL_RESULT",
            payload=observation,
        )
        db.commit()

    with db_session_factory() as db:
        rows = list(
            db.scalars(
                select(RuntimeSubagentTask).where(
                    RuntimeSubagentTask.conversation_id == conversation_id
                )
            )
        )
        assert len(rows) == 1
        task = rows[0]
        assert task.state == "ERROR"
        assert task.error_detail == "Review failed safely."
        assert task.description == "Review safely"
        assert task.llm_response_id == "response-out-of-order"

        with pytest.raises(DomainError, match="identity changed") as drifted:
            _append_runtime_payload(
                db,
                task_conversation := db.get(AgentConversation, conversation_id),
                cursor="observation-drifted",
                event_type="TOOL_RESULT",
                payload={
                    "runtime_task": {
                        **observation["runtime_task"],
                        "observation_event_id": "different-observation",
                    }
                },
            )
        assert task_conversation is not None
        assert drifted.value.code == "RUNTIME_TASK_PROTOCOL_DRIFT"


def test_native_task_usage_resume_replaces_one_ledger_without_double_counting(
    client, db_session_factory
):
    run_id, attempt_id = _create_run(client, None)
    attempt = client.get(f"/api/v1/flow-runs/{run_id}").json()["node_runs"][0]["attempts"][0]
    created = client.post(
        f"/api/v1/node-attempts/{attempt_id}/conversations",
        json={"expected_attempt_state_version": attempt["state_version"]},
        headers={"Idempotency-Key": "native-task-usage-resume"},
    )
    conversation_id = created.json()["id"]
    with db_session_factory() as db:
        conversation = db.get(AgentConversation, conversation_id)
        assert conversation is not None
        for suffix in ("first", "resume"):
            _append_runtime_payload(
                db,
                conversation,
                cursor=f"resume-observation-{suffix}",
                event_type="TOOL_RESULT",
                payload={
                    "runtime_task": {
                        "phase": "COMPLETED",
                        "action_event_id": f"resume-action-{suffix}",
                        "observation_event_id": f"resume-observation-{suffix}",
                        "task_id": "task_resumed",
                        "subagent_type": "reviewer",
                        "status": "completed",
                    }
                },
            )
        project_runtime_task_usage(
            db,
            conversation,
            (
                RuntimeTaskUsageSnapshot(
                    "task_resumed",
                    "resume-observation-resume",
                    "c" * 64,
                    "model",
                    0.3,
                    30,
                    10,
                    0,
                    0,
                    0,
                    100,
                    40,
                ),
            ),
        )
        db.commit()
        assert db.scalar(select(func.count(RuntimeSubagentTask.id))) == 2
        assert db.scalar(select(func.count(RuntimeSubagentTaskUsage.id))) == 1


def test_native_task_usage_projection_replaces_cumulative_snapshot_and_is_idempotent(
    client, db_session_factory
):
    run_id, attempt_id = _create_run(
        client, None, extra_capabilities=_import_task_capabilities(client)
    )
    attempt = client.get(f"/api/v1/flow-runs/{run_id}").json()["node_runs"][0]["attempts"][0]
    created = client.post(
        f"/api/v1/node-attempts/{attempt_id}/conversations",
        json={"expected_attempt_state_version": attempt["state_version"]},
        headers={"Idempotency-Key": "native-task-usage"},
    )
    conversation_id = created.json()["id"]
    observation = {
        "content": "Review complete.",
        "runtime_task": {
            "phase": "COMPLETED",
            "action_event_id": "usage-action-1",
            "observation_event_id": "usage-observation-1",
            "tool_call_id": "usage-call-1",
            "task_id": "task_00000009",
            "subagent_type": "reviewer",
            "status": "completed",
        },
    }

    def usage(cost: float, prompt: int, completion: int) -> RuntimeTaskUsageSnapshot:
        values = {
            "task_id": "task_00000009",
            "model_name": "openai/test-model",
            "accumulated_cost": cost,
            "prompt_tokens": prompt,
            "completion_tokens": completion,
            "cache_read_tokens": 10,
            "cache_write_tokens": 0,
            "reasoning_tokens": 2,
            "context_window": 4096,
            "per_turn_tokens": prompt + completion,
        }
        import hashlib
        import json

        return RuntimeTaskUsageSnapshot(
            **values,
            source_cursor="usage-observation-1",
            digest=hashlib.sha256(
                json.dumps(values, sort_keys=True, separators=(",", ":")).encode()
            ).hexdigest(),
        )

    with db_session_factory() as db:
        conversation = db.get(AgentConversation, conversation_id)
        assert conversation is not None
        _append_runtime_payload(
            db,
            conversation,
            cursor="usage-observation-1",
            event_type="TOOL_RESULT",
            payload=observation,
        )
        first = usage(0.1, 100, 20)
        project_runtime_task_usage(db, conversation, (first,))
        project_runtime_task_usage(db, conversation, (first,))
        project_runtime_task_usage(db, conversation, (usage(0.15, 140, 30),))
        db.commit()

    response = client.get(f"/api/v1/agent-conversations/{conversation_id}/subagents").json()
    assert len(response) == 1
    assert response[0]["usage"] | {"updated_at": None} == {
        "runtime_task_id": "task_00000009",
        "source_cursor": "usage-observation-1",
        "snapshot_digest": response[0]["usage"]["snapshot_digest"],
        "usage_version": 2,
        "model_name": "openai/test-model",
        "accumulated_cost_usd": 0.15,
        "prompt_tokens": 140,
        "completion_tokens": 30,
        "cache_read_tokens": 10,
        "cache_write_tokens": 0,
        "reasoning_tokens": 2,
        "context_window": 4096,
        "per_turn_tokens": 170,
        "budget_limit_usd": 0.12,
        "budget_state": "EXCEEDED",
        "budget_exceeded_at": response[0]["usage"]["budget_exceeded_at"],
        "updated_at": None,
    }
    with db_session_factory() as db:
        assert db.scalar(select(func.count(RuntimeSubagentTaskUsage.id))) == 1
        assert (
            db.scalar(
                select(func.count(RunEvent.cursor)).where(
                    RunEvent.event_type == "RUNTIME_SUBAGENT_USAGE_PROJECTED"
                )
            )
            == 2
        )
        assert (
            db.scalar(
                select(func.count(RunEvent.cursor)).where(
                    RunEvent.event_type == "RUNTIME_SUBAGENT_BUDGET_EXCEEDED"
                )
            )
            == 1
        )


def test_native_task_usage_rejects_cumulative_regression(client, db_session_factory):
    run_id, attempt_id = _create_run(client, None)
    attempt = client.get(f"/api/v1/flow-runs/{run_id}").json()["node_runs"][0]["attempts"][0]
    created = client.post(
        f"/api/v1/node-attempts/{attempt_id}/conversations",
        json={"expected_attempt_state_version": attempt["state_version"]},
        headers={"Idempotency-Key": "native-task-usage-regression"},
    )
    with db_session_factory() as db:
        conversation = db.get(AgentConversation, created.json()["id"])
        assert conversation is not None
        _append_runtime_payload(
            db,
            conversation,
            cursor="regression-observation",
            event_type="TOOL_RESULT",
            payload={
                "runtime_task": {
                    "phase": "COMPLETED",
                    "action_event_id": "regression-action",
                    "observation_event_id": "regression-observation",
                    "task_id": "task_regression",
                    "subagent_type": "reviewer",
                    "status": "completed",
                }
            },
        )
        baseline = RuntimeTaskUsageSnapshot(
            "task_regression", "cursor-2", "a" * 64, "model", 0.2, 20, 10, 0, 0, 0, 100, 30
        )
        project_runtime_task_usage(db, conversation, (baseline,))
        with pytest.raises(DomainError) as raised:
            project_runtime_task_usage(
                db,
                conversation,
                (
                    RuntimeTaskUsageSnapshot(
                        "task_regression",
                        "cursor-1",
                        "b" * 64,
                        "model",
                        0.1,
                        10,
                        5,
                        0,
                        0,
                        0,
                        100,
                        15,
                    ),
                ),
            )
        assert raised.value.code == "RUNTIME_TASK_USAGE_REGRESSION"


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
        json={
            "expected_conversation_version": version,
            "fork_kind": "SEMANTIC",
            "acknowledge_semantic_state_loss": True,
        },
        headers={"Idempotency-Key": "fork-at-first-answer"},
    )
    assert forked.status_code == 202, forked.text
    fork_messages = client.get(f"/api/v1/agent-conversations/{forked.json()['id']}/messages").json()
    assert [message["source"] for message in fork_messages] == ["PROGRAM"]
    assert [message["content"]["parts"][0]["text"] for message in fork_messages] == [
        "已创建显式语义分支；仅复制可见文本，不继承 Runtime 状态。",
    ]
    assert forked.json()["context_baseline"]["semantic_fork"]["history"] == [
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


def test_private_delegation_envelope_is_not_executed_by_platform(client, db_session_factory):
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

        assert parent.state == "IDLE"
        assert list_subagents(db, parent.id) == []
        projected = db.scalar(
            select(AgentMessage).where(
                AgentMessage.conversation_id == parent.id,
                AgentMessage.runtime_event_id == "delivery-result:delegate-message:delegate-1",
            )
        )
        assert projected is not None
        assert projected.content_json["parts"][0]["text"] == envelope


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
        assert recover_conversation_tasks(db) == 3
        recovered = db.scalar(
            select(BackgroundTask).where(
                BackgroundTask.aggregate_id == conversation.id,
                BackgroundTask.task_type == "POLL_CONVERSATION",
                BackgroundTask.state == "PENDING",
            )
        )
        assert recovered is not None
        assert recovered.max_attempts == 10
        wakeup_channels = {
            str((task.payload_json or {}).get("channel"))
            for task in db.scalars(
                select(BackgroundTask).where(
                    BackgroundTask.aggregate_id == conversation.id,
                    BackgroundTask.task_type == "WAIT_CONVERSATION_WAKEUP",
                    BackgroundTask.state == "PENDING",
                )
            )
        }
        assert wakeup_channels == {"CONVERSATION", "BASH"}
        db.rollback()


def test_rest_poll_projects_events_when_websocket_is_unavailable(
    client, db_session_factory, settings
):
    """WebSocket is only a wake-up/text channel; REST cursor polling is durable."""

    run_id, attempt_id = _create_run(client, None)
    run = client.get(f"/api/v1/flow-runs/{run_id}").json()
    attempt = run["node_runs"][0]["attempts"][0]
    created = client.post(
        f"/api/v1/node-attempts/{attempt_id}/conversations",
        json={"expected_attempt_state_version": attempt["state_version"]},
        headers={"Idempotency-Key": "rest-poll-without-websocket"},
    )
    assert created.status_code == 202, created.text
    conversation_id = created.json()["id"]

    with db_session_factory() as db:
        conversation = db.get(AgentConversation, conversation_id)
        assert conversation is not None
        conversation.state = ConversationState.GENERATING
        conversation.runtime_adapter = "mock"
        conversation.runtime_job_id = f"mock-job-{conversation_id}"
        conversation.runtime_conversation_id = f"mock-runtime-{conversation_id}"
        conversation.runtime_cursor = "anchor-1"
        _append(
            db,
            conversation,
            source=MessageSource.HUMAN,
            message_type=MessageType.TEXT,
            content={"parts": [{"type": "text", "text": "continue"}]},
            delivery_state=DeliveryState.DELIVERED,
            client_message_id="rest-poll-human-turn",
        )
        db.execute(
            delete(BackgroundTask).where(
                BackgroundTask.aggregate_id == conversation_id,
                BackgroundTask.task_type == "CREATE_CONVERSATION",
            )
        )
        task = BackgroundTask(
            task_type="POLL_CONVERSATION",
            aggregate_type="CONVERSATION",
            aggregate_id=conversation_id,
            idempotency_key=f"rest-poll:{conversation_id}",
            state=TaskState.RUNNING,
            lease_owner="rest-poll-test",
            lease_until=datetime.now(UTC) + timedelta(minutes=5),
            lease_generation=1,
            attempts=1,
        )
        db.add(task)
        db.commit()
        lease = Lease(task.id, "rest-poll-test", 1)

    class RestOnlyRuntime(MockRuntime):
        read_calls = 0
        stream_calls = 0

        def read_events(self, _handle: RuntimeHandle) -> RuntimeEventBatch:
            self.read_calls += 1
            return RuntimeEventBatch(
                events=(
                    RuntimeEvent(
                        "message-2",
                        "MESSAGE",
                        {"source": "agent", "content": "durable progress"},
                    ),
                    RuntimeEvent("finish-3", "COMPLETED", {}),
                ),
                cursor="finish-3",
                result=RuntimeResult(
                    status="COMPLETED",
                    final_message="durable final",
                    cursor="finish-3",
                ),
            )

        async def stream_events(self, _handle: RuntimeHandle):
            self.stream_calls += 1
            raise AssertionError("durable polling must not depend on WebSocket")
            yield {}

    runtime = RestOnlyRuntime()
    with (
        settings_context(settings),
        runtime_context(runtime),
        db_session_factory() as db,
    ):
        process_poll_conversation(db, conversation_id, 1, lease)

    assert runtime.read_calls == 1
    assert runtime.stream_calls == 0
    projected = client.get(f"/api/v1/agent-conversations/{conversation_id}").json()
    assert projected["state"] == "IDLE"
    with db_session_factory() as db:
        persisted = db.get(AgentConversation, conversation_id)
        assert persisted is not None
        assert persisted.runtime_cursor == "finish-3"
    messages = client.get(f"/api/v1/agent-conversations/{conversation_id}/messages").json()
    assert any(
        message["content"].get("presentation") == "progress"
        and message["content"]["parts"][0]["text"] == "durable progress"
        for message in messages
    )
    assert messages[-1]["content"]["parts"][0]["text"] == "durable final"


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
            manifest_json={
                "runtime_provenance": {
                    "package_versions": dict(OPENHANDS_PACKAGE_VERSIONS),
                    "source_commit": OPENHANDS_SOURCE_COMMIT,
                    "source_ref": OPENHANDS_SOURCE_COMMIT,
                }
            },
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
        _run_worker_until(
            worker,
            lambda: worker_client.get(f"/api/v1/flow-runs/{run['id']}").json()["node_runs"][0][
                "attempts"
            ][0]["state"]
            == "WAITING_ACCEPTANCE",
        )

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
        _run_worker_until(worker, lambda: bool(runtime.collaboration_requests))
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
    assert before_start.json()["conversation_no"] == 1

    execution = client.post(
        f"/api/v1/node-attempts/{attempt_id}/confirm-start",
        json={"expected_state_version": attempt["state_version"]},
        headers={"Idempotency-Key": "conversation-auto-start"},
    )
    assert execution.status_code == 200, execution.text
    attempt = execution.json()
    assert attempt["state"] == "WAITING_ACCEPTANCE"

    conversations = client.get(f"/api/v1/node-attempts/{attempt_id}/conversations").json()
    assert len(conversations) == 2
    automatic = next(item for item in conversations if item["kind"] == "AUTO")
    assert automatic["kind"] == "AUTO"
    assert automatic["conversation_no"] == 2
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
    _run_worker_until(
        worker,
        lambda: worker_client.get(f"/api/v1/flow-runs/{run_id}").json()["node_runs"][0]["attempts"][
            0
        ]["state"]
        == "WAITING_ACCEPTANCE",
    )
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
    _run_worker_until(
        worker,
        lambda: worker_client.get(f"/api/v1/agent-conversations/{conversation['id']}").json()[
            "state"
        ]
        == "IDLE",
    )

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
        "content": [{"type": "text", "text": "请复核异常处理。"}],
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

    _run_worker_until(
        worker,
        lambda: len(
            worker_client.get(f"/api/v1/agent-conversations/{conversation['id']}/messages").json()
        )
        == 3,
    )
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
    assert "$test-skill\n\n请复核异常处理。" in messages[2]["content"]["parts"][0]["text"]
    assert "先读取并遵循" not in messages[2]["content"]["parts"][0]["text"]

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
    _run_worker_until(
        worker,
        lambda: worker_client.get(f"/api/v1/agent-conversations/{conversation['id']}").json()[
            "state"
        ]
        == "IDLE",
    )
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
    _run_worker_until(
        worker,
        lambda: any(
            message["id"] == queued_message["id"] and message["delivery_state"] == "DELIVERED"
            for message in worker_client.get(
                f"/api/v1/agent-conversations/{conversation['id']}/messages"
            ).json()
        ),
    )

    messages = worker_client.get(
        f"/api/v1/agent-conversations/{conversation['id']}/messages"
    ).json()
    cancelled_message = next(
        message for message in messages if message["id"] == cancelled.json()["id"]
    )
    assert cancelled_message["delivery_state"] == "CANCELLED"
    assert cancelled_message["content"]["presentation"] == "cancelled-queue"
    steered_message = next(message for message in messages if message["id"] == queued_message["id"])
    assert steered_message["source"] == "HUMAN"
    assert steered_message["delivery_state"] == "DELIVERED"
    assert steered_message["content"]["presentation"] == "chat"
    steered_index = messages.index(steered_message)
    assert messages[steered_index + 1]["source"] == "AGENT"
    assert "优先检查并发安全" in messages[steered_index + 1]["content"]["parts"][0]["text"]

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
    _run_worker_until(
        worker,
        lambda: any(
            message["id"] == attachment_message["id"] and message["delivery_state"] == "DELIVERED"
            for message in worker_client.get(
                f"/api/v1/agent-conversations/{conversation['id']}/messages"
            ).json()
        ),
    )
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
    scheduled = None
    for _ in range(12):
        with db_session_factory() as db:
            scheduled = db.scalar(
                select(BackgroundTask).where(
                    BackgroundTask.aggregate_id == recovery.json()["id"],
                    BackgroundTask.task_type == "DELIVER_CONVERSATION_MESSAGE",
                    BackgroundTask.state == "PENDING",
                )
            )
        if scheduled is not None:
            break
        assert worker._run_once_sync() is True
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
    _run_worker_until(
        worker,
        lambda: any(
            message["id"] == recovery.json()["id"] and message["delivery_state"] == "DELIVERED"
            for message in worker_client.get(
                f"/api/v1/agent-conversations/{conversation['id']}/messages"
            ).json()
        ),
    )
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
    _run_worker_until(
        worker,
        lambda: (
            (
                snapshot := worker_client.get(
                    f"/api/v1/agent-conversations/{conversation['id']}"
                ).json()
            )["state"]
            == "READ_ONLY"
            and snapshot["runtime_job_id"] is None
            and snapshot["runtime_conversation_id"] is None
        ),
    )
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
