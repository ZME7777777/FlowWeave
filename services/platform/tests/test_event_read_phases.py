from __future__ import annotations

import asyncio
import threading
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from types import SimpleNamespace

import pytest
from sqlalchemy import create_engine, event, text
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import QueuePool

from flowweave.modules.agent_sessions.application import conversations as workspace
from flowweave.modules.agent_sessions.application import flow_node_conversations as node
from flowweave.modules.agent_sessions.application.conversation_cache import ConversationCacheKey
from flowweave.modules.agent_sessions.presentation import router as node_router
from flowweave.modules.agent_workspaces.presentation import router as workspace_router
from flowweave.modules.conversations.presentation import router as run_router
from flowweave.runtime.base import RuntimeEvent, RuntimeEventBatch, RuntimeHandle
from flowweave.runtime.read_budget import hydration_time_left
from flowweave.shared.application.transactions import (
    register_commit_action,
    register_rollback_action,
)
from flowweave.shared.errors import DomainError
from flowweave.shared.http import run_formal_events
from flowweave.shared.observability import Metrics


@pytest.fixture(autouse=True)
def database():
    """Use one local SQL connection for lifetime checks; no server or Docker."""
    yield


def _sql_lifetime_engine():
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=QueuePool,
        pool_size=1,
        max_overflow=0,
        pool_timeout=0.05,
    )

    @event.listens_for(engine, "connect")
    def accept_postgres_session_context(connection, _record):
        # SQLite verifies actual checkout lifetime, not PostgreSQL tenant RLS.
        connection.create_function("set_config", 3, lambda _key, value, _local: value)

    return engine


