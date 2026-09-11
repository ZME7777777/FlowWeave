"""Governed OpenHands Runtime image capability selections.

The OpenHands Docker builder accepts a free-form build argument.  FlowWeave
never exposes that argument: it freezes this small product allowlist onto an
Environment Version before image construction.
"""

from __future__ import annotations

from collections.abc import Iterable

RUNTIME_CAPABILITY_ORDER = ("vscode", "browser", "docker")
RUNTIME_CAPABILITIES = frozenset(RUNTIME_CAPABILITY_ORDER)


def normalize_runtime_capabilities(capabilities: Iterable[str]) -> tuple[str, ...]:
    """Validate and canonically order the governed capability set."""

    selected = tuple(capabilities)
    unknown = sorted(set(selected) - RUNTIME_CAPABILITIES)
    if unknown:
        raise ValueError(f"unknown Runtime capability: {', '.join(unknown)}")
    if len(selected) != len(set(selected)):
        raise ValueError("Runtime capabilities must not contain duplicates")
    return tuple(item for item in RUNTIME_CAPABILITY_ORDER if item in selected)


def openhands_install_capabilities(capabilities: Iterable[str]) -> str:
    """Return the exact value passed to OpenHands' formal build API."""

    return ",".join(normalize_runtime_capabilities(capabilities))


def runtime_capability_profile(capabilities: Iterable[str]) -> str:
    """Name the immutable product profile without leaking a Docker arg."""

    selected = normalize_runtime_capabilities(capabilities)
    return "minimal" if not selected else "+".join(selected)
