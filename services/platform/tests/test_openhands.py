from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path

import pytest

from flowweave.bootstrap.settings import Settings
from flowweave.runtime import openhands as openhands_module
from flowweave.runtime.auth import derive_runtime_session_key
from flowweave.runtime.base import (
    RuntimeAgentContext,
    RuntimeAgentDefinition,
    RuntimeAgentProfile,
    RuntimeAgentSpec,
    RuntimeBudgets,
    RuntimeCondenser,
    RuntimeCritic,
    RuntimeHandle,
    RuntimeMCP,
    RuntimeMCPOAuthCallbackRequest,
    RuntimeMCPOAuthJobRequest,
    RuntimeMCPOAuthStartRequest,
    RuntimeMCPProbeRequest,
    RuntimeMCPToolCall,
    RuntimePlugin,
    RuntimePluginValidationRequest,
    RuntimeProvider,
    RuntimeResult,
    RuntimeSkill,
    RuntimeTool,
    StartAttemptRequest,
)
from flowweave.runtime.openhands import OpenHandsRuntime
from flowweave.runtime.request import build_runtime_request
from flowweave.shared.errors import DomainError
from flowweave.shared.infrastructure.docker_controller import DockerControllerClient


@pytest.fixture(autouse=True)
def database():
    """Override the suite-wide PostgreSQL fixture for adapter-only tests."""

    yield


@pytest.fixture(autouse=True)
def runtime_contract_negotiation(monkeypatch: pytest.MonkeyPatch) -> None:
    """Keep legacy payload tests focused on their original protocol slice."""

    monkeypatch.setattr(
        OpenHandsRuntime, "_negotiate_runtime_contract", lambda *args, **kwargs: None
    )


@pytest.fixture
def openhands_settings(tmp_path) -> Settings:
    """OpenHands adapter tests do not require PostgreSQL or Docker."""

    return Settings(
        workspace_root=Path("./test-workspaces"),
        artifact_root=tmp_path / "artifacts",
        runtime_adapter="openhands",
        sandbox_manager_scope="flowweave-test",
    )


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
        agent_spec=RuntimeAgentSpec(
            provider=RuntimeProvider(
                provider_id="provider-1",
                base_url="http://host.docker.internal:1234/v1",
                model="gpt-5.6-sol",
                api_key="configured-secret",
            ),
            tools=(
                RuntimeTool(name="terminal"),
                RuntimeTool(name="file_editor"),
                RuntimeTool(name="task_tracker"),
            ),
            agent_context=RuntimeAgentContext(
                system_message_suffix="Frozen organization policy",
                user_message_suffix="Use the governed capabilities only.",
                disabled_skills=("unreviewed-skill",),
            ),
            skills=(
                RuntimeSkill(
                    name="requirements",
                    content="# Requirements\nAnalyze the requirement.",
                    description="Requirement analysis",
                    source="requirements/SKILL.md",
                    workspace_path="/workspaces/nodes/node-1/skills/requirements",
                    activation_keywords=("$requirements",),
                    disable_model_invocation=True,
                ),
            ),
            mcp_servers=(
                RuntimeMCP(
                    name="docs",
                    config={"url": "https://mcp.example.test", "transport": "http"},
                    workspace_path="/workspaces/nodes/node-1/mcp/docs",
                ),
            ),
            hook_config={
                "pre_tool_use": [
                    {
                        "matcher": "terminal",
                        "hooks": [
                            {
                                "type": "command",
                                "command": "flowweave-policy-check",
                                "timeout": 30,
                            }
                        ],
                    }
                ]
            },
            budgets=RuntimeBudgets(max_iterations=20),
        ),
        node_workspace_ref="/workspaces/nodes/node-1",
        environment_image="sha256:" + "2" * 64,
        environment_id="environment-1",
        environment_version_id="environment-version-1",
        environment_version_no=1,
        runtime_workspace_relative="workspace/project",
        runtime_working_dir_relative="",
        runtime_sandbox_id="runtime-1",
        runtime_resource_name="fw-sbx-flow-run-1",
        runtime_base_url="http://runtime.test:8000",
    )


def test_collaboration_request_drops_all_node_execution_business_context():
    shared_spec = RuntimeAgentSpec(tools=(RuntimeTool(name="terminal"),))

    request = build_runtime_request(
        None,  # type: ignore[arg-type]
        flow_run_id="flow-run-1",
        runtime_manifest_hash="manifest-1",
        attempt_id="attempt-1",
        execution_key="conversation:create",
        node=_request().node,
        bindings=_request().bindings,
        workspace_ref="/runtime/workspace/project",
        interaction_mode="COLLABORATION",
        startup_prompt="must not leak",
        startup_capability_key="node-skill",
        semantic_history=({"role": "user", "content": "must not leak"},),
        output_targets=_request().output_targets,
        environment_image="sha256:" + "1" * 64,
        environment_id="environment-1",
        environment_version_id="environment-version-1",
        environment_version_no=1,
        agent_spec=shared_spec,
        conversation_id="10000000-0000-4000-8000-000000000004",
    )

    assert request.node == {}
    assert request.bindings == []
    assert request.workspace_ref == "/runtime/workspace/project"
    assert request.node_workspace_ref == ""
    assert request.startup_prompt is None
    assert request.startup_capability_key is None
    assert request.semantic_history == ()
    assert request.output_targets == {}
    assert request.agent_spec is shared_spec


def _handle(cursor: str | None = None) -> RuntimeHandle:
    return RuntimeHandle(
        "env-exec:fw-sbx-flow-run-1",
        "10000000-0000-4000-8000-000000000002",
        cursor,
        "runtime-1",
        "fw-sbx-flow-run-1",
    )


@pytest.mark.parametrize(
    ("execution_status", "ready"),
    [("running", False), ("waiting_for_confirmation", False), ("paused", True), ("idle", True)],
)
def test_openhands_input_readiness_returns_atomic_native_execution_state(
    openhands_settings, monkeypatch, execution_status, ready
):
    runtime = OpenHandsRuntime(openhands_settings)
    monkeypatch.setattr(
        runtime,
        "_conversation_state",
        lambda _handle: {"execution_status": execution_status},
    )

    snapshot = runtime.input_readiness(_handle())

    assert snapshot.ready is ready
    assert snapshot.execution_status == execution_status


def test_openhands_preserves_agent_workspace_selected_subdirectory(openhands_settings):
    runtime = OpenHandsRuntime(openhands_settings)
    request = replace(
        _request(),
        workspace_ref="/runtime/workspace/project/backend",
        runtime_sandbox_id="agent-runtime-1",
        runtime_resource_name="agent-workspace-runtime",
    )

    assert runtime._request_workspace_path(request) == "/runtime/workspace/project/backend"


def _state(**values: object) -> dict[str, object]:
    return {
        "id": "10000000-0000-4000-8000-000000000002",
        "workspace": {
            "kind": "LocalWorkspace",
            "working_dir": "/runtime/workspace/project",
        },
        "persistence_dir": "/runtime/state/conversations/10000000000040008000000000000002",
        **values,
    }


@pytest.mark.parametrize(
    "working_dir",
    [
        "/runtime/workspace/project",
        "/runtime/workspace/project/backend",
        "/runtime/workspace/nodes/asset/sessions/node-run/1",
    ],
)
def test_openhands_reload_accepts_formal_flow_run_workspace_roots(
    openhands_settings, monkeypatch, working_dir: str
):
    runtime = OpenHandsRuntime(openhands_settings)
    state = _state(workspace={"kind": "LocalWorkspace", "working_dir": working_dir})
    monkeypatch.setattr(runtime, "_request", lambda *_args, **_kwargs: state)

    assert runtime._conversation_state(_handle())["workspace"] == state["workspace"]


def test_openhands_reload_rejects_workspace_outside_flow_run_roots(openhands_settings, monkeypatch):
    runtime = OpenHandsRuntime(openhands_settings)
    state = _state(
        workspace={"kind": "LocalWorkspace", "working_dir": "/runtime/workspace/capabilities"}
    )
    monkeypatch.setattr(runtime, "_request", lambda *_args, **_kwargs: state)

    with pytest.raises(DomainError) as raised:
        runtime._conversation_state(_handle())

    assert raised.value.code == "RUNTIME_WORKSPACE_IDENTITY_DRIFT"


@pytest.mark.parametrize(
    ("max_iterations", "expected_refinement"),
    [
        (0, None),
        (2, {"success_threshold": 0.7, "max_iterations": 2}),
    ],
)
def test_openhands_serializes_native_critic_without_invalid_zero_iteration_refinement(
    openhands_settings,
    monkeypatch,
    max_iterations: int,
    expected_refinement: dict[str, object] | None,
):
    runtime = OpenHandsRuntime(openhands_settings)
    captured: dict[str, object] = {}

    def fake_request(method: str, path: str, **kwargs: object) -> dict[str, object]:
        captured.update({"method": method, "path": path, **kwargs})
        return {"id": "10000000-0000-4000-8000-000000000001", "leaf_event_id": "event-1"}

    monkeypatch.setattr(runtime, "_request", fake_request)
    baseline = _request()
    request = replace(
        baseline,
        agent_spec=replace(
            baseline.agent_spec,
            critic=RuntimeCritic(
                mode="finish_and_message",
                success_threshold=0.7,
                max_iterations=max_iterations,
            ),
        ),
    )

    runtime.start(request)

    payload = captured["json"]
    assert isinstance(payload, dict)
    critic = payload["agent"]["critic"]
    assert critic["kind"] == "AgentFinishedCritic"
    assert critic["mode"] == "finish_and_message"
    if expected_refinement is None:
        assert "iterative_refinement" not in critic
    else:
        assert critic["iterative_refinement"] == expected_refinement


def test_openhands_materializes_oracle_profile_with_frozen_binding(openhands_settings, monkeypatch):
    runtime = OpenHandsRuntime(openhands_settings)
    provider = RuntimeProvider(
        provider_id="provider-1",
        base_url="https://provider.example/v1",
        model="gpt-5.6-sol",
        api_key="rotatable-secret",
    )
    requests: list[tuple[str, str, dict[str, object]]] = []

    def fake_request(method: str, path: str, **kwargs: object) -> dict[str, object]:
        requests.append((method, path, kwargs))
        if method == "GET":
            return {"_flowweave_missing": True}
        return {"name": "oracle", "message": "saved"}

    monkeypatch.setattr(runtime, "_request", fake_request)

    runtime._ensure_oracle_profile(  # pyright: ignore[reportPrivateUsage]
        provider,
        base_url="http://runtime.test:8000",
        session_api_key="session-key",
    )

    assert [(method, path) for method, path, _ in requests] == [
        ("GET", "/api/profiles/oracle"),
        ("POST", "/api/profiles/oracle"),
    ]
    body = requests[1][2]["json"]
    assert isinstance(body, dict)
    assert body["include_secrets"] is True
    assert body["llm"]["model"] == "openai/gpt-5.6-sol"
    assert body["llm"]["api_key"] == "rotatable-secret"
    assert body["llm"]["usage_id"].startswith("flowweave-oracle:provider-1:")


def test_openhands_refreshes_only_the_same_frozen_oracle_binding(openhands_settings, monkeypatch):
    runtime = OpenHandsRuntime(openhands_settings)
    provider = RuntimeProvider(
        provider_id="provider-1",
        base_url="https://provider.example/v1",
        model="gpt-5.6-sol",
        api_key="rotated-secret",
    )
    binding_id = runtime._oracle_binding_id(  # pyright: ignore[reportPrivateUsage]
        provider
    )
    methods: list[str] = []

    def fake_request(method: str, _path: str, **_kwargs: object) -> dict[str, object]:
        methods.append(method)
        if method == "GET":
            return {
                "name": "oracle",
                "config": {
                    "model": "openai/gpt-5.6-sol",
                    "usage_id": binding_id,
                },
                "api_key_set": True,
            }
        return {"name": "oracle", "message": "saved"}

    monkeypatch.setattr(runtime, "_request", fake_request)

    runtime._ensure_oracle_profile(  # pyright: ignore[reportPrivateUsage]
        provider, base_url="http://runtime.test:8000", session_api_key="session-key"
    )

    assert methods == ["GET", "POST"]


