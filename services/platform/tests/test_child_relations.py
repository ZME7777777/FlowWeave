from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from pathlib import Path
from threading import Barrier
from types import SimpleNamespace
from uuid import uuid4

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import delete, select, text
from sqlalchemy.exc import DBAPIError

from flowweave.modules.agent_sessions.application import (
    conversations,
    flow_node_conversations,
    search,
)
from flowweave.modules.agent_sessions.application.child_relations import (
    guard_main_route,
    require_direct_child,
    require_ordinary_binding,
    verify_native_child,
)
from flowweave.modules.agent_sessions.application.deletion import delete_binding_records
from flowweave.modules.agent_sessions.application.host import AgentSessionHostContext
from flowweave.modules.agent_sessions.application.runtime_config import (
    FrozenSessionConfig,
    reserve_flow_node_binding,
)
from flowweave.modules.agent_sessions.infrastructure.models import (
    AgentChildConversation,
    AgentConversationBinding,
    AgentConversationSearch,
    AgentConversationSearchHit,
)
from flowweave.modules.agent_workspaces.infrastructure.models import AgentWorkspace
from flowweave.modules.users.application.security import (
    FLOWWEAVE_USER_ID,
    tenant_bypass,
    tenant_user,
)
from flowweave.runtime.base import RuntimeEventBatch, RuntimeHandle
from flowweave.shared.errors import DomainError
from flowweave.shared.models import FlowRun


@pytest.fixture(params=["AGENT_WORKSPACE", "FLOW_NODE"])
def graph(request, db_session_factory):
    with tenant_user(FLOWWEAVE_USER_ID), db_session_factory() as db:
        workspace = AgentWorkspace(scope_key=str(uuid4()))
        db.add(workspace)
        db.flush()
        kind = request.param
        host_id = workspace.id if kind == "AGENT_WORKSPACE" else str(uuid4())
        if kind == "FLOW_NODE":
            db.add(
                FlowRun(id=host_id, flow_definition_id=str(uuid4()), run_no=1, name="child host")
            )
            db.flush()
        scope, runtime = str(uuid4()), str(uuid4())

        def binding(**changes):
            values = dict(
                workspace_id=workspace.id if kind == "AGENT_WORKSPACE" else None,
                host_kind=kind,
                host_id=host_id,
                conversation_scope_id=scope,
                runtime_session_id=runtime,
                openhands_conversation_id=str(uuid4()),
                working_directory="/runtime/workspace/project/test",
                lifecycle="ACTIVE",
                create_idempotency_key=str(uuid4()),
                display_title="root",
                flow_run_id=host_id if kind == "FLOW_NODE" else None,
            )
            values.update(changes)
            result = AgentConversationBinding(**values)
            db.add(result)
            db.flush()
            return result

        parent = binding()
        child = binding(display_title="hidden child", pinned=True, unread=True)

        def relation(parent=parent, child=child, **changes):
            values = dict(
                owner_user_id=FLOWWEAVE_USER_ID,
                host_kind=kind,
                host_id=host_id,
                conversation_scope_id=scope,
                runtime_session_id=runtime,
                parent_binding_id=parent.id,
                child_binding_id=child.id,
                parent_conversation_id=parent.openhands_conversation_id,
                child_conversation_id=child.openhands_conversation_id,
                creation_id=str(uuid4()),
                action_id=str(uuid4()),
                tool_call_id=str(uuid4()),
                accepted_generation=1,
                manifest_digest="a" * 64,
            )
            values.update(changes)
            return AgentChildConversation(**values)

        link = relation()
        db.add(link)
        db.commit()
        host = AgentSessionHostContext.create(
            host_kind=kind,
            host_id=host_id,
            conversation_scope_id=scope,
            runtime_session_id=runtime,
            working_directory=parent.working_directory,
        )
        yield SimpleNamespace(
            db=db,
            parent=parent,
            child=child,
            link=link,
            host=host,
            workspace=workspace,
            binding=binding,
            relation=relation,
        )


