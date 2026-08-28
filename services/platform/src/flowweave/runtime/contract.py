from __future__ import annotations

from typing import Any, cast

from flowweave.runtime.base import RuntimeContract
from flowweave.shared.domain.tool_policy import (
    OPENHANDS_SOURCE_COMMIT,
    OPENHANDS_VERSION,
)

RUNTIME_CONTRACT_SCHEMA_VERSION = 3

OPENHANDS_PACKAGE_VERSIONS: tuple[tuple[str, str], ...] = (
    ("openhands-agent-server", OPENHANDS_VERSION),
    ("openhands-sdk", OPENHANDS_VERSION),
    ("openhands-tools", OPENHANDS_VERSION),
    ("openhands-workspace", OPENHANDS_VERSION),
)

# Only public HTTP operations used by FlowWeave's production RuntimePort are
# frozen here.  Unsupported product surfaces do not become requirements merely
# because the target Agent Server happens to expose them.
REQUIRED_HTTP_OPERATIONS: tuple[tuple[str, str], ...] = tuple(
    sorted(
        {
            ("GET", "/ready"),
            ("GET", "/server_info"),
            ("POST", "/api/conversations"),
            ("GET", "/api/conversations/{conversation_id}"),
            ("POST", "/api/conversations/{conversation_id}/events"),
            ("GET", "/api/conversations/{conversation_id}/events/{event_id}"),
            ("GET", "/api/conversations/{conversation_id}/events/search"),
            ("POST", "/api/conversations/{conversation_id}/interrupt"),
            ("POST", "/api/conversations/{conversation_id}/switch_llm"),
            (
                "POST",
                "/api/conversations/{conversation_id}/events/respond_to_confirmation",
            ),
            ("POST", "/api/conversations/{conversation_id}/run"),
            ("POST", "/api/conversations/{conversation_id}/condense"),
            ("POST", "/api/conversations/{conversation_id}/fork"),
            ("POST", "/api/conversations/{conversation_id}/goal"),
            ("POST", "/api/conversations/{conversation_id}/goal/resume"),
            ("POST", "/api/conversations/{conversation_id}/goal/stop"),
            ("POST", "/api/conversations/{conversation_id}/ask_agent"),
        }
    )
)

REQUIRED_START_FIELDS: tuple[str, ...] = tuple(
    sorted(
        {
            "agent",
            "agent_definitions",
            "confirmation_policy",
            "hook_config",
            "initial_message",
            "max_iterations",
            "observability_metadata",
            "plugins",
            "worktree",
            "workspace",
        }
    )
)

# The target 1.44.0 server currently declares only credential-binding
# capabilities.  FlowWeave does not consume that product surface, so the
# governed requirement is deliberately empty.  The adapter still requires the
# formal ServerInfo.capabilities field to be a list of unique strings.
REQUIRED_SERVER_CAPABILITIES: tuple[str, ...] = ()


def governed_runtime_contract(required_tools: tuple[str, ...]) -> RuntimeContract:
    normalized_tools = tuple(sorted(set(required_tools)))
    if not normalized_tools or len(normalized_tools) != len(required_tools):
        raise ValueError("Runtime contract tools must be non-empty and unique")
    return RuntimeContract(
        schema_version=RUNTIME_CONTRACT_SCHEMA_VERSION,
        openhands_version=OPENHANDS_VERSION,
        source_commit=OPENHANDS_SOURCE_COMMIT,
        source_ref=OPENHANDS_SOURCE_COMMIT,
        package_versions=OPENHANDS_PACKAGE_VERSIONS,
        required_http_operations=REQUIRED_HTTP_OPERATIONS,
        required_start_fields=REQUIRED_START_FIELDS,
        required_server_capabilities=REQUIRED_SERVER_CAPABILITIES,
        required_tools=normalized_tools,
    )


def agent_workspace_runtime_contract(required_tools: tuple[str, ...]) -> RuntimeContract:
    """Contract for the standalone Workspace surface, separate from Flow snapshots."""

    base = governed_runtime_contract(required_tools)
    return RuntimeContract(
        schema_version=4,
        openhands_version=base.openhands_version,
        source_commit=base.source_commit,
        source_ref=base.source_ref,
        package_versions=base.package_versions,
        required_http_operations=tuple(
            sorted(
                set(base.required_http_operations)
                | {
                    ("PATCH", "/api/conversations/{conversation_id}"),
                    ("DELETE", "/api/conversations/{conversation_id}"),
                    ("POST", "/api/conversations/{conversation_id}/load_plugin"),
                }
            )
        ),
        required_start_fields=tuple(sorted(set(base.required_start_fields) | {"conversation_id"})),
        required_server_capabilities=base.required_server_capabilities,
        required_tools=base.required_tools,
    )


def runtime_contract_document(contract: RuntimeContract) -> dict[str, Any]:
    return {
        "schema_version": contract.schema_version,
        "openhands_version": contract.openhands_version,
        "source_commit": contract.source_commit,
        "source_ref": contract.source_ref,
        "package_versions": dict(contract.package_versions),
        "required_http_operations": [
            {"method": method, "path": path} for method, path in contract.required_http_operations
        ],
        "required_start_fields": list(contract.required_start_fields),
        "required_server_capabilities": list(contract.required_server_capabilities),
        "required_tools": list(contract.required_tools),
    }


def compile_runtime_contract(required_tools: tuple[str, ...]) -> dict[str, Any]:
    return runtime_contract_document(governed_runtime_contract(required_tools))


def normalize_runtime_contract(
    value: object, *, required_tools: tuple[str, ...]
) -> RuntimeContract:
    if not isinstance(value, dict):
        raise ValueError("Runtime contract must be an object")
    document = cast(dict[object, object], value)
    expected = governed_runtime_contract(required_tools)
    expected_document = runtime_contract_document(expected)
    if document != expected_document:
        raise ValueError(
            "Runtime contract does not match the governed OpenHands source and protocol"
        )
    return expected
