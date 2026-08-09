from __future__ import annotations

import base64
import binascii
import hashlib
import json
import mimetypes
import os
import re
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, cast
from urllib.parse import unquote, urlparse
from uuid import uuid4

from sqlalchemy import delete, func, select, update
from sqlalchemy.orm import Session

from flowweave.modules.conversations.domain.enums import (
    CONVERSATION_ENABLED_ATTEMPT_STATES,
    TERMINAL_ATTEMPT_STATES,
    ConversationKind,
    ConversationState,
    DeliveryMode,
    DeliveryState,
    MessageSource,
    MessageType,
    transport_role,
)
from flowweave.modules.conversations.infrastructure.models import AgentConversation, AgentMessage
from flowweave.modules.tasks.public import Lease, enqueue, lease_is_current
from flowweave.runtime.base import RuntimeHandle, RuntimeResult
from flowweave.runtime.dependencies import get_runtime
from flowweave.runtime.request import build_runtime_request
from flowweave.shared.application.transactions import finish
from flowweave.shared.errors import DomainError, conflict, not_found
from flowweave.shared.models import (
    ArtifactVersion,
    AttemptInputBinding,
    BackgroundTask,
    EnvironmentVersion,
    FlowRun,
    HumanAction,
    NodeAttempt,
    NodeRun,
    RunEvent,
    RunSnapshot,
    TaskState,
    now,
)
from flowweave.shared.schemas import (
    ConversationCreateWrite,
    ConversationPatchWrite,
    MessageSendWrite,
)
from flowweave.shared.settings import get_settings

_WORKSPACE_IMAGE_TYPES = {
    "image/avif",
    "image/gif",
    "image/jpeg",
    "image/png",
    "image/webp",
}
_WORKSPACE_IMAGE_MAX_BYTES = 25 * 1024 * 1024
_MESSAGE_ATTACHMENT_MAX_BYTES = 10 * 1024 * 1024
_MESSAGE_ATTACHMENTS_MAX_BYTES = 20 * 1024 * 1024
_SAFE_ATTACHMENT_NAME = re.compile(r"[^A-Za-z0-9._\-\u4e00-\u9fff]+")


@dataclass(frozen=True)
class WorkspaceImageReference:
    path: Path
    media_type: str
    filename: str


@dataclass(frozen=True)
class AttachmentWorkspace:
    conversation_id: str
    attempt_root: Path
    workspace_root: Path
    runtime_root: Path


@dataclass(frozen=True)
class PreparedMessageContent:
    parts: list[dict[str, Any]]
    created_paths: tuple[Path, ...]


@dataclass(frozen=True)
class MessageAttachmentReference:
    path: Path
    media_type: str
    filename: str


def _conversation(db: Session, conversation_id: str, *, lock: bool = False) -> AgentConversation:
    query = select(AgentConversation).where(AgentConversation.id == conversation_id)
    if lock:
        query = query.with_for_update()
    item = db.scalar(query)
    if item is None:
        raise not_found("agent_conversation", conversation_id)
    return item


def _attempt(db: Session, attempt_id: str) -> NodeAttempt:
    item = db.get(NodeAttempt, attempt_id)
    if item is None:
        raise not_found("node_attempt", attempt_id)
    return item


def _context(db: Session, attempt: NodeAttempt) -> tuple[NodeRun, str]:
    node_run = db.get(NodeRun, attempt.node_run_id)
    if node_run is None:
        raise not_found("node_run", attempt.node_run_id)
    return node_run, node_run.flow_run_id


def _event(
    db: Session,
    conversation: AgentConversation,
    event_type: str,
    payload: dict[str, Any],
) -> None:
    attempt = _attempt(db, conversation.attempt_id)
    node_run, run_id = _context(db, attempt)
    db.add(
        RunEvent(
            flow_run_id=run_id,
            node_run_id=node_run.id,
            attempt_id=attempt.id,
            event_type=event_type,
            payload_json={"conversation_id": conversation.id, **payload},
        )
    )


def _baseline(db: Session, attempt: NodeAttempt) -> dict[str, Any]:
    node_run, _ = _context(db, attempt)
    bindings = list(
        db.scalars(
            select(AttemptInputBinding)
            .where(AttemptInputBinding.attempt_id == attempt.id)
            .order_by(AttemptInputBinding.input_field_key)
        )
    )
    artifacts = list(
        db.scalars(
            select(ArtifactVersion.id)
            .where(ArtifactVersion.producer_attempt_id == attempt.id)
            .order_by(ArtifactVersion.created_at)
        )
    )
    return {
        "schema_version": 1,
        "snapshot_id": attempt.snapshot_id,
        "node_key": node_run.flow_node_snapshot_key,
        "input_bindings": [
            {"field_key": row.input_field_key, "artifact_version_id": row.artifact_version_id}
            for row in bindings
        ],
        "selected_artifact_ids": artifacts,
        "workspace_ref": attempt.workspace_ref,
        "context_policy": "ATTEMPT_FACTS_NO_CROSS_CONVERSATION_MESSAGES",
    }


def _message_text(content: dict[str, Any]) -> str:
    parts = content.get("parts", [])
    return "\n".join(str(part.get("text", "")) for part in parts if part.get("type") == "text")


def _runtime_message_payload(content: dict[str, Any]) -> tuple[str, tuple[str, ...]]:
    text = _message_text(content)
    raw_refs = content.get("capability_refs")
    refs = (
        [
            cast(dict[str, Any], item)
            for item in cast(list[object], raw_refs)
            if isinstance(item, dict)
        ]
        if isinstance(raw_refs, list)
        else []
    )
    sections: list[str] = []
    if refs:
        directives = ["用户显式指定本条消息必须调用以下能力："]
        for ref in refs:
            capability_type = str(ref.get("capability_type") or "")
            capability_key = str(ref.get("capability_key") or "")
            if capability_type == "SKILL":
                directives.append(f'- Skill "{capability_key}"：先读取并遵循该 Skill，再完成请求。')
            else:
                directives.append(
                    f'- MCP "{capability_key}"：优先使用该 Server 暴露的合适工具完成请求。'
                )
        sections.append("\n".join(directives))
    if text:
        sections.append(("以下是用户原始消息：\n" if refs else "") + text)

    attachment_lines: list[str] = []
    image_urls: list[str] = []
    settings = get_settings()
    workspace_root = settings.workspace_root.resolve()
    for raw in cast(list[object], content.get("parts") or []):
        if not isinstance(raw, dict):
            continue
        part = cast(dict[str, Any], raw)
        if part.get("type") != "attachment":
            continue
        name = str(part.get("filename") or "attachment")
        media_type = str(part.get("mime_type") or "application/octet-stream")
        runtime_path = str(part.get("runtime_path") or "")
        attachment_lines.append(f"- {name}（{media_type}）：{runtime_path}")
        storage_path = Path(str(part.get("storage_path") or ""))
        host_path = (workspace_root / storage_path).resolve()
        if (
            media_type in _WORKSPACE_IMAGE_TYPES
            and host_path.is_relative_to(workspace_root)
            and host_path.is_file()
            and host_path.stat().st_size <= _MESSAGE_ATTACHMENT_MAX_BYTES
        ):
            encoded = base64.b64encode(host_path.read_bytes()).decode("ascii")
            image_urls.append(f"data:{media_type};base64,{encoded}")
    if attachment_lines:
        sections.append(
            "本条消息附带以下临时文件。文件已经放入当前 Attempt 工作区，可直接读取：\n"
            + "\n".join(attachment_lines)
        )
    return "\n\n".join(sections), tuple(image_urls)


def attachment_workspace(db: Session, conversation_id: str) -> AttachmentWorkspace:
    conversation = _conversation(db, conversation_id)
    attempt = _attempt(db, conversation.attempt_id)
    if not attempt.workspace_ref:
        raise DomainError(
            "ATTACHMENT_WORKSPACE_UNAVAILABLE", "Attempt workspace is unavailable", 409
        )
    settings = get_settings()
    workspace_root = settings.workspace_root.resolve()
    attempt_root = Path(attempt.workspace_ref).resolve()
    if not attempt_root.is_relative_to(workspace_root):
        raise DomainError("WORKSPACE_PATH_INVALID", "Attempt workspace is invalid", 409)
    runtime_root = settings.openhands_workspace_root / attempt_root.relative_to(workspace_root)
    return AttachmentWorkspace(conversation.id, attempt_root, workspace_root, runtime_root)


