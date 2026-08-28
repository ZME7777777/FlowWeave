from __future__ import annotations

import copy
import hashlib
import json
from typing import Any, cast

OPENHANDS_VERSION = "1.44.0"
OPENHANDS_SOURCE_COMMIT = "9a24f6c8866f353042a57df0514ccc900e3a0691"
TOOL_POLICY_SCHEMA_VERSION = 2
MAX_TOOL_CONCURRENCY = 16

# Exported from the actual pinned Runtime image.  Keep entries that are not yet
# policy-enabled: their presence is part of the drift contract and their reason
# explains why FlowWeave rejects them instead of silently pretending they do not
# exist.  Parameter schemas describe Tool.create(), not LLM action arguments.
OPENHANDS_TOOL_CATALOG: dict[str, dict[str, Any]] = {
    "ask_oracle": {
        "module": "openhands.tools.ask_oracle.definition",
        "params": {},
        "access": "OPEN_WORLD",
        "confirmation": "REQUIRED",
        "concurrency": "SERIAL_ONLY",
        "policy_enabled": False,
        "disabled_reason": (
            "requires a governed read-at-use LLM profile named oracle; enable in FR-77"
        ),
    },
    "file_editor": {
        "module": "openhands.tools.file_editor.definition",
        "params": {},
        "access": "READ_WRITE",
        "confirmation": "REQUIRED",
        "concurrency": "RESOURCE_LOCKED",
        "policy_enabled": True,
    },
    "task_tool_set": {
        "module": "openhands.tools.task.definition",
        "params": {},
        "access": "CONTROL",
        "confirmation": "REQUIRED",
        "concurrency": "SERIAL_ONLY",
        "policy_enabled": True,
    },
    "task": {
        "module": "openhands.tools.task.definition",
        "params": {},
        "access": "CONTROL",
        "confirmation": "REQUIRED",
        "concurrency": "SERIAL_ONLY",
        "policy_enabled": False,
        "disabled_reason": "requires an OpenHands-internal TaskExecutor; use task_tool_set",
    },
    "task_tracker": {
        "module": "openhands.tools.task_tracker.definition",
        "params": {},
        "access": "CONTROL",
        "confirmation": "REQUIRED",
        "concurrency": "SERIAL_ONLY",
        "policy_enabled": True,
    },
    "terminal": {
        "module": "openhands.tools.terminal.definition",
        "params": {
            "username": {"type": "string", "max_length": 128},
            "no_change_timeout_seconds": {"type": "integer", "minimum": 1, "maximum": 86400},
            "terminal_type": {"type": "string", "enum": ["tmux", "subprocess", "powershell"]},
            "shell_path": {"type": "string", "max_length": 1024},
        },
        "access": "READ_WRITE",
        "confirmation": "REQUIRED",
        # Terminal commands can mutate arbitrary workspace state which cannot be
        # described by a stable resource key at policy compilation time.
        "concurrency": "SERIAL_ONLY",
        "policy_enabled": True,
    },
    "workflow_tool_set": {
        "module": "openhands.tools.workflow.definition",
        "params": {},
        "access": "CONTROL",
        "confirmation": "REQUIRED",
        "concurrency": "SERIAL_ONLY",
        "policy_enabled": True,
    },
    "workflow": {
        "module": "openhands.tools.workflow.definition",
        "params": {},
        "access": "CONTROL",
        "confirmation": "REQUIRED",
        "concurrency": "SERIAL_ONLY",
        "policy_enabled": False,
        "disabled_reason": "requires an OpenHands-internal WorkflowExecutor; use workflow_tool_set",
    },
    "browser_tool_set": {
        "module": "openhands.tools.browser_use.definition",
        "params": {},
        "access": "OPEN_WORLD",
        "confirmation": "REQUIRED",
        "concurrency": "SERIAL_ONLY",
        "policy_enabled": False,
        "disabled_reason": (
            "browser network, credential, artifact, and SSRF controls are not installed"
        ),
    },
    "edit": {
        "module": "openhands.tools.gemini.edit.definition",
        "params": {},
        "access": "READ_WRITE",
        "confirmation": "REQUIRED",
        "concurrency": "RESOURCE_LOCKED",
        "policy_enabled": True,
    },
    "list_directory": {
        "module": "openhands.tools.gemini.list_directory.definition",
        "params": {},
        "access": "READ_ONLY",
        "confirmation": "NONE",
        "concurrency": "READ_ONLY",
        "policy_enabled": True,
    },
    "read_file": {
        "module": "openhands.tools.gemini.read_file.definition",
        "params": {},
        "access": "READ_ONLY",
        "confirmation": "NONE",
        "concurrency": "READ_ONLY",
        "policy_enabled": True,
    },
    "write_file": {
        "module": "openhands.tools.gemini.write_file.definition",
        "params": {},
        "access": "READ_WRITE",
        "confirmation": "REQUIRED",
        "concurrency": "RESOURCE_LOCKED",
        "policy_enabled": True,
    },
    "glob": {
        "module": "openhands.tools.glob.definition",
        "params": {},
        "access": "READ_ONLY",
        "confirmation": "NONE",
        "concurrency": "READ_ONLY",
        "policy_enabled": True,
    },
    "grep": {
        "module": "openhands.tools.grep.definition",
        "params": {},
        "access": "READ_ONLY",
        "confirmation": "NONE",
        "concurrency": "READ_ONLY",
        "policy_enabled": True,
    },
    "planning_file_editor": {
        "module": "openhands.tools.planning_file_editor.definition",
        "params": {"plan_path": {"type": "string", "max_length": 4096}},
        "access": "READ_WRITE",
        "confirmation": "REQUIRED",
        "concurrency": "SERIAL_ONLY",
        "policy_enabled": True,
    },
}
ALLOWED_OPENHANDS_TOOLS = frozenset(
    name for name, item in OPENHANDS_TOOL_CATALOG.items() if item["policy_enabled"]
)
DEFAULT_TOOL_POLICY_KEY = "flowweave-default-tools"


