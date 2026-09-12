from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal
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
from flowweave.modules.agent_sessions.application import usage as usage_projection
from flowweave.modules.agent_sessions.application.event_branch import complete_active_branch
from flowweave.modules.agent_sessions.application.host import (
    ACCESS_TERMINAL,
    CREATE_SESSIONS,
    READ_SESSIONS,
)
from flowweave.modules.agent_sessions.infrastructure.models import AgentConversationUsageBucket
from flowweave.modules.agent_sessions.public import AgentConversationBinding
from flowweave.modules.agent_workspaces.application import work_directories
from flowweave.modules.conversations.application import locator
from flowweave.modules.conversations.application import service as conversation_service
from flowweave.runtime.base import (
    RuntimeEvent,
    RuntimeEventBatch,
    RuntimeHandle,
    RuntimeInputReadiness,
    RuntimeResult,
)
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


def test_usage_cost_defaults_to_zero_before_orm_flush() -> None:
    assert usage_projection._cost(None) == Decimal("0")
    assert usage_projection._cost(0.125) == Decimal("0.125")


def test_usage_token_defaults_to_zero_before_orm_flush() -> None:
    bucket = AgentConversationUsageBucket(binding_id="binding", usage_id="usage")

    assert bucket.observed_prompt_tokens is None
    assert usage_projection._token_count(bucket.observed_prompt_tokens) == 0
    assert usage_projection._token_count(17) == 17


def test_conversation_reference_projection_hides_selected_text_from_message_body() -> None:
    selected_text = "这段引用只能以附件卡片显示"
    prompt, image_urls = session_conversations.message_payload(
        "请基于引用继续处理",
        (),
        ({"event_id": "assistant-event-1", "content": selected_text},),
    )

    assert image_urls == ()
    assert session_conversations._MESSAGE_CONTEXT_V5_MARKER in prompt
    assert prompt.index(selected_text) < prompt.index("请基于引用继续处理")
    assert "reference_materials" in prompt
    assert "current_user_request" in prompt
    display_content, references, workspace_references = (
        session_conversations.project_conversation_references(prompt)
    )
    assert display_content == "请基于引用继续处理"
    assert selected_text not in display_content
    assert references == ({"event_id": "assistant-event-1", "content": selected_text},)
    assert workspace_references == ()


def test_plain_message_bypasses_context_envelope() -> None:
    prompt, _image_urls = session_conversations.message_payload("开始实现代码", (), ())

    display_content, references, workspace_references = (
        session_conversations.project_conversation_references(prompt)
    )

    assert session_conversations._MESSAGE_CONTEXT_V5_MARKER not in prompt
    assert prompt == "开始实现代码"
    assert display_content == "开始实现代码"
    assert references == ()
    assert workspace_references == ()


def test_attachment_only_message_bypasses_context_envelope() -> None:
    attachment_path = (
        "/runtime/workspace/project/uploads/"
        "00000000-0000-0000-0000-000000000001-0123456789abcdef0123456789abcdef--notes.txt"
    )
    prompt, _image_urls = session_conversations.message_payload(
        "请阅读附件", ({"path": attachment_path},), ()
    )

    assert session_conversations._MESSAGE_CONTEXT_V5_MARKER not in prompt
    assert prompt == f"请阅读附件\n\n已上传到共享工作区的附件：\n- {attachment_path}"


def test_conversation_references_are_resolved_by_formal_native_event_id() -> None:
    class Runtime:
        def read_event(self, _handle: object, event_id: str) -> RuntimeEvent | None:
            assert event_id == "native-event"
            return RuntimeEvent(
                cursor="native-event",
                event_type="MESSAGE",
                payload={"content": "服务端已验证的引用内容"},
            )

    resolved = session_conversations.resolve_conversation_references(
        Runtime(),  # type: ignore[arg-type]
        RuntimeHandle(job_id="test", conversation_id="conversation"),
        ({"event_id": "native-event"},),
    )

    assert resolved == ({"event_id": "native-event", "content": "服务端已验证的引用内容"},)