def prepare_message_content(
    payload: MessageSendWrite, workspace: AttachmentWorkspace
) -> PreparedMessageContent:
    parts: list[dict[str, Any]] = []
    created: list[Path] = []
    total_bytes = 0
    target_root = (
        workspace.attempt_root
        / "files"
        / "chat"
        / workspace.conversation_id
        / _SAFE_ATTACHMENT_NAME.sub("-", payload.client_message_id).strip(".-")
    ).resolve()
    if not target_root.is_relative_to(workspace.attempt_root):
        raise DomainError("ATTACHMENT_PATH_INVALID", "Attachment path is invalid", 422)
    for index, item in enumerate(payload.content):
        if item.type == "text":
            parts.append(item.model_dump())
            continue
        try:
            data = base64.b64decode(item.content_base64, validate=True)
        except (binascii.Error, ValueError) as exc:
            raise DomainError(
                "ATTACHMENT_INVALID", "Attachment content is not valid Base64", 422
            ) from exc
        if not data:
            raise DomainError("ATTACHMENT_EMPTY", "Attachment is empty", 422)
        if len(data) > _MESSAGE_ATTACHMENT_MAX_BYTES:
            raise DomainError("ATTACHMENT_TOO_LARGE", "Each attachment must not exceed 10 MB", 413)
        total_bytes += len(data)
        if total_bytes > _MESSAGE_ATTACHMENTS_MAX_BYTES:
            raise DomainError(
                "ATTACHMENTS_TOO_LARGE", "Message attachments must not exceed 20 MB", 413
            )
        original_name = Path(item.filename).name
        safe_name = _SAFE_ATTACHMENT_NAME.sub("-", original_name).strip(".-")[:180] or "attachment"
        digest = hashlib.sha256(data).hexdigest()
        attachment_id = f"{index + 1}-{digest[:16]}"
        destination = (target_root / f"{attachment_id}-{safe_name}").resolve()
        if not destination.is_relative_to(target_root):
            raise DomainError("ATTACHMENT_PATH_INVALID", "Attachment path is invalid", 422)
        destination.parent.mkdir(parents=True, exist_ok=True)
        if not destination.exists():
            temporary = destination.with_name(f".{destination.name}.{uuid4().hex}.tmp")
            temporary.write_bytes(data)
            temporary.chmod(0o644)
            os.replace(temporary, destination)
            created.append(destination)
        runtime_path = workspace.runtime_root / destination.relative_to(workspace.attempt_root)
        parts.append(
            {
                "type": "attachment",
                "attachment_id": attachment_id,
                "filename": original_name,
                "mime_type": item.mime_type,
                "byte_size": len(data),
                "content_hash": digest,
                "storage_path": str(destination.relative_to(workspace.workspace_root)),
                "runtime_path": str(runtime_path),
            }
        )
    return PreparedMessageContent(parts, tuple(created))


def discard_prepared_message_content(prepared: PreparedMessageContent) -> None:
    for path in prepared.created_paths:
        path.unlink(missing_ok=True)


def _public_message_content(content: dict[str, Any]) -> dict[str, Any]:
    result = dict(content)
    result["parts"] = [
        {key: value for key, value in cast(dict[str, Any], part).items() if key != "storage_path"}
        if isinstance(part, dict)
        else part
        for part in cast(list[object], content.get("parts") or [])
    ]
    return result


def _validated_capability_refs(
    db: Session, attempt: NodeAttempt, payload: MessageSendWrite
) -> list[dict[str, str]]:
    requested = [item.model_dump() for item in payload.capability_refs]
    if not requested:
        return []
    snapshot = db.get(RunSnapshot, attempt.snapshot_id)
    node_run, _ = _context(db, attempt)
    raw_nodes: object = snapshot.definition_json.get("nodes", []) if snapshot else []
    nodes = cast(list[dict[str, Any]], raw_nodes) if isinstance(raw_nodes, list) else []
    node = next(
        (item for item in nodes if item.get("instance_key") == node_run.flow_node_snapshot_key),
        None,
    )
    asset = cast(dict[str, Any], node.get("asset") or {}) if node else {}
    raw_capabilities: object = asset.get("capabilities")
    capabilities = (
        [
            cast(dict[str, Any], item)
            for item in cast(list[object], raw_capabilities)
            if isinstance(item, dict)
        ]
        if isinstance(raw_capabilities, list)
        else []
    )
    available = {
        (str(item.get("capability_type") or ""), str(item.get("capability_key") or ""))
        for item in capabilities
    }
    missing = [
        item
        for item in requested
        if (item["capability_type"], item["capability_key"]) not in available
    ]
    if missing:
        raise DomainError(
            "CAPABILITY_NOT_AVAILABLE",
            "Selected capability is not available in this node snapshot",
            422,
            {"capability_refs": missing},
        )
    return requested


def _message_dict(item: AgentMessage) -> dict[str, Any]:
    return {
        "id": item.id,
        "conversation_id": item.conversation_id,
        "sequence_no": item.sequence_no,
        "source": item.source,
        "transport_role": item.transport_role,
        "message_type": item.message_type,
        "content": _public_message_content(item.content_json),
        "delivery_state": item.delivery_state,
        "delivery_mode": item.delivery_mode,
        "client_message_id": item.client_message_id,
        "runtime_cursor": item.runtime_cursor,
        "error_code": item.error_code,
        "error_detail": item.error_detail,
        "created_by": item.created_by,
        "created_at": item.created_at.isoformat(),
        "delivered_at": item.delivered_at.isoformat() if item.delivered_at else None,
    }


def _conversation_dict(db: Session, item: AgentConversation) -> dict[str, Any]:
    last = db.scalar(
        select(AgentMessage)
        .where(AgentMessage.conversation_id == item.id)
        .order_by(AgentMessage.sequence_no.desc())
        .limit(1)
    )
    count = (
        db.scalar(
            select(func.count(AgentMessage.id)).where(AgentMessage.conversation_id == item.id)
        )
        or 0
    )
    return {
        "id": item.id,
        "attempt_id": item.attempt_id,
        "conversation_no": item.conversation_no,
        "kind": item.kind,
        "title": item.title,
        "state": item.state,
        "state_version": item.state_version,
        "runtime_adapter": item.runtime_adapter,
        "runtime_job_id": item.runtime_job_id,
        "runtime_conversation_id": item.runtime_conversation_id,
        "context_baseline": item.context_baseline_json,
        "message_count": count,
        "last_message": _message_dict(last) if last else None,
        "created_at": item.created_at.isoformat(),
        "updated_at": item.updated_at.isoformat(),
    }


def terminal_container_id(db: Session, conversation_id: str) -> str:
    item = _conversation(db, conversation_id)
    job_id = item.runtime_job_id or ""
    for prefix in ("env-exec:", "env-chat:"):
        if job_id.startswith(prefix) and job_id.removeprefix(prefix):
            return job_id.removeprefix(prefix)
    raise DomainError(
        "AGENT_TERMINAL_UNAVAILABLE",
        "This Agent conversation is not running in a terminal environment",
        409,
    )


def _append(
    db: Session,
    conversation: AgentConversation,
    *,
    source: str,
    message_type: str,
    content: dict[str, Any],
    delivery_state: str,
    delivery_mode: str | None = None,
    client_message_id: str | None = None,
    runtime_event_id: str | None = None,
    runtime_cursor: str | None = None,
    created_by: str | None = None,
) -> AgentMessage:
    next_sequence = db.scalar(
        update(AgentConversation)
        .where(AgentConversation.id == conversation.id)
        .values(next_sequence_no=AgentConversation.next_sequence_no + 1)
        .returning(AgentConversation.next_sequence_no)
        .execution_options(synchronize_session=False)
    )
    if next_sequence is None:
        raise not_found("agent_conversation", conversation.id)
    sequence = int(next_sequence) - 1
    db.expire(conversation, ["next_sequence_no"])
    item = AgentMessage(
        conversation_id=conversation.id,
        sequence_no=sequence,
        source=source,
        transport_role=transport_role(source),
        message_type=message_type,
        content_json=content,
        delivery_state=delivery_state,
        delivery_mode=delivery_mode,
        client_message_id=client_message_id,
        runtime_event_id=runtime_event_id,
        runtime_cursor=runtime_cursor,
        created_by=created_by,
        delivered_at=now() if delivery_state == DeliveryState.DELIVERED else None,
    )
    db.add(item)
    db.flush()
    _event(
        db,
        conversation,
        "AGENT_MESSAGE_CREATED",
        {
            "message_id": item.id,
            "sequence_no": item.sequence_no,
            "source": item.source,
            "message_type": item.message_type,
        },
    )
    return item


