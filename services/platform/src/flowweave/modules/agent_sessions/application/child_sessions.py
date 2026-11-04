"""User/host authorization and projections for persistent native child conversations."""

from __future__ import annotations

import base64
from dataclasses import asdict, dataclass, replace
from pathlib import PurePosixPath
from typing import Any, Literal
from uuid import UUID, uuid4

from sqlalchemy import select
from sqlalchemy.orm import Session

from flowweave.modules.agent_sessions.application import conversations
from flowweave.modules.agent_sessions.application.child_creation import POLICY_VERSION
from flowweave.modules.agent_sessions.application.child_parent import read_parent_capability
from flowweave.modules.agent_sessions.application.child_relations import (
    guard_main_route,
    require_direct_child,
)
from flowweave.modules.agent_sessions.application.flow_node_host import (
    assert_flow_node_session_writable,
)
from flowweave.modules.agent_sessions.application.host import AgentSessionHostContext
from flowweave.modules.agent_sessions.application.workspace_file_links import (
    project_sandbox_file_links,
)
from flowweave.modules.agent_sessions.infrastructure.models import (
    AgentConversationBinding,
    AgentConversationMessageAttachment,
)
from flowweave.modules.agent_workspaces.infrastructure.models import (
    AgentWorkspace,
    AgentWorkspaceRuntime,
)
from flowweave.modules.sandboxes import public as sandboxes
from flowweave.modules.users.application.security import current_user_id
from flowweave.modules.users.application.service import agent_session_principal
from flowweave.runtime.base import (
    RuntimeEventBatch,
    RuntimeHandle,
    RuntimeProvider,
    RuntimeWorkspaceFile,
)
from flowweave.runtime.child_governance import NativeGovernedChildPreparation
from flowweave.runtime.dependencies import get_runtime
from flowweave.shared.database import now
from flowweave.shared.errors import DomainError
from flowweave.shared.models import FlowRun, FlowRunRuntime, ManagedSandbox, NodeAttempt, NodeRun


@dataclass(frozen=True, slots=True)
class ChildLocator:
    owner_id: str
    parent_id: str
    binding_id: str
    handle: RuntimeHandle
    parent_handle: RuntimeHandle
    generation: int
    creation_id: UUID
    persistence_dir: str
    working_directory: str
    host_kind: str
    host_id: str
    scope_id: str
    runtime_session_id: str
    native_parent: bool = False


def unavailable() -> DomainError:
    return DomainError("AGENT_CHILD_NOT_AVAILABLE", "子会话不存在或尚未完成准备", 404)


