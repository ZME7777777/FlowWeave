from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from flowweave.modules.agent_sessions.application import (
    conversations as session_conversations,
)
from flowweave.modules.agent_sessions.application import (
    flow_node_conversations,
    flow_node_host,
    flow_node_workspace,
)
from flowweave.modules.agent_sessions.application.host import CREATE_SESSIONS, READ_SESSIONS
from flowweave.modules.agent_sessions.public import AgentConversationBinding
from flowweave.modules.agent_workspaces.application import work_directories
from flowweave.modules.conversations.application import locator
from flowweave.modules.conversations.application import service as conversation_service
from flowweave.runtime.base import RuntimeInputReadiness, RuntimeResult
from flowweave.shared.errors import DomainError
from flowweave.shared.models import (
    BackgroundTask,
    EnvironmentVersion,
    FlowDefinition,
    FlowRun,
    FlowRunRuntime,
    FlowRunRuntimeAllocation,
    FlowRunRuntimeSecretReference,
    NodeAttempt,
    NodeRun,
    RunEvent,
    RunSnapshot,
    TerminalEnvironment,
)
from flowweave.shared.schemas import FlowRunConversationCreateWrite
from flowweave.shared.settings import settings_context


def test_conversation_reference_projection_hides_selected_text_from_message_body() -> None:
    selected_text = "这段引用只能以附件卡片显示"
    prompt, image_urls = session_conversations.message_payload(
        "请基于引用继续处理",
        (),
        ({"event_id": "assistant-event-1", "content": selected_text},),
    )

    assert image_urls == ()
    assert prompt.endswith(
        '{"references":[{"event_id":"assistant-event-1","content":"这段引用只能以附件卡片显示"}]}'
    )
    display_content, references = session_conversations.project_conversation_references(prompt)
    assert display_content == "请基于引用继续处理"
    assert selected_text not in display_content
    assert references == ({"event_id": "assistant-event-1", "content": selected_text},)


def test_conversation_reference_projection_composes_with_attachment_context() -> None:
    attachment_path = (
        "/runtime/workspace/project/uploads/"
        "00000000-0000-0000-0000-000000000001-0123456789abcdef0123456789abcdef--design.png"
    )
    prompt, _image_urls = session_conversations.message_payload(
        "",
        ({"path": attachment_path, "image_data_url": "data:image/png;base64,aGVsbG8="},),
        ({"event_id": "assistant-event-2", "content": "不要展开此引用"},),
    )

    display_content, references = session_conversations.project_conversation_references(prompt)
    assert display_content == f"请查看已上传到共享工作区的附件：\n- {attachment_path}"
    assert references == ({"event_id": "assistant-event-2", "content": "不要展开此引用"},)


def _runtime_context(db: Session) -> tuple[str, str]:
    environment = TerminalEnvironment(
        name=f"environment-{uuid4()}",
        description="",
        base_image="python:3.13",
        base_image_digest="sha256:" + "1" * 64,
    )
    db.add(environment)
    db.flush()
    version = EnvironmentVersion(
        environment_id=environment.id,
        version_no=1,
        state="READY",
        base_image_reference="python@sha256:" + "1" * 64,
        base_image_digest="sha256:" + "1" * 64,
        image_reference="flowweave/environment-test:v1",
        image_digest="sha256:" + "2" * 64,
        manifest_json={},
    )
    flow = FlowDefinition(
        name=f"flow-{uuid4()}",
        description="",
        default_entry_key=None,
    )
    db.add_all((version, flow))
    db.flush()
    run = FlowRun(
        flow_definition_id=flow.id,
        run_no=1,
        name="runtime locator test",
        state="ACTIVE",
        environment_version_id=version.id,
    )
    secret = FlowRunRuntimeSecretReference(
        encrypted_secret_key=b"encrypted",
        secret_digest=uuid4().hex + uuid4().hex,
    )
    db.add_all((run, secret))
    db.flush()
    allocation = FlowRunRuntimeAllocation(
        flow_run_id=run.id,
        secret_reference_id=secret.id,
        relative_root=f".flow-run-runtimes/{run.id}",
    )
    db.add(allocation)
    db.flush()
    runtime = FlowRunRuntime(
        flow_run_id=run.id,
        environment_version_id=version.id,
        runtime_image_digest=version.image_digest,
        workspace_allocation_id=allocation.id,
        status="STARTING",
    )
    db.add(runtime)
    db.flush()
    return run.id, runtime.id


