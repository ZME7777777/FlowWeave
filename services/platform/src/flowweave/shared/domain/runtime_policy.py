from __future__ import annotations

import re
from typing import Any, cast

_UUID = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-"
    r"[89ab][0-9a-f]{3}-[0-9a-f]{12}$"
)
_DIGEST = re.compile(r"^[0-9a-f]{64}$")
_MEMORY_SCOPES = frozenset(
    {"CONVERSATION", "ATTEMPT", "NODE_ASSET", "PROJECT", "USER", "ORGANIZATION"}
)
_CRITIC_MODES = frozenset({"FINISH_AND_MESSAGE", "ALL_ACTIONS"})
_SECRET_FIELDS = frozenset(
    {"api_key", "apikey", "token", "secret", "password", "authorization", "critic_api_key"}
)
OPENHANDS_AGENT_PROFILE_SCHEMA_VERSION = 2
OPENHANDS_AGENT_PROFILE_FIELD_MATRIX: dict[str, str] = {
    "schema_version": "VALIDATE_EXACT",
    "id": "SOURCE_PROVENANCE_ONLY",
    "name": "CAPABILITY_PACKAGE_IDENTITY",
    "revision": "SOURCE_PROVENANCE_ONLY",
    "mcp_server_refs": "MATCH_FROZEN_MCP_BINDINGS",
    "agent_kind": "OPENHANDS_ONLY",
    "llm_profile_ref": "FLOWWEAVE_MODEL_SELECTION",
    "agent": "CODE_ACT_AGENT_ONLY",
    "tools": "MATCH_FROZEN_TOOL_POLICY",
    "system_message_suffix": "MATCH_FROZEN_CONTEXT_POLICY",
    "disabled_skills": "MATCH_FROZEN_CONTEXT_POLICY",
    "condenser": "MATCH_FROZEN_EXECUTOR_POLICY",
    "verification": "MATCH_FROZEN_CRITIC_POLICY",
    "enable_sub_agents": "MATCH_FROZEN_AGENT_DEFINITIONS",
    "enable_switch_llm_tool": "DISABLED_MUTABLE_LLM_STORE",
    "tool_concurrency_limit": "MATCH_FROZEN_TOOL_POLICY",
}

DEFAULT_CONTEXT_POLICY_KEY = "flowweave-default-context"
DEFAULT_CONTEXT_POLICY_CONFIG: dict[str, Any] = {
    # This text is part of the immutable v1 capability identity installed by
    # migration 0032. Changing it without publishing a new Version makes the
    # repository return a Blob whose content hash does not match v1's digest.
    "description": "FlowWeave default OpenHands 1.40.0 context policy",
    "system_message_suffix": "",
    "user_message_suffix": "",
    "load_user_skills": False,
    "load_public_skills": False,
    "marketplace_path": None,
    "load_project_skills": False,
    "registered_marketplaces": [],
    "disabled_skills": [],
}
DEFAULT_MEMORY_POLICY_KEY = "flowweave-memory-disabled"
DEFAULT_MEMORY_POLICY_CONFIG: dict[str, Any] = {
    "description": "FlowWeave default fail-closed Memory policy",
    "enabled": False,
    "scopes": [],
    "source_refs": [],
    "retention_days": None,
    "require_review": True,
    "sensitive_data_scan": True,
    "replay_mode": "FROZEN",
}
DEFAULT_CRITIC_POLICY_KEY = "flowweave-critic-disabled"
DEFAULT_CRITIC_POLICY_CONFIG: dict[str, Any] = {
    "description": "FlowWeave default fail-closed Critic policy",
    "enabled": False,
    "mode": "FINISH_AND_MESSAGE",
    "threshold": 0.6,
    "max_refinement_iterations": 0,
}


def _document(value: object, *, label: str, fields: set[str]) -> dict[object, object]:
    if not isinstance(value, dict):
        raise ValueError(f"{label} root must be an object")
    document = cast(dict[object, object], value)
    unknown = sorted(str(key) for key in document if str(key) not in fields)
    if unknown:
        raise ValueError(f"{label} contains unsupported fields: {', '.join(unknown)}")
    return document


