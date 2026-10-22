"""User-directory Git identity; the Runtime configuration is the fact source."""

from __future__ import annotations

from sqlalchemy.orm import Session

from flowweave.modules.agent_sessions import public as agent_sessions
from flowweave.modules.agent_workspaces.application import workspace
from flowweave.modules.users.application.security import user_runtime_project_root
from flowweave.runtime.base import RuntimeHandle
from flowweave.runtime.dependencies import get_runtime


def git_identity(
    db: Session, workspace_id: str, *, identity: tuple[str, str] | None = None
) -> dict[str, str | bool]:
    workspace.ensure_user_project_root(db, workspace_id)
    resource_name, resource_id = agent_sessions.conversations.terminal_resource_details(
        db, workspace_id
    )
    handle = RuntimeHandle(
        job_id=f"env-exec:{resource_name}",
        conversation_id="",
        runtime_resource_id=resource_id,
        runtime_resource_name=resource_name,
        workspace_root=user_runtime_project_root(workspace_id),
    )
    return get_runtime().workspace_git_identity(handle, identity=identity)