def _connection(runtime_session_id: str, flow_run_id: str, *, generation: int = 1):
    return SimpleNamespace(
        runtime_session_id=runtime_session_id,
        flow_run_id=flow_run_id,
        managed_runtime_id=f"runtime-{generation}",
        resource_name=f"fw-sbx-generation-{generation}",
        generation=generation,
    )


def test_binding_is_an_idempotent_minimal_locator(
    db_session_factory: sessionmaker[Session], monkeypatch: pytest.MonkeyPatch
) -> None:
    with db_session_factory() as db:
        flow_run_id, runtime_session_id = _runtime_context(db)
        monkeypatch.setattr(
            locator.sandboxes,
            "active_flow_run_runtime_connection",
            lambda _db, *, flow_run_id: _connection(runtime_session_id, flow_run_id),
        )

        first = locator.bind_openhands_conversation(
            db,
            flow_run_id=flow_run_id,
            openhands_conversation_id="conversation-original",
            display_label="会话一",
        )
        second = locator.bind_openhands_conversation(
            db,
            flow_run_id=flow_run_id,
            openhands_conversation_id="conversation-original",
            display_label="会话一（更新）",
        )

        assert second.id == first.id
        assert second.runtime_session_id == runtime_session_id
        assert second.openhands_conversation_id == "conversation-original"
        assert second.display_title == "会话一（更新）"
        assert second.host_kind == "FLOW_NODE"
        assert second.flow_run_id == flow_run_id
        assert second.conversation_scope_id == flow_run_id
        assert "flow_run_id" in AgentConversationBinding.__table__.columns


def test_unbound_conversation_fails_closed(
    db_session_factory: sessionmaker[Session],
) -> None:
    with db_session_factory() as db:
        flow_run_id, _runtime_session_id = _runtime_context(db)

        with pytest.raises(DomainError) as caught:
            locator.conversation_locator(
                db,
                flow_run_id=flow_run_id,
                openhands_conversation_id="not-bound",
            )

        assert caught.value.code == "RUNTIME_CONVERSATION_UNBOUND"


def test_route_re_resolves_the_current_generation_without_changing_identity(
    db_session_factory: sessionmaker[Session], monkeypatch: pytest.MonkeyPatch
) -> None:
    with db_session_factory() as db:
        flow_run_id, runtime_session_id = _runtime_context(db)
        current = _connection(runtime_session_id, flow_run_id, generation=1)
        monkeypatch.setattr(
            locator.sandboxes,
            "active_flow_run_runtime_connection",
            lambda _db, *, flow_run_id: current,
        )
        locator.bind_openhands_conversation(
            db,
            flow_run_id=flow_run_id,
            openhands_conversation_id="conversation-original",
        )

        current = _connection(runtime_session_id, flow_run_id, generation=2)
        handle = locator.active_runtime_handle(
            db,
            flow_run_id=flow_run_id,
            openhands_conversation_id="conversation-original",
            cursor=None,
            route_kind="COLLABORATION",
        )

        assert handle.conversation_id == "conversation-original"
        assert handle.runtime_resource_id == "runtime-2"
        assert handle.runtime_resource_name == "fw-sbx-generation-2"
        assert handle.job_id == "env-chat:fw-sbx-generation-2"


def test_route_rejects_locator_session_drift(
    db_session_factory: sessionmaker[Session], monkeypatch: pytest.MonkeyPatch
) -> None:
    with db_session_factory() as db:
        flow_run_id, runtime_session_id = _runtime_context(db)
        current = _connection(runtime_session_id, flow_run_id)
        monkeypatch.setattr(
            locator.sandboxes,
            "active_flow_run_runtime_connection",
            lambda _db, *, flow_run_id: current,
        )
        locator.bind_openhands_conversation(
            db,
            flow_run_id=flow_run_id,
            openhands_conversation_id="conversation-original",
        )

        current = _connection(str(uuid4()), flow_run_id, generation=2)
        with pytest.raises(DomainError) as caught:
            locator.active_runtime_handle(
                db,
                flow_run_id=flow_run_id,
                openhands_conversation_id="conversation-original",
                cursor=None,
                route_kind="EXECUTION",
            )

        assert caught.value.code == "RUNTIME_CONVERSATION_SESSION_DRIFT"