def test_exact_direct_child_identity_and_no_cross_scope_disclosure(graph):
    g = graph
    assert (
        require_direct_child(
            g.db, g.host, parent_binding_id=g.parent.id, child_binding_id=g.child.id
        )[0].id
        == g.link.id
    )
    for field in ("host_id", "conversation_scope_id", "runtime_session_id", "working_directory"):
        with pytest.raises(DomainError) as caught:
            require_direct_child(
                g.db,
                replace(g.host, **{field: "other"}),
                parent_binding_id=g.parent.id,
                child_binding_id=g.child.id,
            )
        assert caught.value.status == 404
    with tenant_user(str(uuid4())), tenant_bypass():
        with pytest.raises(DomainError) as caught:
            guard_main_route(
                g.db, host_kind=g.host.host_kind, host_id=g.host.host_id, binding_id=g.child.id
            )
        assert caught.value.status == 404 and not caught.value.details
    with pytest.raises(DomainError) as caught:
        guard_main_route(
            g.db,
            host_kind=g.host.host_kind,
            host_id=g.host.host_id,
            binding_id=g.child.id,
            conversation_scope_id=g.host.conversation_scope_id,
        )
    assert caught.value.code == "AGENT_CHILD_REQUIRES_PARENT"
    assert caught.value.details == {"binding_id": g.child.id, "parent_binding_id": g.parent.id}
    verify_native_child(
        g.link,
        conversation_id=g.child.openhands_conversation_id,
        parent_conversation_id=g.parent.openhands_conversation_id,
        working_directory=g.host.working_directory,
        expected_working_directory=g.host.working_directory,
        isolation="shared",
    )
    with pytest.raises(DomainError):
        verify_native_child(
            g.link,
            conversation_id=g.child.openhands_conversation_id,
            parent_conversation_id=str(uuid4()),
            working_directory=g.host.working_directory,
            expected_working_directory=g.host.working_directory,
            isolation="shared",
        )


def test_host_metadata_redirect_and_orm_tenant_filters(graph, monkeypatch):
    g = graph
    monkeypatch.setattr(conversations, "get_runtime", lambda: pytest.fail("No native read"))
    monkeypatch.setattr(
        flow_node_conversations, "get_runtime", lambda: pytest.fail("No native read")
    )
    monkeypatch.setattr(
        flow_node_conversations.agent_sessions,
        "resolve_flow_node_session_host",
        lambda *_args, **_kwargs: g.host,
    )
    with pytest.raises(DomainError) as caught:
        if g.host.host_kind == "AGENT_WORKSPACE":
            conversations.get_conversation(g.db, g.host.host_id, g.child.id)
        else:
            flow_node_conversations.get_node_session_view(
                g.db,
                flow_run_id=g.host.host_id,
                attempt_id=g.host.conversation_scope_id,
                binding_id=g.child.id,
            )
    assert caught.value.code == "AGENT_CHILD_REQUIRES_PARENT"
    assert caught.value.details["parent_binding_id"] == g.parent.id
    inconsistent = g.binding(
        **(
            {"workspace_id": str(uuid4())}
            if g.host.host_kind == "AGENT_WORKSPACE"
            else {"flow_run_id": str(uuid4())}
        )
    )
    with pytest.raises(DomainError) as caught:
        guard_main_route(
            g.db, host_kind=g.host.host_kind, host_id=g.host.host_id, binding_id=inconsistent.id
        )
    assert caught.value.status == 404
    with tenant_user(str(uuid4())):
        assert g.db.scalar(select(AgentChildConversation.id)) is None
        with pytest.raises(RuntimeError, match="Cross-user record creation"):
            with g.db.begin_nested():
                g.db.add(g.relation())
                g.db.flush()