def test_rewrite_target_is_read_by_id_without_history_page_scan() -> None:
    class Runtime:
        def read_active_events(self, _handle: object) -> RuntimeEventBatch:
            raise AssertionError("a rewrite target must not scan the active history window")

        def read_event(self, _handle: object, event_id: str) -> RuntimeEvent | None:
            assert event_id == "old-user-event"
            return RuntimeEvent(
                cursor=event_id,
                event_type="MESSAGE",
                payload={"source": "user", "parent_id": "old-parent"},
            )

    target = session_conversations.validated_user_message_event(
        Runtime(),  # type: ignore[arg-type]
        RuntimeHandle(job_id="test", conversation_id="conversation"),
        "old-user-event",
    )

    assert target.cursor == "old-user-event"
    assert target.payload["parent_id"] == "old-parent"


def test_rewrite_target_rejects_missing_or_non_user_native_event() -> None:
    class Runtime:
        def __init__(self, event: RuntimeEvent | None) -> None:
            self.event = event

        def read_event(self, _handle: object, _event_id: str) -> RuntimeEvent | None:
            return self.event

    handle = RuntimeHandle(job_id="test", conversation_id="conversation")
    events = (
        None,
        RuntimeEvent("tool", "TOOL_CALL", {}),
        RuntimeEvent("agent", "MESSAGE", {"source": "agent"}),
    )
    for event in events:
        with pytest.raises(DomainError) as caught:
            session_conversations.validated_user_message_event(
                Runtime(event),  # type: ignore[arg-type]
                handle,
                "requested",
            )
        assert caught.value.code == "AGENT_MESSAGE_REWRITE_UNAVAILABLE"


def test_complete_active_branch_hydrates_every_formal_history_page() -> None:
    calls: list[tuple[str | None, str | None]] = []

    def read(handle: RuntimeHandle) -> RuntimeEventBatch:
        calls.append((handle.history_cursor, handle.active_branch_leaf_event_id))
        if handle.history_cursor == "older":
            return RuntimeEventBatch(
                events=(RuntimeEvent("old-user", "MESSAGE", {"parent_id": "__root__"}),),
                cursor="latest-tool",
            )
        return RuntimeEventBatch(
            events=(RuntimeEvent("latest-tool", "TOOL_CALL", {"parent_id": "old-user"}),),
            cursor="latest-tool",
            history_cursor="older",
            result=RuntimeResult(status="RUNNING"),
        )

    hydrated = complete_active_branch(
        read, RuntimeHandle(job_id="job", conversation_id="conversation")
    )

    assert calls == [
        (None, None),
        ("older", "latest-tool"),
        # Recheck formal HEAD before accepting a branch whose older pages were
        # read using the first page's snapshot.
        (None, None),
    ]
    assert [event.cursor for event in hydrated.events] == ["old-user", "latest-tool"]
    assert hydrated.cursor == "latest-tool"
    assert hydrated.history_cursor is None
    assert hydrated.result == RuntimeResult(status="RUNNING")


def test_complete_active_branch_rejects_head_drift_between_pages() -> None:
    def read(handle: RuntimeHandle) -> RuntimeEventBatch:
        return RuntimeEventBatch(
            cursor="changed-head" if handle.history_cursor else "first-head",
            history_cursor="older" if handle.history_cursor is None else None,
        )

    with pytest.raises(ValueError, match="active branch changed"):
        complete_active_branch(read, RuntimeHandle(job_id="job", conversation_id="conversation"))


