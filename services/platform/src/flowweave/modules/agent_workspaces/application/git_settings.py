"""User-directory Git identity; the Runtime configuration is the fact source."""

from __future__ import annotations

import re

from sqlalchemy.orm import Session

from flowweave.modules.users.application.security import (
    FLOWWEAVE_USER_ID,
    current_user_id,
    user_runtime_project_root,
)
from flowweave.modules.users.infrastructure.models import User
from flowweave.runtime.base import RuntimeHandle
from flowweave.runtime.dependencies import get_runtime


def profile_identity(db: Session) -> tuple[str, str] | None:
    user = db.get(User, current_user_id(default=FLOWWEAVE_USER_ID))
    if user is None or not isinstance(user.display_name, str) or not isinstance(user.email, str):
        return None
    name, email = user.display_name.strip(), user.email.strip()
    if not name or len(name) > 200 or re.search(r"[\x00-\x1f\x7f<>]", name):
        return None
    if len(email) > 320 or not re.fullmatch(
        r"[^\x00-\x1f\x7f\s@<>]+@[^\x00-\x1f\x7f\s@<>]+", email
    ):
        return None
    return name, email


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
    profile = profile_identity(db) if identity is None else None
    return get_runtime().workspace_git_identity(handle, identity=identity, profile=profile)