@pytest.mark.parametrize("mutation", ["owner", "runtime", "scope", "directory", "host", "native"])
def test_database_rejects_mismatched_relation_even_with_tenant_bypass(graph, mutation):
    g = graph
    changes = {
        "owner": {"owner_user_id": str(uuid4())},
        "runtime": {"runtime_session_id": str(uuid4())},
        "scope": {"conversation_scope_id": str(uuid4())},
        "directory": {"working_directory": "/runtime/workspace/project/other"},
        "host": {"host_id": str(uuid4())},
        "native": {},
    }[mutation]
    with tenant_bypass():
        other = g.binding(**changes)
        g.db.commit()
        with pytest.raises(DBAPIError):
            with g.db.begin_nested():
                g.db.add(
                    g.relation(
                        child=other,
                        **({"child_conversation_id": "wrong"} if mutation == "native" else {}),
                    )
                )
                g.db.flush()


def test_database_guards_depth_unique_invocation_and_immutable_binding(graph):
    g = graph
    third, fourth = g.binding(), g.binding()
    g.db.commit()
    invalid = (
        g.relation(parent=g.child, child=third),
        g.relation(parent=third, child=g.parent),
        g.relation(parent=third),
        g.relation(child=third, creation_id=g.link.creation_id),
        g.relation(child=fourth, action_id=g.link.action_id, tool_call_id=g.link.tool_call_id),
        g.relation(child=g.parent),
    )
    for link in invalid:
        with pytest.raises(DBAPIError):
            with g.db.begin_nested():
                g.db.add(link)
                g.db.flush()
    with pytest.raises(DBAPIError):
        with g.db.begin_nested():
            g.child.runtime_session_id = str(uuid4())
            g.db.flush()
    with pytest.raises(DBAPIError):
        with g.db.begin_nested():
            g.link.action_id = "changed"
            g.db.flush()
    g.child.display_title = "still independently editable"
    g.child.unread = False
    g.db.flush()
    with pytest.raises(DomainError, match="子会话清理"):
        delete_binding_records(g.db, g.parent.id)
    with pytest.raises(DBAPIError):
        with g.db.begin_nested():
            g.db.execute(
                delete(AgentConversationBinding).where(AgentConversationBinding.id == g.parent.id)
            )


def test_ordinary_host_reads_and_writes_refuse_children_before_native_calls(graph, monkeypatch):
    g = graph
    monkeypatch.setattr(
        conversations, "get_runtime", lambda: pytest.fail("No native operation permitted")
    )
    for operation in (
        lambda: require_ordinary_binding(g.db, g.child),
        lambda: conversations.history_reference_bindings(
            g.db, g.workspace.id, g.parent.id, g.child.id
        ),
        lambda: flow_node_conversations._binding(g.db, g.child.id),
    ):
        with pytest.raises(DomainError):
            operation()
    if g.host.host_kind == "AGENT_WORKSPACE":
        with pytest.raises(DomainError) as caught:
            conversations.delete_conversation(g.db, g.workspace.id, g.parent.id, str(uuid4()))
        assert caught.value.code == "AGENT_CHILDREN_DELETE_REQUIRED"
        with pytest.raises(DomainError):
            conversations.fork_conversation(g.db, g.workspace.id, g.child.id, "event", None, "fork")
        with pytest.raises(DomainError):
            conversations.create_conversation(
                g.db, g.workspace.id, None, None, g.child.create_idempotency_key
            )
    else:
        with pytest.raises(DomainError):
            reserve_flow_node_binding(
                g.db,
                runtime_session_id=g.host.runtime_session_id,
                flow_run_id=g.host.host_id,
                node_run_id=None,
                node_attempt_id=None,
                working_directory=g.host.working_directory,
                create_idempotency_key=g.child.create_idempotency_key,
                conversation_scope_id=g.host.conversation_scope_id,
                config=FrozenSessionConfig(None, "provider", "model", None, ()),
            )