def _node_session_context(db: Session) -> tuple[str, str, str]:
    flow_run_id, runtime_session_id = _runtime_context(db)
    snapshot = RunSnapshot(
        flow_run_id=flow_run_id,
        version=1,
        schema_version=1,
        definition_json={"nodes": []},
        definition_hash="a" * 64,
        runtime_manifest_json={"schema_version": 1, "nodes": {}},
        runtime_manifest_hash="b" * 64,
    )
    node_run = NodeRun(
        flow_run_id=flow_run_id,
        flow_node_snapshot_key="node-1",
        sequence_no=1,
    )
    db.add_all((snapshot, node_run))
    db.flush()
    attempt = NodeAttempt(
        node_run_id=node_run.id,
        attempt_no=1,
        snapshot_id=snapshot.id,
        state="WAITING_START_CONFIRMATION",
        workspace_ref="/runtime/workspace/project/nodes/node-1",
    )
    db.add(attempt)
    db.flush()
    return flow_run_id, runtime_session_id, attempt.id


def test_flow_node_host_resolves_a_frozen_shared_session_context(
    db_session_factory: sessionmaker[Session], monkeypatch: pytest.MonkeyPatch
) -> None:
    with db_session_factory() as db:
        flow_run_id, runtime_session_id, attempt_id = _node_session_context(db)
        starts: list[tuple[str, str]] = []
        monkeypatch.setattr(
            flow_node_host.sandboxes,
            "allocate_node_attempt_runtime",
            lambda _db, *, flow_run_id, node_attempt_id: starts.append(
                (flow_run_id, node_attempt_id)
            ),
        )
        monkeypatch.setattr(
            flow_node_host.sandboxes,
            "ensure_node_attempt_runtime",
            lambda _db, **_kwargs: None,
        )
        monkeypatch.setattr(
            flow_node_host.sandboxes,
            "active_node_attempt_runtime_connection",
            lambda _db, *, flow_run_id, node_attempt_id: _connection(
                runtime_session_id, flow_run_id
            ),
        )
        monkeypatch.setattr(
            flow_node_host,
            "runtime_node",
            lambda **_kwargs: {"asset": {"name": "Node Agent"}},
        )

        host = flow_node_host.resolve_flow_node_session_host(
            db,
            flow_run_id=flow_run_id,
            attempt_id=attempt_id,
            require_start_permission=True,
        )

        assert host.session.host_kind == "FLOW_NODE"
        assert host.session.host_id == flow_run_id
        assert host.session.conversation_scope_id == attempt_id
        assert host.session.runtime_session_id == runtime_session_id
        # The Attempt remains the server-side authorization provenance, while
        # all interactive node sessions share the mounted FlowRun project.
        assert host.session.working_directory == "/runtime/workspace/project"
        assert host.session.permits(CREATE_SESSIONS)
        assert host.session.permits(READ_SESSIONS)
        assert host.node["asset"]["name"] == "Node Agent"
        assert starts == [(flow_run_id, attempt_id)]


def test_flow_node_host_initializes_a_startable_attempt_without_write_permission(
    db_session_factory: sessionmaker[Session], monkeypatch: pytest.MonkeyPatch
) -> None:
    with db_session_factory() as db:
        flow_run_id, runtime_session_id, attempt_id = _node_session_context(db)
        starts: list[tuple[str, str]] = []
        monkeypatch.setattr(
            flow_node_host.sandboxes,
            "allocate_node_attempt_runtime",
            lambda _db, *, flow_run_id, node_attempt_id: starts.append(
                (flow_run_id, node_attempt_id)
            ),
        )
        monkeypatch.setattr(
            flow_node_host.sandboxes,
            "ensure_node_attempt_runtime",
            lambda _db, **_kwargs: None,
        )
        monkeypatch.setattr(
            flow_node_host.sandboxes,
            "active_node_attempt_runtime_connection",
            lambda _db, *, flow_run_id, node_attempt_id: _connection(
                runtime_session_id, flow_run_id
            ),
        )
        monkeypatch.setattr(flow_node_host, "runtime_node", lambda **_kwargs: {"asset": {}})

        host = flow_node_host.resolve_flow_node_session_host(
            db,
            flow_run_id=flow_run_id,
            attempt_id=attempt_id,
            require_start_permission=False,
            ensure_startable_runtime=True,
        )

        assert starts == [(flow_run_id, attempt_id)]
        assert host.session.permits(READ_SESSIONS)
        assert not host.session.permits(CREATE_SESSIONS)


