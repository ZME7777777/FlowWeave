from __future__ import annotations

from dataclasses import replace

import pytest

from flowweave.runtime.base import (
    RuntimeHandle,
    RuntimeMCP,
    RuntimeProvider,
    RuntimeSkill,
    StartAttemptRequest,
)
from flowweave.runtime.openhands import OpenHandsRuntime
from flowweave.shared.errors import DomainError


def _request() -> StartAttemptRequest:
    return StartAttemptRequest(
        attempt_id="attempt-1",
        execution_key="attempt:attempt-1:start",
        node={
            "instance_key": "design",
            "asset": {
                "name": "方案生成",
                "inputs": [
                    {
                        "field_key": "prd",
                        "data_type": "URL",
                        "template_url": "https://example.feishu.cn/docx/prd-template",
                    }
                ],
                "executor": {
                    "startup_prompt": "生成技术方案",
                    "context_prompt": "保留证据",
                    "max_iterations": 20,
                },
                "outputs": [
                    {
                        "field_key": "design",
                        "data_type": "URL",
                        "description": "技术方案",
                        "template_url": "https://example.feishu.cn/docx/design-template",
                    }
                ],
            },
        },
        bindings=[
            {
                "field_key": "prd",
                "display_name": "需求文档",
                "description": "产品需求事实来源",
                "template_url": "https://example.feishu.cn/docx/prd-template",
                "artifact": {
                    "artifact_type": "URL",
                    "uri": "https://example.feishu.cn/docx/prd-input",
                },
            }
        ],
        output_targets={
            "design": {
                "root_url": "https://example.feishu.cn/drive/folder/root",
                "run_name": "Run 1",
                "template_url": "https://example.feishu.cn/docx/design-template",
                "title": "技术方案",
            }
        },
        workspace_ref="./test-workspaces/run-1/node-1/1",
        provider=RuntimeProvider(
            provider_id="provider-1",
            base_url="http://host.docker.internal:1234/v1",
            model="gpt-5.6-sol",
            api_key="configured-secret",
        ),
        skills=(
            RuntimeSkill(
                name="requirements",
                content="# Requirements\nAnalyze the requirement.",
                description="Requirement analysis",
                source="requirements/SKILL.md",
                workspace_path="/workspaces/nodes/node-1/skills/requirements",
            ),
        ),
        node_workspace_ref="/workspaces/nodes/node-1",
        mcp_servers=(
            RuntimeMCP(
                name="docs",
                config={"url": "https://mcp.example.test", "transport": "http"},
                workspace_path="/workspaces/nodes/node-1/mcp/docs",
            ),
        ),
    )


def test_openhands_starts_real_agent_with_selected_provider_and_skill(settings, monkeypatch):
    runtime = OpenHandsRuntime(settings)
    captured: dict[str, object] = {}

    def fake_request(method: str, path: str, **kwargs: object) -> dict[str, object]:
        captured.update({"method": method, "path": path, **kwargs})
        return {"id": "conversation-1", "leaf_event_id": "event-1"}

    monkeypatch.setattr(runtime, "_request", fake_request)
    handle = runtime.start(_request())

    assert handle == RuntimeHandle("conversation-1", "conversation-1", "event-1")
    payload = captured["json"]
    assert isinstance(payload, dict)
    assert payload["workspace"] == {
        "kind": "LocalWorkspace",
        "working_dir": "/workspaces/run-1/node-1/1",
    }
    assert payload["initial_message"]["run"] is True
    initial_text = payload["initial_message"]["content"][0]["text"]
    assert initial_text == "生成技术方案"
    system_context = payload["agent"]["agent_context"]["system_message_suffix"]
    assert "https://example.feishu.cn/docx/prd-input" in system_context
    assert "https://example.feishu.cn/docx/prd-template" in system_context
    assert "实际读取的飞书文档" in system_context
    assert "https://example.feishu.cn/drive/folder/root" in system_context
    assert "Run 1" in system_context
    assert "平台不会持有或注入飞书账号凭据" in system_context
    assert "不得把 token、cookie 或本地凭据文件写入消息" in system_context
    assert payload["agent"]["llm"] == {
        "model": "openai/gpt-5.6-sol",
        "base_url": "http://host.docker.internal:1234/v1",
        "api_key": "configured-secret",
        "usage_id": "flowweave:provider-1",
    }
    assert [tool["name"] for tool in payload["agent"]["tools"]] == [
        "terminal",
        "file_editor",
        "task_tracker",
    ]
    assert "tool_module_qualnames" not in payload
    assert payload["agent"]["agent_context"]["skills"][0]["content"].startswith("# Requirements")
    assert payload["agent"]["mcp_config"] == {
        "docs": {"url": "https://mcp.example.test", "transport": "http"}
    }
    assert "/workspaces/nodes/node-1/skills/requirements" in system_context
    assert "MCP Servers" in system_context


