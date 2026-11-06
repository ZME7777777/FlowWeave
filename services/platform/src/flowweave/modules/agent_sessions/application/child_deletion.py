"""Durable native family cleanup with a parent lifecycle barrier and resumable steps."""

from __future__ import annotations

import re
from dataclasses import dataclass, replace
from datetime import timedelta
from pathlib import Path
from typing import Any
from uuid import uuid4

from sqlalchemy import select
from sqlalchemy.orm import Session

from flowweave.modules.agent_sessions.application.child_creation import POLICY_VERSION
from flowweave.modules.agent_sessions.application.child_parent import read_parent_capability
from flowweave.modules.agent_sessions.application.child_relations import (
    require_direct_child,
    require_ordinary_binding,
)
from flowweave.modules.agent_sessions.application.deletion import delete_binding_records
from flowweave.modules.agent_sessions.application.draft_attachments import (
    delete_owned_attachment_files,
)
from flowweave.modules.agent_sessions.application.host import AgentSessionHostContext
from flowweave.modules.agent_sessions.infrastructure.models import (
    AgentChildConversation,
    AgentConversationBinding,
)
from flowweave.modules.agent_workspaces import public as workspace_host
from flowweave.modules.agent_workspaces.infrastructure.models import AgentWorkspaceRuntime
from flowweave.modules.sandboxes import public as sandboxes
from flowweave.modules.tasks.public import Lease, enqueue, lease_is_current
from flowweave.modules.users.application.security import FLOWWEAVE_USER_ID, current_user_id
from flowweave.runtime.base import RuntimeHandle
from flowweave.runtime.dependencies import get_runtime
from flowweave.shared.database import now
from flowweave.shared.errors import DomainError
from flowweave.shared.models import BackgroundTask, FlowRunRuntime, ManagedSandbox, TaskState

TASK_TYPE = "DELETE_AGENT_CONVERSATION_FAMILY"


@dataclass(frozen=True, slots=True)
class DeleteRoute:
    handle: RuntimeHandle
    generation: int


def _conflict() -> DomainError:
    return DomainError("AGENT_CHILD_DELETION_CONFLICT", "父子清理范围或运行环境发生变化", 409)


def _task(db: Session, parent_id: str) -> BackgroundTask | None:
    return db.scalar(
        select(BackgroundTask).where(
            BackgroundTask.owner_user_id == current_user_id(default=FLOWWEAVE_USER_ID),
            BackgroundTask.task_type == TASK_TYPE,
            BackgroundTask.idempotency_key == f"delete-child-family:{parent_id}",
        )
    )


def projection(db: Session, parent_id: str) -> dict[str, Any] | None:
    task = _task(db, parent_id)
    if task is None:
        return None
    payload = task.payload_json
    code = str(task.last_error or "").split(":", 1)[0]
    return {
        "task_id": task.id,
        "state": task.state,
        "phase": payload.get("phase"),
        "child_binding_ids": list(payload.get("child_binding_ids") or []),
        "completed_children": int(payload.get("final_index") or 0),
        "failure_code": code if re.fullmatch(r"[A-Z][A-Z0-9_]{0,99}", code) else None,
    }


def family(
    db: Session, parent: AgentConversationBinding
) -> list[tuple[AgentChildConversation, AgentConversationBinding]]:
    require_ordinary_binding(db, parent)
    relations = list(
        db.scalars(
            select(AgentChildConversation)
            .where(AgentChildConversation.parent_binding_id == parent.id)
            .order_by(AgentChildConversation.created_at, AgentChildConversation.child_binding_id)
        )
    )
    if not relations:
        return []
    if any(relation.creation_policy_version != POLICY_VERSION for relation in relations):
        raise DomainError(
            "AGENT_CHILDREN_DELETE_REQUIRED", "历史子关联需要显式清理，无法自动删除", 409
        )
    frozen = read_parent_capability(parent)
    if frozen.profile not in {"OH_FW_V4", "OH_FW_V5"}:
        raise _conflict()
    host = AgentSessionHostContext.create(
        host_kind=parent.host_kind,
        host_id=parent.host_id,
        conversation_scope_id=parent.conversation_scope_id,
        runtime_session_id=parent.runtime_session_id,
        working_directory=parent.working_directory or "",
    )
    result: list[tuple[AgentChildConversation, AgentConversationBinding]] = []
    for relation in relations:
        _, _, child = require_direct_child(
            db, host, parent_binding_id=parent.id, child_binding_id=relation.child_binding_id
        )
        if (
            relation.creation_policy_version != POLICY_VERSION
            or relation.accepted_generation != frozen.policy.generation
            or relation.manifest_digest != frozen.policy.manifest_digest
            or not relation.expected_persistence_dir
        ):
            raise _conflict()
        result.append((relation, child))
    return result


