"""Fail-closed contract probe for the pinned OpenHands Runtime image.

This file runs inside ``flowweave-openhands-runtime:1``.  The platform Python
environment intentionally does not install the OpenHands SDK, so checking the
actual image is the only authoritative way to detect package, API, model, and
default-value drift.
"""

from __future__ import annotations

import json
import os
from importlib.metadata import distribution, version
from inspect import getsource, signature
from pathlib import Path
from tempfile import TemporaryDirectory
from threading import Event, Thread
from types import SimpleNamespace
from unittest.mock import patch
from uuid import uuid4

os.environ.setdefault("OPENHANDS_SUPPRESS_BANNER", "1")
os.environ.setdefault("OH_SECRET_KEY", "flowweave-contract-check-secret-000000000000")

from openhands.agent_server.api import create_app
from openhands.agent_server.mcp_router import (
    MCPOAuthCallbackRequest,
    MCPOAuthStartResponse,
    MCPOAuthStatusResponse,
    MCPTestRequest,
    MCPTestSuccess,
)
from openhands.agent_server.models import (
    BashEventPage,
    ConfirmationResponseRequest,
    ForkConversationRequest,
    NavigateConversationRequest,
    StartConversationRequest,
    StartGoalRequest,
)
from openhands.agent_server.server_details_router import ServerInfo
from openhands.agent_server.sockets import bash_events_socket, events_socket
from openhands.sdk import AgentContext
from openhands.sdk.agent.parallel_executor import ParallelToolExecutor
from openhands.sdk.context.condenser import (
    LLMSummarizingCondenser,
    NoOpCondenser,
    PipelineCondenser,
)
from openhands.sdk.context.memory import (
    MEMORY_CHAR_BUDGET,
    MEMORY_INDEX_RELPATH,
    load_memory,
)
from openhands.sdk.conversation.conversation_stats import ConversationStats
from openhands.sdk.conversation.goal import GoalStatus, GoalVerdict
from openhands.sdk.conversation.state import (
    ConversationExecutionStatus,
    ConversationState,
)
from openhands.sdk.critic import (
    AgentFinishedCritic,
    CriticResult,
    IterativeRefinementConfig,
)
from openhands.sdk.event import ActionEvent, MessageEvent
from openhands.sdk.event.condenser import Condensation, CondensationRequest
from openhands.sdk.event.conversation_state import ConversationStateUpdateEvent
from openhands.sdk.llm import Message, TextContent
from openhands.sdk.llm.utils.metrics import Metrics
from openhands.sdk.marketplace.registration import MarketplaceRegistration
from openhands.sdk.marketplace.registry import MarketplaceRegistry
from openhands.sdk.mcp.config import MCPOAuthAuthCredential, MCPOAuthState
from openhands.sdk.plugin import Plugin, fetch_plugin_with_resolution
from openhands.sdk.plugin.types import PluginSource
from openhands.sdk.profiles import (
    AGENT_PROFILE_SCHEMA_VERSION,
    LaunchedAgentProfile,
    OpenHandsAgentProfile,
    ProfileVerificationSettings,
    validate_agent_profile,
)
from openhands.sdk.security.confirmation_policy import NeverConfirm
from openhands.sdk.skills import KeywordTrigger, Skill
from openhands.sdk.tool.builtins import BUILT_IN_TOOLS
from openhands.sdk.tool.builtins.invoke_skill import (
    InvokeSkillAction,
    InvokeSkillExecutor,
    InvokeSkillTool,
)
from openhands.sdk.tool.registry import (
    get_tool_module_qualnames,
    list_registered_tools,
    list_usable_tools,
    resolve_tool,
)
from openhands.sdk.tool.spec import Tool
from openhands.sdk.tool.tool import DeclaredResources, ToolExecutor
from openhands.tools.preset.default import AgentDefinition
from openhands.tools.task import TaskAction, TaskObservation, TaskToolSet
from openhands.tools.task.impl import TaskExecutor
from openhands.tools.task.manager import Task, TaskManager, TaskStatus