def _identity(document: dict[object, object], fallback_key: str, label: str) -> tuple[str, str]:
    name = str(document.get("name") or fallback_key).strip()
    description = str(document.get("description") or "").strip()
    if not name or len(name) > 200:
        raise ValueError(f"{label} name is invalid")
    if len(description) > 4000:
        raise ValueError(f"{label} description is too long")
    return name, description


def _bounded_text(value: object, *, field: str, maximum: int = 100_000) -> str:
    if value is None:
        return ""
    if not isinstance(value, str) or len(value) > maximum:
        raise ValueError(f"{field} must be a bounded string")
    return value


def _reject_profile_secrets(value: object, *, path: str = "$") -> None:
    if isinstance(value, dict):
        for key, item in cast(dict[object, object], value).items():
            if str(key).lower() in _SECRET_FIELDS:
                raise ValueError(f"Agent Profile cannot contain Secret field {path}.{key}")
            _reject_profile_secrets(item, path=f"{path}.{key}")
    elif isinstance(value, list):
        for index, item in enumerate(cast(list[object], value)):
            _reject_profile_secrets(item, path=f"{path}[{index}]")


def _optional_uuid(value: object, *, field: str) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str) or _UUID.fullmatch(value) is None:
        raise ValueError(f"{field} must reference an immutable Capability Version UUID")
    return value


def context_policy_config(
    *,
    description: str = "",
    system_message_suffix: str = "",
    user_message_suffix: str = "",
    disabled_skills: list[str] | None = None,
) -> dict[str, Any]:
    """Build one complete, explicit AgentContext policy document."""

    return {
        "description": description.strip(),
        "system_message_suffix": system_message_suffix,
        "user_message_suffix": user_message_suffix,
        "load_user_skills": False,
        "load_public_skills": False,
        "marketplace_path": None,
        "load_project_skills": False,
        "registered_marketplaces": [],
        "disabled_skills": sorted(set(disabled_skills or [])),
    }


def normalize_context_policy_document(
    value: object, *, fallback_key: str
) -> tuple[str, dict[str, Any]]:
    """Normalize the explicit, reproducible AgentContext surface.

    Mutable user/public/project discovery and Marketplace auto-load are always
    disabled. Bound Skill and Plugin versions remain the only Runtime sources.
    """

    document = _document(
        value,
        label="Context Policy",
        fields={
            "name",
            "description",
            "system_message_suffix",
            "user_message_suffix",
            "load_user_skills",
            "load_public_skills",
            "marketplace_path",
            "load_project_skills",
            "registered_marketplaces",
            "disabled_skills",
        },
    )
    _reject_profile_secrets(document)
    name, description = _identity(document, fallback_key, "Context Policy")
    for field in ("load_user_skills", "load_public_skills", "load_project_skills"):
        if document.get(field, False) is not False:
            raise ValueError(f"Context Policy {field} must be false for reproducible runs")
    if document.get("registered_marketplaces", []) != []:
        raise ValueError("Context Policy cannot auto-load mutable Marketplace content")
    if document.get("marketplace_path") is not None:
        raise ValueError("Context Policy marketplace_path must be null for reproducible runs")
    raw_disabled = document.get("disabled_skills", [])
    if not isinstance(raw_disabled, list) or any(
        not isinstance(item, str) or not item.strip() or len(item) > 200
        for item in cast(list[object], raw_disabled)
    ):
        raise ValueError("Context Policy disabled_skills must be a bounded string list")
    disabled = sorted({str(item).strip() for item in cast(list[object], raw_disabled)})
    return name, context_policy_config(
        description=description,
        system_message_suffix=_bounded_text(
            document.get("system_message_suffix"), field="system_message_suffix"
        ),
        user_message_suffix=_bounded_text(
            document.get("user_message_suffix"), field="user_message_suffix"
        ),
        disabled_skills=disabled,
    )


