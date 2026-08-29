"""Stable public facade for the independent Agent Workspace."""

from sqlalchemy.orm import Session

from flowweave.modules.agent_sessions import public as agent_sessions
from flowweave.modules.agent_workspaces.application.service import (
    agent_workspace_owner_is_active,
    ensure_default_agent_workspace,
    mark_agent_workspace_runtime_lost,
    process_agent_workspace_runtime,
    recover_default_agent_workspace_runtime_task,
    resolve_agent_workspace_runtime_secret,
    runtime_allocation_for_agent_workspace,
)
from flowweave.modules.agent_workspaces.infrastructure.models import (
    AgentWorkDirectoryVersion,
    AgentWorkspace,
    AgentWorkspaceCapability,
    AgentWorkspaceRuntime,
)

process_agent_conversation_title = agent_sessions.process_agent_conversation_title


def conversation_work_directory_context(
    db: Session, workspace_id: str, work_directory_id: str | None
) -> tuple[str | None, str]:
    """Resolve a new conversation's Workspace-owned directory selection."""

    # Import lazily: the Workspace file service consumes the shared session
    # facade, while the shared session core calls this host adapter.
    from flowweave.modules.agent_workspaces.application import work_directories

    return work_directories.conversation_context(db, workspace_id, work_directory_id)


def frozen_conversation_work_directory_context(
    db: Session, workspace_id: str, version_id: str
) -> str:
    """Revalidate a Workspace directory version frozen on a session locator."""

    from flowweave.modules.agent_workspaces.application import work_directories

    return work_directories.frozen_conversation_context(db, workspace_id, version_id)


def delete_session_attachment_files(db: Session, workspace_id: str, binding_id: str) -> None:
    """Remove only attachment files owned by one shared session locator."""

    from flowweave.modules.agent_workspaces.application import workspace

    workspace.delete_bound_attachment_files(db, workspace_id, binding_id)

__all__ = (
    "agent_workspace_owner_is_active",
    "AgentWorkDirectoryVersion",
    "AgentWorkspace",
    "AgentWorkspaceCapability",
    "AgentWorkspaceRuntime",
    "conversation_work_directory_context",
    "delete_session_attachment_files",
    "ensure_default_agent_workspace",
    "frozen_conversation_work_directory_context",
    "mark_agent_workspace_runtime_lost",
    "process_agent_workspace_runtime",
    "process_agent_conversation_title",
    "recover_default_agent_workspace_runtime_task",
    "resolve_agent_workspace_runtime_secret",
    "runtime_allocation_for_agent_workspace",
)
