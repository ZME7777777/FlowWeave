"""User-directory Git identity; the Runtime configuration is the fact source."""

from __future__ import annotations

import logging

from sqlalchemy.orm import Session

from flowweave.modules.users.application.git_identity import profile_identity
from flowweave.modules.users.application.security import (
    user_runtime_project_root,
)
from flowweave.runtime.base import RuntimeHandle
from flowweave.runtime.dependencies import get_runtime
from flowweave.shared.errors import DomainError

_logger = logging.getLogger(__name__)


def apply_profile_identity(db: Session, workspace_id: str, handle: RuntimeHandle) -> None:
    """Best-effort profile synchronization for ordinary Agent work."""

    profile = profile_identity(db)
    if profile is None:
        return
    from flowweave.modules.agent_workspaces.application.workspace import ensure_user_project_root

    ensure_user_project_root(db, workspace_id)
    try:
        get_runtime().workspace_git_identity(handle, profile=profile)
    except DomainError as exc:
        if exc.code != "AGENT_GIT_CONFIG_UNAVAILABLE":
            raise
        _logger.warning(
            "agent_workspace_git_profile_sync_unavailable workspace=%s runtime=%s",
            workspace_id,
            handle.runtime_resource_name,
        )


def _workspace_handle(db: Session, workspace_id: str) -> RuntimeHandle:
    # These modules also depend on the session service; resolve them only at
    # the authorized application boundary rather than creating an import cycle.
    from flowweave.modules.agent_sessions.application.conversations import terminal_resource_details
    from flowweave.modules.agent_workspaces.application.workspace import ensure_user_project_root

    ensure_user_project_root(db, workspace_id)
    resource_name, resource_id = terminal_resource_details(db, workspace_id)
    return RuntimeHandle(
        job_id=f"env-exec:{resource_name}",
        conversation_id="",
        runtime_resource_id=resource_id,
        runtime_resource_name=resource_name,
        workspace_root=user_runtime_project_root(workspace_id),
    )


def git_identity(db: Session, workspace_id: str) -> dict[str, str | bool]:
    """Read the effective identity without changing user-directory configuration."""

    return get_runtime().workspace_git_identity(_workspace_handle(db, workspace_id))


def synchronize_profile_identity(db: Session, workspace_id: str) -> dict[str, str | bool]:
    """Synchronize the current user's profile only when the directory lacks one."""

    handle = _workspace_handle(db, workspace_id)
    current = get_runtime().workspace_git_identity(handle)
    if current.get("configured"):
        return current
    profile = profile_identity(db)
    if profile is None:
        raise DomainError("AGENT_GIT_PROFILE_UNAVAILABLE", "当前用户未配置有效的姓名和邮箱", 409)
    return get_runtime().workspace_git_identity(handle, profile=profile)
