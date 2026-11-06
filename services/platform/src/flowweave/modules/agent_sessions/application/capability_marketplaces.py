"""Locate governed native marketplaces inherited by existing conversation forks."""

from pathlib import Path
from uuid import UUID

from flowweave.runtime.base import RuntimeHandle
from flowweave.runtime.dependencies import get_runtime
from flowweave.runtime.workspace import agent_workspace_capability_marketplace_name
from flowweave.shared.errors import DomainError


def registered_marketplace_paths(
    handle: RuntimeHandle,
    host_root: Path,
    runtime_root: Path,
    marketplace_name: str,
) -> tuple[Path, Path, str]:
    # Forks retain the source's native registration. Read it from OpenHands
    # instead of inventing a marketplace named after the new binding.
    candidates: list[tuple[Path, Path, str]] = []
    for registration in get_runtime().conversation_marketplaces(handle):
        name = registration.get("name")
        if not isinstance(name, str) or not name.startswith("flowweave-"):
            continue
        try:
            owner_id = str(UUID(name.removeprefix("flowweave-")))
        except ValueError:
            continue
        source_root = runtime_root.parent / owner_id
        if (
            name != agent_workspace_capability_marketplace_name(owner_id)
            or registration.get("source") != str(source_root / "marketplace")
            or registration.get("auto_load") is not False
        ):
            continue
        if name == marketplace_name:
            return host_root, runtime_root, name
        candidates.append((host_root.parent / owner_id, source_root, name))
    if len(candidates) == 1:
        return candidates[0]
    raise DomainError(
        "AGENT_CONVERSATION_MARKETPLACE_UNAVAILABLE",
        "当前会话缺少可验证的能力市场注册，无法加载所选能力。",
        409,
    )