def ensure_auto_conversation(db: Session, attempt: NodeAttempt) -> AgentConversation:
    existing = db.scalar(
        select(AgentConversation).where(
            AgentConversation.attempt_id == attempt.id,
            AgentConversation.kind == ConversationKind.AUTO,
        )
    )
    if existing is not None:
        return existing
    item = AgentConversation(
        attempt_id=attempt.id,
        conversation_no=1,
        kind=ConversationKind.AUTO,
        title=f"自动执行 · Attempt {attempt.attempt_no}",
        state=ConversationState.CREATING,
        context_baseline_json=_baseline(db, attempt),
        created_by_type=MessageSource.PROGRAM,
    )
    db.add(item)
    db.flush()
    snapshot = db.get(RunSnapshot, attempt.snapshot_id)
    node_run, _ = _context(db, attempt)
    node_name = node_run.flow_node_snapshot_key
    if snapshot:
        node = next(
            (
                x
                for x in snapshot.definition_json.get("nodes", [])
                if x.get("instance_key") == node_name
            ),
            None,
        )
        if node:
            node_name = str(node.get("alias") or node.get("asset", {}).get("name") or node_name)
    _append(
        db,
        item,
        source=MessageSource.PROGRAM,
        message_type=MessageType.TEXT,
        content={"parts": [{"type": "text", "text": f"流程自动启动节点：{node_name}"}]},
        delivery_state=DeliveryState.DELIVERING,
    )
    _event(db, item, "CONVERSATION_CREATED", {"kind": item.kind})
    return item


def bind_auto_runtime(
    db: Session,
    attempt_id: str,
    *,
    runtime_job_id: str,
    runtime_conversation_id: str,
    runtime_cursor: str | None,
    runtime_adapter: str,
) -> None:
    attempt = _attempt(db, attempt_id)
    item = ensure_auto_conversation(db, attempt)
    item.runtime_job_id = runtime_job_id
    item.runtime_conversation_id = runtime_conversation_id
    item.runtime_cursor = runtime_cursor
    item.runtime_adapter = runtime_adapter
    item.state = ConversationState.GENERATING
    item.state_version += 1
    program = db.scalar(
        select(AgentMessage)
        .where(
            AgentMessage.conversation_id == item.id, AgentMessage.source == MessageSource.PROGRAM
        )
        .order_by(AgentMessage.sequence_no)
        .limit(1)
    )
    if program:
        program.delivery_state = DeliveryState.DELIVERED
        program.delivered_at = now()
    _event(
        db, item, "CONVERSATION_STATE_CHANGED", {"to": item.state, "version": item.state_version}
    )


def project_runtime_event(
    db: Session,
    attempt_id: str,
    *,
    cursor: str,
    event_type: str,
    payload: dict[str, Any],
) -> None:
    conversation = db.scalar(
        select(AgentConversation).where(
            AgentConversation.attempt_id == attempt_id,
            AgentConversation.kind == ConversationKind.AUTO,
        )
    )
    if conversation is None:
        return
    existing = db.scalar(
        select(AgentMessage.id).where(
            AgentMessage.conversation_id == conversation.id,
            AgentMessage.runtime_event_id == cursor,
        )
    )
    if existing:
        return
    content: dict[str, Any]
    if event_type == "MESSAGE":
        role = str(payload.get("role") or payload.get("source") or "").lower()
        if role in {"user", "human", "program"}:
            return
        raw: object = (
            payload.get("content") or payload.get("message") or payload.get("text") or payload
        )
        if isinstance(raw, dict) and "parts" in raw:
            content = cast(dict[str, Any], raw)
        else:
            rendered = (
                str(raw)
                if isinstance(raw, str | int | float | bool)
                else json.dumps(payload, ensure_ascii=False, default=str)
            )
            content = {"parts": [{"type": "text", "text": rendered}]}
        kind = MessageType.TEXT
    elif event_type in {"TOOL", "TOOL_CALL", "TOOL_RESULT"}:
        content = {"tool": payload}
        kind = MessageType.TOOL_RESULT if event_type == "TOOL_RESULT" else MessageType.TOOL_CALL
    elif event_type == "THOUGHT":
        content = {"state": payload}
        kind = MessageType.STATE
    elif event_type == "ERROR":
        content = {"error": payload}
        kind = MessageType.ERROR
    else:
        return
    _append(
        db,
        conversation,
        source=MessageSource.AGENT,
        message_type=kind,
        content=content,
        delivery_state=DeliveryState.DELIVERED,
        runtime_event_id=cursor,
        runtime_cursor=cursor,
    )
    conversation.runtime_cursor = cursor


def _auto_conversation(db: Session, attempt_id: str) -> AgentConversation | None:
    return db.scalar(
        select(AgentConversation).where(
            AgentConversation.attempt_id == attempt_id,
            AgentConversation.kind == ConversationKind.AUTO,
        )
    )


def record_auto_human_input(db: Session, attempt_id: str, *, action_id: str, content: str) -> None:
    conversation = _auto_conversation(db, attempt_id)
    if conversation is None or conversation.state == ConversationState.READ_ONLY:
        return
    runtime_event_id = f"human-action:{action_id}"
    existing = db.scalar(
        select(AgentMessage.id).where(
            AgentMessage.conversation_id == conversation.id,
            AgentMessage.runtime_event_id == runtime_event_id,
        )
    )
    if existing is None:
        _append(
            db,
            conversation,
            source=MessageSource.HUMAN,
            message_type=MessageType.TEXT,
            content={"parts": [{"type": "text", "text": content}]},
            delivery_state=DeliveryState.QUEUED,
            runtime_event_id=runtime_event_id,
        )
    if conversation.state != ConversationState.GENERATING:
        previous = conversation.state
        conversation.state = ConversationState.GENERATING
        conversation.state_version += 1
        _event(
            db,
            conversation,
            "CONVERSATION_STATE_CHANGED",
            {
                "from": previous,
                "to": conversation.state,
                "version": conversation.state_version,
            },
        )


def mark_auto_human_input_delivered(db: Session, attempt_id: str, *, action_id: str) -> None:
    conversation = _auto_conversation(db, attempt_id)
    if conversation is None:
        return
    message = db.scalar(
        select(AgentMessage).where(
            AgentMessage.conversation_id == conversation.id,
            AgentMessage.runtime_event_id == f"human-action:{action_id}",
        )
    )
    if message is None or message.delivery_state == DeliveryState.DELIVERED:
        return
    message.delivery_state = DeliveryState.DELIVERED
    message.delivered_at = now()
    _event(
        db,
        conversation,
        "AGENT_MESSAGE_DELIVERY_CHANGED",
        {"message_id": message.id, "delivery_state": message.delivery_state},
    )


def project_auto_runtime_result(
    db: Session,
    attempt_id: str,
    result: RuntimeResult,
    *,
    result_key: str,
) -> None:
    conversation = _auto_conversation(db, attempt_id)
    if conversation is None or result.status == "RUNNING":
        return
    runtime_event_id = f"runtime-result:{result_key}:{result.status}:{result.cursor or ''}"
    existing = db.scalar(
        select(AgentMessage.id).where(
            AgentMessage.conversation_id == conversation.id,
            AgentMessage.runtime_event_id == runtime_event_id,
        )
    )
    if existing is not None:
        return
    if result.status == "HUMAN_INPUT_REQUIRED" and result.human_question:
        message_type = MessageType.TEXT
        content: dict[str, Any] = {"parts": [{"type": "text", "text": result.human_question}]}
    elif result.status == "FAILED":
        message_type = MessageType.ERROR
        content = {"error": {"message": result.error or "Runtime execution failed"}}
    elif result.status == "COMPLETED" and result.outputs:
        message_type = MessageType.TEXT
        content = {
            "presentation": "final",
            "parts": [
                {
                    "type": "text",
                    "text": "\n".join(value[1] for value in result.outputs.values()),
                }
            ],
        }
    else:
        return
    _append(
        db,
        conversation,
        source=MessageSource.AGENT,
        message_type=message_type,
        content=content,
        delivery_state=DeliveryState.DELIVERED,
        runtime_event_id=runtime_event_id,
        runtime_cursor=result.cursor,
    )


def set_attempt_conversations_state(db: Session, attempt_id: str, state: str) -> None:
    rows = list(
        db.scalars(select(AgentConversation).where(AgentConversation.attempt_id == attempt_id))
    )
    for item in rows:
        if item.state != state:
            previous = item.state
            item.state = state
            item.state_version += 1
            _event(
                db,
                item,
                "CONVERSATION_STATE_CHANGED",
                {"from": previous, "to": state, "version": item.state_version},
            )
        if state == ConversationState.READ_ONLY:
            queued = list(
                db.scalars(
                    select(AgentMessage).where(
                        AgentMessage.conversation_id == item.id,
                        AgentMessage.delivery_state == DeliveryState.QUEUED,
                    )
                )
            )
            for message in queued:
                message.delivery_state = DeliveryState.CANCELLED
                message.error_code = "ATTEMPT_TERMINAL"