def test_flow_node_host_rejects_non_startable_or_unscoped_attempts(
    db_session_factory: sessionmaker[Session], monkeypatch: pytest.MonkeyPatch
) -> None:
    with db_session_factory() as db:
        flow_run_id, runtime_session_id, attempt_id = _node_session_context(db)
        monkeypatch.setattr(
            flow_node_host.sandboxes,
            "runtime_overview",
            lambda _db, _flow_run_id: {"rerun_required": False},
        )
        monkeypatch.setattr(
            flow_node_host.sandboxes,
            "active_flow_run_runtime_connection",
            lambda _db, *, flow_run_id: _connection(runtime_session_id, flow_run_id),
        )
        monkeypatch.setattr(
            flow_node_host,
            "runtime_node",
            lambda **_kwargs: {"asset": {"name": "Node Agent"}},
        )
        attempt = db.get(NodeAttempt, attempt_id)
        assert attempt is not None
        attempt.state = "RUNNING"
        with pytest.raises(DomainError, match="not ready") as blocked:
            flow_node_host.resolve_flow_node_session_host(
                db,
                flow_run_id=flow_run_id,
                attempt_id=attempt_id,
                require_start_permission=True,
            )
        assert blocked.value.code == "NODE_CONVERSATION_NOT_READY"

        attempt.state = "WAITING_START_CONFIRMATION"
        attempt.workspace_ref = None
        with pytest.raises(DomainError, match="isolated workspace") as unscoped:
            flow_node_host.resolve_flow_node_session_host(
                db,
                flow_run_id=flow_run_id,
                attempt_id=attempt_id,
                require_start_permission=True,
            )
        assert unscoped.value.code == "NODE_WORKSPACE_REQUIRED"


def test_node_session_scope_keeps_bindings_with_the_authorized_attempt(
    db_session_factory: sessionmaker[Session], monkeypatch: pytest.MonkeyPatch
) -> None:
    """A FlowRun shares one Runtime, but node sessions remain attempt-scoped."""

    with db_session_factory() as db:
        flow_run_id, runtime_session_id, first_attempt_id = _node_session_context(db)
        first_attempt = db.get(NodeAttempt, first_attempt_id)
        assert first_attempt is not None
        second_attempt = NodeAttempt(
            node_run_id=first_attempt.node_run_id,
            attempt_no=2,
            snapshot_id=first_attempt.snapshot_id,
            state="WAITING_START_CONFIRMATION",
            workspace_ref="/runtime/workspace/project/nodes/node-2",
        )
        db.add(second_attempt)
        db.flush()
        first_binding = AgentConversationBinding(
            workspace_id=None,
            host_kind="FLOW_NODE",
            host_id=flow_run_id,
            conversation_scope_id=first_attempt_id,
            flow_run_id=flow_run_id,
            node_run_id=first_attempt.node_run_id,
            node_attempt_id=first_attempt_id,
            runtime_session_id=runtime_session_id,
            working_directory=first_attempt.workspace_ref,
            openhands_conversation_id="node-one-conversation",
            lifecycle="ACTIVE",
            create_idempotency_key=f"node-scope:{first_attempt_id}",
        )
        second_binding = AgentConversationBinding(
            workspace_id=None,
            host_kind="FLOW_NODE",
            host_id=flow_run_id,
            conversation_scope_id=second_attempt.id,
            flow_run_id=flow_run_id,
            node_run_id=first_attempt.node_run_id,
            node_attempt_id=second_attempt.id,
            runtime_session_id=runtime_session_id,
            working_directory=second_attempt.workspace_ref,
            openhands_conversation_id="node-two-conversation",
            lifecycle="ACTIVE",
            create_idempotency_key=f"node-scope:{second_attempt.id}",
        )
        db.add_all((first_binding, second_binding))
        db.flush()
        monkeypatch.setattr(
            conversation_service.agent_sessions,
            "resolve_flow_node_session_host",
            lambda *_args, **_kwargs: SimpleNamespace(),
        )

        first_items = conversation_service.list_node_session_views(
            db, flow_run_id=flow_run_id, attempt_id=first_attempt_id
        )
        assert [item["id"] for item in first_items] == [first_binding.id]
        assert (
            conversation_service.get_node_conversation(
                db,
                flow_run_id=flow_run_id,
                attempt_id=first_attempt_id,
                binding_id=first_binding.id,
            )["id"]
            == first_binding.id
        )
        with pytest.raises(DomainError) as isolated:
            conversation_service.get_node_conversation(
                db,
                flow_run_id=flow_run_id,
                attempt_id=first_attempt_id,
                binding_id=second_binding.id,
            )
        assert isolated.value.code == "RESOURCE_NOT_FOUND"