EXPECTED_VERSION = "1.44.0"
EXPECTED_UPSTREAM_BASE = "9a24f6c8866f353042a57df0514ccc900e3a0691"
EXPECTED_SOURCE_ARCHIVE_SHA256 = (
    "94e0bc26a670c552f8bed2dfba048d9a5c6d7bc66778e7844009db6785da6d21"
)
PACKAGES = (
    "openhands-agent-server",
    "openhands-sdk",
    "openhands-tools",
    "openhands-workspace",
)
REQUIRED_PATHS = {
    "/ready",
    "/server_info",
    "/api/conversations",
    "/api/conversations/{conversation_id}/condense",
    "/api/conversations/{conversation_id}/events/search",
    "/api/conversations/{conversation_id}/events/{event_id}",
    "/api/conversations/{conversation_id}/events/respond_to_confirmation",
    "/api/conversations/{conversation_id}/fork",
    "/api/conversations/{conversation_id}/navigate",
    "/api/conversations/{conversation_id}/run",
    "/api/conversations/{conversation_id}/goal",
    "/api/conversations/{conversation_id}/goal/resume",
    "/api/conversations/{conversation_id}/goal/stop",
    "/api/conversations/{conversation_id}/ask_agent",
    "/api/mcp/test",
    "/api/mcp/oauth/start",
    "/api/mcp/oauth/status/{job_id}",
    "/api/mcp/oauth/callback/{job_id}",
    "/api/agent-profiles",
    "/api/agent-profiles/{name}",
    "/api/agent-profiles/{name}/materialize",
    "/api/agent-profiles/{name}/rename",
    "/api/agent-profiles/{profile_id}/activate",
    "/api/plugins",
    "/api/skills",
    "/api/sub-agents",
    "/api/tools/",
    "/api/bash/bash_events/search",
}
REQUIRED_WEBSOCKET_PATHS = {
    "/sockets/events/{conversation_id}",
    "/sockets/bash-events",
}
REQUIRED_START_FIELDS = {
    "agent",
    "agent_definitions",
    "agent_profile_id",
    "confirmation_policy",
    "hook_config",
    "observability_metadata",
    "observability_span_name",
    "observability_tags",
    "parent_conversation_id",
    "plugins",
    "security_analyzer",
    "worktree",
    "workspace",
}
EXPECTED_TOOL_MODULES = {
    "ask_oracle": "openhands.tools.ask_oracle.definition",
    "file_editor": "openhands.tools.file_editor.definition",
    "task_tool_set": "openhands.tools.task.definition",
    "task": "openhands.tools.task.definition",
    "task_tracker": "openhands.tools.task_tracker.definition",
    "terminal": "openhands.tools.terminal.definition",
    "workflow_tool_set": "openhands.tools.workflow.definition",
    "workflow": "openhands.tools.workflow.definition",
    "browser_tool_set": "openhands.tools.browser_use.definition",
    "edit": "openhands.tools.gemini.edit.definition",
    "list_directory": "openhands.tools.gemini.list_directory.definition",
    "read_file": "openhands.tools.gemini.read_file.definition",
    "write_file": "openhands.tools.gemini.write_file.definition",
    "glob": "openhands.tools.glob.definition",
    "grep": "openhands.tools.grep.definition",
    "planning_file_editor": "openhands.tools.planning_file_editor.definition",
}


def _field_default(model: type[object], name: str) -> object:
    return model.model_fields[name].default  # type: ignore[attr-defined]