def test_lists_pages_pinned_and_activity_never_publish_a_child_row(graph, monkeypatch):
    g = graph

    def project(_db, items, **_kwargs):
        return [{"id": item.id} for item in items]

    native = SimpleNamespace(conversation_activity_snapshot=lambda _handle: {})
    monkeypatch.setattr(conversations, "get_runtime", lambda: native)
    monkeypatch.setattr(
        conversations,
        "_handle",
        lambda _db, _workspace, item: RuntimeHandle(
            job_id="test", conversation_id=item.openhands_conversation_id
        ),
    )
    monkeypatch.setattr(conversations, "_dict", lambda _db, item, **_kwargs: {"id": item.id})
    monkeypatch.setattr(conversations, "_page_dicts", project)
    monkeypatch.setattr(conversations, "_workspace_write_available", lambda *_args: False)
    monkeypatch.setattr(
        flow_node_conversations.agent_sessions,
        "resolve_flow_node_session_host",
        lambda *_args, **_kwargs: g.host,
    )
    monkeypatch.setattr(flow_node_conversations, "_node_session_page_dicts", project)
    monkeypatch.setattr(
        flow_node_conversations, "_node_session_dict", lambda _db, item: {"id": item.id}
    )
    monkeypatch.setattr(flow_node_conversations, "get_runtime", lambda: native)
    monkeypatch.setattr(
        flow_node_conversations,
        "_handle",
        lambda _db, binding_id, **_kwargs: RuntimeHandle(job_id="test", conversation_id=binding_id),
    )
    if g.host.host_kind == "AGENT_WORKSPACE":
        pages = (
            conversations.list_conversations(g.db, g.workspace.id),
            conversations.list_conversation_page(g.db, g.workspace.id, limit=1)["items"],
            conversations.list_management_conversations(g.db, g.workspace.id)["items"],
        )
        assert conversations.list_pinned_conversations(g.db, g.workspace.id) == []
        assert conversations.conversation_activity(g.db, g.workspace.id)["conversations"] == []
    else:
        kwargs = dict(flow_run_id=g.host.host_id, attempt_id=g.host.conversation_scope_id)
        pages = (
            flow_node_conversations.list_node_session_views(g.db, **kwargs),
            flow_node_conversations.list_node_session_page(g.db, **kwargs, limit=1)["items"],
            flow_node_conversations.list_node_conversations(g.db, **kwargs),
            flow_node_conversations.list_flow_run_conversations(g.db, g.host.host_id),
        )
        assert flow_node_conversations.node_session_activity(g.db, **kwargs)["conversations"] == []
    assert all([item["id"] for item in page] == [g.parent.id] for page in pages)


def test_search_scan_and_persisted_hits_exclude_child_before_runtime_read(graph, monkeypatch):
    g = graph
    if g.host.host_kind != "AGENT_WORKSPACE":
        return  # Flow Node has no ordinary search endpoint.
    calls = []
    monkeypatch.setattr(
        conversations,
        "_handle",
        lambda _db, _workspace, item: RuntimeHandle(job_id="test", conversation_id=item.id),
    )
    monkeypatch.setattr(
        search,
        "get_runtime",
        lambda: SimpleNamespace(
            search_message_events=lambda handle, _query: calls.append(handle.conversation_id)
            or RuntimeEventBatch(events=()),
            read_search_event=lambda *_args: pytest.fail("Hidden child hit must not read Runtime"),
        ),
    )
    job = AgentConversationSearch(workspace_id=g.workspace.id, query="child")
    g.db.add(job)
    g.db.commit()
    search.process(g.db, job.id)
    assert calls == [g.parent.id] and job.state == "SUCCEEDED"
    for event_id in ("event-1", "event-2"):
        g.db.add(
            AgentConversationSearchHit(search_id=job.id, binding_id=g.child.id, event_id=event_id)
        )
    g.db.flush()
    assert search._hits(g.db, job, cursor=None, limit=1) == ([], None)