def test_openhands_keeps_existing_oracle_when_primary_provider_differs(
    openhands_settings, monkeypatch
):
    runtime = OpenHandsRuntime(openhands_settings)
    provider = RuntimeProvider(
        provider_id="provider-1",
        base_url="https://provider.example/v1",
        model="gpt-5.6-sol",
        api_key="secret",
    )
    methods: list[str] = []

    def fake_request(method: str, _path: str, **_kwargs: object) -> dict[str, object]:
        methods.append(method)
        return {
            "name": "oracle",
            "config": {
                "model": "openai/other-model",
                "usage_id": "flowweave-oracle:other-provider:deadbeef",
            },
            "api_key_set": True,
        }

    monkeypatch.setattr(runtime, "_request", fake_request)

    runtime._ensure_oracle_profile(  # pyright: ignore[reportPrivateUsage]
        provider,
        base_url="http://runtime.test:8000",
        session_api_key="session-key",
    )

    # ``oracle`` is a Runtime-wide OpenHands singleton. A Conversation using
    # another primary provider must neither overwrite it nor be blocked from
    # creation/switch_llm because it exists.
    assert methods == ["GET"]


def test_openhands_starts_real_agent_with_selected_provider_and_skill(
    openhands_settings, monkeypatch
):
    runtime = OpenHandsRuntime(openhands_settings)
    captured: dict[str, object] = {}

    def fake_request(method: str, path: str, **kwargs: object) -> dict[str, object]:
        captured.update({"method": method, "path": path, **kwargs})
        return {"id": "10000000-0000-4000-8000-000000000002", "leaf_event_id": "event-1"}

    monkeypatch.setattr(runtime, "_request", fake_request)
    handle = runtime.start(_request())

    assert handle == RuntimeHandle(
        "env-exec:fw-sbx-flow-run-1",
        "10000000-0000-4000-8000-000000000002",
        "event-1",
        "runtime-1",
        "fw-sbx-flow-run-1",
    )
    payload = captured["json"]
    assert isinstance(payload, dict)
    assert payload["workspace"] == {
        "kind": "LocalWorkspace",
        "working_dir": "/workspaces/run-1/node-1/1",
    }
    assert payload["initial_message"]["run"] is True
    assert payload["confirmation_policy"] == {"kind": "NeverConfirm"}
    assert payload["agent"]["condenser"] == {"kind": "NoOpCondenser"}
    assert payload["agent"]["tool_concurrency_limit"] == 1
    initial_text = payload["initial_message"]["content"][0]["text"]
    assert initial_text.startswith("生成技术方案\n\n本次节点输入：")
    assert "https://example.feishu.cn/docx/prd-input" in initial_text
    system_context = payload["agent"]["agent_context"]["system_message_suffix"]
    assert system_context.startswith("Frozen organization policy\n\n")
    assert payload["agent"]["agent_context"] | {"skills": []} == {
        "skills": [],
        "system_message_suffix": system_context,
        "user_message_suffix": "Use the governed capabilities only.",
        "load_user_skills": False,
        "load_public_skills": False,
        "marketplace_path": None,
        "registered_marketplaces": [],
        "load_project_skills": False,
        "load_memory": False,
        "disabled_skills": ["unreviewed-skill"],
    }
    assert "https://example.feishu.cn/docx/prd-input" in system_context
    assert "https://example.feishu.cn/docx/prd-template" not in system_context
    assert "流程输入" in system_context
    assert "Run 1" in system_context
    assert "URL 输出返回安全 HTTP(S) uri" in system_context
    assert "不得写入 token、cookie 或凭据" in system_context
    assert payload["agent"]["llm"] == {
        "model": "openai/gpt-5.6-sol",
        "base_url": "http://host.docker.internal:1234/v1",
        "api_key": "configured-secret",
        "usage_id": "flowweave:provider-1",
        "stream": True,
        "num_retries": 5,
        "retry_multiplier": 2.0,
        "retry_min_wait": 1,
        "retry_max_wait": 4,
        "timeout": None,
        "max_input_tokens": 922000,
    }


def test_openhands_initial_user_message_carries_file_and_image_inputs(
    openhands_settings, monkeypatch
):
    runtime = OpenHandsRuntime(openhands_settings)
    captured: dict[str, object] = {}

    def fake_request(method: str, path: str, **kwargs: object) -> dict[str, object]:
        captured.update({"method": method, "path": path, **kwargs})
        return {"id": "10000000-0000-4000-8000-000000000002", "leaf_event_id": "event-1"}

    monkeypatch.setattr(runtime, "_request", fake_request)
    request = replace(
        _request(),
        bindings=[
            {
                "field_key": "reference",
                "display_name": "参考图片",
                "artifact": {
                    "artifact_type": "FILE",
                    "runtime_path": "/runtime/workspace/project/uploads/input-image",
                    "mime_type": "image/png",
                    "metadata": {"filename": "reference.png"},
                },
            }
        ],
        input_attachments=(
            {
                "path": "/runtime/workspace/project/uploads/input-image",
                "filename": "reference.png",
                "mime_type": "image/png",
                "byte_size": 3,
                "image_data_url": "data:image/png;base64,UE5H",
            },
        ),
    )

    runtime.start(request)

    payload = captured["json"]
    assert isinstance(payload, dict)
    content = payload["initial_message"]["content"]
    system_context = payload["agent"]["agent_context"]["system_message_suffix"]
    assert "已附加文件：reference.png" in content[0]["text"]
    assert "/runtime/workspace/project/uploads/input-image" not in content[0]["text"]
    assert content[1] == {"type": "image", "image_urls": ["data:image/png;base64,UE5H"]}
    assert [tool["name"] for tool in payload["agent"]["tools"]] == [
        "terminal",
        "file_editor",
        "task_tracker",
    ]
    assert "tool_module_qualnames" not in payload
    assert payload["agent"]["agent_context"]["skills"][0]["content"].startswith("# Requirements")
    assert payload["agent"]["agent_context"]["skills"][0] | {"content": "<content>"} == {
        "name": "requirements",
        "content": "<content>",
        "description": "Requirement analysis",
        "source": "requirements/SKILL.md",
        "trigger": {"type": "keyword", "keywords": ["$requirements"]},
        "is_agentskills_format": True,
        "disable_model_invocation": True,
    }
    assert payload["agent"]["mcp_config"] == {
        "docs": {"url": "https://mcp.example.test", "transport": "http"}
    }
    assert payload["hook_config"] == {
        "pre_tool_use": [
            {
                "matcher": "terminal",
                "hooks": [
                    {
                        "type": "command",
                        "command": "flowweave-policy-check",
                        "timeout": 30,
                    }
                ],
            }
        ]
    }
    assert "/workspaces/nodes/node-1/skills/requirements" not in system_context
    assert "invoke_skill" in system_context
    assert "MCP Servers" in system_context


def test_openhands_probes_mcp_through_target_runtime_and_redacts_oauth_state(
    openhands_settings, monkeypatch
):
    runtime = OpenHandsRuntime(openhands_settings)
    captured: dict[str, object] = {}

    def fake_request(method: str, path: str, **kwargs: object) -> dict[str, object]:
        captured.update({"method": method, "path": path, **kwargs})
        return {
            "ok": True,
            "tools": ["lookup", "lookup", "status"],
            "tool_result": {"is_error": False, "text": "private result"},
        }

    monkeypatch.setattr(runtime, "_request", fake_request)
    result = runtime.probe_mcp(
        RuntimeMCPProbeRequest(
            server=RuntimeMCP(
                name="docs",
                config={
                    "transport": "streamable-http",
                    "url": "https://mcp.example.test/mcp",
                },
            ),
            base_url="http://fw-sbx-probe:8000",
            runtime_resource_name="fw-sbx-probe",
            timeout=12,
            read_only_tool_call=RuntimeMCPToolCall(name="status", arguments={"scope": "current"}),
        )
    )

    assert result.ok is True
    assert result.tools == ("lookup", "status")
    assert result.tool_call_is_error is False
    assert result.tool_call_text == "private result"
    assert captured["method"] == "POST"
    assert captured["path"] == "/api/mcp/test"
    assert captured["base_url"] == "http://fw-sbx-probe:8000"
    assert captured["session_api_key"] != openhands_settings.openhands_session_api_key
    assert captured["json"] == {
        "name": "docs",
        "server": {
            "url": "https://mcp.example.test/mcp",
            "type": "streamable-http",
        },
        "timeout": 12,
        "tool_call": {"name": "status", "arguments": {"scope": "current"}},
    }

    oauth_state = {
        "tokens": {
            "access_token": "encrypted-access-token",
            "refresh_token": "encrypted-refresh-token",
        }
    }
    monkeypatch.setattr(
        runtime,
        "_request",
        lambda method, path, **kwargs: (
            captured.update({"method": method, "path": path, **kwargs})
            or {"ok": True, "tools": ["lookup"], "oauth_state": oauth_state}
        ),
    )
    oauth_result = runtime.probe_mcp(
        RuntimeMCPProbeRequest(
            server=RuntimeMCP(
                name="oauth",
                config={
                    "transport": "http",
                    "url": "https://mcp.example.test/mcp",
                    "auth": {
                        "strategy": "oauth2",
                        "authentication": {"type": "oauth"},
                    },
                },
            ),
            base_url="http://fw-sbx-probe:8000",
            runtime_resource_name="fw-sbx-probe",
            oauth_secret_reference_id="secret-reference",
            oauth_secret_version=4,
            oauth_state=oauth_state,
        )
    )
    assert oauth_result.oauth_state == oauth_state
    assert captured["json"] == {
        "name": "oauth",
        "server": {
            "url": "https://mcp.example.test/mcp",
            "auth": {
                "strategy": "oauth2",
                "authentication": {"type": "oauth"},
                "state": oauth_state,
            },
            "type": "http",
        },
        "timeout": 15.0,
    }

    monkeypatch.setattr(
        runtime,
        "_request",
        lambda *_args, **_kwargs: {"ok": True, "tools": [], "oauth_state": {}},
    )
    with pytest.raises(DomainError) as raised:
        runtime.probe_mcp(
            RuntimeMCPProbeRequest(
                server=RuntimeMCP(
                    name="oauth",
                    config={"transport": "http", "url": "https://mcp.test"},
                ),
                base_url="http://fw-sbx-probe:8000",
                runtime_resource_name="fw-sbx-probe",
            )
        )
    assert raised.value.code == "MCP_OAUTH_LIFECYCLE_REQUIRED"


def test_openhands_classifies_explicit_mcp_initialization_timeout(openhands_settings, monkeypatch):
    runtime = OpenHandsRuntime(openhands_settings)
    response = openhands_module.httpx.Response(
        500,
        text="MCPTimeoutError: MCP tool listing timed out after 30 seconds",
        request=openhands_module.httpx.Request("POST", "http://runtime:8000/api/conversations"),
    )

    class FakeClient:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def request(self, *_args, **_kwargs):
            return response

    monkeypatch.setattr(openhands_module.httpx, "Client", lambda **_kwargs: FakeClient())
    with pytest.raises(DomainError) as raised:
        runtime._request(
            "POST",
            "/api/conversations",
            base_url="http://runtime:8000",
            session_api_key="session-key",
            json={},
        )
    assert raised.value.code == "MCP_INITIALIZATION_UNAVAILABLE"
    assert raised.value.details == {"error_kind": "timeout"}