def test_openhands_routes_control_plane_runtime_without_owning_cleanup(settings, monkeypatch):
    runtime = OpenHandsRuntime(settings.model_copy(update={"sandbox_manager_scope": "test-scope"}))
    requests: list[tuple[str, str | None]] = []

    def fake_request(
        method: str, path: str, *, base_url: str | None = None, **kwargs: object
    ) -> dict[str, object]:
        del kwargs
        requests.append((path, base_url))
        if path == "/api/conversations":
            return {"id": "conversation-env", "leaf_event_id": "event-1"}
        return {
            "items": [
                {
                    "kind": "MessageEvent",
                    "id": "event-1",
                    "source": "user",
                    "llm_message": {"content": [{"type": "text", "text": "start"}]},
                },
                {
                    "kind": "ActionEvent",
                    "id": "event-2",
                    "action": {"kind": "FinishAction", "message": "done"},
                },
            ]
        }

    monkeypatch.setattr(runtime, "_request", fake_request)
    handle = runtime.start(
        replace(
            _request(),
            environment_image="sha256:" + "a" * 64,
            runtime_sandbox_id="sandbox-1",
            runtime_resource_name="runtime-container-1",
            runtime_base_url="http://runtime-container-1:8000",
        )
    )

    assert handle.job_id == "env-exec:runtime-container-1"
    batch = runtime.read_events(handle)
    assert batch.result is not None and batch.result.status == "COMPLETED"
    assert requests == [
        ("/api/conversations", "http://runtime-container-1:8000"),
        (
            "/api/conversations/conversation-env/events/search",
            "http://runtime-container-1:8000",
        ),
    ]


def test_openhands_rejects_environment_without_control_plane_allocation(settings):
    runtime = OpenHandsRuntime(settings)

    with pytest.raises(DomainError) as caught:
        runtime.start(replace(_request(), environment_image="sha256:" + "c" * 64))

    assert caught.value.code == "RUNTIME_SANDBOX_REQUIRED"


def test_openhands_does_not_own_cleanup_when_create_response_has_no_conversation_id(
    settings, monkeypatch
):
    runtime = OpenHandsRuntime(settings.model_copy(update={"sandbox_manager_scope": "test-scope"}))
    monkeypatch.setattr(runtime, "_request", lambda *args, **kwargs: {})

    with pytest.raises(DomainError, match="Missing conversation id"):
        runtime.start(
            replace(
                _request(),
                environment_image="sha256:" + "c" * 64,
                runtime_sandbox_id="sandbox-invalid",
                runtime_resource_name="runtime-invalid-1",
                runtime_base_url="http://runtime-invalid-1:8000",
            )
        )


def test_openhands_environment_cancel_only_interrupts_agent(settings, monkeypatch):
    runtime = OpenHandsRuntime(settings.model_copy(update={"sandbox_manager_scope": "test-scope"}))
    requests: list[str] = []

    def fake_request(
        method: str, path: str, *, base_url: str | None = None, **kwargs: object
    ) -> dict[str, object]:
        del method, base_url, kwargs
        requests.append(path)
        if path == "/api/conversations":
            return {"id": "conversation-chat", "leaf_event_id": "event-1"}
        if path.endswith("/interrupt"):
            return {}
        return {"execution_status": "idle"}

    monkeypatch.setattr(runtime, "_request", fake_request)
    handle = runtime.create_conversation(
        replace(
            _request(),
            environment_image="sha256:" + "b" * 64,
            runtime_sandbox_id="sandbox-chat",
            runtime_resource_name="runtime-chat-1",
            runtime_base_url="http://runtime-chat-1:8000",
        )
    )
    assert handle.job_id == "env-chat:runtime-chat-1"

    runtime.cancel(handle)
    assert requests == [
        "/api/conversations",
        "/api/conversations/conversation-chat/interrupt",
        "/api/conversations/conversation-chat",
    ]