def set_auto_conversation_state(db: Session, attempt_id: str, state: str) -> None:
    item = db.scalar(
        select(AgentConversation).where(
            AgentConversation.attempt_id == attempt_id,
            AgentConversation.kind == ConversationKind.AUTO,
        )
    )
    if item is None:
        return
    previous = item.state
    if previous != state:
        item.state = state
        item.state_version += 1
        _event(
            db,
            item,
            "CONVERSATION_STATE_CHANGED",
            {"from": previous, "to": state, "version": item.state_version},
        )
    if state != ConversationState.IDLE:
        return
    queued = db.scalar(
        select(AgentMessage)
        .where(
            AgentMessage.conversation_id == item.id,
            AgentMessage.delivery_state == DeliveryState.QUEUED,
        )
        .order_by(AgentMessage.sequence_no)
        .limit(1)
    )
    if queued is not None:
        item.state = ConversationState.GENERATING
        item.state_version += 1
        enqueue(
            db,
            task_type="DELIVER_CONVERSATION_MESSAGE",
            aggregate_type="MESSAGE",
            aggregate_id=queued.id,
            idempotency_key=(f"deliver-conversation-message:{queued.id}:v{item.state_version}"),
        )


def schedule_next_queued_message(db: Session, attempt_id: str) -> None:
    conversation = db.scalar(
        select(AgentConversation).where(
            AgentConversation.attempt_id == attempt_id,
            AgentConversation.kind == ConversationKind.AUTO,
        )
    )
    if conversation is None or conversation.state in {
        ConversationState.CREATING,
        ConversationState.GENERATING,
        ConversationState.WAITING_HUMAN,
        ConversationState.FAILED,
        ConversationState.READ_ONLY,
    }:
        return
    message = db.scalar(
        select(AgentMessage)
        .where(
            AgentMessage.conversation_id == conversation.id,
            AgentMessage.delivery_state == DeliveryState.QUEUED,
        )
        .order_by(AgentMessage.sequence_no)
        .limit(1)
    )
    if message is None:
        return
    conversation.state = ConversationState.GENERATING
    conversation.state_version += 1
    enqueue(
        db,
        task_type="DELIVER_CONVERSATION_MESSAGE",
        aggregate_type="MESSAGE",
        aggregate_id=message.id,
        idempotency_key=(
            f"deliver-conversation-message:{message.id}:v{conversation.state_version}"
        ),
    )
    _event(
        db,
        conversation,
        "CONVERSATION_STATE_CHANGED",
        {"to": conversation.state, "version": conversation.state_version},
    )


def list_conversations(db: Session, attempt_id: str) -> list[dict[str, Any]]:
    _attempt(db, attempt_id)
    rows = list(
        db.scalars(
            select(AgentConversation)
            .where(AgentConversation.attempt_id == attempt_id)
            .order_by(AgentConversation.conversation_no)
        )
    )
    return [_conversation_dict(db, item) for item in rows]


def get_conversation(db: Session, conversation_id: str) -> dict[str, Any]:
    return _conversation_dict(db, _conversation(db, conversation_id))


def create_conversation(
    db: Session,
    attempt_id: str,
    payload: ConversationCreateWrite,
    idempotency_key: str,
) -> dict[str, Any]:
    existing_action = db.scalar(
        select(HumanAction).where(HumanAction.idempotency_key == idempotency_key)
    )
    if existing_action:
        conversation_id = str(existing_action.payload_json.get("conversation_id", ""))
        return get_conversation(db, conversation_id)
    attempt = db.scalar(select(NodeAttempt).where(NodeAttempt.id == attempt_id).with_for_update())
    if attempt is None:
        raise not_found("node_attempt", attempt_id)
    if attempt.state in TERMINAL_ATTEMPT_STATES:
        raise DomainError("ATTEMPT_TERMINAL", "Terminal attempt cannot create conversations", 409)
    if attempt.state not in CONVERSATION_ENABLED_ATTEMPT_STATES:
        raise DomainError(
            "ATTEMPT_NOT_STARTED",
            "Attempt must start execution before creating conversations",
            409,
        )
    if attempt.state_version != payload.expected_attempt_state_version:
        raise conflict(
            "attempt was modified",
            expected=payload.expected_attempt_state_version,
            actual=attempt.state_version,
        )
    count = (
        db.scalar(
            select(func.count(AgentConversation.id)).where(
                AgentConversation.attempt_id == attempt.id
            )
        )
        or 0
    )
    if count >= get_settings().conversation_limit_per_attempt:
        raise DomainError("CONVERSATION_LIMIT_REACHED", "Conversation limit reached", 422)
    number = int(count) + 1
    item = AgentConversation(
        attempt_id=attempt.id,
        conversation_no=number,
        kind=ConversationKind.HUMAN_CREATED,
        title=payload.title or f"人工会话 {number}",
        state=ConversationState.CREATING,
        context_baseline_json=_baseline(db, attempt),
        created_by_type=MessageSource.HUMAN,
    )
    db.add(item)
    db.flush()
    _append(
        db,
        item,
        source=MessageSource.PROGRAM,
        message_type=MessageType.TEXT,
        content={
            "parts": [
                {
                    "type": "text",
                    "text": (
                        "当前 Attempt 的快照、输入绑定、候选能力与产物已挂载；"
                        "本会话不包含其他会话消息，也不继承自动执行的启动任务。"
                        "Agent 将根据本会话中的消息动态选择能力。"
                    ),
                }
            ]
        },
        delivery_state=DeliveryState.DELIVERING,
    )
    node_run, run_id = _context(db, attempt)
    db.add(
        HumanAction(
            flow_run_id=run_id,
            node_run_id=node_run.id,
            attempt_id=attempt.id,
            action_type="CREATE_AGENT_CONVERSATION",
            idempotency_key=idempotency_key,
            payload_json={"conversation_id": item.id},
        )
    )
    enqueue(
        db,
        task_type="CREATE_CONVERSATION",
        aggregate_type="CONVERSATION",
        aggregate_id=item.id,
        idempotency_key=f"create-conversation:{item.id}",
    )
    _event(db, item, "CONVERSATION_CREATED", {"kind": item.kind})
    finish(db)
    return _conversation_dict(db, item)


def patch_conversation(
    db: Session, conversation_id: str, payload: ConversationPatchWrite
) -> dict[str, Any]:
    item = _conversation(db, conversation_id, lock=True)
    if item.state_version != payload.expected_conversation_version:
        raise conflict(
            "conversation was modified",
            expected=payload.expected_conversation_version,
            actual=item.state_version,
        )
    item.title = payload.title
    item.state_version += 1
    finish(db)
    return _conversation_dict(db, item)


def delete_conversation(db: Session, conversation_id: str) -> None:
    item = _conversation(db, conversation_id)
    if item.kind == ConversationKind.AUTO:
        raise DomainError(
            "AUTO_CONVERSATION_REQUIRED",
            "The automatic execution conversation is part of the Attempt audit trail",
            409,
        )
    handle = (
        RuntimeHandle(
            item.runtime_job_id or item.runtime_conversation_id,
            item.runtime_conversation_id,
            item.runtime_cursor,
        )
        if item.runtime_conversation_id
        else None
    )
    db.rollback()
    if handle is not None:
        get_runtime().cancel(handle)

    current = _conversation(db, conversation_id, lock=True)
    if current.kind == ConversationKind.AUTO:
        raise DomainError(
            "AUTO_CONVERSATION_REQUIRED", "Automatic conversation cannot be deleted", 409
        )
    message_rows = list(
        db.scalars(select(AgentMessage).where(AgentMessage.conversation_id == current.id))
    )
    message_ids = [message.id for message in message_rows]
    attachment_paths: list[Path] = []
    settings = get_settings()
    workspace_root = settings.workspace_root.resolve()
    attempt = _attempt(db, current.attempt_id)
    attempt_root = Path(attempt.workspace_ref or "").resolve()
    if attempt.workspace_ref and attempt_root.is_relative_to(workspace_root):
        for message in message_rows:
            for raw in cast(list[object], message.content_json.get("parts") or []):
                if not isinstance(raw, dict):
                    continue
                part = cast(dict[str, Any], raw)
                if part.get("type") != "attachment":
                    continue
                path = (workspace_root / str(part.get("storage_path") or "")).resolve()
                if path.is_relative_to(attempt_root):
                    attachment_paths.append(path)
    aggregate_ids = [current.id, *message_ids]
    db.execute(delete(BackgroundTask).where(BackgroundTask.aggregate_id.in_(aggregate_ids)))
    db.delete(current)
    finish(db)
    for path in attachment_paths:
        path.unlink(missing_ok=True)