def main() -> None:
    versions = {package: version(package) for package in PACKAGES}
    assert set(versions.values()) == {EXPECTED_VERSION}, versions
    assert os.environ.get("OPENHANDS_BUILD_GIT_SHA") == EXPECTED_UPSTREAM_BASE
    assert os.environ.get("OPENHANDS_BUILD_GIT_REF") == EXPECTED_UPSTREAM_BASE
    event_socket_parameters = signature(events_socket).parameters
    bash_socket_parameters = signature(bash_events_socket).parameters
    assert {"resend_mode", "after_timestamp"} <= set(event_socket_parameters)
    assert "resend_mode" in bash_socket_parameters
    assert "after_timestamp" not in bash_socket_parameters
    assert set(BashEventPage.model_fields) == {"items", "next_page_id"}
    server_info = ServerInfo(uptime=0, idle_time=0)
    assert server_info.version == EXPECTED_VERSION
    assert server_info.sdk_version == EXPECTED_VERSION
    assert server_info.tools_version == EXPECTED_VERSION
    assert server_info.workspace_version == EXPECTED_VERSION
    assert server_info.build_git_sha == EXPECTED_UPSTREAM_BASE
    assert server_info.build_git_ref == EXPECTED_UPSTREAM_BASE
    assert server_info.usable_tools == list_usable_tools()
    assert len(server_info.usable_tools) == len(set(server_info.usable_tools))
    assert {"file_editor", "task_tracker", "terminal"} <= set(server_info.usable_tools)
    assert len(server_info.capabilities) == len(set(server_info.capabilities))
    event_socket_source = getsource(events_socket)
    bash_socket_source = getsource(bash_events_socket)
    socket_source = event_socket_source + bash_socket_source
    assert (
        '@sockets_router.websocket("/events/{conversation_id}")' in event_socket_source
    )
    assert '@sockets_router.websocket("/bash-events")' in bash_socket_source
    assert (
        "_accept_authenticated_websocket(websocket, session_api_key)" in socket_source
    )
    provenance_path = Path("/runtime/openhands-source-provenance.json")
    if not provenance_path.is_file():
        provenance_path = Path(__file__).with_name("openhands-source-provenance.json")
    provenance = json.loads(provenance_path.read_text(encoding="utf-8"))
    build_input = provenance["build_input"]
    assert build_input["upstream_base_commit"] == EXPECTED_UPSTREAM_BASE
    assert build_input["source_commit"] == EXPECTED_UPSTREAM_BASE
    assert build_input["source_kind"] == "upstream_source"
    assert build_input["fork_commit"] is None
    assert provenance["source_archive_sha256"] == EXPECTED_SOURCE_ARCHIVE_SHA256
    assert provenance["overlays"] == {}
    direct_urls = {}
    allowed_source_roots = (
        "file:///opt/openhands-source",
        # OpenHands' official ``docker.build`` source target installs the
        # copied, non-editable fixed source below /agent-server.
        "file:///agent-server",
    )
    for package in PACKAGES:
        direct_url = json.loads(
            distribution(package).read_text("direct_url.json") or "{}"
        )
        actual_url = direct_url.get("url")
        assert actual_url in tuple(
            f"{root}/{package}" for root in allowed_source_roots
        ), (
            package,
            direct_url,
        )
        direct_urls[package] = direct_url["url"]
    assert len({url.rsplit("/", 1)[0] for url in direct_urls.values()}) == 1
    assert Plugin.__module__ == "openhands.sdk.plugin.plugin"
    assert fetch_plugin_with_resolution.__module__ == "openhands.sdk.plugin.fetch"

    schema = create_app().openapi()
    paths = set(schema["paths"])
    assert {"/ready", "/server_info"} <= paths
    missing_paths = sorted(REQUIRED_PATHS - paths)
    assert not missing_paths, {"missing_paths": missing_paths}
    assert set(schema["paths"]["/ready"]) == {"get"}
    assert set(schema["paths"]["/server_info"]) == {"get"}
    server_info_schema = schema["components"]["schemas"]["ServerInfo"]
    assert {
        "version",
        "sdk_version",
        "tools_version",
        "workspace_version",
        "build_git_sha",
        "build_git_ref",
        "usable_tools",
        "capabilities",
    } <= set(server_info_schema["properties"])
    mcp_test_operation = schema["paths"]["/api/mcp/test"]["post"]
    assert mcp_test_operation["requestBody"]["content"]["application/json"][
        "schema"
    ] == {"$ref": "#/components/schemas/MCPTestRequest"}
    mcp_schemas = schema["components"]["schemas"]
    mcp_request = mcp_schemas["MCPTestRequest"]
    assert set(mcp_request["properties"]) == {
        "name",
        "server",
        "timeout",
        "tool_call",
    }
    assert mcp_request["required"] == ["server"]
    assert mcp_request["properties"]["timeout"]["default"] == 15.0
    mcp_response = mcp_test_operation["responses"]["200"]["content"][
        "application/json"
    ]["schema"]
    assert mcp_response["discriminator"]["propertyName"] == "ok"
    assert mcp_response["oneOf"] == [
        {"$ref": "#/components/schemas/MCPTestSuccess"},
        {"$ref": "#/components/schemas/MCPTestFailure"},
    ]
    mcp_success = mcp_schemas["MCPTestSuccess"]["properties"]
    assert set(mcp_success) == {
        "ok",
        "tools",
        "tool_result",
        "resolved_mcp_servers",
        "oauth_state",
    }
    assert mcp_success["tools"]["items"] == {"type": "string"}
    assert "input_schema" not in mcp_success
    assert set(mcp_schemas["MCPTestFailure"]["properties"]["error_kind"]["enum"]) == {
        "timeout",
        "connection",
        "unknown",
    }
    oauth_state_payload = {
        "tokens": {
            "access_token": "contract-access-token",
            "refresh_token": "contract-refresh-token",
            "token_type": "Bearer",
        },
        "client_info": {
            "client_id": "contract-client",
            "client_secret": "contract-client-secret",
        },
        "token_expires_at": 2_000_000_000.0,
    }
    oauth_request = MCPTestRequest.model_validate(
        {
            "server": {
                "type": "http",
                "url": "https://mcp.example.test/mcp",
                "auth": {
                    "strategy": "oauth2",
                    "state": oauth_state_payload,
                },
            }
        }
    )
    oauth_auth = oauth_request.resolved_server.oauth_auth
    assert isinstance(oauth_auth, MCPOAuthAuthCredential)
    assert isinstance(oauth_auth.state, MCPOAuthState)
    assert oauth_auth.state.to_plain_dict() == oauth_state_payload
    oauth_success = MCPTestSuccess(
        tools=[],
        oauth_state=oauth_auth.state.to_response(),
    ).model_dump(mode="json")
    assert "oauth_state" in oauth_success
    assert "oauth_state" not in oauth_request.model_fields
    # Without a Runtime cipher, the formal endpoint returns plaintext state to
    # its control-plane caller. FlowWeave must encrypt this whole envelope as
    # soon as it crosses the adapter boundary; the state never belongs in a
    # Capability, Snapshot, validation report, or ordinary API response.
    assert oauth_success["oauth_state"] == oauth_state_payload
    assert set(schema["paths"]["/api/mcp/oauth/start"]) == {"post"}
    assert set(schema["paths"]["/api/mcp/oauth/status/{job_id}"]) == {"get"}
    assert set(schema["paths"]["/api/mcp/oauth/callback/{job_id}"]) == {"post"}
    assert set(MCPOAuthStartResponse.model_fields) == {
        "ok",
        "job_id",
        "authorization_url",
        "error",
        "error_kind",
    }
    assert set(MCPOAuthStatusResponse.model_fields) == {
        "ok",
        "status",
        "job_id",
        "authorization_url",
        "callback_ready",
        "tools",
        "tool_result",
        "oauth_state",
        "error",
        "error_kind",
    }
    assert set(MCPOAuthCallbackRequest.model_fields) == {"callback_url"}
    task_http_paths = sorted(
        path
        for path in paths
        if "sub-agent" in path or "subagent" in path or "/tasks" in path
    )
    # The pinned OpenHands source exposes only the Agent Definition catalog.
    # Running TaskToolSet children remain internal blocking LocalConversations;
    # there is no child-task list/cancel/confirmation HTTP contract yet.
    assert task_http_paths == ["/api/sub-agents"]
    assert set(schema["paths"]["/api/sub-agents"]) == {"post"}
    goal_http_paths = sorted(path for path in paths if "/goal" in path)
    assert goal_http_paths == [
        "/api/conversations/{conversation_id}/goal",
        "/api/conversations/{conversation_id}/goal/resume",
        "/api/conversations/{conversation_id}/goal/stop",
    ]
    for path in goal_http_paths:
        assert set(schema["paths"][path]) == {"post"}
    ask_agent_path = "/api/conversations/{conversation_id}/ask_agent"
    assert set(schema["paths"][ask_agent_path]) == {"post"}
    ask_request_schema = schema["components"]["schemas"]["AskAgentRequest"]
    ask_response_schema = schema["components"]["schemas"]["AskAgentResponse"]
    assert ask_request_schema["required"] == ["question"]
    assert ask_response_schema["required"] == ["response"]

    confirmation_fields = ConfirmationResponseRequest.model_fields
    assert set(confirmation_fields) == {"accept", "reason"}
    assert confirmation_fields["accept"].is_required()
    assert confirmation_fields["reason"].default == "User rejected the action."

    start_fields = set(StartConversationRequest.model_fields)
    missing_start_fields = sorted(REQUIRED_START_FIELDS - start_fields)
    assert not missing_start_fields, {"missing_start_fields": missing_start_fields}
    assert isinstance(
        _field_default(StartConversationRequest, "confirmation_policy"), NeverConfirm
    )
    profile_http_methods = {
        path: sorted(schema["paths"][path])
        for path in REQUIRED_PATHS
        if path.startswith("/api/agent-profiles")
    }
    assert profile_http_methods == {
        "/api/agent-profiles": ["get"],
        "/api/agent-profiles/{name}": ["delete", "get", "post"],
        "/api/agent-profiles/{name}/materialize": ["post"],
        "/api/agent-profiles/{name}/rename": ["post"],
        "/api/agent-profiles/{profile_id}/activate": ["post"],
    }
    agent_profile_fields = set(OpenHandsAgentProfile.model_fields)
    assert agent_profile_fields == {
        "schema_version",
        "id",
        "name",
        "revision",
        "mcp_server_refs",
        "agent_kind",
        "llm_profile_ref",
        "agent",
        "tools",
        "system_message_suffix",
        "disabled_skills",
        "condenser",
        "verification",
        "enable_sub_agents",
        "enable_switch_llm_tool",
        "tool_concurrency_limit",
    }
    assert AGENT_PROFILE_SCHEMA_VERSION == 2
    assert _field_default(OpenHandsAgentProfile, "agent_kind") == "openhands"
    assert _field_default(OpenHandsAgentProfile, "agent") == "CodeActAgent"
    assert _field_default(OpenHandsAgentProfile, "tools") is None
    assert _field_default(OpenHandsAgentProfile, "enable_sub_agents") is False
    assert _field_default(OpenHandsAgentProfile, "enable_switch_llm_tool") is True
    assert _field_default(OpenHandsAgentProfile, "tool_concurrency_limit") == 1
    assert set(ProfileVerificationSettings.model_fields) == {
        "critic_enabled",
        "critic_mode",
        "enable_iterative_refinement",
        "critic_threshold",
        "max_refinement_iterations",
        "critic_server_url",
        "critic_model_name",
    }
    governed_profile = validate_agent_profile(
        {
            "schema_version": 2,
            "name": "flowweave-contract-profile",
            "agent_kind": "openhands",
            "llm_profile_ref": "governed-llm",
            "mcp_server_refs": [],
            "tools": [{"name": "terminal", "params": {}}],
            "disabled_skills": ["ambient-skill"],
            "condenser": {"condenser_kind": "no_op"},
            "verification": {"critic_enabled": False},
            "enable_sub_agents": False,
            "enable_switch_llm_tool": False,
            "tool_concurrency_limit": 1,
        }
    )
    assert isinstance(governed_profile, OpenHandsAgentProfile)
    launched_profile = LaunchedAgentProfile(
        agent_profile_id=governed_profile.id,
        revision=governed_profile.revision,
    )
    assert launched_profile.model_dump(mode="json") == {
        "agent_profile_id": str(governed_profile.id),
        "revision": 0,
    }
    explicit_agent = StartConversationRequest.model_validate(
        {
            "workspace": {"kind": "LocalWorkspace", "working_dir": "/tmp"},
            "agent": {
                "kind": "Agent",
                "llm": {"model": "openai/test", "api_key": "contract-only"},
                "tools": [],
            },
        }
    )
    assert explicit_agent.agent_profile_id is None
    try:
        StartConversationRequest.model_validate(
            {
                "workspace": {"kind": "LocalWorkspace", "working_dir": "/tmp"},
                "agent": explicit_agent.agent,
                "agent_profile_id": str(governed_profile.id),
            }
        )
    except ValueError:
        pass
    else:
        raise AssertionError(
            "OpenHands unexpectedly accepts agent and agent_profile_id together"
        )
    plugin_source_fields = PluginSource.model_fields
    assert set(plugin_source_fields) == {"source", "ref", "repo_path"}
    assert plugin_source_fields["source"].is_required()
    assert plugin_source_fields["ref"].default is None
    assert plugin_source_fields["repo_path"].default is None
    local_plugin = PluginSource(
        source="/runtime/capabilities/nodes/node-1/plugins/review"
    )
    assert local_plugin.model_dump(mode="json", exclude_none=True) == {
        "source": "/runtime/capabilities/nodes/node-1/plugins/review"
    }
    marketplace_commit = "a" * 40
    with TemporaryDirectory() as directory:
        marketplace_root = Path(directory)
        manifest_dir = marketplace_root / ".plugin"
        manifest_dir.mkdir()
        (manifest_dir / "marketplace.json").write_text(
            json.dumps(
                {
                    "name": "flowweave-contract",
                    "owner": {"name": "FlowWeave"},
                    "plugins": [{"name": "review", "source": "./plugins/review"}],
                }
            )
        )
        registration = MarketplaceRegistration(
            name="governed",
            source="https://github.com/openhands/extensions.git",
            ref=marketplace_commit,
            auto_load=False,
        )
        with patch(
            "openhands.sdk.marketplace.registry.fetch_plugin_with_resolution",
            return_value=(marketplace_root, marketplace_commit),
        ):
            fetched_marketplace = MarketplaceRegistry(
                [registration]
            ).get_marketplace_with_resolution("governed")
        assert fetched_marketplace.resolved_ref == marketplace_commit
        marketplace_plugin = fetched_marketplace.marketplace.get_plugin("review")
        assert marketplace_plugin is not None
        assert fetched_marketplace.marketplace.resolve_plugin_source(
            marketplace_plugin
        ) == (
            str(marketplace_root / "plugins/review"),
            None,
            None,
        )
    assert set(list_registered_tools()) == set(EXPECTED_TOOL_MODULES)
    assert get_tool_module_qualnames() == EXPECTED_TOOL_MODULES
    assert "tool_module_qualnames" in StartConversationRequest.model_fields
    tool_modules_field = StartConversationRequest.model_fields["tool_module_qualnames"]
    assert tool_modules_field.default_factory is dict
    assert tool_modules_field.get_default(call_default_factory=True) == {}
    explicit_concurrency = StartConversationRequest.model_validate(
        {
            "workspace": {"kind": "LocalWorkspace", "working_dir": "/tmp"},
            "agent": {
                "kind": "Agent",
                "llm": {"model": "openai/test", "api_key": "contract-only"},
                "tools": [{"name": "grep", "params": {}}],
                "tool_concurrency_limit": 4,
            },
        }
    )
    assert explicit_concurrency.agent.tool_concurrency_limit == 4
    assert explicit_concurrency.tool_module_qualnames == {}
    assert ParallelToolExecutor._resolve_lock_keys(  # noqa: SLF001
        DeclaredResources(keys=(), declared=False),
        SimpleNamespace(name="undeclared"),  # type: ignore[arg-type]
    ) == ["tool:undeclared"]
    assert (
        ParallelToolExecutor._resolve_lock_keys(  # noqa: SLF001
            DeclaredResources(keys=(), declared=True),
            SimpleNamespace(name="read-only"),  # type: ignore[arg-type]
        )
        == []
    )
    assert ParallelToolExecutor._resolve_lock_keys(  # noqa: SLF001
        DeclaredResources(keys=("file:/tmp/a",), declared=True),
        SimpleNamespace(name="file-editor"),  # type: ignore[arg-type]
    ) == ["file:/tmp/a"]

    assert _field_default(ForkConversationRequest, "from_event_id") is None
    assert _field_default(ForkConversationRequest, "reset_metrics") is True
    assert _field_default(NavigateConversationRequest, "event_id") is None

    goal_request_fields = StartGoalRequest.model_fields
    assert set(goal_request_fields) == {"objective", "max_iterations"}
    assert goal_request_fields["objective"].is_required()
    assert _field_default(StartGoalRequest, "max_iterations") == 10
    assert set(GoalStatus.model_fields) == {
        "active",
        "status",
        "iteration",
        "max_iterations",
        "objective",
        "verdict",
    }
    assert _field_default(GoalStatus, "verdict") is None
    assert set(GoalVerdict.model_fields) == {"score", "complete", "missing"}
    assert _field_default(GoalVerdict, "missing") == ""
    goal_event = ConversationStateUpdateEvent(
        key="goal",
        value=GoalStatus(
            active=True,
            status="running",
            iteration=0,
            max_iterations=2,
            objective="Verify the governed result",
        ).model_dump(),
    )
    assert (
        goal_event.model_dump(mode="json", exclude_none=True)["value"]["status"]
        == "running"
    )

    agent_context_fields = AgentContext.model_fields
    assert {"load_memory", "memory_context"} <= set(agent_context_fields)
    assert _field_default(AgentContext, "load_memory") is False
    assert _field_default(AgentContext, "memory_context") is None
    assert MEMORY_INDEX_RELPATH == ".openhands/memory/MEMORY.md"
    assert MEMORY_CHAR_BUDGET == 6000
    # The public switch loads both ambient user memory and workspace memory.
    # It has no tier selector or strict failure mode, so FlowWeave cannot use
    # it until governed content is isolated and its materialization is verified
    # before conversation startup.
    assert not {"load_user_memory", "load_project_memory"} & set(agent_context_fields)
    with TemporaryDirectory() as directory:
        memory_root = Path(directory)
        user_root = memory_root / "user"
        workspace_root = memory_root / "workspace"
        user_index = user_root / MEMORY_INDEX_RELPATH
        project_index = workspace_root / MEMORY_INDEX_RELPATH
        user_index.parent.mkdir(parents=True)
        project_index.parent.mkdir(parents=True)
        user_index.write_text("ambient user fact", encoding="utf-8")
        project_index.write_text("governed project fact", encoding="utf-8")
        with patch("pathlib.Path.home", return_value=user_root):
            combined_memory = load_memory(workspace_root)
        assert combined_memory is not None
        assert "# User memory" in combined_memory
        assert "ambient user fact" in combined_memory
        assert "# Project memory" in combined_memory
        assert "governed project fact" in combined_memory

        user_index.write_bytes(b"\xff")
        project_index.unlink()
        with patch("pathlib.Path.home", return_value=user_root):
            unreadable_memory = load_memory(workspace_root)
        assert unreadable_memory is None

    assert "critic_result" in ActionEvent.model_fields
    assert "critic_result" in MessageEvent.model_fields
    assert _field_default(ActionEvent, "critic_result") is None
    assert _field_default(MessageEvent, "critic_result") is None
    assert set(CriticResult.model_fields) == {"score", "message", "metadata"}
    assert _field_default(CriticResult, "metadata") is None
    assert set(IterativeRefinementConfig.model_fields) == {
        "success_threshold",
        "max_iterations",
    }
    assert _field_default(IterativeRefinementConfig, "success_threshold") == 0.6
    assert _field_default(IterativeRefinementConfig, "max_iterations") == 3
    try:
        IterativeRefinementConfig(max_iterations=0)
    except ValueError:
        pass
    else:
        raise AssertionError(
            "OpenHands iterative refinement unexpectedly accepts zero iterations"
        )
    critic = AgentFinishedCritic(
        iterative_refinement=IterativeRefinementConfig(
            success_threshold=0.7, max_iterations=2
        )
    )
    assert critic.model_dump(mode="json", exclude_none=True) == {
        "kind": "AgentFinishedCritic",
        "mode": "finish_and_message",
        "iterative_refinement": {
            "success_threshold": 0.7,
            "max_iterations": 2,
        },
    }

    task_action_fields = TaskAction.model_fields
    assert set(task_action_fields) == {
        "description",
        "prompt",
        "subagent_type",
        "resume",
        "max_turns",
    }
    assert task_action_fields["prompt"].is_required()
    assert _field_default(TaskAction, "description") is None
    assert _field_default(TaskAction, "subagent_type") == "general-purpose"
    assert _field_default(TaskAction, "resume") is None
    assert _field_default(TaskAction, "max_turns") is None

    task_observation_fields = TaskObservation.model_fields
    assert set(task_observation_fields) == {
        "content",
        "is_error",
        "task_id",
        "subagent",
        "status",
    }
    for field in ("task_id", "subagent", "status"):
        assert task_observation_fields[field].is_required()
    assert set(ConversationStats.model_fields) == {"usage_to_metrics"}

    # TaskToolSet is a synchronous, in-process executor in this source. Its only
    # terminal states are completed/error; there is no cancellable child handle.
    assert set(TaskStatus) == {
        TaskStatus.RUNNING,
        TaskStatus.COMPLETED,
        TaskStatus.ERROR,
    }
    assert set(Task.model_fields) == {
        "id",
        "status",
        "conversation_id",
        "result",
        "error",
        "conversation",
    }
    assert not any(
        hasattr(TaskManager, name)
        for name in ("cancel_task", "interrupt_task", "pause_task")
    )
    assert list(signature(TaskExecutor.__call__).parameters) == [
        "self",
        "action",
        "conversation",
    ]
    assert signature(TaskExecutor.__call__).return_annotation in {
        "TaskObservation",
        TaskObservation,
    }
    # Parent interrupt cancels the async wrapper, but a Task tool already
    # executing in its worker thread has no child cancellation contract.
    # TaskExecutor inherits ToolExecutor's no-op interrupt, so parent PAUSED is
    # not evidence that the synchronous child stopped.
    assert TaskExecutor.interrupt is ToolExecutor.interrupt
    task_started = Event()
    release_task = Event()

    class BlockingTaskManager:
        def start_task(self, **_kwargs: object) -> SimpleNamespace:
            task_started.set()
            assert release_task.wait(timeout=5)
            return SimpleNamespace(
                id="task_blocking",
                status=TaskStatus.COMPLETED,
                result="released",
                error=None,
            )

        def close(self) -> None:
            pass

    blocking_executor = TaskExecutor(BlockingTaskManager())  # type: ignore[arg-type]
    blocking_thread = Thread(
        target=blocking_executor,
        args=(TaskAction(prompt="block until released"),),
        daemon=True,
    )
    blocking_thread.start()
    assert task_started.wait(timeout=5)
    blocking_executor.interrupt()
    assert blocking_thread.is_alive()
    release_task.set()
    blocking_thread.join(timeout=5)
    assert not blocking_thread.is_alive()
    task_tool_create = signature(TaskToolSet.create).parameters
    assert list(task_tool_create) == ["conv_state", "confirmation_handler"]
    assert task_tool_create["confirmation_handler"].default is None
    # Agent Server resolves the governed, serializable Tool spec through the
    # public registry. With no private params, that exact production path calls
    # TaskToolSet.create(conv_state=...) and therefore creates a manager with no
    # confirmation callback. There is no HTTP identity that could supply the
    # in-process callable later while a child is waiting.
    task_tool_spec = Tool(name=TaskToolSet.name)
    assert task_tool_spec.model_dump(mode="json") == {
        "name": "task_tool_set",
        "params": {},
    }
    resolved_task_tools = resolve_tool(task_tool_spec, SimpleNamespace())  # type: ignore[arg-type]
    assert len(resolved_task_tools) == 1
    resolved_task_executor = resolved_task_tools[0].executor
    assert isinstance(resolved_task_executor, TaskExecutor)
    assert resolved_task_executor._manager._confirmation_handler is None  # noqa: SLF001
    # The target source persists child conversation files under the parent's
    # ``subagents`` directory, but a fresh manager does not rebuild its task-id
    # index from that directory.  Existing durable child data therefore does
    # not constitute a service-restart resume contract.
    with TemporaryDirectory() as parent_persistence_dir:
        persisted_subagent_dir = (
            Path(parent_persistence_dir) / "subagents" / "persisted-child"
        )
        persisted_subagent_dir.mkdir(parents=True)
        (persisted_subagent_dir / "events.jsonl").write_text(
            "persisted child state\n", encoding="utf-8"
        )
        restarted_manager = TaskManager()
        restarted_manager.attach_parent(
            SimpleNamespace(
                state=SimpleNamespace(persistence_dir=Path(parent_persistence_dir))
            )  # type: ignore[arg-type]
        )
        assert restarted_manager._persistence_dir == (  # noqa: SLF001
            Path(parent_persistence_dir) / "subagents"
        )
        assert persisted_subagent_dir.exists()
        try:
            restarted_manager._resume_task(  # noqa: SLF001
                resume="task_00000001",
                subagent_type="reviewer",
            )
        except ValueError as exc:
            assert "Task 'task_00000001' not found" in str(exc)
        else:
            raise AssertionError(
                "a restarted TaskManager unexpectedly restored a Task identity"
            )

    class CompletedTaskManager:
        def start_task(self, **kwargs: object) -> SimpleNamespace:
            assert kwargs == {
                "prompt": "inspect the governed workspace",
                "subagent_type": "reviewer",
                "description": "inspect workspace",
                "resume": None,
                "conversation": None,
            }
            return SimpleNamespace(
                id="task_00000001",
                status=TaskStatus.COMPLETED,
                result="review complete",
                error=None,
            )

        def close(self) -> None:
            pass

    completed_observation = TaskExecutor(CompletedTaskManager())(  # type: ignore[arg-type]
        TaskAction(
            prompt="inspect the governed workspace",
            subagent_type="reviewer",
            description="inspect workspace",
        )
    )
    completed_payload = completed_observation.model_dump(mode="json")
    assert completed_payload["is_error"] is False
    assert completed_payload["task_id"] == "task_00000001"
    assert completed_payload["subagent"] == "reviewer"
    assert completed_payload["status"] == "completed"
    assert len(completed_payload["content"]) == 1
    assert completed_payload["content"][0]["type"] == "text"
    assert completed_payload["content"][0]["text"] == "review complete"

    # Confirmation is an internal synchronous callback.  The Task does not
    # expose a pending child identity through Agent Server HTTP while waiting.
    confirmation_calls: list[tuple[str, list[object]]] = []

    def approve_task(task_id: str, pending: list[object]) -> bool:
        confirmation_calls.append((task_id, pending))
        return True

    confirmation_manager = TaskManager(confirmation_handler=approve_task)  # type: ignore[arg-type]

    class ConfirmationConversation:
        def __init__(self) -> None:
            self.state = SimpleNamespace(
                execution_status=ConversationExecutionStatus.WAITING_FOR_CONFIRMATION,
                events=[],
            )
            self.run_count = 0

        def run(self) -> None:
            self.run_count += 1
            if self.run_count == 2:
                self.state.execution_status = ConversationExecutionStatus.FINISHED

        def reject_pending_actions(self, _reason: str) -> None:
            raise AssertionError("approved Task confirmation must not be rejected")

    confirmation_conversation = ConfirmationConversation()
    pending_action = object()
    with patch.object(
        ConversationState,
        "get_unmatched_actions",
        return_value=[pending_action],
    ):
        confirmation_manager._run_until_finished(  # noqa: SLF001
            "task_00000002",
            confirmation_conversation,  # type: ignore[arg-type]
        )
    assert confirmation_conversation.run_count == 2
    assert confirmation_calls == [("task_00000002", [pending_action])]

    automatic_conversation = ConfirmationConversation()
    with patch.object(
        ConversationState,
        "get_unmatched_actions",
        return_value=[pending_action],
    ):
        TaskManager()._run_until_finished(  # noqa: SLF001
            "task_00000004",
            automatic_conversation,  # type: ignore[arg-type]
        )
    assert automatic_conversation.run_count == 2

    # A child has independent metrics while running, but this source replaces its
    # cumulative total under task:<id> in the parent stats before eviction.
    parent_stats = ConversationStats()
    child_stats = ConversationStats()
    child_metrics = Metrics(model_name="contract-model")
    child_metrics.add_cost(0.25)
    child_metrics.add_token_usage(3, 2, 0, 0, 100, "child-response-1")
    child_stats.usage_to_metrics["child-agent"] = child_metrics
    metrics_task = Task.model_construct(
        id="task_00000003",
        status=TaskStatus.COMPLETED,
        conversation_id=uuid4(),
        conversation=SimpleNamespace(conversation_stats=child_stats),
    )
    TaskManager()._update_parent_metrics(  # noqa: SLF001
        SimpleNamespace(conversation_stats=parent_stats),  # type: ignore[arg-type]
        metrics_task,
    )
    attributed = parent_stats.usage_to_metrics["task:task_00000003"]
    assert attributed.accumulated_cost == 0.25
    assert attributed.accumulated_token_usage is not None
    assert attributed.accumulated_token_usage.prompt_tokens == 3
    child_metrics.add_cost(0.5)
    TaskManager()._update_parent_metrics(  # noqa: SLF001
        SimpleNamespace(conversation_stats=parent_stats),  # type: ignore[arg-type]
        metrics_task,
    )
    assert parent_stats.usage_to_metrics["task:task_00000003"].accumulated_cost == 0.75

    governed_skill = Skill(
        name="flowweave-review",
        content="Review the governed change.",
        description="Review a governed change",
        source=None,
        trigger=KeywordTrigger(keywords=["$flowweave-review"]),
        is_agentskills_format=True,
    )
    governed_skill_context = AgentContext(skills=[governed_skill])
    triggered = governed_skill_context.get_user_message_suffix(
        Message(
            role="user",
            content=[TextContent(text="$flowweave-review inspect this patch")],
        ),
        skip_skill_names=[],
    )
    assert triggered is not None
    triggered_content, activated_skill_names = triggered
    assert activated_skill_names == ["flowweave-review"]
    assert "Review the governed change." in triggered_content.text

    invoked_skills: list[str] = []
    invocation_conversation = SimpleNamespace(
        state=SimpleNamespace(
            agent=SimpleNamespace(agent_context=governed_skill_context),
            workspace=SimpleNamespace(working_dir=None),
            invoked_skills=invoked_skills,
        )
    )
    invoke_executor = InvokeSkillExecutor()
    invoked = invoke_executor(
        InvokeSkillAction(name="flowweave-review"),
        invocation_conversation,
    )
    assert invoked.is_error is False
    assert invoked.skill_name == "flowweave-review"
    assert "Review the governed change." in invoked.text
    invoke_executor(
        InvokeSkillAction(name="flowweave-review"),
        invocation_conversation,
    )
    assert invoked_skills == ["flowweave-review"]
    assert InvokeSkillTool not in BUILT_IN_TOOLS

    agent_definition_fields = AgentDefinition.model_fields
    required_agent_definition_fields = {
        "name",
        "description",
        "model",
        "tools",
        "skills",
        "system_prompt",
        "when_to_use_examples",
        "permission_mode",
        "max_iteration_per_run",
        "max_budget_per_run",
        "condenser",
        "metadata",
    }
    assert not required_agent_definition_fields - set(agent_definition_fields)
    assert _field_default(AgentDefinition, "model") == "inherit"
    assert _field_default(AgentDefinition, "permission_mode") is None
    assert _field_default(AgentDefinition, "max_iteration_per_run") is None
    assert _field_default(AgentDefinition, "max_budget_per_run") is None
    governed_definition = AgentDefinition.model_validate(
        {
            "name": "flowweave-contract-reviewer",
            "description": "Review a governed change",
            "model": "inherit",
            "tools": ["terminal", "grep"],
            "skills": [],
            "system_prompt": "Review carefully.",
            "when_to_use_examples": ["Review a proposed patch"],
            "permission_mode": "confirm_risky",
            "max_iteration_per_run": 20,
            "max_budget_per_run": 1.5,
            "condenser": {"kind": "NoOpCondenser"},
            "metadata": {},
        }
    )
    assert governed_definition.model_dump(mode="json")["condenser"] == {
        "kind": "NoOpCondenser"
    }

    expected_condenser_defaults = {
        "max_size": 240,
        "max_tokens": None,
        "keep_first": 2,
        "minimum_progress": 0.1,
        "hard_context_reset_max_retries": 5,
        "hard_context_reset_context_scaling": 0.8,
    }
    actual_condenser_defaults = {
        name: _field_default(LLMSummarizingCondenser, name)
        for name in expected_condenser_defaults
    }
    assert actual_condenser_defaults == expected_condenser_defaults

    # Importing these public types is itself part of the frozen source contract.
    native_types = (
        NoOpCondenser,
        PipelineCondenser,
        CondensationRequest,
        Condensation,
        AgentDefinition,
        TaskAction,
        TaskObservation,
        TaskToolSet,
        Task,
        TaskManager,
        TaskExecutor,
        InvokeSkillAction,
        InvokeSkillTool,
        KeywordTrigger,
        Skill,
        PluginSource,
        MarketplaceRegistration,
        MarketplaceRegistry,
        GoalStatus,
        GoalVerdict,
        AgentFinishedCritic,
        CriticResult,
        IterativeRefinementConfig,
        ConversationStateUpdateEvent,
        OpenHandsAgentProfile,
        LaunchedAgentProfile,
        ProfileVerificationSettings,
    )
    assert all(value.__module__.startswith("openhands.") for value in native_types)
    assert Plugin.__module__ == "openhands.sdk.plugin.plugin"
    assert fetch_plugin_with_resolution.__module__ == "openhands.sdk.plugin.fetch"

    print(
        json.dumps(
            {
                "status": "ok",
                "versions": versions,
                "source_kind": build_input["source_kind"],
                "source_commit": build_input["source_commit"],
                "source_archive_sha256": provenance["source_archive_sha256"],
                "source_direct_urls": direct_urls,
                "server_info_source_commit": server_info.build_git_sha,
                "server_info_capabilities": sorted(server_info.capabilities),
                "required_path_count": len(REQUIRED_PATHS),
                "required_websocket_paths": sorted(REQUIRED_WEBSOCKET_PATHS),
                "conversation_websocket_since_replay": True,
                "bash_websocket_since_replay": False,
                "bash_rest_compensation": True,
                "start_field_count": len(start_fields),
                "tool_count": len(EXPECTED_TOOL_MODULES),
                "agent_definition_field_count": len(agent_definition_fields),
                "agent_profile_field_count": len(agent_profile_fields),
                "agent_profile_http_methods": profile_http_methods,
                "task_action_fields": sorted(task_action_fields),
                "task_observation_fields": sorted(task_observation_fields),
                "task_http_paths": task_http_paths,
                "task_statuses": sorted(status.value for status in TaskStatus),
                "task_metrics_key": "task:<task_id>",
                "parent_interrupt_stops_started_task_tool": False,
                "task_registry_confirmation_handler": False,
                "task_restart_resume_identity_restored": False,
                "native_skill_activation": activated_skill_names,
                "native_skill_invocation": invoked_skills,
                "goal_http_paths": goal_http_paths,
                "memory_index_relpath": MEMORY_INDEX_RELPATH,
                "memory_char_budget": MEMORY_CHAR_BUDGET,
                "memory_tiers_independently_selectable": False,
                "memory_read_failure_is_fatal": False,
                "condenser_defaults": actual_condenser_defaults,
            },
            sort_keys=True,
        )
    )


if __name__ == "__main__":
    main()