def test_openhands_maps_formal_mcp_oauth_job_routes(openhands_settings, monkeypatch):
    runtime = OpenHandsRuntime(openhands_settings)
    calls: list[tuple[str, str, dict[str, object]]] = []
    responses = iter(
        [
            {
                "ok": True,
                "job_id": "native-oauth-job",
                "authorization_url": "https://identity.example.test/authorize",
            },
            {
                "ok": True,
                "status": "authorizing",
                "job_id": "native-oauth-job",
                "callback_ready": True,
            },
            {
                "ok": True,
                "status": "succeeded",
                "job_id": "native-oauth-job",
                "callback_ready": True,
                "tools": ["lookup", "lookup", "status"],
                "oauth_state": {"tokens": {"access_token": "runtime-secret"}},
            },
        ]
    )

    def fake_request(method: str, path: str, **kwargs: object) -> dict[str, object]:
        calls.append((method, path, kwargs))
        return next(responses)

    monkeypatch.setattr(runtime, "_request", fake_request)
    start = runtime.start_mcp_oauth(
        RuntimeMCPOAuthStartRequest(
            server=RuntimeMCP(
                name="oauth-docs",
                config={
                    "transport": "http",
                    "url": "https://mcp.example.test/mcp",
                    "auth": {
                        "strategy": "oauth2",
                        "authentication": {"type": "oauth"},
                    },
                },
            ),
            base_url="http://fw-sbx-oauth:8000",
            runtime_resource_name="fw-sbx-oauth",
            timeout=12,
        )
    )
    assert start.status == "authorizing"
    assert start.job_id == "native-oauth-job"
    assert calls[0][0:2] == ("POST", "/api/mcp/oauth/start")
    assert calls[0][2]["json"] == {
        "name": "oauth-docs",
        "server": {
            "url": "https://mcp.example.test/mcp",
            "auth": {
                "strategy": "oauth2",
                "authentication": {"type": "oauth"},
            },
            "type": "http",
        },
        "timeout": 12,
    }

    status = runtime.read_mcp_oauth(
        RuntimeMCPOAuthJobRequest(
            job_id="native-oauth-job",
            base_url="http://fw-sbx-oauth:8000",
            runtime_resource_name="fw-sbx-oauth",
        )
    )
    assert status.callback_ready is True
    assert calls[1][0:2] == (
        "GET",
        "/api/mcp/oauth/status/native-oauth-job",
    )

    callback_url = "http://localhost:54321/callback?code=private-code"
    completed = runtime.submit_mcp_oauth_callback(
        RuntimeMCPOAuthCallbackRequest(
            job_id="native-oauth-job",
            base_url="http://fw-sbx-oauth:8000",
            runtime_resource_name="fw-sbx-oauth",
            callback_url=callback_url,
        )
    )
    assert completed.status == "succeeded"
    assert completed.tools == ("lookup", "status")
    assert completed.oauth_state == {"tokens": {"access_token": "runtime-secret"}}
    assert calls[2][0:2] == (
        "POST",
        "/api/mcp/oauth/callback/native-oauth-job",
    )
    assert calls[2][2]["json"] == {"callback_url": callback_url}


def test_openhands_materializes_governed_profile_without_server_store_lookup(
    openhands_settings, monkeypatch
):
    runtime = OpenHandsRuntime(openhands_settings)
    captured: dict[str, object] = {}

    def fake_request(method: str, path: str, **kwargs: object) -> dict[str, object]:
        captured.update({"method": method, "path": path, **kwargs})
        return {"id": "10000000-0000-4000-8000-000000000009", "leaf_event_id": "event-1"}

    monkeypatch.setattr(runtime, "_request", fake_request)
    baseline = _request()
    profile = RuntimeAgentProfile(
        capability_version_id="62904a11-70aa-4a53-a8cb-43bcaf9a85f0",
        capability_key="governed-profile",
        digest="a" * 64,
        content_hash="b" * 64,
    )
    runtime.start(replace(baseline, agent_spec=replace(baseline.agent_spec, agent_profile=profile)))

    payload = captured["json"]
    assert isinstance(payload, dict)
    assert "agent" in payload
    assert "agent_profile_id" not in payload
    assert "agent_settings" not in payload
    assert payload["observability_metadata"] == {
        "flowweave.agent_profile_version_id": profile.capability_version_id,
        "flowweave.agent_profile_key": profile.capability_key,
        "flowweave.agent_profile_digest": profile.digest,
        "flowweave.agent_profile_schema_version": 2,
        "flowweave.agent_profile_source_id": None,
        "flowweave.agent_profile_source_revision": 0,
    }


def test_openhands_serializes_governed_agent_definitions(openhands_settings, monkeypatch):
    runtime = OpenHandsRuntime(openhands_settings)
    captured: dict[str, object] = {}

    def fake_request(method: str, path: str, **kwargs: object) -> dict[str, object]:
        captured.update({"method": method, "path": path, **kwargs})
        return {"id": "10000000-0000-4000-8000-000000000010", "leaf_event_id": "event-1"}

    monkeypatch.setattr(runtime, "_request", fake_request)
    baseline = _request()
    request = replace(
        baseline,
        agent_spec=replace(
            baseline.agent_spec,
            tools=(RuntimeTool(name="terminal"), RuntimeTool(name="task_tool_set")),
            agent_definitions=(
                RuntimeAgentDefinition(
                    name="reviewer",
                    description="Review a proposed change",
                    tools=("terminal",),
                    system_prompt="Review the change and report concrete findings.",
                    when_to_use_examples=("review a patch",),
                    permission_mode="never_confirm",
                    max_iteration_per_run=20,
                    max_budget_per_run=1.5,
                ),
            ),
        ),
    )

    runtime.start(request)

    payload = captured["json"]
    assert isinstance(payload, dict)
    assert payload["agent_definitions"] == [
        {
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
        }
    ]


def test_openhands_sends_frozen_plugins_and_leaves_ambient_discovery_native(
    openhands_settings, monkeypatch
):
    runtime = OpenHandsRuntime(openhands_settings)
    captured: dict[str, object] = {}

    def fake_request(method: str, path: str, **kwargs: object) -> dict[str, object]:
        captured.update({"method": method, "path": path, **kwargs})
        return {"id": "10000000-0000-4000-8000-000000000008", "leaf_event_id": "event-1"}

    monkeypatch.setattr(runtime, "_request", fake_request)
    baseline = _request()
    request = replace(
        baseline,
        agent_spec=replace(
            baseline.agent_spec,
            plugins=(
                RuntimePlugin(
                    name="governed-review",
                    source=(
                        "/runtime/capabilities/nodes/node-1/plugins/governed-review-version-id"
                    ),
                    content_hash="a" * 64,
                ),
            ),
        ),
    )

    runtime.start(request)

    payload = captured["json"]
    assert isinstance(payload, dict)
    assert payload["plugins"] == [
        {"source": ("/runtime/capabilities/nodes/node-1/plugins/governed-review-version-id")}
    ]
    assert "load_ambient_plugins" not in payload
    assert "ref" not in payload["plugins"][0]
    assert "repo_path" not in payload["plugins"][0]


def test_openhands_configures_codex_oauth_for_responses(openhands_settings, monkeypatch):
    runtime = OpenHandsRuntime(openhands_settings)
    captured: dict[str, object] = {}

    def fake_request(method: str, path: str, **kwargs: object) -> dict[str, object]:
        captured.update({"method": method, "path": path, **kwargs})
        return {"id": "10000000-0000-4000-8000-000000000004", "leaf_event_id": "event-1"}

    monkeypatch.setattr(runtime, "_request", fake_request)
    baseline = _request()
    request = replace(
        baseline,
        agent_spec=replace(
            baseline.agent_spec,
            provider=RuntimeProvider(
                provider_id="codex-oauth",
                base_url="https://chatgpt.com/backend-api/codex",
                model="gpt-5.6-sol",
                api_key="short-lived-access-token",
                auth_type="CODEX_OAUTH",
                extra_headers={
                    "originator": "codex_cli_rs",
                    "OpenAI-Beta": "responses=experimental",
                    "chatgpt-account-id": "account-123",
                },
                reasoning_effort="high",
            ),
        ),
    )
    runtime.start(request)

    payload = captured["json"]
    assert isinstance(payload, dict)
    llm = payload["agent"]["llm"]
    assert llm["model"] == "openai/gpt-5.6-sol"
    assert llm["base_url"] == "https://chatgpt.com/backend-api/codex"
    assert llm["api_key"] == "short-lived-access-token"
    assert llm["api_mode"] == "responses"
    assert llm["model_canonical_name"] == "openai/codex-auto-review"
    assert llm["stream"] is True
    assert llm["num_retries"] == 5
    assert llm["retry_multiplier"] == 2.0
    assert llm["retry_min_wait"] == 1
    assert llm["retry_max_wait"] == 4
    assert llm["timeout"] is None
    assert llm["litellm_extra_body"] == {
        "store": False,
        "reasoning": {"effort": "high"},
    }
    assert llm["extra_headers"]["chatgpt-account-id"] == "account-123"


def test_openhands_disables_native_autotitle_for_agent_workspace(openhands_settings, monkeypatch):
    runtime = OpenHandsRuntime(openhands_settings)
    captured: dict[str, object] = {}

    def fake_request(method: str, path: str, **kwargs: object) -> dict[str, object]:
        captured.update({"method": method, "path": path, **kwargs})
        return {"id": "10000000-0000-4000-8000-000000000004", "leaf_event_id": "event-1"}

    monkeypatch.setattr(runtime, "_request", fake_request)
    request = replace(
        _request(),
        execution_key="agent-workspace:workspace-1:conversation:binding-1",
    )
    runtime.create_conversation(request)

    payload = captured["json"]
    assert isinstance(payload, dict)
    assert payload["autotitle"] is False


def test_openhands_disables_native_autotitle_for_collaboration(openhands_settings, monkeypatch):
    runtime = OpenHandsRuntime(openhands_settings)
    captured: dict[str, object] = {}

    def fake_request(method: str, path: str, **kwargs: object) -> dict[str, object]:
        captured.update({"method": method, "path": path, **kwargs})
        return {"id": "10000000-0000-4000-8000-000000000004", "leaf_event_id": "event-1"}

    monkeypatch.setattr(runtime, "_request", fake_request)
    runtime.create_conversation(replace(_request(), interaction_mode="COLLABORATION"))

    payload = captured["json"]
    assert isinstance(payload, dict)
    assert payload["autotitle"] is False


def test_openhands_routes_control_plane_runtime_without_owning_cleanup(
    openhands_settings, monkeypatch
):
    runtime = OpenHandsRuntime(
        openhands_settings.model_copy(update={"sandbox_manager_scope": "test-scope"})
    )
    requests: list[tuple[str, str | None]] = []

    def fake_request(
        method: str, path: str, *, base_url: str | None = None, **kwargs: object
    ) -> dict[str, object]:
        del kwargs
        requests.append((path, base_url))
        if path == "/api/conversations":
            return {"id": "10000000-0000-4000-8000-000000000006", "leaf_event_id": "event-1"}
        if path == "/api/conversations/10000000-0000-4000-8000-000000000006":
            return {
                "id": "10000000-0000-4000-8000-000000000006",
                "leaf_event_id": "event-2",
                "workspace": {
                    "kind": "LocalWorkspace",
                    "working_dir": "/runtime/workspace/project",
                },
                "persistence_dir": "/runtime/state/conversations/10000000000040008000000000000006",
            }
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
            "/api/conversations/10000000-0000-4000-8000-000000000006/events/search",
            "http://runtime-container-1:8000",
        ),
        (
            "/api/conversations/10000000-0000-4000-8000-000000000006",
            "http://runtime-container-1:8000",
        ),
    ]


def test_openhands_rejects_environment_without_control_plane_allocation(openhands_settings):
    runtime = OpenHandsRuntime(openhands_settings)

    with pytest.raises(DomainError) as caught:
        runtime.start(
            replace(
                _request(),
                environment_image="sha256:" + "c" * 64,
                runtime_sandbox_id="",
                runtime_resource_name="",
                runtime_base_url="",
            )
        )

    assert caught.value.code == "RUNTIME_SANDBOX_REQUIRED"


