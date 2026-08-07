from __future__ import annotations

from dataclasses import replace

from flowweave.runtime.base import (
    RuntimeHandle,
    RuntimeMCP,
    RuntimeProvider,
    RuntimeSkill,
    StartAttemptRequest,
)
from flowweave.runtime.openhands import OpenHandsRuntime


def _request() -> StartAttemptRequest:
    return StartAttemptRequest(
        attempt_id="attempt-1",
        execution_key="attempt:attempt-1:start",
        node={
            "instance_key": "design",
            "asset": {
                "name": "方案生成",
                "executor": {
                    "startup_prompt": "生成技术方案",
                    "context_prompt": "保留证据",
                    "max_iterations": 20,
                },
                "outputs": [
                    {
                        "field_key": "design",
                        "data_type": "DOCUMENT",
                        "description": "技术方案",
                    }
                ],
            },
        },
        bindings=[
            {
                "field_key": "prd",
                "artifact": {"artifact_type": "DOCUMENT", "inline_content": "需求正文"},
            }
        ],
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
    assert "需求正文" in payload["initial_message"]["content"][0]["text"]
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
    assert payload["agent"]["agent_context"]["skills"][0]["content"].startswith("# Requirements")
    assert payload["agent"]["mcp_config"] == {
        "docs": {"url": "https://mcp.example.test", "transport": "http"}
    }
    initial_text = payload["initial_message"]["content"][0]["text"]
    assert "/workspaces/nodes/node-1/skills/requirements" in initial_text
    assert "MCP Servers" in initial_text


def test_openhands_human_conversation_uses_dynamic_capability_selection(settings, monkeypatch):
    runtime = OpenHandsRuntime(settings)
    captured: dict[str, object] = {}

    def fake_request(method: str, path: str, **kwargs: object) -> dict[str, object]:
        captured.update({"method": method, "path": path, **kwargs})
        return {"id": "collaboration-1", "leaf_event_id": "event-1"}

    monkeypatch.setattr(runtime, "_request", fake_request)
    runtime.create_conversation(replace(_request(), interaction_mode="COLLABORATION"))

    payload = captured["json"]
    assert isinstance(payload, dict)
    assert payload["initial_message"]["run"] is False
    initial_text = payload["initial_message"]["content"][0]["text"]
    assert "生成技术方案" not in initial_text
    assert "完成任务后，请调用 finish" not in initial_text
    assert "不预设默认 Skill" in initial_text
    assert "根据用户当前消息动态选择" in initial_text
    assert "需求正文" in initial_text
    assert runtime._contracts["collaboration-1"] == []


def test_openhands_normalizes_incremental_events_and_terminal_result(settings, monkeypatch):
    runtime = OpenHandsRuntime(settings)
    runtime._contracts["conversation-1"] = [
        {"field_key": "design", "artifact_type": "DOCUMENT", "description": ""}
    ]
    responses = iter(
        [
            {
                "items": [
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
                        "kind": "ActionEvent",
                        "id": "14",
                        "source": "agent",
                        "action": {
                            "kind": "FinishAction",
                            "message": (
                                '{"design":{"artifact_type":"DOCUMENT","content":"final design"}}'
                            ),
                        },
                    }
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
    assert terminal.result.outputs == {"design": ("DOCUMENT", "final design")}
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
