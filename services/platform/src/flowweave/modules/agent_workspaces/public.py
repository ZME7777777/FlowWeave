"""Stable public facade for the independent Agent Workspace."""

from flowweave.modules.agent_workspaces.application.service import (
    agent_workspace_owner_is_active,
    ensure_default_agent_workspace,
    mark_agent_workspace_runtime_lost,
    process_agent_workspace_runtime,
    recover_default_agent_workspace_runtime_task,
    resolve_agent_workspace_runtime_secret,
    runtime_allocation_for_agent_workspace,
)

__all__ = (
    "agent_workspace_owner_is_active",
    "ensure_default_agent_workspace",
    "mark_agent_workspace_runtime_lost",
    "process_agent_workspace_runtime",
    "recover_default_agent_workspace_runtime_task",
    "resolve_agent_workspace_runtime_secret",
    "runtime_allocation_for_agent_workspace",
)