def test_hydration_reuses_active_batch_context_and_readiness(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workspace = object()
    binding = object()
    handle = RuntimeHandle(job_id="job", conversation_id="conversation")
    context = {"model_name": "test-model", "window_tokens": 128_000}
    readiness = RuntimeInputReadiness(ready=False, execution_status="running")
    captured: dict[str, object] = {}

    class Runtime:
        def read_active_events(self, _handle: object) -> RuntimeEventBatch:
            return RuntimeEventBatch(
                events=(RuntimeEvent("event", "MESSAGE", {"source": "user"}),),
                cursor="event",
                context=context,
                readiness=readiness,
            )

        def conversation_context(self, _handle: object):
            raise AssertionError("hydration must reuse its active-batch context")

        def input_readiness(self, _handle: object):
            raise AssertionError("hydration must reuse its active-batch readiness")

    monkeypatch.setattr(session_conversations, "_workspace", lambda _db, _id: workspace)
    monkeypatch.setattr(session_conversations, "_binding", lambda _db, _workspace_id, _id: binding)
    monkeypatch.setattr(session_conversations, "_handle", lambda _db, _workspace, _binding: handle)
    monkeypatch.setattr(session_conversations, "get_runtime", lambda: Runtime())
    monkeypatch.setattr(
        session_conversations,
        "events",
        lambda *_args, **kwargs: captured.update(kwargs) or {"events": []},
    )

    hydrated = session_conversations.hydrate_conversation(None, "workspace", "binding")

    assert captured["context_override"] == context
    assert hydrated == {
        "events": {"events": []},
        "context": context,
        "readiness": readiness.as_dict(),
    }


def test_node_hydration_reuses_active_batch_context_and_readiness(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    binding = object()
    handle = RuntimeHandle(job_id="job", conversation_id="conversation")
    context = {"model_name": "node-model", "window_tokens": 128_000}
    readiness = RuntimeInputReadiness(ready=True, execution_status="idle")

    class Runtime:
        def read_active_events(self, _handle: object) -> RuntimeEventBatch:
            return RuntimeEventBatch(cursor="event", context=context, readiness=readiness)

        def conversation_context(self, _handle: object):
            raise AssertionError("node hydration must reuse active-batch context")

        def input_readiness(self, _handle: object):
            raise AssertionError("node hydration must reuse active-batch readiness")

    monkeypatch.setattr(
        flow_node_conversations, "_binding_for_attempt", lambda *_args, **_kwargs: binding
    )
    monkeypatch.setattr(
        flow_node_conversations, "_binding_for_run", lambda *_args, **_kwargs: binding
    )
    monkeypatch.setattr(
        flow_node_conversations, "_flow_run_handle", lambda *_args, **_kwargs: handle
    )
    monkeypatch.setattr(flow_node_conversations, "get_runtime", lambda: Runtime())
    monkeypatch.setattr(flow_node_conversations, "_event_batch_dict", lambda *_args: {"events": []})

    hydrated = flow_node_conversations.hydrate_node_conversation(
        None, flow_run_id="run", attempt_id="attempt", binding_id="binding"
    )

    assert hydrated == {
        "events": {"events": []},
        "context": context,
        "readiness": readiness.as_dict(),
    }


def test_conversation_head_reads_only_the_formal_native_leaf(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workspace = object()
    binding = object()
    calls: list[object] = []

    class Runtime:
        def read_active_events(self, handle: object) -> RuntimeEventBatch:
            calls.append(handle)
            return RuntimeEventBatch(
                events=(RuntimeEvent("latest", "MESSAGE", {"content": "ignored window"}),),
                cursor="formal-head",
            )

    runtime = Runtime()
    handle = RuntimeHandle(job_id="job", conversation_id="conversation")
    monkeypatch.setattr(session_conversations, "_workspace", lambda _db, _id: workspace)
    monkeypatch.setattr(session_conversations, "_binding", lambda _db, _workspace_id, _id: binding)
    monkeypatch.setattr(session_conversations, "_handle", lambda _db, _workspace, _binding: handle)
    monkeypatch.setattr(session_conversations, "get_runtime", lambda: runtime)

    assert session_conversations.conversation_head(None, "workspace", "binding") == {
        "cursor": "formal-head"
    }
    assert calls == [handle]


def test_node_conversation_head_reads_only_the_formal_native_leaf(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[object] = []

    class Runtime:
        def read_active_events(self, handle: object) -> RuntimeEventBatch:
            calls.append(handle)
            return RuntimeEventBatch(cursor="node-formal-head")

    handle = RuntimeHandle(job_id="job", conversation_id="conversation")
    monkeypatch.setattr(flow_node_conversations, "_node_handle", lambda _db, **_kwargs: handle)
    monkeypatch.setattr(flow_node_conversations, "get_runtime", lambda: Runtime())

    assert flow_node_conversations.node_conversation_head(
        None, flow_run_id="flow", attempt_id="attempt", binding_id="binding"
    ) == {"cursor": "node-formal-head"}
    assert calls == [handle]


def test_conversation_reference_resolver_rejects_client_supplied_content() -> None:
    with pytest.raises(DomainError, match="会话引用无效"):
        session_conversations.resolve_conversation_references(
            object(),  # type: ignore[arg-type]
            object(),  # type: ignore[arg-type]
            ({"event_id": "native-event", "content": "浏览器伪造内容"},),
        )


def test_conversation_reference_total_budget_is_limited() -> None:
    references = tuple(
        {"event_id": f"event-{index}", "content": "x" * 10_000} for index in range(3)
    )

    with pytest.raises(DomainError, match="引用内容过长"):
        session_conversations.message_payload("继续", (), references)


def test_message_context_v3_is_hidden_from_existing_user_messages() -> None:
    prompt = (
        "这是 FlowWeave 生成的结构化消息上下文。"
        + session_conversations._MESSAGE_CONTEXT_V3_MARKER
        + (
            '{"version":3,"references":[],"current_message":'
            '{"category":"CURRENT_USER_REQUEST","content":"开始实现代码"}}'
        )
    )

    display_content, references, workspace_references = (
        session_conversations.project_conversation_references(prompt)
    )

    assert display_content == "开始实现代码"
    assert references == ()
    assert workspace_references == ()


def test_conversation_reference_projection_supports_legacy_suffix_format() -> None:
    prompt = (
        "请基于引用继续处理"
        + session_conversations._CONVERSATION_REFERENCE_MARKER
        + '{"references":[{"event_id":"assistant-event-1","content":"旧引用"}]}'
    )

    display_content, references, workspace_references = (
        session_conversations.project_conversation_references(prompt)
    )

    assert display_content == "请基于引用继续处理"
    assert references == ({"event_id": "assistant-event-1", "content": "旧引用"},)
    assert workspace_references == ()


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

    display_content, references, workspace_references = (
        session_conversations.project_conversation_references(prompt)
    )
    assert display_content == f"请查看已上传到共享工作区的附件：\n- {attachment_path}"
    assert references == ({"event_id": "assistant-event-2", "content": "不要展开此引用"},)
    assert workspace_references == ()


def test_workspace_reference_projection_keeps_container_paths_out_of_message_body() -> None:
    reference = {
        "path": "/runtime/workspace/project/src",
        "kind": "directory",
        "display_name": "src",
    }
    prompt, image_urls = session_conversations.message_payload(
        "请检查这个目录",
        (),
        (),
        (reference,),
    )

    display_content, references, workspace_references = (
        session_conversations.project_conversation_references(prompt)
    )

    assert image_urls == ()
    assert display_content == "请检查这个目录"
    assert references == ()
    assert workspace_references == (reference,)


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


def test_binding_accepts_record_scoped_runtime_workspace(
    db_session_factory: sessionmaker[Session],
) -> None:
    with db_session_factory() as db:
        flow_run_id, runtime_session_id = _runtime_context(db)
        record_id = str(uuid4())
        binding = AgentConversationBinding(
            workspace_id=None,
            runtime_session_id=runtime_session_id,
            host_kind="FLOW_NODE",
            host_id=flow_run_id,
            conversation_scope_id=record_id,
            flow_run_id=flow_run_id,
            node_run_id=record_id,
            node_attempt_id=record_id,
            working_directory=f"/runtime/workspace/{record_id}",
            openhands_conversation_id=str(uuid4()),
            lifecycle="PROVISIONING",
            create_idempotency_key=f"record-workspace:{record_id}",
        )
        db.add(binding)
        db.flush()


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
        ensured: list[str] = []
        monkeypatch.setattr(
            flow_node_host.sandboxes,
            "ensure_flow_run_runtime",
            lambda _db, **_kwargs: ensured.append(str(_kwargs["flow_run_id"])),
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
        assert ensured == [flow_run_id]


def test_flow_node_host_initializes_a_startable_attempt_without_write_permission(
    db_session_factory: sessionmaker[Session], monkeypatch: pytest.MonkeyPatch
) -> None:
    with db_session_factory() as db:
        flow_run_id, runtime_session_id, attempt_id = _node_session_context(db)
        ensured: list[str] = []
        monkeypatch.setattr(
            flow_node_host.sandboxes,
            "ensure_flow_run_runtime",
            lambda _db, **_kwargs: ensured.append(str(_kwargs["flow_run_id"])),
        )
        monkeypatch.setattr(
            flow_node_host.sandboxes,
            "active_flow_run_runtime_connection",
            lambda _db, *, flow_run_id: _connection(runtime_session_id, flow_run_id),
        )
        monkeypatch.setattr(flow_node_host, "runtime_node", lambda **_kwargs: {"asset": {}})

        host = flow_node_host.resolve_flow_node_session_host(
            db,
            flow_run_id=flow_run_id,
            attempt_id=attempt_id,
            require_start_permission=False,
            ensure_startable_runtime=True,
        )

        assert ensured == [flow_run_id]
        assert host.session.permits(READ_SESSIONS)
        assert not host.session.permits(CREATE_SESSIONS)
        assert host.session.permits(ACCESS_TERMINAL)


def test_cancelled_node_session_restarts_only_for_read_only_history(
    db_session_factory: sessionmaker[Session], monkeypatch: pytest.MonkeyPatch
) -> None:
    with db_session_factory() as db:
        flow_run_id, runtime_session_id, attempt_id = _node_session_context(db)
        attempt = db.get(NodeAttempt, attempt_id)
        assert attempt is not None
        attempt.state = "CANCELLED"
        attempt.conversation_id = "aa632164-3d6a-44c8-af92-4d25f5890958"
        ensured: list[str] = []
        monkeypatch.setattr(
            flow_node_host.sandboxes,
            "node_attempt_workspace_context",
            lambda _db, **_kwargs: SimpleNamespace(
                attempt_owned=True,
                host_working_directory="/data/workspaces/node-1",
                runtime_working_directory="/runtime/workspace/project",
            ),
        )
        monkeypatch.setattr(
            flow_node_host.sandboxes,
            "ensure_node_attempt_runtime",
            lambda _db, **kwargs: ensured.append(str(kwargs["node_attempt_id"])),
        )
        monkeypatch.setattr(
            flow_node_host.sandboxes,
            "active_node_runtime_connection",
            lambda _db, **_kwargs: _connection(runtime_session_id, flow_run_id),
        )
        manifest_options: list[bool] = []
        monkeypatch.setattr(
            flow_node_host,
            "validate_runtime_manifest",
            lambda _manifest, **kwargs: manifest_options.append(
                bool(kwargs["allow_legacy_frozen_runtime"])
            ),
        )
        snapshot_options: list[bool] = []
        monkeypatch.setattr(
            flow_node_host,
            "runtime_node",
            lambda **kwargs: (
                snapshot_options.append(bool(kwargs["allow_legacy_read_only_snapshot"])),
                {"asset": {}},
            )[1],
        )

        host = flow_node_host.resolve_flow_node_session_host(
            db,
            flow_run_id=flow_run_id,
            attempt_id=attempt_id,
            require_start_permission=False,
        )

        assert ensured == [attempt_id]
        assert manifest_options == [True]
        assert snapshot_options == [True]
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
        attempt.conversation_id = binding.openhands_conversation_id

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


def test_cancelled_node_attempt_fences_every_session_write(
    db_session_factory: sessionmaker[Session], monkeypatch: pytest.MonkeyPatch
) -> None:
    """Cancellation is read-only immediately, before native interrupt settles."""

    with db_session_factory() as db:
        flow_run_id, _runtime_session_id, attempt_id = _node_session_context(db)
        attempt = db.get(NodeAttempt, attempt_id)
        assert attempt is not None
        attempt.state = "CANCELLED"
        attempt.runtime_phase = "CANCELLING"

        with pytest.raises(DomainError) as paused:
            flow_node_conversations.resume_node_conversation(
                db,
                flow_run_id=flow_run_id,
                attempt_id=attempt_id,
                binding_id=str(uuid4()),
            )
        assert paused.value.code == "NODE_ATTEMPT_CANCELLED"

        with pytest.raises(DomainError) as sent:
            flow_node_conversations.send_node_message(
                db,
                flow_run_id=flow_run_id,
                attempt_id=attempt_id,
                binding_id=str(uuid4()),
                content="不应发送",
            )
        assert sent.value.code == "NODE_ATTEMPT_CANCELLED"

        monkeypatch.setattr(
            flow_node_conversations.agent_sessions,
            "resolve_flow_node_session_host",
            lambda *_args, **_kwargs: SimpleNamespace(
                session=SimpleNamespace(working_directory="/runtime/workspace/project")
            ),
        )
        monkeypatch.setattr(
            flow_node_conversations.sandboxes,
            "active_node_runtime_connection",
            lambda *_args, **_kwargs: SimpleNamespace(
                resource_name="node-runtime", managed_runtime_id="runtime-id"
            ),
        )
        assert flow_node_conversations.node_draft_terminal_resource_details(
            db, flow_run_id=flow_run_id, attempt_id=attempt_id
        ) == ("node-runtime", "runtime-id", "/runtime/workspace/project")

        status = flow_node_conversations.node_runtime_status(
            db, flow_run_id=flow_run_id, attempt_id=attempt_id
        )
        assert status["state"] == "ACTIVE"
        assert status["write_available"] is False
        assert status["terminal_available"] is True
        assert "正在停止" in str(status["message"])


def test_completed_flow_run_makes_node_conversation_read_only_but_keeps_terminal_available(
    db_session_factory: sessionmaker[Session], monkeypatch: pytest.MonkeyPatch
) -> None:
    with db_session_factory() as db:
        flow_run_id, _runtime_session_id, attempt_id = _node_session_context(db)
        run = db.get(FlowRun, flow_run_id)
        assert run is not None
        run.state = "COMPLETED"

        with pytest.raises(DomainError) as blocked:
            flow_node_host.assert_flow_node_session_writable(
                db, flow_run_id=flow_run_id, attempt_id=attempt_id
            )
        assert blocked.value.code == "FLOW_RUN_TERMINAL"

        monkeypatch.setattr(
            flow_node_conversations.agent_sessions,
            "resolve_flow_node_session_host",
            lambda *_args, **_kwargs: None,
        )
        status = flow_node_conversations.node_runtime_status(
            db, flow_run_id=flow_run_id, attempt_id=attempt_id
        )
        assert status["write_available"] is False
        assert status["terminal_available"] is True
        assert "流程已结束" in str(status["message"])


def test_terminal_node_attempt_keeps_workspace_entry_operations(
    settings, db_session_factory: sessionmaker[Session], monkeypatch: pytest.MonkeyPatch
) -> None:
    from flowweave.modules.sandboxes.application.runtime_allocation import (
        allocate_flow_run_runtime,
        allocate_node_attempt_runtime,
        node_attempt_workspace_project_path,
    )

    with settings_context(settings), db_session_factory() as db:
        flow_run_id, _runtime_session_id, attempt_id = _node_session_context(db)
        run = db.get(FlowRun, flow_run_id)
        assert run is not None
        allocate_flow_run_runtime(db, flow_run_id)
        allocate_node_attempt_runtime(db, flow_run_id=flow_run_id, node_attempt_id=attempt_id)
        project_root = node_attempt_workspace_project_path(
            db, flow_run_id=flow_run_id, node_attempt_id=attempt_id
        )
        run.state = "COMPLETED"
        monkeypatch.setattr(
            flow_node_workspace,
            "resolve_flow_node_session_host",
            lambda _db, **_kwargs: SimpleNamespace(
                session=SimpleNamespace(working_directory="/runtime/workspace/project")
            ),
        )

        flow_node_workspace.create_entry(
            db,
            flow_run_id=flow_run_id,
            attempt_id=attempt_id,
            binding_id=None,
            work_directory_id=None,
            parent_path="/runtime/workspace/project",
            name="post-run-notes.txt",
            kind="FILE",
        )
        assert (project_root / "post-run-notes.txt").is_file()
        assert flow_node_workspace.delete_entries(
            db,
            flow_run_id=flow_run_id,
            attempt_id=attempt_id,
            binding_id=None,
            work_directory_id=None,
            paths=("/runtime/workspace/project/post-run-notes.txt",),
        ) == ["/runtime/workspace/project/post-run-notes.txt"]
        assert not (project_root / "post-run-notes.txt").exists()


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


def test_resume_node_conversation_recovers_an_end_blocked_attempt_when_openhands_is_paused(
    db_session_factory: sessionmaker[Session], monkeypatch: pytest.MonkeyPatch
) -> None:
    """A prior pause observation must not permanently own the native Conversation."""

    with db_session_factory() as db:
        flow_run_id, runtime_session_id, attempt_id = _node_session_context(db)
        attempt = db.get(NodeAttempt, attempt_id)
        assert attempt is not None
        attempt.state = "END_BLOCKED"
        attempt.runtime_phase = "COMPLETED"
        attempt.error_code = "END_GATE_DELIVERY_FAILED"
        attempt.error_detail = (
            "Tool call interrupted before completion. The conversation was paused."
        )
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
            model_provider_id="market-provider",
            model_name="market-model",
            openhands_conversation_id="failed-paused-conversation",
            lifecycle="ACTIVE",
            create_idempotency_key=f"failed-paused:{attempt_id}",
        )
        db.add(binding)
        db.flush()
        attempt.conversation_id = binding.openhands_conversation_id

        switched: list[object] = []

        class NativePausedRuntime:
            def switch_model(self, _handle: object, provider: object) -> None:
                switched.append(provider)

            def input_readiness(self, _handle: object) -> RuntimeInputReadiness:
                return RuntimeInputReadiness(ready=True, execution_status="paused")

            def run(self, _handle: object) -> RuntimeResult:
                return RuntimeResult(status="RUNNING", cursor="resumed-leaf")

        monkeypatch.setattr(
            flow_node_conversations, "_binding_for_attempt", lambda *_args, **_kwargs: binding
        )
        monkeypatch.setattr(
            flow_node_conversations, "_node_handle", lambda *_args, **_kwargs: object()
        )
        monkeypatch.setattr(flow_node_conversations, "get_runtime", lambda: NativePausedRuntime())
        monkeypatch.setattr(flow_node_conversations, "config_from_binding", lambda *_args: object())
        monkeypatch.setattr(flow_node_conversations, "provider_for_config", lambda *_args: "market")

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
        assert result == {"accepted": True, "cursor": "resumed-leaf"}
        assert (resumed.state, resumed.runtime_phase, resumed.error_code, resumed.error_detail) == (
            "EXECUTING",
            "RUNNING",
            None,
            None,
        )
        assert run.state == "ACTIVE"
        assert switched == ["market"]
        event = db.scalar(
            select(RunEvent)
            .where(RunEvent.attempt_id == attempt_id, RunEvent.event_type == "ATTEMPT_RESUMED")
            .order_by(RunEvent.cursor.desc())
        )
        assert event is not None
        assert event.payload_json["reason"] == "NATIVE_PAUSE_AFTER_BLOCKED_PROJECTION"
        assert (
            db.scalar(
                select(BackgroundTask).where(
                    BackgroundTask.idempotency_key
                    == f"wait-runtime-wakeup:{attempt_id}:v{resumed.state_version}:1"
                )
            )
            is not None
        )


def test_node_message_keeps_an_end_blocked_attempt_observing_native_events(
    db_session_factory: sessionmaker[Session], monkeypatch: pytest.MonkeyPatch
) -> None:
    with db_session_factory() as db:
        flow_run_id, runtime_session_id, attempt_id = _node_session_context(db)
        attempt = db.get(NodeAttempt, attempt_id)
        assert attempt is not None
        attempt.state = "END_BLOCKED"
        attempt.runtime_phase = "COMPLETED"
        attempt.error_code = "END_GATE_DELIVERY_FAILED"
        attempt.error_detail = "A restart occurred while this tool was in progress."
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
            openhands_conversation_id="restart-recovery-conversation",
            lifecycle="ACTIVE",
            create_idempotency_key=f"restart-recovery:{attempt_id}",
        )
        db.add(binding)
        db.flush()
        attempt.conversation_id = binding.openhands_conversation_id

        class RestartRecoveredRuntime:
            def input_readiness(self, _handle: object) -> RuntimeInputReadiness:
                return RuntimeInputReadiness(ready=False, execution_status="running")

            def switch_model(self, _handle: object, _provider: object) -> None:
                raise AssertionError("a running native turn must not be rebound")

            def send_message(
                self, _handle: object, _content: str, _images: tuple[str, ...]
            ) -> RuntimeResult:
                return RuntimeResult(status="RUNNING", cursor="new-user-turn")

        monkeypatch.setattr(
            flow_node_conversations, "_binding_for_attempt", lambda *_args, **_kwargs: binding
        )
        monkeypatch.setattr(
            flow_node_conversations, "_node_handle", lambda *_args, **_kwargs: object()
        )
        monkeypatch.setattr(
            flow_node_conversations, "get_runtime", lambda: RestartRecoveredRuntime()
        )
        monkeypatch.setattr(flow_node_conversations, "config_from_binding", lambda *_args: object())
        monkeypatch.setattr(flow_node_conversations, "provider_for_config", lambda *_args: None)

        result = flow_node_conversations.send_node_message(
            db,
            flow_run_id=flow_run_id,
            attempt_id=attempt_id,
            binding_id=binding.id,
            content="请从中断处继续，并在完成后提交正式输出。",
        )

        db.expire_all()
        blocked = db.get(NodeAttempt, attempt_id)
        run = db.get(FlowRun, flow_run_id)
        assert blocked is not None
        assert run is not None
        assert result == {
            "accepted": True,
            "cursor": "new-user-turn",
            "compacted": False,
            "queued_during_turn": True,
        }
        assert (blocked.state, blocked.runtime_phase, blocked.error_code, blocked.error_detail) == (
            "END_BLOCKED",
            "COMPLETED",
            "END_GATE_DELIVERY_FAILED",
            "A restart occurred while this tool was in progress.",
        )
        assert run.state == "WAITING_HUMAN"
        assert (
            db.scalar(
                select(BackgroundTask).where(
                    BackgroundTask.idempotency_key
                    == f"wait-runtime-wakeup:{attempt_id}:v{blocked.state_version}:1"
                )
            )
            is not None
        )


def test_legacy_flow_run_question_queues_during_native_async_turn(
    db_session_factory: sessionmaker[Session], monkeypatch: pytest.MonkeyPatch
) -> None:
    """The legacy FlowRun question endpoint must not reject a running turn."""

    with db_session_factory() as db:
        flow_run_id, runtime_session_id, attempt_id = _node_session_context(db)
        attempt = db.get(NodeAttempt, attempt_id)
        assert attempt is not None
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
            openhands_conversation_id="async-running-conversation",
            lifecycle="ACTIVE",
            create_idempotency_key=f"async-running:{attempt_id}",
        )
        db.add(binding)
        db.flush()

        sent: list[tuple[str, tuple[str, ...]]] = []

        class RunningRuntime:
            def input_readiness(self, _handle: object) -> RuntimeInputReadiness:
                return RuntimeInputReadiness(ready=False, execution_status="running")

            def switch_model(self, _handle: object, _provider: object) -> None:
                raise AssertionError("a running turn must not rebind its model")

            def send_message(
                self, _handle: object, content: str, images: tuple[str, ...]
            ) -> RuntimeResult:
                sent.append((content, images))
                return RuntimeResult(status="RUNNING", cursor="queued-user-event")

        monkeypatch.setattr(flow_node_conversations, "_binding", lambda *_args, **_kwargs: binding)
        monkeypatch.setattr(flow_node_conversations, "_handle", lambda *_args, **_kwargs: object())
        monkeypatch.setattr(flow_node_conversations, "get_runtime", lambda: RunningRuntime())
        monkeypatch.setattr(
            flow_node_conversations, "_observe_task_watchdogs_after_send", lambda *_args: None
        )

        payload = SimpleNamespace(
            client_question_id="queued-turn",
            content=[SimpleNamespace(type="text", text="继续处理当前任务")],
        )
        result = flow_node_conversations.send_question(
            db, binding.id, payload, "queued-turn-key", "user-1"
        )

        assert result["accepted"] is True
        assert result["cursor"] == "queued-user-event"
        assert result["queued_during_turn"] is True
        assert sent == [("继续处理当前任务", ())]


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

        first = conversation_service.list_node_session_page(
            db, flow_run_id=flow_run_id, attempt_id=attempt_id, limit=2
        )
        second = conversation_service.list_node_session_page(
            db,
            flow_run_id=flow_run_id,
            attempt_id=attempt_id,
            cursor=first["next_cursor"],
            limit=2,
        )

        assert [item["id"] for item in first["items"]] == [
            oldest_but_recently_active.id,
            newest.id,
        ]
        assert [item["id"] for item in second["items"]] == [oldest.id]
        assert second["next_cursor"] is None


def test_node_workspace_projection_shares_project_across_node_attempts(
    settings, db_session_factory: sessionmaker[Session], monkeypatch: pytest.MonkeyPatch
) -> None:
    """Every node in one continuous run sees the same project files."""

    from flowweave.modules.sandboxes.application.runtime_allocation import (
        allocate_flow_run_runtime,
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
        allocate_flow_run_runtime(db, flow_run_id)
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
        assert first_root == second_root
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
        assert second_runtime_path in paths
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
        second_content, _content_type, second_filename = flow_node_workspace.read_file(
            db,
            flow_run_id=flow_run_id,
            attempt_id=second_attempt.id,
            binding_id=None,
            work_directory_id=None,
            path=first_runtime_path,
        )
        assert second_content == b"first"
        assert second_filename == "first.txt"
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
        allocate_flow_run_runtime,
        allocate_node_attempt_runtime,
        node_attempt_workspace_project_path,
    )

    with settings_context(settings), db_session_factory() as db:
        flow_run_id, _runtime_session_id, attempt_id = _node_session_context(db)
        attempt = db.get(NodeAttempt, attempt_id)
        assert attempt is not None
        allocate_flow_run_runtime(db, flow_run_id)
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