def test_resume_node_conversation_reconciles_a_confirmed_native_pause(
    db_session_factory: sessionmaker[Session], monkeypatch: pytest.MonkeyPatch
) -> None:
    """A lost local interrupt projection must not strand a paused Conversation."""

    with db_session_factory() as db:
        flow_run_id, runtime_session_id, attempt_id = _node_session_context(db)
        attempt = db.get(NodeAttempt, attempt_id)
        assert attempt is not None
        attempt.state = "EXECUTING"
        attempt.runtime_phase = "RUNNING"
        binding = AgentConversationBinding(
            workspace_id=None,
            host_kind="FLOW_NODE",
            host_id=flow_run_id,
            conversation_scope_id=attempt_id,
            flow_run_id=flow_run_id,
            node_run_id=attempt.node_run_id,
            node_attempt_id=attempt_id,
            runtime_session_id=runtime_session_id,
            working_directory=attempt.workspace_ref,
            openhands_conversation_id="native-paused-conversation",
            lifecycle="ACTIVE",
            create_idempotency_key=f"native-paused:{attempt_id}",
        )
        db.add(binding)
        db.flush()

        class NativePausedRuntime:
            def input_readiness(self, _handle: object) -> RuntimeInputReadiness:
                return RuntimeInputReadiness(ready=True, execution_status="paused")

            def run(self, _handle: object) -> RuntimeResult:
                return RuntimeResult(status="RUNNING", cursor="native-leaf")

        runtime = NativePausedRuntime()
        monkeypatch.setattr(
            flow_node_conversations, "_binding_for_attempt", lambda *_args, **_kwargs: binding
        )
        monkeypatch.setattr(
            flow_node_conversations, "_node_handle", lambda *_args, **_kwargs: object()
        )
        monkeypatch.setattr(flow_node_conversations, "get_runtime", lambda: runtime)

        result = flow_node_conversations.resume_node_conversation(
            db,
            flow_run_id=flow_run_id,
            attempt_id=attempt_id,
            binding_id=binding.id,
        )

        db.expire_all()
        resumed = db.get(NodeAttempt, attempt_id)
        run = db.get(FlowRun, flow_run_id)
        assert resumed is not None
        assert run is not None
        assert result == {"accepted": True, "cursor": "native-leaf"}
        assert (resumed.state, resumed.runtime_phase, resumed.state_version) == (
            "EXECUTING",
            "RUNNING",
            3,
        )
        assert run.state == "ACTIVE"
        assert [
            event.event_type
            for event in db.scalars(
                select(RunEvent).where(RunEvent.attempt_id == attempt_id).order_by(RunEvent.cursor)
            )
        ] == ["ATTEMPT_PAUSED", "ATTEMPT_RESUMED"]
        wakeup = db.scalar(
            select(BackgroundTask).where(
                BackgroundTask.idempotency_key == f"wait-runtime-wakeup:{attempt_id}:v3:1"
            )
        )
        assert wakeup is not None
        assert wakeup.task_type == "WAIT_RUNTIME_WAKEUP"


def test_resume_node_conversation_does_not_reconcile_non_paused_native_state(
    db_session_factory: sessionmaker[Session], monkeypatch: pytest.MonkeyPatch
) -> None:
    with db_session_factory() as db:
        flow_run_id, runtime_session_id, attempt_id = _node_session_context(db)
        attempt = db.get(NodeAttempt, attempt_id)
        assert attempt is not None
        attempt.state = "EXECUTING"
        attempt.runtime_phase = "RUNNING"
        binding = AgentConversationBinding(
            workspace_id=None,
            host_kind="FLOW_NODE",
            host_id=flow_run_id,
            conversation_scope_id=attempt_id,
            flow_run_id=flow_run_id,
            node_run_id=attempt.node_run_id,
            node_attempt_id=attempt_id,
            runtime_session_id=runtime_session_id,
            working_directory=attempt.workspace_ref,
            openhands_conversation_id="native-running-conversation",
            lifecycle="ACTIVE",
            create_idempotency_key=f"native-running:{attempt_id}",
        )
        db.add(binding)
        db.flush()

        monkeypatch.setattr(
            flow_node_conversations, "_binding_for_attempt", lambda *_args, **_kwargs: binding
        )
        monkeypatch.setattr(
            flow_node_conversations, "_node_handle", lambda *_args, **_kwargs: object()
        )
        monkeypatch.setattr(
            flow_node_conversations,
            "get_runtime",
            lambda: SimpleNamespace(
                input_readiness=lambda _handle: RuntimeInputReadiness(
                    ready=False, execution_status="running"
                )
            ),
        )

        with pytest.raises(DomainError) as caught:
            flow_node_conversations.resume_node_conversation(
                db, flow_run_id=flow_run_id, attempt_id=attempt_id, binding_id=binding.id
            )
        assert caught.value.code == "VERSION_CONFLICT"
        db.expire_all()
        unchanged = db.get(NodeAttempt, attempt_id)
        assert unchanged is not None
        assert (unchanged.state, unchanged.runtime_phase, unchanged.state_version) == (
            "EXECUTING",
            "RUNNING",
            1,
        )


