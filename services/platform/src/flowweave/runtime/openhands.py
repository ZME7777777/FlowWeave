from __future__ import annotations

import hashlib
import json
import math
import re
import time
from collections.abc import AsyncIterator
from pathlib import Path, PurePosixPath
from typing import Any, Literal, cast
from urllib.parse import urlparse
from uuid import UUID

import httpx
from websockets.asyncio.client import connect
from websockets.exceptions import ConnectionClosed, InvalidHandshake
from websockets.sync.client import connect as sync_connect

from flowweave.bootstrap.settings import Settings
from flowweave.runtime.auth import derive_runtime_session_key
from flowweave.runtime.base import (
    RuntimeAskAgentResult,
    RuntimeCondenser,
    RuntimeContract,
    RuntimeConversationIdentity,
    RuntimeEvent,
    RuntimeEventBatch,
    RuntimeEventType,
    RuntimeForkResult,
    RuntimeHandle,
    RuntimeMCP,
    RuntimeMCPOAuthCallbackRequest,
    RuntimeMCPOAuthJobRequest,
    RuntimeMCPOAuthStartRequest,
    RuntimeMCPOAuthStatus,
    RuntimeMCPProbeRequest,
    RuntimeMCPProbeResult,
    RuntimePendingAction,
    RuntimePendingConfirmation,
    RuntimePluginValidationRequest,
    RuntimePluginValidationResult,
    RuntimeProvider,
    RuntimeResult,
    RuntimeTaskUsageSnapshot,
    RuntimeUsageSnapshot,
    RuntimeWakeup,
    StartAttemptRequest,
)
from flowweave.shared.errors import DomainError
from flowweave.shared.infrastructure.docker_controller import (
    DockerControllerClient,
    DockerControllerError,
    controller_is_remote,
    validate_owned_runtime_plugin,
)


