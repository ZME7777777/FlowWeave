from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
from test_agent_workspaces import _ready_workspace_for_conversation

from flowweave.modules.agent_sessions.application import conversations
from flowweave.modules.agent_sessions.infrastructure.models import AgentSidebarConversation
from flowweave.modules.users.application.security import tenant_bypass, tenant_user
from flowweave.shared.errors import DomainError
from flowweave.shared.models import (
    AgentConversationBinding,
    AgentWorkDirectory,
    AgentWorkDirectoryVersion,
)
from flowweave.shared.settings import settings_context


@pytest.fixture
def archive_records(client, user_client, settings, db_session_factory, monkeypatch):
    owner = client.get("/api/v1/auth/me").json()["id"]
    other_owner = user_client.get("/api/v1/auth/me").json()["id"]
    with settings_context(settings), tenant_user(owner), db_session_factory() as db:
        workspace = _ready_workspace_for_conversation(db)
        directory = AgentWorkDirectory(
            workspace_id=workspace.id, display_name="Retained workspace", current_version=2
        )
        db.add(directory)
        db.flush()
        versions = [
            AgentWorkDirectoryVersion(
                work_directory_id=directory.id, version=n, working_path=f"project-{n}"
            )
            for n in (1, 2)
        ]
        db.add_all(versions)
        db.flush()
        records = {}
        for index, name in enumerate(
            ("first", "second", "running", "temporary", "root", "archived", "private")
        ):
            item = AgentConversationBinding(
                workspace_id=workspace.id,
                runtime_session_id=workspace.id,
                host_id=workspace.id,
                conversation_scope_id=workspace.id,
                openhands_conversation_id=str(uuid4()),
                create_idempotency_key=f"bulk-{name}",
                lifecycle="ACTIVE",
                display_title=name,
                work_directory_version_id=None if name == "root" else versions[index % 2].id,
                archived_at=datetime.now(UTC) - timedelta(days=1) if name == "archived" else None,
            )
            db.add(item)
            db.flush()
            records[name] = item.id
        db.add(
            AgentSidebarConversation(
                workspace_id=workspace.id,
                source_binding_id=records["first"],
                sidebar_binding_id=records["temporary"],
                expires_at=datetime.now(UTC) + timedelta(hours=1),
            )
        )
        db.commit()
        workspace_id, directory_id = workspace.id, directory.id
    with tenant_bypass(), db_session_factory() as db:
        db.get(AgentConversationBinding, records["private"]).owner_user_id = other_owner
        db.commit()
    checked = []

    def stopped(db, workspace_id, binding_id, **kwargs):
        checked.append(binding_id)
        if binding_id == records["running"]:
            raise DomainError("AGENT_CONVERSATION_RUNNING", "Running", 409)

    monkeypatch.setattr(conversations, "assert_conversation_stopped", stopped)
    return workspace_id, directory_id, records, checked


def test_bulk_archive_is_scoped_idempotent_and_keeps_workspace(
    client, user_client, db_session_factory, archive_records
):
    workspace_id, directory_id, records, checked = archive_records
    path = f"/api/v1/agent-workspaces/{workspace_id}/conversation-management/archive"
    payload = {"binding_ids": list(records.values()) + [records["first"], "missing"]}
    result = client.post(path, params={"work_directory_id": directory_id}, json=payload)
    assert result.status_code == 200, result.text
    body = result.json()
    assert {item["id"] for item in body["archived"]} == {
        records[n] for n in ("first", "second", "archived")
    }
    assert {item["id"] for item in body["skipped"]} == {
        records[n] for n in ("running", "root", "private", "temporary")
    } | {"missing"}
    assert (
        next(item for item in body["skipped"] if item["id"] == records["running"])["code"]
        == "AGENT_CONVERSATION_RUNNING"
    )
    assert set(checked) == {records[n] for n in ("first", "second", "running")}
    repeated = client.post(
        path,
        params={"work_directory_id": directory_id},
        json={"binding_ids": [records["first"], records["second"], records["archived"]]},
    )
    assert repeated.json()["archived"] == body["archived"]
    with db_session_factory() as db:
        directory = db.get(AgentWorkDirectory, directory_id)
        assert directory.archived_at is None and directory.current_version == 2
        assert db.get(AgentConversationBinding, records["root"]).archived_at is None
    active = client.get(
        f"/api/v1/agent-workspaces/{workspace_id}/conversation-management",
        params={"work_directory_id": directory_id, "archived": "false"},
    ).json()
    assert [item["id"] for item in active["items"]] == [records["running"]]
    denied = user_client.post(
        path, params={"work_directory_id": directory_id}, json={"binding_ids": [records["first"]]}
    )
    assert denied.status_code in (403, 404)


def test_bulk_archive_validation_and_root_scope(client, archive_records):
    workspace_id, directory_id, records, _checked = archive_records
    path = f"/api/v1/agent-workspaces/{workspace_id}/conversation-management/archive"
    for ids in ([], ["x"] * 21):
        assert client.post(path, json={"binding_ids": ids}).status_code == 422
    root = client.post(path, json={"binding_ids": [records["root"], records["first"]]})
    assert [item["id"] for item in root.json()["archived"]] == [records["root"]]
    assert root.json()["skipped"] == [
        {"id": records["first"], "code": "AGENT_CONVERSATION_SCOPE_MISMATCH"}
    ]
    client.cookies.clear()
    assert (
        client.post(
            path,
            params={"work_directory_id": directory_id},
            json={"binding_ids": [records["first"]]},
        ).status_code
        == 401
    )


def test_bulk_archive_rolls_back_the_request_on_unexpected_failure(
    client, db_session_factory, archive_records, monkeypatch
):
    workspace_id, directory_id, records, _checked = archive_records
    selected = sorted([records["first"], records["second"]])

    def fail_late(db, workspace_id, binding_id, **kwargs):
        if binding_id == selected[-1]:
            raise RuntimeError("controlled probe failure")

    monkeypatch.setattr(conversations, "assert_conversation_stopped", fail_late)
    with pytest.raises(RuntimeError, match="controlled probe failure"):
        client.post(
            f"/api/v1/agent-workspaces/{workspace_id}/conversation-management/archive",
            params={"work_directory_id": directory_id},
            json={"binding_ids": selected},
        )
    with db_session_factory() as db:
        assert all(
            db.get(AgentConversationBinding, identity).archived_at is None for identity in selected
        )