def test_node_session_list_orders_recent_activity_first(
    db_session_factory: sessionmaker[Session], monkeypatch: pytest.MonkeyPatch
) -> None:
    """Node session navigation follows the Agent Workspace ordering contract."""

    with db_session_factory() as db:
        flow_run_id, runtime_session_id, attempt_id = _node_session_context(db)
        attempt = db.get(NodeAttempt, attempt_id)
        assert attempt is not None
        baseline = datetime(2026, 1, 1, tzinfo=UTC)
        oldest_but_recently_active = AgentConversationBinding(
            workspace_id=None,
            host_kind="FLOW_NODE",
            host_id=flow_run_id,
            conversation_scope_id=attempt_id,
            flow_run_id=flow_run_id,
            node_run_id=attempt.node_run_id,
            node_attempt_id=attempt_id,
            runtime_session_id=runtime_session_id,
            working_directory=attempt.workspace_ref,
            openhands_conversation_id="recently-active-conversation",
            display_title="最近活动",
            lifecycle="ACTIVE",
            create_idempotency_key="recently-active",
            created_at=baseline,
            updated_at=baseline + timedelta(hours=3),
        )
        newest = AgentConversationBinding(
            workspace_id=None,
            host_kind="FLOW_NODE",
            host_id=flow_run_id,
            conversation_scope_id=attempt_id,
            flow_run_id=flow_run_id,
            node_run_id=attempt.node_run_id,
            node_attempt_id=attempt_id,
            runtime_session_id=runtime_session_id,
            working_directory=attempt.workspace_ref,
            openhands_conversation_id="newest-conversation",
            display_title="最新创建",
            lifecycle="ACTIVE",
            create_idempotency_key="newest",
            created_at=baseline + timedelta(hours=2),
            updated_at=baseline + timedelta(hours=2),
        )
        oldest = AgentConversationBinding(
            workspace_id=None,
            host_kind="FLOW_NODE",
            host_id=flow_run_id,
            conversation_scope_id=attempt_id,
            flow_run_id=flow_run_id,
            node_run_id=attempt.node_run_id,
            node_attempt_id=attempt_id,
            runtime_session_id=runtime_session_id,
            working_directory=attempt.workspace_ref,
            openhands_conversation_id="oldest-conversation",
            display_title="最早会话",
            lifecycle="ACTIVE",
            create_idempotency_key="oldest",
            created_at=baseline + timedelta(hours=1),
            updated_at=baseline + timedelta(hours=1),
        )
        db.add_all((oldest, newest, oldest_but_recently_active))
        db.flush()
        monkeypatch.setattr(
            conversation_service.agent_sessions,
            "resolve_flow_node_session_host",
            lambda *_args, **_kwargs: SimpleNamespace(),
        )

        items = conversation_service.list_node_session_views(
            db, flow_run_id=flow_run_id, attempt_id=attempt_id
        )

        assert [item["id"] for item in items] == [
            oldest_but_recently_active.id,
            newest.id,
            oldest.id,
        ]


