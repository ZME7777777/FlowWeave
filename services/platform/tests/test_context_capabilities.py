from __future__ import annotations

import base64
from pathlib import Path
from types import SimpleNamespace

import pytest

from flowweave.modules.agent_sessions.application import conversations
from flowweave.modules.agent_sessions.application.runtime_config import (
    AGENT_WORKSPACE_MAX_ITERATIONS,
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
    assert AGENT_WORKSPACE_MAX_ITERATIONS == 300
    assert spec.budgets.max_iterations == AGENT_WORKSPACE_MAX_ITERATIONS
    assert "首要目标是把用户任务做对" in suffix
    assert "OpenHands 内置 think 工具" in suffix
    assert "绝不能为了写进展而跳过、延迟、拆散或重排必要的检查" in suffix
    assert "在重要工具调用或关键执行阶段之前" in suffix
    assert "不要只复述工具、文件、命令、函数名或参数" in suffix


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


def test_agent_definition_is_allowed_only_during_conversation_creation(monkeypatch):
    published = SimpleNamespace(
        package=SimpleNamespace(capability_type="AGENT_DEFINITION", capability_key="reviewer"),
        version=SimpleNamespace(id="agent-version", digest="b" * 64),
    )
    monkeypatch.setattr(conversations, "resolve_version", lambda *_args: published)

    with pytest.raises(conversations.DomainError, match="Agent 会话不支持该能力类型"):
        conversations._validated_capabilities(None, ("agent-version",))

    assert conversations._validated_capabilities(
        None,
        ("agent-version",),
        allowed_types=conversations._CREATION_CAPABILITY_TYPES,
    ) == ((published, "AGENT_DEFINITION"),)


def test_agent_definition_is_compiled_only_into_new_conversation_spec(settings):
    definition = FrozenSessionCapability(
        version_id="agent-version",
        capability_type="AGENT_DEFINITION",
        capability_key="reviewer",
        digest="b" * 64,
        runtime_config={
            "name": "reviewer",
            "description": "Review a proposed change",
            "model": "inherit",
            "tools": ["terminal"],
            "skills": [],
            "system_prompt": "Review the change and report concrete findings.",
            "when_to_use_examples": ["review a patch"],
            "permission_mode": "never_confirm",
            "max_iteration_per_run": 20,
            "max_budget_per_run": 1.5,
            "condenser": {"kind": "NoOpCondenser"},
            "metadata": {},
        },
    )
    with settings_context(settings):
        spec = build_agent_spec(
            FrozenSessionConfig(None, None, None, None, (definition,)),
            provider=None,
            binding_id="agent-definition-binding",
            working_directory="/runtime/workspace/project",
            host_root=settings.workspace_root / "agent-definition-test" / "host",
            runtime_root=Path("/runtime/capabilities/conversations/agent-definition-binding"),
        )

    assert [(item.name, item.system_prompt) for item in spec.agent_definitions] == [
        ("reviewer", "Review the change and report concrete findings.")
    ]


def test_hook_is_compiled_only_into_new_conversation_spec(settings):
    hook = FrozenSessionCapability(
        version_id="hook-version",
        capability_type="HOOK",
        capability_key="terminal-review",
        digest="c" * 64,
        runtime_config={
            "hook_set_schema_version": 2,
            "openhands_version": "1.44.0",
            "source_commit": "9a24f6c8866f353042a57df0514ccc900e3a0691",
            "runtime_mutation": "FORBIDDEN",
            "execution_mode": "PROMPT",
            "event": "pre_tool_use",
            "matcher": "terminal",
            "pre_tool_use": [
                {
                    "matcher": "terminal",
                    "hooks": [
                        {
                            "type": "prompt",
                            "name": "flowweave/terminal-review",
                            "command": "",
                            "prompt": "Review the terminal action.",
                            "timeout": 30,
                        }
                    ],
                }
            ],
        },
    )
    with settings_context(settings):
        spec = build_agent_spec(
            FrozenSessionConfig(None, None, None, None, (hook,)),
            provider=None,
            binding_id="hook-binding",
            working_directory="/runtime/workspace/project",
            host_root=settings.workspace_root / "hook-test" / "host",
            runtime_root=Path("/runtime/capabilities/conversations/hook-binding"),
        )

    assert spec.hook_config == {
        "pre_tool_use": [
            {
                "matcher": "terminal",
                "hooks": [
                    {
                        "type": "prompt",
                        "name": "flowweave/terminal-review",
                        "command": "",
                        "prompt": "Review the terminal action.",
                        "timeout": 30,
                    }
                ],
            }
        ]
    }


def test_hook_is_allowed_only_during_conversation_creation(monkeypatch):
    published = SimpleNamespace(
        package=SimpleNamespace(capability_type="HOOK", capability_key="terminal-review"),
        version=SimpleNamespace(id="hook-version", digest="c" * 64),
    )
    monkeypatch.setattr(conversations, "resolve_version", lambda *_args: published)

    with pytest.raises(conversations.DomainError, match="Agent 会话不支持该能力类型"):
        conversations._validated_capabilities(None, ("hook-version",))

    assert conversations._validated_capabilities(
        None,
        ("hook-version",),
        allowed_types=conversations._CREATION_CAPABILITY_TYPES,
    ) == ((published, "HOOK"),)


def test_agent_capability_validation_has_no_flowweave_count_limit(monkeypatch):
    published = {
        f"capability-{index}": SimpleNamespace(
            package=SimpleNamespace(capability_type="SKILL", capability_key=f"skill-{index}"),
            version=SimpleNamespace(id=f"capability-{index}", digest=f"{index:064x}"),
        )
        for index in range(31)
    }
    monkeypatch.setattr(
        conversations, "resolve_version", lambda _db, version_id: published[version_id]
    )

    selected = conversations._validated_capabilities(None, tuple(published))

    assert len(selected) == 31
