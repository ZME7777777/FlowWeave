"""Formal statistics remain cheap, tenant-scoped, and safe under late snapshots."""

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import text
from test_work_directory_deletion import graph as directory_graph

from flowweave.modules.agent_sessions.application import conversations, usage, usage_reconciliation
from flowweave.modules.agent_sessions.infrastructure.models import AgentConversationBinding
from flowweave.modules.users.application.security import USER_USER_ID, tenant_user
from flowweave.runtime.base import RuntimeEventBatch, RuntimeUsageSnapshot

graph = directory_graph


def snapshot(count, tokens=100):
    return RuntimeEventBatch(
        stored_event_count=count,
        usage=(
            RuntimeUsageSnapshot(
                usage_id="flowweave:primary",
                model_name="test-model",
                accumulated_cost=0,
                prompt_tokens=tokens,
                completion_tokens=20,
                cache_read_tokens=30,
                cache_write_tokens=0,
                reasoning_tokens=0,
                context_window=100000,
                per_turn_tokens=50,
            ),
        ),
    )


def test_management_projects_archived_size_without_reading_native_history(graph, monkeypatch):
    g = graph
    old = g.binding()
    empty = g.binding()
    large = g.binding()
    usage_reconciliation.record_success(g.db, empty.id, RuntimeEventBatch(stored_event_count=0))
    usage_reconciliation.record_success(g.db, large.id, snapshot(12000))
    monkeypatch.setattr(
        g.runtime,
        "read_active_events",
        lambda *_: pytest.fail("Management must read only projections"),
    )
    items = {
        item["id"]: item
        for item in conversations.list_management_conversations(g.db, g.host.id)["items"]
    }
    assert items[old.id]["stored_event_count"] is None
    assert items[old.id]["total_tokens"] is None
    assert items[empty.id]["stored_event_count"] == 0
    assert items[empty.id]["total_tokens"] == 0
    assert items[large.id]["stored_event_count"] == 12000
    assert items[large.id]["total_tokens"] == 150
    assert items[large.id]["archived_at"]
    directory = g.directory("separate")
    assert (
        conversations.list_management_conversations(
            g.db, g.host.id, work_directory_id=directory["id"]
        )["items"]
        == []
    )
    with tenant_user(USER_USER_ID):
        assert conversations.list_management_conversations(g.db, g.host.id)["items"] == []


def test_late_count_snapshots_cannot_lower_the_committed_projection(graph, db_session_factory):
    g = graph
    binding = g.binding()
    usage_reconciliation.record_success(g.db, binding.id, snapshot(100))
    g.db.commit()
    with db_session_factory() as stale:
        previous = stale.get(AgentConversationBinding, binding.id)
        assert previous.stored_event_count == 100
        stale.commit()
        usage_reconciliation.record_success(g.db, binding.id, snapshot(200))
        g.db.commit()
        usage.capture(stale, previous, (), stored_event_count=110)
        stale.commit()
    g.db.expire_all()
    assert g.db.get(AgentConversationBinding, binding.id).stored_event_count == 200
    usage_reconciliation.record_success(g.db, binding.id, RuntimeEventBatch())
    assert g.db.get(AgentConversationBinding, binding.id).stored_event_count == 200


def test_event_count_migration_roundtrip_preserves_legacy_records(graph, settings):
    g = graph
    binding = g.binding()
    binding_id = binding.id
    g.db.commit()
    config = Config("alembic.ini")
    config.attributes["database_url"] = settings.database_url
    command.downgrade(config, "0193_case_copy_stepwise_execution")
    assert (
        g.db.execute(
            text("SELECT id FROM agent_conversation_bindings WHERE id=:id"), {"id": binding_id}
        ).scalar_one()
        == binding_id
    )
    g.db.rollback()
    command.upgrade(config, "head")
    g.db.expire_all()
    assert g.db.get(AgentConversationBinding, binding_id).stored_event_count is None