def request(db: Session, parent: AgentConversationBinding) -> dict[str, Any] | None:
    """Called after host authorization and stopped-parent checks, under its row lock."""
    if parent.owner_user_id != current_user_id(default=FLOWWEAVE_USER_ID):
        raise _conflict()
    task = _task(db, parent.id)
    members = family(db, parent)
    if not members and task is None:
        return None
    if task is None:
        if parent.lifecycle != "ACTIVE":
            raise _conflict()
        parent.lifecycle = "DELETE_PENDING"
        for _, child in members:
            child.lifecycle = "DELETE_PENDING"
        task = enqueue(
            db,
            task_type=TASK_TYPE,
            aggregate_type="AGENT_CONVERSATION_DELETION",
            # The audit ledger outlives binding-scoped record removal.
            aggregate_id=str(uuid4()),
            idempotency_key=f"delete-child-family:{parent.id}",
            payload={
                "parent_binding_id": parent.id,
                "parent_conversation_id": parent.openhands_conversation_id,
                "runtime_session_id": parent.runtime_session_id,
                "host_kind": parent.host_kind,
                "host_id": parent.host_id,
                "conversation_scope_id": parent.conversation_scope_id,
                "child_binding_ids": [child.id for _, child in members],
                "phase": "CHILDREN",
                "first_index": 0,
                "final_index": 0,
            },
        )
    elif parent.lifecycle != "DELETE_PENDING":
        raise _conflict()
    elif task.state == TaskState.DEAD:
        # An explicit user retry revives the same operation, preserving its checkpoints.
        task.state = TaskState.PENDING
        task.attempts = 0
        task.available_at = now()
        task.last_error = None
    db.flush()
    return {"parent_binding_id": parent.id, **(projection(db, parent.id) or {})}


def _route(db: Session, parent: AgentConversationBinding) -> DeleteRoute:
    frozen = read_parent_capability(parent)
    if frozen.profile not in {"OH_FW_V4", "OH_FW_V5"}:
        raise _conflict()
    if parent.host_kind == "AGENT_WORKSPACE":
        from flowweave.modules.agent_sessions.application import conversations

        handle = conversations.deletion_runtime_handle(db, parent)
        resource = db.get(ManagedSandbox, handle.runtime_resource_id, populate_existing=True)
        session = db.get(AgentWorkspaceRuntime, parent.runtime_session_id, populate_existing=True)
        if resource is None or resource.image_reference != frozen.runtime_image_digest:
            raise _conflict()
    else:
        connection = sandboxes.active_node_runtime_connection(
            db, flow_run_id=parent.host_id, node_attempt_id=parent.conversation_scope_id
        )
        session = db.get(FlowRunRuntime, parent.runtime_session_id, populate_existing=True)
        if (
            connection.runtime_session_id != parent.runtime_session_id
            or connection.protocol_profile.value != frozen.profile
        ):
            raise _conflict()
        handle = RuntimeHandle(
            f"env-chat:{connection.resource_name}",
            parent.openhands_conversation_id,
            runtime_resource_id=connection.managed_runtime_id,
            runtime_resource_name=connection.resource_name,
            workspace_root=frozen.working_directory,
            protocol_profile=frozen.profile,
        )
        if session is None or connection.generation != session.active_generation:
            raise _conflict()
    if (
        session is None
        or session.status != "ACTIVE"
        or session.active_generation is None
        or session.runtime_image_digest != frozen.runtime_image_digest
    ):
        raise _conflict()
    return DeleteRoute(handle, session.active_generation)