def normalize_memory_policy_document(
    value: object, *, fallback_key: str
) -> tuple[str, dict[str, Any]]:
    """Normalize Memory governance without storing Memory text or secrets."""

    document = _document(
        value,
        label="Memory Policy",
        fields={
            "name",
            "description",
            "enabled",
            "scopes",
            "source_refs",
            "retention_days",
            "require_review",
            "sensitive_data_scan",
            "replay_mode",
        },
    )
    name, description = _identity(document, fallback_key, "Memory Policy")
    enabled = document.get("enabled", False)
    if not isinstance(enabled, bool):
        raise ValueError("Memory Policy enabled must be boolean")
    raw_scopes = document.get("scopes", [])
    if not isinstance(raw_scopes, list):
        raise ValueError("Memory Policy scopes must be a list")
    scopes = sorted({str(item) for item in cast(list[object], raw_scopes)})
    if any(scope not in _MEMORY_SCOPES for scope in scopes):
        raise ValueError("Memory Policy contains an unsupported scope")
    raw_sources = document.get("source_refs", [])
    if not isinstance(raw_sources, list):
        raise ValueError("Memory Policy source_refs must be a list")
    sources: list[dict[str, str]] = []
    for raw_source in cast(list[object], raw_sources):
        if not isinstance(raw_source, dict):
            raise ValueError("Memory Policy source_refs must contain objects")
        source = cast(dict[object, object], raw_source)
        if set(map(str, source)) != {"reference_id", "digest"}:
            raise ValueError("Memory source must contain only reference_id and digest")
        reference_id = _optional_uuid(source.get("reference_id"), field="reference_id")
        digest = source.get("digest")
        if reference_id is None or not isinstance(digest, str) or _DIGEST.fullmatch(digest) is None:
            raise ValueError("Memory source must have a fixed UUID and sha256 digest")
        sources.append({"reference_id": reference_id, "digest": digest})
    sources.sort(key=lambda item: (item["reference_id"], item["digest"]))
    if enabled and (not scopes or not sources):
        raise ValueError("Enabled Memory Policy requires scopes and fixed source_refs")
    if not enabled and (scopes or sources):
        raise ValueError("Disabled Memory Policy cannot declare scopes or source_refs")
    retention = document.get("retention_days")
    if retention is not None and (
        not isinstance(retention, int) or isinstance(retention, bool) or not 1 <= retention <= 3650
    ):
        raise ValueError("Memory Policy retention_days must be between 1 and 3650")
    require_review = document.get("require_review", True)
    sensitive_scan = document.get("sensitive_data_scan", True)
    if not isinstance(require_review, bool) or not isinstance(sensitive_scan, bool):
        raise ValueError("Memory Policy review and scan controls must be boolean")
    if enabled and (not require_review or not sensitive_scan):
        raise ValueError("Enabled Memory Policy must require review and sensitive-data scanning")
    if document.get("replay_mode", "FROZEN") != "FROZEN":
        raise ValueError("Memory Policy replay_mode must be FROZEN")
    return name, {
        "description": description,
        "enabled": enabled,
        "scopes": scopes,
        "source_refs": sources,
        "retention_days": retention,
        "require_review": require_review,
        "sensitive_data_scan": sensitive_scan,
        "replay_mode": "FROZEN",
    }