def test_node_workspace_projection_isolates_node_attempt_work_directories(
    settings, db_session_factory: sessionmaker[Session], monkeypatch: pytest.MonkeyPatch
) -> None:
    """Each node entry exposes only its own Attempt Runtime project root."""

    from flowweave.modules.sandboxes.application.runtime_allocation import (
        allocate_node_attempt_runtime,
        node_attempt_workspace_project_path,
    )

    settings = settings.model_copy(
        update={
            "runtime_host_workspace_root": "/srv/flowweave/workspaces",
            "ide_ssh_host": "dev.flowweave.test",
            "ide_ssh_user": "flowweave",
        }
    )
    with settings_context(settings), db_session_factory() as db:
        flow_run_id, runtime_session_id, first_attempt_id = _node_session_context(db)
        first_attempt = db.get(NodeAttempt, first_attempt_id)
        assert first_attempt is not None
        second_node_run = NodeRun(
            flow_run_id=flow_run_id,
            flow_node_snapshot_key="node-2",
            sequence_no=2,
        )
        db.add(second_node_run)
        db.flush()
        second_attempt = NodeAttempt(
            node_run_id=second_node_run.id,
            attempt_no=1,
            snapshot_id=first_attempt.snapshot_id,
            state="WAITING_START_CONFIRMATION",
            workspace_ref="",
        )
        db.add(second_attempt)
        db.flush()
        allocate_node_attempt_runtime(db, flow_run_id=flow_run_id, node_attempt_id=first_attempt_id)
        allocate_node_attempt_runtime(
            db, flow_run_id=flow_run_id, node_attempt_id=second_attempt.id
        )
        first_root = node_attempt_workspace_project_path(
            db, flow_run_id=flow_run_id, node_attempt_id=first_attempt_id
        )
        second_root = node_attempt_workspace_project_path(
            db, flow_run_id=flow_run_id, node_attempt_id=second_attempt.id
        )
        (first_root / "first.txt").write_text("first")
        (second_root / "second.txt").write_text("second")
        first_attempt.workspace_ref = str(first_root)
        second_attempt.workspace_ref = str(second_root)
        db.flush()
        monkeypatch.setattr(
            flow_node_workspace,
            "resolve_flow_node_session_host",
            lambda _db, **_kwargs: SimpleNamespace(
                attempt_id=_kwargs["attempt_id"],
                session=SimpleNamespace(
                    working_directory=(
                        "/runtime/workspace/project"
                        if _kwargs["attempt_id"] == first_attempt_id
                        else "/runtime/workspace/project"
                    )
                ),
            ),
        )

        details = flow_node_workspace.details(
            db, flow_run_id=flow_run_id, attempt_id=first_attempt_id
        )
        assert details["ide"]["gateway"] == {
            "supported": True,
            "status": "可通过 SSH 连接",
            "note": "在 JetBrains Gateway 中选择 SSH，并打开以下宿主机目录。",
            "transport": "SSH_REMOTE",
            "host": "dev.flowweave.test",
            "port": 22,
            "user": "flowweave",
            "path": str(
                Path("/srv/flowweave/workspaces") / first_root.relative_to(settings.workspace_root)
            ),
            "ssh_command": "ssh -p 22 flowweave@dev.flowweave.test",
        }
        paths = {item["path"] for item in details["files"]}
        first_runtime_path = "/runtime/workspace/project/first.txt"
        second_runtime_path = "/runtime/workspace/project/second.txt"
        assert first_runtime_path in paths
        assert second_runtime_path not in paths
        content, _content_type, filename = flow_node_workspace.read_file(
            db,
            flow_run_id=flow_run_id,
            attempt_id=first_attempt_id,
            binding_id=None,
            work_directory_id=None,
            path=first_runtime_path,
        )
        assert content == b"first"
        assert filename == "first.txt"
        directory = work_directories.create_flow_run_work_directory(
            db, flow_run_id, first_attempt_id, "节点一目录", (".",)
        )
        first_directories = work_directories.list_flow_run_work_directories(
            db, flow_run_id, first_attempt_id
        )
        second_directories = work_directories.list_flow_run_work_directories(
            db, flow_run_id, second_attempt.id
        )
        assert [item["id"] for item in first_directories["items"]] == [directory["id"]]
        assert second_directories["items"] == []
        with pytest.raises(DomainError) as caught:
            work_directories.get_flow_run_work_directory(
                db, flow_run_id, second_attempt.id, str(directory["id"])
            )
        assert caught.value.code == "AGENT_WORK_DIRECTORY_NOT_FOUND"
        with pytest.raises(DomainError) as caught:
            flow_node_workspace.details(
                db,
                flow_run_id=flow_run_id,
                attempt_id=second_attempt.id,
                work_directory_id=str(directory["id"]),
            )
        assert caught.value.code == "AGENT_WORK_DIRECTORY_NOT_FOUND"
        scoped = flow_node_workspace.details(
            db,
            flow_run_id=flow_run_id,
            attempt_id=first_attempt_id,
            work_directory_id=str(directory["id"]),
        )
        scoped_paths = {item["path"] for item in scoped["files"]}
        assert scoped["scope"] == {
            "kind": "WORK_DIRECTORY",
            "id": directory["id"],
            "display_name": "节点一目录",
        }
        assert first_runtime_path in scoped_paths
        assert second_runtime_path not in scoped_paths


