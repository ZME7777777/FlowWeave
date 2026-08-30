from __future__ import annotations

from typing import Any, cast

from flowweave.shared.domain.openhands import normalize_fixed_tool_entries

_FIELDS = frozenset(
    {
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
)


def normalize_agent_definition_document(
    value: object, *, fallback_key: str
) -> tuple[str, dict[str, Any]]:
    """Validate the governed OpenHands 1.44.0 AgentDefinition subset.

    FlowWeave intentionally rejects nested Skill/MCP/Hook/profile configuration
    until those references can be resolved to immutable Capability Versions.
    Definitions use only inherited model credentials and an explicit NoOp
    condenser, so secrets and floating profile paths never enter a Snapshot.
    """

    if not isinstance(value, dict):
        raise ValueError("Agent Definition root must be an object")
    document = cast(dict[object, object], value)
    unknown = sorted(str(key) for key in document if str(key) not in _FIELDS)
    if unknown:
        raise ValueError(f"Agent Definition contains unsupported fields: {', '.join(unknown)}")
    name = str(document.get("name") or fallback_key).strip()
    if not name or len(name) > 200:
        raise ValueError("Agent Definition name is invalid")
    description = str(document.get("description") or "").strip()
    system_prompt = str(document.get("system_prompt") or "").strip()
    if len(description) > 4000:
        raise ValueError("Agent Definition description is too long")
    if not system_prompt or len(system_prompt) > 100_000:
        raise ValueError("Agent Definition system_prompt is required and must be bounded")
    if str(document.get("model") or "inherit") != "inherit":
        raise ValueError("Agent Definition model must inherit the governed parent model")

    raw_tools = document.get("tools")
    if not isinstance(raw_tools, list):
        raise ValueError("Agent Definition tools must be a non-empty list")
    tool_names = [str(item).strip() for item in cast(list[object], raw_tools)]
    if any(not name for name in tool_names):
        raise ValueError("Agent Definition tool names cannot be blank")
    tools = normalize_fixed_tool_entries(tool_names)
    normalized_tools = [str(item["name"]) for item in tools]
    if "task_tool_set" in normalized_tools:
        raise ValueError("Agent Definition cannot recursively enable task_tool_set")
    if document.get("skills", []) != []:
        raise ValueError(
            "Agent Definition skills must be empty until immutable Skill references are governed"
        )
    if document.get("metadata", {}) != {}:
        raise ValueError("Agent Definition metadata must be empty")

    raw_examples_value: object = document.get("when_to_use_examples", [])
    if not isinstance(raw_examples_value, list):
        raise ValueError("Agent Definition when_to_use_examples must contain at most 20 items")
    raw_examples = cast(list[object], raw_examples_value)
    if len(raw_examples) > 20:
        raise ValueError("Agent Definition when_to_use_examples must contain at most 20 items")
    examples = [str(item).strip() for item in raw_examples]
    if any(not item or len(item) > 1000 for item in examples):
        raise ValueError("Agent Definition when_to_use_examples are invalid")

    permission_mode = document.get("permission_mode")
    if permission_mode != "never_confirm":
        raise ValueError(
            "Agent Definition permission_mode must be never_confirm: OpenHands 1.44.0 "
            "TaskToolSet has no Agent Server confirmation handler and would otherwise "
            "auto-approve sub-agent actions"
        )
    iterations = document.get("max_iteration_per_run")
    if iterations is not None and (
        not isinstance(iterations, int)
        or isinstance(iterations, bool)
        or not 1 <= iterations <= 1000
    ):
        raise ValueError("Agent Definition max_iteration_per_run must be between 1 and 1000")
    budget = document.get("max_budget_per_run")
    if budget is not None and (
        not isinstance(budget, int | float)
        or isinstance(budget, bool)
        or not 0 < float(budget) <= 1_000_000
    ):
        raise ValueError("Agent Definition max_budget_per_run is invalid")
    condenser = document.get("condenser", {"kind": "NoOpCondenser"})
    if condenser != {"kind": "NoOpCondenser"}:
        raise ValueError(
            "Agent Definition condenser must be explicit NoOpCondenser until "
            "nested model references are governed"
        )

    return name, {
        "name": name,
        "description": description,
        "model": "inherit",
        "tools": normalized_tools,
        "skills": [],
        "system_prompt": system_prompt,
        "when_to_use_examples": examples,
        "permission_mode": permission_mode,
        "max_iteration_per_run": iterations,
        "max_budget_per_run": float(budget) if budget is not None else None,
        "condenser": {"kind": "NoOpCondenser"},
        "metadata": {},
    }
