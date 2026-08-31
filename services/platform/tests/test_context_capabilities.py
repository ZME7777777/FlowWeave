from __future__ import annotations

import base64
from pathlib import Path
from types import SimpleNamespace

import pytest

from flowweave.modules.agent_sessions.application import conversations
from flowweave.modules.agent_sessions.application.runtime_config import (
    FrozenSessionCapability,
    FrozenSessionConfig,
    build_agent_spec,
)
from flowweave.shared.settings import settings_context


def _import_context(client, *, filename: str = "product-context.md") -> str:
    validated = client.post(
        "/api/v1/capability-imports/validate",
        json={
            "capability_type": "CONTEXT",
            "filename": filename,
            "content_base64": base64.b64encode(
                "# 产品上下文\n必须引用来源并标明不确定性。\n".encode()
            ).decode(),
        },
    )
    assert validated.status_code == 200, validated.text
    committed = client.post(
        "/api/v1/capability-imports",
        json={"import_token": validated.json()["import_token"]},
    )
    assert committed.status_code == 201, committed.text
    return committed.json()["capabilities"][0]["capability_id"]


def test_node_context_is_frozen_and_blocks_capability_deletion(client):
    context_id = _import_context(client)
    created = client.post(
        "/api/v1/node-assets",
        json={
            "name": "带 Context 的节点",
            "description": "",
            "inputs": [],
            "outputs": [],
            "executor": {
                "startup_prompt": "开始任务",
                "context_prompt": "自由文本上下文",
                "context_capability_ids": [context_id],
            },
        },
    )
    assert created.status_code == 201, created.text
    asset = created.json()
    assert asset["executor"]["context_capability_ids"] == [context_id]
    assert asset["context_capabilities"] == [
        {
            "id": context_id,
            "capability_key": "product-context",
            "digest": asset["context_capabilities"][0]["digest"],
            "content_hash": asset["context_capabilities"][0]["content_hash"],
            "text": "# 产品上下文\n必须引用来源并标明不确定性。",
        }
    ]

    deleted = client.request("DELETE", "/api/v1/capabilities", json={"ids": [context_id]})
    assert deleted.status_code == 200, deleted.text
    assert deleted.json()["deleted_ids"] == []
    assert deleted.json()["blocked"][0]["relation"] == "NODE_CONTEXT"
    assert deleted.json()["blocked"][0]["nodes"] == [
        {"id": asset["id"], "name": "带 Context 的节点"}
    ]


def test_agent_context_is_compiled_only_into_openhands_system_suffix(settings):
    context = FrozenSessionCapability(
        version_id="context-version",
        capability_type="CONTEXT",
        capability_key="product-context",
        digest="a" * 64,
        runtime_config={"schema_version": 1, "text": "固定的产品背景"},
    )
    with settings_context(settings):
        spec = build_agent_spec(
            FrozenSessionConfig(None, None, None, None, (context,)),
            provider=None,
            binding_id="context-binding",
            working_directory="/runtime/workspace/project",
            host_root=settings.workspace_root / "context-test" / "host",
            runtime_root=Path("/runtime/capabilities/conversations/context-binding"),
        )
    suffix = spec.agent_context.system_message_suffix
    assert "[product-context]\n固定的产品背景" in suffix
    assert "已冻结 Context（仅作系统级会话背景）" in suffix
    assert spec.skills == ()
    assert spec.plugins == ()
    assert spec.mcp_servers == ()


def test_agent_context_is_allowed_only_during_conversation_creation(monkeypatch):
    published = SimpleNamespace(
        package=SimpleNamespace(capability_type="CONTEXT", capability_key="product-context"),
        version=SimpleNamespace(id="context-version", digest="a" * 64),
    )
    monkeypatch.setattr(conversations, "resolve_version", lambda *_args: published)

    with pytest.raises(
        conversations.DomainError,
        match="Agent 会话不支持该能力类型",
    ):
        conversations._validated_capabilities(None, ("context-version",))

    assert conversations._validated_capabilities(
        None,
        ("context-version",),
        allowed_types=conversations._CREATION_CAPABILITY_TYPES,
    ) == ((published, "CONTEXT"),)
