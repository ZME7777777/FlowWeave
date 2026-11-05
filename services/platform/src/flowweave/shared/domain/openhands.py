"""Fixed OpenHands Runtime facts shared by every Agent session.

These are Runtime capabilities, not configurable FlowWeave policies.  Every
conversation gets this exact tool set and OpenHands' native NeverConfirm mode.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, cast

OPENHANDS_VERSION = "1.58.2"
OPENHANDS_UPSTREAM_BASE_COMMIT = "ce5716a3c0a955a85bb0cc4dcfaa94d5fefcda50"
OPENHANDS_SOURCE_COMMIT = "b2131e0ef31a459486f6bd716e150c7e710c8a78"
OPENHANDS_SOURCE_ARCHIVE_SHA256 = "a7defbd65c78ba5e1fa26b4dd5eca3f7ddc9a546a02fc5a4873f6cb3e3e12f9a"


@dataclass(frozen=True, slots=True)
class OpenHandsServerIdentity:
    """Immutable Agent Server provenance expected from one Runtime image."""

    package_version: str
    source_commit: str
    source_ref: str


CURRENT_OPENHANDS_SERVER_IDENTITY = OpenHandsServerIdentity(
    package_version=OPENHANDS_VERSION,
    source_commit=OPENHANDS_SOURCE_COMMIT,
    source_ref=OPENHANDS_SOURCE_COMMIT,
)

FIXED_RUNTIME_TOOL_NAMES: tuple[str, ...] = (
    "browser_tool_set",
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