def normalize_critic_policy_document(
    value: object, *, fallback_key: str
) -> tuple[str, dict[str, Any]]:
    document = _document(
        value,
        label="Critic Policy",
        fields={
            "name",
            "description",
            "enabled",
            "mode",
            "threshold",
            "max_refinement_iterations",
        },
    )
    name, description = _identity(document, fallback_key, "Critic Policy")
    enabled = document.get("enabled", False)
    if not isinstance(enabled, bool):
        raise ValueError("Critic Policy enabled must be boolean")
    mode = str(document.get("mode") or "FINISH_AND_MESSAGE")
    if mode not in _CRITIC_MODES:
        raise ValueError("Critic Policy mode is invalid")
    threshold = document.get("threshold", 0.6)
    if (
        not isinstance(threshold, int | float)
        or isinstance(threshold, bool)
        or not 0 <= float(threshold) <= 1
    ):
        raise ValueError("Critic Policy threshold must be between 0 and 1")
    iterations = document.get("max_refinement_iterations", 0)
    if not isinstance(iterations, int) or isinstance(iterations, bool) or not 0 <= iterations <= 2:
        raise ValueError("Critic Policy max_refinement_iterations must be between 0 and 2")
    if not enabled and iterations != 0:
        raise ValueError("Disabled Critic Policy cannot refine")
    if enabled and iterations < 1:
        raise ValueError("Enabled Critic Policy requires at least one refinement iteration")
    return name, {
        "description": description,
        "enabled": enabled,
        "mode": mode,
        "threshold": float(threshold),
        "max_refinement_iterations": iterations,
    }