def list_messages(
    db: Session, conversation_id: str, after_sequence: int, limit: int
) -> list[dict[str, Any]]:
    _conversation(db, conversation_id)
    rows = list(
        db.scalars(
            select(AgentMessage)
            .where(
                AgentMessage.conversation_id == conversation_id,
                AgentMessage.sequence_no > after_sequence,
            )
            .order_by(AgentMessage.sequence_no)
            .limit(min(limit, 200))
        )
    )
    return [_message_dict(item) for item in rows]


def workspace_image_reference(db: Session, message_id: str, source: str) -> WorkspaceImageReference:
    message = db.get(AgentMessage, message_id)
    if message is None:
        raise not_found("agent_message", message_id)
    conversation = _conversation(db, message.conversation_id)
    attempt = _attempt(db, conversation.attempt_id)
    if not attempt.workspace_ref:
        raise not_found("workspace_image", message_id)

    settings = get_settings()
    workspace_root = settings.workspace_root.resolve()
    attempt_root = Path(attempt.workspace_ref).resolve()
    if attempt_root != workspace_root and not attempt_root.is_relative_to(workspace_root):
        raise DomainError("WORKSPACE_PATH_INVALID", "Attempt workspace is invalid", 409)

    parsed = urlparse(source)
    if parsed.scheme not in {"", "file"} or parsed.netloc not in {"", "localhost"}:
        raise DomainError("WORKSPACE_IMAGE_INVALID", "Workspace image URL is invalid", 422)
    raw_path = Path(unquote(parsed.path if parsed.scheme == "file" else source))
    openhands_root = settings.openhands_workspace_root
    if raw_path.is_absolute() and raw_path.is_relative_to(openhands_root):
        candidate = workspace_root / raw_path.relative_to(openhands_root)
    elif raw_path.is_absolute() and raw_path.is_relative_to(workspace_root):
        candidate = raw_path
    elif not raw_path.is_absolute():
        candidate = attempt_root / raw_path
    else:
        raise not_found("workspace_image", message_id)

    resolved = candidate.resolve()
    if not resolved.is_relative_to(attempt_root) or not resolved.is_file():
        raise not_found("workspace_image", message_id)
    media_type = mimetypes.guess_type(resolved.name)[0] or "application/octet-stream"
    if media_type not in _WORKSPACE_IMAGE_TYPES:
        raise DomainError(
            "WORKSPACE_IMAGE_UNSUPPORTED",
            "Workspace file is not a supported image",
            415,
        )
    if resolved.stat().st_size > _WORKSPACE_IMAGE_MAX_BYTES:
        raise DomainError("WORKSPACE_IMAGE_TOO_LARGE", "Workspace image is too large", 413)
    return WorkspaceImageReference(resolved, media_type, resolved.name)


def message_attachment_reference(
    db: Session, message_id: str, attachment_id: str
) -> MessageAttachmentReference:
    message = db.get(AgentMessage, message_id)
    if message is None:
        raise not_found("agent_message", message_id)
    conversation = _conversation(db, message.conversation_id)
    attempt = _attempt(db, conversation.attempt_id)
    if not attempt.workspace_ref:
        raise not_found("message_attachment", attachment_id)
    part = None
    for raw in cast(list[object], message.content_json.get("parts") or []):
        if not isinstance(raw, dict):
            continue
        candidate = cast(dict[str, Any], raw)
        if (
            candidate.get("type") == "attachment"
            and candidate.get("attachment_id") == attachment_id
        ):
            part = candidate
            break
    if part is None:
        raise not_found("message_attachment", attachment_id)
    settings = get_settings()
    workspace_root = settings.workspace_root.resolve()
    attempt_root = Path(attempt.workspace_ref).resolve()
    candidate = (workspace_root / str(part.get("storage_path") or "")).resolve()
    if not candidate.is_relative_to(attempt_root) or not candidate.is_file():
        raise not_found("message_attachment", attachment_id)
    return MessageAttachmentReference(
        candidate,
        str(part.get("mime_type") or "application/octet-stream"),
        Path(str(part.get("filename") or candidate.name)).name,
    )


