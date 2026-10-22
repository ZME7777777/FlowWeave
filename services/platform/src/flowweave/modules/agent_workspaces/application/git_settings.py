"""User-directory Git identity; the Runtime configuration is the fact source."""

from __future__ import annotations

from sqlalchemy.orm import Session

from flowweave.modules.users.application.git_identity import profile_identity
from flowweave.modules.users.application.security import (
    user_runtime_project_root,
)
from flowweave.runtime.base import RuntimeHandle
from flowweave.runtime.dependencies import get_runtime


def apply_profile_identity(db: Session, workspace_id: str, handle: RuntimeHandle) -> None:
    profile = profile_identity(db)
    if profile is None:
        return
    from flowweave.modules.agent_workspaces.application.workspace import ensure_user_project_root

    ensure_user_project_root(db, workspace_id)
    get_runtime().workspace_git_identity(handle, profile=profile)


def git_identity(
    db: Session, workspace_id: str, *, identity: tuple[str, str] | None = None
) -> dict[str, str | bool]:
    # These modules also depend on the session service; resolve them only at
    # the authorized application boundary rather than creating an import cycle.
    from flowweave.modules.agent_sessions.application.conversations import terminal_resource_details
    from flowweave.modules.agent_workspaces.application.workspace import ensure_user_project_root

    ensure_user_project_root(db, workspace_id)
    resource_name, resource_id = terminal_resource_details(db, workspace_id)
    handle = RuntimeHandle(
        job_id=f"env-exec:{resource_name}",
        conversation_id="",
        runtime_resource_id=resource_id,
        runtime_resource_name=resource_name,
        workspace_root=user_runtime_project_root(workspace_id),
    )
    return get_runtime().workspace_git_identity(handle, identity=identity)
