"""Stable public facade for the independent Agent Workspace."""

from typing import Any

from sqlalchemy.orm import Session

from flowweave.modules.agent_sessions import public as agent_sessions
from flowweave.modules.agent_workspaces.application.service import (
    agent_workspace_owner_is_active,
    agent_workspace_record_path,
    ensure_default_agent_workspace,
    mark_agent_workspace_runtime_lost,
    process_agent_workspace_runtime,
    recover_default_agent_workspace_runtime_task,
    resolve_agent_workspace_runtime_secret,
    runtime_allocation_for_agent_workspace,
)
from flowweave.modules.agent_workspaces.infrastructure.models import (
    AgentWorkDirectory,
    AgentWorkDirectoryPath,
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


def list_flow_run_work_directories(
    db: Session, flow_run_id: str, node_attempt_id: str
) -> dict[str, Any]:
    """List logical work directories owned by one FlowRun node Attempt."""

    from flowweave.modules.agent_workspaces.application import work_directories

    return work_directories.list_flow_run_work_directories(db, flow_run_id, node_attempt_id)


def create_flow_run_work_directory(
    db: Session,
    flow_run_id: str,
    node_attempt_id: str,
    display_name: str,
    selected_paths: tuple[str, ...],
) -> dict[str, Any]:
    """Create a logical work directory for one FlowRun node Attempt."""

    from flowweave.modules.agent_workspaces.application import work_directories

    return work_directories.create_flow_run_work_directory(
        db, flow_run_id, node_attempt_id, display_name, selected_paths
    )


def get_flow_run_work_directory(
    db: Session, flow_run_id: str, node_attempt_id: str, work_directory_id: str
) -> dict[str, Any]:
    """Return one logical work directory owned by the current node Attempt."""

    from flowweave.modules.agent_workspaces.application import work_directories

    return work_directories.get_flow_run_work_directory(
        db, flow_run_id, node_attempt_id, work_directory_id
    )


def delete_flow_run_work_directory(
    db: Session, flow_run_id: str, node_attempt_id: str, work_directory_id: str
) -> None:
    """Delete one unreferenced logical directory owned by a node Attempt."""

    from flowweave.modules.agent_workspaces.application import work_directories

    work_directories.delete_flow_run_work_directory(
        db, flow_run_id, node_attempt_id, work_directory_id
    )


def flow_run_conversation_work_directory_context(
    db: Session,
    flow_run_id: str,
    node_attempt_id: str,
    work_directory_id: str | None,
) -> tuple[str | None, str]:
    """Freeze a node Attempt-owned directory selection for a new session."""

    from flowweave.modules.agent_workspaces.application import work_directories

    return work_directories.flow_run_conversation_context(
        db, flow_run_id, node_attempt_id, work_directory_id
    )


def delete_session_attachment_files(db: Session, workspace_id: str, binding_id: str) -> None:
    """Remove only attachment files owned by one shared session locator."""

    from flowweave.modules.agent_workspaces.application import workspace

    workspace.delete_bound_attachment_files(db, workspace_id, binding_id)


__all__ = (
    "agent_workspace_owner_is_active",
    "agent_workspace_record_path",
    "AgentWorkDirectory",
    "AgentWorkDirectoryPath",
    "AgentWorkDirectoryVersion",
    "AgentWorkspace",
    "AgentWorkspaceCapability",
    "AgentWorkspaceRuntime",
    "conversation_work_directory_context",
    "create_flow_run_work_directory",
    "delete_flow_run_work_directory",
    "delete_session_attachment_files",
    "ensure_default_agent_workspace",
    "frozen_conversation_work_directory_context",
    "flow_run_conversation_work_directory_context",
    "get_flow_run_work_directory",
    "list_flow_run_work_directories",
    "mark_agent_workspace_runtime_lost",
    "process_agent_workspace_runtime",
    "process_agent_conversation_title",
    "recover_default_agent_workspace_runtime_task",
    "resolve_agent_workspace_runtime_secret",
    "runtime_allocation_for_agent_workspace",
)