def test_openhands_human_conversation_uses_dynamic_capability_selection(settings, monkeypatch):
    runtime = OpenHandsRuntime(settings)
    requests: list[dict[str, object]] = []

    def fake_request(method: str, path: str, **kwargs: object) -> dict[str, object]:
        requests.append({"method": method, "path": path, **kwargs})
        return {"id": "collaboration-1" if path == "/api/conversations" else "user-1"}

    monkeypatch.setattr(runtime, "_request", fake_request)
    handle = runtime.create_conversation(replace(_request(), interaction_mode="COLLABORATION"))
    runtime.send_message(handle, "你好")

    payload = requests[0]["json"]
    assert isinstance(payload, dict)
    assert "initial_message" not in payload
    system_context = payload["agent"]["agent_context"]["system_message_suffix"]
    assert "节点预置说明（仅作协作背景" in system_context
    assert "生成技术方案" in system_context
    assert "完成任务后，请调用 finish" not in system_context
    assert "这些 Skill 与 MCP 是可选能力" in system_context
    assert "根据用户当前消息动态选择" in system_context
    assert "https://example.feishu.cn/docx/prd-input" in system_context
    assert requests[1]["path"] == "/api/conversations/collaboration-1/events"
    assert requests[1]["json"] == {
        "role": "user",
        "content": [{"type": "text", "text": "你好"}],
        "run": True,
    }
    assert runtime._contracts["collaboration-1"] == []


@pytest.mark.parametrize(
    ("uri", "accepted"),
    (
        ("https://example.feishu.cn/docx/output", True),
        ("https://example.larksuite.com/docx/output", True),
        ("https://example.larkoffice.com/docx/output", True),
        ("http://example.feishu.cn/docx/output", False),
        ("https://feishu.cn.attacker.example/docx/output", False),
        ("https://example.com/docx/output", False),
        ("javascript:alert(1)", False),
    ),
)
def test_openhands_accepts_only_declared_official_https_output_urls(settings, uri, accepted):
    runtime = OpenHandsRuntime(settings)
    runtime._contracts["conversation-1"] = [{"field_key": "design"}]

    outputs = runtime._outputs(
        "conversation-1",
        '{"outputs": {"design": {"uri": ' + repr(uri).replace("'", '"') + "}, "
        '"undeclared": "https://example.feishu.cn/docx/other"}}',
    )

    expected = {"design": ("URL", uri)} if accepted else {}
    assert outputs == expected


