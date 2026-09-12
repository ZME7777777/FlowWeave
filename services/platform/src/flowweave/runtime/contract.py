from __future__ import annotations

from typing import Any, cast

from flowweave.runtime.base import RuntimeContract
from flowweave.shared.domain.openhands import (
    CURRENT_OPENHANDS_SERVER_IDENTITY,
    OPENHANDS_VERSION,
    OpenHandsServerIdentity,
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

# The target 1.47.0 server currently declares only credential-binding
# capabilities.  FlowWeave does not consume that product surface, so the
# governed requirement is deliberately empty.  The adapter still requires the
# formal ServerInfo.capabilities field to be a list of unique strings.
REQUIRED_SERVER_CAPABILITIES: tuple[str, ...] = ()


def governed_runtime_contract(required_tools: tuple[str, ...]) -> RuntimeContract:
    return runtime_contract_for_server_identity(required_tools, CURRENT_OPENHANDS_SERVER_IDENTITY)


def runtime_contract_for_server_identity(
    required_tools: tuple[str, ...], identity: OpenHandsServerIdentity
) -> RuntimeContract:
    """Build the protocol contract for one immutable Runtime image.

    A FlowRun validates the Agent Server packaged by its frozen Environment
    Version, not a newer control-plane OpenHands baseline.
    """

    normalized_tools = tuple(sorted(set(required_tools)))
    if (
        not normalized_tools
        or len(normalized_tools) != len(required_tools)
        or not all((identity.package_version, identity.source_commit, identity.source_ref))
    ):
        raise ValueError("Runtime contract tools must be non-empty and unique")
    return RuntimeContract(
        schema_version=RUNTIME_CONTRACT_SCHEMA_VERSION,
        openhands_version=identity.package_version,
        source_commit=identity.source_commit,
        source_ref=identity.source_ref,
        package_versions=tuple(
            (package, identity.package_version) for package, _version in OPENHANDS_PACKAGE_VERSIONS
        ),
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
    """Parse a Snapshot's immutable Agent Server contract.

    Snapshots must retain the protocol and server identity used when they were
    created. Comparing the serialized document with a contract generated from
    the *current* OpenHands baseline makes every baseline upgrade invalidate
    prior nodes. The adapter will validate a live Agent Server against this
    parsed, frozen contract before it sends any OpenHands command.
    """

    if not isinstance(value, dict):
        raise ValueError("Runtime contract must be an object")
    document = cast(dict[object, object], value)
    expected_keys = {
        "schema_version",
        "openhands_version",
        "source_commit",
        "source_ref",
        "package_versions",
        "required_http_operations",
        "required_start_fields",
        "required_server_capabilities",
        "required_tools",
    }
    if {str(key) for key in document} != expected_keys:
        raise ValueError("Runtime contract fields are invalid")

    schema_version = document.get("schema_version")
    openhands_version = document.get("openhands_version")
    source_commit = document.get("source_commit")
    source_ref = document.get("source_ref")
    packages = document.get("package_versions")
    operations = document.get("required_http_operations")
    start_fields = document.get("required_start_fields")
    capabilities = document.get("required_server_capabilities")
    tools = document.get("required_tools")

    if (
        not isinstance(schema_version, int)
        or isinstance(schema_version, bool)
        or schema_version < 1
        or not all(
            isinstance(item, str) and item
            for item in (openhands_version, source_commit, source_ref)
        )
        or not isinstance(packages, dict)
        or not isinstance(operations, list)
        or not isinstance(start_fields, list)
        or not isinstance(capabilities, list)
        or not isinstance(tools, list)
    ):
        raise ValueError("Runtime contract fields are invalid")
    package_document = cast(dict[str, object], packages)
    operation_documents = cast(list[object], operations)
    start_field_documents = cast(list[object], start_fields)
    capability_documents = cast(list[object], capabilities)
    tool_documents = cast(list[object], tools)
    frozen_openhands_version = cast(str, openhands_version)
    frozen_source_commit = cast(str, source_commit)
    frozen_source_ref = cast(str, source_ref)

    expected_package_names = {
        "openhands-agent-server",
        "openhands-sdk",
        "openhands-tools",
        "openhands-workspace",
    }
    package_versions = {name: value for name, value in package_document.items()}
    if (
        set(package_versions) != expected_package_names
        or any(not isinstance(item, str) or not item for item in package_versions.values())
        or len(set(package_versions.values())) != 1
        or package_versions["openhands-agent-server"] != frozen_openhands_version
    ):
        raise ValueError("Runtime contract package versions are invalid")

    parsed_operations: list[tuple[str, str]] = []
    for operation in operation_documents:
        if not isinstance(operation, dict):
            raise ValueError("Runtime contract operations are invalid")
        item = cast(dict[str, object], operation)
        method, path = item.get("method"), item.get("path")
        if not isinstance(method, str) or not method or not isinstance(path, str) or not path:
            raise ValueError("Runtime contract operations are invalid")
        parsed_operations.append((method, path))
    if any(not isinstance(item, str) for item in start_field_documents):
        raise ValueError("Runtime contract requirements are invalid")
    if any(not isinstance(item, str) for item in capability_documents):
        raise ValueError("Runtime contract requirements are invalid")
    if any(not isinstance(item, str) for item in tool_documents):
        raise ValueError("Runtime contract requirements are invalid")
    parsed_start_fields = tuple(cast(str, item) for item in start_field_documents)
    parsed_capabilities = tuple(cast(str, item) for item in capability_documents)
    parsed_tools = tuple(cast(str, item) for item in tool_documents)
    if (
        any(not item for item in (*parsed_start_fields, *parsed_capabilities, *parsed_tools))
        or len(set(parsed_operations)) != len(parsed_operations)
        or len(set(parsed_start_fields)) != len(parsed_start_fields)
        or len(set(parsed_capabilities)) != len(parsed_capabilities)
        or tuple(sorted(set(parsed_tools))) != tuple(sorted(set(required_tools)))
        or len(parsed_tools) != len(set(parsed_tools))
    ):
        raise ValueError("Runtime contract requirements are invalid")

    return RuntimeContract(
        schema_version=schema_version,
        openhands_version=frozen_openhands_version,
        source_commit=frozen_source_commit,
        source_ref=frozen_source_ref,
        package_versions=tuple(
            sorted((name, cast(str, version)) for name, version in package_versions.items())
        ),
        required_http_operations=tuple(sorted(parsed_operations)),
        required_start_fields=tuple(sorted(parsed_start_fields)),
        required_server_capabilities=tuple(sorted(parsed_capabilities)),
        required_tools=tuple(sorted(parsed_tools)),
    )