def send_message(
    db: Session,
    conversation_id: str,
    payload: MessageSendWrite,
    idempotency_key: str,
    actor: str | None,
    *,
    prepared_parts: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    existing_action = db.scalar(
        select(HumanAction).where(HumanAction.idempotency_key == idempotency_key)
    )
    if existing_action is not None:
        message_id = str(existing_action.payload_json.get("message_id", ""))
        existing_message = db.get(AgentMessage, message_id)
        if existing_message is not None:
            return _message_dict(existing_message)
    item = _conversation(db, conversation_id, lock=True)
    existing = db.scalar(
        select(AgentMessage).where(
            AgentMessage.conversation_id == item.id,
            AgentMessage.client_message_id == payload.client_message_id,
        )
    )
    if existing:
        return _message_dict(existing)
    allow_stale_queue = (
        item.state == ConversationState.GENERATING
        and payload.delivery_mode == DeliveryMode.QUEUE_AFTER_TURN
    )
    if item.state_version != payload.expected_conversation_version and not allow_stale_queue:
        raise conflict(
            "conversation was modified",
            expected=payload.expected_conversation_version,
            actual=item.state_version,
        )
    attempt = _attempt(db, item.attempt_id)
    if item.state == ConversationState.READ_ONLY or attempt.state in TERMINAL_ATTEMPT_STATES:
        raise DomainError("CONVERSATION_READ_ONLY", "Conversation is read only", 409)
    if item.kind == ConversationKind.AUTO and attempt.state == "WAITING_HUMAN":
        raise DomainError(
            "ATTEMPT_HUMAN_INPUT_REQUIRED",
            "Reply through the Attempt human-input command",
            409,
        )
    if item.state == ConversationState.CREATING:
        raise DomainError("CONVERSATION_NOT_READY", "Conversation is not ready", 409)
    text = "\n".join(part.text for part in payload.content if part.type == "text")
    if len(text) > get_settings().conversation_message_max_chars:
        raise DomainError("MESSAGE_TOO_LARGE", "Message is too large", 422)
    capability_refs = _validated_capability_refs(db, attempt, payload)
    queued_during_turn = item.state == ConversationState.GENERATING
    message = _append(
        db,
        item,
        source=MessageSource.HUMAN,
        message_type=MessageType.TEXT,
        content={
            "parts": prepared_parts or [part.model_dump() for part in payload.content],
            "capability_refs": capability_refs,
            "presentation": "queued" if queued_during_turn else "chat",
        },
        delivery_state=DeliveryState.QUEUED,
        delivery_mode=payload.delivery_mode,
        client_message_id=payload.client_message_id,
        created_by=actor,
    )
    item.state_version += 1
    node_run, run_id = _context(db, attempt)
    db.add(
        HumanAction(
            flow_run_id=run_id,
            node_run_id=node_run.id,
            attempt_id=attempt.id,
            action_type="SEND_AGENT_MESSAGE",
            idempotency_key=idempotency_key,
            payload_json={
                "conversation_id": item.id,
                "message_id": message.id,
                "client_message_id": payload.client_message_id,
                "content_length": len(text),
                "attachment_count": sum(part.type == "attachment" for part in payload.content),
                "capability_refs": capability_refs,
            },
        )
    )
    if item.state == ConversationState.FAILED:
        item.state = ConversationState.CREATING
        item.runtime_job_id = None
        item.runtime_conversation_id = None
        item.runtime_cursor = None
        enqueue(
            db,
            task_type="CREATE_CONVERSATION",
            aggregate_type="CONVERSATION",
            aggregate_id=item.id,
            idempotency_key=f"recreate-conversation:{item.id}:message:{message.id}",
        )
    elif item.state in {ConversationState.IDLE, ConversationState.WAITING_HUMAN}:
        item.state = ConversationState.GENERATING
        message.delivery_state = DeliveryState.DELIVERING
        enqueue(
            db,
            task_type="DELIVER_CONVERSATION_MESSAGE",
            aggregate_type="MESSAGE",
            aggregate_id=message.id,
            idempotency_key=f"deliver-conversation-message:{message.id}",
        )
    finish(db)
    result = _message_dict(message)
    result["conversation_state_version"] = item.state_version
    return result


def steer_message(db: Session, message_id: str, idempotency_key: str) -> dict[str, Any]:
    existing_action = db.scalar(
        select(HumanAction).where(HumanAction.idempotency_key == idempotency_key)
    )
    if existing_action is not None:
        existing = db.get(AgentMessage, message_id)
        if existing is not None:
            return _message_dict(existing)
    message = db.scalar(select(AgentMessage).where(AgentMessage.id == message_id).with_for_update())
    if message is None:
        raise not_found("agent_message", message_id)
    conversation = _conversation(db, message.conversation_id, lock=True)
    attempt = _attempt(db, conversation.attempt_id)
    if (
        conversation.state == ConversationState.READ_ONLY
        or attempt.state in TERMINAL_ATTEMPT_STATES
    ):
        raise DomainError("CONVERSATION_READ_ONLY", "Conversation is read only", 409)
    if message.source != MessageSource.HUMAN:
        raise DomainError("MESSAGE_NOT_STEERABLE", "Only human messages can steer the Agent", 409)
    if message.delivery_mode == DeliveryMode.INTERRUPT_AND_RESUME:
        return _message_dict(message)
    if message.delivery_state not in {DeliveryState.QUEUED, DeliveryState.DELIVERING}:
        return _message_dict(message)
    message.delivery_mode = DeliveryMode.INTERRUPT_AND_RESUME
    message.delivery_state = DeliveryState.DELIVERING
    message.content_json = {**message.content_json, "presentation": "chat"}
    node_run, run_id = _context(db, attempt)
    db.add(
        HumanAction(
            flow_run_id=run_id,
            node_run_id=node_run.id,
            attempt_id=attempt.id,
            action_type="STEER_AGENT_MESSAGE",
            idempotency_key=idempotency_key,
            payload_json={"conversation_id": conversation.id, "message_id": message.id},
        )
    )
    enqueue(
        db,
        task_type="DELIVER_CONVERSATION_MESSAGE",
        aggregate_type="MESSAGE",
        aggregate_id=message.id,
        idempotency_key=f"steer-conversation-message:{message.id}:{idempotency_key}",
    )
    finish(db)
    return _message_dict(message)


def cancel_queued_message(db: Session, message_id: str, idempotency_key: str) -> dict[str, Any]:
    existing_action = db.scalar(
        select(HumanAction).where(HumanAction.idempotency_key == idempotency_key)
    )
    if existing_action is not None:
        existing = db.get(AgentMessage, message_id)
        if existing is not None:
            return _message_dict(existing)
    message = db.scalar(select(AgentMessage).where(AgentMessage.id == message_id).with_for_update())
    if message is None:
        raise not_found("agent_message", message_id)
    if message.delivery_state == DeliveryState.CANCELLED:
        return _message_dict(message)
    if (
        message.source != MessageSource.HUMAN
        or message.delivery_state != DeliveryState.QUEUED
        or message.content_json.get("presentation") != "queued"
    ):
        raise DomainError(
            "MESSAGE_NOT_QUEUED",
            "Only a message waiting in the Agent queue can be removed",
            409,
        )
    conversation = _conversation(db, message.conversation_id, lock=True)
    attempt = _attempt(db, conversation.attempt_id)
    message.delivery_state = DeliveryState.CANCELLED
    message.content_json = {**message.content_json, "presentation": "cancelled-queue"}
    message.delivered_at = now()
    node_run, run_id = _context(db, attempt)
    db.add(
        HumanAction(
            flow_run_id=run_id,
            node_run_id=node_run.id,
            attempt_id=attempt.id,
            action_type="CANCEL_QUEUED_AGENT_MESSAGE",
            idempotency_key=idempotency_key,
            payload_json={"conversation_id": conversation.id, "message_id": message.id},
        )
    )
    _event(
        db,
        conversation,
        "AGENT_MESSAGE_DELIVERY_CHANGED",
        {"message_id": message.id, "delivery_state": DeliveryState.CANCELLED},
    )
    finish(db)
    return _message_dict(message)


def retry_message(db: Session, message_id: str, idempotency_key: str) -> dict[str, Any]:
    message = db.get(AgentMessage, message_id)
    if message is None:
        raise not_found("agent_message", message_id)
    conversation = _conversation(db, message.conversation_id, lock=True)
    if message.delivery_state not in {DeliveryState.FAILED, DeliveryState.QUEUED}:
        return _message_dict(message)
    attempt = _attempt(db, conversation.attempt_id)
    if (
        conversation.state == ConversationState.READ_ONLY
        or attempt.state in TERMINAL_ATTEMPT_STATES
    ):
        raise DomainError("CONVERSATION_READ_ONLY", "Conversation is read only", 409)
    if conversation.kind == ConversationKind.AUTO and attempt.state == "WAITING_HUMAN":
        raise DomainError(
            "ATTEMPT_HUMAN_INPUT_REQUIRED",
            "Reply through the Attempt human-input command",
            409,
        )
    was_failed = message.delivery_state == DeliveryState.FAILED
    message.delivery_state = DeliveryState.QUEUED
    message.error_code = None
    message.error_detail = None
    conversation.state_version += 1
    if (
        conversation.state in {ConversationState.FAILED, ConversationState.CREATING}
        or not conversation.runtime_conversation_id
    ):
        conversation.state = ConversationState.CREATING
        if was_failed or not conversation.runtime_conversation_id:
            conversation.runtime_job_id = None
            conversation.runtime_conversation_id = None
            conversation.runtime_cursor = None
        if (
            _active_task(
                db,
                aggregate_id=conversation.id,
                task_types={"CREATE_CONVERSATION"},
            )
            is None
        ):
            enqueue(
                db,
                task_type="CREATE_CONVERSATION",
                aggregate_type="CONVERSATION",
                aggregate_id=conversation.id,
                idempotency_key=f"recreate-conversation:{conversation.id}:{idempotency_key}",
            )
    else:
        conversation.state = ConversationState.GENERATING
        if (
            _active_task(
                db,
                aggregate_id=message.id,
                task_types={"DELIVER_CONVERSATION_MESSAGE"},
            )
            is None
        ):
            enqueue(
                db,
                task_type="DELIVER_CONVERSATION_MESSAGE",
                aggregate_type="MESSAGE",
                aggregate_id=message.id,
                idempotency_key=f"retry-conversation-message:{message.id}:{idempotency_key}",
            )
    finish(db)
    return _message_dict(message)


def _active_task(db: Session, *, aggregate_id: str, task_types: set[str]) -> BackgroundTask | None:
    return db.scalar(
        select(BackgroundTask).where(
            BackgroundTask.aggregate_id == aggregate_id,
            BackgroundTask.task_type.in_(task_types),
            BackgroundTask.state.in_([TaskState.PENDING, TaskState.RETRY, TaskState.RUNNING]),
        )
    )


def _recover_delivery(
    db: Session,
    *,
    task_type: str,
    aggregate_type: str,
    aggregate_id: str,
    recovery_key: str,
    payload: dict[str, Any] | None = None,
) -> bool:
    if _active_task(db, aggregate_id=aggregate_id, task_types={task_type}) is not None:
        return False
    existing = db.scalar(
        select(BackgroundTask).where(BackgroundTask.idempotency_key == recovery_key)
    )
    if existing is None:
        enqueue(
            db,
            task_type=task_type,
            aggregate_type=aggregate_type,
            aggregate_id=aggregate_id,
            idempotency_key=recovery_key,
            payload=payload,
            available_at=datetime.now(UTC),
        )
    else:
        existing.state = TaskState.RETRY
        existing.available_at = datetime.now(UTC)
        existing.lease_owner = None
        existing.lease_until = None
        existing.last_error = "STARTUP_RECOVERY"
        existing.payload_json = payload or {}
    return True


def recover_conversation_tasks(db: Session) -> int:
    """Restore missing creation, delivery, and polling work after worker restart."""

    conversations = list(
        db.scalars(
            select(AgentConversation)
            .where(
                AgentConversation.state.in_(
                    [
                        ConversationState.CREATING,
                        ConversationState.IDLE,
                        ConversationState.GENERATING,
                        ConversationState.WAITING_HUMAN,
                    ]
                )
            )
            .order_by(AgentConversation.updated_at, AgentConversation.id)
            .with_for_update(skip_locked=True)
        )
    )
    recovered = 0
    for conversation in conversations:
        attempt = _attempt(db, conversation.attempt_id)
        if attempt.state in TERMINAL_ATTEMPT_STATES:
            set_attempt_conversations_state(db, attempt.id, ConversationState.READ_ONLY)
            continue
        if conversation.state == ConversationState.CREATING:
            if conversation.kind == ConversationKind.HUMAN_CREATED and _recover_delivery(
                db,
                task_type="CREATE_CONVERSATION",
                aggregate_type="CONVERSATION",
                aggregate_id=conversation.id,
                recovery_key=(
                    f"recovery:create-conversation:{conversation.id}:v{conversation.state_version}"
                ),
            ):
                recovered += 1
            continue

        queued = db.scalar(
            select(AgentMessage)
            .where(
                AgentMessage.conversation_id == conversation.id,
                AgentMessage.delivery_state == DeliveryState.QUEUED,
            )
            .order_by(AgentMessage.sequence_no)
            .limit(1)
        )
        if conversation.state in {ConversationState.IDLE, ConversationState.WAITING_HUMAN}:
            if (
                conversation.kind == ConversationKind.AUTO
                and conversation.state == ConversationState.WAITING_HUMAN
            ):
                continue
            if queued is not None and _recover_delivery(
                db,
                task_type="DELIVER_CONVERSATION_MESSAGE",
                aggregate_type="MESSAGE",
                aggregate_id=queued.id,
                recovery_key=f"recovery:deliver-conversation-message:{queued.id}",
            ):
                conversation.state = ConversationState.GENERATING
                conversation.state_version += 1
                recovered += 1
            continue

        if conversation.kind == ConversationKind.AUTO:
            if (
                queued is not None
                and attempt.state not in {"EXECUTING", "WAITING_HUMAN"}
                and _recover_delivery(
                    db,
                    task_type="DELIVER_CONVERSATION_MESSAGE",
                    aggregate_type="MESSAGE",
                    aggregate_id=queued.id,
                    recovery_key=(
                        f"recovery:deliver-conversation-message:{queued.id}"
                        f":v{conversation.state_version}"
                    ),
                )
            ):
                recovered += 1
            continue
        last_agent_sequence = (
            db.scalar(
                select(func.max(AgentMessage.sequence_no)).where(
                    AgentMessage.conversation_id == conversation.id,
                    AgentMessage.source == MessageSource.AGENT,
                )
            )
            or 0
        )
        last_delivered_human_sequence = (
            db.scalar(
                select(func.max(AgentMessage.sequence_no)).where(
                    AgentMessage.conversation_id == conversation.id,
                    AgentMessage.source == MessageSource.HUMAN,
                    AgentMessage.delivery_state == DeliveryState.DELIVERED,
                )
            )
            or 0
        )
        if queued is not None and last_delivered_human_sequence <= last_agent_sequence:
            if _recover_delivery(
                db,
                task_type="DELIVER_CONVERSATION_MESSAGE",
                aggregate_type="MESSAGE",
                aggregate_id=queued.id,
                recovery_key=f"recovery:deliver-conversation-message:{queued.id}",
            ):
                recovered += 1
        elif conversation.runtime_conversation_id and _recover_delivery(
            db,
            task_type="POLL_CONVERSATION",
            aggregate_type="CONVERSATION",
            aggregate_id=conversation.id,
            recovery_key=(
                f"recovery:poll-conversation:{conversation.id}:v{conversation.state_version}"
            ),
            payload={"poll_no": 1},
        ):
            recovered += 1
    finish(db)
    return recovered


def process_create_conversation(
    db: Session, conversation_id: str, lease: Lease, *, commit: bool = True
) -> None:
    item = _conversation(db, conversation_id)
    if item.state != ConversationState.CREATING:
        return
    attempt = _attempt(db, item.attempt_id)
    if attempt.state in TERMINAL_ATTEMPT_STATES:
        set_attempt_conversations_state(db, attempt.id, ConversationState.READ_ONLY)
        return
    snapshot = db.get(RunSnapshot, attempt.snapshot_id)
    node_run, run_id = _context(db, attempt)
    run = db.get(FlowRun, run_id)
    environment = (
        db.get(EnvironmentVersion, run.environment_version_id)
        if run and run.environment_version_id
        else None
    )
    raw_nodes: object = snapshot.definition_json.get("nodes", []) if snapshot else []
    nodes = cast(list[dict[str, Any]], raw_nodes) if isinstance(raw_nodes, list) else []
    node = next(
        item for item in nodes if item.get("instance_key") == node_run.flow_node_snapshot_key
    )
    bindings: list[dict[str, Any]] = []
    for binding in db.scalars(
        select(AttemptInputBinding).where(AttemptInputBinding.attempt_id == attempt.id)
    ):
        artifact = db.get(ArtifactVersion, binding.artifact_version_id)
        if artifact:
            bindings.append(
                {
                    "field_key": binding.input_field_key,
                    "artifact": {
                        "id": artifact.id,
                        "artifact_type": artifact.artifact_type,
                        "inline_content": artifact.inline_content,
                    },
                }
            )
    request = build_runtime_request(
        db,
        attempt_id=f"{attempt.id}:{item.id}",
        execution_key=f"conversation:{item.id}:create",
        node=node,
        bindings=bindings,
        workspace_ref=attempt.workspace_ref or "",
        interaction_mode="COLLABORATION",
        environment_image=environment.image_digest if environment else None,
    )
    expected_version = item.state_version
    db.rollback()
    handle = get_runtime().create_conversation(request)
    if not lease_is_current(db, lease):
        get_runtime().cancel(handle)
        raise RuntimeError("task lease was lost during conversation creation")
    claimed = db.scalar(
        update(AgentConversation)
        .where(
            AgentConversation.id == conversation_id,
            AgentConversation.state == ConversationState.CREATING,
            AgentConversation.state_version == expected_version,
        )
        .values(
            state=ConversationState.IDLE,
            state_version=AgentConversation.state_version + 1,
            runtime_adapter=get_settings().runtime_adapter,
            runtime_job_id=handle.job_id,
            runtime_conversation_id=handle.conversation_id,
            runtime_cursor=handle.cursor,
        )
        .returning(AgentConversation.id)
        .execution_options(synchronize_session=False)
    )
    if claimed is None:
        db.rollback()
        get_runtime().cancel(handle)
        return
    db.expire_all()
    current = _conversation(db, conversation_id)
    program = db.scalar(
        select(AgentMessage)
        .where(
            AgentMessage.conversation_id == current.id, AgentMessage.source == MessageSource.PROGRAM
        )
        .order_by(AgentMessage.sequence_no)
        .limit(1)
    )
    if program:
        program.delivery_state = DeliveryState.DELIVERED
        program.delivered_at = now()
    _event(
        db,
        current,
        "CONVERSATION_STATE_CHANGED",
        {"to": current.state, "version": current.state_version},
    )
    _schedule_next_message(db, current)
    db.commit() if commit else db.flush()


def _append_runtime_payload(
    db: Session,
    conversation: AgentConversation,
    *,
    cursor: str,
    event_type: str,
    payload: dict[str, Any],
) -> None:
    existing = db.scalar(
        select(AgentMessage.id).where(
            AgentMessage.conversation_id == conversation.id,
            AgentMessage.runtime_event_id == cursor,
        )
    )
    if existing is not None:
        return
    if event_type == "MESSAGE":
        role = str(payload.get("role") or payload.get("source") or "").lower()
        if role in {"user", "human", "program"}:
            return
        raw = payload.get("content") or payload.get("message") or payload.get("text")
        text = (
            str(raw)
            if isinstance(raw, str | int | float | bool)
            else json.dumps(payload, ensure_ascii=False, default=str)
        )
        content: dict[str, Any] = {"parts": [{"type": "text", "text": text}]}
        message_type = MessageType.TEXT
    elif event_type in {"TOOL", "TOOL_CALL", "TOOL_RESULT"}:
        content = {"tool": payload}
        message_type = (
            MessageType.TOOL_RESULT if event_type == "TOOL_RESULT" else MessageType.TOOL_CALL
        )
    elif event_type == "THOUGHT":
        content = {"state": payload}
        message_type = MessageType.STATE
    elif event_type == "ERROR":
        content = {"error": payload}
        message_type = MessageType.ERROR
    else:
        return
    _append(
        db,
        conversation,
        source=MessageSource.AGENT,
        message_type=message_type,
        content=content,
        delivery_state=DeliveryState.DELIVERED,
        runtime_event_id=cursor,
        runtime_cursor=cursor,
    )


def _apply_conversation_result(
    db: Session,
    conversation: AgentConversation,
    result: RuntimeResult,
    *,
    message_id: str,
) -> None:
    conversation.runtime_cursor = result.cursor or conversation.runtime_cursor
    if result.status == "RUNNING":
        next_state = ConversationState.GENERATING
    elif result.status == "HUMAN_INPUT_REQUIRED":
        next_state = ConversationState.WAITING_HUMAN
        if result.human_question:
            _append(
                db,
                conversation,
                source=MessageSource.AGENT,
                message_type=MessageType.TEXT,
                content={"parts": [{"type": "text", "text": result.human_question}]},
                delivery_state=DeliveryState.DELIVERED,
                runtime_event_id=f"human-question:{message_id}:{result.cursor or ''}",
                runtime_cursor=result.cursor,
            )
    elif result.status == "FAILED":
        next_state = ConversationState.FAILED
        _append(
            db,
            conversation,
            source=MessageSource.AGENT,
            message_type=MessageType.ERROR,
            content={"error": {"message": result.error or "Runtime conversation failed"}},
            delivery_state=DeliveryState.DELIVERED,
            runtime_event_id=f"conversation-error:{message_id}:{result.cursor or ''}",
            runtime_cursor=result.cursor,
        )
    else:
        next_state = ConversationState.IDLE
        if result.outputs:
            text = "\n".join(value[1] for value in result.outputs.values())
            _append(
                db,
                conversation,
                source=MessageSource.AGENT,
                message_type=MessageType.TEXT,
                content={
                    "presentation": "final",
                    "parts": [{"type": "text", "text": text}],
                },
                delivery_state=DeliveryState.DELIVERED,
                runtime_event_id=f"delivery-result:{message_id}:{result.cursor or ''}",
                runtime_cursor=result.cursor,
            )
    previous = conversation.state
    conversation.state = next_state
    conversation.state_version += 1
    if previous != next_state:
        _event(
            db,
            conversation,
            "CONVERSATION_STATE_CHANGED",
            {"from": previous, "to": next_state, "version": conversation.state_version},
        )


def _schedule_next_message(db: Session, conversation: AgentConversation) -> None:
    if conversation.state not in {ConversationState.IDLE, ConversationState.WAITING_HUMAN}:
        return
    if (
        conversation.kind == ConversationKind.AUTO
        and conversation.state == ConversationState.WAITING_HUMAN
    ):
        return
    next_message = db.scalar(
        select(AgentMessage)
        .where(
            AgentMessage.conversation_id == conversation.id,
            AgentMessage.delivery_state == DeliveryState.QUEUED,
        )
        .order_by(AgentMessage.sequence_no)
        .limit(1)
    )
    if next_message is None:
        return
    conversation.state = ConversationState.GENERATING
    conversation.state_version += 1
    enqueue(
        db,
        task_type="DELIVER_CONVERSATION_MESSAGE",
        aggregate_type="MESSAGE",
        aggregate_id=next_message.id,
        idempotency_key=(
            f"deliver-conversation-message:{next_message.id}:v{conversation.state_version}"
        ),
    )


def process_poll_conversation(
    db: Session,
    conversation_id: str,
    poll_no: int,
    lease: Lease,
    *,
    commit: bool = True,
) -> None:
    conversation = _conversation(db, conversation_id)
    if conversation.state != ConversationState.GENERATING:
        return
    attempt = _attempt(db, conversation.attempt_id)
    if attempt.state in TERMINAL_ATTEMPT_STATES:
        set_attempt_conversations_state(db, attempt.id, ConversationState.READ_ONLY)
        db.commit() if commit else db.flush()
        return
    if not conversation.runtime_conversation_id:
        conversation.state = ConversationState.FAILED
        conversation.state_version += 1
        db.commit() if commit else db.flush()
        return
    handle = RuntimeHandle(
        conversation.runtime_job_id or conversation.runtime_conversation_id,
        conversation.runtime_conversation_id,
        conversation.runtime_cursor,
    )
    db.rollback()
    runtime = get_runtime()
    batch = runtime.read_events(handle)
    result = batch.result or runtime.inspect(
        RuntimeHandle(handle.job_id, handle.conversation_id, batch.cursor or handle.cursor)
    )
    if not lease_is_current(db, lease):
        raise RuntimeError("task lease was lost during conversation polling")
    current = _conversation(db, conversation_id, lock=True)
    current_attempt = _attempt(db, current.attempt_id)
    if (
        current.state == ConversationState.READ_ONLY
        or current_attempt.state in TERMINAL_ATTEMPT_STATES
    ):
        db.rollback()
        return
    for event in batch.events:
        _append_runtime_payload(
            db, current, cursor=event.cursor, event_type=event.event_type, payload=event.payload
        )
    if result.cursor is None and batch.cursor is not None:
        result = RuntimeResult(
            status=result.status,
            outputs=result.outputs,
            human_question=result.human_question,
            cursor=batch.cursor,
            error=result.error,
        )
    _apply_conversation_result(
        db, current, result, message_id=f"poll:{batch.cursor or current.runtime_cursor or '0'}"
    )
    if result.status == "RUNNING":
        enqueue(
            db,
            task_type="POLL_CONVERSATION",
            aggregate_type="CONVERSATION",
            aggregate_id=current.id,
            idempotency_key=(
                f"poll-conversation:{current.id}:v{current.state_version}:{poll_no + 1}"
            ),
            payload={"poll_no": poll_no + 1},
            available_at=datetime.now(UTC) + timedelta(seconds=get_settings().runtime_poll_seconds),
        )
    else:
        _schedule_next_message(db, current)
    db.commit() if commit else db.flush()


def process_deliver_message(
    db: Session, message_id: str, lease: Lease, *, commit: bool = True
) -> None:
    message = db.get(AgentMessage, message_id)
    if message is None or message.delivery_state not in {
        DeliveryState.QUEUED,
        DeliveryState.DELIVERING,
    }:
        return
    conversation = _conversation(db, message.conversation_id)
    attempt = _attempt(db, conversation.attempt_id)
    if (
        attempt.state in TERMINAL_ATTEMPT_STATES
        or conversation.state == ConversationState.READ_ONLY
    ):
        message.delivery_state = DeliveryState.CANCELLED
        message.error_code = "ATTEMPT_TERMINAL"
        db.commit() if commit else db.flush()
        return
    if conversation.kind == ConversationKind.AUTO and attempt.state == "WAITING_HUMAN":
        message.delivery_state = DeliveryState.QUEUED
        if conversation.state != ConversationState.WAITING_HUMAN:
            conversation.state = ConversationState.WAITING_HUMAN
            conversation.state_version += 1
        db.commit() if commit else db.flush()
        return
    if not conversation.runtime_conversation_id:
        message.delivery_state = DeliveryState.FAILED
        message.error_code = "RUNTIME_CONVERSATION_UNAVAILABLE"
        conversation.state = ConversationState.FAILED
        db.commit() if commit else db.flush()
        return
    message.delivery_state = DeliveryState.DELIVERING
    current_conversation_id = conversation.id
    handle = RuntimeHandle(
        conversation.runtime_job_id or conversation.runtime_conversation_id,
        conversation.runtime_conversation_id,
        conversation.runtime_cursor,
    )
    content_json = dict(message.content_json)
    steering = message.delivery_mode == DeliveryMode.INTERRUPT_AND_RESUME
    db.flush()
    db.rollback()
    content, image_urls = _runtime_message_payload(content_json)
    try:
        result = (
            get_runtime().resume(handle, content, image_urls)
            if steering
            else get_runtime().send_message(handle, content, image_urls)
        )
    except DomainError as exc:
        failed = db.get(AgentMessage, message_id)
        current = _conversation(db, current_conversation_id, lock=True)
        if failed is not None:
            failed.delivery_state = DeliveryState.FAILED
            failed.error_code = exc.code
            failed.error_detail = exc.message
        current.state = ConversationState.FAILED
        current.state_version += 1
        _event(
            db,
            current,
            "AGENT_MESSAGE_DELIVERY_CHANGED",
            {"message_id": message_id, "delivery_state": DeliveryState.FAILED},
        )
        db.commit() if commit else db.flush()
        return
    if not lease_is_current(db, lease):
        raise RuntimeError("task lease was lost during message delivery")
    current = _conversation(db, current_conversation_id, lock=True)
    current_message = db.get(AgentMessage, message_id)
    current_attempt = _attempt(db, current.attempt_id)
    if (
        current_message is None
        or current.state == ConversationState.READ_ONLY
        or current_attempt.state in TERMINAL_ATTEMPT_STATES
    ):
        db.rollback()
        return
    current_message.delivery_state = DeliveryState.DELIVERED
    current_message.content_json = {**current_message.content_json, "presentation": "chat"}
    current_message.delivered_at = now()
    current_message.runtime_cursor = result.cursor
    current.runtime_cursor = result.cursor
    _event(
        db,
        current,
        "AGENT_MESSAGE_DELIVERY_CHANGED",
        {"message_id": message_id, "delivery_state": current_message.delivery_state},
    )
    if not steering or result.status != "RUNNING":
        _apply_conversation_result(db, current, result, message_id=message_id)
    if result.status == "RUNNING" and not steering:
        enqueue(
            db,
            task_type="POLL_CONVERSATION",
            aggregate_type="CONVERSATION",
            aggregate_id=current.id,
            idempotency_key=f"poll-conversation:{current.id}:v{current.state_version}:1",
            payload={"poll_no": 1},
            available_at=datetime.now(UTC) + timedelta(seconds=get_settings().runtime_poll_seconds),
        )
    else:
        _schedule_next_message(db, current)
    db.commit() if commit else db.flush()