@pytest.mark.asyncio
@pytest.mark.parametrize("history", [False, True])
@pytest.mark.parametrize("ending", ["ok", "timeout", "cancel", "runtime_error", "project_error"])
async def test_phased_events_release_sql_during_runtime_but_keep_real_slot(history, ending):
    engine = _sql_lifetime_engine()
    sessions = sessionmaker(engine)
    started = threading.Event()
    release = threading.Event()
    phases: list[str] = []
    callbacks: list[str] = []
    retained_sessions: list[object] = []
    deadlines: list[float] = []
    metrics = Metrics()

    def prepare(session):
        session.execute(text("SELECT 1"))
        assert engine.pool.checkedout() == 1
        phases.append("prepare")
        retained_sessions.append(session)
        register_commit_action(session, lambda: callbacks.append("prepare_commit"))
        return "immutable-locator"

    def read(prepared):
        assert prepared == "immutable-locator"
        assert engine.pool.checkedout() == 0
        phases.append("runtime")
        deadlines.append(hydration_time_left())
        started.set()
        assert release.wait(2)
        if ending == "runtime_error":
            raise DomainError("EXECUTOR_UNAVAILABLE", "read failed", 503)
        return "formal-snapshot"

    def project(session, prepared, snapshot):
        assert prepared == "immutable-locator"
        assert snapshot == "formal-snapshot"
        session.execute(text("SELECT 1"))
        assert engine.pool.checkedout() == 1
        retained_sessions.append(session)
        phases.append("project")
        deadlines.append(hydration_time_left())
        register_commit_action(session, lambda: callbacks.append("project_commit"))
        register_rollback_action(session, lambda: callbacks.append("project_rollback"))
        if ending == "project_error":
            raise DomainError("AGENT_RUNTIME_REPLACEMENT_FENCED", "generation changed", 409)
        return {"ok": True}

    with ThreadPoolExecutor(max_workers=1) as executor:
        slots = asyncio.Semaphore(1)
        container = SimpleNamespace(
            blocking_executor=executor,
            history_read_executor=executor,
            blocking_io_slots=slots,
            history_read_slots=slots,
            database=SimpleNamespace(blocking_sessions=sessions, history_sessions=sessions),
            metrics=metrics,
            settings=SimpleNamespace(
                runtime_event_read_timeout_seconds=0.05 if ending == "timeout" else 1,
                blocking_pool_timeout_seconds=0.01,
                blocking_pool_size=1,
                history_read_pool_size=1,
            ),
        )
        task = asyncio.create_task(
            run_formal_events(container, prepare, read, project, history=history)
        )
        try:
            assert await asyncio.to_thread(started.wait, 1)
            assert slots.locked()
            # Real checkout succeeds while the only event worker remains blocked.
            with sessions() as peer:
                assert peer.scalar(text("SELECT 1")) == 1
            assert engine.pool.checkedout() == 0
            if ending == "timeout":
                with pytest.raises(DomainError) as caught:
                    await task
                assert caught.value.code == "RUNTIME_BUSINESS_READ_TIMEOUT"
                assert slots.locked()
            elif ending == "cancel":
                task.cancel()
                task.cancel()
                with pytest.raises(asyncio.CancelledError):
                    await task
                assert slots.locked()
            # New readers cannot reclaim a cancelled/timed-out worker's slot.
            with pytest.raises(DomainError) as saturated:
                await run_formal_events(container, prepare, read, project, history=history)
            assert saturated.value.code == (
                "RUNTIME_HISTORY_READ_SATURATED" if history else "RUNTIME_READ_SATURATED"
            )
        finally:
            release.set()
            if ending in {"runtime_error", "project_error"}:
                with pytest.raises(DomainError):
                    await task
            elif ending == "ok":
                assert await task == {"ok": True}
            for _ in range(300):
                if not slots.locked():
                    break
                await asyncio.sleep(0.001)
            assert not slots.locked()
            assert engine.pool.checkedout() == 0
            engine.dispose()
    assert phases[:2] == ["prepare", "runtime"]
    assert callbacks[0] == "prepare_commit"
    if ending in {"ok", "cancel", "project_error"}:
        # Client cancellation is not a thread cancellation; valid projections
        # may finish, retaining the slot and transaction ownership until then.
        assert phases == ["prepare", "runtime", "project"]
        assert len(retained_sessions) == 2
        assert retained_sessions[0] is not retained_sessions[1]
        assert 0 < deadlines[1] <= deadlines[0]
        assert callbacks[-1] == (
            "project_rollback" if ending == "project_error" else "project_commit"
        )
    else:
        assert phases == ["prepare", "runtime"]
        assert callbacks == ["prepare_commit"]
    rendered = metrics.render()
    assert 'operation="agent_session.events.prepare_db"' in rendered
    assert 'operation="agent_session.events.runtime_read"' in rendered
    if "project" in phases:
        assert 'operation="agent_session.events.project_db"' in rendered
    assert "immutable-locator" not in rendered


def _locator(host):
    handle = RuntimeHandle(
        job_id="job",
        conversation_id="conversation",
        runtime_resource_id="generation-1",
        runtime_resource_name="runtime-1",
        workspace_root="/workspace/project",
        cursor="incremental",
        history_cursor="older",
    )
    key = ConversationCacheKey(
        user_id="user",
        host_kind="AGENT_WORKSPACE" if host == "workspace" else "FLOW_NODE",
        host_id="workspace" if host == "workspace" else "attempt",
        binding_id="binding",
        runtime_session_id="session",
        runtime_generation="generation-1",
        conversation_id="conversation",
    )
    if host == "workspace":
        return workspace.PreparedConversationEvents(
            workspace.PreparedConversationHydration("workspace", "binding", key, handle),
            "incremental",
            "older",
            "foreground",
        )
    return node.PreparedFlowRunConversationEvents(
        "run", "attempt", "binding", key, handle, "foreground"
    )


