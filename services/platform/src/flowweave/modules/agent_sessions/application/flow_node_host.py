"""FlowRun/node host context for the shared Agent-session core.

This module owns FlowRun-specific authorization and Runtime lookup only.  It
does not create or interpret a Conversation, and it deliberately never exposes
the Runtime endpoint to callers.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import PurePosixPath
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
from flowweave.modules.environments.public import (
    lock_referenceable_version,
    validate_runtime_manifest,
)
from flowweave.modules.sandboxes import public as sandboxes
from flowweave.runtime.manifest import runtime_node
from flowweave.shared.errors import DomainError, not_found
from flowweave.shared.models import FlowRun, NodeAttempt, NodeRun, RunSnapshot


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


def _runtime_working_directory(*, workspace_ref: str) -> str:
    """Map a node session to its Attempt-owned project mount.

    ``workspace_ref`` is historical lineage only.  The Attempt Runtime mounts
    its own persistent project root at this stable OpenHands path, so it must
    never be interpreted against another FlowRun's host allocation.
    """

    if not workspace_ref.strip():
        raise DomainError(
            "NODE_WORKSPACE_REQUIRED",
            "The selected node Attempt has no isolated workspace",
            409,
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
    if require_start_permission:
        if not run.environment_version_id:
            raise DomainError(
                "RUN_ENVIRONMENT_REQUIRED",
                "The FlowRun has no Environment Version",
                409,
            )
        environment = lock_referenceable_version(db, run.environment_version_id)
        if environment is None:
            raise DomainError(
                "RUN_ENVIRONMENT_VERSION_INVALID",
                "The frozen FlowRun Environment Version is unavailable",
                409,
            )
        validate_runtime_manifest(environment.manifest_json, environment_version_id=environment.id)
        sandboxes.allocate_node_attempt_runtime(db, flow_run_id=run.id, node_attempt_id=attempt.id)
        sandboxes.ensure_node_attempt_runtime(
            db,
            flow_run_id=run.id,
            node_attempt_id=attempt.id,
            image=environment.image_digest,
            environment_id=environment.environment_id,
            environment_version_id=environment.id,
            environment_version_no=environment.version_no,
        )
    runtime_session_id = sandboxes.active_node_attempt_runtime_connection(
        db, flow_run_id=run.id, node_attempt_id=attempt.id
    ).runtime_session_id
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
    runtime_working_directory = _runtime_working_directory(workspace_ref=working_directory)
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