def _catalog_digest() -> str:
    governed_catalog = {
        name: {key: copy.deepcopy(value) for key, value in item.items() if key != "disabled_reason"}
        for name, item in OPENHANDS_TOOL_CATALOG.items()
    }
    encoded = json.dumps(
        governed_catalog,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode()
    return hashlib.sha256(encoded).hexdigest()


OPENHANDS_TOOL_CATALOG_DIGEST = _catalog_digest()


def tool_policy_catalog() -> dict[str, Any]:
    """Expose the pinned, governed Tool.create() catalog for configuration UIs."""

    return {
        "schema_version": TOOL_POLICY_SCHEMA_VERSION,
        "openhands_version": OPENHANDS_VERSION,
        "source_commit": OPENHANDS_SOURCE_COMMIT,
        "catalog_digest": OPENHANDS_TOOL_CATALOG_DIGEST,
        "max_tool_concurrency": MAX_TOOL_CONCURRENCY,
        "tools": [
            {
                "name": name,
                "module": str(item["module"]),
                "params": copy.deepcopy(item["params"]),
                "access": str(item["access"]),
                "confirmation": str(item["confirmation"]),
                "concurrency": str(item["concurrency"]),
                "policy_enabled": bool(item["policy_enabled"]),
                "disabled_reason": item.get("disabled_reason"),
            }
            for name, item in OPENHANDS_TOOL_CATALOG.items()
        ],
    }


def normalize_tool_entries(value: object) -> list[dict[str, Any]]:
    """Validate the OpenHands 1.44.0 Tool subset governed by FlowWeave.

    The catalog and create-parameter schemas are frozen from the pinned image.
    Unknown names, disabled low-level entries, and undeclared parameters fail
    closed before an immutable Capability Version can be published.
    """

    if not isinstance(value, list):
        raise ValueError("tools must be a non-empty list with at most 20 entries")
    raw_tools = cast(list[object], value)
    if not raw_tools or len(raw_tools) > 20:
        raise ValueError("tools must be a non-empty list with at most 20 entries")
    result: list[dict[str, Any]] = []
    seen: set[str] = set()
    for raw_tool in raw_tools:
        if not isinstance(raw_tool, dict):
            raise ValueError("each tool must be an object")
        tool = cast(dict[object, object], raw_tool)
        unknown = sorted(
            str(key)
            for key in tool
            if str(key)
            not in {
                "name",
                "params",
                "parameter_limits",
                "access",
                "confirmation",
                "concurrency",
                "source",
            }
        )
        if unknown:
            raise ValueError(f"tool contains unsupported fields: {', '.join(unknown)}")
        name = str(tool.get("name") or "").strip()
        catalog_item = OPENHANDS_TOOL_CATALOG.get(name)
        if catalog_item is None:
            raise ValueError(
                f"tool is not registered by OpenHands {OPENHANDS_VERSION}: {name or '<blank>'}"
            )
        if name not in ALLOWED_OPENHANDS_TOOLS:
            reason = str(catalog_item.get("disabled_reason") or "not policy-enabled")
            raise ValueError(f"tool is fail-closed by FlowWeave: {name} ({reason})")
        if name in seen:
            raise ValueError(f"tool names must be unique: {name}")
        params = tool.get("params", {})
        if not isinstance(params, dict):
            raise ValueError(f"tool params must be an object: {name}")
        normalized_params = _normalize_tool_params(name, cast(dict[object, object], params))
        governed = {
            "name": name,
            "params": normalized_params,
            "parameter_limits": copy.deepcopy(catalog_item["params"]),
            "access": str(catalog_item["access"]),
            "confirmation": str(catalog_item["confirmation"]),
            "concurrency": str(catalog_item["concurrency"]),
            "source": {
                "distribution": "openhands-tools",
                "version": OPENHANDS_VERSION,
                "module": str(catalog_item["module"]),
                "source_commit": OPENHANDS_SOURCE_COMMIT,
            },
        }
        for field in (
            "parameter_limits",
            "access",
            "confirmation",
            "concurrency",
            "source",
        ):
            if field in tool and tool[field] != governed[field]:
                raise ValueError(f"tool {name} frozen {field} does not match the catalog")
        seen.add(name)
        result.append(governed)
    return result


def _normalize_tool_params(name: str, value: dict[object, object]) -> dict[str, Any]:
    schema = cast(dict[str, dict[str, Any]], OPENHANDS_TOOL_CATALOG[name]["params"])
    unknown = sorted(str(key) for key in value if str(key) not in schema)
    if unknown:
        raise ValueError(f"tool {name} contains unsupported params: {', '.join(unknown)}")
    result: dict[str, Any] = {}
    for raw_key, raw_value in value.items():
        key = str(raw_key)
        field = schema[key]
        expected = field["type"]
        if expected == "string":
            if not isinstance(raw_value, str) or not raw_value:
                raise ValueError(f"tool {name} param {key} must be a non-empty string")
            max_length = field.get("max_length")
            if max_length is not None and len(raw_value) > int(max_length):
                raise ValueError(f"tool {name} param {key} is too long")
            allowed = field.get("enum")
            if isinstance(allowed, list) and raw_value not in allowed:
                raise ValueError(f"tool {name} param {key} is not an allowed value")
        elif expected == "integer":
            if not isinstance(raw_value, int) or isinstance(raw_value, bool):
                raise ValueError(f"tool {name} param {key} must be an integer")
            if not int(field["minimum"]) <= raw_value <= int(field["maximum"]):
                raise ValueError(f"tool {name} param {key} is outside the allowed range")
        else:  # pragma: no cover - catalog authoring guard
            raise RuntimeError(f"unsupported Tool Catalog schema type: {expected}")
        result[key] = raw_value
    return result


def normalize_tool_policy_document(
    value: object, *, fallback_key: str
) -> tuple[str, dict[str, Any]]:
    if not isinstance(value, dict):
        raise ValueError("Tool Policy root must be an object")
    document = cast(dict[object, object], value)
    allowed_fields = {
        "name",
        "description",
        "schema_version",
        "openhands_version",
        "source_commit",
        "catalog_digest",
        "unknown_tool",
        "tool_concurrency_limit",
        "confirmation_required_tools",
        "tools",
    }
    unknown = sorted(str(key) for key in document if str(key) not in allowed_fields)
    if unknown:
        raise ValueError(f"Tool Policy contains unsupported fields: {', '.join(unknown)}")
    key = str(document.get("name") or fallback_key).strip()
    if not key or len(key) > 200:
        raise ValueError("Tool Policy name is invalid")
    description = str(document.get("description") or "").strip()
    if len(description) > 2000:
        raise ValueError("Tool Policy description is too long")
    expected_metadata: dict[str, object] = {
        "schema_version": TOOL_POLICY_SCHEMA_VERSION,
        "openhands_version": OPENHANDS_VERSION,
        "source_commit": OPENHANDS_SOURCE_COMMIT,
        "catalog_digest": OPENHANDS_TOOL_CATALOG_DIGEST,
        "unknown_tool": "DENY",
    }
    for field, expected in expected_metadata.items():
        if field in document and document[field] != expected:
            raise ValueError(f"Tool Policy {field} does not match the governed catalog")
    concurrency = document.get("tool_concurrency_limit", 1)
    if (
        not isinstance(concurrency, int)
        or isinstance(concurrency, bool)
        or not 1 <= concurrency <= MAX_TOOL_CONCURRENCY
    ):
        raise ValueError(f"tool_concurrency_limit must be between 1 and {MAX_TOOL_CONCURRENCY}")
    tools = normalize_tool_entries(document.get("tools"))
    if concurrency > 1:
        serial_tools = sorted(
            str(tool["name"]) for tool in tools if tool["concurrency"] == "SERIAL_ONLY"
        )
        if serial_tools:
            raise ValueError(
                "concurrent Tool Policy contains tools without a safe read-only or "
                f"resource-lock contract: {', '.join(serial_tools)}"
            )
    confirmation_required = sorted(
        str(tool["name"]) for tool in tools if tool["confirmation"] == "REQUIRED"
    )
    if (
        "confirmation_required_tools" in document
        and document["confirmation_required_tools"] != confirmation_required
    ):
        raise ValueError(
            "Tool Policy confirmation_required_tools does not match the governed catalog"
        )
    return key, {
        "description": description,
        **expected_metadata,
        "tool_concurrency_limit": concurrency,
        "confirmation_required_tools": confirmation_required,
        "tools": tools,
    }


_DEFAULT_TOOL_POLICY_DOCUMENT: dict[str, Any] = {
    "name": DEFAULT_TOOL_POLICY_KEY,
    "description": "FlowWeave default OpenHands 1.44.0 tool policy",
    "tool_concurrency_limit": 1,
    "tools": [
        {"name": "terminal", "params": {}},
        {"name": "file_editor", "params": {}},
        {"name": "task_tracker", "params": {}},
    ],
}
_, DEFAULT_TOOL_POLICY_CONFIG = normalize_tool_policy_document(
    _DEFAULT_TOOL_POLICY_DOCUMENT,
    fallback_key=DEFAULT_TOOL_POLICY_KEY,
)
