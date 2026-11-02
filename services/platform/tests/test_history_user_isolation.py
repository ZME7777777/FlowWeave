"""Persist both users' bindings: no mocked owner-aware binding resolver."""

from types import SimpleNamespace
from uuid import uuid4

import pytest
import test_history_sessions as session_fixtures
from sqlalchemy import MetaData

from flowweave.modules.agent_sessions.application import (
    conversations,
    history_access,
    history_sessions,
)
from flowweave.modules.agent_sessions.infrastructure.models import AgentConversationBinding
from flowweave.modules.agent_sessions.presentation import history_mcp as mcp
from flowweave.modules.agent_workspaces.infrastructure.models import AgentWorkspace
from flowweave.modules.users.application.security import tenant_bypass, tenant_user
from flowweave.modules.users.infrastructure.models import User
from flowweave.runtime.base import RuntimeEvent, RuntimeEventBatch
from flowweave.shared.errors import DomainError

# The shared fixture replaces this resolver; these tests restore the real SQL path.
_owned_bindings = conversations.history_reference_bindings
graph = session_fixtures.graph


@pytest.fixture(autouse=True)
def database():
    """SQLite application isolation, not PostgreSQL RLS acceptance."""


@pytest.fixture
def persisted(graph, monkeypatch):
    g = graph
    table = AgentConversationBinding.__table__.to_metadata(MetaData())
    # Only the unrelated PostgreSQL regex CHECK is unavailable in SQLite.
    for constraint in tuple(table.constraints):
        if constraint.name == "ck_agent_conversation_working_directory":
            table.constraints.remove(constraint)
    table.create(g.db.get_bind())
    AgentWorkspace.__table__.create(g.db.get_bind())
    g.db.add(AgentWorkspace(id="workspace", scope_key="default"))
    for binding in g.bindings.values():
        g.db.add(
            AgentConversationBinding(
                **vars(binding),
                workspace_id="workspace",
                conversation_scope_id=binding.id,
                create_idempotency_key=binding.id,
            )
        )
    g.db.commit()
    other = User(id=str(uuid4()), username="other", role="SUPER_ADMIN", is_active=True)
    with tenant_user(other.id):
        g.db.add(other)
        target = AgentConversationBinding(
            workspace_id="workspace",
            host_id="workspace",
            conversation_scope_id=str(uuid4()),
            create_idempotency_key=str(uuid4()),
            lifecycle="ACTIVE",
            runtime_session_id=g.target.runtime_session_id,
            openhands_conversation_id=g.target.openhands_conversation_id,
        )
        source = AgentConversationBinding(
            workspace_id="workspace",
            host_id="workspace",
            conversation_scope_id=str(uuid4()),
            create_idempotency_key=str(uuid4()),
            lifecycle="ACTIVE",
            runtime_session_id=g.first.runtime_session_id,
            openhands_conversation_id=g.first.openhands_conversation_id,
        )
        g.db.add_all([target, source])
        g.db.commit()
    monkeypatch.setattr(conversations, "history_reference_bindings", _owned_bindings)
    return SimpleNamespace(**vars(g), other=other, other_target=target, other_source=source)


def test_each_user_can_only_grant_and_query_own_explicit_sources(persisted):
    g = persisted
    _, token = history_sessions.renew(g.db, "workspace", g.target.id, (g.first.id,))
    assert history_access.authorize(g.db, token, source_binding_id=g.first.id)
    with pytest.raises(DomainError):
        history_access.authorize(g.db, token, source_binding_id=g.other_source.id)
    with pytest.raises(DomainError):
        history_sessions.renew(g.db, "workspace", g.target.id, (g.other_source.id,))
    with tenant_user(g.other.id):
        for target_id, source_id in ((g.target.id, g.first.id), (g.other_target.id, g.first.id)):
            with pytest.raises(DomainError):
                history_sessions.renew(g.db, "workspace", target_id, (source_id,))
        _, other_token = history_sessions.renew(
            g.db,
            "workspace",
            g.other_target.id,
            (g.other_source.id,),
        )
        assert history_access.authorize(g.db, other_token, source_binding_id=g.other_source.id)
        with pytest.raises(DomainError):
            history_access.authorize(g.db, other_token, source_binding_id=g.first.id)


@pytest.mark.parametrize("foreign_side", ["target", "source"])
def test_explicit_owner_predicates_still_apply_during_trusted_tenant_bypass(
    persisted, foreign_side
):
    g = persisted
    target = g.other_target.id if foreign_side == "target" else g.target.id
    source = g.other_source.id if foreign_side == "source" else g.first.id
    with tenant_bypass():
        with pytest.raises(DomainError) as caught:
            _owned_bindings(g.db, "workspace", target, source)
        assert caught.value.code == "AGENT_CONVERSATION_NOT_FOUND"


def test_reference_metadata_cannot_disclose_another_users_conversation(persisted, monkeypatch):
    g = persisted
    class Runtime:
        def reload_conversation(self, _handle):
            return None

        def read_active_events(self, _handle):
            return RuntimeEventBatch(
                events=(
                    RuntimeEvent(
                        cursor="source-message",
                        event_type="MESSAGE",
                        payload={"source": "user", "content": "来源内容"},
                    ),
                )
            )

    monkeypatch.setattr(
        conversations, "get_runtime", lambda: Runtime()
    )
    monkeypatch.setattr(conversations, "_handle", lambda *_args: object())
    workspace = g.db.get(AgentWorkspace, "workspace")
    own = conversations._resolve_source_conversation_reference(
        g.db, workspace, g.first.id, None, None
    )
    assert own["source_binding_id"] == g.first.id
    assert own["history_tool"] == "platform_conversation_history"
    assert own["resolved_content"] == "user: 来源内容"
    with pytest.raises(DomainError) as caught:
        conversations._resolve_source_conversation_reference(
            g.db, workspace, g.other_source.id, None, None
        )
    assert caught.value.code == "AGENT_CONVERSATION_NOT_FOUND"


@pytest.mark.parametrize("phase", ["prepare", "project"])
def test_mcp_rejects_foreign_source_before_preparing_or_projecting_history(
    persisted, monkeypatch, phase
):
    g = persisted
    _, token = history_sessions.renew(g.db, "workspace", g.target.id, (g.first.id,))
    args = mcp.HistoryArguments(source_binding_id=g.other_source.id)
    monkeypatch.setattr(
        conversations, "prepare_conversation_events", lambda *_args: pytest.fail("Not authorized")
    )
    monkeypatch.setattr(
        conversations,
        "assert_prepared_conversation_events_current",
        lambda *_args: pytest.fail("Not authorized"),
    )
    with pytest.raises(DomainError) as caught:
        if phase == "prepare":
            mcp.prepare_page(g.db, token, args)
        else:
            mcp.project_page(g.db, None, RuntimeEventBatch(events=()), token, args)
    assert caught.value.code == "AGENT_HISTORY_ACCESS_DENIED"