def resolve(
    db: Session, parent_id: str, child_id: str, *, write: bool = False, native_parent: bool = False
) -> ChildLocator:
    owner = current_user_id()
    if agent_session_principal(db, owner) is None:
        raise unavailable()
    parent = db.scalar(
        select(AgentConversationBinding).where(
            AgentConversationBinding.id == parent_id,
            AgentConversationBinding.owner_user_id == owner,
        )
    )
    if parent is None or parent.lifecycle not in (
        {"ACTIVE", "PROVISIONING"} if native_parent else {"ACTIVE"}
    ):
        raise unavailable()
    guard_main_route(db, host_kind=parent.host_kind, host_id=parent.host_id, binding_id=parent.id)
    frozen = read_parent_capability(parent)
    if frozen.profile != "OH_FW_V4":
        raise unavailable()
    if parent.host_kind == "AGENT_WORKSPACE":
        workspace = db.get(AgentWorkspace, parent.host_id)
        if workspace is None:
            raise unavailable()
        if write and workspace.desired_state != "RUNNING":
            raise unavailable()
        handle = conversations.usage_reconciliation_handle(db, parent)
        resource = db.get(ManagedSandbox, handle.runtime_resource_id)
        if resource is None or resource.image_reference != frozen.runtime_image_digest:
            raise unavailable()
        session = db.get(AgentWorkspaceRuntime, parent.runtime_session_id)
    else:
        # Existing node context checks remain the host's authority. Children
        # have no Attempt ownership; all node mutation fences use the parent.
        attempt = db.get(NodeAttempt, parent.conversation_scope_id)
        node = db.get(NodeRun, attempt.node_run_id) if attempt is not None else None
        run = db.get(FlowRun, parent.host_id)
        if node is None or node.flow_run_id != parent.host_id or run is None:
            raise unavailable()
        if write:
            assert_flow_node_session_writable(
                db, flow_run_id=parent.host_id, attempt_id=parent.conversation_scope_id
            )
        connection = sandboxes.active_node_runtime_connection(
            db, flow_run_id=parent.host_id, node_attempt_id=parent.conversation_scope_id
        )
        handle = RuntimeHandle(
            f"env-chat:{connection.resource_name}",
            parent.openhands_conversation_id,
            runtime_resource_id=connection.managed_runtime_id,
            runtime_resource_name=connection.resource_name,
            workspace_root=frozen.working_directory,
            protocol_profile=connection.protocol_profile.value,
        )
        session = db.get(FlowRunRuntime, parent.runtime_session_id)
        if (
            connection.runtime_session_id != parent.runtime_session_id
            or connection.generation != frozen.policy.generation
        ):
            raise unavailable()
    if (
        session is None
        or session.status != "ACTIVE"
        or session.active_generation != frozen.policy.generation
        or session.runtime_image_digest != frozen.runtime_image_digest
        or handle.protocol_profile != "OH_FW_V4"
    ):
        raise unavailable()
    host = AgentSessionHostContext.create(
        host_kind=parent.host_kind,
        host_id=parent.host_id,
        conversation_scope_id=parent.conversation_scope_id,
        runtime_session_id=parent.runtime_session_id,
        working_directory=frozen.working_directory,
    )
    relation, _, child = require_direct_child(
        db,
        host,
        parent_binding_id=parent.id,
        child_binding_id=child_id,
        allow_provisioning_parent=native_parent,
    )
    if (
        relation.creation_policy_version != POLICY_VERSION
        or child.lifecycle != "ACTIVE"
        or not relation.expected_persistence_dir
        or relation.accepted_generation != frozen.policy.generation
        or relation.manifest_digest != frozen.policy.manifest_digest
    ):
        raise unavailable()
    return ChildLocator(
        owner,
        parent.id,
        child.id,
        replace(handle, conversation_id=child.openhands_conversation_id),
        handle,
        frozen.policy.generation,
        UUID(relation.creation_id),
        relation.expected_persistence_dir,
        frozen.working_directory,
        parent.host_kind,
        parent.host_id,
        parent.conversation_scope_id,
        parent.runtime_session_id,
        native_parent,
    )


def revalidate(
    db: Session, locator: ChildLocator, *, write: bool = False
) -> AgentConversationBinding:
    fresh = resolve(
        db, locator.parent_id, locator.binding_id, write=write, native_parent=locator.native_parent
    )
    if fresh != locator:
        raise DomainError("AGENT_RUNTIME_REPLACEMENT_FENCED", "子会话定位已变化，请刷新", 409)
    child = db.get(AgentConversationBinding, locator.binding_id)
    if child is None:
        raise unavailable()
    return child


def verify_native(locator: ChildLocator) -> None:
    runtime = get_runtime()
    prepared = runtime.read_child_preparation(locator.parent_handle, locator.creation_id)
    configuration = runtime.child_conversation_configuration(locator.handle)
    if (
        not isinstance(prepared, NativeGovernedChildPreparation)
        or prepared.phase != "started"
        or str(prepared.governance_id) != locator.binding_id
        or str(prepared.receipt.identity.conversation_id) != locator.handle.conversation_id
        or str(prepared.receipt.identity.parent_conversation_id)
        != locator.parent_handle.conversation_id
        or prepared.receipt.identity.creation_id != locator.creation_id
        or prepared.workspace != locator.working_directory
        or str(configuration.id) != locator.handle.conversation_id
        or str(configuration.parent_conversation_id) != locator.parent_handle.conversation_id
        or configuration.workspace.working_dir != locator.working_directory
        or configuration.persistence_dir != locator.persistence_dir
    ):
        raise unavailable()


def read(
    locator: ChildLocator, cursor: str | None = None, history_cursor: str | None = None
) -> RuntimeEventBatch:
    verify_native(locator)
    return get_runtime().read_active_events(
        replace(locator.handle, cursor=cursor, history_cursor=history_cursor)
    )