class OpenHandsRuntime:
    """OpenHands Agent Server adapter backed by the node's configured model provider."""

    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.base_url = settings.openhands_base_url.rstrip("/")
        self.root_session_api_key = settings.openhands_session_api_key
        self.manager_scope = settings.sandbox_manager_scope
        self.workspace_root = settings.workspace_root.resolve()
        self.openhands_workspace_root = settings.openhands_workspace_root
        self._contracts: dict[str, list[dict[str, str]]] = {}

    @staticmethod
    def _environment_route(job_id: str) -> tuple[str, bool] | None:
        for prefix, disposable in (("env-exec:", True), ("env-chat:", False)):
            if job_id.startswith(prefix):
                container_name = job_id.removeprefix(prefix)
                if container_name:
                    return container_name, disposable
        return None

    def _base_url_for_handle(self, handle: RuntimeHandle) -> str:
        route = self._environment_route(handle.job_id)
        return f"http://{route[0]}:8000" if route else self.base_url

    def _session_key_for_resource(self, resource_name: str | None) -> str:
        if not resource_name:
            return self.root_session_api_key
        return derive_runtime_session_key(
            self.root_session_api_key, self.manager_scope, resource_name
        )

    def _session_key_for_handle(self, handle: RuntimeHandle) -> str:
        route = self._environment_route(handle.job_id)
        return self._session_key_for_resource(route[0] if route else None)

    def _request(
        self,
        method: str,
        path: str,
        *,
        missing_ok: bool = False,
        base_url: str | None = None,
        session_api_key: str | None = None,
        **kwargs: Any,
    ) -> dict[str, Any]:
        try:
            with httpx.Client(timeout=30, follow_redirects=False) as client:
                response = client.request(
                    method,
                    f"{base_url or self.base_url}{path}",
                    headers={"X-Session-API-Key": session_api_key or self.root_session_api_key},
                    **kwargs,
                )
                if missing_ok and response.status_code == 404:
                    return {"_flowweave_missing": True}
                response.raise_for_status()
                value = cast(object, response.json())
                if not isinstance(value, dict):
                    raise ValueError("OpenHands response must be an object")
                return cast(dict[str, Any], value)
        except httpx.HTTPStatusError as exc:
            if exc.response.status_code == 404 and path.startswith("/api/conversations/"):
                raise DomainError(
                    "RUNTIME_CONVERSATION_MISSING",
                    "The original OpenHands Conversation is unavailable and cannot be replaced",
                    409,
                    {"status_code": 404, "path": path},
                ) from exc
            raise DomainError(
                "EXECUTOR_UNAVAILABLE",
                "OpenHands Agent Server is unavailable or rejected the request",
                503,
                {"status_code": exc.response.status_code},
            ) from exc
        except (httpx.HTTPError, ValueError) as exc:
            raise DomainError(
                "EXECUTOR_UNAVAILABLE",
                "OpenHands Agent Server is unavailable or rejected the request",
                503,
            ) from exc

    @staticmethod
    def _incompatible(reason: str, **details: object) -> DomainError:
        return DomainError(
            "RUNTIME_CONTRACT_INCOMPATIBLE",
            "OpenHands Agent Server does not satisfy the frozen Runtime contract",
            409,
            {"reason": reason, **details},
        )

    @classmethod
    def _validate_runtime_contract(
        cls,
        contract: RuntimeContract,
        *,
        ready: dict[str, Any],
        server_info: dict[str, Any],
        openapi: dict[str, Any],
    ) -> None:
        if ready.get("status") != "ready":
            raise cls._incompatible("server_not_ready")

        package_fields = {
            "openhands-agent-server": "version",
            "openhands-sdk": "sdk_version",
            "openhands-tools": "tools_version",
            "openhands-workspace": "workspace_version",
        }
        expected_packages = dict(contract.package_versions)
        actual_packages = {
            package: server_info.get(field) for package, field in package_fields.items()
        }
        if actual_packages != expected_packages:
            raise cls._incompatible(
                "package_version_mismatch",
                expected=expected_packages,
                actual=actual_packages,
            )
        if server_info.get("build_git_sha") != contract.source_commit:
            raise cls._incompatible(
                "source_commit_mismatch",
                expected=contract.source_commit,
                actual=server_info.get("build_git_sha"),
            )
        if server_info.get("build_git_ref") != contract.source_ref:
            raise cls._incompatible(
                "source_ref_mismatch",
                expected=contract.source_ref,
                actual=server_info.get("build_git_ref"),
            )

        raw_capabilities: object = server_info.get("capabilities")
        raw_tools: object = server_info.get("usable_tools")
        if not isinstance(raw_capabilities, list):
            raise cls._incompatible("invalid_server_capabilities")
        capability_values = cast(list[object], raw_capabilities)
        if any(not isinstance(item, str) or not item for item in capability_values):
            raise cls._incompatible("invalid_server_capabilities")
        capabilities = cast(list[str], capability_values)
        if len(set(capabilities)) != len(capabilities):
            raise cls._incompatible("invalid_server_capabilities")
        if not isinstance(raw_tools, list):
            raise cls._incompatible("invalid_usable_tools")
        tool_values = cast(list[object], raw_tools)
        if any(not isinstance(item, str) or not item for item in tool_values):
            raise cls._incompatible("invalid_usable_tools")
        usable_tools = cast(list[str], tool_values)
        if len(set(usable_tools)) != len(usable_tools):
            raise cls._incompatible("invalid_usable_tools")
        missing_capabilities = sorted(
            set(contract.required_server_capabilities) - set(capabilities)
        )
        missing_tools = sorted(set(contract.required_tools) - set(usable_tools))
        if missing_capabilities or missing_tools:
            raise cls._incompatible(
                "missing_capabilities",
                missing_server_capabilities=missing_capabilities,
                missing_tools=missing_tools,
            )

        raw_paths = openapi.get("paths")
        if not isinstance(raw_paths, dict):
            raise cls._incompatible("invalid_openapi_paths")
        paths = cast(dict[object, object], raw_paths)
        missing_operations: list[dict[str, str]] = []
        for method, path in contract.required_http_operations:
            raw_operation = paths.get(path)
            if not isinstance(raw_operation, dict) or method.lower() not in raw_operation:
                missing_operations.append({"method": method, "path": path})
        if missing_operations:
            raise cls._incompatible(
                "missing_http_operations", missing_operations=missing_operations
            )

        try:
            create_operation = cast(
                dict[str, Any], cast(dict[str, Any], paths["/api/conversations"])["post"]
            )
            request_schema = create_operation["requestBody"]["content"]["application/json"][
                "schema"
            ]
            schemas = openapi["components"]["schemas"]
            start_schema = schemas["StartConversationRequest"]
            start_fields: object = start_schema["properties"]
        except (KeyError, TypeError):
            raise cls._incompatible("invalid_start_conversation_schema") from None
        if (
            not isinstance(request_schema, dict)
            or request_schema.get("$ref") != "#/components/schemas/StartConversationRequest"
        ):
            raise cls._incompatible("invalid_start_conversation_schema")
        if not isinstance(start_fields, dict):
            raise cls._incompatible("invalid_start_conversation_schema")
        start_properties = cast(dict[object, object], start_fields)
        missing_fields = sorted(
            set(contract.required_start_fields) - {str(field) for field in start_properties}
        )
        if missing_fields:
            raise cls._incompatible(
                "missing_start_conversation_fields", missing_fields=missing_fields
            )

    def _negotiate_runtime_contract(
        self,
        contract: RuntimeContract | None,
        *,
        required_tools: tuple[str, ...],
        base_url: str,
        session_api_key: str,
    ) -> None:
        if contract is None:
            raise self._incompatible("snapshot_contract_missing")
        if tuple(sorted(set(required_tools))) != contract.required_tools:
            raise self._incompatible(
                "snapshot_tool_contract_mismatch",
                expected=list(contract.required_tools),
                actual=sorted(set(required_tools)),
            )
        ready = self._request("GET", "/ready", base_url=base_url, session_api_key=session_api_key)
        server_info = self._request(
            "GET", "/server_info", base_url=base_url, session_api_key=session_api_key
        )
        openapi = self._request(
            "GET", "/openapi.json", base_url=base_url, session_api_key=session_api_key
        )
        self._validate_runtime_contract(
            contract, ready=ready, server_info=server_info, openapi=openapi
        )

    def probe_mcp(self, request: RuntimeMCPProbeRequest) -> RuntimeMCPProbeResult:
        """Probe one MCP server through the target environment's Agent Server."""

        config = dict(request.server.config)
        transport = str(config.pop("transport", config.pop("type", "")) or "")
        if not transport:
            transport = "stdio" if config.get("command") else "streamable-http"
        server: dict[str, Any] = {
            key: value
            for key, value in config.items()
            if key
            in {
                "command",
                "args",
                "env",
                "cwd",
                "url",
                "headers",
                "auth",
                "timeout",
                "sse_read_timeout",
                "keep_alive",
            }
        }
        server["type"] = transport
        if request.oauth_secret_reference_id is not None:
            raw_auth = server.get("auth")
            if not isinstance(raw_auth, dict):
                raise DomainError(
                    "MCP_OAUTH_PROTOCOL_ERROR",
                    "Governed OAuth state requires auth.strategy=oauth2",
                    422,
                )
            auth = cast(dict[str, Any], raw_auth)
            if auth.get("strategy") != "oauth2":
                raise DomainError(
                    "MCP_OAUTH_PROTOCOL_ERROR",
                    "Governed OAuth state requires auth.strategy=oauth2",
                    422,
                )
            oauth_auth = dict(auth)
            if request.oauth_state is not None:
                oauth_auth["state"] = request.oauth_state
            server["auth"] = oauth_auth
        payload: dict[str, Any] = {
            "name": request.server.name,
            "server": server,
            "timeout": request.timeout,
        }
        if request.read_only_tool_call is not None:
            payload["tool_call"] = {
                "name": request.read_only_tool_call.name,
                "arguments": dict(request.read_only_tool_call.arguments),
            }
        response = self._request(
            "POST",
            "/api/mcp/test",
            base_url=request.base_url,
            session_api_key=self._session_key_for_resource(request.runtime_resource_name),
            json=payload,
        )
        raw_oauth_state = response.get("oauth_state")
        if raw_oauth_state is not None and request.oauth_secret_reference_id is None:
            raise DomainError(
                "MCP_OAUTH_LIFECYCLE_REQUIRED",
                "MCP OAuth state requires Secret Reference governance",
                422,
            )
        if raw_oauth_state is not None and not isinstance(raw_oauth_state, dict):
            raise DomainError(
                "RUNTIME_PROTOCOL_ERROR",
                "OpenHands returned invalid MCP OAuth state",
                502,
            )
        ok = response.get("ok")
        if ok is False:
            error_kind = str(response.get("error_kind") or "unknown")
            if error_kind not in {"timeout", "connection", "unknown"}:
                raise DomainError(
                    "RUNTIME_PROTOCOL_ERROR",
                    "OpenHands returned an invalid MCP probe error kind",
                    502,
                )
            return RuntimeMCPProbeResult(
                ok=False,
                error_kind=cast(Literal["timeout", "connection", "unknown"], error_kind),
            )
        if ok is not True:
            raise DomainError(
                "RUNTIME_PROTOCOL_ERROR",
                "OpenHands returned an invalid MCP probe response",
                502,
            )
        raw_tools: object = response.get("tools")
        if not isinstance(raw_tools, list):
            raise DomainError(
                "RUNTIME_PROTOCOL_ERROR",
                "OpenHands MCP probe omitted the Tool catalog",
                502,
            )
        tool_names: list[str] = []
        for raw_tool in cast(list[object], raw_tools):
            if not isinstance(raw_tool, str):
                raise DomainError(
                    "RUNTIME_PROTOCOL_ERROR",
                    "OpenHands MCP probe omitted the Tool catalog",
                    502,
                )
            tool_names.append(raw_tool)
        tool_result: object = response.get("tool_result")
        tool_call_is_error: bool | None = None
        tool_call_text: str | None = None
        if tool_result is not None:
            if not isinstance(tool_result, dict):
                raise DomainError(
                    "RUNTIME_PROTOCOL_ERROR",
                    "OpenHands returned an invalid MCP Tool result",
                    502,
                )
            tool_result_mapping = cast(dict[object, object], tool_result)
            raw_is_error = tool_result_mapping.get("is_error")
            raw_text = tool_result_mapping.get("text")
            if not isinstance(raw_is_error, bool) or not isinstance(raw_text, str):
                raise DomainError(
                    "RUNTIME_PROTOCOL_ERROR",
                    "OpenHands returned an invalid MCP Tool result",
                    502,
                )
            tool_call_is_error = raw_is_error
            tool_call_text = raw_text
        return RuntimeMCPProbeResult(
            ok=True,
            tools=tuple(dict.fromkeys(tool_names)),
            tool_call_is_error=tool_call_is_error,
            tool_call_text=tool_call_text,
            oauth_state=(
                cast(dict[str, Any], raw_oauth_state) if isinstance(raw_oauth_state, dict) else None
            ),
        )

    def validate_plugin(
        self, request: RuntimePluginValidationRequest
    ) -> RuntimePluginValidationResult:
        """Run the native Plugin loader through one ownership-checked Runtime."""

        try:
            if controller_is_remote(self.settings):
                response = DockerControllerClient(self.settings).post(
                    "/v1/runtimes/validate-plugin",
                    {
                        "resource_name": request.runtime_resource_name,
                        "resource_id": request.runtime_resource_id,
                        "validation_id": request.validation_id,
                        "plugin_path": request.plugin.source,
                    },
                    timeout=30,
                )
            else:
                response = validate_owned_runtime_plugin(
                    self.settings,
                    resource_name=request.runtime_resource_name,
                    resource_id=request.runtime_resource_id,
                    validation_id=request.validation_id,
                    plugin_path=request.plugin.source,
                )
        except DomainError:
            raise
        except Exception as exc:
            raise DomainError(
                "EXECUTOR_UNAVAILABLE",
                "The target Runtime Plugin loader is unavailable",
                503,
            ) from exc
        fields = {
            "plugin_name": str,
            "plugin_version": str,
            "skill_count": int,
            "command_count": int,
            "agent_count": int,
            "mcp_server_count": int,
            "has_hooks": bool,
        }
        if any(
            key not in response
            or isinstance(response[key], bool) != (expected is bool)
            or not isinstance(response[key], expected)
            for key, expected in fields.items()
        ):
            raise DomainError(
                "RUNTIME_PROTOCOL_ERROR",
                "The target Runtime returned an invalid Plugin loader report",
                502,
            )
        counts = (
            response["skill_count"],
            response["command_count"],
            response["agent_count"],
            response["mcp_server_count"],
        )
        if any(value < 0 for value in counts):
            raise DomainError(
                "RUNTIME_PROTOCOL_ERROR",
                "The target Runtime returned an invalid Plugin loader report",
                502,
            )
        return RuntimePluginValidationResult(
            plugin_name=response["plugin_name"],
            plugin_version=response["plugin_version"],
            skill_count=response["skill_count"],
            command_count=response["command_count"],
            agent_count=response["agent_count"],
            mcp_server_count=response["mcp_server_count"],
            has_hooks=response["has_hooks"],
        )

    @staticmethod
    def _mcp_server_payload(server: RuntimeMCP) -> dict[str, Any]:
        config = dict(server.config)
        transport = str(config.pop("transport", config.pop("type", "")) or "")
        if not transport:
            transport = "stdio" if config.get("command") else "streamable-http"
        payload = {
            key: value
            for key, value in config.items()
            if key
            in {
                "command",
                "args",
                "env",
                "cwd",
                "url",
                "headers",
                "auth",
                "timeout",
                "sse_read_timeout",
                "keep_alive",
            }
        }
        payload["type"] = transport
        return payload

    @staticmethod
    def _oauth_status(response: dict[str, Any]) -> RuntimeMCPOAuthStatus:
        raw_status = response.get("status")
        if raw_status is None and response.get("authorization_url") is not None:
            raw_status = "authorizing"
        if raw_status is None and response.get("ok") is False:
            raw_status = "failed"
        if raw_status not in {"pending", "authorizing", "succeeded", "failed"}:
            raise DomainError(
                "RUNTIME_PROTOCOL_ERROR",
                "OpenHands returned an invalid MCP OAuth status",
                502,
            )
        job_id = response.get("job_id")
        if not isinstance(job_id, str) or not job_id:
            raise DomainError(
                "RUNTIME_PROTOCOL_ERROR",
                "OpenHands omitted the MCP OAuth job identity",
                502,
            )
        raw_url = response.get("authorization_url")
        if raw_url is not None and (not isinstance(raw_url, str) or not raw_url):
            raise DomainError(
                "RUNTIME_PROTOCOL_ERROR",
                "OpenHands returned an invalid MCP OAuth authorization URL",
                502,
            )
        raw_tools = response.get("tools")
        tools: tuple[str, ...] = ()
        if raw_tools is not None:
            if not isinstance(raw_tools, list):
                raise DomainError(
                    "RUNTIME_PROTOCOL_ERROR",
                    "OpenHands returned an invalid MCP OAuth Tool catalog",
                    502,
                )
            raw_tool_items = cast(list[object], raw_tools)
            if not all(isinstance(item, str) for item in raw_tool_items):
                raise DomainError(
                    "RUNTIME_PROTOCOL_ERROR",
                    "OpenHands returned an invalid MCP OAuth Tool catalog",
                    502,
                )
            tools = tuple(dict.fromkeys(item for item in raw_tool_items if isinstance(item, str)))
        raw_state = response.get("oauth_state")
        if raw_state is not None and not isinstance(raw_state, dict):
            raise DomainError(
                "RUNTIME_PROTOCOL_ERROR",
                "OpenHands returned invalid MCP OAuth state",
                502,
            )
        raw_error_kind = response.get("error_kind")
        if raw_error_kind is not None and raw_error_kind not in {
            "timeout",
            "connection",
            "unknown",
        }:
            raise DomainError(
                "RUNTIME_PROTOCOL_ERROR",
                "OpenHands returned an invalid MCP OAuth error kind",
                502,
            )
        return RuntimeMCPOAuthStatus(
            ok=response.get("ok") is True,
            status=cast(
                Literal["pending", "authorizing", "succeeded", "failed"],
                raw_status,
            ),
            job_id=job_id,
            authorization_url=raw_url,
            callback_ready=response.get("callback_ready") is True,
            tools=tools,
            error_kind=cast(
                Literal["timeout", "connection", "unknown"] | None,
                raw_error_kind,
            ),
            oauth_state=(cast(dict[str, Any], raw_state) if isinstance(raw_state, dict) else None),
        )

    def start_mcp_oauth(self, request: RuntimeMCPOAuthStartRequest) -> RuntimeMCPOAuthStatus:
        server = self._mcp_server_payload(request.server)
        raw_auth: object = server.get("auth")
        if not isinstance(raw_auth, dict):
            raise DomainError(
                "MCP_OAUTH_PROTOCOL_ERROR",
                "MCP OAuth start requires auth.strategy=oauth2",
                422,
            )
        auth = cast(dict[str, Any], raw_auth)
        if auth.get("strategy") != "oauth2":
            raise DomainError(
                "MCP_OAUTH_PROTOCOL_ERROR",
                "MCP OAuth start requires auth.strategy=oauth2",
                422,
            )
        if "state" in auth:
            raise DomainError(
                "MCP_OAUTH_PROTOCOL_ERROR",
                "Initial MCP OAuth authorization cannot include stored state",
                422,
            )
        response = self._request(
            "POST",
            "/api/mcp/oauth/start",
            base_url=request.base_url,
            session_api_key=self._session_key_for_resource(request.runtime_resource_name),
            json={
                "name": request.server.name,
                "server": server,
                "timeout": request.timeout,
            },
        )
        return self._oauth_status(response)

    def read_mcp_oauth(self, request: RuntimeMCPOAuthJobRequest) -> RuntimeMCPOAuthStatus:
        response = self._request(
            "GET",
            f"/api/mcp/oauth/status/{request.job_id}",
            base_url=request.base_url,
            session_api_key=self._session_key_for_resource(request.runtime_resource_name),
        )
        return self._oauth_status(response)

    def submit_mcp_oauth_callback(
        self, request: RuntimeMCPOAuthCallbackRequest
    ) -> RuntimeMCPOAuthStatus:
        response = self._request(
            "POST",
            f"/api/mcp/oauth/callback/{request.job_id}",
            base_url=request.base_url,
            session_api_key=self._session_key_for_resource(request.runtime_resource_name),
            json={"callback_url": request.callback_url},
        )
        return self._oauth_status(response)

    @staticmethod
    def _model_name(model: str) -> str:
        return model if "/" in model else f"openai/{model}"

    def _llm_payload(self, provider: RuntimeProvider) -> dict[str, Any]:
        llm: dict[str, Any] = {
            "model": self._model_name(provider.model),
            "base_url": provider.base_url,
            "api_key": provider.api_key,
            "usage_id": f"flowweave:{provider.provider_id}",
        }
        if provider.auth_type == "CODEX_OAUTH":
            extra_body: dict[str, Any] = {"store": False}
            if provider.reasoning_effort:
                extra_body["reasoning"] = {"effort": provider.reasoning_effort}
            llm.update(
                {
                    "api_mode": "responses",
                    "extra_headers": provider.extra_headers,
                    "litellm_extra_body": extra_body,
                    "stream": True,
                    "temperature": None,
                    "max_output_tokens": None,
                    "capability_overrides": {
                        "supports_responses_api": True,
                        "supports_sampling_params": False,
                    },
                }
            )
        return llm

    def _condenser_payload(
        self, condenser: RuntimeCondenser, provider: RuntimeProvider | None
    ) -> dict[str, Any]:
        if condenser.kind == "NO_OP":
            return {"kind": "NoOpCondenser"}
        if condenser.kind != "LLM_SUMMARIZING":
            raise DomainError(
                "RUNTIME_CONFIGURATION_INVALID",
                "Unsupported OpenHands condenser mode",
                422,
                {"kind": condenser.kind},
            )
        if provider is None:
            raise DomainError(
                "RUNTIME_CONFIGURATION_INVALID",
                "LLM summarizing condenser is missing its frozen model provider",
                422,
            )
        llm = self._llm_payload(provider)
        # OpenHands accounts usage by usage_id. Keep summarization separate from
        # the main agent ledger while reusing the frozen provider/model/secret.
        llm["usage_id"] = "condenser"
        payload: dict[str, Any] = {
            "kind": "LLMSummarizingCondenser",
            "llm": llm,
            "max_size": condenser.max_size,
            "keep_first": condenser.keep_first,
            "minimum_progress": condenser.minimum_progress,
            "hard_context_reset_max_retries": (condenser.hard_context_reset_max_retries),
            "hard_context_reset_context_scaling": (condenser.hard_context_reset_context_scaling),
        }
        if condenser.max_tokens is not None:
            payload["max_tokens"] = condenser.max_tokens
        return payload

    def _workspace_path(self, value: str) -> str:
        path = Path(value)
        try:
            relative = path.resolve().relative_to(self.workspace_root)
        except ValueError:
            relative = Path(path.name)
        return str(self.openhands_workspace_root / relative)

    def _request_workspace_path(self, request: StartAttemptRequest) -> str:
        if (
            request.runtime_sandbox_id
            and request.node_workspace_ref.startswith("/runtime/workspace/project/")
            and request.runtime_working_dir_relative
        ):
            working = Path(request.runtime_working_dir_relative)
            if working.is_absolute() or any(
                part in {"", ".", ".."} for part in working.parts
            ):
                raise DomainError(
                    "RUNTIME_WORKSPACE_INVALID",
                    "The FlowRun Runtime working directory is invalid",
                    422,
                )
            return str(Path(request.node_workspace_ref).joinpath(*working.parts))
        return self._workspace_path(request.workspace_ref)

    @staticmethod
    def _artifact_input(binding: dict[str, Any]) -> dict[str, Any]:
        artifact = cast(dict[str, Any], binding.get("artifact") or {})
        return {
            "field_key": binding.get("field_key"),
            "display_name": binding.get("display_name"),
            "description": binding.get("description"),
            "template_url": binding.get("template_url"),
            "artifact_type": artifact.get("artifact_type"),
            "inline_content": artifact.get("inline_content"),
            "uri": artifact.get("uri"),
            "metadata": artifact.get("metadata", {}),
        }

    @staticmethod
    def _output_contract(request: StartAttemptRequest) -> list[dict[str, str]]:
        if request.interaction_mode == "COLLABORATION":
            return []
        return [
            {
                "field_key": field_key,
                "artifact_type": "URL",
                "root_url": target.get("root_url", ""),
                "run_name": target.get("run_name", ""),
                "title": target.get("title", field_key),
                "display_name": target.get("display_name", field_key),
                "description": target.get("description", ""),
                "template_url": target.get("template_url", ""),
            }
            for field_key, target in request.output_targets.items()
        ]

    def _initial_text(self, request: StartAttemptRequest) -> str:
        asset = cast(dict[str, Any], request.node.get("asset") or {})
        executor = cast(dict[str, Any], asset.get("executor") or {})
        startup = str(request.startup_prompt or executor.get("startup_prompt") or "").strip()
        if request.startup_capability_key:
            startup = f"${request.startup_capability_key}\n{startup}".strip()
        return startup or f"执行节点：{asset.get('name') or request.node.get('instance_key')}"

    def _context_text(self, request: StartAttemptRequest) -> str:
        asset = cast(dict[str, Any], request.node.get("asset") or {})
        executor = cast(dict[str, Any], asset.get("executor") or {})
        startup = str(request.startup_prompt or executor.get("startup_prompt") or "").strip()
        context = str(executor.get("context_prompt") or "").strip()
        inputs = [self._artifact_input(item) for item in request.bindings]
        outputs = self._output_contract(request)
        collaboration = request.interaction_mode == "COLLABORATION"
        sections = (
            [
                "你正在一个由人工新建的独立协作会话中。等待并响应用户在本会话中的请求。"
                "节点启动提示词在本会话中仅作背景，不是需要独立执行的预设任务。"
                "可用 Skill 与 MCP 均为候选能力：先理解用户意图，再自行选择真正相关的能力；"
                "用户通过 $ 显式指定能力时必须优先遵循。不要仅因某项能力是节点默认值就调用它。"
            ]
            if collaboration
            else []
        )
        if collaboration and startup:
            sections.append(
                "节点预置说明（仅作协作背景，不是需要独立执行或立即答复的用户任务）：\n" + startup
            )
        if context:
            heading = "节点背景上下文（仅作协作参考）" if collaboration else "任务上下文"
            sections.append(f"{heading}：\n{context}")
        if collaboration and request.semantic_history:
            sections.append(
                "这是显式创建的语义分支。下列内容只是源会话截至分叉点的可见文本副本，"
                "仅供理解用户语境；它不是本 Runtime 的既有历史，也不继承 Tool/Observation、"
                "Agent state、已激活 Skill、Condensation、usage stats 或 Runtime HEAD。"
                "不要把这些副本描述为已在本会话执行过的事件，也不要重复回答最后一条文本。"
                "等待用户的新消息后再继续。\n"
                + json.dumps(request.semantic_history, ensure_ascii=False)
            )
        if request.node_workspace_ref:
            resource_lines = [
                f"节点持久工作目录：{request.node_workspace_ref}",
                f"文本与附件目录：{request.node_workspace_ref}/files",
                f"代码仓库目录：{request.node_workspace_ref}/repositories",
            ]
            if request.agent_spec.mcp_servers:
                resource_lines.append("可用 MCP Servers：")
                resource_lines.extend(
                    f"- {server.name}: {server.workspace_path}"
                    for server in request.agent_spec.mcp_servers
                )
            resource_lines.append(
                "Skill 与 MCP 是可选能力；根据用户当前消息动态选择。"
                "Skill 的目录、内容和附带资源只通过 OpenHands 原生 Skill 触发或 "
                "invoke_skill 结果披露。"
                if collaboration
                else "Skill 的目录、内容和附带资源只通过 OpenHands 原生 Skill 触发或 "
                "invoke_skill 结果披露。"
            )
            sections.append("运行资源：\n" + "\n".join(resource_lines))
        input_heading = "当前 Attempt 输入（协作参考）" if collaboration else "流程输入"
        rendered_inputs = json.dumps(inputs, ensure_ascii=False, default=str)
        sections.append(f"{input_heading}：\n{rendered_inputs}")
        if inputs and not collaboration:
            sections.append(
                "输入项中的 uri 是本次运行实际读取的飞书文档；非空的 template_url 是节点定义时"
                "指定的可选格式与结构参考。请以实际输入内容为事实来源；有模板时参考其组织方式，"
                "没有模板时按 description 和任务要求处理。不得修改输入文档或输入模板。"
            )
        if outputs:
            sections.append(
                "平台不会持有或注入飞书账号凭据。请使用本终端环境内已登录的 lark-cli，"
                "在 root_url 下按 run_name/标题创建并编辑下列输出文档；有 template_url 时复制模板，"
                "否则创建空白文档。完成后调用 finish，并把 message 严格写成 JSON："
                '{"outputs":{"字段标识":{"artifact_type":"URL","uri":"https://..."}}}'
                "。不得把 token、cookie 或本地凭据文件写入消息。\n"
                + json.dumps(outputs, ensure_ascii=False)
            )
        return "\n\n".join(sections)

    def _create(self, request: StartAttemptRequest, *, run: bool) -> RuntimeHandle:
        spec = request.agent_spec
        provider = spec.provider
        if provider is None:
            raise DomainError(
                "MODEL_PROVIDER_REQUIRED",
                "The node executor must select a model provider before it can run",
                422,
            )
        skills = [
            {
                "name": skill.name,
                "content": skill.content,
                "description": skill.description or None,
                "source": skill.source or None,
                "trigger": (
                    {"type": "keyword", "keywords": list(skill.activation_keywords)}
                    if skill.activation_keywords
                    else None
                ),
                "is_agentskills_format": True,
                "disable_model_invocation": skill.disable_model_invocation,
            }
            for skill in spec.skills
        ]
        if not spec.tools:
            raise DomainError(
                "RUNTIME_AGENT_SPEC_INVALID",
                "Runtime Agent Spec must allow at least one Tool",
                409,
            )
        agent: dict[str, Any] = {
            "kind": "Agent",
            "llm": self._llm_payload(provider),
            "condenser": self._condenser_payload(spec.condenser, spec.condenser_provider),
            "tools": [{"name": tool.name, "params": dict(tool.params)} for tool in spec.tools],
            "tool_concurrency_limit": spec.tool_concurrency_limit,
        }
        if spec.critic is not None:
            critic: dict[str, Any] = {
                "kind": spec.critic.kind,
                "mode": spec.critic.mode,
            }
            if spec.critic.max_iterations > 0:
                critic["iterative_refinement"] = {
                    "success_threshold": spec.critic.success_threshold,
                    "max_iterations": spec.critic.max_iterations,
                }
            agent["critic"] = critic
        frozen_suffix = spec.agent_context.system_message_suffix
        runtime_suffix = self._context_text(request)
        system_suffix = (
            f"{frozen_suffix}\n\n{runtime_suffix}"
            if frozen_suffix and runtime_suffix
            else frozen_suffix or runtime_suffix
        )
        agent["agent_context"] = {
            "skills": skills,
            "system_message_suffix": system_suffix or None,
            "user_message_suffix": spec.agent_context.user_message_suffix or None,
            "load_user_skills": spec.agent_context.load_user_skills,
            "load_public_skills": spec.agent_context.load_public_skills,
            "marketplace_path": spec.agent_context.marketplace_path,
            "registered_marketplaces": [
                dict(item) for item in spec.agent_context.registered_marketplaces
            ],
            "load_project_skills": spec.agent_context.load_project_skills,
            "load_memory": spec.agent_context.load_memory,
            "disabled_skills": list(spec.agent_context.disabled_skills),
        }
        if spec.mcp_servers:
            agent["mcp_config"] = {server.name: server.config for server in spec.mcp_servers}
        payload: dict[str, Any] = {
            "workspace": {
                "kind": "LocalWorkspace",
                "working_dir": self._request_workspace_path(request),
            },
            "worktree": False,
            "max_iterations": spec.budgets.max_iterations,
            "agent": agent,
            "confirmation_policy": {
                "kind": (
                    "AlwaysConfirm" if spec.confirmation_policy == "ALWAYS" else "NeverConfirm"
                )
            },
            # Production runs must never discover mutable user/project or
            # Marketplace-installed Plugins outside the frozen Snapshot.
            "load_ambient_plugins": False,
        }
        if spec.agent_profile is not None:
            # The immutable FlowWeave Profile has already been materialized in
            # the explicit Agent payload above.  Never send agent_profile_id:
            # OpenHands would resolve it from its mutable server-side stores.
            payload["observability_metadata"] = {
                "flowweave.agent_profile_version_id": spec.agent_profile.capability_version_id,
                "flowweave.agent_profile_key": spec.agent_profile.capability_key,
                "flowweave.agent_profile_digest": spec.agent_profile.digest,
                "flowweave.agent_profile_schema_version": spec.agent_profile.schema_version,
                "flowweave.agent_profile_source_id": spec.agent_profile.source_profile_id,
                "flowweave.agent_profile_source_revision": spec.agent_profile.source_revision,
            }
        if spec.agent_definitions:
            payload["agent_definitions"] = [
                {
                    "name": definition.name,
                    "description": definition.description,
                    "model": "inherit",
                    "tools": list(definition.tools),
                    "skills": [],
                    "system_prompt": definition.system_prompt,
                    "when_to_use_examples": list(definition.when_to_use_examples),
                    "permission_mode": definition.permission_mode,
                    "max_iteration_per_run": definition.max_iteration_per_run,
                    "max_budget_per_run": definition.max_budget_per_run,
                    "condenser": {"kind": "NoOpCondenser"},
                    "metadata": {},
                }
                for definition in spec.agent_definitions
            ]
        if spec.plugins:
            payload["plugins"] = [{"source": plugin.source} for plugin in spec.plugins]
        if spec.hook_config:
            payload["hook_config"] = spec.hook_config
        if run:
            payload["initial_message"] = {
                "role": "user",
                "content": [{"type": "text", "text": self._initial_text(request)}],
                "run": True,
            }
        if request.environment_image and not (
            request.runtime_sandbox_id
            and request.runtime_resource_name
            and request.runtime_base_url
        ):
            raise DomainError(
                "RUNTIME_SANDBOX_REQUIRED",
                "A published environment Runtime must be allocated by the sandbox control plane",
                500,
            )
        target_base_url = request.runtime_base_url or self.base_url
        target_session_key = self._session_key_for_resource(request.runtime_resource_name or None)
        self._negotiate_runtime_contract(
            spec.runtime_contract,
            required_tools=tuple(tool.name for tool in spec.tools),
            base_url=target_base_url,
            session_api_key=target_session_key,
        )
        created = self._request(
            "POST",
            "/api/conversations",
            base_url=target_base_url,
            session_api_key=target_session_key,
            json=payload,
        )
        conversation_id = str(created.get("id") or "")
        if not conversation_id:
            raise DomainError("RUNTIME_PROTOCOL_ERROR", "Missing conversation id", 502)
        try:
            canonical_conversation_id = str(UUID(conversation_id))
        except ValueError as exc:
            raise DomainError(
                "RUNTIME_PROTOCOL_ERROR",
                "OpenHands returned an invalid Conversation identity",
                502,
            ) from exc
        if canonical_conversation_id != conversation_id:
            raise DomainError(
                "RUNTIME_PROTOCOL_ERROR",
                "OpenHands returned a non-canonical Conversation identity",
                502,
            )
        self._contracts[conversation_id] = self._output_contract(request)
        cursor_value = created.get("leaf_event_id") or created.get("last_user_message_id")
        cursor = str(cursor_value) if cursor_value else None
        job_id = (
            f"{'env-exec' if run else 'env-chat'}:{request.runtime_resource_name}"
            if request.runtime_resource_name
            else conversation_id
        )
        return RuntimeHandle(job_id=job_id, conversation_id=conversation_id, cursor=cursor)

    def create_conversation(self, request: StartAttemptRequest) -> RuntimeHandle:
        return self._create(request, run=False)

    def start(self, request: StartAttemptRequest) -> RuntimeHandle:
        return self._create(request, run=True)

    @staticmethod
    def _formal_identity(value: object, *, field: str, required: bool) -> str | None:
        if value is None and not required:
            return None
        if (
            not isinstance(value, str)
            or not value
            or len(value) > 200
            or (
                not (field == "parent_id" and value == "__root__")
                and re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,199}", value) is None
            )
        ):
            raise DomainError(
                "RUNTIME_EVENT_IDENTITY_INVALID",
                "OpenHands returned an invalid formal event identity",
                502,
                {"field": field},
            )
        return value

    @classmethod
    def _event_identity(
        cls, item: dict[str, Any]
    ) -> tuple[str, str | None, str | None, str | None]:
        event_id = cls._formal_identity(item.get("id"), field="id", required=True)
        assert event_id is not None
        return (
            event_id,
            cls._formal_identity(item.get("parent_id"), field="parent_id", required=False),
            cls._formal_identity(item.get("action_id"), field="action_id", required=False),
            cls._formal_identity(
                item.get("tool_call_id"), field="tool_call_id", required=False
            ),
        )

    def _conversation_state(self, handle: RuntimeHandle) -> dict[str, Any]:
        try:
            expected_id = str(UUID(handle.conversation_id))
        except ValueError as exc:
            raise DomainError(
                "RUNTIME_CONVERSATION_ID_INVALID",
                "The OpenHands Conversation locator is invalid",
                409,
            ) from exc
        if expected_id != handle.conversation_id:
            raise DomainError(
                "RUNTIME_CONVERSATION_ID_INVALID",
                "The OpenHands Conversation locator is not canonical",
                409,
            )
        state = self._request(
            "GET",
            f"/api/conversations/{handle.conversation_id}",
            base_url=self._base_url_for_handle(handle),
            session_api_key=self._session_key_for_handle(handle),
        )
        if str(state.get("id") or "") != handle.conversation_id:
            raise DomainError(
                "RUNTIME_CONVERSATION_IDENTITY_DRIFT",
                "OpenHands returned a different Conversation identity",
                409,
                {"conversation_id": handle.conversation_id},
            )
        raw_workspace = state.get("workspace")
        workspace = (
            cast(dict[str, Any], raw_workspace) if isinstance(raw_workspace, dict) else {}
        )
        working_dir_raw = workspace.get("working_dir")
        working_dir = (
            PurePosixPath(working_dir_raw) if isinstance(working_dir_raw, str) else None
        )
        project_root = PurePosixPath("/runtime/workspace/project")
        if (
            workspace.get("kind") != "LocalWorkspace"
            or working_dir is None
            or not working_dir.is_absolute()
            or ".." in working_dir.parts
            or not working_dir.is_relative_to(project_root)
        ):
            raise DomainError(
                "RUNTIME_WORKSPACE_IDENTITY_DRIFT",
                "The reloaded Conversation is not bound to the FlowRun workspace",
                409,
                {"conversation_id": handle.conversation_id},
            )
        if state.get("persistence_dir") != "/runtime/state/conversations":
            raise DomainError(
                "RUNTIME_PERSISTENCE_IDENTITY_DRIFT",
                "The reloaded Conversation is not bound to external OpenHands state",
                409,
                {"conversation_id": handle.conversation_id},
            )
        return state

    def reload_conversation(
        self,
        handle: RuntimeHandle,
        *,
        expected: RuntimeConversationIdentity | None = None,
    ) -> RuntimeConversationIdentity:
        """Hydrate one persisted Conversation by its original OpenHands identity."""

        if expected is not None and expected.conversation_id != handle.conversation_id:
            raise DomainError(
                "RUNTIME_RELOAD_IDENTITY_MISMATCH",
                "The reload probe targets a different OpenHands Conversation",
                409,
                {"conversation_id": handle.conversation_id},
            )
        state = self._conversation_state(handle)
        leaf_event_id = self._formal_identity(
            state.get("leaf_event_id"), field="leaf_event_id", required=False
        )
        probe_event_id = (
            expected.event_id
            if expected is not None and expected.event_id is not None
            else leaf_event_id
        )
        event: dict[str, Any] | None = None
        if probe_event_id is not None:
            event = self._request(
                "GET",
                f"/api/conversations/{handle.conversation_id}/events/{probe_event_id}",
                base_url=self._base_url_for_handle(handle),
                session_api_key=self._session_key_for_handle(handle),
            )
            if str(event.get("id") or "") != probe_event_id:
                raise DomainError(
                    "RUNTIME_EVENT_IDENTITY_DRIFT",
                    "OpenHands reloaded a different event identity",
                    409,
                    {"conversation_id": handle.conversation_id},
                )
        else:
            page = self._request(
                "GET",
                f"/api/conversations/{handle.conversation_id}/events/search",
                base_url=self._base_url_for_handle(handle),
                session_api_key=self._session_key_for_handle(handle),
                params={"limit": 1, "sort_order": "TIMESTAMP_DESC"},
            )
            raw_items = page.get("items")
            if not isinstance(raw_items, list) or any(
                not isinstance(item, dict) for item in cast(list[object], raw_items)
            ):
                raise DomainError(
                    "RUNTIME_EVENT_IDENTITY_INVALID",
                    "OpenHands returned an invalid event identity page",
                    502,
                )
            if raw_items:
                event = cast(dict[str, Any], raw_items[0])

        event_identity = self._event_identity(event) if event is not None else None
        raw_workspace = cast(dict[str, Any], state["workspace"])
        identity = RuntimeConversationIdentity(
            conversation_id=handle.conversation_id,
            workspace_working_dir=str(raw_workspace["working_dir"]),
            persistence_dir=str(state["persistence_dir"]),
            event_id=event_identity[0] if event_identity is not None else None,
            parent_id=event_identity[1] if event_identity is not None else None,
            action_id=event_identity[2] if event_identity is not None else None,
            tool_call_id=event_identity[3] if event_identity is not None else None,
        )
        if expected is not None and identity != expected:
            raise DomainError(
                "RUNTIME_RELOAD_IDENTITY_MISMATCH",
                "The original OpenHands Conversation or event identity did not survive reload",
                409,
                {"conversation_id": handle.conversation_id},
            )
        return identity

    @staticmethod
    def _text_content(value: object) -> str:
        if isinstance(value, str):
            return value
        if isinstance(value, dict):
            item = cast(dict[str, object], value)
            text = item.get("text")
            if isinstance(text, str):
                return text
            return OpenHandsRuntime._text_content(item.get("content"))
        if not isinstance(value, list):
            return ""
        values: list[str] = []
        for raw in cast(list[object], value):
            if isinstance(raw, dict):
                item = cast(dict[str, object], raw)
                text = item.get("text")
                if isinstance(text, str):
                    values.append(text)
        return "\n".join(values)

    @classmethod
    def _event_text(cls, item: dict[str, Any]) -> str:
        kind = str(item.get("kind") or "")
        if kind == "MessageEvent":
            message = item.get("llm_message")
            if isinstance(message, dict):
                return cls._text_content(cast(dict[str, object], message).get("content"))
        if kind == "ActionEvent":
            action = item.get("action")
            if isinstance(action, dict):
                action_item = cast(dict[str, object], action)
                value = action_item.get("message") or action_item.get("thought")
                return cls._text_content(value)
        if kind == "ObservationEvent":
            observation = item.get("observation")
            if isinstance(observation, dict):
                observation_item = cast(dict[str, object], observation)
                value = observation_item.get("content") or observation_item.get("message")
                return cls._text_content(value)
            value = item.get("content") or item.get("message")
            return cls._text_content(value)
        return ""

    @classmethod
    def _event_type(cls, item: dict[str, Any]) -> RuntimeEventType:
        kind = str(item.get("kind") or "")
        if kind == "CondensationRequest":
            return "CONDENSATION_REQUESTED"
        if kind == "Condensation":
            return "CONDENSATION_COMPLETED"
        if kind == "MessageEvent":
            return "MESSAGE"
        if kind == "ActionEvent":
            action = item.get("action")
            action_kind = (
                str(cast(dict[str, object], action).get("kind") or "")
                if isinstance(action, dict)
                else ""
            )
            if action_kind == "FinishAction":
                return "COMPLETED"
            return "THOUGHT" if action_kind == "ThinkAction" else "TOOL_CALL"
        if kind == "ObservationEvent":
            observation = item.get("observation")
            observation_kind = (
                str(cast(dict[str, object], observation).get("kind") or "")
                if isinstance(observation, dict)
                else ""
            )
            if observation_kind == "FinishObservation":
                return "COMPLETED"
            return "TOOL_RESULT"
        if "error" in kind.lower():
            return "ERROR"
        return "STATE"

    @classmethod
    def _critic_result(cls, item: dict[str, Any]) -> dict[str, Any] | None:
        raw = item.get("critic_result")
        if raw is None:
            return None
        if not isinstance(raw, dict):
            raise DomainError(
                "RUNTIME_CRITIC_PROTOCOL_DRIFT",
                "OpenHands Critic result is not an object",
                502,
            )
        value = cast(dict[str, Any], raw)
        score = value.get("score")
        if (
            isinstance(score, bool)
            or not isinstance(score, int | float)
            or not math.isfinite(float(score))
            or not 0.0 <= float(score) <= 1.0
        ):
            raise DomainError(
                "RUNTIME_CRITIC_PROTOCOL_DRIFT",
                "OpenHands Critic score is invalid",
                502,
            )
        raw_message = value.get("message")
        if raw_message is not None and not isinstance(raw_message, str):
            raise DomainError(
                "RUNTIME_CRITIC_PROTOCOL_DRIFT",
                "OpenHands Critic message is invalid",
                502,
            )
        return {
            "score": float(score),
            "message": raw_message[:2000] if isinstance(raw_message, str) else None,
        }

    @classmethod
    def _goal_status(cls, item: dict[str, Any]) -> dict[str, Any] | None:
        if item.get("kind") != "ConversationStateUpdateEvent" or item.get("key") != "goal":
            return None
        raw = item.get("value")
        if not isinstance(raw, dict):
            raise DomainError(
                "RUNTIME_GOAL_PROTOCOL_DRIFT",
                "OpenHands Goal status is not an object",
                502,
            )
        value = cast(dict[str, Any], raw)
        status = str(value.get("status") or "")
        iteration = value.get("iteration")
        max_iterations = value.get("max_iterations")
        objective = value.get("objective")
        if (
            status not in {"running", "complete", "capped", "interrupted"}
            or not isinstance(value.get("active"), bool)
            or isinstance(iteration, bool)
            or not isinstance(iteration, int)
            or iteration < 0
            or isinstance(max_iterations, bool)
            or not isinstance(max_iterations, int)
            or max_iterations < 1
            or not isinstance(objective, str)
            or not objective.strip()
        ):
            raise DomainError(
                "RUNTIME_GOAL_PROTOCOL_DRIFT",
                "OpenHands Goal status contains invalid fields",
                502,
            )
        verdict = value.get("verdict")
        safe_verdict: dict[str, Any] | None = None
        if verdict is not None:
            if not isinstance(verdict, dict):
                raise DomainError(
                    "RUNTIME_GOAL_PROTOCOL_DRIFT",
                    "OpenHands Goal verdict is invalid",
                    502,
                )
            verdict_value = cast(dict[str, Any], verdict)
            score = verdict_value.get("score")
            if (
                isinstance(score, bool)
                or not isinstance(score, int | float)
                or not math.isfinite(float(score))
                or not 0.0 <= float(score) <= 1.0
                or not isinstance(verdict_value.get("complete"), bool)
            ):
                raise DomainError(
                    "RUNTIME_GOAL_PROTOCOL_DRIFT",
                    "OpenHands Goal verdict contains invalid fields",
                    502,
                )
            safe_verdict = {
                "score": float(score),
                "complete": verdict_value["complete"],
                "missing": str(verdict_value.get("missing") or "")[:2000],
            }
        return {
            "active": value["active"],
            "status": status,
            "iteration": iteration,
            "max_iterations": max_iterations,
            "objective": objective[:20_000],
            "verdict": safe_verdict,
        }

    @classmethod
    def _safe_event_detail(cls, value: object, *, depth: int = 0) -> object:
        if depth >= 6:
            return "[truncated]"
        if isinstance(value, dict):
            return {
                str(key): (
                    "[redacted]"
                    if any(
                        marker in str(key).lower()
                        for marker in ("api_key", "authorization", "password", "secret", "token")
                    )
                    else cls._safe_event_detail(child, depth=depth + 1)
                )
                for key, child in list(cast(dict[object, object], value).items())[:100]
            }
        if isinstance(value, list):
            sequence = cast(list[object], value)
            return [cls._safe_event_detail(item, depth=depth + 1) for item in sequence[:100]]
        if isinstance(value, str):
            return value[:20_000]
        return value if value is None or isinstance(value, int | float | bool) else str(value)

    @classmethod
    def _event_payload(cls, item: dict[str, Any]) -> dict[str, Any]:
        kind = str(item.get("kind") or "UNKNOWN")
        payload: dict[str, Any] = {
            "source_type": kind,
            "source": item.get("source"),
            "content": cls._event_text(item),
        }
        critic_result = cls._critic_result(item)
        if critic_result is not None:
            payload["critic_result"] = critic_result
        goal_status = cls._goal_status(item)
        if goal_status is not None:
            payload["goal_status"] = goal_status
        if kind == "CondensationRequest":
            payload["event_name"] = kind
        elif kind == "Condensation":
            payload.update(
                {
                    "event_name": kind,
                    "forgotten_event_ids": sorted(
                        str(value) for value in item.get("forgotten_event_ids", [])
                    ),
                    "summary": cls._safe_event_detail(item.get("summary")),
                    "summary_offset": item.get("summary_offset"),
                    "llm_response_id": item.get("llm_response_id"),
                }
            )
        raw_detail = (
            item.get("action")
            if kind == "ActionEvent"
            else item.get("observation")
            if kind == "ObservationEvent"
            else None
        )
        if isinstance(raw_detail, dict):
            detail = cast(dict[str, Any], raw_detail)
            event_name = str(detail.get("kind") or kind)
            private_detail_fields = {"kind", "message", "thought"}
            if kind == "ActionEvent" and event_name == "TaskAction":
                # A native Task prompt may contain credentials or private input.
                # OpenHands owns that execution payload; FlowWeave persists only
                # the governed lifecycle projection and stable event identities.
                private_detail_fields.add("prompt")
            payload["event_name"] = event_name
            payload["details"] = cls._safe_event_detail(
                {key: value for key, value in detail.items() if key not in private_detail_fields}
            )
            if kind == "ActionEvent" and event_name == "TaskAction":
                payload["runtime_task"] = {
                    "phase": "REQUESTED",
                    "action_event_id": str(item.get("id") or ""),
                    "tool_call_id": str(item.get("tool_call_id") or ""),
                    "llm_response_id": str(item.get("llm_response_id") or ""),
                    "subagent_type": str(detail.get("subagent_type") or "general-purpose"),
                    "description": cls._safe_event_detail(detail.get("description")),
                    "resume_task_id": cls._safe_event_detail(detail.get("resume")),
                }
            elif kind == "ObservationEvent" and event_name == "TaskObservation":
                status = str(detail.get("status") or "error").lower()
                payload["runtime_task"] = {
                    "phase": (
                        "COMPLETED"
                        if status == "completed" and not bool(detail.get("is_error"))
                        else "ERROR"
                    ),
                    "action_event_id": str(item.get("action_id") or ""),
                    "observation_event_id": str(item.get("id") or ""),
                    "tool_call_id": str(item.get("tool_call_id") or ""),
                    "task_id": str(detail.get("task_id") or "unknown"),
                    "subagent_type": str(detail.get("subagent") or "unknown"),
                    "status": status,
                }
            elif kind == "ActionEvent" and event_name == "InvokeSkillAction":
                payload["runtime_skill"] = {
                    "phase": "INVOKED",
                    "skill_name": str(detail.get("name") or ""),
                    "action_event_id": str(item.get("id") or ""),
                    "tool_call_id": str(item.get("tool_call_id") or ""),
                    "llm_response_id": str(item.get("llm_response_id") or ""),
                }
            elif kind == "ObservationEvent" and event_name == "InvokeSkillObservation":
                payload["runtime_skill"] = {
                    "phase": "LOADED" if not bool(detail.get("is_error")) else "ERROR",
                    "skill_name": str(detail.get("skill_name") or ""),
                    "action_event_id": str(item.get("action_id") or ""),
                    "observation_event_id": str(item.get("id") or ""),
                    "tool_call_id": str(item.get("tool_call_id") or ""),
                }
        if kind == "MessageEvent":
            activated = item.get("activated_skills")
            if isinstance(activated, list):
                activated_values = cast(list[object], activated)
                activated_names = [name for name in activated_values[:100] if isinstance(name, str)]
                payload["activated_skills"] = [name[:200] for name in activated_names]
        return payload

    def _events(
        self,
        conversation_id: str,
        cursor: str | None,
        *,
        base_url: str | None = None,
        session_api_key: str | None = None,
    ) -> tuple[list[dict[str, Any]], str | None]:
        items: list[dict[str, Any]] = []
        seen_event_ids: set[str] = set()
        seen_page_ids: set[str] = set()
        page_id = cursor
        first_page = True
        while True:
            if page_id is not None:
                if page_id in seen_page_ids:
                    raise DomainError(
                        "RUNTIME_EVENT_IDENTITY_INVALID",
                        "OpenHands returned a cyclic event page identity",
                        502,
                    )
                seen_page_ids.add(page_id)
            params: dict[str, Any] = {"limit": 100, "sort_order": "TIMESTAMP"}
            if page_id:
                params["page_id"] = page_id
            data = self._request(
                "GET",
                f"/api/conversations/{conversation_id}/events/search",
                base_url=base_url,
                session_api_key=session_api_key,
                params=params,
            )
            raw_items: object = data.get("items", [])
            if not isinstance(raw_items, list) or any(
                not isinstance(item, dict) for item in cast(list[object], raw_items)
            ):
                raise DomainError(
                    "RUNTIME_EVENT_IDENTITY_INVALID",
                    "OpenHands returned an invalid event identity page",
                    502,
                )
            page_items = [
                cast(dict[str, Any], item) for item in cast(list[object], raw_items)
            ]
            page_event_ids = [self._event_identity(item)[0] for item in page_items]
            if len(set(page_event_ids)) != len(page_event_ids):
                raise DomainError(
                    "RUNTIME_EVENT_IDENTITY_INVALID",
                    "OpenHands returned duplicate formal event identities",
                    502,
                )
            if first_page and cursor:
                anchor_index = next(
                    (
                        index
                        for index, item in enumerate(page_items)
                        if str(item.get("id") or "") == cursor
                    ),
                    None,
                )
                if anchor_index is None:
                    raise DomainError(
                        "RUNTIME_EVENT_IDENTITY_MISMATCH",
                        "The persisted OpenHands event anchor is missing after reload",
                        409,
                        {"conversation_id": conversation_id, "event_id": cursor},
                    )
                page_items = page_items[anchor_index + 1 :]
                page_event_ids = page_event_ids[anchor_index + 1 :]
            elif not first_page and page_id and (
                not page_event_ids or page_event_ids[0] != page_id
            ):
                raise DomainError(
                    "RUNTIME_EVENT_IDENTITY_INVALID",
                    "OpenHands returned a page without its formal event anchor",
                    502,
                    {"conversation_id": conversation_id, "event_id": page_id},
                )
            if any(event_id in seen_event_ids for event_id in page_event_ids):
                raise DomainError(
                    "RUNTIME_EVENT_IDENTITY_INVALID",
                    "OpenHands replayed a formal event identity across pages",
                    502,
                )
            seen_event_ids.update(page_event_ids)
            items.extend(page_items)
            raw_next_page_id = data.get("next_page_id")
            next_page_id = self._formal_identity(
                raw_next_page_id, field="next_page_id", required=False
            )
            if not next_page_id or next_page_id == page_id:
                break
            page_id = next_page_id
            first_page = False
        # OpenHands treats page_id as an inclusive event anchor, not as an
        # opaque "start after this event" cursor.  The adapter persists the
        # last projected event id as its cursor, so exposing the anchor again
        # would replay a previous FinishAction as the result of the next human
        # message.  Only events created after the persisted anchor belong to
        # the current poll.  If OpenHands cannot find the anchor it starts at
        # the beginning of the log; reject that fallback rather than replaying
        # arbitrary history into the current turn.
        next_cursor = str(items[-1].get("id")) if items and items[-1].get("id") else cursor
        return items, next_cursor

    @staticmethod
    def _canonical_digest(value: object) -> str:
        encoded = json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode()
        return hashlib.sha256(encoded).hexdigest()

    @staticmethod
    def _active_branch(
        events: list[dict[str, Any]], leaf_event_id: str | None
    ) -> list[dict[str, Any]]:
        """Reproduce ConversationState.active_branch from the public event tree."""

        if not events or not leaf_event_id:
            return events
        by_id = {str(item.get("id")): item for item in events if item.get("id")}
        leaf = by_id.get(leaf_event_id)
        if leaf is None:
            raise DomainError(
                "RUNTIME_PROTOCOL_ERROR",
                "OpenHands active branch leaf is missing from the event log",
                502,
                {"leaf_event_id": leaf_event_id},
            )
        # Pre-tree conversations have no parent ids. OpenHands treats their
        # persisted linear log as the active branch.
        if not any(item.get("parent_id") is not None for item in events):
            return events[: events.index(leaf) + 1]
        branch: list[dict[str, Any]] = []
        current = leaf
        visited: set[str] = set()
        while True:
            event_id = str(current.get("id") or "")
            if not event_id or event_id in visited:
                raise DomainError(
                    "RUNTIME_PROTOCOL_ERROR",
                    "OpenHands event tree contains an invalid active branch",
                    502,
                )
            visited.add(event_id)
            branch.append(current)
            parent_id = current.get("parent_id")
            if parent_id in {None, "__root__"}:
                break
            current = by_id.get(str(parent_id))
            if current is None:
                raise DomainError(
                    "RUNTIME_PROTOCOL_ERROR",
                    "OpenHands active branch parent is missing from the event log",
                    502,
                    {"parent_id": str(parent_id)},
                )
        branch.reverse()
        return branch

    @classmethod
    def _pending_actions(
        cls, events: list[dict[str, Any]], leaf_event_id: str | None
    ) -> tuple[RuntimePendingAction, ...]:
        """Match OpenHands 1.40.0 ConversationState.get_unmatched_actions."""

        branch = cls._active_branch(events, leaf_event_id)
        observed_action_ids: set[str] = set()
        observed_tool_call_ids: set[str] = set()
        pending: list[RuntimePendingAction] = []
        for event in reversed(branch):
            kind = str(event.get("kind") or "")
            if kind in {"ObservationEvent", "UserRejectObservation"}:
                action_id = str(event.get("action_id") or "")
                if action_id:
                    observed_action_ids.add(action_id)
                continue
            if kind == "AgentErrorEvent":
                tool_call_id = str(event.get("tool_call_id") or "")
                if tool_call_id:
                    observed_tool_call_ids.add(tool_call_id)
                continue
            if kind != "ActionEvent" or not isinstance(event.get("action"), dict):
                continue
            action_id = str(event.get("id") or "")
            tool_call_id = str(event.get("tool_call_id") or "")
            if action_id in observed_action_ids or tool_call_id in observed_tool_call_ids:
                continue
            raw_action = cast(dict[str, Any], event["action"])
            tool_name = str(event.get("tool_name") or raw_action.get("kind") or "")
            canonical = {
                "action_id": action_id,
                "tool_call_id": tool_call_id,
                "tool_name": tool_name,
                "action": raw_action,
            }
            safe_arguments = cls._safe_event_detail(
                {key: value for key, value in raw_action.items() if key != "kind"}
            )
            pending.insert(
                0,
                RuntimePendingAction(
                    action_id=action_id,
                    tool_call_id=tool_call_id,
                    tool_name=tool_name,
                    arguments=(
                        cast(dict[str, Any], safe_arguments)
                        if isinstance(safe_arguments, dict)
                        else {}
                    ),
                    security_risk=str(event.get("security_risk") or "UNKNOWN"),
                    summary=str(event.get("summary") or ""),
                    digest=cls._canonical_digest(canonical),
                ),
            )
        return tuple(pending)

    def get_pending_confirmation(self, handle: RuntimeHandle) -> RuntimePendingConfirmation | None:
        base_url = self._base_url_for_handle(handle)
        session_api_key = self._session_key_for_handle(handle)
        state = self._conversation_state(handle)
        if str(state.get("execution_status") or "").lower() != "waiting_for_confirmation":
            return None
        leaf = str(state.get("leaf_event_id") or "") or None
        events, cursor = self._events(
            handle.conversation_id,
            None,
            base_url=base_url,
            session_api_key=session_api_key,
        )
        actions = self._pending_actions(events, leaf)
        if not actions:
            raise DomainError(
                "RUNTIME_PROTOCOL_ERROR",
                "OpenHands is waiting for confirmation without pending actions",
                502,
            )
        digest = self._canonical_digest([action.digest for action in actions])
        return RuntimePendingConfirmation(digest, actions, cursor or leaf)

    def respond_to_confirmation(
        self,
        handle: RuntimeHandle,
        expected_pending_digest: str,
        accept: bool,
        reason: str,
    ) -> RuntimeResult:
        pending = self.get_pending_confirmation(handle)
        if pending is None or pending.pending_actions_digest != expected_pending_digest:
            raise DomainError(
                "RUNTIME_CONFIRMATION_DRIFTED",
                "The OpenHands pending action batch changed; refresh before deciding",
                409,
                {
                    "expected_pending_digest": expected_pending_digest,
                    "actual_pending_digest": (
                        pending.pending_actions_digest if pending is not None else None
                    ),
                },
            )
        self._request(
            "POST",
            f"/api/conversations/{handle.conversation_id}/events/respond_to_confirmation",
            base_url=self._base_url_for_handle(handle),
            session_api_key=self._session_key_for_handle(handle),
            json={"accept": accept, "reason": reason},
        )
        return RuntimeResult(status="RUNNING", cursor=pending.cursor)

    def _outputs(self, conversation_id: str, text: str) -> dict[str, tuple[str, str]]:
        expected = {item["field_key"] for item in self._contracts.get(conversation_id, [])}
        candidate = text.strip()
        if candidate.startswith("```"):
            lines = candidate.splitlines()
            candidate = "\n".join(lines[1:-1]).strip() if len(lines) >= 3 else ""
        try:
            value: object = json.loads(candidate)
        except ValueError:
            start, end = candidate.find("{"), candidate.rfind("}")
            if start < 0 or end <= start:
                return {}
            try:
                value = json.loads(candidate[start : end + 1])
            except ValueError:
                return {}
        if not isinstance(value, dict):
            return {}
        raw_outputs = cast(dict[str, object], value).get("outputs")
        if not isinstance(raw_outputs, dict):
            return {}
        outputs: dict[str, tuple[str, str]] = {}
        for field_key, raw in cast(dict[object, object], raw_outputs).items():
            key = str(field_key)
            if key not in expected:
                continue
            if isinstance(raw, str):
                uri = raw
            elif isinstance(raw, dict):
                item = cast(dict[object, object], raw)
                uri = str(item.get("uri") or item.get("url") or "")
            else:
                continue
            parsed = urlparse(uri)
            host = (parsed.hostname or "").lower()
            if parsed.scheme != "https" or not any(
                host == suffix or host.endswith(f".{suffix}")
                for suffix in ("feishu.cn", "larksuite.com", "larkoffice.com")
            ):
                continue
            outputs[key] = ("URL", uri)
        return outputs

    def _result_from_events(
        self,
        conversation_id: str,
        items: list[dict[str, Any]],
        cursor: str | None,
        *,
        assistant_message_is_final: bool = False,
    ) -> RuntimeResult | None:
        for item in reversed(items):
            if self._event_type(item) == "COMPLETED":
                text = self._event_text(item)
                return RuntimeResult(
                    status="COMPLETED",
                    outputs=self._outputs(conversation_id, text),
                    final_message=text,
                    cursor=cursor,
                )
            if self._event_type(item) == "ERROR":
                return RuntimeResult(
                    status="FAILED",
                    error=self._event_text(item) or "OpenHands failed",
                    cursor=cursor,
                )
            if assistant_message_is_final and str(item.get("kind") or "") == "MessageEvent":
                message = item.get("llm_message")
                role = (
                    str(cast(dict[str, object], message).get("role") or "").lower()
                    if isinstance(message, dict)
                    else ""
                )
                if role == "assistant" or str(item.get("source") or "").lower() == "agent":
                    text = self._event_text(item)
                    if text:
                        return RuntimeResult(
                            status="COMPLETED",
                            outputs=self._outputs(conversation_id, text),
                            final_message=text,
                            cursor=cursor,
                        )
        return None

    def read_events(self, handle: RuntimeHandle) -> RuntimeEventBatch:
        base_url = self._base_url_for_handle(handle)
        session_api_key = self._session_key_for_handle(handle)
        items, cursor = self._events(
            handle.conversation_id,
            handle.cursor,
            base_url=base_url,
            session_api_key=session_api_key,
        )
        events = tuple(
            RuntimeEvent(
                cursor=self._event_identity(item)[0],
                event_type=self._event_type(item),
                payload=self._event_payload(item),
            )
            for item in items
        )
        state = self._conversation_state(handle)
        state_cursor = str(state.get("leaf_event_id") or cursor or handle.cursor or "") or None
        return RuntimeEventBatch(
            events=events,
            cursor=cursor,
            result=self._result_from_events(handle.conversation_id, items, cursor),
            task_usage=self._task_usage_snapshots(state, source_cursor=state_cursor),
            usage=self._usage_snapshots(state),
        )

    @classmethod
    def _usage_snapshots(cls, state: dict[str, Any]) -> tuple[RuntimeUsageSnapshot, ...]:
        stats = state.get("stats")
        if stats is None:
            return ()
        if not isinstance(stats, dict):
            raise DomainError(
                "RUNTIME_USAGE_PROTOCOL_DRIFT",
                "OpenHands Conversation stats have an invalid usage envelope",
                502,
            )
        stats_value = cast(dict[str, Any], stats)
        usage_to_metrics = stats_value.get("usage_to_metrics", {})
        if not isinstance(usage_to_metrics, dict):
            raise DomainError(
                "RUNTIME_USAGE_PROTOCOL_DRIFT",
                "OpenHands Conversation stats have an invalid usage envelope",
                502,
            )
        snapshots: list[RuntimeUsageSnapshot] = []
        for raw_usage_id, raw_metrics in sorted(
            cast(dict[object, object], usage_to_metrics).items(),
            key=lambda item: str(item[0]),
        ):
            usage_id = str(raw_usage_id)
            if not usage_id or len(usage_id) > 200 or not isinstance(raw_metrics, dict):
                raise DomainError(
                    "RUNTIME_USAGE_PROTOCOL_DRIFT",
                    "OpenHands usage identity or metrics are invalid",
                    502,
                )
            metrics = cast(dict[str, Any], raw_metrics)
            raw_tokens = metrics.get("accumulated_token_usage")
            if raw_tokens is not None and not isinstance(raw_tokens, dict):
                raise DomainError(
                    "RUNTIME_USAGE_PROTOCOL_DRIFT",
                    "OpenHands token usage is invalid",
                    502,
                    {"usage_id": usage_id},
                )
            tokens = cast(dict[str, Any], raw_tokens) if isinstance(raw_tokens, dict) else {}

            def counter(
                name: str,
                *,
                token_snapshot: dict[str, Any] = tokens,
                current_usage_id: str = usage_id,
            ) -> int:
                value = token_snapshot.get(name, 0)
                if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                    raise DomainError(
                        "RUNTIME_USAGE_PROTOCOL_DRIFT",
                        "OpenHands token usage contains an invalid counter",
                        502,
                        {"usage_id": current_usage_id, "field": name},
                    )
                return value

            raw_cost = metrics.get("accumulated_cost", 0.0)
            if (
                isinstance(raw_cost, bool)
                or not isinstance(raw_cost, int | float)
                or not math.isfinite(float(raw_cost))
                or float(raw_cost) < 0
            ):
                raise DomainError(
                    "RUNTIME_USAGE_PROTOCOL_DRIFT",
                    "OpenHands usage contains an invalid accumulated cost",
                    502,
                    {"usage_id": usage_id},
                )
            snapshots.append(
                RuntimeUsageSnapshot(
                    usage_id=usage_id,
                    model_name=str(metrics.get("model_name") or "default")[:200],
                    accumulated_cost=float(raw_cost),
                    prompt_tokens=counter("prompt_tokens"),
                    completion_tokens=counter("completion_tokens"),
                    cache_read_tokens=counter("cache_read_tokens"),
                    cache_write_tokens=counter("cache_write_tokens"),
                    reasoning_tokens=counter("reasoning_tokens"),
                )
            )
        return tuple(snapshots)

    @classmethod
    def _task_usage_snapshots(
        cls, state: dict[str, Any], *, source_cursor: str | None
    ) -> tuple[RuntimeTaskUsageSnapshot, ...]:
        """Normalize only the formal cumulative Task metrics exposed by 1.40.0."""

        stats = state.get("stats")
        if stats is None:
            return ()
        if not isinstance(stats, dict):
            raise DomainError(
                "RUNTIME_TASK_USAGE_PROTOCOL_DRIFT",
                "OpenHands Conversation stats have an invalid Task usage envelope",
                502,
            )
        stats_map = cast(dict[str, object], stats)
        raw_usage_map = stats_map.get("usage_to_metrics", {})
        if not isinstance(raw_usage_map, dict):
            raise DomainError(
                "RUNTIME_TASK_USAGE_PROTOCOL_DRIFT",
                "OpenHands Conversation stats have an invalid Task usage envelope",
                502,
            )
        snapshots: list[RuntimeTaskUsageSnapshot] = []
        for usage_id, raw_metrics in sorted(
            cast(dict[object, object], raw_usage_map).items(),
            key=lambda item: str(item[0]),
        ):
            key = str(usage_id)
            if not key.startswith("task:"):
                continue
            task_id = key.removeprefix("task:")
            if not task_id or len(task_id) > 100 or not isinstance(raw_metrics, dict):
                raise DomainError(
                    "RUNTIME_TASK_USAGE_PROTOCOL_DRIFT",
                    "OpenHands Task usage has an invalid identity or metrics snapshot",
                    502,
                    {"usage_id": key[:120]},
                )
            metrics = cast(dict[str, object], raw_metrics)
            raw_tokens = metrics.get("accumulated_token_usage")
            if raw_tokens is not None and not isinstance(raw_tokens, dict):
                raise DomainError(
                    "RUNTIME_TASK_USAGE_PROTOCOL_DRIFT",
                    "OpenHands Task token usage snapshot is invalid",
                    502,
                    {"task_id": task_id},
                )
            tokens = cast(dict[str, object], raw_tokens) if isinstance(raw_tokens, dict) else {}

            def nonnegative_int(
                field: str,
                *,
                token_snapshot: dict[str, object] = tokens,
                runtime_task_id: str = task_id,
            ) -> int:
                value = token_snapshot.get(field, 0)
                if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                    raise DomainError(
                        "RUNTIME_TASK_USAGE_PROTOCOL_DRIFT",
                        "OpenHands Task token usage contains an invalid counter",
                        502,
                        {"task_id": runtime_task_id, "field": field},
                    )
                return value

            raw_cost = metrics.get("accumulated_cost", 0.0)
            if (
                isinstance(raw_cost, bool)
                or not isinstance(raw_cost, int | float)
                or not math.isfinite(float(raw_cost))
                or float(raw_cost) < 0
            ):
                raise DomainError(
                    "RUNTIME_TASK_USAGE_PROTOCOL_DRIFT",
                    "OpenHands Task usage contains an invalid accumulated cost",
                    502,
                    {"task_id": task_id},
                )
            model_name = str(metrics.get("model_name") or "default")[:200]
            accumulated_cost = float(raw_cost)
            prompt_tokens = nonnegative_int("prompt_tokens")
            completion_tokens = nonnegative_int("completion_tokens")
            cache_read_tokens = nonnegative_int("cache_read_tokens")
            cache_write_tokens = nonnegative_int("cache_write_tokens")
            reasoning_tokens = nonnegative_int("reasoning_tokens")
            context_window = nonnegative_int("context_window")
            per_turn_tokens = nonnegative_int("per_turn_token")
            normalized: dict[str, str | float | int] = {
                "task_id": task_id,
                "model_name": model_name,
                "accumulated_cost": accumulated_cost,
                "prompt_tokens": prompt_tokens,
                "completion_tokens": completion_tokens,
                "cache_read_tokens": cache_read_tokens,
                "cache_write_tokens": cache_write_tokens,
                "reasoning_tokens": reasoning_tokens,
                "context_window": context_window,
                "per_turn_tokens": per_turn_tokens,
            }
            snapshots.append(
                RuntimeTaskUsageSnapshot(
                    task_id=task_id,
                    source_cursor=source_cursor,
                    digest=cls._canonical_digest(normalized),
                    model_name=model_name,
                    accumulated_cost=accumulated_cost,
                    prompt_tokens=prompt_tokens,
                    completion_tokens=completion_tokens,
                    cache_read_tokens=cache_read_tokens,
                    cache_write_tokens=cache_write_tokens,
                    reasoning_tokens=reasoning_tokens,
                    context_window=context_window,
                    per_turn_tokens=per_turn_tokens,
                )
            )
        return tuple(snapshots)

    async def stream_events(self, handle: RuntimeHandle) -> AsyncIterator[dict[str, Any]]:
        """Relay transient visible-text deltas without persisting model reasoning."""

        route = self._environment_route(handle.job_id)
        if route is not None and controller_is_remote(self.settings):
            if not handle.runtime_resource_id or not handle.runtime_resource_name:
                raise DomainError(
                    "AGENT_STREAM_UNAVAILABLE",
                    "The isolated Runtime stream has no verified sandbox binding",
                    409,
                )
            async for event in DockerControllerClient(self.settings).stream_runtime_events(
                resource_name=handle.runtime_resource_name,
                resource_id=handle.runtime_resource_id,
                conversation_id=handle.conversation_id,
            ):
                visible = self._visible_stream_event(cast(dict[str, object], event))
                if visible is not None:
                    yield visible
            return

        base_url = self._base_url_for_handle(handle)
        websocket_url = (
            f"{base_url.replace('https://', 'wss://').replace('http://', 'ws://')}"
            f"/sockets/events/{handle.conversation_id}"
        )
        async with connect(
            websocket_url,
            open_timeout=10,
            ping_interval=20,
            ping_timeout=20,
            max_size=2 * 1024 * 1024,
        ) as upstream:
            await upstream.send(
                json.dumps(
                    {
                        "type": "auth",
                        "session_api_key": self._session_key_for_handle(handle),
                    }
                )
            )
            async for raw in upstream:
                if not isinstance(raw, str):
                    continue
                try:
                    value: object = json.loads(raw)
                except ValueError:
                    continue
                if not isinstance(value, dict):
                    continue
                event = cast(dict[str, object], value)
                visible = self._visible_stream_event(event)
                if visible is not None:
                    yield visible

    def _wait_for_wakeup_frame(
        self,
        handle: RuntimeHandle,
        *,
        channel: Literal["CONVERSATION", "BASH"],
        timeout_seconds: float,
    ) -> bool:
        route = self._environment_route(handle.job_id)
        if route is not None and controller_is_remote(self.settings):
            if not handle.runtime_resource_id or not handle.runtime_resource_name:
                raise DomainError(
                    "RUNTIME_WAKEUP_UNAVAILABLE",
                    "The isolated Runtime wake-up has no verified sandbox binding",
                    409,
                )
            return DockerControllerClient(self.settings).wait_runtime_event(
                resource_name=handle.runtime_resource_name,
                resource_id=handle.runtime_resource_id,
                conversation_id=handle.conversation_id if channel == "CONVERSATION" else "",
                channel=channel,
                timeout_seconds=timeout_seconds,
            )

        base_url = self._base_url_for_handle(handle)
        path = (
            f"/sockets/events/{handle.conversation_id}"
            if channel == "CONVERSATION"
            else "/sockets/bash-events"
        )
        websocket_url = (
            f"{base_url.replace('https://', 'wss://').replace('http://', 'ws://')}{path}"
        )
        with sync_connect(
            websocket_url,
            open_timeout=10,
            ping_interval=20,
            ping_timeout=20,
            max_size=2 * 1024 * 1024,
        ) as upstream:
            upstream.send(
                json.dumps(
                    {
                        "type": "auth",
                        "session_api_key": self._session_key_for_handle(handle),
                    }
                )
            )
            try:
                upstream.recv(timeout=timeout_seconds)
            except TimeoutError:
                return False
            return True

    @staticmethod
    def _bash_event_identity(raw: object) -> dict[str, Any] | None:
        if not isinstance(raw, dict):
            return None
        event = cast(dict[str, object], raw)
        event_id = str(event.get("id") or "")
        if not event_id:
            return None
        return {
            "event_id": event_id,
            "kind": str(event.get("kind") or "UNKNOWN")[:80],
            "timestamp": str(event.get("timestamp") or "")[:80],
            "command_id": str(event.get("command_id") or "")[:200] or None,
            "order": event.get("order") if isinstance(event.get("order"), int) else None,
            "exit_code": (
                event.get("exit_code") if isinstance(event.get("exit_code"), int) else None
            ),
            "actor": "HUMAN_OR_SYSTEM",
            "source": "DIRECT_BASH",
        }

    def _read_bash_event_identities(
        self, handle: RuntimeHandle, cursor: str | None
    ) -> tuple[tuple[dict[str, Any], ...], str | None]:
        """Read a bounded Bash page after the last stable timestamp/event identity."""
        cursor_timestamp = ""
        cursor_event_id = ""
        if cursor:
            try:
                cursor_value = cast(object, json.loads(cursor))
            except ValueError:
                cursor_value = None
            if isinstance(cursor_value, dict):
                cursor_item = cast(dict[str, object], cursor_value)
                cursor_timestamp = str(cursor_item.get("timestamp") or "")
                cursor_event_id = str(cursor_item.get("event_id") or "")
        data = self._request(
            "GET",
            "/api/bash/bash_events/search",
            base_url=self._base_url_for_handle(handle),
            session_api_key=self._session_key_for_handle(handle),
            params=(
                {"timestamp__gte": cursor_timestamp, "limit": 100}
                if cursor_timestamp
                else {"limit": 100}
            ),
        )
        identities = tuple(
            item
            for raw in cast(list[object], data.get("items") or [])
            for item in [self._bash_event_identity(raw)]
            if item is not None
            and (str(item["timestamp"]), str(item["event_id"]))
            > (cursor_timestamp, cursor_event_id)
        )
        if len(identities) >= 100:
            raise DomainError(
                "RUNTIME_BASH_COMPENSATION_BACKLOG",
                "Bash REST compensation exceeded one bounded page; retry after review",
                409,
            )
        next_cursor = (
            json.dumps(
                {
                    "timestamp": identities[-1]["timestamp"],
                    "event_id": identities[-1]["event_id"],
                },
                separators=(",", ":"),
            )
            if identities
            else cursor
        )
        return identities, next_cursor

    def wait_for_wakeup(
        self,
        handle: RuntimeHandle,
        *,
        channel: Literal["CONVERSATION", "BASH"],
        timeout_seconds: float,
        cursor: str | None = None,
    ) -> RuntimeWakeup:
        try:
            notified = self._wait_for_wakeup_frame(
                handle,
                channel=channel,
                timeout_seconds=timeout_seconds,
            )
        except (
            OSError,
            TimeoutError,
            ConnectionClosed,
            InvalidHandshake,
            DockerControllerError,
        ) as exc:
            raise DomainError(
                "RUNTIME_WAKEUP_UNAVAILABLE",
                "OpenHands Runtime wake-up channel is unavailable; REST polling remains active",
                503,
            ) from exc
        if channel == "CONVERSATION":
            return RuntimeWakeup(channel=channel, notified=notified, cursor=cursor)
        events, next_cursor = self._read_bash_event_identities(handle, cursor)
        return RuntimeWakeup(
            channel=channel,
            notified=notified or bool(events),
            cursor=next_cursor,
            events=events,
        )

    @staticmethod
    def _visible_stream_event(event: dict[str, object]) -> dict[str, Any] | None:
        """Map an OpenHands frame to the public stream without exposing reasoning."""

        kind = str(event.get("kind") or "")
        if kind == "StreamingDeltaEvent":
            content = event.get("content")
            return (
                {"type": "delta", "content": content}
                if isinstance(content, str) and content
                else None
            )
        if kind != "MessageEvent":
            return None
        raw_message = event.get("llm_message")
        message = cast(dict[str, object], raw_message) if isinstance(raw_message, dict) else {}
        role = str(message.get("role") or "").lower()
        source = str(event.get("source") or "").lower()
        return {"type": "message_complete"} if role == "assistant" or source == "agent" else None

    def inspect(self, handle: RuntimeHandle) -> RuntimeResult:
        base_url = self._base_url_for_handle(handle)
        data = self._conversation_state(handle)
        status = str(data.get("execution_status") or "running").lower()
        cursor = str(data.get("leaf_event_id") or handle.cursor or "") or None
        if status == "finished":
            turn_anchor = str(data.get("last_user_message_id") or handle.cursor or "") or None
            items, event_cursor = self._events(
                handle.conversation_id,
                turn_anchor,
                base_url=base_url,
                session_api_key=self._session_key_for_handle(handle),
            )
            result = self._result_from_events(
                handle.conversation_id,
                items,
                event_cursor or cursor,
                assistant_message_is_final=True,
            )
            # A finished status without a completion event after this turn's
            # anchor can occur while event persistence is catching up.  Keep
            # polling instead of completing the turn with an old or empty
            # result.
            return result or RuntimeResult(status="RUNNING", cursor=event_cursor or handle.cursor)
        if status in {"error", "stuck"}:
            return RuntimeResult(
                status="FAILED",
                error=str(data.get("error") or f"OpenHands status: {status}"),
                cursor=cursor,
            )
        if status == "waiting_for_confirmation":
            return RuntimeResult(
                status="CONFIRMATION_REQUIRED",
                cursor=cursor,
            )
        return RuntimeResult(status="RUNNING", cursor=cursor)

    def send_message(
        self, handle: RuntimeHandle, content: str, image_urls: tuple[str, ...] = ()
    ) -> RuntimeResult:
        parts: list[dict[str, Any]] = []
        if content:
            parts.append({"type": "text", "text": content})
        if image_urls:
            parts.append({"type": "image", "image_urls": list(image_urls)})
        created = self._request(
            "POST",
            f"/api/conversations/{handle.conversation_id}/events",
            base_url=self._base_url_for_handle(handle),
            session_api_key=self._session_key_for_handle(handle),
            json={
                "role": "user",
                "content": parts,
                "run": True,
            },
        )
        cursor_value = (
            created.get("id")
            or created.get("event_id")
            or created.get("last_user_message_id")
            or created.get("leaf_event_id")
        )
        if not cursor_value:
            state = self._conversation_state(handle)
            cursor_value = state.get("last_user_message_id") or handle.cursor
        cursor = str(cursor_value) if cursor_value else None
        return RuntimeResult(status="RUNNING", cursor=cursor)

    def switch_model(self, handle: RuntimeHandle, provider: RuntimeProvider) -> None:
        self._request(
            "POST",
            f"/api/conversations/{handle.conversation_id}/switch_llm",
            base_url=self._base_url_for_handle(handle),
            session_api_key=self._session_key_for_handle(handle),
            json={"llm": self._llm_payload(provider)},
        )

    def interrupt(self, handle: RuntimeHandle) -> None:
        self._request(
            "POST",
            f"/api/conversations/{handle.conversation_id}/interrupt",
            base_url=self._base_url_for_handle(handle),
            session_api_key=self._session_key_for_handle(handle),
            json={},
        )

    def run(self, handle: RuntimeHandle) -> RuntimeResult:
        """Start the native OpenHands loop without fabricating a user message."""

        base_url = self._base_url_for_handle(handle)
        session_api_key = self._session_key_for_handle(handle)
        state = self._conversation_state(handle)
        status = str(state.get("execution_status") or "").lower()
        # Only IDLE/PAUSED are resumable. A worker may crash after a rejected
        # batch has already resumed and completed but before FlowWeave commits
        # its CAS; re-triggering FINISHED here would execute the turn twice.
        # RUNNING and terminal states are reconciled by the normal poll path.
        if status in {"idle", "paused"}:
            self._request(
                "POST",
                f"/api/conversations/{handle.conversation_id}/run",
                base_url=base_url,
                session_api_key=session_api_key,
                json={},
            )
        cursor = str(state.get("leaf_event_id") or handle.cursor or "") or None
        return RuntimeResult(status="RUNNING", cursor=cursor)

    def condense(self, handle: RuntimeHandle) -> RuntimeResult:
        """Request native condensation; completion is observed via event cursor."""

        self._request(
            "POST",
            f"/api/conversations/{handle.conversation_id}/condense",
            base_url=self._base_url_for_handle(handle),
            session_api_key=self._session_key_for_handle(handle),
        )
        # HTTP success only means CondensationRequest was accepted. The durable
        # Condensation event is projected by read_events after the agent step.
        return RuntimeResult(status="RUNNING", cursor=handle.cursor)

    def start_goal(self, handle: RuntimeHandle, objective: str, max_iterations: int) -> None:
        self._request(
            "POST",
            f"/api/conversations/{handle.conversation_id}/goal",
            base_url=self._base_url_for_handle(handle),
            session_api_key=self._session_key_for_handle(handle),
            json={"objective": objective, "max_iterations": max_iterations},
        )

    def stop_goal(self, handle: RuntimeHandle) -> None:
        self._request(
            "POST",
            f"/api/conversations/{handle.conversation_id}/goal/stop",
            base_url=self._base_url_for_handle(handle),
            session_api_key=self._session_key_for_handle(handle),
        )

    def resume_goal(self, handle: RuntimeHandle) -> None:
        self._request(
            "POST",
            f"/api/conversations/{handle.conversation_id}/goal/resume",
            base_url=self._base_url_for_handle(handle),
            session_api_key=self._session_key_for_handle(handle),
        )

    def ask_agent(
        self, handle: RuntimeHandle, question: str, *, timeout_seconds: float
    ) -> RuntimeAskAgentResult:
        base_url = self._base_url_for_handle(handle)
        session_api_key = self._session_key_for_handle(handle)
        before_state = self._conversation_state(handle)
        before = next(
            (
                item
                for item in self._usage_snapshots(before_state)
                if item.usage_id == "ask-agent-llm"
            ),
            None,
        )
        try:
            with httpx.Client(timeout=timeout_seconds, follow_redirects=False) as client:
                response = client.post(
                    f"{base_url}/api/conversations/{handle.conversation_id}/ask_agent",
                    headers={"X-Session-API-Key": session_api_key},
                    json={"question": question},
                )
                response.raise_for_status()
                value = cast(object, response.json())
        except httpx.TimeoutException as exc:
            raise DomainError(
                "RUNTIME_ASK_AGENT_TIMEOUT",
                "OpenHands ask_agent exceeded the governed timeout",
                504,
            ) from exc
        except httpx.HTTPStatusError as exc:
            if exc.response.status_code == 404:
                raise DomainError(
                    "RUNTIME_CONVERSATION_MISSING",
                    "Agent Runtime conversation no longer exists",
                    409,
                ) from exc
            raise DomainError(
                "EXECUTOR_UNAVAILABLE",
                "OpenHands ask_agent was rejected",
                503,
                {"status_code": exc.response.status_code},
            ) from exc
        except (httpx.HTTPError, ValueError) as exc:
            raise DomainError(
                "EXECUTOR_UNAVAILABLE",
                "OpenHands ask_agent is unavailable",
                503,
            ) from exc
        if not isinstance(value, dict):
            raise DomainError(
                "RUNTIME_ASK_AGENT_PROTOCOL_DRIFT",
                "OpenHands ask_agent returned an invalid response",
                502,
            )
        response_value = cast(dict[str, object], value).get("response")
        if not isinstance(response_value, str):
            raise DomainError(
                "RUNTIME_ASK_AGENT_PROTOCOL_DRIFT",
                "OpenHands ask_agent returned an invalid response",
                502,
            )
        answer = response_value
        if len(answer.encode("utf-8")) > 256 * 1024:
            raise DomainError(
                "RUNTIME_ASK_AGENT_RESPONSE_TOO_LARGE",
                "OpenHands ask_agent response exceeds the governed size limit",
                502,
            )
        after_state = self._conversation_state(handle)
        after = next(
            (
                item
                for item in self._usage_snapshots(after_state)
                if item.usage_id == "ask-agent-llm"
            ),
            None,
        )
        return RuntimeAskAgentResult(response=answer, before_usage=before, after_usage=after)

    def fork_conversation(
        self,
        handle: RuntimeHandle,
        *,
        target_conversation_id: str,
        title: str,
        from_event_id: str | None,
        expected_source_leaf_event_id: str,
        reset_metrics: bool,
    ) -> RuntimeForkResult:
        """Create or recover one native fork with a caller-owned identity."""

        base_url = self._base_url_for_handle(handle)
        session_api_key = self._session_key_for_handle(handle)
        source_state = self._conversation_state(handle)
        source_leaf = str(source_state.get("leaf_event_id") or "") or None
        source_status = str(source_state.get("execution_status") or "").lower()
        if source_leaf != expected_source_leaf_event_id:
            raise DomainError(
                "RUNTIME_FORK_HEAD_DRIFT",
                "OpenHands source HEAD changed before the native fork",
                409,
            )
        if source_status in {"starting", "running", "executing", "stopping"}:
            raise DomainError(
                "RUNTIME_FORK_SOURCE_BUSY",
                "OpenHands source conversation is still executing",
                409,
            )
        payload: dict[str, object] = {
            "id": target_conversation_id,
            "title": title,
            "reset_metrics": reset_metrics,
        }
        if from_event_id is not None:
            payload["from_event_id"] = from_event_id
        try:
            created = self._request(
                "POST",
                f"/api/conversations/{handle.conversation_id}/fork",
                base_url=base_url,
                session_api_key=session_api_key,
                json=payload,
            )
        except DomainError as exc:
            if not (exc.code == "EXECUTOR_UNAVAILABLE" and exc.details.get("status_code") == 409):
                raise
            created = self._request(
                "GET",
                f"/api/conversations/{target_conversation_id}",
                base_url=base_url,
                session_api_key=session_api_key,
            )

        created_id = str(created.get("id") or "")
        source_id = str(created.get("forked_from_conversation_id") or "")
        raw_source_event = created.get("forked_from_event_id")
        source_event = str(raw_source_event) if raw_source_event is not None else None
        leaf = str(created.get("leaf_event_id") or "") or None
        if (
            created_id != target_conversation_id
            or source_id != handle.conversation_id
            or source_event != from_event_id
            or leaf != (from_event_id or expected_source_leaf_event_id)
        ):
            raise DomainError(
                "RUNTIME_FORK_IDENTITY_DRIFT",
                "OpenHands returned a fork with mismatched source or event identity",
                409,
            )
        fork_handle = RuntimeHandle(
            job_id=handle.job_id,
            conversation_id=created_id,
            cursor=leaf,
            runtime_resource_id=handle.runtime_resource_id,
            runtime_resource_name=handle.runtime_resource_name,
        )
        return RuntimeForkResult(
            handle=fork_handle,
            source_conversation_id=source_id,
            source_event_id=source_event,
            leaf_event_id=leaf,
            reset_metrics=reset_metrics,
        )

    def resume(
        self, handle: RuntimeHandle, content: str, image_urls: tuple[str, ...] = ()
    ) -> RuntimeResult:
        self.interrupt(handle)
        return self.send_message(handle, content, image_urls)

    def cancel(self, handle: RuntimeHandle) -> None:
        path = f"/api/conversations/{handle.conversation_id}"
        base_url = self._base_url_for_handle(handle)
        route = self._environment_route(handle.job_id)
        try:
            interrupted = self._request(
                "POST",
                f"{path}/interrupt",
                missing_ok=True,
                base_url=base_url,
                session_api_key=self._session_key_for_handle(handle),
                json={},
            )
            if interrupted.get("_flowweave_missing"):
                return
            for poll_no in range(10):
                data = self._request(
                    "GET",
                    path,
                    missing_ok=True,
                    base_url=base_url,
                    session_api_key=self._session_key_for_handle(handle),
                )
                if data.get("_flowweave_missing"):
                    return
                status = str(data.get("execution_status") or "").lower()
                if status not in {"starting", "running", "executing", "stopping"}:
                    return
                if poll_no < 9:
                    time.sleep(0.1)
            raise DomainError(
                "EXECUTOR_CANCEL_UNCONFIRMED",
                "OpenHands accepted the interrupt but the Agent is still running",
                503,
                {"conversation_id": handle.conversation_id},
            )
        except DomainError:
            # Managed Runtime deletion is authoritative and handled by the
            # sandbox control plane. The HTTP endpoint may already be gone.
            if route is None:
                raise
