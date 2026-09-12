"""FlowRun/node host context for the shared Agent-session core.

This module owns FlowRun-specific authorization and Runtime lookup only.  It
does not create or interpret a Conversation, and it deliberately never exposes
the Runtime endpoint to callers.
"""

from __future__ import annotations

from dataclasses import dataclass
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
    runtime_server_identity,
)
from flowweave.modules.orchestration.application.runtime_freeze import (
    require_flow_run_runtime_writable,
)
from flowweave.modules.sandboxes import public as sandboxes
from flowweave.runtime.manifest import runtime_node
from flowweave.shared.domain.enums import AttemptState
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
_TERMINAL_PERMISSIONS = frozenset({ACCESS_TERMINAL})
_WRITE_PERMISSIONS = frozenset({CREATE_SESSIONS, WRITE_SESSIONS, CONTROL_SESSIONS})


def assert_flow_node_session_writable(
    db: Session, *, flow_run_id: str, attempt_id: str
) -> NodeAttempt:
    """Reject writes after a node Attempt has been cancelled.

    A cancelled Attempt keeps its OpenHands Conversation and workspace for
    audit and inspection.  It must never, however, regain a write path while
    the asynchronous native interrupt is in flight or after it completes.
    """

    attempt = db.get(NodeAttempt, attempt_id)
    node_run = db.get(NodeRun, attempt.node_run_id) if attempt is not None else None
    if attempt is None or node_run is None or node_run.flow_run_id != flow_run_id:
        raise DomainError(
            "NODE_CONVERSATION_CONTEXT_MISMATCH",
            "The selected node Attempt does not belong to this FlowRun",
            409,
            {"flow_run_id": flow_run_id, "node_attempt_id": attempt_id},
        )
    run = db.get(FlowRun, flow_run_id)
    if run is None:
        raise not_found("flow_run", flow_run_id)
    require_flow_run_runtime_writable(db, run)
    if run.state in {"COMPLETED", "CANCELLED"}:
        raise DomainError(
            "FLOW_RUN_TERMINAL",
            "流程已结束，会话仅可查看；工作区文件和终端仍可使用",
            409,
            {"flow_run_id": flow_run_id, "state": run.state},
        )
    if attempt.state == AttemptState.CANCELLED:
        raise DomainError(
            "NODE_ATTEMPT_CANCELLED",
            "该节点执行已取消，会话和工作区仅可查看",
            409,
            {"node_attempt_id": attempt.id, "runtime_phase": attempt.runtime_phase},
        )
    return attempt


def resolve_flow_node_session_host(
    db: Session,
    *,
    flow_run_id: str,
    attempt_id: str,
    require_start_permission: bool,
    ensure_startable_runtime: bool = False,
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
    require_flow_run_runtime_writable(db, run)
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
    workspace = sandboxes.node_attempt_workspace_context(
        db, flow_run_id=run.id, node_attempt_id=attempt.id
    )
    existing_session_needs_runtime = (
        workspace.attempt_owned
        and attempt.conversation_id is not None
        # Terminal Runs stop their physical Runtime to avoid holding compute,
        # while retaining the external OpenHands state for the product's
        # read-only “view node session” action. Re-provision only an existing
        # Conversation here; permission construction below continues to deny
        # every mutation after cancellation or Run completion.
        and not require_start_permission
    )
    # A failed START gate remains an operator-actionable node state.  The
    # Workbench intentionally offers its session entry here so the operator
    # can inspect and amend the node before retrying the gate.  A copied node
    # can arrive in this state before it has ever created a Conversation, so
    # ``conversation_id`` cannot be the condition that provisions its first
    # Attempt-owned Agent Server generation.
    startable_attempt_states = {
        AttemptState.WAITING_START_CONFIRMATION,
        AttemptState.START_BLOCKED,
    }
    should_ensure_runtime = (
        workspace.attempt_owned
        and (
            require_start_permission
            or (ensure_startable_runtime and attempt.state in startable_attempt_states)
            or existing_session_needs_runtime
        )
    ) or (not workspace.attempt_owned and (require_start_permission or ensure_startable_runtime))
    if should_ensure_runtime:
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
        server_identity = runtime_server_identity(
            environment.manifest_json,
            environment_version_id=environment.id,
            allow_legacy_frozen_runtime=existing_session_needs_runtime,
        )
        if workspace.attempt_owned:
            sandboxes.ensure_node_attempt_runtime(
                db,
                flow_run_id=run.id,
                node_attempt_id=attempt.id,
                image=environment.image_digest,
                environment_id=environment.environment_id,
                environment_version_id=environment.id,
                environment_version_no=environment.version_no,
                runtime_server_identity=server_identity,
            )
        else:
            sandboxes.ensure_flow_run_runtime(
                db,
                flow_run_id=run.id,
                image=environment.image_digest,
                environment_id=environment.environment_id,
                environment_version_id=environment.id,
                environment_version_no=environment.version_no,
                runtime_server_identity=server_identity,
            )
    runtime_session_id = sandboxes.active_node_runtime_connection(
        db, flow_run_id=run.id, node_attempt_id=attempt.id
    ).runtime_session_id
    node = runtime_node(
        definition=snapshot.definition_json,
        manifest=snapshot.runtime_manifest_json or {},
        expected_hash=snapshot.runtime_manifest_hash,
        snapshot_id=snapshot.id,
        instance_key=node_run.flow_node_snapshot_key,
        allow_legacy_read_only_snapshot=existing_session_needs_runtime,
    )
    working_directory = str(workspace.host_working_directory)
    runtime_working_directory = str(workspace.runtime_working_directory)
    return FlowNodeSessionHost(
        session=AgentSessionHostContext.create(
            host_kind="FLOW_NODE",
            host_id=run.id,
            conversation_scope_id=attempt.id,
            runtime_session_id=runtime_session_id,
            working_directory=runtime_working_directory,
            runtime_manifest=snapshot.runtime_manifest_json or {},
            model_policy={},
            # A terminal remains a Workspace tool after an Attempt or its
            # FlowRun has reached a terminal state. Conversation mutations
            # must still be fenced separately, but completed work must remain
            # inspectable and manually operable from its persistent workspace.
            permissions=(
                _READ_PERMISSIONS | _TERMINAL_PERMISSIONS | _WRITE_PERMISSIONS
                if require_start_permission
                else _READ_PERMISSIONS | _TERMINAL_PERMISSIONS
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


__all__ = (
    "FlowNodeSessionHost",
    "assert_flow_node_session_writable",
    "resolve_flow_node_session_host",
)