def test_openhands_does_not_own_cleanup_when_create_response_has_no_conversation_id(
    openhands_settings, monkeypatch
):
    runtime = OpenHandsRuntime(
        openhands_settings.model_copy(update={"sandbox_manager_scope": "test-scope"})
    )
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


def test_openhands_environment_cancel_only_interrupts_agent(openhands_settings, monkeypatch):
    runtime = OpenHandsRuntime(
        openhands_settings.model_copy(update={"sandbox_manager_scope": "test-scope"})
    )
    requests: list[str] = []

    def fake_request(
        method: str, path: str, *, base_url: str | None = None, **kwargs: object
    ) -> dict[str, object]:
        del method, base_url, kwargs
        requests.append(path)
        if path == "/api/conversations":
            return {"id": "10000000-0000-4000-8000-000000000003", "leaf_event_id": "event-1"}
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
        "/api/conversations/10000000-0000-4000-8000-000000000003/interrupt",
        "/api/conversations/10000000-0000-4000-8000-000000000003",
    ]


def test_openhands_human_conversation_uses_dynamic_capability_selection(
    openhands_settings, monkeypatch
):
    runtime = OpenHandsRuntime(openhands_settings)
    requests: list[dict[str, object]] = []
    conversation_id = "10000000-0000-4000-8000-000000000011"

    def fake_request(method: str, path: str, **kwargs: object) -> dict[str, object]:
        requests.append({"method": method, "path": path, **kwargs})
        return {"id": conversation_id if path == "/api/conversations" else "user-1"}

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
    assert "可用 Skill 与 MCP 均为候选能力" in system_context
    assert "先理解用户意图，再自行选择真正相关的能力" in system_context
    assert "https://example.feishu.cn/docx/prd-input" in system_context
    assert requests[1]["path"] == f"/api/conversations/{conversation_id}/events"
    assert requests[1]["json"] == {
        "role": "user",
        "content": [{"type": "text", "text": "你好"}],
        "run": True,
    }
    assert runtime._contracts[conversation_id] == []


@pytest.mark.parametrize(
    ("uri", "accepted"),
    (
        ("https://example.feishu.cn/docx/output", True),
        ("https://example.larksuite.com/docx/output", True),
        ("https://example.larkoffice.com/docx/output", True),
        ("http://example.feishu.cn/docx/output", True),
        ("https://feishu.cn.attacker.example/docx/output", True),
        ("https://example.com/docx/output", True),
        ("https://user:secret@example.com/private", False),
        ("javascript:alert(1)", False),
    ),
)
def test_openhands_accepts_declared_safe_http_output_urls(openhands_settings, uri, accepted):
    runtime = OpenHandsRuntime(openhands_settings)
    runtime._contracts["10000000-0000-4000-8000-000000000002"] = [{"field_key": "design"}]

    outputs = runtime._outputs(
        "10000000-0000-4000-8000-000000000002",
        '{"outputs": {"design": {"uri": ' + repr(uri).replace("'", '"') + "}, "
        '"undeclared": "https://example.feishu.cn/docx/other"}}',
    )

    expected = {"design": ("URL", uri)} if accepted else {}
    assert outputs == expected


@pytest.mark.parametrize(
    ("path", "accepted"),
    (
        ("/runtime/workspace/nodes/asset/attempt/report.pdf", True),
        ("/runtime/workspace/nodes/asset/other/report.pdf", False),
        ("/runtime/workspace/nodes/asset/attempt/../secret.txt", False),
        ("relative/report.pdf", False),
    ),
)
def test_openhands_accepts_file_outputs_only_inside_declared_node_workspace(
    openhands_settings, path, accepted
):
    runtime = OpenHandsRuntime(openhands_settings)
    conversation_id = "10000000-0000-4000-8000-000000000002"
    runtime._contracts[conversation_id] = [
        {
            "field_key": "report",
            "artifact_type": "FILE",
            "workspace_root": "/runtime/workspace/nodes/asset/attempt",
        }
    ]

    outputs = runtime._outputs(
        conversation_id,
        json.dumps({"outputs": {"report": {"artifact_type": "FILE", "path": path}}}),
    )

    expected = {"report": ("FILE", path)} if accepted else {}
    assert outputs == expected


def test_openhands_normalizes_incremental_events_and_terminal_result(
    openhands_settings, monkeypatch
):
    runtime = OpenHandsRuntime(openhands_settings)
    runtime._contracts["10000000-0000-4000-8000-000000000002"] = [
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
                        "timestamp": "2026-08-26T10:00:01+00:00",
                        "source": "agent",
                        "llm_message": {
                            "role": "assistant",
                            "content": [{"type": "text", "text": "working"}],
                        },
                    },
                    {
                        "kind": "ActionEvent",
                        "id": "12",
                        "timestamp": "2026-08-26T10:00:02+00:00",
                        "source": "agent",
                        "thought": [{"type": "text", "text": "search"}],
                        "action": {"kind": "ThinkAction"},
                    },
                    {"kind": "FutureEvent", "id": "13", "source": "environment"},
                ]
            },
            _state(leaf_event_id="13", stats={"usage_to_metrics": {}}),
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
            _state(leaf_event_id="14", stats={"usage_to_metrics": {}}),
        ]
    )
    requests: list[tuple[str, str, object]] = []

    def fake_request(method: str, path: str, **kwargs: object) -> dict[str, object]:
        requests.append((method, path, kwargs.get("params")))
        return next(responses)

    monkeypatch.setattr(runtime, "_request", fake_request)
    handle = _handle("10")

    running = runtime.read_events(handle)
    terminal = runtime.read_events(_handle(running.cursor))

    assert [event.event_type for event in running.events] == ["MESSAGE", "THOUGHT", "STATE"]
    assert [event.cursor for event in running.events] == ["11", "12", "13"]
    assert running.events[2].payload["source_type"] == "FutureEvent"
    assert running.events[1].payload["event_name"] == "ThinkAction"
    assert running.events[1].payload["content"] == "search"
    assert running.events[0].payload["timestamp"] == "2026-08-26T10:00:01+00:00"
    assert running.events[1].payload["timestamp"] == "2026-08-26T10:00:02+00:00"
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
            "/api/conversations/10000000-0000-4000-8000-000000000002/events/search",
            {"limit": 100, "sort_order": "TIMESTAMP", "page_id": "10"},
        ),
        ("GET", "/api/conversations/10000000-0000-4000-8000-000000000002", None),
        (
            "GET",
            "/api/conversations/10000000-0000-4000-8000-000000000002/events/search",
            {"limit": 100, "sort_order": "TIMESTAMP", "page_id": "13"},
        ),
        ("GET", "/api/conversations/10000000-0000-4000-8000-000000000002", None),
    ]


def test_openhands_reads_only_the_native_active_head_branch(openhands_settings, monkeypatch):
    runtime = OpenHandsRuntime(openhands_settings)
    responses = iter(
        [
            {
                "items": [
                    {
                        "kind": "MessageEvent",
                        "id": "user-1",
                        "parent_id": "__root__",
                        "source": "user",
                        "llm_message": {"role": "user", "content": "first"},
                    },
                    {
                        "kind": "MessageEvent",
                        "id": "old-answer",
                        "parent_id": "user-1",
                        "source": "agent",
                        "llm_message": {"role": "assistant", "content": "old"},
                    },
                    {
                        "kind": "MessageEvent",
                        "id": "new-answer",
                        "parent_id": "user-1",
                        "source": "agent",
                        "llm_message": {"role": "assistant", "content": "new"},
                    },
                ]
            },
            _state(leaf_event_id="new-answer"),
        ]
    )
    monkeypatch.setattr(runtime, "_request", lambda *_args, **_kwargs: next(responses))

    batch = runtime.read_active_events(_handle())

    assert [event.cursor for event in batch.events] == ["user-1", "new-answer"]
    assert [event.payload["content"] for event in batch.events] == ["first", "new"]


def test_openhands_does_not_replay_cursor_finish_as_next_turn_result(
    openhands_settings, monkeypatch
):
    runtime = OpenHandsRuntime(openhands_settings)
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
            _state(
                execution_status="finished",
                leaf_event_id="old-finish",
                last_user_message_id="new-user",
            ),
            _state(
                execution_status="finished",
                leaf_event_id="old-finish",
                last_user_message_id="new-user",
            ),
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
    handle = _handle("old-finish")

    batch = runtime.read_events(handle)
    inspected = runtime.inspect(handle)

    assert batch.events == ()
    assert batch.result is None
    assert batch.cursor == "old-finish"
    assert inspected.status == "RUNNING"
    assert inspected.final_message is None


def test_openhands_rejects_missing_persisted_event_anchor(openhands_settings, monkeypatch):
    """Event correlation must fail closed instead of guessing across missing anchors."""

    runtime = OpenHandsRuntime(openhands_settings)
    responses = iter(
        [
            {
                "items": [
                    {
                        "kind": "ActionEvent",
                        "id": "old-finish",
                        "source": "agent",
                        "action": {"kind": "FinishAction", "message": "old"},
                    }
                ]
            },
            _state(leaf_event_id="old-finish", stats={"usage_to_metrics": {}}),
            {
                "items": [
                    {
                        "kind": "ActionEvent",
                        "id": "cursor-1",
                        "source": "agent",
                        "thought": [{"type": "text", "text": "anchor"}],
                        "action": {"kind": "ThinkAction"},
                    },
                    {
                        "kind": "MessageEvent",
                        "id": "message-2",
                        "source": "agent",
                        "llm_message": {
                            "role": "assistant",
                            "content": [{"type": "text", "text": "recovered"}],
                        },
                    },
                ]
            },
            _state(leaf_event_id="message-2", stats={"usage_to_metrics": {}}),
        ]
    )
    monkeypatch.setattr(runtime, "_request", lambda *_args, **_kwargs: next(responses))
    handle = _handle("cursor-1")

    with pytest.raises(DomainError) as caught:
        runtime.read_events(handle)

    assert caught.value.code == "RUNTIME_EVENT_IDENTITY_MISMATCH"


def test_openhands_finished_turn_uses_assistant_message_after_latest_user(
    openhands_settings, monkeypatch
):
    runtime = OpenHandsRuntime(openhands_settings)
    responses = iter(
        [
            _state(
                execution_status="finished",
                leaf_event_id="state-finished",
                last_user_message_id="user-current",
            ),
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

    result = runtime.inspect(_handle("old-finish"))

    assert result.status == "COMPLETED"
    assert result.final_message == "你好！"
    assert result.cursor == "state-finished"


def test_openhands_resume_interrupts_the_active_turn_before_steering(
    openhands_settings, monkeypatch
):
    runtime = OpenHandsRuntime(openhands_settings)
    requests: list[tuple[str, str, object]] = []

    def fake_request(method: str, path: str, **kwargs: object) -> dict[str, object]:
        requests.append((method, path, kwargs.get("json")))
        return (
            _state(execution_status="running", leaf_event_id="cursor-1") if method == "GET" else {}
        )

    monkeypatch.setattr(runtime, "_request", fake_request)
    result = runtime.resume(
        _handle("cursor-1"),
        "新的约束",
        ("data:image/png;base64,aW1hZ2U=",),
    )

    assert result.status == "RUNNING"
    assert requests == [
        ("POST", "/api/conversations/10000000-0000-4000-8000-000000000002/interrupt", {}),
        (
            "POST",
            "/api/conversations/10000000-0000-4000-8000-000000000002/events",
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": "新的约束"},
                    {"type": "image", "image_urls": ["data:image/png;base64,aW1hZ2U="]},
                ],
                "run": True,
            },
        ),
        ("GET", "/api/conversations/10000000-0000-4000-8000-000000000002", None),
    ]


def test_openhands_send_message_advances_cursor_to_user_event(openhands_settings, monkeypatch):
    runtime = OpenHandsRuntime(openhands_settings)

    def fake_request(_method: str, _path: str, **_kwargs: object) -> dict[str, object]:
        return {"id": "user-event-2"}

    monkeypatch.setattr(runtime, "_request", fake_request)
    result = runtime.send_message(
        _handle("initial-event-1"),
        "读取当前输入",
    )

    assert result.status == "RUNNING"
    assert result.cursor == "user-event-2"


