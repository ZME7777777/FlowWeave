"""FlowRun/node host context for the shared Agent-session core.

This module owns FlowRun-specific authorization and Runtime lookup only.  It
does not create or interpret a Conversation, and it deliberately never exposes
the Runtime endpoint to callers.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any

from sqlalchemy.orm import Session

from flowweave.modules.agent_sessions.application.host import (
    ACCESS_FILES,
    ACCESS_TERMINAL,
    CONTROL_SESSIONS,
    CREATE_SESSIONS,
    LIST_SESSIONS,
    READ_SESSIONS,
    WRITE_SESSIONS,
    AgentSessionHostContext,
)
from flowweave.modules.sandboxes import public as sandboxes
from flowweave.runtime.manifest import runtime_node
from flowweave.shared.errors import DomainError, not_found
from flowweave.shared.models import FlowRun, NodeAttempt, NodeRun, RunSnapshot
from flowweave.shared.settings import get_settings


@dataclass(frozen=True, slots=True)
class FlowNodeSessionHost:
    """Server-verified context for sessions owned by one FlowRun node Attempt."""

    session: AgentSessionHostContext
    flow_run_id: str
    node_run_id: str
    attempt_id: str
    snapshot_id: str
    runtime_session_id: str
    working_directory: str
    node: dict[str, Any]
    startup_prompt: str | None


_READ_PERMISSIONS = frozenset({LIST_SESSIONS, READ_SESSIONS, ACCESS_FILES})
_WRITE_PERMISSIONS = frozenset({CREATE_SESSIONS, WRITE_SESSIONS, ACCESS_TERMINAL, CONTROL_SESSIONS})
_RUNTIME_PROJECT = PurePosixPath("/runtime/workspace/project")


def _runtime_working_directory(*, flow_run_id: str, workspace_ref: str) -> str:
    """Authorize an Attempt path while using the shared Runtime project root.

    ``NodeAttempt.workspace_ref`` is an absolute host path under the FlowRun
    allocation.  It remains the server-side provenance and materialization
    path, but it is not an OpenHands working directory.  Every interactive
    node session, terminal, and workspace view uses the one project mount so
    agents can collaborate on the complete FlowRun project.
    """

    raw = workspace_ref.strip()
    runtime_path = PurePosixPath(raw)
    if (
        runtime_path.is_absolute()
        and runtime_path.is_relative_to(_RUNTIME_PROJECT)
        and runtime_path.as_posix() == raw
    ):
        return str(_RUNTIME_PROJECT)
    # Attempt data is deliberately a sibling of the shared project mount:
    # ``workspace/nodes/...`` maps to ``/runtime/workspace/nodes/...``.  It
    # remains an authorization/provenance path only; the returned Agent cwd
    # below is still the shared project root.
    nodes_root = sandboxes.flow_run_workspace_nodes_path(flow_run_id)
    try:
        relative = Path(raw).relative_to(nodes_root)
    except ValueError as exc:
        raise DomainError(
            "NODE_WORKSPACE_INVALID",
            "The selected node Attempt workspace is outside its FlowRun allocation",
            409,
            {"flow_run_id": flow_run_id},
        ) from exc
    if not relative.parts or any(part in {"", ".", ".."} for part in relative.parts):
        raise DomainError(
            "NODE_WORKSPACE_INVALID",
            "The selected node Attempt workspace layout is invalid",
            409,
            {"flow_run_id": flow_run_id},
        )
    return str(_RUNTIME_PROJECT)


def resolve_flow_node_session_host(
    db: Session,
    *,
    flow_run_id: str,
    attempt_id: str,
    require_start_permission: bool,
) -> FlowNodeSessionHost:
    """Resolve one node host without treating a FlowRun as an Agent Workspace.

    An existing session remains readable after an Attempt moves forward, so
    only new-session entry passes ``require_start_permission=True``. Every
    path still resolves the active FlowRun Runtime session afresh, which keeps
    generation replacement fenced at the existing Runtime boundary.
    """

    run = db.get(FlowRun, flow_run_id)
    if run is None:
        raise not_found("flow_run", flow_run_id)
    runtime_owner_id = sandboxes.runtime_owner_flow_run_id(db, flow_run_id)
    runtime_overview = sandboxes.runtime_overview(db, flow_run_id)
    if runtime_overview["rerun_required"]:
        raise DomainError(
            "LEGACY_RUNTIME_INCOMPATIBLE",
            "Historical FlowRun Runtime data is incompatible; rerun the Flow",
            409,
            {"flow_run_id": flow_run_id},
        )
    attempt = db.get(NodeAttempt, attempt_id)
    if attempt is None:
        raise DomainError(
            "NODE_CONVERSATION_CONTEXT_REQUIRED",
            "Select and start a FlowRun node before creating a Conversation",
            422,
            {"flow_run_id": flow_run_id, "node_attempt_id": attempt_id},
        )
    node_run = db.get(NodeRun, attempt.node_run_id)
    if node_run is None or node_run.flow_run_id != run.id:
        raise DomainError(
            "NODE_CONVERSATION_CONTEXT_MISMATCH",
            "The selected node Attempt does not belong to this FlowRun",
            409,
            {"flow_run_id": flow_run_id, "node_attempt_id": attempt_id},
        )
    snapshot = db.get(RunSnapshot, attempt.snapshot_id)
    if snapshot is None:
        raise DomainError("SNAPSHOT_INVALID", "Attempt Snapshot is unavailable", 409)
    if require_start_permission and attempt.state != "WAITING_START_CONFIRMATION":
        raise DomainError(
            "NODE_CONVERSATION_NOT_READY",
            "The selected node is not ready to start a Conversation",
            409,
            {"node_attempt_id": attempt.id, "state": attempt.state},
        )
    try:
        runtime_session_id = sandboxes.active_flow_run_runtime_connection(
            db, flow_run_id=run.id
        ).runtime_session_id
    except DomainError as exc:
        if exc.code != "RUNTIME_SESSION_NOT_ACTIVE" or get_settings().runtime_adapter != "mock":
            raise
        runtime_session_id = str(runtime_overview.get("runtime_session_id") or "")
        if not runtime_session_id:
            raise
    node = runtime_node(
        definition=snapshot.definition_json,
        manifest=snapshot.runtime_manifest_json or {},
        expected_hash=snapshot.runtime_manifest_hash,
        snapshot_id=snapshot.id,
        instance_key=node_run.flow_node_snapshot_key,
    )
    working_directory = (attempt.workspace_ref or "").strip()
    if not working_directory:
        raise DomainError(
            "NODE_WORKSPACE_REQUIRED",
            "The selected node Attempt has no isolated workspace",
            409,
            {"node_attempt_id": attempt.id},
        )
    runtime_working_directory = _runtime_working_directory(
        flow_run_id=runtime_owner_id, workspace_ref=working_directory
    )
    return FlowNodeSessionHost(
        session=AgentSessionHostContext.create(
            host_kind="FLOW_NODE",
            host_id=run.id,
            conversation_scope_id=attempt.id,
            runtime_session_id=runtime_session_id,
            working_directory=runtime_working_directory,
            runtime_manifest=snapshot.runtime_manifest_json or {},
            model_policy={},
            permissions=(
                _READ_PERMISSIONS | _WRITE_PERMISSIONS
                if require_start_permission
                else _READ_PERMISSIONS
            ),
        ),
        flow_run_id=run.id,
        node_run_id=node_run.id,
        attempt_id=attempt.id,
        snapshot_id=snapshot.id,
        runtime_session_id=runtime_session_id,
        working_directory=working_directory,
        node=node,
        startup_prompt=attempt.startup_prompt,
    )


__all__ = ("FlowNodeSessionHost", "resolve_flow_node_session_host")