def normalize_agent_profile_document(
    value: object, *, fallback_key: str
) -> tuple[str, dict[str, Any]]:
    """Normalize an imported Profile into immutable Capability references.

    Runtime never resolves a mutable OpenHands Profile Store identifier. The
    profile is a governed template and every referenced policy is a Version UUID.
    """

    document = _document(
        value,
        label="Agent Profile",
        fields={
            "name",
            "description",
            "schema_version",
            "source_profile_id",
            "source_revision",
            "agent_kind",
            "model",
            "llm_profile_ref",
            "agent",
            "mcp_server_refs",
            "tools",
            "system_message_suffix",
            "disabled_skills",
            "condenser",
            "verification",
            "enable_sub_agents",
            "enable_switch_llm_tool",
            "tool_concurrency_limit",
            "compatibility_matrix",
            "tool_policy_version_id",
            "context_policy_version_id",
            "memory_policy_version_id",
            "critic_policy_version_id",
            "confirmation_policy",
            "max_iterations",
        },
    )
    name, description = _identity(document, fallback_key, "Agent Profile")
    schema_version = document.get("schema_version", OPENHANDS_AGENT_PROFILE_SCHEMA_VERSION)
    if schema_version != OPENHANDS_AGENT_PROFILE_SCHEMA_VERSION:
        raise ValueError("Agent Profile schema_version must match OpenHands schema v2")
    source_profile_id = _optional_uuid(document.get("source_profile_id"), field="source_profile_id")
    source_revision = document.get("source_revision", 0)
    if (
        not isinstance(source_revision, int)
        or isinstance(source_revision, bool)
        or source_revision < 0
    ):
        raise ValueError("Agent Profile source_revision must be a non-negative integer")
    if str(document.get("agent_kind") or "OPENHANDS") != "OPENHANDS":
        raise ValueError("Agent Profile agent_kind must be OPENHANDS")
    model = str(document.get("model") or "inherit")
    llm_profile_ref = str(document.get("llm_profile_ref") or "inherit")
    if model != "inherit" or llm_profile_ref != "inherit":
        raise ValueError("Agent Profile model must inherit the governed node model")
    if str(document.get("agent") or "CodeActAgent") != "CodeActAgent":
        raise ValueError("Agent Profile agent must be CodeActAgent")

    raw_mcp_refs = document.get("mcp_server_refs", None)
    if raw_mcp_refs is not None and (
        not isinstance(raw_mcp_refs, list)
        or any(
            not isinstance(item, str) or not item.strip() or len(item) > 200
            for item in cast(list[object], raw_mcp_refs)
        )
    ):
        raise ValueError("Agent Profile mcp_server_refs must be null or a bounded string list")
    mcp_server_refs = (
        None
        if raw_mcp_refs is None
        else sorted({str(item).strip() for item in cast(list[object], raw_mcp_refs)})
    )
    raw_tools = document.get("tools", None)
    tools: list[dict[str, Any]] | None = None
    if raw_tools is not None:
        if not isinstance(raw_tools, list):
            raise ValueError("Agent Profile tools must be null or a list")
        tools = []
        seen_tools: set[str] = set()
        for raw_tool in cast(list[object], raw_tools):
            if not isinstance(raw_tool, dict):
                raise ValueError("Agent Profile tools must contain objects")
            tool = cast(dict[object, object], raw_tool)
            if set(map(str, tool)) - {"name", "params"}:
                raise ValueError("Agent Profile Tool contains unsupported fields")
            tool_name = str(tool.get("name") or "").strip()
            params = tool.get("params", {})
            if not tool_name or len(tool_name) > 200 or tool_name in seen_tools:
                raise ValueError("Agent Profile Tool names must be unique and bounded")
            if not isinstance(params, dict):
                raise ValueError("Agent Profile Tool params must be an object")
            seen_tools.add(tool_name)
            tools.append({"name": tool_name, "params": cast(dict[str, Any], params)})
    raw_disabled = document.get("disabled_skills", [])
    if not isinstance(raw_disabled, list) or any(
        not isinstance(item, str) or not item.strip() or len(item) > 200
        for item in cast(list[object], raw_disabled)
    ):
        raise ValueError("Agent Profile disabled_skills must be a bounded string list")
    disabled_skills = sorted({str(item).strip() for item in cast(list[object], raw_disabled)})
    system_message_suffix = _bounded_text(
        document.get("system_message_suffix"), field="system_message_suffix"
    )
    condenser = document.get("condenser")
    if condenser is not None and not isinstance(condenser, dict):
        raise ValueError("Agent Profile condenser must be an object")
    condenser_document = (
        cast(dict[object, object], condenser) if isinstance(condenser, dict) else None
    )
    if condenser_document is not None and set(map(str, condenser_document)) - {
        "enabled",
        "max_size",
        "condenser_kind",
        "max_tokens",
        "keep_first",
        "minimum_progress",
        "hard_context_reset_max_retries",
        "hard_context_reset_context_scaling",
    }:
        raise ValueError("Agent Profile condenser contains unsupported fields")
    verification = document.get("verification")
    if verification is not None and not isinstance(verification, dict):
        raise ValueError("Agent Profile verification must be an object")
    verification_document = (
        cast(dict[object, object], verification) if isinstance(verification, dict) else None
    )
    if verification_document is not None and set(map(str, verification_document)) - {
        "critic_enabled",
        "critic_mode",
        "enable_iterative_refinement",
        "critic_threshold",
        "max_refinement_iterations",
        "critic_server_url",
        "critic_model_name",
    }:
        raise ValueError("Agent Profile verification contains unsupported fields")
    enable_sub_agents = document.get("enable_sub_agents", False)
    enable_switch_llm_tool = document.get("enable_switch_llm_tool", False)
    if not isinstance(enable_sub_agents, bool) or not isinstance(enable_switch_llm_tool, bool):
        raise ValueError("Agent Profile enable flags must be boolean")
    if enable_switch_llm_tool:
        raise ValueError(
            "Agent Profile enable_switch_llm_tool must be false because "
            "mutable LLM stores are disabled"
        )
    tool_concurrency_limit = document.get("tool_concurrency_limit", 1)
    if (
        not isinstance(tool_concurrency_limit, int)
        or isinstance(tool_concurrency_limit, bool)
        or not 1 <= tool_concurrency_limit <= 64
    ):
        raise ValueError("Agent Profile tool_concurrency_limit must be between 1 and 64")
    policy_references: dict[str, str] = {}
    for field in (
        "tool_policy_version_id",
        "context_policy_version_id",
        "memory_policy_version_id",
        "critic_policy_version_id",
    ):
        version_id = _optional_uuid(document.get(field), field=field)
        if version_id is None:
            raise ValueError(f"Agent Profile must reference {field}")
        policy_references[field] = version_id
    confirmation = str(document.get("confirmation_policy") or "ALWAYS")
    if confirmation not in {"ALWAYS", "NEVER"}:
        raise ValueError("Agent Profile confirmation_policy is invalid")
    iterations = document.get("max_iterations", 100)
    if (
        not isinstance(iterations, int)
        or isinstance(iterations, bool)
        or not 1 <= iterations <= 1000
    ):
        raise ValueError("Agent Profile max_iterations must be between 1 and 1000")
    return name, {
        "description": description,
        "schema_version": OPENHANDS_AGENT_PROFILE_SCHEMA_VERSION,
        "source_profile_id": source_profile_id,
        "source_revision": source_revision,
        "agent_kind": "OPENHANDS",
        "model": "inherit",
        "llm_profile_ref": "inherit",
        "agent": "CodeActAgent",
        "mcp_server_refs": mcp_server_refs,
        "tools": tools,
        "system_message_suffix": system_message_suffix,
        "disabled_skills": disabled_skills,
        "condenser": cast(dict[str, Any] | None, condenser),
        "verification": cast(dict[str, Any] | None, verification),
        "enable_sub_agents": enable_sub_agents,
        "enable_switch_llm_tool": False,
        "tool_concurrency_limit": tool_concurrency_limit,
        "compatibility_matrix": dict(OPENHANDS_AGENT_PROFILE_FIELD_MATRIX),
        **policy_references,
        "confirmation_policy": confirmation,
        "max_iterations": iterations,
    }