def test_openhands_public_stream_exposes_text_but_not_reasoning():
    assert OpenHandsRuntime._visible_stream_event(
        {
            "kind": "StreamingDeltaEvent",
            "content": "可见正文",
            "reasoning_content": "不得外泄的推理",
        }
    ) == ({"type": "delta", "content": "可见正文"},)
    assert (
        OpenHandsRuntime._visible_stream_event(
            {"kind": "StreamingDeltaEvent", "reasoning_content": "不得外泄的推理"}
        )
        == ()
    )
    assert (
        OpenHandsRuntime._visible_stream_event(
            {
                "kind": "MessageEvent",
                "id": "user-1",
                "source": "user",
                "llm_message": {"role": "user"},
            }
        )
        == ()
    )
    assert OpenHandsRuntime._visible_stream_event(
        {
            "kind": "ActionEvent",
            "id": "tool-1",
            "timestamp": "2026-08-26T10:00:03+00:00",
            "thought": [{"type": "text", "text": "可见过程说明"}],
            "summary": "检查当前工作目录",
            "tool_name": "terminal",
            "tool_call_id": "call-tool-1",
            "reasoning_content": "不得外泄的顶层推理",
            "thinking_blocks": [{"thinking": "不得外泄的思考块"}],
            "responses_reasoning_item": {"encrypted_content": "must-not-leak"},
            "action": {
                "kind": "TerminalAction",
                "command": "pwd",
                "api_key": "must-not-leak",
            },
        }
    ) == (
        {
            "type": "event",
            "event": {
                "id": "tool-1",
                "event_type": "TOOL_CALL",
                "payload": {
                    "source_type": "ActionEvent",
                    "source": None,
                    "content": "可见过程说明",
                    "timestamp": "2026-08-26T10:00:03+00:00",
                    "thought": "可见过程说明",
                    "summary": "检查当前工作目录",
                    "action_id": "tool-1",
                    "tool_call_id": "call-tool-1",
                    "tool_name": "terminal",
                    "event_name": "TerminalAction",
                    "details": {"command": "pwd", "api_key": "[redacted]"},
                },
            },
        },
    )
    assert OpenHandsRuntime._visible_stream_event(
        {
            "kind": "ObservationEvent",
            "id": "result-1",
            "timestamp": "2026-08-26T10:00:03.500000+00:00",
            "source": "environment",
            "parent_id": "unrelated-sequential-event",
            "action_id": "tool-1",
            "tool_call_id": "call-tool-1",
            "tool_name": "terminal",
            "observation": {
                "kind": "TerminalObservation",
                "content": "workspace\n",
                "command": "pwd",
                "exit_code": 0,
                "is_error": False,
                "api_token": "must-not-leak",
            },
        }
    ) == (
        {
            "type": "event",
            "event": {
                "id": "result-1",
                "event_type": "TOOL_RESULT",
                "payload": {
                    "source_type": "ObservationEvent",
                    "source": "environment",
                    "content": "workspace\n",
                    "timestamp": "2026-08-26T10:00:03.500000+00:00",
                    "action_id": "tool-1",
                    "tool_call_id": "call-tool-1",
                    "tool_name": "terminal",
                    "parent_id": "unrelated-sequential-event",
                    "event_name": "TerminalObservation",
                    "details": {
                        "content": "workspace\n",
                        "command": "pwd",
                        "exit_code": 0,
                        "is_error": False,
                        "api_token": "[redacted]",
                    },
                },
            },
        },
    )
    assert OpenHandsRuntime._visible_stream_event(
        {
            "kind": "ActionEvent",
            "id": "finish-1",
            "timestamp": "2026-08-26T10:00:04+00:00",
            "source": "agent",
            "parent_id": "tool-1",
            "thought": [{"type": "text", "text": "我已完成核对，下面给出结论。"}],
            "summary": "汇总最终结论",
            "reasoning_content": "不得外泄的顶层推理",
            "action": {
                "kind": "FinishAction",
                "message": "正式最终回复",
            },
        }
    ) == (
        {
            "type": "event",
            "event": {
                "id": "finish-1",
                "event_type": "COMPLETED",
                "payload": {
                    "source_type": "ActionEvent",
                    "source": "agent",
                    "content": "正式最终回复",
                    "timestamp": "2026-08-26T10:00:04+00:00",
                    "thought": "我已完成核对，下面给出结论。",
                    "summary": "汇总最终结论",
                    "action_id": "finish-1",
                    "parent_id": "tool-1",
                    "event_name": "FinishAction",
                    "details": {},
                },
            },
        },
        {"type": "message_complete"},
    )


def test_bash_wakeup_identity_excludes_command_output_and_marks_direct_actor():
    identity = OpenHandsRuntime._bash_event_identity(
        {
            "id": "bash-event-1",
            "kind": "BashOutput",
            "timestamp": "2026-08-13T10:00:00Z",
            "command_id": "command-1",
            "order": 2,
            "exit_code": 0,
            "stdout": "secret output",
            "stderr": "secret error",
        }
    )

    assert identity == {
        "event_id": "bash-event-1",
        "kind": "BashOutput",
        "timestamp": "2026-08-13T10:00:00Z",
        "command_id": "command-1",
        "order": 2,
        "exit_code": 0,
        "actor": "HUMAN_OR_SYSTEM",
        "source": "DIRECT_BASH",
    }
    assert "stdout" not in identity
    assert "stderr" not in identity


@pytest.mark.asyncio
async def test_openhands_isolated_stream_uses_controller_and_filters_reasoning(
    openhands_settings, monkeypatch
):
    runtime = OpenHandsRuntime(
        openhands_settings.model_copy(
            update={
                "docker_controller_mode": "remote",
                "docker_controller_api_key": "a" * 32,
            }
        )
    )
    observed: dict[str, str] = {}

    async def stream(_client, *, resource_name: str, resource_id: str, conversation_id: str):
        observed.update(
            resource_name=resource_name,
            resource_id=resource_id,
            conversation_id=conversation_id,
        )
        yield {
            "kind": "StreamingDeltaEvent",
            "content": "可见正文",
            "reasoning_content": "隐藏推理",
        }
        yield {
            "kind": "MessageEvent",
            "id": "assistant-1",
            "timestamp": "2026-08-26T10:00:04+00:00",
            "source": "agent",
            "llm_message": {"role": "assistant", "content": "已完成"},
        }

    monkeypatch.setattr(DockerControllerClient, "stream_runtime_events", stream)
    handle = RuntimeHandle(
        "env-chat:fw-sbx-runtime",
        "10000000-0000-4000-8000-000000000002",
        runtime_resource_id="sandbox-1",
        runtime_resource_name="fw-sbx-runtime",
    )

    events = [event async for event in runtime.stream_events(handle)]

    assert events == [
        {"type": "delta", "content": "可见正文"},
        {
            "type": "event",
            "event": {
                "id": "assistant-1",
                "event_type": "MESSAGE",
                "payload": {
                    "source_type": "MessageEvent",
                    "source": "agent",
                    "content": "已完成",
                    "timestamp": "2026-08-26T10:00:04+00:00",
                },
            },
        },
        {"type": "message_complete"},
    ]
    assert observed == {
        "resource_name": "fw-sbx-runtime",
        "resource_id": "sandbox-1",
        "conversation_id": "10000000-0000-4000-8000-000000000002",
    }


@pytest.mark.asyncio
async def test_openhands_isolated_stream_rejects_missing_sandbox_binding(openhands_settings):
    runtime = OpenHandsRuntime(
        openhands_settings.model_copy(
            update={
                "docker_controller_mode": "remote",
                "docker_controller_api_key": "a" * 32,
            }
        )
    )

    with pytest.raises(DomainError, match="verified sandbox binding"):
        await anext(
            runtime.stream_events(
                RuntimeHandle("env-chat:fw-sbx-runtime", "10000000-0000-4000-8000-000000000002")
            )
        )


def test_openhands_switches_llm_in_place_with_reasoning(openhands_settings, monkeypatch):
    runtime = OpenHandsRuntime(openhands_settings)
    captured: dict[str, object] = {}

    def fake_request(method: str, path: str, **kwargs: object) -> dict[str, object]:
        captured.update({"method": method, "path": path, **kwargs})
        return {"success": True}

    monkeypatch.setattr(runtime, "_request", fake_request)
    runtime.switch_model(
        _handle("cursor-1"),
        RuntimeProvider(
            provider_id="codex-oauth",
            base_url="https://chatgpt.com/backend-api/codex",
            model="gpt-5.6-sol",
            api_key="short-lived-access-token",
            auth_type="CODEX_OAUTH",
            extra_headers={"chatgpt-account-id": "account-123"},
            reasoning_effort="high",
        ),
    )

    assert captured["method"] == "POST"
    assert captured["path"] == "/api/conversations/10000000-0000-4000-8000-000000000002/switch_llm"
    payload = captured["json"]
    assert isinstance(payload, dict)
    assert payload["llm"]["model"] == "openai/gpt-5.6-sol"
    assert payload["llm"]["stream"] is True
    assert payload["llm"]["num_retries"] == 5
    assert payload["llm"]["retry_multiplier"] == 2.0
    assert payload["llm"]["retry_min_wait"] == 1
    assert payload["llm"]["retry_max_wait"] == 4
    assert payload["llm"]["timeout"] is None
    assert payload["llm"]["litellm_extra_body"] == {
        "store": False,
        "reasoning": {"effort": "high"},
    }
    assert payload["llm"]["extra_headers"] == {"chatgpt-account-id": "account-123"}


def test_openhands_send_message_reads_user_anchor_when_endpoint_returns_success(
    openhands_settings, monkeypatch
):
    runtime = OpenHandsRuntime(openhands_settings)
    responses = iter(
        [
            {"success": True},
            _state(
                last_user_message_id="user-event-current",
                leaf_event_id="state-running",
            ),
        ]
    )
    requests: list[tuple[str, str]] = []

    def fake_request(method: str, path: str, **_kwargs: object) -> dict[str, object]:
        requests.append((method, path))
        return next(responses)

    monkeypatch.setattr(runtime, "_request", fake_request)
    result = runtime.send_message(_handle("old-finish"), "你好")

    assert result.status == "RUNNING"
    assert result.cursor == "user-event-current"
    assert requests == [
        ("POST", "/api/conversations/10000000-0000-4000-8000-000000000002/events"),
        ("GET", "/api/conversations/10000000-0000-4000-8000-000000000002"),
    ]


def test_openhands_projects_and_decides_native_confirmation_batch(openhands_settings, monkeypatch):
    runtime = OpenHandsRuntime(openhands_settings)
    action = {
        "kind": "ActionEvent",
        "id": "action-1",
        "parent_id": "user-1",
        "tool_name": "terminal",
        "tool_call_id": "call-1",
        "security_risk": "HIGH",
        "summary": "install package",
        "action": {"kind": "TerminalAction", "command": "uv add package", "token": "secret"},
    }
    responses = iter(
        [
            _state(execution_status="waiting_for_confirmation", leaf_event_id="action-1"),
            {"items": [{"kind": "MessageEvent", "id": "user-1"}, action]},
            _state(execution_status="waiting_for_confirmation", leaf_event_id="action-1"),
            {"items": [{"kind": "MessageEvent", "id": "user-1"}, action]},
            {"success": True},
        ]
    )
    requests: list[tuple[str, str, object]] = []

    def fake_request(method: str, path: str, **kwargs: object) -> dict[str, object]:
        requests.append((method, path, kwargs.get("json")))
        return next(responses)

    monkeypatch.setattr(runtime, "_request", fake_request)
    handle = _handle("user-1")
    pending = runtime.get_pending_confirmation(handle)

    assert pending is not None
    assert len(pending.actions) == 1
    assert pending.actions[0].action_id == "action-1"
    assert pending.actions[0].tool_name == "terminal"
    assert pending.actions[0].arguments == {"command": "uv add package", "token": "[redacted]"}
    result = runtime.respond_to_confirmation(
        handle, pending.pending_actions_digest, True, "approved"
    )
    assert result.status == "RUNNING"
    assert requests[-1] == (
        "POST",
        "/api/conversations/10000000-0000-4000-8000-000000000002/events/respond_to_confirmation",
        {"accept": True, "reason": "approved"},
    )


