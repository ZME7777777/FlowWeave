"""Default Agent Workspace adapter for the shared Agent-session host contract."""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session

from flowweave.modules.agent_sessions.public import (
    ACCESS_FILES,
    ACCESS_TERMINAL,
    CONTROL_SESSIONS,
    CREATE_SESSIONS,
    LIST_SESSIONS,
    READ_SESSIONS,
    WRITE_SESSIONS,
    AgentSessionHostContext,
)
from flowweave.modules.agent_workspaces.infrastructure.models import (
    AgentWorkspace,
    AgentWorkspaceRuntime,
)
from flowweave.shared.errors import DomainError, not_found

_PROJECT_ROOT = "/runtime/workspace/project"
_READ_PERMISSIONS = frozenset({LIST_SESSIONS, READ_SESSIONS, ACCESS_FILES})
_WRITE_PERMISSIONS = frozenset(
    {CREATE_SESSIONS, WRITE_SESSIONS, ACCESS_TERMINAL, CONTROL_SESSIONS}
)


def resolve_agent_workspace_session_host(
    db: Session, workspace_id: str, *, require_write: bool = False
) -> AgentSessionHostContext:
    """Resolve the default Workspace's server-owned session host facts.

    This adapter intentionally contains the only Agent Workspace ORM lookup.
    The shared session core receives its result and therefore has no reason to
    learn about this host's table names, routes or Runtime owner type.
    """

    workspace = db.get(AgentWorkspace, workspace_id)
    if workspace is None:
        raise not_found("agent_workspace", workspace_id)
    runtime = db.scalar(
        select(AgentWorkspaceRuntime).where(
            AgentWorkspaceRuntime.workspace_id == workspace.id
        )
    )
    if runtime is None:
        raise DomainError(
            "AGENT_RUNTIME_NOT_READY",
            "Agent 运行环境正在初始化",
            503,
            {"agent_workspace_id": workspace.id},
        )
    writable = runtime.status == "ACTIVE" and workspace.desired_state == "RUNNING"
    if require_write and not writable:
        raise DomainError(
            "AGENT_RUNTIME_RECOVERING",
            "Agent 运行环境正在恢复，数据已保留",
            503,
            {"agent_workspace_id": workspace.id},
        )
    return AgentSessionHostContext.create(
        host_kind="AGENT_WORKSPACE",
        host_id=workspace.id,
        conversation_scope_id=workspace.id,
        runtime_session_id=runtime.id,
        working_directory=_PROJECT_ROOT,
        model_policy={"default_model_provider_id": workspace.default_model_provider_id},
        permissions=_READ_PERMISSIONS | (_WRITE_PERMISSIONS if writable else frozenset()),
    )


__all__ = ("resolve_agent_workspace_session_host",)
