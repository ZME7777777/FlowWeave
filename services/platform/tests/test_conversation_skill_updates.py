from pathlib import Path
from types import SimpleNamespace

import pytest
from sqlalchemy import select
from test_agent_workspaces import _ready_workspace_for_conversation

from flowweave.modules.agent_sessions.application import conversations, flow_node_conversations
from flowweave.modules.agent_sessions.application.runtime_config import config_from_binding
from flowweave.modules.agent_sessions.infrastructure.models import (
    AgentConversationBinding,
    AgentConversationCapability,
)
from flowweave.modules.users.application.security import FLOWWEAVE_USER_ID, tenant_user
from flowweave.runtime.base import RuntimeProvider
from flowweave.runtime.dependencies import runtime_context
from flowweave.runtime.mock import MockRuntime
from flowweave.shared.artifact_store import artifact_store_context
from flowweave.shared.errors import DomainError
from flowweave.shared.settings import settings_context


@pytest.mark.parametrize("host", ["workspace", "node"])
@pytest.mark.parametrize("outcome", ["success", "failure", "running"])
def test_registered_skill_updates_keep_current_projection_and_immutable_history(
    settings,
    client,
    container,
    db_session_factory,
    monkeypatch,
    skill_capability,
    tmp_path,
    host,
    outcome,
):
    old_id = skill_capability["capability_id"]
    source = client.get(f"/api/v1/capabilities/{old_id}/source").json()["content"]
    response = client.put(
        f"/api/v1/capabilities/{old_id}/source",
        json={"content": source + "\nUse the updated workflow.\n"},
    )
    assert response.status_code == 200, response.text
    new_id = response.json()["id"]

    class Runtime(MockRuntime):
        fail = False
        ready = True

        def __init__(self):
            super().__init__()
            self.loads = []

        def can_accept_input(self, handle):
            return self.ready

        def load_plugin(self, handle, plugin_ref):
            self.loads.append(plugin_ref)
            if self.fail:
                raise DomainError("PLUGIN_LOAD_FAILED", "加载失败", 502)

    monkeypatch.setattr(
        conversations,
        "runtime_provider",
        lambda _db, asset, **kwargs: RuntimeProvider(
            provider_id=asset["asset"]["executor"]["model_provider_id"],
            base_url="https://models.example.test/v1",
            model="test-model",
            api_key="x",
        ),
    )
    runtime = Runtime()
    with (
        tenant_user(FLOWWEAVE_USER_ID),
        settings_context(settings),
        artifact_store_context(container.artifact_store),
        db_session_factory() as db,
        runtime_context(runtime),
    ):
        workspace = _ready_workspace_for_conversation(db)
        created = conversations.create_conversation(
            db, workspace.id, "Skill 升级", workspace.default_model_provider_id, "skill-upgrade"
        )
        binding = db.get(AgentConversationBinding, created["id"])
        conversations.register_conversation_capabilities(db, workspace.id, binding.id, (old_id,))
        original = db.scalar(
            select(AgentConversationCapability).where(
                AgentConversationCapability.binding_id == binding.id
            )
        )
        original_identity = (original.id, original.capability_version_id, original.digest)
        native_handle = conversations._handle(db, workspace, binding)

        if host == "node":
            # Exercise the node registration service with controlled host location,
            # without provisioning a FlowRun or invoking an external Runtime.
            monkeypatch.setattr(
                flow_node_conversations, "_assert_node_session_writable", lambda *a, **k: None
            )
            monkeypatch.setattr(
                flow_node_conversations, "_binding_for_attempt", lambda *a, **k: binding
            )
            monkeypatch.setattr(
                flow_node_conversations, "_node_handle", lambda *a, **k: native_handle
            )
            monkeypatch.setattr(flow_node_conversations, "_attempt", lambda *a: SimpleNamespace())
            monkeypatch.setattr(
                flow_node_conversations,
                "_attempt_context",
                lambda *a: (None, None, SimpleNamespace(runtime_manifest_hash="a" * 64)),
            )
            monkeypatch.setattr(
                flow_node_conversations,
                "_node_capability_root",
                lambda *a, **k: Path(tmp_path) / "node-capabilities",
            )
            monkeypatch.setattr(
                flow_node_conversations, "_node_session_write_available", lambda *a: True
            )

        def register(version_ids):
            if host == "node":
                return flow_node_conversations.register_node_conversation_capabilities(
                    db,
                    flow_run_id="controlled-run",
                    attempt_id="controlled-attempt",
                    binding_id=binding.id,
                    capability_version_ids=version_ids,
                )
            return conversations.register_conversation_capabilities(
                db, workspace.id, binding.id, version_ids
            )

        runtime.fail = outcome == "failure"
        runtime.ready = outcome != "running"
        if outcome == "running":
            with pytest.raises(DomainError, match="完成当前回复"):
                register((new_id,))
            assert len(runtime.loads) == 1
        else:
            result = register((new_id,))
            assert result["completed"] is (outcome == "success")
            assert [item["id"] for item in result["conversation"]["capabilities"]] == [
                new_id if outcome == "success" else old_id
            ]
            assert runtime.loads[-1] != runtime.loads[0]

        expected = new_id if outcome == "success" else old_id
        db.commit()
        db.expire_all()
        assert [item["id"] for item in conversations._dict(db, binding)["capabilities"]] == [
            expected
        ]
        assert [item.version_id for item in config_from_binding(db, binding).capabilities] == [
            expected
        ]
        projected = conversations._page_dicts(db, [binding], write_available=True)[0]
        assert [item["id"] for item in projected["capabilities"]] == [expected]
        assert (original.id, original.capability_version_id, original.digest) == original_identity
        history = list(
            db.scalars(
                select(AgentConversationCapability).where(
                    AgentConversationCapability.binding_id == binding.id
                )
            )
        )
        assert len(history) == (2 if outcome == "success" else 1)

        if outcome == "success":
            count = len(runtime.loads)
            assert register((new_id,))["already_loaded_capability_version_ids"] == [new_id]
            assert len(runtime.loads) == count
            with pytest.raises(DomainError, match="新版本"):
                register((old_id,))
        elif outcome == "failure":
            runtime.fail = False
            assert register((new_id,))["completed"] is True
            assert [item.version_id for item in config_from_binding(db, binding).capabilities] == [
                new_id
            ]