def test_openhands_confirmation_rejects_drifted_batch(openhands_settings, monkeypatch):
    runtime = OpenHandsRuntime(openhands_settings)
    responses = iter(
        [
            _state(execution_status="waiting_for_confirmation", leaf_event_id="action-2"),
            {
                "items": [
                    {
                        "kind": "ActionEvent",
                        "id": "action-2",
                        "tool_name": "terminal",
                        "tool_call_id": "call-2",
                        "action": {"kind": "TerminalAction", "command": "changed"},
                    }
                ]
            },
        ]
    )

    monkeypatch.setattr(runtime, "_request", lambda *_args, **_kwargs: next(responses))
    with pytest.raises(DomainError) as raised:
        runtime.respond_to_confirmation(
            _handle(),
            "stale-digest",
            False,
            "no",
        )
    assert raised.value.code == "RUNTIME_CONFIRMATION_DRIFTED"


def test_openhands_run_uses_native_endpoint_without_user_message(openhands_settings, monkeypatch):
    runtime = OpenHandsRuntime(openhands_settings)
    responses = iter(
        [
            _state(execution_status="idle", leaf_event_id="reject-1"),
            {"success": True},
        ]
    )
    requests: list[tuple[str, str, object]] = []

    def fake_request(method: str, path: str, **kwargs: object) -> dict[str, object]:
        requests.append((method, path, kwargs.get("json")))
        return next(responses)

    monkeypatch.setattr(runtime, "_request", fake_request)
    result = runtime.run(_handle("action-1"))

    assert result == RuntimeResult(status="RUNNING", cursor="reject-1")
    assert requests == [
        ("GET", "/api/conversations/10000000-0000-4000-8000-000000000002", None),
        ("POST", "/api/conversations/10000000-0000-4000-8000-000000000002/run", {}),
    ]


def test_openhands_run_does_not_retrigger_running_conversation(openhands_settings, monkeypatch):
    runtime = OpenHandsRuntime(openhands_settings)
    requests: list[tuple[str, str]] = []

    def fake_request(method: str, path: str, **_kwargs: object) -> dict[str, object]:
        requests.append((method, path))
        return _state(execution_status="running", leaf_event_id="running-1")

    monkeypatch.setattr(runtime, "_request", fake_request)
    result = runtime.run(_handle())

    assert result.cursor == "running-1"
    assert requests == [("GET", "/api/conversations/10000000-0000-4000-8000-000000000002")]


@pytest.mark.parametrize(
    "status",
    ["finished", "error", "stuck", "waiting_for_confirmation"],
)
def test_openhands_run_does_not_retrigger_non_resumable_conversation(
    openhands_settings, monkeypatch, status
):
    runtime = OpenHandsRuntime(openhands_settings)
    requests: list[tuple[str, str]] = []

    def fake_request(method: str, path: str, **_kwargs: object) -> dict[str, object]:
        requests.append((method, path))
        return _state(execution_status=status, leaf_event_id="after-confirmation")

    monkeypatch.setattr(runtime, "_request", fake_request)
    result = runtime.run(_handle("action-1"))

    assert result.cursor == "after-confirmation"
    assert requests == [("GET", "/api/conversations/10000000-0000-4000-8000-000000000002")]


def test_openhands_maps_disabled_confirmation_policy(openhands_settings, monkeypatch):
    runtime = OpenHandsRuntime(openhands_settings)
    captured: dict[str, object] = {}

    def fake_request(method: str, path: str, **kwargs: object) -> dict[str, object]:
        captured.update({"method": method, "path": path, **kwargs})
        return {"id": "10000000-0000-4000-8000-000000000007", "leaf_event_id": "event-never"}

    monkeypatch.setattr(runtime, "_request", fake_request)
    request = _request()
    runtime.start(
        replace(
            request,
            agent_spec=replace(request.agent_spec, confirmation_policy="NEVER"),
        )
    )

    payload = captured["json"]
    assert isinstance(payload, dict)
    assert payload["confirmation_policy"] == {"kind": "NeverConfirm"}


def test_openhands_serializes_frozen_summarizing_condenser(openhands_settings, monkeypatch):
    runtime = OpenHandsRuntime(openhands_settings)
    captured: dict[str, object] = {}

    def fake_request(method: str, path: str, **kwargs: object) -> dict[str, object]:
        captured.update({"method": method, "path": path, **kwargs})
        return {"id": "10000000-0000-4000-8000-000000000005", "leaf_event_id": "event-1"}

    monkeypatch.setattr(runtime, "_request", fake_request)
    request = _request()
    runtime.start(
        replace(
            request,
            agent_spec=replace(
                request.agent_spec,
                condenser=RuntimeCondenser(
                    kind="LLM_SUMMARIZING", max_size=80, max_tokens=120_000, keep_first=4
                ),
                condenser_provider=request.agent_spec.provider,
            ),
        )
    )

    payload = captured["json"]
    assert isinstance(payload, dict)
    condenser = payload["agent"]["condenser"]
    assert condenser == {
        "kind": "LLMSummarizingCondenser",
        "llm": {
            "model": "openai/gpt-5.6-sol",
            "base_url": "http://host.docker.internal:1234/v1",
            "api_key": "configured-secret",
            "usage_id": "condenser",
            "stream": True,
            "num_retries": 5,
            "retry_multiplier": 2.0,
            "retry_min_wait": 1,
            "retry_max_wait": 4,
            "timeout": None,
            "max_input_tokens": 922000,
        },
        "max_size": 80,
        "max_tokens": 120_000,
        "keep_first": 4,
        "minimum_progress": 0.1,
        "hard_context_reset_max_retries": 5,
        "hard_context_reset_context_scaling": 0.8,
    }


def test_openhands_derives_native_condenser_token_limit_from_declared_window(
    openhands_settings, monkeypatch
):
    runtime = OpenHandsRuntime(openhands_settings)
    captured: dict[str, object] = {}

    def fake_request(method: str, path: str, **kwargs: object) -> dict[str, object]:
        captured.update({"method": method, "path": path, **kwargs})
        return {"id": "10000000-0000-4000-8000-000000000005", "leaf_event_id": "event-1"}

    monkeypatch.setattr(runtime, "_request", fake_request)
    request = _request()
    runtime.start(
        replace(
            request,
            agent_spec=replace(
                request.agent_spec,
                condenser=RuntimeCondenser(
                    kind="LLM_SUMMARIZING",
                    max_size=10_000,
                    max_tokens_ratio=0.9,
                    keep_first=4,
                ),
                condenser_provider=request.agent_spec.provider,
            ),
        )
    )

    payload = captured["json"]
    assert isinstance(payload, dict)
    condenser = payload["agent"]["condenser"]
    assert condenser["max_tokens"] == 829_800
    assert condenser["max_size"] == 10_000
    assert condenser["keep_first"] == 4


def test_openhands_condense_uses_native_endpoint_and_waits_for_event(
    openhands_settings, monkeypatch
):
    runtime = OpenHandsRuntime(openhands_settings)
    requests: list[tuple[str, str, object, object]] = []

    def fake_request(method: str, path: str, **kwargs: object) -> dict[str, object]:
        requests.append((method, path, kwargs.get("json"), kwargs.get("timeout")))
        return {"success": True}

    monkeypatch.setattr(runtime, "_request", fake_request)
    result = runtime.condense(_handle("event-4"))

    assert result == RuntimeResult(status="RUNNING", cursor="event-4")
    assert requests == [
        (
            "POST",
            "/api/conversations/10000000-0000-4000-8000-000000000002/condense",
            None,
            180,
        )
    ]


def test_openhands_fork_replaces_only_the_governed_condenser(openhands_settings, monkeypatch):
    runtime = OpenHandsRuntime(openhands_settings)
    requests: list[tuple[str, str, object]] = []
    responses = iter(
        [
            _state(execution_status="idle", leaf_event_id="event-4"),
            {
                "id": "10000000-0000-4000-8000-000000000003",
                "forked_from_conversation_id": ("10000000-0000-4000-8000-000000000002"),
                "forked_from_event_id": "event-4",
                "leaf_event_id": "event-4",
            },
        ]
    )

    def fake_request(method: str, path: str, **kwargs: object) -> dict[str, object]:
        requests.append((method, path, kwargs.get("json")))
        return next(responses)

    monkeypatch.setattr(runtime, "_request", fake_request)
    request = _request()
    result = runtime.fork_conversation(
        _handle("event-4"),
        target_conversation_id="10000000-0000-4000-8000-000000000003",
        title="Fork",
        from_event_id="event-4",
        expected_source_leaf_event_id="event-4",
        reset_metrics=True,
        condenser=RuntimeCondenser(
            kind="LLM_SUMMARIZING",
            max_size=10_000,
            max_tokens_ratio=0.8,
            keep_first=4,
        ),
        condenser_provider=request.agent_spec.provider,
    )

    assert result.handle.conversation_id == "10000000-0000-4000-8000-000000000003"
    assert requests[0][:2] == (
        "GET",
        "/api/conversations/10000000-0000-4000-8000-000000000002",
    )
    method, path, payload = requests[1]
    assert method == "POST"
    assert path == "/api/conversations/10000000-0000-4000-8000-000000000002/fork"
    assert isinstance(payload, dict)
    assert payload["condenser"] == {
        "kind": "LLMSummarizingCondenser",
        "llm": {
            "model": "openai/gpt-5.6-sol",
            "base_url": "http://host.docker.internal:1234/v1",
            "api_key": "configured-secret",
            "usage_id": "condenser",
            "stream": True,
            "num_retries": 5,
            "retry_multiplier": 2.0,
            "retry_min_wait": 1,
            "retry_max_wait": 4,
            "timeout": None,
            "max_input_tokens": 922_000,
        },
        "max_size": 10_000,
        "max_tokens": 737_600,
        "keep_first": 4,
        "minimum_progress": 0.1,
        "hard_context_reset_max_retries": 5,
        "hard_context_reset_context_scaling": 0.8,
    }
    assert payload["from_event_id"] == "event-4"
    assert payload["reset_metrics"] is True


def test_openhands_resolves_visible_finish_to_executed_fork_boundary(
    openhands_settings, monkeypatch
):
    runtime = OpenHandsRuntime(openhands_settings)
    requests: list[tuple[str, str]] = []
    finish_action = {
        "kind": "ActionEvent",
        "id": "finish-action",
        "parent_id": "user-1",
        "source": "agent",
        "tool_call_id": "finish-call",
        "action": {"kind": "FinishAction", "message": "done"},
    }
    finish_observation = {
        "kind": "ObservationEvent",
        "id": "finish-observation",
        "parent_id": "finish-action",
        "source": "environment",
        "action_id": "finish-action",
        "tool_call_id": "finish-call",
        "observation": {"kind": "FinishObservation", "content": "done"},
    }

    def fake_request(method: str, path: str, **kwargs: object) -> dict[str, object]:
        del kwargs
        requests.append((method, path))
        if path.endswith("/events/finish-action"):
            return finish_action
        return {"items": [finish_action, finish_observation]}

    monkeypatch.setattr(runtime, "_request", fake_request)

    boundary = runtime.resolve_fork_boundary(_handle("finish-observation"), "finish-action")

    assert boundary == "finish-observation"
    assert OpenHandsRuntime._event_type(finish_action) == "COMPLETED"
    assert OpenHandsRuntime._event_type(finish_observation) == "TOOL_RESULT"
    assert OpenHandsRuntime._event_payload(finish_observation)["content"] == ""
    assert requests == [
        (
            "GET",
            "/api/conversations/10000000-0000-4000-8000-000000000002/events/finish-action",
        ),
        (
            "GET",
            "/api/conversations/10000000-0000-4000-8000-000000000002/events/search",
        ),
    ]