def test_openhands_normalizes_incremental_events_and_terminal_result(settings, monkeypatch):
    runtime = OpenHandsRuntime(settings)
    runtime._contracts["conversation-1"] = [
        {
            "field_key": "design",
            "artifact_type": "URL",
            "url": "https://example.feishu.cn/docx/design-output",
            "title": "技术方案",
        }
    ]
    responses = iter(
        [
            {
                "items": [
                    {
                        "kind": "ActionEvent",
                        "id": "10",
                        "source": "agent",
                        "action": {"kind": "FinishAction", "message": "old result"},
                    },
                    {
                        "kind": "MessageEvent",
                        "id": "11",
                        "source": "agent",
                        "llm_message": {
                            "role": "assistant",
                            "content": [{"type": "text", "text": "working"}],
                        },
                    },
                    {
                        "kind": "ActionEvent",
                        "id": "12",
                        "source": "agent",
                        "action": {"kind": "ThinkAction", "thought": "search"},
                    },
                    {"kind": "FutureEvent", "id": "13", "source": "environment"},
                ]
            },
            {
                "items": [
                    {
                        "kind": "FutureEvent",
                        "id": "13",
                        "source": "environment",
                    },
                    {
                        "kind": "ActionEvent",
                        "id": "14",
                        "source": "agent",
                        "action": {
                            "kind": "FinishAction",
                            "message": (
                                '{"outputs":{"design":{"artifact_type":"URL",'
                                '"uri":"https://example.feishu.cn/docx/design-output"}}}'
                            ),
                        },
                    },
                ]
            },
        ]
    )
    requests: list[tuple[str, str, object]] = []

    def fake_request(method: str, path: str, **kwargs: object) -> dict[str, object]:
        requests.append((method, path, kwargs.get("params")))
        return next(responses)

    monkeypatch.setattr(runtime, "_request", fake_request)
    handle = RuntimeHandle("conversation-1", "conversation-1", "10")

    running = runtime.read_events(handle)
    terminal = runtime.read_events(
        RuntimeHandle("conversation-1", "conversation-1", running.cursor)
    )

    assert [event.event_type for event in running.events] == ["MESSAGE", "THOUGHT", "STATE"]
    assert [event.cursor for event in running.events] == ["11", "12", "13"]
    assert running.events[2].payload["source_type"] == "FutureEvent"
    assert running.events[1].payload["event_name"] == "ThinkAction"
    assert running.events[1].payload["content"] == "search"
    assert running.cursor == "13"
    assert running.result is None
    assert terminal.cursor == "14"
    assert terminal.events[0].event_type == "COMPLETED"
    assert terminal.result is not None
    assert terminal.result.status == "COMPLETED"
    assert terminal.result.outputs == {
        "design": ("URL", "https://example.feishu.cn/docx/design-output")
    }
    assert requests == [
        (
            "GET",
            "/api/conversations/conversation-1/events/search",
            {"limit": 100, "sort_order": "TIMESTAMP", "page_id": "10"},
        ),
        (
            "GET",
            "/api/conversations/conversation-1/events/search",
            {"limit": 100, "sort_order": "TIMESTAMP", "page_id": "13"},
        ),
    ]


def test_openhands_does_not_replay_cursor_finish_as_next_turn_result(settings, monkeypatch):
    runtime = OpenHandsRuntime(settings)
    responses = iter(
        [
            {
                "items": [
                    {
                        "kind": "ActionEvent",
                        "id": "old-finish",
                        "source": "agent",
                        "action": {"kind": "FinishAction", "message": "authorize again"},
                    }
                ]
            },
            {
                "execution_status": "finished",
                "leaf_event_id": "old-finish",
                "last_user_message_id": "new-user",
            },
            {
                "items": [
                    {
                        "kind": "MessageEvent",
                        "id": "new-user",
                        "source": "user",
                        "llm_message": {"role": "user", "content": "hello"},
                    }
                ]
            },
        ]
    )

    monkeypatch.setattr(runtime, "_request", lambda *_args, **_kwargs: next(responses))
    handle = RuntimeHandle("job-1", "conversation-1", "old-finish")

    batch = runtime.read_events(handle)
    inspected = runtime.inspect(handle)

    assert batch.events == ()
    assert batch.result is None
    assert batch.cursor == "old-finish"
    assert inspected.status == "RUNNING"
    assert inspected.final_message is None


def test_openhands_finished_turn_uses_assistant_message_after_latest_user(settings, monkeypatch):
    runtime = OpenHandsRuntime(settings)
    responses = iter(
        [
            {
                "execution_status": "finished",
                "leaf_event_id": "state-finished",
                "last_user_message_id": "user-current",
            },
            {
                "items": [
                    {
                        "kind": "MessageEvent",
                        "id": "user-current",
                        "source": "user",
                        "llm_message": {"role": "user", "content": "你好"},
                    },
                    {
                        "kind": "MessageEvent",
                        "id": "assistant-current",
                        "source": "agent",
                        "llm_message": {
                            "role": "assistant",
                            "content": [{"type": "text", "text": "你好！"}],
                        },
                    },
                    {
                        "kind": "ConversationStateUpdateEvent",
                        "id": "state-finished",
                        "source": "environment",
                        "key": "execution_status",
                        "value": "finished",
                    },
                ]
            },
        ]
    )
    monkeypatch.setattr(runtime, "_request", lambda *_args, **_kwargs: next(responses))

    result = runtime.inspect(RuntimeHandle("job-1", "conversation-1", "old-finish"))

    assert result.status == "COMPLETED"
    assert result.final_message == "你好！"
    assert result.cursor == "state-finished"