def file_url(locator: ChildLocator, path: str) -> str:
    from urllib.parse import quote

    base = f"/api/v1/agent-session-children/{locator.parent_id}/{locator.binding_id}/file"
    return f"{base}?path={quote(path, safe='')}"


def project_events(db: Session, locator: ChildLocator, batch: RuntimeEventBatch) -> dict[str, Any]:
    revalidate(db, locator)
    # Project only browser display metadata. Native events retain their IDs,
    # parent/action/tool-call ancestry and all pagination/branch cursors.
    ids = [event.cursor for event in batch.events]
    records = (
        list(
            db.scalars(
                select(AgentConversationMessageAttachment).where(
                    AgentConversationMessageAttachment.binding_id == locator.binding_id,
                    AgentConversationMessageAttachment.event_id.in_(ids),
                )
            )
        )
        if ids
        else []
    )
    from flowweave.modules.agent_sessions.application.event_branch import (
        terminal_assistant_message_ids,
    )
    from flowweave.modules.agent_workspaces.application.task_watchdog import task_control_projection

    terminal = terminal_assistant_message_ids(batch)
    events: list[dict[str, Any]] = []
    for event in batch.events:
        payload = dict(event.payload)
        if event.event_type == "MESSAGE" and str(payload.get("source") or "").lower() in {
            "agent",
            "assistant",
        }:
            payload["final_reply"] = event.cursor in terminal
        content = payload.get("content")
        if isinstance(content, str):
            text, refs, workspace_refs, annotations = conversations.project_conversation_references(
                content
            )
            payload["content"] = project_sandbox_file_links(
                text, lambda path: file_url(locator, path)
            )
            if refs:
                payload["conversation_references"] = list(refs)
            if workspace_refs:
                payload["workspace_references"] = list(workspace_refs)
            if annotations:
                payload["collaboration_annotations"] = list(annotations)
        attachments = [
            {
                "path": a.path,
                "filename": a.filename,
                "mime_type": a.mime_type,
                "byte_size": a.byte_size,
                "url": file_url(locator, a.path),
            }
            for a in records
            if a.event_id == event.cursor
        ]
        if attachments:
            payload["attachments"] = attachments
        events.append({"id": event.cursor, "event_type": event.event_type, "payload": payload})
    return {
        "events": events,
        "next_cursor": batch.cursor,
        "history_cursor": batch.history_cursor,
        "cursor_anchor_found": batch.cursor_anchor_found,
        "result": batch.result.as_dict() if batch.result else None,
        "task_usage": [asdict(item) for item in batch.task_usage],
        "task_control": task_control_projection(db, locator.binding_id),
        "condensation_task": condensation_projection(db, locator.binding_id),
        "context": batch.context,
        "readiness": batch.readiness.as_dict() if batch.readiness else None,
    }


def validate_file(
    db: Session, locator: ChildLocator, path: str, *, attachment_only: bool = False
) -> str:
    parsed = PurePosixPath(path)
    root = PurePosixPath(locator.handle.workspace_root or locator.working_directory)
    work = PurePosixPath(locator.working_directory)
    if parsed.as_posix() != path or ".." in parsed.parts or not parsed.is_absolute():
        raise unavailable()
    if parsed.is_relative_to(root / "uploads"):
        conversations.validate_attachment_owners(
            locator.binding_id, ({"path": path},), workspace_root=str(root)
        )
        if not db.scalar(
            select(AgentConversationMessageAttachment.id).where(
                AgentConversationMessageAttachment.binding_id == locator.binding_id,
                AgentConversationMessageAttachment.path == path,
            )
        ):
            raise unavailable()
    elif (
        attachment_only
        or not parsed.is_relative_to(work)
        or "uploads" in parsed.relative_to(work).parts
    ):
        raise unavailable()
    return path