def test_openhands_detects_legacy_fork_that_only_replayed_copied_finish(
    openhands_settings, monkeypatch
):
    runtime = OpenHandsRuntime(openhands_settings)
    source_id = "10000000-0000-4000-8000-000000000003"
    fork_state = _state(
        execution_status="finished",
        leaf_event_id="replayed-observation",
        forked_from_conversation_id=source_id,
        forked_from_event_id="finish-action",
    )
    source_user = {
        "kind": "MessageEvent",
        "id": "source-user",
        "parent_id": "__root__",
        "source": "user",
        "llm_message": {"role": "user", "content": "source request"},
    }
    copied_finish = {
        "kind": "ActionEvent",
        "id": "finish-action",
        "parent_id": "source-user",
        "source": "agent",
        "tool_call_id": "finish-call",
        "action": {"kind": "FinishAction", "message": "old reply"},
    }
    retry_user = {
        "kind": "MessageEvent",
        "id": "retry-user",
        "parent_id": "finish-action",
        "source": "user",
        "llm_message": {"role": "user", "content": "continue"},
    }
    replayed_observation = {
        "kind": "ObservationEvent",
        "id": "replayed-observation",
        "parent_id": "retry-user",
        "source": "environment",
        "action_id": "finish-action",
        "tool_call_id": "finish-call",
        "observation": {"kind": "FinishObservation", "content": "old reply"},
    }
    source_finish = {**copied_finish}
    source_observation = {
        **replayed_observation,
        "id": "source-finish-observation",
        "parent_id": "finish-action",
    }

    def fake_request(method: str, path: str, **kwargs: object) -> dict[str, object]:
        del method, kwargs
        if path.endswith("/events/finish-action"):
            return source_finish
        if f"/conversations/{source_id}/events/search" in path:
            return {"items": [source_finish, source_observation]}
        if path.endswith(f"/conversations/{source_id}"):
            return {
                **_state(leaf_event_id="source-finish-observation"),
                "id": source_id,
                "persistence_dir": (
                    "/runtime/state/conversations/10000000000040008000000000000003"
                ),
            }
        if path.endswith("/events/search"):
            return {"items": [source_user, copied_finish, retry_user, replayed_observation]}
        return fork_state

    monkeypatch.setattr(runtime, "_request", fake_request)

    recovery = runtime.incomplete_fork_recovery(_handle("replayed-observation"))

    assert recovery is not None
    assert recovery.source_conversation_id == source_id
    assert recovery.requested_event_id == "finish-action"
    assert recovery.completed_event_id == "source-finish-observation"
    assert recovery.source_leaf_event_id == "source-finish-observation"


def test_openhands_does_not_rebuild_fork_after_new_agent_progress(openhands_settings, monkeypatch):
    runtime = OpenHandsRuntime(openhands_settings)
    state = _state(
        execution_status="finished",
        leaf_event_id="new-answer",
        forked_from_conversation_id="10000000-0000-4000-8000-000000000003",
        forked_from_event_id="finish-action",
    )
    events = [
        {
            "kind": "MessageEvent",
            "id": "source-user",
            "parent_id": "__root__",
            "source": "user",
            "llm_message": {"role": "user", "content": "source request"},
        },
        {
            "kind": "ActionEvent",
            "id": "finish-action",
            "parent_id": "source-user",
            "source": "agent",
            "tool_call_id": "finish-call",
            "action": {"kind": "FinishAction", "message": "old reply"},
        },
        {
            "kind": "MessageEvent",
            "id": "new-user",
            "parent_id": "finish-action",
            "source": "user",
            "llm_message": {"role": "user", "content": "continue"},
        },
        {
            "kind": "MessageEvent",
            "id": "new-answer",
            "parent_id": "new-user",
            "source": "agent",
            "llm_message": {"role": "assistant", "content": "new reply"},
        },
    ]
    responses = iter([state, {"items": events}])
    monkeypatch.setattr(runtime, "_request", lambda *_args, **_kwargs: next(responses))

    assert runtime.incomplete_fork_recovery(_handle("new-answer")) is None


def test_openhands_read_events_projects_only_native_task_cumulative_usage(
    openhands_settings, monkeypatch
):
    runtime = OpenHandsRuntime(openhands_settings)
    responses = iter(
        [
            {"items": [], "next_page_id": None},
            _state(
                leaf_event_id="task-observation-1",
                stats={
                    "usage_to_metrics": {
                        "flowweave:provider-1": {"accumulated_cost": 9.0},
                        "task:task_00000001": {
                            "model_name": "openai/test-model",
                            "accumulated_cost": 0.125,
                            "accumulated_token_usage": {
                                "prompt_tokens": 120,
                                "completion_tokens": 30,
                                "cache_read_tokens": 40,
                                "cache_write_tokens": 5,
                                "reasoning_tokens": 7,
                                "context_window": 4096,
                                "per_turn_token": 150,
                            },
                        },
                    }
                },
            ),
        ]
    )
    monkeypatch.setattr(runtime, "_request", lambda *_args, **_kwargs: next(responses))

    batch = runtime.read_events(_handle())

    assert len(batch.task_usage) == 1
    usage = batch.task_usage[0]
    assert usage.task_id == "task_00000001"
    assert usage.source_cursor == "task-observation-1"
    assert usage.accumulated_cost == 0.125
    assert usage.prompt_tokens == 120
    assert usage.completion_tokens == 30
    assert len(usage.digest) == 64


def test_openhands_conversation_context_reads_the_active_native_usage_bucket(
    openhands_settings, monkeypatch
):
    runtime = OpenHandsRuntime(openhands_settings)
    monkeypatch.setattr(
        runtime,
        "_request",
        lambda *_args, **_kwargs: _state(
            agent={
                "llm": {
                    "model": "openai/gpt-5.6-luna",
                    "usage_id": "flowweave:provider-1",
                }
            },
            stats={
                "usage_to_metrics": {
                    "condenser": {
                        "model_name": "openai/gpt-5.6-luna",
                        "accumulated_cost": 0.0,
                        "accumulated_token_usage": {
                            "prompt_tokens": 999,
                            "completion_tokens": 0,
                            "cache_read_tokens": 0,
                            "cache_write_tokens": 0,
                            "reasoning_tokens": 0,
                            "context_window": 20_000,
                            "per_turn_token": 999,
                        },
                    },
                    "flowweave:provider-1": {
                        "model_name": "openai/gpt-5.6-luna",
                        "accumulated_cost": 0.1,
                        "accumulated_token_usage": {
                            "prompt_tokens": 12_654,
                            "completion_tokens": 62,
                            "cache_read_tokens": 0,
                            "cache_write_tokens": 0,
                            "reasoning_tokens": 0,
                            "context_window": 922_000,
                            "per_turn_token": 6_380,
                        },
                    },
                }
            },
        ),
    )

    assert runtime.conversation_context(_handle()) == {
        "used_tokens": 6_380,
        "window_tokens": 922_000,
        "cumulative_tokens": 13_715,
        "provider_id": "provider-1",
        "model_name": "openai/gpt-5.6-luna",
        "reasoning_effort": None,
        "condenser_max_size": None,
        "condenser_max_tokens": None,
    }


def test_openhands_conversation_context_exposes_zero_token_baseline_for_pinned_codex_model(
    openhands_settings, monkeypatch
):
    runtime = OpenHandsRuntime(openhands_settings)
    monkeypatch.setattr(
        runtime,
        "_request",
        lambda *_args, **_kwargs: _state(
            agent={
                "llm": {
                    "model": "openai/gpt-5.4",
                    "usage_id": "flowweave:provider-1",
                    "max_input_tokens": 1_050_000,
                }
            },
            stats={"usage_to_metrics": {}},
        ),
    )

    context = runtime.conversation_context(_handle())

    assert context["used_tokens"] == 0
    assert context["window_tokens"] == 1_050_000
    assert context["cumulative_tokens"] is None


@pytest.mark.parametrize(
    "metrics",
    [
        {"accumulated_cost": -0.1},
        {"accumulated_cost": 0.1, "accumulated_token_usage": {"prompt_tokens": -1}},
    ],
)
def test_openhands_rejects_invalid_native_task_usage(metrics):
    with pytest.raises(DomainError) as raised:
        OpenHandsRuntime._task_usage_snapshots(
            {"stats": {"usage_to_metrics": {"task:task_1": metrics}}},
            source_cursor="cursor-1",
        )
    assert raised.value.code == "RUNTIME_TASK_USAGE_PROTOCOL_DRIFT"


def test_openhands_normalizes_condensation_request_and_completion():
    request = {"kind": "CondensationRequest", "id": "condense-request-1"}
    completed = {
        "kind": "Condensation",
        "id": "condensation-1",
        "forgotten_event_ids": ["event-3", "event-2"],
        "summary": "Earlier work was summarized.",
        "summary_offset": 2,
        "llm_response_id": "response-1",
    }

    assert OpenHandsRuntime._event_type(request) == "CONDENSATION_REQUESTED"
    assert OpenHandsRuntime._event_payload(request)["event_name"] == "CondensationRequest"
    assert OpenHandsRuntime._event_type(completed) == "CONDENSATION_COMPLETED"
    assert OpenHandsRuntime._event_payload(completed) == {
        "source_type": "Condensation",
        "source": None,
        "content": "",
        "event_name": "Condensation",
        "forgotten_event_ids": ["event-2", "event-3"],
        "summary": "Earlier work was summarized.",
        "summary_offset": 2,
        "llm_response_id": "response-1",
    }


def test_openhands_projects_native_conversation_error_details():
    event = {
        "kind": "ConversationErrorEvent",
        "id": "error-1",
        "timestamp": "2026-08-26T10:00:05+00:00",
        "source": "environment",
        "parent_id": "user-1",
        "code": "LLMRateLimitError",
        "detail": "The usage limit has been reached",
        "classification": {"kind": "rate_limit", "retryable": True, "user_action": "retry"},
    }

    assert OpenHandsRuntime._event_type(event) == "ERROR"
    assert OpenHandsRuntime._event_payload(event) == {
        "source_type": "ConversationErrorEvent",
        "source": "environment",
        "content": "The usage limit has been reached",
        "timestamp": "2026-08-26T10:00:05+00:00",
        "parent_id": "user-1",
        "event_name": "ConversationErrorEvent",
        "error_code": "LLMRateLimitError",
        "classification": {"kind": "rate_limit", "retryable": True, "user_action": "retry"},
    }


def test_openhands_does_not_fail_a_completed_reply_for_late_autotitle_error(
    openhands_settings,
):
    runtime = OpenHandsRuntime(openhands_settings)
    result = runtime._result_from_events(
        "10000000-0000-4000-8000-000000000002",
        [
            {
                "kind": "MessageEvent",
                "id": "assistant-1",
                "source": "agent",
                "parent_id": "user-1",
                "llm_message": {"role": "assistant", "content": "已完成"},
            },
            {
                "kind": "ConversationErrorEvent",
                "id": "title-error",
                "source": "environment",
                "parent_id": "user-1",
                "code": "NotFoundError",
                "detail": "title provider returned 404",
            },
        ],
        "title-error",
        assistant_message_is_final=True,
    )

    assert result is not None
    assert result.status == "COMPLETED"
    assert result.final_message == "已完成"


def test_openhands_keeps_an_error_from_a_different_turn_after_an_assistant_reply(
    openhands_settings,
):
    runtime = OpenHandsRuntime(openhands_settings)
    result = runtime._result_from_events(
        "10000000-0000-4000-8000-000000000002",
        [
            {
                "kind": "MessageEvent",
                "id": "assistant-1",
                "source": "agent",
                "parent_id": "old-user",
                "llm_message": {"role": "assistant", "content": "较早回复"},
            },
            {
                "kind": "ConversationErrorEvent",
                "id": "current-error",
                "source": "environment",
                "parent_id": "current-user",
                "code": "LLMRateLimitError",
                "detail": "当前回合失败",
            },
        ],
        "current-error",
    )

    assert result is not None
    assert result.status == "FAILED"
    assert result.error == "当前回合失败"