def _delete_native(handle: RuntimeHandle) -> None:
    try:
        get_runtime().delete_conversation(handle)
    except DomainError as exc:
        if exc.code != "RUNTIME_CONVERSATION_MISSING":
            raise


def _remove_files(
    db: Session, parent: AgentConversationBinding, binding_id: str, runtime_root: str
) -> None:
    root = (
        workspace_host.agent_workspace_record_path(db, parent.host_id)
        if parent.host_kind == "AGENT_WORKSPACE"
        else sandboxes.node_attempt_workspace_context(
            db, flow_run_id=parent.host_id, node_attempt_id=parent.conversation_scope_id
        ).host_working_directory
    )
    delete_owned_attachment_files(root, runtime_root, binding_id)
    # The native file API is Runtime-scoped. An upload admitted before the
    # fence can finish after this sweep, including an upload whose HTTP reply
    # was lost. Retain an independent, owner-prefix-only fallback after the
    # bounded upload window, even when the binding and record no longer exist.
    enqueue(
        db,
        task_type="CLEANUP_DELETED_CONVERSATION_ATTACHMENTS",
        aggregate_type="AGENT_CONVERSATION_DELETION",
        aggregate_id=str(uuid4()),
        idempotency_key=f"cleanup-deleted-conversation-attachments:{binding_id}",
        available_at=now() + timedelta(minutes=10),
        payload={"binding_id": binding_id, "host_root": str(root), "runtime_root": runtime_root},
    )


def sweep_deleted_attachments(db: Session, payload: dict[str, Any]) -> None:
    """The protected cleanup receipt is created only from an authorized host route."""
    binding_id = str(payload["binding_id"])
    if db.get(AgentConversationBinding, binding_id) is not None:
        return
    root = Path(str(payload["host_root"]))
    if not root.is_absolute():
        raise _conflict()
    delete_owned_attachment_files(root, str(payload["runtime_root"]), binding_id)


def _remove_child(
    db: Session, parent: AgentConversationBinding, child_id: str, runtime_root: str
) -> None:
    members = {child.id: (relation, child) for relation, child in family(db, parent)}
    if child_id not in members:
        raise _conflict()
    relation, _ = members[child_id]
    _remove_files(db, parent, child_id, runtime_root)
    db.delete(relation)
    db.flush()
    delete_binding_records(db, child_id)


def _job(db: Session, lease: Lease) -> BackgroundTask:
    task = db.get(BackgroundTask, lease.task_id, populate_existing=True)
    if (
        task is None
        or task.task_type != TASK_TYPE
        or task.owner_user_id != current_user_id(default=FLOWWEAVE_USER_ID)
        or not lease_is_current(db, lease)
    ):
        raise DomainError("AGENT_CHILD_DELETION_LEASE_LOST", "会话清理任务租约已失效", 409)
    return task


def _parent_for_job(db: Session, task: BackgroundTask) -> AgentConversationBinding | None:
    payload = task.payload_json
    parent = db.get(AgentConversationBinding, payload["parent_binding_id"], populate_existing=True)
    if parent is None:
        if db.scalar(
            select(AgentChildConversation.id).where(
                AgentChildConversation.parent_binding_id == payload["parent_binding_id"]
            )
        ) or db.scalar(
            select(AgentConversationBinding.id).where(
                AgentConversationBinding.id.in_(payload["child_binding_ids"])
            )
        ):
            raise _conflict()
        return None
    if (
        parent.owner_user_id != task.owner_user_id
        or parent.lifecycle != "DELETE_PENDING"
        or parent.openhands_conversation_id != payload["parent_conversation_id"]
        or parent.runtime_session_id != payload["runtime_session_id"]
        or parent.host_kind != payload["host_kind"]
        or parent.host_id != payload["host_id"]
        or parent.conversation_scope_id != payload["conversation_scope_id"]
    ):
        raise _conflict()
    return parent