def validate_message(
    db: Session,
    locator: ChildLocator,
    content: str,
    attachments: tuple[dict[str, Any], ...],
    references: tuple[dict[str, Any], ...],
) -> None:
    if not content.strip() and not attachments and not references:
        raise DomainError("AGENT_MESSAGE_EMPTY", "消息不能为空", 422)
    for item in attachments:
        validate_file(db, locator, str(item.get("path") or ""), attachment_only=True)
    # Only this child's own formal events or its parent can be quoted. Text
    # supplied by the browser is never trusted as the source event's content.
    for ref in references:
        source_id = str(ref.get("source_binding_id") or locator.binding_id)
        if source_id not in {locator.parent_id, locator.binding_id}:
            raise unavailable()
        event_id = ref.get("source_event_id")
        if not isinstance(event_id, str) or not event_id:
            raise unavailable()
    # Runtime lookup is deferred into dispatch, after the DB session is closed.


def send(
    locator: ChildLocator,
    submission_id: UUID,
    content: str,
    attachments: tuple[dict[str, Any], ...],
    references: tuple[dict[str, Any], ...],
) -> str:
    verify_native(locator)
    runtime = get_runtime()
    refs: list[dict[str, str]] = []
    for reference in references:
        source = str(reference.get("source_binding_id") or locator.binding_id)
        handle = locator.parent_handle if source == locator.parent_id else locator.handle
        event_id = str(reference["source_event_id"])
        event = runtime.read_event(handle, event_id)
        text = conversations.referenceable_event_text(event)
        if text is None:
            raise unavailable()
        selected = conversations.selected_reference_text(text, reference.get("selection"))
        if not selected:
            raise unavailable()
        refs.append({"event_id": event_id, "content": selected, "source_binding_id": source})
    prompt, images = conversations.message_payload(content, attachments, tuple(refs))
    # No optimistic platform queue or busy-read/append race: native submission
    # admission serializes user input with parent Tool sends and run slots.
    return runtime.send_child_message(locator.handle, submission_id, prompt, images)


def project_send(
    db: Session,
    locator: ChildLocator,
    event_id: str,
    content: str,
    attachments: tuple[dict[str, Any], ...],
) -> dict[str, object]:
    binding = mutation_binding(db, locator)
    # Stable native retries may return the same formal ID; metadata is idempotent.
    existing = set(
        db.scalars(
            select(AgentConversationMessageAttachment.path).where(
                AgentConversationMessageAttachment.binding_id == locator.binding_id,
                AgentConversationMessageAttachment.event_id == event_id,
            )
        )
    )
    conversations.record_message_attachments(
        db,
        binding,
        event_id,
        content,
        tuple(item for item in attachments if str(item["path"]) not in existing),
    )
    binding.final_reply_preview_event_id = None
    binding.final_reply_preview_content = None
    binding.last_message_at = now()
    db.flush()
    return {"accepted": True, "cursor": event_id, "queued_during_turn": False}


def control(
    locator: ChildLocator,
    operation: Literal["interrupt", "resume", "condense"],
    provider: RuntimeProvider | None = None,
) -> dict[str, object]:
    verify_native(locator)
    runtime = get_runtime()
    if provider is not None:
        if not runtime.can_accept_input(locator.handle):
            raise DomainError("AGENT_CONVERSATION_BUSY", "请在回复完成或暂停后切换模型", 409)
        runtime.switch_model(locator.handle, provider)
    elif operation == "interrupt":
        runtime.interrupt(locator.handle)
    elif operation == "resume":
        runtime.run(locator.handle)
    else:
        if not runtime.can_accept_input(locator.handle):
            raise DomainError("AGENT_CONVERSATION_BUSY", "请在回复完成或暂停后压缩上下文", 409)
        runtime.condense(locator.handle)
    return {"accepted": True}


def upload(locator: ChildLocator, filename: str, mime: str, content: bytes) -> dict[str, Any]:
    if (
        not filename
        or len(filename) > 240
        or "\x00" in filename
        or not content
        or len(content) > 25 * 1024 * 1024
        or len(mime) > 200
    ):
        raise DomainError("AGENT_ATTACHMENT_INVALID", "附件无效或超过 25 MiB", 422)
    verify_native(locator)
    path = get_runtime().upload_workspace_file(
        locator.handle,
        filename=filename,
        content_type=mime,
        content=content,
        attachment_owner_id=locator.binding_id,
    )
    conversations.validate_attachment_owners(
        locator.binding_id, ({"path": path},), workspace_root=locator.handle.workspace_root
    )
    return {
        "filename": filename,
        "mime_type": mime,
        "byte_size": len(content),
        "path": path,
        "image_data_url": f"data:{mime};base64,{base64.b64encode(content).decode('ascii')}"
        if mime.startswith("image/")
        else None,
    }