def test_openhands_projects_native_task_tool_lifecycle_without_fabricating_child_api():
    requested = {
        "kind": "ActionEvent",
        "id": "task-action-1",
        "tool_call_id": "task-call-1",
        "llm_response_id": "task-response-1",
        "action": {
            "kind": "TaskAction",
            "description": "review change",
            "prompt": "Review token=private without leaking it into the summary.",
            "subagent_type": "reviewer",
            "resume": None,
        },
    }
    completed = {
        "kind": "ObservationEvent",
        "id": "task-observation-1",
        "action_id": "task-action-1",
        "tool_call_id": "task-call-1",
        "observation": {
            "kind": "TaskObservation",
            "content": [{"type": "text", "text": "Looks good."}],
            "is_error": False,
            "task_id": "task_00000001",
            "subagent": "reviewer",
            "status": "completed",
        },
    }

    requested_payload = OpenHandsRuntime._event_payload(requested)
    completed_payload = OpenHandsRuntime._event_payload(completed)

    assert OpenHandsRuntime._event_type(requested) == "TOOL_CALL"
    assert requested_payload["runtime_task"] == {
        "phase": "REQUESTED",
        "action_event_id": "task-action-1",
        "tool_call_id": "task-call-1",
        "llm_response_id": "task-response-1",
        "subagent_type": "reviewer",
        "description": "review change",
        "resume_task_id": None,
    }
    assert "prompt" not in requested_payload["runtime_task"]
    assert "prompt" not in requested_payload["details"]
    assert OpenHandsRuntime._event_type(completed) == "TOOL_RESULT"
    assert completed_payload["runtime_task"] == {
        "phase": "COMPLETED",
        "action_event_id": "task-action-1",
        "observation_event_id": "task-observation-1",
        "tool_call_id": "task-call-1",
        "task_id": "task_00000001",
        "subagent_type": "reviewer",
        "status": "completed",
        "outcome": {
            "is_error": False,
            "content": [{"type": "text", "text": "Looks good."}],
        },
    }


def test_openhands_projects_native_skill_activation_and_invocation_events():
    activated = {
        "kind": "MessageEvent",
        "id": "message-1",
        "activated_skills": ["requirements"],
        "llm_message": {"content": [{"type": "text", "text": "$requirements"}]},
    }
    invoked = {
        "kind": "ActionEvent",
        "id": "skill-action-1",
        "tool_call_id": "skill-call-1",
        "llm_response_id": "response-1",
        "action": {"kind": "InvokeSkillAction", "name": "requirements"},
    }
    loaded = {
        "kind": "ObservationEvent",
        "id": "skill-observation-1",
        "action_id": "skill-action-1",
        "tool_call_id": "skill-call-1",
        "observation": {
            "kind": "InvokeSkillObservation",
            "skill_name": "requirements",
            "is_error": False,
            "content": [{"type": "text", "text": "governed skill body"}],
        },
    }

    assert OpenHandsRuntime._event_payload(activated)["activated_skills"] == ["requirements"]
    assert OpenHandsRuntime._event_payload(invoked)["runtime_skill"] == {
        "phase": "INVOKED",
        "skill_name": "requirements",
        "action_event_id": "skill-action-1",
        "tool_call_id": "skill-call-1",
        "llm_response_id": "response-1",
    }
    assert OpenHandsRuntime._event_payload(loaded)["runtime_skill"] == {
        "phase": "LOADED",
        "skill_name": "requirements",
        "action_event_id": "skill-action-1",
        "observation_event_id": "skill-observation-1",
        "tool_call_id": "skill-call-1",
    }


def test_openhands_projects_native_task_error_as_terminal_task_error():
    failed = {
        "kind": "ObservationEvent",
        "id": "task-observation-2",
        "action_id": "task-action-2",
        "tool_call_id": "task-call-2",
        "observation": {
            "kind": "TaskObservation",
            "content": [{"type": "text", "text": "Budget exceeded."}],
            "is_error": True,
            "task_id": "task_00000002",
            "subagent": "reviewer",
            "status": "error",
        },
    }

    assert OpenHandsRuntime._event_payload(failed)["runtime_task"] == {
        "phase": "ERROR",
        "action_event_id": "task-action-2",
        "observation_event_id": "task-observation-2",
        "tool_call_id": "task-call-2",
        "task_id": "task_00000002",
        "subagent_type": "reviewer",
        "status": "error",
        "outcome": {
            "is_error": True,
            "content": [{"type": "text", "text": "Budget exceeded."}],
        },
    }


def test_openhands_pending_actions_only_use_active_branch():
    actions = OpenHandsRuntime._pending_actions(
        [
            {"kind": "MessageEvent", "id": "root"},
            {
                "kind": "ActionEvent",
                "id": "abandoned",
                "parent_id": "root",
                "tool_name": "terminal",
                "tool_call_id": "old",
                "action": {"kind": "TerminalAction", "command": "old"},
            },
            {
                "kind": "ActionEvent",
                "id": "active",
                "parent_id": "root",
                "tool_name": "file_editor",
                "tool_call_id": "new",
                "action": {"kind": "FileEditorAction", "path": "README.md"},
            },
        ],
        "active",
    )
    assert [item.action_id for item in actions] == ["active"]


def test_openhands_cancel_waits_until_agent_is_no_longer_running(openhands_settings, monkeypatch):
    runtime = OpenHandsRuntime(openhands_settings)
    responses = iter([{"ok": True}, {"execution_status": "running"}, {"execution_status": "idle"}])
    requests: list[tuple[str, str, bool]] = []

    def fake_request(method: str, path: str, **kwargs: object) -> dict[str, object]:
        requests.append((method, path, bool(kwargs.get("missing_ok"))))
        return next(responses)

    monkeypatch.setattr(runtime, "_request", fake_request)
    runtime.cancel(_handle("cursor-1"))

    assert requests == [
        ("POST", "/api/conversations/10000000-0000-4000-8000-000000000002/interrupt", True),
        ("GET", "/api/conversations/10000000-0000-4000-8000-000000000002", True),
        ("GET", "/api/conversations/10000000-0000-4000-8000-000000000002", True),
    ]


def test_openhands_cancel_treats_missing_conversation_as_already_stopped(
    openhands_settings, monkeypatch
):
    runtime = OpenHandsRuntime(openhands_settings)

    def fake_request(_method: str, _path: str, **_kwargs: object) -> dict[str, object]:
        return {"_flowweave_missing": True}

    monkeypatch.setattr(runtime, "_request", fake_request)
    runtime.cancel(
        RuntimeHandle(
            "env-exec:fw-sbx-flow-run-1",
            "missing-conversation",
            None,
            "runtime-1",
            "fw-sbx-flow-run-1",
        )
    )


def _plugin_validation_request() -> RuntimePluginValidationRequest:
    validation_id = "33333333-3333-4333-8333-333333333333"
    return RuntimePluginValidationRequest(
        plugin=RuntimePlugin(
            name="governed-review",
            source=(
                f"/runtime/capabilities/nodes/plugin-probe-{validation_id}/plugins/governed-review"
            ),
            content_hash="a" * 64,
        ),
        validation_id=validation_id,
        runtime_resource_id="11111111-1111-4111-8111-111111111111",
        runtime_resource_name="fw-sbx-plugin-probe",
    )


def _plugin_loader_report() -> dict[str, object]:
    return {
        "plugin_name": "governed-review",
        "plugin_version": "1.0.0",
        "skill_count": 0,
        "command_count": 1,
        "agent_count": 0,
        "mcp_server_count": 0,
        "has_hooks": False,
    }


def test_openhands_validates_plugin_locally_with_fixed_owned_operation(
    openhands_settings, monkeypatch
):
    runtime = OpenHandsRuntime(openhands_settings)
    captured: dict[str, object] = {}

    def validate(_settings, **kwargs):
        captured.update(kwargs)
        return _plugin_loader_report()

    monkeypatch.setattr(openhands_module, "validate_owned_runtime_plugin", validate)
    result = runtime.validate_plugin(_plugin_validation_request())

    assert result.plugin_name == "governed-review"
    assert result.command_count == 1
    assert captured == {
        "resource_name": "fw-sbx-plugin-probe",
        "resource_id": "11111111-1111-4111-8111-111111111111",
        "validation_id": "33333333-3333-4333-8333-333333333333",
        "plugin_path": (
            "/runtime/capabilities/nodes/plugin-probe-"
            "33333333-3333-4333-8333-333333333333/plugins/governed-review"
        ),
    }


def test_openhands_validates_plugin_remotely_with_fixed_controller_payload(
    openhands_settings, monkeypatch
):
    runtime = OpenHandsRuntime(
        openhands_settings.model_copy(
            update={
                "docker_controller_mode": "remote",
                "docker_controller_api_key": "a" * 32,
            }
        )
    )
    captured: dict[str, object] = {}

    def post(_client, path, payload, *, timeout):
        captured.update(path=path, payload=payload, timeout=timeout)
        return _plugin_loader_report()

    monkeypatch.setattr(DockerControllerClient, "post", post)
    result = runtime.validate_plugin(_plugin_validation_request())

    assert result.plugin_version == "1.0.0"
    assert captured == {
        "path": "/v1/runtimes/validate-plugin",
        "payload": {
            "resource_name": "fw-sbx-plugin-probe",
            "resource_id": "11111111-1111-4111-8111-111111111111",
            "validation_id": "33333333-3333-4333-8333-333333333333",
            "plugin_path": (
                "/runtime/capabilities/nodes/plugin-probe-"
                "33333333-3333-4333-8333-333333333333/plugins/governed-review"
            ),
        },
        "timeout": 30,
    }


def test_openhands_plugin_validation_rejects_loader_protocol_drift(openhands_settings, monkeypatch):
    runtime = OpenHandsRuntime(openhands_settings)
    invalid = {**_plugin_loader_report(), "command_count": -1}
    monkeypatch.setattr(
        openhands_module,
        "validate_owned_runtime_plugin",
        lambda *_args, **_kwargs: invalid,
    )

    with pytest.raises(DomainError) as raised:
        runtime.validate_plugin(_plugin_validation_request())

    assert raised.value.code == "RUNTIME_PROTOCOL_ERROR"


def test_openhands_routes_agent_workspace_rename_and_delete(openhands_settings, monkeypatch):
    runtime = OpenHandsRuntime(openhands_settings)
    handle = RuntimeHandle(
        job_id="agent-workspace:workspace-1",
        conversation_id="10000000-0000-4000-8000-000000000020",
        runtime_resource_name="fw-sbx-agent-workspace-1",
    )
    requests: list[tuple[str, str, dict[str, object]]] = []

    def request(method: str, path: str, **kwargs: object) -> dict[str, object]:
        requests.append((method, path, dict(kwargs)))
        return {"success": True}

    monkeypatch.setattr(runtime, "_request", request)
    runtime.rename_conversation(handle, "Renamed")
    runtime.delete_conversation(handle)

    assert [(method, path) for method, path, _ in requests] == [
        ("PATCH", "/api/conversations/10000000-0000-4000-8000-000000000020"),
        ("DELETE", "/api/conversations/10000000-0000-4000-8000-000000000020"),
    ]
    assert requests[0][2]["json"] == {"title": "Renamed"}
    for _method, _path, request_kwargs in requests:
        assert request_kwargs["base_url"] == "http://fw-sbx-agent-workspace-1:8000"
        assert request_kwargs["session_api_key"] == derive_runtime_session_key(
            openhands_settings.openhands_session_api_key,
            openhands_settings.sandbox_manager_scope,
            "fw-sbx-agent-workspace-1",
        )