def test_openhands_resume_interrupts_the_active_turn_before_steering(settings, monkeypatch):
    runtime = OpenHandsRuntime(settings)
    requests: list[tuple[str, str, object]] = []

    def fake_request(method: str, path: str, **kwargs: object) -> dict[str, object]:
        requests.append((method, path, kwargs.get("json")))
        return {}

    monkeypatch.setattr(runtime, "_request", fake_request)
    result = runtime.resume(
        RuntimeHandle("job-1", "conversation-1", "cursor-1"),
        "新的约束",
        ("data:image/png;base64,aW1hZ2U=",),
    )

    assert result.status == "RUNNING"
    assert requests == [
        ("POST", "/api/conversations/conversation-1/interrupt", {}),
        (
            "POST",
            "/api/conversations/conversation-1/events",
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": "新的约束"},
                    {"type": "image", "image_urls": ["data:image/png;base64,aW1hZ2U="]},
                ],
                "run": True,
            },
        ),
        ("GET", "/api/conversations/conversation-1", None),
    ]


def test_openhands_send_message_advances_cursor_to_user_event(settings, monkeypatch):
    runtime = OpenHandsRuntime(settings)

    def fake_request(_method: str, _path: str, **_kwargs: object) -> dict[str, object]:
        return {"id": "user-event-2"}

    monkeypatch.setattr(runtime, "_request", fake_request)
    result = runtime.send_message(
        RuntimeHandle("job-1", "conversation-1", "initial-event-1"),
        "读取当前输入",
    )

    assert result.status == "RUNNING"
    assert result.cursor == "user-event-2"


def test_openhands_send_message_reads_user_anchor_when_endpoint_returns_success(
    settings, monkeypatch
):
    runtime = OpenHandsRuntime(settings)
    responses = iter(
        [
            {"success": True},
            {"last_user_message_id": "user-event-current", "leaf_event_id": "state-running"},
        ]
    )
    requests: list[tuple[str, str]] = []

    def fake_request(method: str, path: str, **_kwargs: object) -> dict[str, object]:
        requests.append((method, path))
        return next(responses)

    monkeypatch.setattr(runtime, "_request", fake_request)
    result = runtime.send_message(RuntimeHandle("job-1", "conversation-1", "old-finish"), "你好")

    assert result.status == "RUNNING"
    assert result.cursor == "user-event-current"
    assert requests == [
        ("POST", "/api/conversations/conversation-1/events"),
        ("GET", "/api/conversations/conversation-1"),
    ]


def test_openhands_cancel_waits_until_agent_is_no_longer_running(settings, monkeypatch):
    runtime = OpenHandsRuntime(settings)
    responses = iter([{"ok": True}, {"execution_status": "running"}, {"execution_status": "idle"}])
    requests: list[tuple[str, str, bool]] = []

    def fake_request(method: str, path: str, **kwargs: object) -> dict[str, object]:
        requests.append((method, path, bool(kwargs.get("missing_ok"))))
        return next(responses)

    monkeypatch.setattr(runtime, "_request", fake_request)
    runtime.cancel(RuntimeHandle("job-1", "conversation-1", "cursor-1"))

    assert requests == [
        ("POST", "/api/conversations/conversation-1/interrupt", True),
        ("GET", "/api/conversations/conversation-1", True),
        ("GET", "/api/conversations/conversation-1", True),
    ]


def test_openhands_cancel_treats_missing_conversation_as_already_stopped(settings, monkeypatch):
    runtime = OpenHandsRuntime(settings)

    def fake_request(_method: str, _path: str, **_kwargs: object) -> dict[str, object]:
        return {"_flowweave_missing": True}

    monkeypatch.setattr(runtime, "_request", fake_request)
    runtime.cancel(RuntimeHandle("job-1", "missing-conversation", None))