def project_upload(db: Session, locator: ChildLocator, value: dict[str, Any]) -> dict[str, Any]:
    revalidate(db, locator, write=True)
    db.add(
        AgentConversationMessageAttachment(
            binding_id=locator.binding_id,
            event_id=f"pending:{uuid4()}",
            content="",
            filename=value["filename"],
            mime_type=value["mime_type"],
            byte_size=value["byte_size"],
            path=value["path"],
        )
    )
    db.flush()
    return value


def download(locator: ChildLocator, path: str) -> RuntimeWorkspaceFile:
    verify_native(locator)
    return get_runtime().download_workspace_file(locator.handle, path)


def result_reference(locator: ChildLocator, event_id: str) -> dict[str, str]:
    verify_native(locator)
    event = get_runtime().read_event(locator.handle, event_id)
    if (
        event is None
        or event.event_type != "MESSAGE"
        or str(event.payload.get("source") or "").lower() not in {"agent", "assistant"}
    ):
        raise unavailable()
    text = conversations.referenceable_event_text(event)
    if text is None or len(text) > 10000:
        raise DomainError("AGENT_CHILD_REFERENCE_TOO_LARGE", "请选择不超过一万字的正式回复", 422)
    return {"source_binding_id": locator.binding_id, "source_event_id": event_id, "content": text}


def remove_pending_attachment(db: Session, locator: ChildLocator, path: str) -> bool:
    from flowweave.modules.agent_sessions.application.draft_attachments import (
        delete_pending_attachment,
    )
    from flowweave.modules.agent_workspaces import public as workspace_host

    revalidate(db, locator, write=True)
    validate_file(db, locator, path, attachment_only=True)
    root = (
        workspace_host.agent_workspace_record_path(db, locator.host_id)
        if locator.host_kind == "AGENT_WORKSPACE"
        else sandboxes.node_attempt_workspace_context(
            db, flow_run_id=locator.host_id, node_attempt_id=locator.scope_id
        ).host_working_directory
    )
    return delete_pending_attachment(
        db,
        root,
        locator.handle.workspace_root or locator.working_directory,
        locator.binding_id,
        path,
    )


def parent_reference_locator(
    db: Session, parent: AgentConversationBinding, source_id: str
) -> ChildLocator:
    # Only an explicitly selected direct child of this exact target parent.
    return resolve(db, parent.id, source_id)


def view(db: Session, locator: ChildLocator) -> dict[str, Any]:
    binding = revalidate(db, locator)
    try:
        resolve(db, locator.parent_id, locator.binding_id, write=True)
        writable = True
    except DomainError:
        writable = False
    return {
        **conversations.binding_view(db, binding, write_available=writable),
        "parent_binding_id": locator.parent_id,
    }


def mutation_binding(db: Session, locator: ChildLocator) -> AgentConversationBinding:
    try:
        binding = revalidate(db, locator, write=True)
        db.scalar(
            select(AgentConversationBinding)
            .where(AgentConversationBinding.id == binding.id)
            .with_for_update()
        )
        db.expire_all()
        return revalidate(db, locator, write=True)
    except DomainError:
        raise DomainError(
            "AGENT_CHILD_OPERATION_AMBIGUOUS",
            "子会话操作可能已受理，请按原提交身份对账",
            504,
            {"outcome_unknown": True},
        ) from None


def condensation_projection(db: Session, binding_id: str) -> dict[str, object] | None:
    from flowweave.modules.agent_sessions.application.condensation import (
        condensation_task_failure_reason,
    )
    from flowweave.shared.models import BackgroundTask

    task = db.scalar(
        select(BackgroundTask)
        .where(
            BackgroundTask.aggregate_id == binding_id,
            BackgroundTask.task_type == "CONDENSE_AGENT_CONVERSATION",
        )
        .order_by(BackgroundTask.created_at.desc())
        .limit(1)
    )
    return (
        {
            "task_id": task.id,
            "state": task.state,
            "failure_reason": condensation_task_failure_reason(task.last_error),
        }
        if task
        else None
    )