def process(db: Session, lease: Lease) -> None:
    """One Worker lease owns short committed checkpoints around native I/O.

    In the fixed native source, parent deletion and child prepare/release hold
    the same parent lifecycle lock. A final child pass after that barrier also
    collects a preparation which was absent during the first pass.
    """
    while True:
        task = _job(db, lease)
        data = dict(task.payload_json)
        if data["phase"] == "COMPLETE":
            return
        parent = _parent_for_job(db, task)
        if parent is None:
            # Authorized record cleanup can have completed the whole family.
            data.update(phase="COMPLETE", final_index=len(data["child_binding_ids"]))
            task.payload_json = data
            db.commit()
            return
        route = _route(db, parent)
        ids = data["child_binding_ids"]
        phase = data["phase"]
        if phase == "CHILDREN" and data["first_index"] == len(ids):
            data["phase"] = "PARENT"
            task.payload_json = data
            db.commit()
            continue
        if phase == "FINAL" and data["final_index"] == len(ids):
            data["phase"] = "RECORD"
            task.payload_json = data
            db.commit()
            continue
        child_id = None
        if phase == "CHILDREN":
            child_id = ids[data["first_index"]]
        elif phase == "FINAL":
            child_id = ids[data["final_index"]]
        native = route.handle
        if child_id is not None:
            children = {child.id: child for _, child in family(db, parent)}
            if child_id not in children:
                raise _conflict()
            native = replace(native, conversation_id=children[child_id].openhands_conversation_id)
        parent_id = parent.id
        db.commit()
        if phase != "RECORD":
            _delete_native(native)
        # The worker's heartbeat runs independently. A stale lease/generation
        # can repeat an idempotent native effect but cannot discard any locator.
        task = _job(db, lease)
        parent = _parent_for_job(db, task)
        if parent is None or task.payload_json != data or _route(db, parent) != route:
            raise _conflict()
        if phase == "CHILDREN":
            data["first_index"] += 1
        elif phase == "PARENT":
            data["phase"] = "FINAL"
        elif phase == "FINAL" and child_id is not None:
            _remove_child(db, parent, child_id, route.handle.workspace_root or "")
            data["final_index"] += 1
        elif phase == "RECORD":
            _remove_files(db, parent, parent_id, route.handle.workspace_root or "")
            delete_binding_records(db, parent_id)
            data["phase"] = "COMPLETE"
        else:
            raise _conflict()
        task.payload_json = data
        db.commit()


def cleanup_record_family(
    db: Session, parent: AgentConversationBinding, *, allow_inactive_runtime: bool
) -> bool:
    """Use the same native barrier inside an already cancelled record's UoW.

    The enclosing record deletion owns retries. No internal commit may release
    its NodeRun/Attempt cancellation fence or commit unrelated record edits.
    """
    members = family(db, parent)
    if not members:
        task = _task(db, parent.id)
        if task is None or parent.lifecycle != "DELETE_PENDING":
            return False
        if task.payload_json["final_index"] != len(task.payload_json["child_binding_ids"]):
            raise _conflict()
    parent.lifecycle = "DELETE_PENDING"
    for _, child in members:
        child.lifecycle = "DELETE_PENDING"
    db.flush()
    try:
        route = _route(db, parent)
    except DomainError as exc:
        if (
            exc.code != "RUNTIME_SESSION_NOT_ACTIVE"
            or not allow_inactive_runtime
            or sandboxes.runtime_owner_flow_run_id(db, parent.host_id) != parent.host_id
        ):
            raise
        # Full owner-record deletion also removes this Runtime's persistence.
        # A borrowed or still reachable Runtime never takes this path.
        route = None
    if route is not None:
        for _, child in members:
            _delete_native(replace(route.handle, conversation_id=child.openhands_conversation_id))
        _delete_native(route.handle)
        for _, child in members:
            _delete_native(replace(route.handle, conversation_id=child.openhands_conversation_id))
        if _route(db, parent) != route:
            raise _conflict()
    runtime_root = route.handle.workspace_root if route else parent.working_directory
    for _, child in members:
        _remove_child(db, parent, child.id, runtime_root or "")
    _remove_files(db, parent, parent.id, runtime_root or "")
    delete_binding_records(db, parent.id)
    return True