def test_postgresql_rls_hides_child_relations_and_rejects_foreign_writes(graph):
    g = graph
    engine = g.db.get_bind()
    role = "fr710_" + uuid4().hex
    with engine.begin() as conn:
        conn.execute(text(f'CREATE ROLE "{role}" NOLOGIN NOSUPERUSER NOBYPASSRLS'))
        conn.execute(text(f'GRANT USAGE ON SCHEMA public TO "{role}"'))
        conn.execute(text(f'GRANT SELECT, INSERT ON agent_child_conversations TO "{role}"'))
        conn.execute(text(f'GRANT SELECT, UPDATE ON agent_conversation_bindings TO "{role}"'))
        conn.execute(text(f'GRANT SELECT ON agent_sidebar_conversations TO "{role}"'))
    try:
        with engine.begin() as conn:
            conn.execute(text(f'SET LOCAL ROLE "{role}"'))
            conn.execute(
                text("SELECT set_config('flowweave.user_id', :owner, true)"),
                {"owner": FLOWWEAVE_USER_ID},
            )
            assert conn.execute(
                text("SELECT id FROM agent_child_conversations")
            ).scalars().all() == [g.link.id]
            conn.execute(
                text("SELECT set_config('flowweave.user_id', :owner, true)"),
                {"owner": str(uuid4())},
            )
            assert conn.execute(text("SELECT id FROM agent_child_conversations")).all() == []
        fields = [column.name for column in AgentChildConversation.__table__.columns]
        payload = {name: getattr(g.link, name) for name in fields}
        payload["id"] = str(uuid4())
        with pytest.raises(DBAPIError):
            with engine.begin() as conn:
                conn.execute(text(f'SET LOCAL ROLE "{role}"'))
                conn.execute(
                    text("SELECT set_config('flowweave.user_id', :owner, true)"),
                    {"owner": str(uuid4())},
                )
                conn.execute(
                    text(
                        "INSERT INTO agent_child_conversations ("
                        + ",".join(fields)
                        + ") VALUES ("
                        + ",".join(":" + name for name in fields)
                        + ")"
                    ),
                    payload,
                )
        with engine.begin() as conn:
            policy = conn.execute(
                text(
                    "SELECT qual, with_check FROM pg_policies "
                    "WHERE tablename = 'agent_child_conversations'"
                )
            ).one()
            assert all("owner_user_id" in expression for expression in policy)
            assert conn.execute(
                text(
                    "SELECT relrowsecurity AND relforcerowsecurity FROM pg_class "
                    "WHERE oid = 'agent_child_conversations'::regclass"
                )
            ).scalar_one()
    finally:
        with engine.begin() as conn:
            conn.execute(text(f'DROP OWNED BY "{role}"'))
            conn.execute(text(f'DROP ROLE "{role}"'))


def test_concurrent_relations_cannot_create_a_grandchild(graph, db_session_factory):
    g = graph
    first, middle, last = g.binding(), g.binding(), g.binding()
    g.db.commit()
    candidates = (g.relation(parent=first, child=middle), g.relation(parent=middle, child=last))
    payloads = [
        {key: value for key, value in vars(link).items() if not key.startswith("_")}
        for link in candidates
    ]
    start = Barrier(2)

    def insert(payload):
        with tenant_user(FLOWWEAVE_USER_ID), db_session_factory() as db:
            db.add(AgentChildConversation(**payload))
            start.wait(timeout=5)
            try:
                db.commit()
            except DBAPIError:
                db.rollback()
                return False
            return True

    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(executor.map(insert, payloads))
    assert sorted(results) == [False, True]


def test_migration_downgrade_refuses_live_relations_and_preserves_parent(graph, settings):
    g = graph
    config = Config(str(Path(__file__).parents[1] / "alembic.ini"))
    config.attributes["database_url"] = settings.database_url
    g.db.commit()
    with pytest.raises(DBAPIError, match="Cannot downgrade"):
        command.downgrade(config, "0175_add_oh_fw_v3_protocol_profile")
    assert g.db.get(AgentChildConversation, g.link.id) is not None
    g.db.execute(delete(AgentChildConversation).where(AgentChildConversation.id == g.link.id))
    g.db.execute(delete(AgentConversationBinding).where(AgentConversationBinding.id == g.child.id))
    g.db.commit()
    command.downgrade(config, "0175_add_oh_fw_v3_protocol_profile")
    command.upgrade(config, "head")
    assert g.db.get(AgentConversationBinding, g.parent.id) is not None