def validate_agent_profile_materialization(
    profile: dict[str, Any],
    *,
    tool_policy: dict[str, Any],
    context_policy: dict[str, Any],
    critic_policy: dict[str, Any],
    mcp_server_names: set[str],
    agent_definitions_enabled: bool,
) -> None:
    """Prove that optional upstream Profile fields equal frozen FlowWeave state.

    OpenHands treats several Profile fields as references into mutable stores.
    FlowWeave instead accepts them only as a compatibility declaration and
    verifies that they describe the already-selected immutable policies.
    """

    declared_tools = profile.get("tools")
    frozen_tools = tool_policy.get("tools")
    if declared_tools is not None and declared_tools != frozen_tools:
        raise ValueError("Agent Profile tools must match its frozen Tool Policy")
    if profile.get("tool_concurrency_limit") != tool_policy.get("tool_concurrency_limit"):
        raise ValueError("Agent Profile concurrency must match its frozen Tool Policy")

    declared_mcp = profile.get("mcp_server_refs")
    if declared_mcp is not None and set(cast(list[str], declared_mcp)) != mcp_server_names:
        raise ValueError("Agent Profile MCP references must match frozen MCP bindings")
    if profile.get("system_message_suffix") != context_policy.get("system_message_suffix"):
        raise ValueError("Agent Profile system suffix must match its frozen Context Policy")
    if profile.get("disabled_skills") != context_policy.get("disabled_skills"):
        raise ValueError("Agent Profile disabled Skills must match its frozen Context Policy")
    if bool(profile.get("enable_sub_agents")) != agent_definitions_enabled:
        raise ValueError("Agent Profile sub-agent flag must match frozen Agent Definitions")

    verification = profile.get("verification")
    if verification is not None:
        expected = {
            "critic_enabled": bool(critic_policy.get("enabled")),
            "critic_mode": str(critic_policy.get("mode") or "FINISH_AND_MESSAGE").lower(),
            "enable_iterative_refinement": bool(
                int(critic_policy.get("max_refinement_iterations") or 0) > 0
            ),
            "critic_threshold": float(critic_policy.get("threshold") or 0.6),
            "max_refinement_iterations": max(
                1, int(critic_policy.get("max_refinement_iterations") or 0)
            ),
            "critic_server_url": None,
            "critic_model_name": None,
        }
        declared = {
            **expected,
            **cast(dict[str, Any], verification),
        }
        if declared != expected:
            raise ValueError("Agent Profile verification must match its frozen Critic Policy")