def test_node_candidate_output_preview_resolves_only_a_declared_relative_file(
    settings, db_session_factory: sessionmaker[Session], monkeypatch: pytest.MonkeyPatch
) -> None:
    """A candidate preview never trusts a path from the Conversation transcript."""

    from flowweave.modules.sandboxes.application.runtime_allocation import (
        allocate_node_attempt_runtime,
        node_attempt_workspace_project_path,
    )

    with settings_context(settings), db_session_factory() as db:
        flow_run_id, _runtime_session_id, attempt_id = _node_session_context(db)
        attempt = db.get(NodeAttempt, attempt_id)
        assert attempt is not None
        allocate_node_attempt_runtime(db, flow_run_id=flow_run_id, node_attempt_id=attempt_id)
        project_root = node_attempt_workspace_project_path(
            db, flow_run_id=flow_run_id, node_attempt_id=attempt_id
        )
        (project_root / "report.md").write_text("# Candidate report\n")
        attempt.workspace_ref = str(project_root)
        attempt.output_targets_json = {
            "report": {"artifact_type": "FILE"},
            "link": {"artifact_type": "URL"},
        }
        db.flush()
        monkeypatch.setattr(
            flow_node_workspace,
            "resolve_flow_node_session_host",
            lambda _db, **_kwargs: SimpleNamespace(
                working_directory=str(project_root),
                node={"asset": {"id": "asset-1"}},
            ),
        )

        content, content_type, filename = flow_node_workspace.read_candidate_output_file(
            db,
            flow_run_id=flow_run_id,
            attempt_id=attempt_id,
            field_key="report",
            path="report.md",
        )

        assert content == b"# Candidate report\n"
        assert content_type == "text/markdown"
        assert filename == "report.md"
        with pytest.raises(DomainError) as invalid_path:
            flow_node_workspace.read_candidate_output_file(
                db,
                flow_run_id=flow_run_id,
                attempt_id=attempt_id,
                field_key="report",
                path="../other.txt",
            )
        assert invalid_path.value.code == "RUNTIME_OUTPUT_INVALID"
        with pytest.raises(DomainError) as invalid_slot:
            flow_node_workspace.read_candidate_output_file(
                db,
                flow_run_id=flow_run_id,
                attempt_id=attempt_id,
                field_key="link",
                path="report.md",
            )
        assert invalid_slot.value.code == "RUNTIME_OUTPUT_INVALID"


def test_flow_run_creation_resolves_the_node_host_once(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    host = SimpleNamespace(attempt_id="attempt-1", flow_run_id="run-1")
    attempt = SimpleNamespace(id="attempt-1", state_version=7)
    observed: dict[str, object] = {}
    monkeypatch.setattr(
        conversation_service.agent_sessions,
        "resolve_flow_node_session_host",
        lambda _db, *, flow_run_id, attempt_id, require_start_permission: observed.update(
            {
                "flow_run_id": flow_run_id,
                "attempt_id": attempt_id,
                "require_start_permission": require_start_permission,
            }
        )
        or host,
    )
    monkeypatch.setattr(conversation_service, "_attempt", lambda _db, _attempt_id: attempt)
    monkeypatch.setattr(
        conversation_service,
        "create_conversation",
        lambda _db, attempt_id, payload, idempotency_key, *, host: observed.update(
            {
                "resolved_attempt_id": attempt_id,
                "state_version": payload.expected_attempt_state_version,
                "idempotency_key": idempotency_key,
                "host": host,
            }
        )
        or {"id": "binding-1"},
    )

    result = conversation_service.create_flow_run_conversation(
        SimpleNamespace(scalar=lambda _query: None),
        "run-1",
        FlowRunConversationCreateWrite(
            node_attempt_id="attempt-1",
            title="Node session",
        ),
        "create-1",
    )

    assert result == {"id": "binding-1"}
    assert observed == {
        "flow_run_id": "run-1",
        "attempt_id": "attempt-1",
        "require_start_permission": True,
        "resolved_attempt_id": "attempt-1",
        "state_version": 7,
        "idempotency_key": "create-1",
        "host": host,
    }
