"""Fixed OpenHands Runtime facts shared by every Agent session.

These are Runtime capabilities, not configurable FlowWeave policies.  Every
conversation gets this exact tool set and OpenHands' native NeverConfirm mode.
"""

from __future__ import annotations

from typing import Any, cast

OPENHANDS_VERSION = "1.44.0"
OPENHANDS_SOURCE_COMMIT = "9a24f6c8866f353042a57df0514ccc900e3a0691"

FIXED_RUNTIME_TOOL_NAMES: tuple[str, ...] = (
    "file_editor",
    # ``task`` is the executor-backed child Tool returned by TaskToolSet and
    # is not a public factory: resolving it directly makes the SDK inject
    # ``conv_state`` into TaskTool.create(), which is an explicit TypeError.
    # Conversations must request the registered factory instead.
    "task_tool_set",
    "task_tracker",
    "terminal",
    # ``workflow_tool_set`` resolves to the executable tool named
    # ``workflow``. Configuring both names resolves the same executable twice
    # and OpenHands rejects the Agent because tool names must be unique.
    "workflow_tool_set",
)
FIXED_TOOL_CONCURRENCY_LIMIT = 1


def normalize_fixed_tool_entries(value: object) -> list[dict[str, Any]]:
    """Validate optional Agent Definition tools against the fixed Runtime set."""

    if not isinstance(value, list):
        raise ValueError("Agent Definition tools must be a non-empty list")
    names = [str(item).strip() for item in cast(list[object], value)]
    if not names or any(not name for name in names):
        raise ValueError("Agent Definition tool names cannot be blank")
    if len(names) != len(set(names)):
        raise ValueError("Agent Definition tool names must be unique")
    unsupported = sorted(set(names) - set(FIXED_RUNTIME_TOOL_NAMES))
    if unsupported:
        raise ValueError(
            f"Agent Definition tools are not in the fixed Runtime set: {', '.join(unsupported)}"
        )
    return [{"name": name, "params": {}} for name in names]