@pytest.mark.parametrize("host", ["workspace", "node"])
@pytest.mark.parametrize(
    "drift", ["session", "conversation", "generation", "resource", "root", "host", "authorization"]
)
def test_event_projection_rejects_drift_before_exposing_product_data(monkeypatch, host, drift):
    prepared = _locator(host)
    locator = prepared.locator if host == "workspace" else prepared
    current = locator.handle
    binding = SimpleNamespace(
        runtime_session_id="session",
        openhands_conversation_id="conversation",
        host_kind=locator.key.host_kind,
    )
    if drift in {"session", "conversation", "host"}:
        field = {
            "session": "runtime_session_id",
            "conversation": "openhands_conversation_id",
            "host": "host_kind",
        }[drift]
        setattr(binding, field, "changed")
    else:
        field = {
            "generation": "runtime_resource_id",
            "resource": "runtime_resource_name",
            "root": "workspace_root",
        }.get(drift)
        if field:
            current = replace(current, **{field: "changed"})

    def authorize(*_args, **_kwargs):
        if drift == "authorization":
            raise DomainError("NOT_FOUND", "binding no longer authorized", 404)
        return binding

    def must_not_project(*_args):
        raise AssertionError("stale or unauthorized data must not reach projection")

    if host == "workspace":
        monkeypatch.setattr(workspace, "_workspace", lambda *_args: SimpleNamespace(id="workspace"))
        monkeypatch.setattr(workspace, "_binding", authorize)
        monkeypatch.setattr(workspace, "_handle", lambda *_args: current)
        monkeypatch.setattr(workspace, "_project_conversation_events", must_not_project)
        project = workspace.project_prepared_conversation_events
    else:
        monkeypatch.setattr(node, "_binding_for_attempt", authorize)
        monkeypatch.setattr(node, "_flow_run_handle", lambda *_args: current)
        monkeypatch.setattr(node, "_event_batch_dict", must_not_project)
        project = node.project_prepared_flow_run_conversation_events
    with pytest.raises(DomainError) as caught:
        project(object(), prepared, RuntimeEventBatch())
    assert caught.value.code == (
        "NOT_FOUND" if drift == "authorization" else "AGENT_RUNTIME_REPLACEMENT_FENCED"
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("host", ["workspace", "node", "run"])
@pytest.mark.parametrize(
    ("cursor", "history"), [(None, None), ("incremental", "older"), (None, "older")]
)
async def test_event_routes_delegate_three_phases_with_exact_scope_and_cursor(
    monkeypatch, host, cursor, history
):
    module = {"workspace": workspace_router, "node": node_router, "run": run_router}[host]
    application = workspace if host == "workspace" else node
    calls = []
    prepared = object()
    batch = RuntimeEventBatch(cursor="native-head", history_cursor="native-older")
    expected = {
        "next_cursor": "native-head",
        "history_cursor": "native-older",
        "attachments": ["original"],
    }

    def prepare(_session, *args, **kwargs):
        calls.append((args, kwargs))
        return prepared

    def read(value):
        assert value is prepared
        return batch

    def project(_session, value, snapshot):
        assert value is prepared and snapshot is batch
        return expected

    async def phases(_container, prepare_cb, read_cb, project_cb, *, history):
        assert history == bool(history_cursor and not cursor)
        return project_cb(object(), prepare_cb(object()), read_cb(prepared))

    history_cursor = history
    suffix = "conversation_events" if host == "workspace" else "flow_run_conversation_events"
    monkeypatch.setattr(application, "prepare_" + suffix, prepare)
    monkeypatch.setattr(application, "read_prepared_" + suffix, read)
    monkeypatch.setattr(application, "project_prepared_" + suffix, project)
    monkeypatch.setattr(module, "run_formal_events", phases)
    if host == "workspace":
        result = await module.agent_events(
            "workspace",
            "binding",
            object(),
            cursor=cursor,
            history_cursor=history,
            diagnostic_trigger="focus",
        )
        assert calls == [
            (("workspace", "binding", cursor, history), {"diagnostic_trigger": "focus"})
        ]
    elif host == "node":
        result = await module.node_session_events(
            "run",
            "attempt",
            "binding",
            object(),
            cursor=cursor,
            history_cursor=history,
            diagnostic_trigger="focus",
        )
        assert calls == [
            (
                (),
                dict(
                    flow_run_id="run",
                    attempt_id="attempt",
                    binding_id="binding",
                    cursor=cursor,
                    history_cursor=history,
                    diagnostic_trigger="focus",
                ),
            )
        ]
    else:
        result = await module.live_conversation_events(
            "run", "binding", object(), cursor=cursor, history_cursor=history
        )
        assert calls == [(("run", "binding"), dict(cursor=cursor, history_cursor=history))]
    assert result is expected


@pytest.mark.parametrize("host", ["workspace", "node"])
def test_native_event_read_and_product_projection_preserve_formal_batch(monkeypatch, host):
    from flowweave.runtime.base import RuntimeResult

    prepared = _locator(host)
    locator = prepared.locator if host == "workspace" else prepared
    calls = []
    batch = RuntimeEventBatch(
        events=(
            RuntimeEvent(
                "message-id",
                "MESSAGE",
                {"source": "user", "content": "native-body", "parent_id": "parent-id"},
            ),
        ),
        cursor="native-head",
        history_cursor="native-older",
        result=RuntimeResult(status="COMPLETED"),
    )

    class Runtime:
        def reload_conversation(self, handle):
            assert handle == locator.handle
            calls.append("reload")

        def read_active_events(self, handle):
            assert handle.cursor == "incremental" and handle.history_cursor == "older"
            assert handle.conversation_id == "conversation"
            assert handle.runtime_resource_id == "generation-1"
            calls.append("read")
            return batch

    application = workspace if host == "workspace" else node
    monkeypatch.setattr(application, "get_runtime", lambda: Runtime())
    read = (
        workspace.read_prepared_conversation_events
        if host == "workspace"
        else node.read_prepared_flow_run_conversation_events
    )
    assert read(prepared) is batch
    assert calls == (["reload", "read"] if host == "workspace" else ["read"])

    binding = SimpleNamespace(
        id="binding",
        runtime_session_id="session",
        openhands_conversation_id="conversation",
        host_kind=locator.key.host_kind,
        working_directory="/workspace/project",
        node_attempt_id="attempt",
        conversation_scope_id="attempt",
        flow_run_id="run",
    )
    attachment = SimpleNamespace(
        content="display-body",
        filename="original.txt",
        mime_type="text/plain",
        byte_size=7,
        path="/workspace/project/uploads/original.txt",
        event_id="message-id",
    )

    class Session:
        def scalars(self, _query):
            return SimpleNamespace(all=lambda: [attachment])

        def get(self, _model, _key):
            return SimpleNamespace(flow_run_id="run")

    def must_not_read():
        raise AssertionError("projection must not perform Runtime I/O")

    monkeypatch.setattr(application, "get_runtime", must_not_read)
    if host == "workspace":
        monkeypatch.setattr(workspace, "_workspace", lambda *_args: SimpleNamespace(id="workspace"))
        monkeypatch.setattr(workspace, "_binding", lambda *_args: binding)
        monkeypatch.setattr(workspace, "_handle", lambda *_args: locator.handle)
        projected = workspace.project_prepared_conversation_events(Session(), prepared, batch)
    else:
        monkeypatch.setattr(node, "_binding_for_attempt", lambda *_args, **_kwargs: binding)
        monkeypatch.setattr(node, "_flow_run_handle", lambda *_args: locator.handle)
        monkeypatch.setattr(
            node, "_attempt", lambda *_args: SimpleNamespace(id="attempt", node_run_id="node")
        )
        projected = node.project_prepared_flow_run_conversation_events(Session(), prepared, batch)
    assert projected["next_cursor"] == "native-head"
    assert projected["history_cursor"] == "native-older"
    assert projected["events"][0]["id"] == "message-id"
    assert projected["events"][0]["payload"]["parent_id"] == "parent-id"
    assert projected["events"][0]["payload"]["content"] == "native-body"
    assert projected["events"][0]["payload"]["display_content"] == "display-body"
    assert projected["events"][0]["payload"]["attachments"] == [
        dict(filename="original.txt", mime_type="text/plain", byte_size=7, path=attachment.path)
    ]
    assert projected["task_control"] == []
    assert "monitoring" in projected


@pytest.mark.asyncio
async def test_prepare_failure_rolls_back_and_never_starts_runtime_or_projection():
    engine = _sql_lifetime_engine()
    sessions = sessionmaker(engine)
    callbacks = []
    with ThreadPoolExecutor(max_workers=1) as executor:
        slots = asyncio.Semaphore(1)
        container = SimpleNamespace(
            blocking_executor=executor,
            blocking_io_slots=slots,
            database=SimpleNamespace(blocking_sessions=sessions),
            settings=SimpleNamespace(
                runtime_event_read_timeout_seconds=1,
                blocking_pool_timeout_seconds=0.1,
                blocking_pool_size=1,
            ),
        )

        def fail_prepare(session):
            register_commit_action(session, lambda: callbacks.append("incorrect_commit"))
            register_rollback_action(session, lambda: callbacks.append("rollback"))
            raise DomainError("NOT_FOUND", "binding not authorized", 404)

        def must_not_run(*_args):
            raise AssertionError("prepare failed before native read")

        try:
            with pytest.raises(DomainError) as caught:
                await run_formal_events(
                    container, fail_prepare, must_not_run, must_not_run, history=False
                )
            assert caught.value.code == "NOT_FOUND"
            assert callbacks == ["rollback"]
            assert not slots.locked()
        finally:
            engine.dispose()


@pytest.mark.parametrize("host", ["workspace", "node", "run"])
def test_preparation_freezes_ids_and_preserves_entry_authorization(monkeypatch, host):
    locator = _locator("workspace" if host == "workspace" else "node")
    saved = locator.locator if host == "workspace" else locator
    binding = SimpleNamespace(
        id="binding",
        owner_user_id="user",
        host_kind=saved.key.host_kind,
        runtime_session_id="session",
        openhands_conversation_id="conversation",
    )
    authorization = []

    def authorize(*args, **kwargs):
        authorization.append((args[1:], kwargs))
        return binding

    if host == "workspace":
        monkeypatch.setattr(workspace, "_workspace", lambda *_args: SimpleNamespace(id="workspace"))
        monkeypatch.setattr(workspace, "_binding", authorize)
        monkeypatch.setattr(
            workspace,
            "_handle",
            lambda *_args: replace(saved.handle, cursor=None, history_cursor=None),
        )
        prepared = workspace.prepare_conversation_events(
            object(), "workspace", "binding", "incremental", "older", diagnostic_trigger="focus"
        )
        assert authorization == [(("workspace", "binding"), {})]
        assert prepared.cursor == "incremental" and prepared.history_cursor == "older"
        assert prepared.diagnostic_trigger == "focus"
        identity = prepared.locator
    else:
        monkeypatch.setattr(node, "_binding_for_attempt", authorize)
        monkeypatch.setattr(node, "_binding_for_run", authorize)
        monkeypatch.setattr(
            node,
            "_flow_run_handle",
            lambda *_args, **kwargs: replace(
                saved.handle,
                cursor=kwargs.get("cursor"),
                history_cursor=kwargs.get("history_cursor"),
            ),
        )
        prepared = node.prepare_flow_run_conversation_events(
            object(),
            "run",
            "binding",
            cursor="incremental",
            history_cursor="older",
            attempt_id="attempt" if host == "node" else None,
            diagnostic_trigger="focus",
        )
        if host == "node":
            assert authorization == [
                ((), dict(flow_run_id="run", attempt_id="attempt", binding_id="binding"))
            ]
        else:
            assert authorization == [(("run", "binding"), {})]
        assert prepared.handle.cursor == "incremental" and prepared.handle.history_cursor == "older"
        identity = prepared
    # No live ORM object is carried into the native phase; identifiers remain
    # unchanged even when the original binding object changes after commit.
    binding.runtime_session_id = "new-session"
    binding.openhands_conversation_id = "new-conversation"
    assert identity.key.runtime_session_id == "session"
    assert identity.key.conversation_id == "conversation"
    assert identity.key.runtime_generation == identity.handle.runtime_resource_id == "generation-1"
    assert identity.key.binding_id == "binding" and identity.key.user_id == "user"
