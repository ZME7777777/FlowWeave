"""Two-host child unread and activity aggregation through authenticated routes."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from uuid import uuid4

import pytest
import test_child_sessions as child_fixtures

from flowweave.modules.agent_sessions.application import (
    child_activity,
    conversations,
    flow_node_conversations,
)
from flowweave.modules.agent_sessions.infrastructure.models import AgentConversationBinding
from flowweave.modules.users.application.security import tenant_user
from flowweave.runtime.base import RuntimeConversationActivity, RuntimeEventBatch, RuntimeResult
from flowweave.runtime.dependencies import runtime_context
from flowweave.shared.errors import DomainError
from flowweave.shared.models import BackgroundTask

parent_graph = child_fixtures.parent_graph
parent_profile = child_fixtures.parent_profile
gateway_base_url = child_fixtures.gateway_base_url
launcher_http = child_fixtures.launcher_http
child_http = child_fixtures.child_http
pytestmark = pytest.mark.parametrize("parent_profile", ["OH_FW_V4"], indirect=True)


@pytest.fixture
def activity(child_http, monkeypatch):
    x = child_http
    catalog = {}
    completion = SimpleNamespace(result=None, failure=None, calls=[])
    x.client.app.state.container.blocking_mutation_slots = asyncio.Semaphore(1)

    def read(handle):
        completion.calls.append(handle.conversation_id)
        if completion.failure is not None:
            raise completion.failure
        return RuntimeEventBatch(result=completion.result)

    x.runtime.read_active_events = read
    x.runtime.conversation_activity_snapshot = lambda handle: dict(catalog)
    monkeypatch.setattr(conversations, "_handle", lambda *_args: x.loc.parent_handle)
    monkeypatch.setattr(
        flow_node_conversations, "_node_handle", lambda *_args, **_kwargs: x.loc.parent_handle
    )
    monkeypatch.setattr(
        flow_node_conversations.agent_sessions,
        "resolve_flow_node_session_host",
        lambda *_args, **_kwargs: SimpleNamespace(),
    )
    if x.g.kind == "AGENT_WORKSPACE":
        path = f"/api/v1/agent-workspaces/{x.loc.host_id}/conversation-activity"
    else:
        path = (
            f"/api/v1/flow-runs/{x.loc.host_id}/node-attempts/{x.loc.scope_id}"
            "/agent-sessions/activity"
        )
    yield SimpleNamespace(x=x, catalog=catalog, completion=completion, path=path)


def observe(a, status="running", *, stale=False, child_id=None):
    identity = child_id or a.x.loc.handle.conversation_id
    a.catalog[identity] = RuntimeConversationActivity(
        identity, status, "2000-01-01T00:00:00+00:00" if stale else "2999-01-01T00:00:00+00:00"
    )


def poll(a, **params):
    a.x.g.db.commit()
    response = a.x.client.get(a.path, params=params)
    assert response.status_code == 200, response.text
    return response.json()


def child(a):
    a.x.g.db.expire_all()
    return a.x.g.db.get(AgentConversationBinding, a.x.loc.binding_id)


def final(event_id="formal-child-final"):
    return RuntimeResult(
        status="COMPLETED",
        final_message="正式子会话最终回复",
        completion_event_id=event_id,
        completion_event_kind="ASSISTANT_MESSAGE",
    )


def test_child_running_keeps_parent_entry_without_parent_spinner(activity):
    a = activity
    observe(a)
    observe(a, "finished", child_id=a.x.loc.parent_handle.conversation_id)
    observe(a, child_id="unbound-native-child")
    result = poll(a, active_binding_id=a.x.loc.parent_id)
    assert result["running_binding_ids"] == []
    assert result["child_activity_parent_binding_ids"] == [a.x.loc.parent_id]
    assert [item["id"] for item in result["conversations"]] == [a.x.loc.parent_id]
    assert result["conversations"][0]["child_summary"] == {
        "total": 1,
        "running": 1,
        "unread": 0,
        "attention": 0,
        "condensing": 0,
        "unknown": 0,
        "pending": 0,
    }
    assert result["conversations"][0]["unread"] is False
    assert [item["id"] for item in result["child_activity"]] == [a.x.loc.binding_id]
    assert a.x.controls == []
    assert a.completion.calls == []


@pytest.mark.parametrize("failure", [None, "RUNTIME_BUSINESS_READ_TIMEOUT", "missing", "no-final"])
def test_only_formal_child_completion_notifies_and_is_deduplicated(activity, failure):
    a = activity
    observe(a)
    poll(a)
    assert child(a).activity_was_running is True
    a.catalog.clear()
    if failure in {"RUNTIME_BUSINESS_READ_TIMEOUT", "missing"}:
        code = "RUNTIME_CONVERSATION_MISSING" if failure == "missing" else failure
        a.completion.failure = DomainError(
            code, "unavailable", 404 if failure == "missing" else 504
        )
    if failure == "no-final":
        a.completion.result = RuntimeResult(status="RUNNING")
    unknown = poll(a)
    assert unknown["child_activity"][0]["execution_status"] == "unknown"
    assert unknown["running_binding_ids"] == []
    assert unknown["child_activity_parent_binding_ids"] == [a.x.loc.parent_id]
    assert child(a).unread is False
    assert child(a).activity_was_running is True
    assert child(a).terminal_reconciliation_pending is True
    a.completion.failure = None
    a.completion.result = final()
    complete = poll(a, active_binding_id=a.x.loc.parent_id)
    row = child(a)
    assert row.unread is True and row.unread_origin == "MANUAL"
    assert row.last_notified_completion_event_id == "formal-child-final"
    assert row.final_reply_preview_event_id == "formal-child-final"
    assert row.final_reply_preview_content == "正式子会话最终回复"
    assert row.activity_was_running is False and row.terminal_reconciliation_pending is False
    assert complete["conversations"][0]["unread"] is False
    assert complete["conversations"][0]["child_summary"]["unread"] == 1
    a.x.g.db.commit()
    marked = a.x.client.put(a.x.root + "/unread", json={"unread": False})
    assert marked.status_code == 200, marked.text
    poll(a)
    assert child(a).unread is False
    assert child(a).final_reply_preview_event_id is None


def test_focused_child_completion_does_not_become_background_unread(activity):
    a = activity
    observe(a)
    params = {"active_binding_id": a.x.loc.parent_id, "active_child_id": a.x.loc.binding_id}
    poll(a, **params)
    observe(a, "finished")
    a.completion.result = final()
    poll(a, **params)
    assert child(a).unread is False
    assert child(a).last_notified_completion_event_id == "formal-child-final"
    assert child(a).terminal_reconciliation_pending is False


@pytest.mark.parametrize("status", ["running", "error", "stuck"])
def test_child_attention_is_separate_and_system_ack_survives_polling(activity, status):
    a = activity
    observe(a, status, stale=True)
    result = poll(a)
    assert child(a).unread_origin == "SYSTEM" and child(a).unread is True
    assert result["conversations"][0]["child_summary"]["attention"] == 1
    assert result["failed_binding_ids"] == []
    assert result["possibly_stuck_binding_ids"] == []
    assert result["child_activity"][0]["possibly_stuck"] == (status == "running")
    a.x.g.db.commit()
    response = a.x.client.put(
        a.x.root + "/unread", json={"unread": False, "unread_origin": "SYSTEM"}
    )
    assert response.status_code == 200, response.text
    poll(a)
    assert child(a).unread is False and child(a).unread_origin == "SYSTEM"
    a.x.g.db.commit()
    a.x.client.put(a.x.root + "/unread", json={"unread": True})
    result = poll(a)
    assert child(a).unread_origin == "MANUAL"
    assert result["conversations"][0]["child_summary"]["attention"] == 0


def test_unknown_catalog_does_not_revoke_system_ack_or_mark_terminal(activity):
    a = activity
    observe(a, stale=True)
    poll(a)
    a.catalog.clear()
    a.x.g.db.commit()
    a.x.client.put(a.x.root + "/unread", json={"unread": False, "unread_origin": "SYSTEM"})
    poll(a)
    assert child(a).unread_origin == "SYSTEM"
    assert child(a).activity_was_running is True


def test_poll_reloads_child_under_lock_after_concurrent_unread_ack(activity):
    a = activity
    observe(a, stale=True)
    poll(a)
    stale = child(a)
    assert stale.unread is True
    with a.x.g.sessions() as writer:
        child_activity.set_unread(
            writer, a.x.loc.parent_id, a.x.loc.binding_id, unread=False, origin="SYSTEM"
        )
        writer.commit()
    assert stale.unread is True
    with runtime_context(a.x.runtime):
        summaries, _ = child_activity.reconcile(
            a.x.g.db,
            [a.x.g.db.get(AgentConversationBinding, a.x.loc.parent_id)],
            a.catalog,
            active_binding_id=None,
            active_child_id=None,
        )
    assert stale.unread is False
    assert summaries[a.x.loc.parent_id]["attention"] == 0


def test_native_finish_action_cannot_become_assistant_reply_preview(activity):
    a = activity
    observe(a)
    poll(a)
    observe(a, "finished")
    a.completion.result = RuntimeResult(
        status="COMPLETED",
        final_message="tool result",
        completion_event_id="formal-finish",
        completion_event_kind="FINISH_ACTION",
    )
    result = poll(a)
    assert child(a).unread is False
    assert child(a).final_reply_preview_event_id is None
    assert child(a).terminal_reconciliation_pending is False
    assert result["child_activity_parent_binding_ids"] == []


def test_child_condensation_uses_owned_task_without_parent_running_state(activity):
    a = activity
    observe(a, "finished")
    a.completion.result = final()
    poll(a, active_binding_id=a.x.loc.parent_id, active_child_id=a.x.loc.binding_id)

    def task(owner):
        return BackgroundTask(
            owner_user_id=owner,
            aggregate_type="AGENT_CONVERSATION",
            aggregate_id=a.x.loc.binding_id,
            task_type="CONDENSE_AGENT_CONVERSATION",
            idempotency_key=str(uuid4()),
            state="PENDING",
        )

    a.x.g.db.add(task(a.x.g.other))
    assert poll(a)["child_activity_parent_binding_ids"] == []
    a.x.g.db.add(task(a.x.g.owner))
    result = poll(a)
    assert result["running_binding_ids"] == []
    assert result["condensing_binding_ids"] == []
    assert result["child_activity_parent_binding_ids"] == [a.x.loc.parent_id]
    assert result["conversations"][0]["child_summary"]["condensing"] == 1
    assert result["child_activity"][0]["condensing"] is True


def test_child_followup_invalidates_formal_reply_preview(activity):
    a = activity
    row = child(a)
    row.unread = True
    row.unread_origin = "MANUAL"
    row.final_reply_preview_event_id = "old-final-event"
    row.final_reply_preview_content = "old final answer"
    a.x.g.db.commit()
    response = a.x.client.post(
        a.x.root + "/messages", json={"submission_id": str(uuid4()), "content": "follow up"}
    )
    assert response.status_code == 202, response.text
    assert child(a).final_reply_preview_event_id is None
    assert child(a).final_reply_preview_content is None


def test_list_and_unread_work_offline_without_touching_parent_or_sibling(activity, monkeypatch):
    a = activity
    g = a.x.g
    # A second native creation has its own formal invocation and governed binding.
    lf = child_fixtures.launcher_fixtures
    original_proof = lf.proof

    def second_proof(graph, **changes):
        return original_proof(
            graph, **{**changes, "action_id": "second-action", "tool_call_id": "second-tool"}
        )

    monkeypatch.setattr(lf, "proof", second_proof)
    response = lf.post_child(g, a.x.client)
    assert response.status_code == 200, response.text
    sibling_id = response.json()["binding_id"]
    assert sibling_id != a.x.loc.binding_id
    parent = g.db.get(AgentConversationBinding, a.x.loc.parent_id)
    sibling = g.db.get(AgentConversationBinding, sibling_id)
    target = child(a)
    parent.unread = sibling.unread = target.unread = True
    parent.unread_origin = sibling.unread_origin = "MANUAL"
    g.db.commit()
    monkeypatch.setattr(
        a.x.runtime, "conversation_activity_snapshot", lambda _: pytest.fail("offline")
    )
    monkeypatch.setattr(a.x.runtime, "read_child_preparation", lambda *_: pytest.fail("offline"))
    listed = a.x.client.get(a.x.root.rsplit("/", 1)[0])
    assert listed.status_code == 200, listed.text
    assert {row["id"] for row in listed.json()} == {sibling_id, a.x.loc.binding_id}
    result = a.x.client.put(a.x.root + "/unread", json={"unread": False})
    assert result.status_code == 200, result.text
    g.db.expire_all()
    assert child(a).unread is False
    assert g.db.get(AgentConversationBinding, sibling_id).unread is True
    assert g.db.get(AgentConversationBinding, a.x.loc.parent_id).unread is True
    with runtime_context(a.x.runtime):
        summary = child_activity.summaries(
            g.db, [g.db.get(AgentConversationBinding, a.x.loc.parent_id)]
        )
    parent = g.db.get(AgentConversationBinding, a.x.loc.parent_id)
    page = (
        conversations._page_dicts(g.db, [parent], write_available=True)
        if g.kind == "AGENT_WORKSPACE"
        else flow_node_conversations._node_session_page_dicts(g.db, [parent])
    )
    assert page[0]["child_summary"] == summary[parent.id]
    assert summary[a.x.loc.parent_id]["unread"] == 1
    assert summary[a.x.loc.parent_id]["running"] is None
    assert a.x.controls == []


@pytest.mark.parametrize("operation", ["list", "unread", "wrong-parent", "anonymous"])
def test_child_metadata_does_not_cross_authority(activity, operation):
    a = activity
    if operation == "anonymous":
        a.x.client.cookies.clear()
        response = a.x.client.get(a.x.root.rsplit("/", 1)[0])
        assert response.status_code == 401
        return
    if operation == "wrong-parent":
        response = a.x.client.put(
            a.x.root.replace(a.x.loc.parent_id, str(uuid4())) + "/unread", json={"unread": True}
        )
    else:
        a.x.client.cookies.set("flowweave_session", "other-cookie")
        # The graph fixture's ambient owner must not override the browser principal.
        with tenant_user(a.x.g.other):
            response = (
                a.x.client.get(a.x.root.rsplit("/", 1)[0])
                if operation == "list"
                else a.x.client.put(a.x.root + "/unread", json={"unread": True})
            )
    assert response.status_code == 404, response.text
    assert a.x.loc.handle.conversation_id not in response.text
    assert child(a).unread is False


@pytest.mark.parametrize("admission", ["create", "user", "parent", "lost-first", "lost-followup"])
def test_fast_native_completion_is_not_lost_before_first_activity_poll(activity, admission):
    from urllib.parse import urlsplit

    a = activity
    if admission in {"user", "parent", "lost-followup"}:
        observe(a, "finished")
        a.completion.result = final("old-final")
        poll(a)
        a.x.client.put(a.x.root + "/unread", json={"unread": False})
        if admission == "user":
            response = a.x.client.post(
                a.x.root + "/messages",
                json={
                    "submission_id": str(uuid4()),
                    "content": "followup",
                },
            )
            assert response.status_code == 202, response.text
        elif admission == "parent":
            response = a.x.client.post(
                urlsplit(a.x.g.frozen.command_url).path.removeprefix("/prefix"),
                headers={"X-Session-API-Key": a.x.g.key.get_secret_value()},
                json={
                    "token": "native-proof",
                    "command": {
                        "operation": "send",
                        "conversation_id": a.x.loc.handle.conversation_id,
                        "text": "parent followup",
                    },
                },
            )
            assert response.status_code == 200, response.text
        else:
            # Accepted native send, process died before metadata projection.
            # The formal catalog changes even if no running sample was seen.
            a.catalog[a.x.loc.handle.conversation_id] = RuntimeConversationActivity(
                a.x.loc.handle.conversation_id, "finished", "2999-01-02T00:00:00+00:00"
            )
    elif admission == "lost-first":
        row = child(a)
        row.last_message_at = None
        row.terminal_reconciliation_pending = False
        a.x.g.db.commit()
    if admission not in {"lost-first", "lost-followup"}:
        assert child(a).terminal_reconciliation_pending is True
        assert child(a).activity_was_running is False
    if admission != "lost-followup":
        observe(a, "finished")
    a.completion.result = final("fast-final")
    result = poll(a)
    assert child(a).unread is True
    assert child(a).last_notified_completion_event_id == "fast-final"
    assert result["conversations"][0]["unread"] is False
    a.x.client.put(a.x.root + "/unread", json={"unread": False})
    calls = len(a.completion.calls)
    poll(a)
    assert child(a).unread is False
    assert len(a.completion.calls) == calls


def test_fast_completion_timeout_preserves_pending_and_retries_without_duplicate(activity):
    a = activity
    observe(a, "finished")
    a.completion.failure = DomainError("RUNTIME_BUSINESS_READ_TIMEOUT", "defer", 504)
    poll(a)
    assert child(a).terminal_reconciliation_pending is True
    assert child(a).unread is False
    a.completion.failure = None
    a.completion.result = final()
    poll(a)
    assert child(a).unread is True
    assert child(a).terminal_reconciliation_pending is False


def test_prepared_child_without_formal_completion_cannot_get_unread(activity):
    a = activity
    observe(a, "idle")
    a.completion.result = None
    poll(a)
    assert child(a).unread is False
    assert child(a).terminal_reconciliation_pending is True


def test_completion_read_watermark_migration_preserves_child_and_native_locator(activity, settings):
    from pathlib import Path

    from alembic import command
    from alembic.config import Config

    a = activity
    observe(a, "finished")
    a.completion.result = final()
    poll(a)
    assert child(a).child_completion_checked_activity_at is not None
    original = child(a).openhands_conversation_id
    a.x.g.db.commit()
    config = Config(str(Path(__file__).parents[1] / "alembic.ini"))
    config.attributes["database_url"] = settings.database_url
    command.downgrade(config, "0179_agent_child_creation_audit")
    command.upgrade(config, "head")
    a.x.g.db.expire_all()
    assert child(a).openhands_conversation_id == original
    assert child(a).unread is True
    assert child(a).last_notified_completion_event_id == "formal-child-final"
    assert child(a).child_completion_checked_activity_at is None
    a.x.g.db.commit()
    a.x.client.put(a.x.root + "/unread", json={"unread": False})
    poll(a)
    assert child(a).unread is False


def test_catalog_change_only_schedules_read_and_never_correlates_completion_by_time(activity):
    a = activity
    observe(a, "finished")
    a.completion.result = final()
    poll(a)
    a.x.client.put(a.x.root + "/unread", json={"unread": False})
    a.catalog[a.x.loc.handle.conversation_id] = RuntimeConversationActivity(
        a.x.loc.handle.conversation_id, "finished", "3000-01-01T00:00:00+00:00"
    )
    poll(a)
    assert child(a).unread is False
    assert child(a).last_notified_completion_event_id == "formal-child-final"
