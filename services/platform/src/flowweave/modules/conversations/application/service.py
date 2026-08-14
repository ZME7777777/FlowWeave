from __future__ import annotations

import base64
import binascii
import copy
import hashlib
import json
import mimetypes
import os
import re
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Literal, cast
from urllib.parse import unquote, urlparse
from uuid import uuid4

from sqlalchemy import delete, func, select, update
from sqlalchemy.orm import Session

from flowweave.modules.catalog.public import resolve_snapshot_memory
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
from flowweave.modules.conversations.infrastructure.models import (
    AgentConversation,
    AgentMessage,
    RuntimeCondensation,
    RuntimeCondensationCommand,
    RuntimeConversationFork,
    RuntimeCriticEvaluation,
    RuntimeDiagnosticQuery,
    RuntimeGoalCommand,
    RuntimeGoalStatus,
    RuntimeSubagentTask,
    RuntimeSubagentTaskUsage,
)
from flowweave.modules.sandboxes import public as sandboxes
from flowweave.modules.tasks.public import Lease, enqueue, lease_is_current
from flowweave.runtime.base import (
    RuntimeEvent,
    RuntimeEventBatch,
    RuntimeHandle,
    RuntimeResult,
    RuntimeTaskUsageSnapshot,
    RuntimeUsageSnapshot,
)
from flowweave.runtime.dependencies import get_runtime
from flowweave.runtime.manifest import runtime_node
from flowweave.runtime.request import (
    build_runtime_request,
    frozen_memory_policy,
    resolve_runtime_provider,
    resolve_runtime_selection,
)
from flowweave.runtime.routing import runtime_for
from flowweave.runtime.workspace import materialize_runtime_memory
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
    ConversationAskAgentWrite,
    ConversationCondenseWrite,
    ConversationCreateWrite,
    ConversationForkWrite,
    ConversationGoalWrite,
    ConversationPatchWrite,
    ConversationReviseWrite,
    ConversationStopWrite,
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
_GOAL_USAGE_IDS = frozenset({"ask-agent-llm"})


def _usage_totals(
    usage: tuple[RuntimeUsageSnapshot, ...],
) -> tuple[float, int]:
    governed = tuple(item for item in usage if item.usage_id not in _GOAL_USAGE_IDS)
    return (
        sum(item.accumulated_cost for item in governed),
        sum(item.prompt_tokens + item.completion_tokens for item in governed),
    )


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


def conversation_sandbox_owner_is_active(
    db: Session,
    owner_id: str,
    sandbox_id: str,
    *,
    created_at: datetime,
    now_at: datetime,
    binding_grace_seconds: int,
) -> bool:
    """Report whether a human conversation still authorizes its Runtime sandbox."""

    item = db.get(AgentConversation, owner_id)
    shared_branch_active = (
        db.scalar(
            select(AgentConversation.id)
            .where(
                AgentConversation.runtime_sandbox_id == sandbox_id,
                AgentConversation.state.not_in(
                    [ConversationState.FAILED, ConversationState.READ_ONLY]
                ),
            )
            .limit(1)
        )
        is not None
    )
    provisioning = bool(
        item is not None
        and item.runtime_sandbox_id is None
        and item.state == ConversationState.CREATING
        and created_at + timedelta(seconds=binding_grace_seconds) > now_at
    )
    return bool(
        provisioning
        or shared_branch_active
        or (
            item is not None
            and item.runtime_sandbox_id == sandbox_id
            and item.state not in {ConversationState.FAILED, ConversationState.READ_ONLY}
        )
    )


def _runtime_sandbox_id(db: Session, item: AgentConversation) -> str | None:
    if item.runtime_sandbox_id is not None:
        return item.runtime_sandbox_id
    if item.kind == ConversationKind.AUTO:
        return _attempt(db, item.attempt_id).runtime_sandbox_id
    return None


def _runtime_sandbox_has_other_active_conversations(
    db: Session,
    sandbox_id: str | None,
    *,
    excluding_conversation_id: str,
) -> bool:
    if sandbox_id is None:
        return False
    return (
        db.scalar(
            select(AgentConversation.id)
            .where(
                AgentConversation.id != excluding_conversation_id,
                AgentConversation.runtime_sandbox_id == sandbox_id,
                AgentConversation.runtime_conversation_id.is_not(None),
                AgentConversation.state.not_in(
                    [ConversationState.FAILED, ConversationState.READ_ONLY]
                ),
            )
            .limit(1)
        )
        is not None
    )


def _runtime_sandbox_is_conversation_owned(db: Session, sandbox_id: str | None) -> bool:
    snapshot = sandboxes.sandbox_snapshot(db, sandbox_id)
    return snapshot is not None and snapshot.get("owner_type") == "CONVERSATION"


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


def _attempt_node(db: Session, attempt: NodeAttempt) -> dict[str, Any]:
    snapshot = db.get(RunSnapshot, attempt.snapshot_id)
    node_run, _ = _context(db, attempt)
    raw_nodes: object = snapshot.definition_json.get("nodes", []) if snapshot else []
    nodes = cast(list[dict[str, Any]], raw_nodes) if isinstance(raw_nodes, list) else []
    node = next(
        (item for item in nodes if item.get("instance_key") == node_run.flow_node_snapshot_key),
        None,
    )
    if node is None:
        raise DomainError("SNAPSHOT_NODE_MISSING", "Attempt node is unavailable", 409)
    return node


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


def _runtime_handle(item: AgentConversation) -> RuntimeHandle:
    if not item.runtime_conversation_id or not item.runtime_adapter:
        raise DomainError(
            "RUNTIME_CONVERSATION_UNAVAILABLE",
            "Conversation has no available Runtime",
            409,
        )
    return RuntimeHandle(
        item.runtime_job_id or item.runtime_conversation_id,
        item.runtime_conversation_id,
        item.runtime_cursor,
        runtime_resource_id=item.runtime_sandbox_id or "",
        runtime_resource_name=(
            (item.runtime_job_id or "").split(":", 1)[1]
            if ":" in (item.runtime_job_id or "")
            else ""
        ),
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
        "confirmation_policy": attempt.confirmation_policy,
        "condenser": copy.deepcopy(attempt.condenser_config_json or {"kind": "NO_OP"}),
        "context_policy": "ATTEMPT_FACTS_NO_CROSS_CONVERSATION_MESSAGES",
    }


def _message_text(content: dict[str, Any]) -> str:
    parts = content.get("parts", [])
    return "\n".join(str(part.get("text", "")) for part in parts if part.get("type") == "text")


def _runtime_message_payload(
    content: dict[str, Any], *, workspace_root: Path | None = None
) -> tuple[str, tuple[str, ...]]:
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
    skill_triggers: list[str] = []
    lowered_text = text.lower()
    for ref in refs:
        if str(ref.get("capability_type") or "") != "SKILL":
            continue
        trigger = f"${str(ref.get('capability_key') or '')}"
        if not re.search(
            rf"(?<![a-z0-9]){re.escape(trigger.lower())}(?![a-z0-9])",
            lowered_text,
        ):
            skill_triggers.append(trigger)
    if skill_triggers:
        # These are frozen KeywordTrigger values on the governed AgentSkills.
        # OpenHands records the activation on its MessageEvent; this is not a
        # FlowWeave prompt instruction or a private tool-control protocol.
        sections.append("\n".join(skill_triggers))
    mcp_refs = [
        str(ref.get("capability_key") or "")
        for ref in refs
        if str(ref.get("capability_type") or "") == "MCP"
    ]
    if mcp_refs:
        sections.append(
            "用户显式指定本条消息优先使用以下 MCP Server 的合适工具：\n"
            + "\n".join(f'- MCP "{name}"' for name in mcp_refs)
        )
    if text:
        sections.append(text)

    attachment_lines: list[str] = []
    image_urls: list[str] = []
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
        host_path = (workspace_root / storage_path).resolve() if workspace_root else None
        if (
            host_path is not None
            and workspace_root is not None
            and media_type in _WORKSPACE_IMAGE_TYPES
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


def _message_is_superseded(item: AgentMessage) -> bool:
    return item.content_json.get("superseded") is True


def _message_is_progress(item: AgentMessage) -> bool:
    return item.content_json.get("presentation") == "progress"


def _condensation_dict(item: RuntimeCondensation) -> dict[str, Any]:
    return {
        "id": item.id,
        "attempt_id": item.attempt_id,
        "conversation_id": item.conversation_id,
        "runtime_event_id": item.runtime_event_id,
        "runtime_cursor": item.runtime_cursor,
        "event_type": item.event_type,
        "forgotten_event_ids": item.forgotten_event_ids_json,
        "summary": item.summary,
        "summary_offset": item.summary_offset,
        "llm_response_id": item.llm_response_id,
        "created_at": item.created_at.isoformat(),
    }


def _condensation_command_dict(item: RuntimeCondensationCommand) -> dict[str, Any]:
    return {
        "id": item.id,
        "attempt_id": item.attempt_id,
        "conversation_id": item.conversation_id,
        "runtime_conversation_id": item.runtime_conversation_id,
        "baseline_cursor": item.baseline_cursor,
        "request_event_id": item.request_event_id,
        "completion_event_id": item.completion_event_id,
        "state": item.state,
        "state_version": item.state_version,
        "error_code": item.error_code,
        "error_detail": item.error_detail,
        "created_at": item.created_at.isoformat(),
        "started_at": item.started_at.isoformat() if item.started_at else None,
        "completed_at": item.completed_at.isoformat() if item.completed_at else None,
    }


def project_runtime_condensation(
    db: Session,
    conversation: AgentConversation,
    *,
    cursor: str,
    event_type: str,
    payload: dict[str, Any],
) -> RuntimeCondensation:
    existing = db.scalar(
        select(RuntimeCondensation).where(
            RuntimeCondensation.conversation_id == conversation.id,
            RuntimeCondensation.runtime_event_id == cursor,
        )
    )
    if existing is not None:
        return existing
    completed = event_type == "CONDENSATION_COMPLETED"
    raw_ids = payload.get("forgotten_event_ids") if completed else None
    forgotten_event_ids = (
        sorted(
            {
                value[:200]
                for value in cast(list[object], raw_ids)[:10_000]
                if isinstance(value, str)
            }
        )
        if isinstance(raw_ids, list)
        else []
    )
    raw_summary = payload.get("summary") if completed else None
    summary = str(raw_summary)[:200_000] if raw_summary is not None else None
    raw_offset = payload.get("summary_offset") if completed else None
    summary_offset = raw_offset if isinstance(raw_offset, int) and raw_offset >= 0 else None
    raw_response_id = payload.get("llm_response_id") if completed else None
    item = RuntimeCondensation(
        attempt_id=conversation.attempt_id,
        conversation_id=conversation.id,
        runtime_event_id=cursor,
        runtime_cursor=cursor,
        event_type=("COMPLETED" if completed else "REQUESTED"),
        forgotten_event_ids_json=forgotten_event_ids,
        summary=summary,
        summary_offset=summary_offset,
        llm_response_id=(str(raw_response_id)[:200] if raw_response_id is not None else None),
    )
    db.add(item)
    db.flush()
    conversation.runtime_cursor = cursor
    _event(
        db,
        conversation,
        "CONVERSATION_CONDENSATION_" + item.event_type,
        {
            "runtime_event_id": cursor,
            "forgotten_event_count": len(forgotten_event_ids),
            "has_summary": summary is not None,
            "llm_response_id": item.llm_response_id,
        },
    )
    return item


def _conversation_dict(db: Session, item: AgentConversation) -> dict[str, Any]:
    active_messages = [
        message
        for message in db.scalars(
            select(AgentMessage)
            .where(AgentMessage.conversation_id == item.id)
            .order_by(AgentMessage.sequence_no)
        )
        if not _message_is_superseded(message)
    ]
    last = active_messages[-1] if active_messages else None
    count = len(active_messages)
    connection = _connection_status(db, item)
    runtime_resource = _runtime_resource(db, item)
    runtime_selection = cast(
        dict[str, Any], item.context_baseline_json.get("runtime_selection") or {}
    )
    latest_goal = db.scalar(
        select(RuntimeGoalStatus)
        .where(RuntimeGoalStatus.conversation_id == item.id)
        .order_by(RuntimeGoalStatus.created_at.desc(), RuntimeGoalStatus.id.desc())
        .limit(1)
    )
    critic_evaluations = list(
        db.scalars(
            select(RuntimeCriticEvaluation)
            .where(RuntimeCriticEvaluation.conversation_id == item.id)
            .order_by(RuntimeCriticEvaluation.created_at, RuntimeCriticEvaluation.id)
        )
    )
    return {
        "id": item.id,
        "attempt_id": item.attempt_id,
        "conversation_no": item.conversation_no,
        "kind": item.kind,
        "title": item.title,
        "state": item.state,
        "state_version": item.state_version,
        "model_name": runtime_selection.get("model_name"),
        "reasoning_effort": runtime_selection.get("reasoning_effort"),
        "runtime_adapter": item.runtime_adapter,
        "runtime_job_id": item.runtime_job_id,
        "runtime_conversation_id": item.runtime_conversation_id,
        "fork_kind": item.fork_kind,
        "source_conversation_id": item.source_conversation_id,
        "source_runtime_conversation_id": item.source_runtime_conversation_id,
        "source_runtime_event_id": item.source_runtime_event_id,
        "runtime_branch_metadata": item.runtime_branch_metadata_json,
        "metrics_reset": item.metrics_reset,
        "runtime_resource": runtime_resource,
        "connection_status": connection,
        "context_baseline": item.context_baseline_json,
        "editable_message_id": cast(
            dict[str, Any], item.context_baseline_json.get("stopped_turn") or {}
        ).get("editable_message_id"),
        "message_count": count,
        "last_message": _message_dict(last) if last else None,
        "runtime_condensations": [
            _condensation_dict(condensation)
            for condensation in db.scalars(
                select(RuntimeCondensation)
                .where(RuntimeCondensation.conversation_id == item.id)
                .order_by(RuntimeCondensation.created_at, RuntimeCondensation.id)
            )
        ],
        "runtime_condensation_commands": [
            _condensation_command_dict(command)
            for command in db.scalars(
                select(RuntimeCondensationCommand)
                .where(RuntimeCondensationCommand.conversation_id == item.id)
                .order_by(
                    RuntimeCondensationCommand.created_at,
                    RuntimeCondensationCommand.id,
                )
            )
        ],
        "latest_goal_status": (
            {
                "runtime_event_id": latest_goal.runtime_event_id,
                "active": latest_goal.active,
                "status": latest_goal.status,
                "iteration": latest_goal.iteration,
                "max_iterations": latest_goal.max_iterations,
                "objective": latest_goal.objective,
                "verdict": latest_goal.verdict_json,
            }
            if latest_goal is not None
            else None
        ),
        "critic_evaluations": [
            {
                "runtime_event_id": evaluation.runtime_event_id,
                "source_type": evaluation.source_type,
                "score": float(evaluation.score),
                "message": evaluation.message,
                "created_at": evaluation.created_at.isoformat(),
            }
            for evaluation in critic_evaluations
        ],
        "created_at": item.created_at.isoformat(),
        "updated_at": item.updated_at.isoformat(),
    }


def _runtime_resource(db: Session, item: AgentConversation) -> dict[str, Any] | None:
    """Expose the durable Docker binding so operators can trace and audit cleanup."""

    sandbox_id = item.runtime_sandbox_id
    if sandbox_id is None and item.kind == ConversationKind.AUTO:
        sandbox_id = _attempt(db, item.attempt_id).runtime_sandbox_id
    sandbox = sandboxes.sandbox_snapshot(db, sandbox_id)
    if sandbox is None:
        return None
    if sandbox["observed_state"] == "DELETED":
        lifecycle = "DELETED"
    elif sandbox["desired_state"] == "DELETED":
        lifecycle = "DELETING"
    elif sandbox["observed_state"] == "ERROR":
        lifecycle = "ERROR"
    else:
        lifecycle = "RUNNING"
    return {
        "sandbox_id": sandbox["id"],
        "container_name": sandbox["backend_resource_name"],
        "owner_type": sandbox["owner_type"],
        "owner_id": sandbox["owner_id"],
        "desired_state": sandbox["desired_state"],
        "observed_state": sandbox["observed_state"],
        "lifecycle": lifecycle,
        "cleanup_policy": (
            "DELETE_WITH_CONVERSATION"
            if sandbox.get("owner_type") == "CONVERSATION"
            else "DELETE_WITH_ATTEMPT"
        ),
    }


def _connection_status(db: Session, item: AgentConversation) -> dict[str, Any]:
    """Derive a crash-safe connection phase from durable task and sandbox state."""

    if item.state == ConversationState.FAILED:
        return {"phase": "FAILED", "started_at": item.updated_at.isoformat()}
    if item.state != ConversationState.CREATING:
        return {"phase": "READY", "started_at": item.updated_at.isoformat()}

    automatic = item.kind == ConversationKind.AUTO
    owner_type = "ATTEMPT" if automatic else "CONVERSATION"
    owner_id = item.attempt_id if automatic else item.id
    task_type = "START_RUNTIME" if automatic else "CREATE_CONVERSATION"
    sandbox = sandboxes.latest_runtime_sandbox_snapshot(
        db, owner_type=owner_type, owner_id=owner_id
    )
    task = db.scalar(
        select(BackgroundTask)
        .where(
            BackgroundTask.task_type == task_type,
            BackgroundTask.aggregate_id == owner_id,
        )
        .order_by(BackgroundTask.created_at.desc())
        .limit(1)
    )
    if sandbox is not None:
        if sandbox["observed_state"] == "ERROR":
            phase = "FAILED"
        elif sandbox["observed_state"] in {"PENDING", "CREATING"}:
            phase = "STARTING_RUNTIME"
        else:
            phase = "CONNECTING_AGENT"
        started_at = cast(datetime, sandbox["created_at"])
        detail = sandbox["last_error_detail"]
    elif task is not None and task.state in {TaskState.PENDING, TaskState.RETRY}:
        phase = "WAITING_WORKER"
        started_at = task.available_at
        detail = task.last_error
    else:
        phase = "PREPARING_CONTEXT"
        started_at = task.updated_at if task is not None else item.updated_at
        detail = task.last_error if task is not None else None
    elapsed = max(0, int((datetime.now(UTC) - started_at).total_seconds()))
    return {
        "phase": phase,
        "started_at": started_at.isoformat(),
        "elapsed_seconds": elapsed,
        "detail": detail,
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


def terminal_resource_details(db: Session, conversation_id: str) -> tuple[str, str, str]:
    """Return the Runtime resource name and its ownership identifiers."""

    item = _conversation(db, conversation_id)
    resource_name = terminal_container_id(db, conversation_id)
    sandbox_id = item.runtime_sandbox_id
    if not sandbox_id and item.kind == ConversationKind.AUTO:
        # AUTO conversations share the Attempt Runtime. Older/in-flight rows
        # may predate projection of the sandbox ledger ID onto the
        # conversation, so resolve it from the authoritative Attempt binding.
        sandbox_id = _attempt(db, item.attempt_id).runtime_sandbox_id
    if not sandbox_id:
        raise DomainError(
            "AGENT_TERMINAL_UNAVAILABLE",
            "This Agent conversation has no managed Runtime sandbox",
            409,
        )
    sandbox = sandboxes.sandbox_snapshot(db, sandbox_id)
    raw_spec = sandbox.get("spec") if sandbox is not None else None
    spec = cast(dict[str, Any], raw_spec) if isinstance(raw_spec, dict) else {}
    environment_id = str(spec.get("environment_id") or "")
    if not environment_id:
        raise DomainError(
            "AGENT_TERMINAL_UNAVAILABLE",
            "This Agent Runtime has no terminal environment binding",
            409,
        )
    return resource_name, sandbox_id, environment_id


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
    maximum = (
        db.scalar(
            select(func.max(AgentConversation.conversation_no)).where(
                AgentConversation.attempt_id == attempt.id
            )
        )
        or 0
    )
    selected_model, selected_effort = resolve_runtime_selection(
        db, _attempt_node(db, attempt), attempt.model_name, attempt.reasoning_effort
    )
    baseline = _baseline(db, attempt)
    baseline["runtime_selection"] = {
        "model_name": selected_model,
        "reasoning_effort": selected_effort,
    }
    item = AgentConversation(
        attempt_id=attempt.id,
        conversation_no=int(maximum) + 1,
        kind=ConversationKind.AUTO,
        title=f"自动执行 · Attempt {attempt.attempt_no}",
        state=ConversationState.CREATING,
        context_baseline_json=baseline,
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
    runtime_sandbox_id: str | None = None,
) -> None:
    attempt = _attempt(db, attempt_id)
    item = ensure_auto_conversation(db, attempt)
    item.runtime_job_id = runtime_job_id
    item.runtime_conversation_id = runtime_conversation_id
    item.runtime_cursor = runtime_cursor
    item.runtime_adapter = runtime_adapter
    item.runtime_sandbox_id = runtime_sandbox_id
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
    if event_type in {"CONDENSATION_REQUESTED", "CONDENSATION_COMPLETED"}:
        project_runtime_condensation(
            db, conversation, cursor=cursor, event_type=event_type, payload=payload
        )
        return
    if event_type in {"TOOL", "TOOL_CALL", "TOOL_RESULT"}:
        _project_runtime_subagent_task(db, conversation, cursor=cursor, payload=payload)
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


def project_runtime_conversation_event(
    db: Session,
    conversation_id: str,
    *,
    cursor: str,
    event_type: str,
    payload: dict[str, Any],
) -> None:
    """Project a formal Runtime event onto its exact durable conversation.

    Cancellation recovery can cover AUTO and human-created conversations owned by
    the same Attempt. The caller supplies the database conversation identity
    resolved from the Runtime handle; no event ordering or display name is used.
    """

    conversation = _conversation(db, conversation_id)
    if event_type in {"CONDENSATION_REQUESTED", "CONDENSATION_COMPLETED"}:
        project_runtime_condensation(
            db, conversation, cursor=cursor, event_type=event_type, payload=payload
        )
        return
    if event_type in {"TOOL", "TOOL_CALL", "TOOL_RESULT"}:
        _project_runtime_subagent_task(db, conversation, cursor=cursor, payload=payload)
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


def record_auto_human_input(
    db: Session,
    attempt_id: str,
    *,
    action_id: str,
    content: str,
    runtime_selection: dict[str, Any] | None = None,
) -> None:
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
            content={
                "parts": [{"type": "text", "text": content}],
                "runtime_selection": runtime_selection or {},
            },
            delivery_state=DeliveryState.QUEUED,
            runtime_event_id=runtime_event_id,
        )
    if runtime_selection:
        baseline = copy.deepcopy(conversation.context_baseline_json or {})
        baseline["runtime_selection"] = runtime_selection
        conversation.context_baseline_json = baseline
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
            _ensure_conversation_runtime_cleanup(db, item)


def _ensure_conversation_runtime_cleanup(db: Session, item: AgentConversation) -> bool:
    """Ensure terminal conversation compute has durable cleanup work."""

    if item.kind == ConversationKind.AUTO or not item.runtime_conversation_id:
        return False
    key = f"cleanup-conversation-runtime:{item.id}:{item.runtime_conversation_id}"
    existing = db.scalar(select(BackgroundTask).where(BackgroundTask.idempotency_key == key))
    if existing is None:
        task = enqueue(
            db,
            task_type="CLEANUP_CONVERSATION_RUNTIME",
            aggregate_type="CONVERSATION",
            aggregate_id=item.id,
            idempotency_key=key,
        )
        task.max_attempts = max(task.max_attempts, 20)
        return True
    existing.max_attempts = max(existing.max_attempts, 20)
    if existing.state in {TaskState.PENDING, TaskState.RETRY, TaskState.RUNNING}:
        return False
    existing.state = TaskState.RETRY
    existing.available_at = datetime.now(UTC)
    existing.lease_owner = None
    existing.lease_until = None
    existing.last_error = "RUNTIME_CLEANUP_RECOVERY"
    return True


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
            .where(
                AgentConversation.attempt_id == attempt_id,
                AgentConversation.kind != "SUBAGENT",
            )
            .order_by(AgentConversation.conversation_no)
        )
    )
    return [_conversation_dict(db, item) for item in rows]


def list_subagents(db: Session, conversation_id: str) -> list[dict[str, Any]]:
    _conversation(db, conversation_id)
    rows = list(
        db.scalars(
            select(RuntimeSubagentTask)
            .where(RuntimeSubagentTask.conversation_id == conversation_id)
            .order_by(RuntimeSubagentTask.created_at, RuntimeSubagentTask.id)
        )
    )
    usage_by_task = {
        item.runtime_subagent_task_id: item
        for item in db.scalars(
            select(RuntimeSubagentTaskUsage).where(
                RuntimeSubagentTaskUsage.conversation_id == conversation_id
            )
        )
    }
    return [_subagent_task_dict(item, usage_by_task.get(item.id)) for item in rows]


def pending_runtime_task_control_facts(
    db: Session, attempt_id: str
) -> tuple[dict[str, str | None], ...]:
    """Return redacted formal identities for Task invocations without an Observation."""

    return tuple(
        {
            "runtime_subagent_task_id": item.id,
            "conversation_id": item.conversation_id,
            "action_event_id": item.action_event_id,
            "tool_call_id": item.tool_call_id,
        }
        for item in db.scalars(
            select(RuntimeSubagentTask)
            .where(
                RuntimeSubagentTask.attempt_id == attempt_id,
                RuntimeSubagentTask.state == "REQUESTED",
            )
            .order_by(RuntimeSubagentTask.created_at, RuntimeSubagentTask.id)
        )
    )


def _subagent_task_dict(
    item: RuntimeSubagentTask, usage: RuntimeSubagentTaskUsage | None
) -> dict[str, Any]:
    return {
        "id": item.id,
        "attempt_id": item.attempt_id,
        "conversation_id": item.conversation_id,
        "action_event_id": item.action_event_id,
        "action_cursor": item.action_cursor,
        "tool_call_id": item.tool_call_id,
        "llm_response_id": item.llm_response_id,
        "observation_event_id": item.observation_event_id,
        "observation_cursor": item.observation_cursor,
        "runtime_task_id": item.runtime_task_id,
        "subagent_type": item.subagent_type,
        "description": item.description,
        "resume_task_id": item.resume_task_id,
        "state": item.state,
        "native_status": item.native_status,
        "result": item.result,
        "error_detail": item.error_detail,
        "usage": _subagent_usage_dict(usage) if usage is not None else None,
        "created_at": item.created_at.isoformat(),
        "completed_at": item.completed_at.isoformat() if item.completed_at else None,
        "updated_at": item.updated_at.isoformat(),
    }


def _subagent_usage_dict(item: RuntimeSubagentTaskUsage) -> dict[str, Any]:
    return {
        "runtime_task_id": item.runtime_task_id,
        "source_cursor": item.source_cursor,
        "snapshot_digest": item.snapshot_digest,
        "usage_version": item.usage_version,
        "model_name": item.model_name,
        "accumulated_cost_usd": float(item.accumulated_cost_usd),
        "prompt_tokens": item.prompt_tokens,
        "completion_tokens": item.completion_tokens,
        "cache_read_tokens": item.cache_read_tokens,
        "cache_write_tokens": item.cache_write_tokens,
        "reasoning_tokens": item.reasoning_tokens,
        "context_window": item.context_window,
        "per_turn_tokens": item.per_turn_tokens,
        "budget_limit_usd": (
            float(item.budget_limit_usd) if item.budget_limit_usd is not None else None
        ),
        "budget_state": item.budget_state,
        "budget_exceeded_at": (
            item.budget_exceeded_at.isoformat() if item.budget_exceeded_at else None
        ),
        "updated_at": item.updated_at.isoformat(),
    }


def get_conversation(db: Session, conversation_id: str) -> dict[str, Any]:
    return _conversation_dict(db, _conversation(db, conversation_id))


def runtime_stream_details(db: Session, conversation_id: str) -> tuple[str | None, RuntimeHandle]:
    item = _conversation(db, conversation_id)
    if not item.runtime_conversation_id:
        raise DomainError(
            "AGENT_STREAM_UNAVAILABLE",
            "This Agent conversation is not connected to a Runtime",
            409,
        )
    sandbox_id = item.runtime_sandbox_id
    if not sandbox_id and item.kind == ConversationKind.AUTO:
        sandbox_id = _attempt(db, item.attempt_id).runtime_sandbox_id
    sandbox = sandboxes.sandbox_snapshot(db, sandbox_id)
    resource_id = str(sandbox.get("id") or "") if sandbox is not None else ""
    resource_name = str(sandbox.get("backend_resource_name") or "") if sandbox is not None else ""
    return item.runtime_adapter, RuntimeHandle(
        item.runtime_job_id or item.runtime_conversation_id,
        item.runtime_conversation_id,
        item.runtime_cursor,
        resource_id,
        resource_name,
    )


def _fork_message_text(message: AgentMessage) -> str:
    return "\n".join(
        str(part.get("text") or "")
        for raw in cast(list[object], message.content_json.get("parts") or [])
        if isinstance(raw, dict)
        for part in [cast(dict[str, Any], raw)]
        if part.get("type") == "text" and part.get("text")
    ).strip()


def _fork_runtime_history(messages: list[AgentMessage]) -> tuple[dict[str, str], ...]:
    history: list[dict[str, str]] = []
    for message in messages:
        if (
            message.source not in {MessageSource.HUMAN, MessageSource.AGENT}
            or message.message_type != MessageType.TEXT
            or message.delivery_state == DeliveryState.CANCELLED
            or _message_is_superseded(message)
            or _message_is_progress(message)
        ):
            continue
        text = _fork_message_text(message)
        if text:
            history.append(
                {
                    "role": "user" if message.source == MessageSource.HUMAN else "assistant",
                    "content": text,
                }
            )
    return tuple(history)


def fork_conversation(
    db: Session,
    message_id: str,
    payload: ConversationForkWrite,
    idempotency_key: str,
) -> dict[str, Any]:
    """Create an explicitly selected native or lossy semantic fork."""

    existing_action = db.scalar(
        select(HumanAction).where(HumanAction.idempotency_key == idempotency_key)
    )
    if existing_action is not None:
        return get_conversation(db, str(existing_action.payload_json.get("conversation_id") or ""))
    source_message = db.get(AgentMessage, message_id)
    if source_message is None:
        raise not_found("agent_message", message_id)
    source = _conversation(db, source_message.conversation_id, lock=True)
    if (
        source_message.source != MessageSource.AGENT
        or source_message.message_type != MessageType.TEXT
        or _message_is_superseded(source_message)
        or _message_is_progress(source_message)
    ):
        raise DomainError(
            "MESSAGE_NOT_FORKABLE",
            "Only an active Agent text reply can be forked",
            409,
        )
    if source.state != ConversationState.IDLE:
        raise DomainError(
            "CONVERSATION_NOT_IDLE",
            "Native Runtime fork requires an idle source conversation",
            409,
        )
    if source.state_version != payload.expected_conversation_version:
        raise conflict(
            "conversation was modified",
            expected=payload.expected_conversation_version,
            actual=source.state_version,
        )
    attempt = db.scalar(
        select(NodeAttempt).where(NodeAttempt.id == source.attempt_id).with_for_update()
    )
    if attempt is None:
        raise not_found("node_attempt", source.attempt_id)
    if attempt.state in TERMINAL_ATTEMPT_STATES:
        raise DomainError("ATTEMPT_TERMINAL", "Terminal attempt cannot fork conversations", 409)
    if attempt.state not in CONVERSATION_ENABLED_ATTEMPT_STATES:
        raise DomainError("ATTEMPT_NOT_STARTED", "Attempt must be active before forking", 409)
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
    baseline = _baseline(db, attempt)
    node_run, run_id = _context(db, attempt)
    if payload.fork_kind == "SEMANTIC":
        source_messages = list(
            db.scalars(
                select(AgentMessage)
                .where(
                    AgentMessage.conversation_id == source.id,
                    AgentMessage.sequence_no <= source_message.sequence_no,
                )
                .order_by(AgentMessage.sequence_no)
            )
        )
        history = _fork_runtime_history(source_messages)
        losses = [
            "tool_and_observation_state",
            "agent_state",
            "activated_skills",
            "condensation_state",
            "usage_stats",
            "runtime_head",
        ]
        baseline["semantic_fork"] = {
            "schema_version": 1,
            "source_conversation_id": source.id,
            "source_message_id": source_message.id,
            "history": list(history),
            "losses": losses,
            "explicitly_acknowledged": True,
        }
        item = AgentConversation(
            attempt_id=attempt.id,
            conversation_no=number,
            kind=ConversationKind.HUMAN_CREATED,
            title=payload.title or f"语义分支 · {source.title}",
            state=ConversationState.CREATING,
            fork_kind="SEMANTIC",
            source_conversation_id=source.id,
            source_runtime_conversation_id=source.runtime_conversation_id,
            source_runtime_event_id=source_message.runtime_cursor,
            runtime_branch_metadata_json={
                "scope": payload.fork_scope,
                "losses": losses,
                "explicitly_acknowledged": True,
            },
            metrics_reset=True,
            context_baseline_json=baseline,
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
                        "text": "已创建显式语义分支；仅复制可见文本，不继承 Runtime 状态。",
                    }
                ],
                "semantic_fork": {"losses": losses, "explicitly_acknowledged": True},
            },
            delivery_state=DeliveryState.DELIVERED,
        )
        db.add(
            HumanAction(
                flow_run_id=run_id,
                node_run_id=node_run.id,
                attempt_id=attempt.id,
                action_type="SEMANTIC_FORK_AGENT_CONVERSATION",
                idempotency_key=idempotency_key,
                payload_json={
                    "conversation_id": item.id,
                    "source_conversation_id": source.id,
                    "source_message_id": source_message.id,
                    "losses": losses,
                },
            )
        )
        enqueue(
            db,
            task_type="CREATE_CONVERSATION",
            aggregate_type="CONVERSATION",
            aggregate_id=item.id,
            idempotency_key=f"create-semantic-conversation:{item.id}:v1",
        )
        _event(
            db,
            item,
            "SEMANTIC_CONVERSATION_FORK_CREATED",
            {"source_message_id": source_message.id, "losses": losses},
        )
        finish(db)
        return _conversation_dict(db, item)

    if not source.runtime_conversation_id or not source.runtime_adapter:
        raise DomainError(
            "RUNTIME_CONVERSATION_UNAVAILABLE",
            "Native Runtime fork requires an available source Runtime; "
            "select SEMANTIC explicitly to degrade",
            409,
        )
    if not source.runtime_cursor:
        raise DomainError(
            "RUNTIME_HEAD_IDENTITY_UNAVAILABLE",
            "Native Runtime fork requires a frozen source HEAD identity",
            409,
        )
    if payload.fork_scope == "MESSAGE" and not source_message.runtime_cursor:
        raise DomainError(
            "RUNTIME_EVENT_IDENTITY_UNAVAILABLE",
            "The selected reply has no formal Runtime event identity",
            409,
        )
    from_event_id = source_message.runtime_cursor if payload.fork_scope == "MESSAGE" else None
    baseline["runtime_fork"] = {
        "kind": "RUNTIME",
        "source_conversation_id": source.id,
        "source_message_id": source_message.id,
        "source_runtime_conversation_id": source.runtime_conversation_id,
        "source_runtime_event_id": from_event_id,
        "source_runtime_head_event_id": source.runtime_cursor,
        "scope": payload.fork_scope,
        "metrics_reset": payload.reset_metrics,
    }
    source_sandbox_id = _runtime_sandbox_id(db, source)
    target_runtime_id = str(uuid4())
    item = AgentConversation(
        attempt_id=attempt.id,
        conversation_no=number,
        kind=ConversationKind.HUMAN_CREATED,
        title=payload.title or f"分支 · {source.title}",
        state=ConversationState.CREATING,
        runtime_adapter=source.runtime_adapter,
        runtime_job_id=source.runtime_job_id,
        runtime_sandbox_id=source_sandbox_id,
        fork_kind="RUNTIME",
        source_conversation_id=source.id,
        source_runtime_conversation_id=source.runtime_conversation_id,
        source_runtime_event_id=from_event_id,
        runtime_branch_metadata_json={
            "target_runtime_conversation_id": target_runtime_id,
            "scope": payload.fork_scope,
            "source_runtime_head_event_id": source.runtime_cursor,
        },
        metrics_reset=payload.reset_metrics,
        context_baseline_json=baseline,
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
            "parts": [{"type": "text", "text": "已从既有会话创建上下文分支。"}],
            "runtime_fork": baseline["runtime_fork"],
        },
        delivery_state=DeliveryState.DELIVERING,
    )
    command = RuntimeConversationFork(
        attempt_id=attempt.id,
        source_conversation_id=source.id,
        target_conversation_id=item.id,
        runtime_adapter=source.runtime_adapter,
        runtime_job_id=source.runtime_job_id or source.runtime_conversation_id,
        runtime_sandbox_id=source_sandbox_id,
        source_runtime_conversation_id=source.runtime_conversation_id,
        target_runtime_conversation_id=target_runtime_id,
        requested_from_event_id=from_event_id,
        source_head_event_id=source.runtime_cursor,
        reset_metrics=payload.reset_metrics,
        source_state_version=source.state_version + 1,
        idempotency_key=idempotency_key,
    )
    db.add(command)
    source.state = ConversationState.FORKING
    source.state_version += 1
    db.add(
        HumanAction(
            flow_run_id=run_id,
            node_run_id=node_run.id,
            attempt_id=attempt.id,
            action_type="FORK_AGENT_CONVERSATION",
            idempotency_key=idempotency_key,
            payload_json={
                "conversation_id": item.id,
                "source_conversation_id": source.id,
                "source_message_id": source_message.id,
                "runtime_fork_id": command.id,
            },
        )
    )
    enqueue(
        db,
        task_type="FORK_CONVERSATION",
        aggregate_type="RUNTIME_CONVERSATION_FORK",
        aggregate_id=command.id,
        idempotency_key=f"fork-conversation:{command.id}:v1",
    )
    _event(
        db,
        item,
        "CONVERSATION_CREATED",
        {
            "kind": item.kind,
            "fork_kind": "RUNTIME",
            "forked_from_message_id": source_message.id,
        },
    )
    finish(db)
    return _conversation_dict(db, item)


def process_fork_conversation(
    db: Session, fork_id: str, lease: Lease, *, commit: bool = True
) -> None:
    command = db.scalar(
        select(RuntimeConversationFork)
        .where(RuntimeConversationFork.id == fork_id)
        .with_for_update()
    )
    if command is None or command.state != "PENDING":
        return
    source = (
        _conversation(db, command.source_conversation_id)
        if command.source_conversation_id
        else None
    )
    target = _conversation(db, command.target_conversation_id)
    if (
        source is None
        or source.state != ConversationState.FORKING
        or source.state_version != command.source_state_version
        or source.runtime_conversation_id != command.source_runtime_conversation_id
        or _runtime_sandbox_id(db, source) != command.runtime_sandbox_id
        or target.state != ConversationState.CREATING
    ):
        raise DomainError(
            "RUNTIME_FORK_IDENTITY_DRIFT",
            "Native Runtime fork source identity changed",
            409,
        )
    expected_target_version = target.state_version
    handle = RuntimeHandle(
        command.runtime_job_id,
        command.source_runtime_conversation_id,
        source.runtime_cursor,
    )
    adapter = command.runtime_adapter
    target_title = target.title
    db.rollback()
    result = runtime_for(adapter, handle).fork_conversation(
        handle,
        target_conversation_id=command.target_runtime_conversation_id,
        title=target_title,
        from_event_id=command.requested_from_event_id,
        expected_source_leaf_event_id=command.source_head_event_id,
        reset_metrics=command.reset_metrics,
    )
    if not lease_is_current(db, lease):
        raise RuntimeError("task lease was lost during native Runtime fork")
    command = db.scalar(
        select(RuntimeConversationFork)
        .where(RuntimeConversationFork.id == fork_id)
        .with_for_update()
    )
    if command is None or command.state != "PENDING":
        return
    source = _conversation(db, command.source_conversation_id or "", lock=True)
    target = _conversation(db, command.target_conversation_id, lock=True)
    if (
        source.state != ConversationState.FORKING
        or source.state_version != command.source_state_version
        or target.state != ConversationState.CREATING
        or target.state_version != expected_target_version
    ):
        raise DomainError(
            "RUNTIME_FORK_IDENTITY_DRIFT",
            "Native Runtime fork control-plane identity changed",
            409,
        )
    target.runtime_conversation_id = result.handle.conversation_id
    target.runtime_cursor = result.leaf_event_id
    target.runtime_branch_metadata_json = {
        **target.runtime_branch_metadata_json,
        "forked_from_conversation_id": result.source_conversation_id,
        "forked_from_event_id": result.source_event_id,
        "source_head_event_id": command.source_head_event_id,
        "leaf_event_id": result.leaf_event_id,
    }
    target.state = ConversationState.IDLE
    target.state_version += 1
    source.state = ConversationState.IDLE
    source.state_version += 1
    command.resolved_source_event_id = result.source_event_id
    command.fork_leaf_event_id = result.leaf_event_id
    command.state = "SUCCEEDED"
    command.state_version += 1
    command.completed_at = now()
    program = db.scalar(
        select(AgentMessage)
        .where(
            AgentMessage.conversation_id == target.id,
            AgentMessage.source == MessageSource.PROGRAM,
        )
        .order_by(AgentMessage.sequence_no)
        .limit(1)
    )
    if program is not None:
        program.delivery_state = DeliveryState.DELIVERED
        program.delivered_at = now()
    _event(
        db,
        target,
        "RUNTIME_CONVERSATION_FORK_CREATED",
        {
            "runtime_fork_id": command.id,
            "source_runtime_conversation_id": result.source_conversation_id,
            "source_runtime_event_id": result.source_event_id,
            "target_runtime_conversation_id": result.handle.conversation_id,
            "leaf_event_id": result.leaf_event_id,
            "metrics_reset": command.reset_metrics,
        },
    )
    db.commit() if commit else db.flush()


def record_fork_conversation_failure(
    db: Session, fork_id: str, error: str, *, terminal: bool
) -> None:
    if not terminal:
        return
    command = db.scalar(
        select(RuntimeConversationFork)
        .where(RuntimeConversationFork.id == fork_id)
        .with_for_update()
    )
    if command is None or command.state != "PENDING":
        return
    detail = error[:2000]
    command.state = "FAILED"
    command.state_version += 1
    command.error_code = "RUNTIME_FORK_FAILED"
    command.error_detail = detail
    command.completed_at = now()
    target = _conversation(db, command.target_conversation_id, lock=True)
    target.state = ConversationState.FAILED
    target.state_version += 1
    target.runtime_conversation_id = (
        target.runtime_conversation_id or command.target_runtime_conversation_id
    )
    source = (
        _conversation(db, command.source_conversation_id, lock=True)
        if command.source_conversation_id
        else None
    )
    if (
        source is not None
        and source.state == ConversationState.FORKING
        and source.state_version == command.source_state_version
    ):
        source.state = ConversationState.IDLE
        source.state_version += 1
    _event(
        db,
        target,
        "RUNTIME_CONVERSATION_FORK_FAILED",
        {"runtime_fork_id": command.id, "error": detail},
    )
    cleanup = enqueue(
        db,
        task_type="CLEANUP_CONVERSATION_RUNTIME",
        aggregate_type="CONVERSATION",
        aggregate_id=target.id,
        idempotency_key=f"cleanup-failed-runtime-fork:{command.id}",
    )
    cleanup.max_attempts = max(cleanup.max_attempts, 10)
    db.flush()


def request_conversation_condensation(
    db: Session,
    conversation_id: str,
    payload: ConversationCondenseWrite,
    idempotency_key: str,
    actor: str | None,
) -> dict[str, Any]:
    """Freeze a governed native condensation request before Runtime I/O."""

    duplicate = db.scalar(
        select(RuntimeCondensationCommand).where(
            RuntimeCondensationCommand.idempotency_key == idempotency_key
        )
    )
    if duplicate is not None:
        if duplicate.conversation_id != conversation_id:
            raise conflict(
                "condensation idempotency key is already used",
                condensation_command_id=duplicate.id,
            )
        return _condensation_command_dict(duplicate)

    conversation = _conversation(db, conversation_id, lock=True)
    if conversation.state_version != payload.expected_conversation_version:
        raise conflict(
            "conversation version changed",
            expected=payload.expected_conversation_version,
            actual=conversation.state_version,
        )
    if conversation.state != ConversationState.IDLE:
        raise DomainError(
            "CONVERSATION_NOT_IDLE",
            "Manual condensation requires an idle conversation",
            409,
            {"state": conversation.state},
        )
    if not conversation.runtime_conversation_id:
        raise DomainError(
            "RUNTIME_CONVERSATION_UNAVAILABLE",
            "Conversation has no active OpenHands Runtime",
            409,
        )
    attempt = _attempt(db, conversation.attempt_id)
    condenser = cast(
        dict[str, Any],
        conversation.context_baseline_json.get("condenser")
        or attempt.condenser_config_json
        or {"kind": "NO_OP"},
    )
    if condenser.get("kind") != "LLM_SUMMARIZING":
        raise DomainError(
            "CONDENSER_NOT_AVAILABLE",
            "Manual condensation requires the frozen LLM summarizing condenser",
            409,
        )
    active = db.scalar(
        select(RuntimeCondensationCommand).where(
            RuntimeCondensationCommand.conversation_id == conversation.id,
            RuntimeCondensationCommand.state == "PENDING",
        )
    )
    if active is not None:
        raise conflict(
            "conversation already has an active condensation command",
            condensation_command_id=active.id,
        )

    command = RuntimeCondensationCommand(
        attempt_id=attempt.id,
        conversation_id=conversation.id,
        runtime_conversation_id=conversation.runtime_conversation_id,
        baseline_cursor=conversation.runtime_cursor,
        idempotency_key=idempotency_key,
        requested_by=actor,
    )
    db.add(command)
    db.flush()
    previous = conversation.state
    conversation.state = ConversationState.CONDENSING
    conversation.state_version += 1
    task = enqueue(
        db,
        task_type="CONDENSE_CONVERSATION",
        aggregate_type="RUNTIME_CONDENSATION_COMMAND",
        aggregate_id=command.id,
        idempotency_key=f"condense-conversation:{command.id}:v1",
    )
    task.max_attempts = max(task.max_attempts, 20)
    _event(
        db,
        conversation,
        "CONVERSATION_CONDENSATION_COMMAND_CREATED",
        {
            "condensation_command_id": command.id,
            "baseline_cursor": command.baseline_cursor,
        },
    )
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
    finish(db)
    return _condensation_command_dict(command)


def process_conversation_condensation(
    db: Session,
    command_id: str,
    lease: Lease,
    *,
    commit: bool = True,
) -> None:
    """Execute or reconcile one native manual condensation with lease fencing."""

    command = db.get(RuntimeCondensationCommand, command_id)
    if command is None or command.state != "PENDING":
        return
    conversation = _conversation(db, command.conversation_id)
    attempt = _attempt(db, conversation.attempt_id)
    if (
        conversation.runtime_conversation_id != command.runtime_conversation_id
        or conversation.state != ConversationState.CONDENSING
        or attempt.state in TERMINAL_ATTEMPT_STATES
    ):
        command.state = "CANCELLED"
        command.state_version += 1
        command.completed_at = now()
        if conversation.state == ConversationState.CONDENSING:
            conversation.state = ConversationState.IDLE
            conversation.state_version += 1
        db.commit() if commit else db.flush()
        return

    command_version = command.state_version
    handle = RuntimeHandle(
        conversation.runtime_job_id or command.runtime_conversation_id,
        command.runtime_conversation_id,
        command.baseline_cursor,
    )
    runtime_adapter = conversation.runtime_adapter
    runtime_sandbox_id = conversation.runtime_sandbox_id
    db.rollback()

    runtime = runtime_for(runtime_adapter, handle)

    def native_pair(events: tuple[RuntimeEvent, ...]) -> tuple[str | None, str | None]:
        """Return the single ordered Request -> Condensation pair after the baseline.

        OpenHands 1.40.0 does not expose a command/correlation id for manual
        condensation.  Its synchronous endpoint appends exactly one
        ``CondensationRequest`` followed by exactly one ``Condensation``.  The
        frozen event cursor is therefore the command boundary; accepting an
        orphaned completion or multiple requests would attribute an unrelated
        native operation to this durable command.
        """

        request_id: str | None = None
        completion_id: str | None = None
        for event in events:
            if event.event_type == "CONDENSATION_REQUESTED":
                if request_id is not None:
                    raise DomainError(
                        "RUNTIME_CONDENSATION_DRIFTED",
                        "OpenHands emitted multiple condensation requests after the frozen cursor",
                        409,
                        {
                            "first_request_event_id": request_id,
                            "unexpected_request_event_id": event.cursor,
                        },
                    )
                request_id = event.cursor
            elif event.event_type == "CONDENSATION_COMPLETED":
                if request_id is None or completion_id is not None:
                    raise DomainError(
                        "RUNTIME_CONDENSATION_DRIFTED",
                        (
                            "OpenHands condensation events do not form one ordered "
                            "request/completion pair"
                        ),
                        409,
                        {
                            "request_event_id": request_id,
                            "unexpected_completion_event_id": event.cursor,
                        },
                    )
                completion_id = event.cursor
        return request_id, completion_id

    before = runtime.read_events(handle)
    request_id, completion_id = native_pair(before.events)
    observed = before
    if request_id is None:
        runtime.condense(handle)
        observed = runtime.read_events(handle)
        request_id, completion_id = native_pair(observed.events)
    if request_id is None or completion_id is None:
        raise DomainError(
            "RUNTIME_CONDENSATION_PENDING",
            "OpenHands condensation is accepted but its durable completion event is not visible",
            503,
            {
                "request_event_id": request_id,
                "completion_event_id": completion_id,
            },
        )
    sandboxes.touch_runtime(db, runtime_sandbox_id)
    if not lease_is_current(db, lease):
        raise RuntimeError("task lease was lost during conversation condensation")

    current = db.scalar(
        select(RuntimeCondensationCommand)
        .where(
            RuntimeCondensationCommand.id == command_id,
            RuntimeCondensationCommand.state == "PENDING",
            RuntimeCondensationCommand.state_version == command_version,
        )
        .with_for_update()
    )
    if current is None:
        db.rollback()
        return
    current_conversation = _conversation(db, current.conversation_id, lock=True)
    if current_conversation.state != ConversationState.CONDENSING:
        db.rollback()
        return
    for event in observed.events:
        _append_runtime_payload(
            db,
            current_conversation,
            cursor=event.cursor,
            event_type=event.event_type,
            payload=event.payload,
        )
    current.request_event_id = request_id
    current.completion_event_id = completion_id
    current.state = "SUCCEEDED"
    current.state_version += 1
    current.started_at = current.started_at or current.created_at
    current.completed_at = now()
    current_conversation.runtime_cursor = observed.cursor or completion_id
    previous = current_conversation.state
    current_conversation.state = ConversationState.IDLE
    current_conversation.state_version += 1
    _event(
        db,
        current_conversation,
        "CONVERSATION_CONDENSATION_COMMAND_SUCCEEDED",
        {
            "condensation_command_id": current.id,
            "request_event_id": request_id,
            "completion_event_id": completion_id,
        },
    )
    _event(
        db,
        current_conversation,
        "CONVERSATION_STATE_CHANGED",
        {
            "from": previous,
            "to": current_conversation.state,
            "version": current_conversation.state_version,
        },
    )
    db.commit() if commit else db.flush()


def revise_message(
    db: Session,
    message_id: str,
    payload: ConversationReviseWrite,
    idempotency_key: str,
    actor: str | None,
) -> dict[str, Any]:
    """Replace the last stopped human turn and rebuild the same conversation Runtime."""

    existing_action = db.scalar(
        select(HumanAction).where(HumanAction.idempotency_key == idempotency_key)
    )
    if existing_action is not None:
        return get_conversation(db, str(existing_action.payload_json.get("conversation_id") or ""))
    source_message = db.get(AgentMessage, message_id)
    if source_message is None:
        raise not_found("agent_message", message_id)
    conversation = _conversation(db, source_message.conversation_id, lock=True)
    if conversation.state_version != payload.expected_conversation_version:
        raise conflict(
            "conversation was modified",
            expected=payload.expected_conversation_version,
            actual=conversation.state_version,
        )
    stopped_turn = cast(
        dict[str, Any], conversation.context_baseline_json.get("stopped_turn") or {}
    )
    if (
        conversation.kind == ConversationKind.AUTO
        or conversation.state != ConversationState.IDLE
        or stopped_turn.get("editable_message_id") != source_message.id
        or source_message.source != MessageSource.HUMAN
        or source_message.message_type != MessageType.TEXT
        or _message_is_superseded(source_message)
    ):
        raise DomainError(
            "MESSAGE_NOT_REVISABLE",
            "Only the last human message from a stopped turn can be revised",
            409,
        )
    attempt = _attempt(db, conversation.attempt_id)
    if attempt.state in TERMINAL_ATTEMPT_STATES:
        raise DomainError("CONVERSATION_READ_ONLY", "Conversation is read only", 409)

    active_prefix = [
        row
        for row in db.scalars(
            select(AgentMessage)
            .where(
                AgentMessage.conversation_id == conversation.id,
                AgentMessage.sequence_no < source_message.sequence_no,
                AgentMessage.source != MessageSource.PROGRAM,
            )
            .order_by(AgentMessage.sequence_no)
        )
        if not _message_is_superseded(row)
    ]
    superseded_rows = list(
        db.scalars(
            select(AgentMessage).where(
                AgentMessage.conversation_id == conversation.id,
                AgentMessage.sequence_no >= source_message.sequence_no,
            )
        )
    )
    revision_id = str(uuid4())
    for row in superseded_rows:
        row.content_json = {
            **row.content_json,
            "superseded": True,
            "superseded_by_revision_id": revision_id,
        }
        if row.delivery_state in {DeliveryState.QUEUED, DeliveryState.DELIVERING}:
            row.delivery_state = DeliveryState.CANCELLED
            row.error_code = "MESSAGE_SUPERSEDED"
            row.error_detail = "Superseded by an edited resend"

    revised_content = copy.deepcopy(source_message.content_json)
    revised_content.pop("superseded", None)
    revised_content.pop("superseded_by_revision_id", None)
    non_text_parts = [
        part
        for part in cast(list[dict[str, Any]], revised_content.get("parts") or [])
        if part.get("type") != "text"
    ]
    revised_content["parts"] = [{"type": "text", "text": payload.text}, *non_text_parts]
    revised_content["presentation"] = "chat"
    revised_content["revision"] = {
        "id": revision_id,
        "replaces_message_id": source_message.id,
    }
    revised = _append(
        db,
        conversation,
        source=MessageSource.HUMAN,
        message_type=MessageType.TEXT,
        content=revised_content,
        delivery_state=DeliveryState.QUEUED,
        delivery_mode=DeliveryMode.QUEUE_AFTER_TURN,
        client_message_id=f"revise:{source_message.id}:{revision_id}",
        created_by=actor,
    )
    baseline = copy.deepcopy(conversation.context_baseline_json or {})
    baseline.pop("stopped_turn", None)
    baseline["fork"] = {
        "history": list(_fork_runtime_history(active_prefix)),
        "revision_id": revision_id,
        "replaces_message_id": source_message.id,
    }
    conversation.context_baseline_json = baseline
    conversation.state = ConversationState.CREATING
    conversation.state_version += 1
    conversation.runtime_job_id = None
    conversation.runtime_conversation_id = None
    conversation.runtime_cursor = None
    conversation.runtime_sandbox_id = None
    node_run, run_id = _context(db, attempt)
    db.add(
        HumanAction(
            flow_run_id=run_id,
            node_run_id=node_run.id,
            attempt_id=attempt.id,
            action_type="REVISE_AGENT_MESSAGE",
            idempotency_key=idempotency_key,
            payload_json={
                "conversation_id": conversation.id,
                "source_message_id": source_message.id,
                "message_id": revised.id,
                "revision_id": revision_id,
            },
        )
    )
    enqueue(
        db,
        task_type="CREATE_CONVERSATION",
        aggregate_type="CONVERSATION",
        aggregate_id=conversation.id,
        idempotency_key=f"recreate-conversation:{conversation.id}:revision:{revision_id}",
    )
    _event(
        db,
        conversation,
        "CONVERSATION_MESSAGE_REVISED",
        {
            "source_message_id": source_message.id,
            "message_id": revised.id,
            "revision_id": revision_id,
        },
    )
    finish(db)
    return _conversation_dict(db, conversation)


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
    selected_model, selected_effort = resolve_runtime_selection(
        db, _attempt_node(db, attempt), payload.model_name, payload.reasoning_effort
    )
    count, maximum = db.execute(
        select(
            func.count(AgentConversation.id),
            func.max(AgentConversation.conversation_no),
        ).where(AgentConversation.attempt_id == attempt.id)
    ).one()
    if count >= get_settings().conversation_limit_per_attempt:
        raise DomainError("CONVERSATION_LIMIT_REACHED", "Conversation limit reached", 422)
    number = int(maximum or 0) + 1
    baseline = _baseline(db, attempt)
    baseline["runtime_selection"] = {
        "model_name": selected_model,
        "reasoning_effort": selected_effort,
    }
    item = AgentConversation(
        attempt_id=attempt.id,
        conversation_no=number,
        kind=ConversationKind.HUMAN_CREATED,
        title=payload.title or f"人工会话 {number}",
        state=ConversationState.CREATING,
        context_baseline_json=baseline,
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


def request_goal_command(
    db: Session,
    conversation_id: str,
    payload: ConversationGoalWrite,
    idempotency_key: str,
    actor: str | None,
) -> dict[str, Any]:
    if not actor:
        raise DomainError(
            "GOAL_ACTOR_REQUIRED",
            "Goal control requires an authenticated actor",
            403,
        )
    existing = db.scalar(
        select(RuntimeGoalCommand).where(RuntimeGoalCommand.idempotency_key == idempotency_key)
    )
    if existing is not None:
        if existing.requested_by != actor:
            raise DomainError(
                "GOAL_COMMAND_FORBIDDEN", "Goal command belongs to another actor", 403
            )
        return {"id": existing.id, "state": existing.state, "action": existing.action}
    item = _conversation(db, conversation_id, lock=True)
    if item.kind == ConversationKind.AUTO or item.state != ConversationState.IDLE:
        raise DomainError(
            "GOAL_CONVERSATION_NOT_IDLE", "Goal commands require an idle human conversation", 409
        )
    if item.state_version != payload.expected_conversation_version:
        raise conflict(
            "conversation was modified",
            expected=payload.expected_conversation_version,
            actual=item.state_version,
        )
    handle = _runtime_handle(item)
    active = db.scalar(
        select(RuntimeGoalCommand).where(
            RuntimeGoalCommand.conversation_id == item.id,
            RuntimeGoalCommand.state.in_(("PENDING", "RUNNING")),
        )
    )
    if active is not None and active.action == "STOP":
        raise DomainError("GOAL_COMMAND_ACTIVE", "A Goal stop command is already active", 409)
    if active is not None and not (payload.action == "STOP" and active.state == "RUNNING"):
        raise DomainError("GOAL_COMMAND_ACTIVE", "A Goal command is already active", 409)
    latest_status = db.scalar(
        select(RuntimeGoalStatus)
        .where(RuntimeGoalStatus.conversation_id == item.id)
        .order_by(RuntimeGoalStatus.created_at.desc(), RuntimeGoalStatus.id.desc())
        .limit(1)
    )
    if payload.action == "START" and latest_status is not None and latest_status.active:
        raise DomainError("GOAL_STATE_INVALID", "A native Goal is already active", 409)
    if payload.action in {"STOP", "RESUME"}:
        prior = latest_status
        if (
            prior is None
            or (payload.action == "STOP" and not prior.active)
            or (payload.action == "RESUME" and prior.status != "interrupted")
        ):
            raise DomainError(
                "GOAL_STATE_INVALID", "Goal action does not match the native Goal state", 409
            )
        objective = prior.objective
        max_iterations = prior.max_iterations
        origin = db.scalar(
            select(RuntimeGoalCommand)
            .where(
                RuntimeGoalCommand.conversation_id == item.id,
                RuntimeGoalCommand.action == "START",
            )
            .order_by(RuntimeGoalCommand.created_at.desc(), RuntimeGoalCommand.id.desc())
            .limit(1)
        )
        if origin is None:
            raise DomainError(
                "GOAL_GOVERNANCE_UNAVAILABLE",
                "Goal governance baseline is unavailable",
                409,
            )
        max_tokens = origin.max_tokens
        max_cost_usd = origin.max_cost_usd
        baseline_cost_usd = origin.baseline_cost_usd
        baseline_tokens = origin.baseline_tokens
        if payload.action == "STOP" and active is not None:
            active.state = "SUCCEEDED"
            active.state_version += 1
            active.terminal_status = "stop_requested"
            active.completed_at = now()
            db.flush()
    else:
        objective = (payload.objective or "").strip()
        max_iterations = payload.max_iterations
        max_tokens = payload.max_tokens
        max_cost_usd = payload.max_cost_usd
        baseline_cost_usd = 0
        baseline_tokens = 0
    command = RuntimeGoalCommand(
        attempt_id=item.attempt_id,
        conversation_id=item.id,
        runtime_conversation_id=handle.conversation_id,
        action=payload.action,
        objective=objective,
        max_iterations=max_iterations,
        max_tokens=max_tokens,
        max_cost_usd=max_cost_usd,
        baseline_cost_usd=baseline_cost_usd,
        baseline_tokens=baseline_tokens,
        state="PENDING",
        idempotency_key=idempotency_key,
        requested_by=actor,
    )
    db.add(command)
    db.flush()
    enqueue(
        db,
        task_type="CONTROL_CONVERSATION_GOAL",
        aggregate_type="GOAL_COMMAND",
        aggregate_id=command.id,
        idempotency_key=f"control-goal:{command.id}:v1",
    )
    node_run, run_id = _context(db, _attempt(db, item.attempt_id))
    db.add(
        HumanAction(
            flow_run_id=run_id,
            node_run_id=node_run.id,
            attempt_id=item.attempt_id,
            action_type=f"{payload.action}_AGENT_GOAL",
            idempotency_key=idempotency_key,
            payload_json={
                "conversation_id": item.id,
                "goal_command_id": command.id,
                "max_iterations": max_iterations,
                "max_tokens": max_tokens,
                "max_cost_usd": max_cost_usd,
            },
        )
    )
    _event(
        db,
        item,
        "CONVERSATION_GOAL_COMMAND_REQUESTED",
        {"goal_command_id": command.id, "action": command.action},
    )
    finish(db)
    return {"id": command.id, "state": command.state, "action": command.action}


def process_goal_command(
    db: Session, command_id: str, lease: Lease, *, commit: bool = True
) -> None:
    command = db.scalar(
        select(RuntimeGoalCommand)
        .where(
            RuntimeGoalCommand.id == command_id,
            RuntimeGoalCommand.state.in_(("PENDING", "RUNNING")),
        )
        .with_for_update()
    )
    if command is None:
        db.rollback()
        return
    item = _conversation(db, command.conversation_id)
    handle = _runtime_handle(item)
    adapter = item.runtime_adapter
    command_version = command.state_version
    command_state = command.state
    dispatch_pending = command.error_code == "GOAL_COMMAND_DISPATCHING"
    action = command.action
    objective = command.objective or ""
    max_iterations = command.max_iterations
    db.rollback()
    runtime = runtime_for(adapter, handle)
    baseline = runtime.read_events(handle)
    baseline_cost, baseline_tokens = _usage_totals(baseline.usage)
    baseline_goal = next(
        (
            cast(dict[str, Any], event.payload["goal_status"])
            for event in reversed(baseline.events)
            if isinstance(event.payload.get("goal_status"), dict)
        ),
        None,
    )
    already_applied = bool(
        baseline_goal is not None
        and (
            (
                action == "START"
                and baseline_goal.get("objective") == objective
                and baseline_goal.get("max_iterations") == max_iterations
            )
            or (action == "STOP" and baseline_goal.get("status") == "interrupted")
            or (
                action == "RESUME"
                and baseline_goal.get("status") in {"running", "complete", "capped"}
                and baseline_goal.get("objective") == objective
                and baseline_goal.get("max_iterations") == max_iterations
            )
        )
    )
    action_handle = RuntimeHandle(
        handle.job_id,
        handle.conversation_id,
        baseline.cursor or handle.cursor,
        runtime_resource_id=handle.runtime_resource_id,
        runtime_resource_name=handle.runtime_resource_name,
    )
    if command_state == "RUNNING" and dispatch_pending and not already_applied:
        if not lease_is_current(db, lease):
            raise RuntimeError("task lease was lost during Goal command reconciliation")
        current = db.scalar(
            select(RuntimeGoalCommand)
            .where(
                RuntimeGoalCommand.id == command_id,
                RuntimeGoalCommand.state_version == command_version,
            )
            .with_for_update()
        )
        if current is None:
            db.rollback()
            return
        current.state = "FAILED"
        current.state_version += 1
        current.error_code = "RUNTIME_GOAL_COMMAND_OUTCOME_UNKNOWN"
        current.error_detail = (
            "A prior Goal control call lost its durable completion fence and was not repeated"
        )
        current.completed_at = now()
        conversation = _conversation(db, current.conversation_id)
        _event(
            db,
            conversation,
            "CONVERSATION_GOAL_COMMAND_FAILED",
            {"goal_command_id": current.id, "error": current.error_code},
        )
        db.commit() if commit else db.flush()
        return
    if command_state == "RUNNING" or already_applied:
        observed = RuntimeEventBatch(cursor=baseline.cursor, usage=baseline.usage)
    else:
        if baseline_goal is not None:
            raise DomainError(
                "RUNTIME_GOAL_COMMAND_DRIFT",
                "Observed Goal state does not match the pending governed command",
                409,
            )
        if not lease_is_current(db, lease):
            raise RuntimeError("task lease was lost before Goal command dispatch")
        dispatching = db.scalar(
            select(RuntimeGoalCommand)
            .where(
                RuntimeGoalCommand.id == command_id,
                RuntimeGoalCommand.state == "PENDING",
                RuntimeGoalCommand.state_version == command_version,
            )
            .with_for_update()
        )
        if dispatching is None:
            db.rollback()
            return
        dispatching.state = "RUNNING"
        dispatching.state_version += 1
        dispatching.error_code = "GOAL_COMMAND_DISPATCHING"
        if action == "START":
            dispatching.baseline_cost_usd = baseline_cost
            dispatching.baseline_tokens = baseline_tokens
        command_version = dispatching.state_version
        db.commit()
        if action == "START":
            runtime.start_goal(action_handle, objective, max_iterations)
        elif action == "STOP":
            runtime.stop_goal(action_handle)
        else:
            runtime.resume_goal(action_handle)
        observed = runtime.read_events(action_handle)
    if not lease_is_current(db, lease):
        raise RuntimeError("task lease was lost during Goal command")
    current = db.scalar(
        select(RuntimeGoalCommand)
        .where(
            RuntimeGoalCommand.id == command_id, RuntimeGoalCommand.state_version == command_version
        )
        .with_for_update()
    )
    if current is None:
        db.rollback()
        return
    conversation = _conversation(db, current.conversation_id, lock=True)
    if action == "START" and current.baseline_cost_usd == 0 and current.baseline_tokens == 0:
        current.baseline_cost_usd = baseline_cost
        current.baseline_tokens = baseline_tokens
    if current.error_code == "GOAL_COMMAND_DISPATCHING":
        current.error_code = None
    if current.state == "PENDING" and not already_applied:
        current.state = "RUNNING"
        current.state_version += 1
    for event in baseline.events:
        _append_runtime_payload(
            db,
            conversation,
            cursor=event.cursor,
            event_type=event.event_type,
            payload=event.payload,
        )
    for event in observed.events:
        _append_runtime_payload(
            db,
            conversation,
            cursor=event.cursor,
            event_type=event.event_type,
            payload=event.payload,
        )
    if current.state == "PENDING":
        current.state = "RUNNING"
        current.state_version += 1
    conversation.runtime_cursor = observed.cursor or baseline.cursor or conversation.runtime_cursor
    _event(
        db,
        conversation,
        "CONVERSATION_GOAL_COMMAND_APPLIED",
        {"goal_command_id": current.id, "action": action},
    )
    if current.state == "RUNNING":
        enqueue(
            db,
            task_type="POLL_CONVERSATION",
            aggregate_type="CONVERSATION",
            aggregate_id=conversation.id,
            idempotency_key=f"poll-goal:{conversation.id}:{current.id}:1",
            payload={"poll_no": 1},
            available_at=datetime.now(UTC) + timedelta(seconds=get_settings().runtime_poll_seconds),
        )
    db.commit() if commit else db.flush()


def request_ask_agent(
    db: Session,
    conversation_id: str,
    payload: ConversationAskAgentWrite,
    idempotency_key: str,
    actor: str | None,
) -> dict[str, Any]:
    if not actor:
        raise DomainError(
            "DIAGNOSTIC_ACTOR_REQUIRED",
            "ask_agent requires an authenticated actor",
            403,
        )
    existing = db.scalar(
        select(RuntimeDiagnosticQuery).where(
            RuntimeDiagnosticQuery.idempotency_key == idempotency_key
        )
    )
    if existing is not None:
        if existing.requested_by != actor:
            raise DomainError(
                "DIAGNOSTIC_QUERY_FORBIDDEN",
                "Diagnostic query belongs to another actor",
                403,
            )
        return diagnostic_query_dict(existing)
    item = _conversation(db, conversation_id, lock=True)
    if item.kind == ConversationKind.AUTO or item.state in {
        ConversationState.CREATING,
        ConversationState.FAILED,
        ConversationState.READ_ONLY,
    }:
        raise DomainError(
            "DIAGNOSTIC_CONVERSATION_UNAVAILABLE",
            "ask_agent requires an active human conversation",
            409,
        )
    handle = _runtime_handle(item)
    question = payload.question.strip()
    query = RuntimeDiagnosticQuery(
        attempt_id=item.attempt_id,
        conversation_id=item.id,
        runtime_conversation_id=handle.conversation_id,
        question_text=question,
        question_digest=hashlib.sha256(question.encode()).hexdigest(),
        question_length=len(question),
        output_classification=payload.output_classification,
        timeout_seconds=payload.timeout_seconds,
        state="PENDING",
        idempotency_key=idempotency_key,
        requested_by=actor,
    )
    db.add(query)
    db.flush()
    enqueue(
        db,
        task_type="ASK_CONVERSATION_AGENT",
        aggregate_type="DIAGNOSTIC_QUERY",
        aggregate_id=query.id,
        idempotency_key=f"ask-agent:{query.id}",
    )
    node_run, run_id = _context(db, _attempt(db, item.attempt_id))
    db.add(
        HumanAction(
            flow_run_id=run_id,
            node_run_id=node_run.id,
            attempt_id=item.attempt_id,
            action_type="ASK_AGENT_DIAGNOSTIC",
            idempotency_key=idempotency_key,
            payload_json={
                "conversation_id": item.id,
                "diagnostic_query_id": query.id,
                "question_digest": query.question_digest,
                "question_length": query.question_length,
                "output_classification": query.output_classification,
                "timeout_seconds": query.timeout_seconds,
            },
        )
    )
    _event(
        db,
        item,
        "CONVERSATION_DIAGNOSTIC_REQUESTED",
        {
            "diagnostic_query_id": query.id,
            "question_digest": query.question_digest,
            "output_classification": query.output_classification,
        },
    )
    finish(db)
    return diagnostic_query_dict(query)


def diagnostic_query_dict(query: RuntimeDiagnosticQuery) -> dict[str, Any]:
    return {
        "id": query.id,
        "conversation_id": query.conversation_id,
        "output_classification": query.output_classification,
        "timeout_seconds": query.timeout_seconds,
        "state": query.state,
        "response_text": query.response_text,
        "cost_usd": float(query.cost_usd) if query.cost_usd is not None else None,
        "prompt_tokens": query.prompt_tokens,
        "completion_tokens": query.completion_tokens,
        "error_code": query.error_code,
        "created_at": query.created_at.isoformat(),
        "completed_at": query.completed_at.isoformat() if query.completed_at else None,
    }


def get_diagnostic_query(db: Session, query_id: str, actor: str) -> dict[str, Any]:
    query = db.get(RuntimeDiagnosticQuery, query_id)
    if query is None:
        raise not_found("runtime_diagnostic_query", query_id)
    if query.requested_by != actor:
        raise DomainError(
            "DIAGNOSTIC_QUERY_FORBIDDEN",
            "Diagnostic query belongs to another actor",
            403,
        )
    return diagnostic_query_dict(query)


def process_ask_agent(db: Session, query_id: str, lease: Lease, *, commit: bool = True) -> None:
    query = db.scalar(
        select(RuntimeDiagnosticQuery)
        .where(
            RuntimeDiagnosticQuery.id == query_id,
            RuntimeDiagnosticQuery.state.in_(("PENDING", "RUNNING")),
        )
        .with_for_update()
    )
    if query is None:
        db.rollback()
        return
    if query.state == "RUNNING":
        query.state = "FAILED"
        query.question_text = ""
        query.error_code = "RUNTIME_ASK_AGENT_OUTCOME_UNKNOWN"
        query.error_detail = (
            "A prior ask_agent call lost its durable completion fence and was not repeated"
        )
        query.completed_at = now()
        conversation = _conversation(db, query.conversation_id)
        _event(
            db,
            conversation,
            "CONVERSATION_DIAGNOSTIC_FAILED",
            {"diagnostic_query_id": query.id, "error": query.error_code},
        )
        db.commit() if commit else db.flush()
        return
    item = _conversation(db, query.conversation_id)
    handle = _runtime_handle(item)
    adapter = item.runtime_adapter
    timeout_seconds = query.timeout_seconds
    question_digest = query.question_digest
    question = query.question_text
    if not question or hashlib.sha256(question.encode()).hexdigest() != question_digest:
        raise DomainError("DIAGNOSTIC_QUESTION_DRIFTED", "Diagnostic question digest drifted", 409)
    query.state = "RUNNING"
    db.commit()
    result = runtime_for(adapter, handle).ask_agent(
        handle, question, timeout_seconds=timeout_seconds
    )
    if not lease_is_current(db, lease):
        raise RuntimeError("task lease was lost during ask_agent")
    current = db.scalar(
        select(RuntimeDiagnosticQuery)
        .where(RuntimeDiagnosticQuery.id == query_id, RuntimeDiagnosticQuery.state == "RUNNING")
        .with_for_update()
    )
    if current is None:
        db.rollback()
        return
    current.response_text = result.response
    current.question_text = ""
    current.state = "SUCCEEDED"
    current.completed_at = now()
    if result.after_usage is not None:
        before_cost = result.before_usage.accumulated_cost if result.before_usage else 0.0
        before_prompt = result.before_usage.prompt_tokens if result.before_usage else 0
        before_completion = result.before_usage.completion_tokens if result.before_usage else 0
        current.cost_usd = max(0.0, result.after_usage.accumulated_cost - before_cost)
        current.prompt_tokens = max(0, result.after_usage.prompt_tokens - before_prompt)
        current.completion_tokens = max(0, result.after_usage.completion_tokens - before_completion)
    conversation = _conversation(db, current.conversation_id, lock=True)
    _event(
        db,
        conversation,
        "CONVERSATION_DIAGNOSTIC_SUCCEEDED",
        {
            "diagnostic_query_id": current.id,
            "output_classification": current.output_classification,
            "cost_usd": float(current.cost_usd or 0),
        },
    )
    db.commit() if commit else db.flush()


def record_goal_command_failure(
    db: Session, command_id: str, error: str, *, terminal: bool
) -> None:
    if not terminal:
        return
    command = db.scalar(
        select(RuntimeGoalCommand)
        .where(
            RuntimeGoalCommand.id == command_id,
            RuntimeGoalCommand.state.in_(("PENDING", "RUNNING")),
        )
        .with_for_update()
    )
    if command is None:
        return
    command.state = "FAILED"
    command.state_version += 1
    command.error_code = "RUNTIME_GOAL_COMMAND_FAILED"
    command.error_detail = error[:2000]
    command.completed_at = now()
    conversation = _conversation(db, command.conversation_id)
    _event(
        db,
        conversation,
        "CONVERSATION_GOAL_COMMAND_FAILED",
        {"goal_command_id": command.id, "error": error[:2000]},
    )
    db.flush()


def record_ask_agent_failure(db: Session, query_id: str, error: str, *, terminal: bool) -> None:
    if not terminal:
        return
    query = db.scalar(
        select(RuntimeDiagnosticQuery)
        .where(
            RuntimeDiagnosticQuery.id == query_id,
            RuntimeDiagnosticQuery.state.in_(("PENDING", "RUNNING")),
        )
        .with_for_update()
    )
    if query is None:
        return
    query.state = "FAILED"
    query.question_text = ""
    query.error_code = "RUNTIME_ASK_AGENT_FAILED"
    query.error_detail = error[:2000]
    query.completed_at = now()
    conversation = _conversation(db, query.conversation_id)
    _event(
        db,
        conversation,
        "CONVERSATION_DIAGNOSTIC_FAILED",
        {"diagnostic_query_id": query.id, "error": error[:2000]},
    )
    db.flush()


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


def stop_conversation(
    db: Session,
    conversation_id: str,
    payload: ConversationStopWrite,
    idempotency_key: str,
) -> dict[str, Any]:
    """Durably request interruption of a human-created Agent turn.

    AUTO conversations own the node execution Runtime and must be stopped through
    the flow-run cancellation command so the Attempt cannot remain falsely active.
    """

    existing_action = db.scalar(
        select(HumanAction).where(HumanAction.idempotency_key == idempotency_key)
    )
    if existing_action is not None:
        return get_conversation(db, conversation_id)
    item = _conversation(db, conversation_id, lock=True)
    if item.kind == ConversationKind.AUTO:
        raise DomainError(
            "AUTO_CONVERSATION_REQUIRES_RUN_CANCEL",
            "The automatic conversation must be stopped by cancelling its flow run",
            409,
        )
    # Runtime polling advances the conversation version while the same Agent
    # turn is still generating. A stop command is state-targeted and remains
    # safe in that state, so do not reject it solely because the UI observed an
    # earlier poll version. Other states keep strict optimistic concurrency.
    if (
        item.state_version != payload.expected_conversation_version
        and item.state != ConversationState.GENERATING
    ):
        raise conflict(
            "conversation was modified",
            expected=payload.expected_conversation_version,
            actual=item.state_version,
        )
    if item.state == ConversationState.STOPPING:
        return _conversation_dict(db, item)
    if item.state != ConversationState.GENERATING:
        raise DomainError(
            "CONVERSATION_NOT_RUNNING", "Conversation is not generating a response", 409
        )
    attempt = _attempt(db, item.attempt_id)
    if attempt.state in TERMINAL_ATTEMPT_STATES:
        raise DomainError("CONVERSATION_READ_ONLY", "Conversation is read only", 409)
    node_run, run_id = _context(db, attempt)
    db.add(
        HumanAction(
            flow_run_id=run_id,
            node_run_id=node_run.id,
            attempt_id=attempt.id,
            action_type="STOP_AGENT_CONVERSATION",
            idempotency_key=idempotency_key,
            payload_json={"conversation_id": item.id},
        )
    )
    previous = item.state
    item.state = ConversationState.STOPPING
    item.state_version += 1
    queued = list(
        db.scalars(
            select(AgentMessage).where(
                AgentMessage.conversation_id == item.id,
                AgentMessage.delivery_state.in_([DeliveryState.QUEUED, DeliveryState.DELIVERING]),
            )
        )
    )
    for message in queued:
        message.delivery_state = DeliveryState.CANCELLED
        message.error_code = "CONVERSATION_STOPPED"
        message.error_detail = "Cancelled because the Agent turn was stopped"
    task = enqueue(
        db,
        task_type="STOP_CONVERSATION_RUNTIME",
        aggregate_type="CONVERSATION",
        aggregate_id=item.id,
        idempotency_key=f"stop-conversation-runtime:{item.id}:v{item.state_version}",
    )
    task.max_attempts = max(task.max_attempts, 10)
    _event(
        db,
        item,
        "CONVERSATION_STATE_CHANGED",
        {"from": previous, "to": item.state, "version": item.state_version},
    )
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
    # The conversation row is the sandbox owner. Persist the monotonic delete
    # intent before removing that owner so cleanup is explicit, crash-safe, and
    # auditable in managed_sandboxes even after the conversation disappears.
    runtime_sandbox_id = _runtime_sandbox_id(db, current)
    if _runtime_sandbox_is_conversation_owned(
        db, runtime_sandbox_id
    ) and not _runtime_sandbox_has_other_active_conversations(
        db, runtime_sandbox_id, excluding_conversation_id=current.id
    ):
        sandboxes.request_delete_durable(db, runtime_sandbox_id)
    db.execute(delete(BackgroundTask).where(BackgroundTask.aggregate_id.in_(aggregate_ids)))
    db.delete(current)
    finish(db)
    for path in attachment_paths:
        path.unlink(missing_ok=True)


def list_messages(
    db: Session, conversation_id: str, after_sequence: int, limit: int
) -> list[dict[str, Any]]:
    _conversation(db, conversation_id)
    rows = [
        item
        for item in db.scalars(
            select(AgentMessage)
            .where(
                AgentMessage.conversation_id == conversation_id,
                AgentMessage.sequence_no > after_sequence,
            )
            .order_by(AgentMessage.sequence_no)
        )
        if not _message_is_superseded(item)
    ][: min(limit, 200)]
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
    attempt_relative = attempt_root.relative_to(workspace_root)
    if len(attempt_relative.parts) < 2 or attempt_relative.parts[0] != "nodes":
        raise DomainError("WORKSPACE_PATH_INVALID", "Attempt workspace is invalid", 409)
    node_root = workspace_root.joinpath(*attempt_relative.parts[:2]).resolve()
    if not attempt_root.is_relative_to(node_root) or not node_root.is_relative_to(workspace_root):
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
    allowed_root = node_root if raw_path.is_absolute() else attempt_root
    # Absolute Runtime paths may refer to durable files shared by all sessions
    # of this node (for example ``nodes/<id>/files/auth-qrcode.png``). Relative
    # paths still resolve from the Attempt directory above. Never allow a
    # message to read another node's workspace.
    if not resolved.is_relative_to(allowed_root) or not resolved.is_file():
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
    active_goal = db.scalar(
        select(RuntimeGoalCommand.id).where(
            RuntimeGoalCommand.conversation_id == item.id,
            RuntimeGoalCommand.state.in_(("PENDING", "RUNNING")),
        )
    )
    latest_goal_active = db.scalar(
        select(RuntimeGoalStatus.active)
        .where(RuntimeGoalStatus.conversation_id == item.id)
        .order_by(RuntimeGoalStatus.created_at.desc(), RuntimeGoalStatus.id.desc())
        .limit(1)
    )
    if active_goal is not None or latest_goal_active is True:
        raise DomainError(
            "CONVERSATION_GOAL_ACTIVE",
            "Stop the active Goal before sending a conversation message",
            409,
        )
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
    if item.state == ConversationState.CONDENSING:
        raise DomainError(
            "CONVERSATION_CONDENSING",
            "Conversation context is being condensed; retry after it returns to idle",
            409,
        )
    text = "\n".join(part.text for part in payload.content if part.type == "text")
    if len(text) > get_settings().conversation_message_max_chars:
        raise DomainError("MESSAGE_TOO_LARGE", "Message is too large", 422)
    capability_refs = _validated_capability_refs(db, attempt, payload)
    current_selection = cast(
        dict[str, Any], item.context_baseline_json.get("runtime_selection") or {}
    )
    requested_model = payload.model_name or current_selection.get("model_name")
    requested_effort = (
        payload.reasoning_effort
        if "reasoning_effort" in payload.model_fields_set
        else current_selection.get("reasoning_effort")
    )
    selected_model, selected_effort = resolve_runtime_selection(
        db, _attempt_node(db, attempt), requested_model, requested_effort
    )
    runtime_selection = {
        "model_name": selected_model,
        "reasoning_effort": selected_effort,
    }
    queued_during_turn = item.state == ConversationState.GENERATING
    message = _append(
        db,
        item,
        source=MessageSource.HUMAN,
        message_type=MessageType.TEXT,
        content={
            "parts": prepared_parts or [part.model_dump() for part in payload.content],
            "capability_refs": capability_refs,
            "runtime_selection": runtime_selection,
            "presentation": "queued" if queued_during_turn else "chat",
        },
        delivery_state=DeliveryState.QUEUED,
        delivery_mode=payload.delivery_mode,
        client_message_id=payload.client_message_id,
        created_by=actor,
    )
    if item.context_baseline_json.get("stopped_turn"):
        baseline = copy.deepcopy(item.context_baseline_json)
        baseline.pop("stopped_turn", None)
        item.context_baseline_json = baseline
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
                "runtime_selection": runtime_selection,
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
        task = enqueue(
            db,
            task_type=task_type,
            aggregate_type=aggregate_type,
            aggregate_id=aggregate_id,
            idempotency_key=recovery_key,
            payload=payload,
            available_at=datetime.now(UTC),
        )
        if task_type == "POLL_CONVERSATION":
            task.max_attempts = max(task.max_attempts, 10)
    else:
        existing.state = TaskState.RETRY
        existing.available_at = datetime.now(UTC)
        existing.lease_owner = None
        existing.lease_until = None
        existing.last_error = "STARTUP_RECOVERY"
        existing.payload_json = payload or {}
        if task_type == "POLL_CONVERSATION":
            existing.max_attempts = max(existing.max_attempts, 10)
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
                        ConversationState.FORKING,
                        ConversationState.CONDENSING,
                        ConversationState.IDLE,
                        ConversationState.GENERATING,
                        ConversationState.STOPPING,
                        ConversationState.WAITING_HUMAN,
                        ConversationState.READ_ONLY,
                    ]
                )
            )
            .order_by(AgentConversation.updated_at, AgentConversation.id)
            .with_for_update(skip_locked=True)
        )
    )
    recovered = 0
    for conversation in conversations:
        if conversation.state == ConversationState.FORKING:
            command = db.scalar(
                select(RuntimeConversationFork)
                .where(
                    RuntimeConversationFork.source_conversation_id == conversation.id,
                    RuntimeConversationFork.state == "PENDING",
                )
                .order_by(RuntimeConversationFork.created_at.desc())
                .limit(1)
            )
            if command is not None and _recover_delivery(
                db,
                task_type="FORK_CONVERSATION",
                aggregate_type="RUNTIME_CONVERSATION_FORK",
                aggregate_id=command.id,
                recovery_key=f"recovery:fork-conversation:{command.id}:v{command.state_version}",
            ):
                recovered += 1
            continue
        if conversation.state == ConversationState.CONDENSING:
            command = db.scalar(
                select(RuntimeCondensationCommand)
                .where(
                    RuntimeCondensationCommand.conversation_id == conversation.id,
                    RuntimeCondensationCommand.state == "PENDING",
                )
                .order_by(RuntimeCondensationCommand.created_at.desc())
                .limit(1)
            )
            if command is None:
                previous = conversation.state
                conversation.state = ConversationState.IDLE
                conversation.state_version += 1
                _event(
                    db,
                    conversation,
                    "CONVERSATION_STATE_CHANGED",
                    {
                        "from": previous,
                        "to": conversation.state,
                        "version": conversation.state_version,
                        "reason": "MISSING_CONDENSATION_COMMAND",
                    },
                )
            elif _recover_delivery(
                db,
                task_type="CONDENSE_CONVERSATION",
                aggregate_type="RUNTIME_CONDENSATION_COMMAND",
                aggregate_id=command.id,
                recovery_key=f"condense-conversation:{command.id}:v1",
            ):
                recovered += 1
            continue
        if conversation.state == ConversationState.STOPPING:
            if _recover_delivery(
                db,
                task_type="STOP_CONVERSATION_RUNTIME",
                aggregate_type="CONVERSATION",
                aggregate_id=conversation.id,
                recovery_key=(
                    f"recovery:stop-conversation-runtime:{conversation.id}"
                    f":v{conversation.state_version}"
                ),
            ):
                recovered += 1
            continue
        if conversation.state == ConversationState.READ_ONLY:
            if _ensure_conversation_runtime_cleanup(db, conversation):
                recovered += 1
            continue
        attempt = _attempt(db, conversation.attempt_id)
        if attempt.state in TERMINAL_ATTEMPT_STATES:
            set_attempt_conversations_state(db, attempt.id, ConversationState.READ_ONLY)
            continue
        if conversation.state == ConversationState.CREATING:
            if conversation.kind != ConversationKind.AUTO and _recover_delivery(
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

        pending_goal = db.scalar(
            select(RuntimeGoalCommand)
            .where(
                RuntimeGoalCommand.conversation_id == conversation.id,
                RuntimeGoalCommand.state == "PENDING",
            )
            .order_by(RuntimeGoalCommand.created_at.desc(), RuntimeGoalCommand.id.desc())
            .limit(1)
        )
        if pending_goal is not None and _recover_delivery(
            db,
            task_type="CONTROL_CONVERSATION_GOAL",
            aggregate_type="GOAL_COMMAND",
            aggregate_id=pending_goal.id,
            recovery_key=f"control-goal:{pending_goal.id}:v1",
        ):
            recovered += 1
        running_goal = db.scalar(
            select(RuntimeGoalCommand.id).where(
                RuntimeGoalCommand.conversation_id == conversation.id,
                RuntimeGoalCommand.state == "RUNNING",
            )
        )
        if running_goal is not None and conversation.runtime_conversation_id:
            if _recover_delivery(
                db,
                task_type="POLL_CONVERSATION",
                aggregate_type="CONVERSATION",
                aggregate_id=conversation.id,
                recovery_key=f"recovery:poll-goal:{conversation.id}:{running_goal}",
                payload={"poll_no": 1},
            ):
                recovered += 1
        for diagnostic_id in db.scalars(
            select(RuntimeDiagnosticQuery.id).where(
                RuntimeDiagnosticQuery.conversation_id == conversation.id,
                RuntimeDiagnosticQuery.state.in_(("PENDING", "RUNNING")),
            )
        ):
            if _recover_delivery(
                db,
                task_type="ASK_CONVERSATION_AGENT",
                aggregate_type="RUNTIME_DIAGNOSTIC_QUERY",
                aggregate_id=diagnostic_id,
                recovery_key=f"ask-agent:{diagnostic_id}:v1",
            ):
                recovered += 1

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
        last_agent_sequence = max(
            (
                message.sequence_no
                for message in db.scalars(
                    select(AgentMessage).where(
                        AgentMessage.conversation_id == conversation.id,
                        AgentMessage.source == MessageSource.AGENT,
                    )
                )
                if not _message_is_progress(message)
            ),
            default=0,
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
        elif conversation.runtime_conversation_id:
            if _recover_delivery(
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
            active_wakeup_channels = {
                str((task.payload_json or {}).get("channel") or "CONVERSATION")
                for task in db.scalars(
                    select(BackgroundTask).where(
                        BackgroundTask.aggregate_id == conversation.id,
                        BackgroundTask.task_type == "WAIT_CONVERSATION_WAKEUP",
                        BackgroundTask.state.in_(
                            [TaskState.PENDING, TaskState.RETRY, TaskState.RUNNING]
                        ),
                    )
                )
            }
            for channel in ("CONVERSATION", "BASH"):
                if channel not in active_wakeup_channels:
                    _schedule_conversation_wakeup(db, conversation, channel, 1)
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
    if snapshot is None:
        raise DomainError("SNAPSHOT_INVALID", "Attempt Snapshot is unavailable", 409)
    node = runtime_node(
        definition=snapshot.definition_json,
        manifest=snapshot.runtime_manifest_json or {},
        expected_hash=snapshot.runtime_manifest_hash,
        snapshot_id=snapshot.id,
        instance_key=node_run.flow_node_snapshot_key,
    )
    memory_enabled, source_refs = frozen_memory_policy(node, runtime_scope="CONVERSATION")
    settings = get_settings()
    if memory_enabled and (
        environment is None
        or settings.runtime_adapter != "openhands"
        or settings.terminal_environment_backend != "docker"
    ):
        raise DomainError(
            "MEMORY_SOURCE_UNAVAILABLE",
            "Enabled Memory requires an isolated managed Runtime",
            409,
        )
    asset = cast(dict[str, Any], node.get("asset") or {})
    input_contracts = {
        str(item.get("field_key") or ""): item
        for raw in cast(list[object], asset.get("inputs") or [])
        if isinstance(raw, dict)
        for item in [cast(dict[str, Any], raw)]
    }
    bindings: list[dict[str, Any]] = []
    for binding in db.scalars(
        select(AttemptInputBinding).where(AttemptInputBinding.attempt_id == attempt.id)
    ):
        artifact = db.get(ArtifactVersion, binding.artifact_version_id)
        if artifact:
            contract = input_contracts.get(binding.input_field_key, {})
            bindings.append(
                {
                    "field_key": binding.input_field_key,
                    "display_name": contract.get("display_name"),
                    "description": contract.get("description"),
                    "template_url": contract.get("template_url"),
                    "artifact": {
                        "id": artifact.id,
                        "artifact_type": artifact.artifact_type,
                        "uri": artifact.uri,
                        "inline_content": artifact.inline_content,
                        "metadata": artifact.metadata_json,
                    },
                }
            )
    runtime_selection = cast(
        dict[str, Any], item.context_baseline_json.get("runtime_selection") or {}
    )
    request = build_runtime_request(
        db,
        attempt_id=f"{attempt.id}:{item.id}",
        execution_key=f"conversation:{item.id}:create",
        node=node,
        bindings=bindings,
        workspace_ref=attempt.workspace_ref or "",
        interaction_mode="COLLABORATION",
        model_name=runtime_selection.get("model_name"),
        reasoning_effort=runtime_selection.get("reasoning_effort"),
        semantic_history=tuple(
            cast(
                list[dict[str, str]],
                cast(dict[str, Any], item.context_baseline_json.get("semantic_fork") or {}).get(
                    "history"
                )
                or [],
            )
        ),
        environment_image=environment.image_digest if environment else None,
        environment_id=environment.environment_id if environment else None,
        environment_version_id=environment.id if environment else None,
        environment_version_no=environment.version_no if environment else None,
        memory_materialized=memory_enabled,
    )
    if memory_enabled:
        materials = resolve_snapshot_memory(
            db,
            snapshot_id=snapshot.id,
            source_refs=source_refs,
            allowed_scopes={"USER", "PROJECT"},
        )
        materialize_runtime_memory(owner_type="CONVERSATION", owner_id=item.id, materials=materials)
    allocation = None
    if request.environment_image and get_settings().runtime_adapter != "mock":
        try:
            allocation = sandboxes.create_runtime_sandbox(
                db,
                owner_type="CONVERSATION",
                owner_id=item.id,
                image=request.environment_image,
                environment_id=request.environment_id,
                environment_version_id=request.environment_version_id,
                environment_version_no=request.environment_version_no,
                workspace_relative=request.runtime_workspace_relative,
                memory_enabled=request.memory_enabled,
                memory_working_dir_relative=request.runtime_working_dir_relative,
            )
        except BaseException:
            if request.memory_enabled:
                sandboxes.cleanup_unclaimed_runtime_memory(
                    db, owner_type="CONVERSATION", owner_id=item.id
                )
            raise
        request = replace(
            request,
            runtime_sandbox_id=allocation.id,
            runtime_resource_name=allocation.resource_name,
            runtime_base_url=allocation.base_url,
        )
    expected_version = item.state_version
    db.rollback()
    try:
        handle = get_runtime().create_conversation(request)
        if allocation is not None:
            sandboxes.mark_runtime_bound(db, allocation.id)
    except BaseException:
        if allocation is not None:
            sandboxes.request_delete_durable(db, allocation.id)
        raise
    if not lease_is_current(db, lease):
        try:
            get_runtime().cancel(handle)
        finally:
            if allocation is not None:
                sandboxes.request_delete_durable(db, allocation.id)
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
            runtime_sandbox_id=allocation.id if allocation else None,
        )
        .returning(AgentConversation.id)
        .execution_options(synchronize_session=False)
    )
    if claimed is None:
        db.rollback()
        get_runtime().cancel(handle)
        if allocation is not None:
            sandboxes.request_delete_durable(db, allocation.id)
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
    _schedule_conversation_wakeup(db, current, "BASH", 1)
    db.commit() if commit else db.flush()


def process_cleanup_conversation_runtime(
    db: Session, conversation_id: str, lease: Lease, *, commit: bool = True
) -> None:
    """Release compute owned by a terminal non-automatic conversation."""

    item = _conversation(db, conversation_id)
    if (
        item.kind == ConversationKind.AUTO
        or item.state not in {ConversationState.READ_ONLY, ConversationState.FAILED}
        or not item.runtime_conversation_id
    ):
        return
    runtime_conversation_id = item.runtime_conversation_id
    handle = RuntimeHandle(
        item.runtime_job_id or runtime_conversation_id,
        runtime_conversation_id,
        item.runtime_cursor,
    )
    adapter = item.runtime_adapter
    runtime_sandbox_id = item.runtime_sandbox_id
    db.rollback()
    runtime_for(adapter, handle).cancel(handle)
    if not lease_is_current(db, lease):
        raise RuntimeError("task lease was lost during conversation Runtime cleanup")
    current = _conversation(db, conversation_id, lock=True)
    if (
        current.state in {ConversationState.READ_ONLY, ConversationState.FAILED}
        and current.runtime_conversation_id == runtime_conversation_id
    ):
        current.runtime_job_id = None
        current.runtime_conversation_id = None
        current.runtime_cursor = None
        if (
            runtime_sandbox_id is not None
            and _runtime_sandbox_is_conversation_owned(db, runtime_sandbox_id)
            and not _runtime_sandbox_has_other_active_conversations(
                db, runtime_sandbox_id, excluding_conversation_id=current.id
            )
        ):
            sandboxes.request_delete(db, runtime_sandbox_id)
    db.commit() if commit else db.flush()


def process_stop_conversation_runtime(
    db: Session, conversation_id: str, lease: Lease, *, commit: bool = True
) -> None:
    item = _conversation(db, conversation_id)
    if item.state != ConversationState.STOPPING:
        return
    expected_version = item.state_version
    runtime_conversation_id = item.runtime_conversation_id
    runtime_sandbox_id = item.runtime_sandbox_id
    # Sandbox deletion is the authoritative force-stop. Persist it outside the
    # worker transaction before asking the Agent API to interrupt, so a wedged
    # Agent cannot keep the terminal container alive.
    if _runtime_sandbox_is_conversation_owned(
        db, runtime_sandbox_id
    ) and not _runtime_sandbox_has_other_active_conversations(
        db, runtime_sandbox_id, excluding_conversation_id=item.id
    ):
        sandboxes.request_delete_durable(db, runtime_sandbox_id)
    if runtime_conversation_id:
        handle = RuntimeHandle(
            item.runtime_job_id or runtime_conversation_id,
            runtime_conversation_id,
            item.runtime_cursor,
        )
        adapter = item.runtime_adapter
        db.rollback()
        runtime_for(adapter, handle).cancel(handle)
        if not lease_is_current(db, lease):
            raise RuntimeError("task lease was lost while stopping Agent conversation")
    current = _conversation(db, conversation_id, lock=True)
    if current.state != ConversationState.STOPPING or current.state_version != expected_version:
        db.rollback()
        return
    previous = current.state
    current.state = ConversationState.IDLE
    current.runtime_job_id = None
    current.runtime_conversation_id = None
    current.runtime_cursor = None
    latest_human = next(
        (
            message
            for message in reversed(
                list(
                    db.scalars(
                        select(AgentMessage)
                        .where(
                            AgentMessage.conversation_id == current.id,
                            AgentMessage.source == MessageSource.HUMAN,
                            AgentMessage.delivery_state == DeliveryState.DELIVERED,
                        )
                        .order_by(AgentMessage.sequence_no)
                    )
                )
            )
            if not _message_is_superseded(message)
        ),
        None,
    )
    baseline = copy.deepcopy(current.context_baseline_json or {})
    if latest_human is not None:
        baseline["stopped_turn"] = {
            "editable_message_id": latest_human.id,
            "stopped_at_version": current.state_version,
        }
    current.context_baseline_json = baseline
    current.state_version += 1
    _event(
        db,
        current,
        "CONVERSATION_STATE_CHANGED",
        {"from": previous, "to": current.state, "version": current.state_version},
    )
    db.commit() if commit else db.flush()


def record_stop_conversation_failure(
    db: Session, conversation_id: str, error: str, *, terminal: bool
) -> None:
    if not terminal:
        return
    item = _conversation(db, conversation_id, lock=True)
    if item.state != ConversationState.STOPPING:
        return
    previous = item.state
    item.state = ConversationState.FAILED
    item.state_version += 1
    _event(
        db,
        item,
        "CONVERSATION_STATE_CHANGED",
        {
            "from": previous,
            "to": item.state,
            "version": item.state_version,
            "error": error[:2000],
        },
    )
    db.flush()


def record_poll_conversation_failure(
    db: Session, conversation_id: str, error: str, *, terminal: bool
) -> None:
    """End a turn whose Runtime polling exhausted all retries.

    Without this projection the durable task can be DEAD while the conversation
    remains GENERATING forever, which makes the UI falsely report that the model
    is still thinking.
    """

    if not terminal:
        return
    item = _conversation(db, conversation_id, lock=True)
    if item.state != ConversationState.GENERATING:
        return
    previous = item.state
    item.state = ConversationState.FAILED
    item.state_version += 1
    detail = error[:2000]
    _append(
        db,
        item,
        source=MessageSource.AGENT,
        message_type=MessageType.ERROR,
        content={
            "error": {
                "code": "RUNTIME_POLL_FAILED",
                "message": detail,
            }
        },
        delivery_state=DeliveryState.DELIVERED,
        runtime_event_id=f"poll-failed:{item.id}:v{item.state_version}",
        runtime_cursor=item.runtime_cursor,
    )
    _event(
        db,
        item,
        "CONVERSATION_STATE_CHANGED",
        {
            "from": previous,
            "to": item.state,
            "version": item.state_version,
            "error": detail,
        },
    )
    db.flush()


def record_conversation_condensation_failure(
    db: Session, command_id: str, error: str, *, terminal: bool
) -> None:
    """Release a conversation when native manual condensation exhausts retries."""

    if not terminal:
        return
    command = db.scalar(
        select(RuntimeCondensationCommand)
        .where(
            RuntimeCondensationCommand.id == command_id,
            RuntimeCondensationCommand.state == "PENDING",
        )
        .with_for_update()
    )
    if command is None:
        return
    conversation = _conversation(db, command.conversation_id, lock=True)
    detail = error[:2000]
    command.state = "FAILED"
    command.state_version += 1
    command.error_code = "RUNTIME_CONDENSATION_FAILED"
    command.error_detail = detail
    command.completed_at = now()
    if conversation.state == ConversationState.CONDENSING:
        previous = conversation.state
        conversation.state = ConversationState.IDLE
        conversation.state_version += 1
        _event(
            db,
            conversation,
            "CONVERSATION_STATE_CHANGED",
            {
                "from": previous,
                "to": conversation.state,
                "version": conversation.state_version,
                "error": detail,
            },
        )
    _event(
        db,
        conversation,
        "CONVERSATION_CONDENSATION_COMMAND_FAILED",
        {"condensation_command_id": command.id, "error": detail},
    )
    db.flush()


def record_create_conversation_failure(
    db: Session, conversation_id: str, error: str, *, terminal: bool
) -> None:
    """Project a terminal child creation failure and unblock its parent batch."""

    if not terminal:
        return
    item = _conversation(db, conversation_id, lock=True)
    if item.state != ConversationState.CREATING:
        return
    previous = item.state
    item.state = ConversationState.FAILED
    item.state_version += 1
    detail = error[:2000]
    _append(
        db,
        item,
        source=MessageSource.AGENT,
        message_type=MessageType.ERROR,
        content={"error": {"code": "RUNTIME_CREATE_FAILED", "message": detail}},
        delivery_state=DeliveryState.DELIVERED,
        runtime_event_id=f"create-failed:{item.id}:v{item.state_version}",
    )
    _event(
        db,
        item,
        "CONVERSATION_STATE_CHANGED",
        {
            "from": previous,
            "to": item.state,
            "version": item.state_version,
            "error": detail,
        },
    )
    db.flush()


def _task_text(value: object, *, maximum: int) -> str | None:
    if value is None:
        return None
    text = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False, default=str)
    return text[:maximum]


def _project_runtime_subagent_task(
    db: Session,
    conversation: AgentConversation,
    *,
    cursor: str,
    payload: dict[str, Any],
) -> RuntimeSubagentTask | None:
    raw_task = payload.get("runtime_task")
    if not isinstance(raw_task, dict):
        return None
    task = cast(dict[str, Any], raw_task)
    phase = str(task.get("phase") or "")
    if phase not in {"REQUESTED", "COMPLETED", "ERROR"}:
        return None
    action_event_id = str(task.get("action_event_id") or "")
    if not action_event_id:
        raise DomainError(
            "RUNTIME_TASK_PROTOCOL_DRIFT",
            "OpenHands Task event has no action identity",
            502,
        )
    item = db.scalar(
        select(RuntimeSubagentTask)
        .where(
            RuntimeSubagentTask.conversation_id == conversation.id,
            RuntimeSubagentTask.action_event_id == action_event_id,
        )
        .with_for_update()
    )
    tool_call_id = str(task.get("tool_call_id") or "") or None
    if item is None:
        item = RuntimeSubagentTask(
            attempt_id=conversation.attempt_id,
            conversation_id=conversation.id,
            action_event_id=action_event_id,
            subagent_type=str(task.get("subagent_type") or "unknown")[:200],
        )
        db.add(item)
        db.flush()
    elif item.tool_call_id and tool_call_id and item.tool_call_id != tool_call_id:
        raise DomainError(
            "RUNTIME_TASK_PROTOCOL_DRIFT",
            "OpenHands Task tool-call identity changed",
            502,
            {"action_event_id": action_event_id},
        )

    item.tool_call_id = item.tool_call_id or tool_call_id
    if phase == "REQUESTED":
        llm_response_id = str(task.get("llm_response_id") or "") or None
        if item.llm_response_id and llm_response_id and item.llm_response_id != llm_response_id:
            raise DomainError(
                "RUNTIME_TASK_PROTOCOL_DRIFT",
                "OpenHands Task LLM response identity changed",
                502,
                {"action_event_id": action_event_id},
            )
        item.action_cursor = item.action_cursor or cursor
        item.llm_response_id = item.llm_response_id or llm_response_id
        item.subagent_type = str(task.get("subagent_type") or item.subagent_type)[:200]
        item.description = _task_text(task.get("description"), maximum=2_000)
        item.resume_task_id = _task_text(task.get("resume_task_id"), maximum=100)
        return item

    observation_event_id = str(task.get("observation_event_id") or "")
    runtime_task_id = str(task.get("task_id") or "")
    if not observation_event_id or not runtime_task_id:
        raise DomainError(
            "RUNTIME_TASK_PROTOCOL_DRIFT",
            "OpenHands Task observation has no formal identity",
            502,
            {"action_event_id": action_event_id},
        )
    if (item.observation_event_id and item.observation_event_id != observation_event_id) or (
        item.runtime_task_id and item.runtime_task_id != runtime_task_id
    ):
        raise DomainError(
            "RUNTIME_TASK_PROTOCOL_DRIFT",
            "OpenHands Task observation identity changed",
            502,
            {"action_event_id": action_event_id},
        )
    item.observation_event_id = observation_event_id
    item.observation_cursor = cursor
    item.runtime_task_id = runtime_task_id[:100]
    item.subagent_type = str(task.get("subagent_type") or item.subagent_type)[:200]
    item.native_status = str(task.get("status") or "error")[:40]
    item.state = phase
    # The Task lifecycle envelope intentionally carries no prompt/result copy.
    # Project the user-visible native Observation text already normalized at
    # the Runtime boundary, keeping one durable result without duplicating the
    # sub-agent execution input in FlowWeave.
    result = _task_text(payload.get("content"), maximum=100_000)
    if phase == "ERROR":
        item.error_detail = result or "OpenHands sub-agent task failed"
        item.result = None
    else:
        item.result = result
        item.error_detail = None
    item.completed_at = item.completed_at or now()
    return item


def _subagent_budget_limit(
    db: Session, conversation: AgentConversation, task: RuntimeSubagentTask
) -> float | None:
    """Resolve the immutable Agent Definition budget used by OpenHands 1.40.0."""

    snapshot = db.get(RunSnapshot, _attempt(db, conversation.attempt_id).snapshot_id)
    node_run, _ = _context(db, _attempt(db, conversation.attempt_id))
    if snapshot is None:
        raise DomainError("SNAPSHOT_INVALID", "Attempt Snapshot is unavailable", 409)
    node = runtime_node(
        definition=snapshot.definition_json,
        manifest=snapshot.runtime_manifest_json or {},
        expected_hash=snapshot.runtime_manifest_hash,
        snapshot_id=snapshot.id,
        instance_key=node_run.flow_node_snapshot_key,
    )
    agent_spec = cast(dict[str, Any], node.get("runtime_agent_spec") or {})
    for raw in cast(list[object], agent_spec.get("agent_definitions") or []):
        if not isinstance(raw, dict):
            continue
        raw_entry = cast(dict[str, object], raw)
        raw_config = raw_entry.get("runtime_config")
        config = cast(dict[str, Any], raw_config) if isinstance(raw_config, dict) else {}
        if str(config.get("name") or "") != task.subagent_type:
            continue
        value = config.get("max_budget_per_run")
        return (
            float(value) if isinstance(value, int | float) and not isinstance(value, bool) else None
        )
    return None


def project_runtime_task_usage(
    db: Session,
    conversation: AgentConversation,
    snapshots: tuple[RuntimeTaskUsageSnapshot, ...],
) -> None:
    """CAS-project cumulative Task stats without adding them into parent totals."""

    for snapshot in snapshots:
        tasks = list(
            db.scalars(
                select(RuntimeSubagentTask)
                .where(
                    RuntimeSubagentTask.conversation_id == conversation.id,
                    RuntimeSubagentTask.runtime_task_id == snapshot.task_id,
                )
                .with_for_update()
            )
        )
        if not tasks:
            # Stats can become visible just before the matching Observation page.
            # The next poll will retry after that formal identity is projected.
            continue
        # A resumed Task reuses the formal task_id but emits a new Action /
        # Observation invocation. Attribute the one cumulative ledger row to
        # the latest completed invocation without duplicating its cost.
        task = max(
            tasks,
            key=lambda item: (
                item.completed_at or item.created_at,
                item.observation_event_id or "",
                item.id,
            ),
        )
        existing = db.scalar(
            select(RuntimeSubagentTaskUsage)
            .where(
                RuntimeSubagentTaskUsage.conversation_id == conversation.id,
                RuntimeSubagentTaskUsage.runtime_task_id == snapshot.task_id,
            )
            .with_for_update()
        )
        counters = (
            snapshot.accumulated_cost,
            snapshot.prompt_tokens,
            snapshot.completion_tokens,
            snapshot.cache_read_tokens,
            snapshot.cache_write_tokens,
            snapshot.reasoning_tokens,
        )
        if existing is not None:
            if existing.snapshot_digest == snapshot.digest:
                continue
            previous = (
                float(existing.accumulated_cost_usd),
                existing.prompt_tokens,
                existing.completion_tokens,
                existing.cache_read_tokens,
                existing.cache_write_tokens,
                existing.reasoning_tokens,
            )
            if any(current < old for current, old in zip(counters, previous, strict=True)):
                raise DomainError(
                    "RUNTIME_TASK_USAGE_REGRESSION",
                    "OpenHands cumulative Task usage moved backwards",
                    502,
                    {"task_id": snapshot.task_id},
                )
        budget_limit = (
            float(existing.budget_limit_usd)
            if existing is not None and existing.budget_limit_usd is not None
            else _subagent_budget_limit(db, conversation, task)
        )
        budget_state = (
            "UNBOUNDED"
            if budget_limit is None
            else "EXCEEDED"
            if snapshot.accumulated_cost >= budget_limit
            else "WITHIN"
        )
        exceeded_now = budget_state == "EXCEEDED" and (
            existing is None or existing.budget_state != "EXCEEDED"
        )
        if existing is None:
            existing = RuntimeSubagentTaskUsage(
                attempt_id=conversation.attempt_id,
                conversation_id=conversation.id,
                runtime_subagent_task_id=task.id,
                runtime_task_id=snapshot.task_id,
                usage_version=1,
                budget_exceeded_at=now() if exceeded_now else None,
            )
            db.add(existing)
        else:
            existing.usage_version += 1
            existing.runtime_subagent_task_id = task.id
            if exceeded_now:
                existing.budget_exceeded_at = now()
        existing.source_cursor = snapshot.source_cursor
        existing.snapshot_digest = snapshot.digest
        existing.model_name = snapshot.model_name
        existing.accumulated_cost_usd = snapshot.accumulated_cost
        existing.prompt_tokens = snapshot.prompt_tokens
        existing.completion_tokens = snapshot.completion_tokens
        existing.cache_read_tokens = snapshot.cache_read_tokens
        existing.cache_write_tokens = snapshot.cache_write_tokens
        existing.reasoning_tokens = snapshot.reasoning_tokens
        existing.context_window = snapshot.context_window
        existing.per_turn_tokens = snapshot.per_turn_tokens
        existing.budget_limit_usd = budget_limit
        existing.budget_state = budget_state
        db.flush()
        _event(
            db,
            conversation,
            "RUNTIME_SUBAGENT_USAGE_PROJECTED",
            {
                "runtime_subagent_task_id": task.id,
                "runtime_task_id": snapshot.task_id,
                "source_cursor": snapshot.source_cursor,
                "snapshot_digest": snapshot.digest,
                "usage_version": existing.usage_version,
                "accumulated_cost_usd": snapshot.accumulated_cost,
                "prompt_tokens": snapshot.prompt_tokens,
                "completion_tokens": snapshot.completion_tokens,
                "budget_limit_usd": budget_limit,
                "budget_state": budget_state,
            },
        )
        if exceeded_now:
            _event(
                db,
                conversation,
                "RUNTIME_SUBAGENT_BUDGET_EXCEEDED",
                {
                    "runtime_subagent_task_id": task.id,
                    "runtime_task_id": snapshot.task_id,
                    "accumulated_cost_usd": snapshot.accumulated_cost,
                    "budget_limit_usd": budget_limit,
                    "control_action": "NONE_UPSTREAM_TASK_ALREADY_TERMINAL",
                },
            )


def missing_runtime_task_usage_ids(
    db: Session,
    conversation: AgentConversation,
    *,
    observed_stats_task_ids: tuple[str, ...] = (),
) -> tuple[str, ...]:
    """Return formal Task identities not yet joined to a durable lifecycle row."""

    terminal_task_ids = {
        task_id
        for task_id in db.scalars(
            select(RuntimeSubagentTask.runtime_task_id).where(
                RuntimeSubagentTask.conversation_id == conversation.id,
                RuntimeSubagentTask.state.in_(("COMPLETED", "ERROR")),
                RuntimeSubagentTask.runtime_task_id.is_not(None),
                RuntimeSubagentTask.runtime_task_id != "unknown",
            )
        )
        if task_id
    }
    projected_task_ids = set(
        db.scalars(
            select(RuntimeSubagentTaskUsage.runtime_task_id).where(
                RuntimeSubagentTaskUsage.conversation_id == conversation.id
            )
        )
    )
    return tuple(sorted((terminal_task_ids | set(observed_stats_task_ids)) - projected_task_ids))


def record_runtime_task_usage_recovery(
    db: Session,
    conversation: AgentConversation,
    *,
    missing_task_ids: tuple[str, ...],
    recovery_poll: int,
    exhausted: bool,
) -> None:
    """Append a redacted audit fact for the bounded stats/event visibility recovery."""

    _event(
        db,
        conversation,
        (
            "RUNTIME_SUBAGENT_USAGE_RECOVERY_EXHAUSTED"
            if exhausted
            else "RUNTIME_SUBAGENT_USAGE_RECOVERY_PENDING"
        ),
        {
            "runtime_task_ids": list(missing_task_ids),
            "recovery_poll": recovery_poll,
        },
    )


def _append_runtime_payload(
    db: Session,
    conversation: AgentConversation,
    *,
    cursor: str,
    event_type: str,
    payload: dict[str, Any],
) -> None:
    raw_critic = payload.get("critic_result")
    if isinstance(raw_critic, dict):
        existing_critic = db.scalar(
            select(RuntimeCriticEvaluation.id).where(
                RuntimeCriticEvaluation.conversation_id == conversation.id,
                RuntimeCriticEvaluation.runtime_event_id == cursor,
            )
        )
        if existing_critic is None:
            critic = cast(dict[str, Any], raw_critic)
            db.add(
                RuntimeCriticEvaluation(
                    attempt_id=conversation.attempt_id,
                    conversation_id=conversation.id,
                    runtime_event_id=cursor,
                    source_type=str(payload.get("source_type") or "UNKNOWN")[:40],
                    score=float(critic["score"]),
                    message=(str(critic["message"])[:2000] if critic.get("message") else None),
                )
            )
            _event(
                db,
                conversation,
                "RUNTIME_CRITIC_EVALUATED",
                {"runtime_event_id": cursor, "score": float(critic["score"])},
            )
    raw_goal = payload.get("goal_status")
    if isinstance(raw_goal, dict):
        existing_goal = db.scalar(
            select(RuntimeGoalStatus.id).where(
                RuntimeGoalStatus.conversation_id == conversation.id,
                RuntimeGoalStatus.runtime_event_id == cursor,
            )
        )
        if existing_goal is None:
            goal = cast(dict[str, Any], raw_goal)
            db.add(
                RuntimeGoalStatus(
                    attempt_id=conversation.attempt_id,
                    conversation_id=conversation.id,
                    runtime_event_id=cursor,
                    active=bool(goal["active"]),
                    status=str(goal["status"]),
                    iteration=int(goal["iteration"]),
                    max_iterations=int(goal["max_iterations"]),
                    objective=str(goal["objective"]),
                    verdict_json=(
                        cast(dict[str, Any], goal["verdict"])
                        if isinstance(goal.get("verdict"), dict)
                        else None
                    ),
                )
            )
            active_command = db.scalar(
                select(RuntimeGoalCommand)
                .where(
                    RuntimeGoalCommand.conversation_id == conversation.id,
                    RuntimeGoalCommand.state == "RUNNING",
                )
                .order_by(RuntimeGoalCommand.created_at.desc())
                .limit(1)
            )
            if active_command is not None:
                terminal = str(goal["status"]) in {"complete", "capped", "interrupted"}
                if active_command.action == "STOP" and goal["status"] == "interrupted":
                    terminal = True
                active_command.state = "SUCCEEDED" if terminal else "RUNNING"
                active_command.terminal_status = str(goal["status"]) if terminal else None
                active_command.state_version += 1
                active_command.completed_at = now() if terminal else None
            _event(
                db,
                conversation,
                "RUNTIME_GOAL_STATUS_CHANGED",
                {
                    "runtime_event_id": cursor,
                    "status": goal["status"],
                    "iteration": goal["iteration"],
                    "max_iterations": goal["max_iterations"],
                    "verdict": goal.get("verdict"),
                },
            )
    if event_type in {"CONDENSATION_REQUESTED", "CONDENSATION_COMPLETED"}:
        project_runtime_condensation(
            db, conversation, cursor=cursor, event_type=event_type, payload=payload
        )
        return
    if event_type in {"TOOL", "TOOL_CALL", "TOOL_RESULT"}:
        _project_runtime_subagent_task(db, conversation, cursor=cursor, payload=payload)
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
        ).strip()
        if not text:
            return
        content: dict[str, Any] = {"parts": [{"type": "text", "text": text}]}
        if conversation.kind != ConversationKind.AUTO:
            latest_human = db.scalar(
                select(AgentMessage)
                .where(
                    AgentMessage.conversation_id == conversation.id,
                    AgentMessage.source == MessageSource.HUMAN,
                    AgentMessage.delivery_state == DeliveryState.DELIVERED,
                )
                .order_by(AgentMessage.sequence_no.desc())
                .limit(1)
            )
            # Ignore the assistant acknowledgement emitted while an empty
            # collaboration Runtime is being initialized.  Once a human turn
            # is active, retain assistant MessageEvents as visible progress
            # updates; the Finish result remains the only formal answer.
            if latest_human is None:
                return
            content.update(
                {
                    "presentation": "progress",
                    "turn_message_id": latest_human.id,
                }
            )
        message_type = MessageType.TEXT
    elif event_type in {"TOOL", "TOOL_CALL", "TOOL_RESULT"}:
        content = {"tool": payload}
        message_type = (
            MessageType.TOOL_RESULT if event_type == "TOOL_RESULT" else MessageType.TOOL_CALL
        )
    elif event_type == "THOUGHT":
        content = {"state": payload}
        message_type = MessageType.STATE
    elif event_type == "COMPLETED":
        return
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
    def supersede_duplicate_progress(text: str) -> None:
        normalized = text.strip()
        if not normalized:
            return
        latest_human = db.scalar(
            select(AgentMessage)
            .where(
                AgentMessage.conversation_id == conversation.id,
                AgentMessage.source == MessageSource.HUMAN,
                AgentMessage.delivery_state == DeliveryState.DELIVERED,
            )
            .order_by(AgentMessage.sequence_no.desc())
            .limit(1)
        )
        if latest_human is None:
            return
        for progress in db.scalars(
            select(AgentMessage).where(
                AgentMessage.conversation_id == conversation.id,
                AgentMessage.sequence_no > latest_human.sequence_no,
                AgentMessage.source == MessageSource.AGENT,
            )
        ):
            if (
                _message_is_progress(progress)
                and not _message_is_superseded(progress)
                and _message_text(progress.content_json).strip() == normalized
            ):
                progress.content_json = {
                    **progress.content_json,
                    "superseded": True,
                    "superseded_by_final": True,
                }

    def append_once(
        *,
        runtime_event_id: str,
        message_type: str,
        content: dict[str, Any],
    ) -> None:
        existing = db.scalar(
            select(AgentMessage.id).where(
                AgentMessage.conversation_id == conversation.id,
                AgentMessage.runtime_event_id == runtime_event_id,
            )
        )
        if existing is not None:
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

    conversation.runtime_cursor = result.cursor or conversation.runtime_cursor
    if result.status == "RUNNING":
        next_state = ConversationState.GENERATING
    elif result.status == "HUMAN_INPUT_REQUIRED":
        next_state = ConversationState.WAITING_HUMAN
        if result.human_question:
            append_once(
                message_type=MessageType.TEXT,
                content={"parts": [{"type": "text", "text": result.human_question}]},
                runtime_event_id=f"human-question:{message_id}:{result.cursor or ''}",
            )
    elif result.status == "FAILED":
        next_state = ConversationState.FAILED
        append_once(
            message_type=MessageType.ERROR,
            content={"error": {"message": result.error or "Runtime conversation failed"}},
            runtime_event_id=f"conversation-error:{message_id}:{result.cursor or ''}",
        )
    else:
        next_state = ConversationState.IDLE
        text = result.final_message or (
            "\n".join(value[1] for value in result.outputs.values()) if result.outputs else ""
        )
        if text:
            supersede_duplicate_progress(text)
            append_once(
                message_type=MessageType.TEXT,
                content={
                    "presentation": "final",
                    "parts": [{"type": "text", "text": text}],
                },
                runtime_event_id=f"delivery-result:{message_id}:{result.cursor or ''}",
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


def _schedule_conversation_wakeup(
    db: Session,
    conversation: AgentConversation,
    channel: str,
    wakeup_no: int,
    *,
    cursor: str | None = None,
    delayed: bool = False,
) -> None:
    task = enqueue(
        db,
        task_type="WAIT_CONVERSATION_WAKEUP",
        aggregate_type="CONVERSATION",
        aggregate_id=conversation.id,
        idempotency_key=(
            f"wait-conversation-wakeup:{conversation.id}:v{conversation.state_version}:"
            f"{channel.lower()}:{wakeup_no}"
        ),
        payload={
            "channel": channel,
            "wakeup_no": wakeup_no,
            **({"cursor": cursor} if cursor else {}),
        },
        available_at=(
            datetime.now(UTC) + timedelta(seconds=get_settings().runtime_poll_seconds)
            if delayed
            else None
        ),
    )
    task.max_attempts = max(task.max_attempts, 100)


def process_conversation_wakeup(
    db: Session,
    conversation_id: str,
    channel: str,
    wakeup_no: int,
    cursor: str | None,
    lease: Lease,
    *,
    backoff_no: int = 0,
    commit: bool = True,
) -> None:
    if channel not in {"CONVERSATION", "BASH"}:
        raise DomainError("RUNTIME_WAKEUP_CHANNEL_INVALID", "Unknown Runtime wake-up channel", 422)
    conversation = _conversation(db, conversation_id)
    if conversation.state != ConversationState.GENERATING:
        return
    expected_version = conversation.state_version
    runtime_handle = runtime_stream_details(db, conversation_id)[1]
    db.rollback()
    try:
        wakeup = get_runtime().wait_for_wakeup(
            runtime_handle,
            channel=cast(Literal["CONVERSATION", "BASH"], channel),
            timeout_seconds=get_settings().runtime_wakeup_timeout_seconds,
            cursor=cursor,
        )
    except DomainError:
        wakeup = None
    if not lease_is_current(db, lease):
        raise RuntimeError("task lease was lost during Runtime wake-up")
    sandboxes.touch_runtime(db, conversation.runtime_sandbox_id)
    current = _conversation(db, conversation_id, lock=True)
    if current.state != ConversationState.GENERATING or current.state_version != expected_version:
        return
    if wakeup is not None and wakeup.notified and channel == "CONVERSATION":
        poll_task = enqueue(
            db,
            task_type="POLL_CONVERSATION",
            aggregate_type="CONVERSATION",
            aggregate_id=current.id,
            idempotency_key=(
                f"poll-conversation-wakeup:{current.id}:v{current.state_version}:"
                f"{channel.lower()}:{wakeup_no}"
            ),
            payload={"poll_no": wakeup_no},
        )
        poll_task.max_attempts = max(poll_task.max_attempts, 10)
    if wakeup is not None and channel == "BASH" and wakeup.events:
        for event in wakeup.events:
            cursor_id = f"bash:{event['event_id']}"
            _append_runtime_payload(
                db,
                current,
                cursor=cursor_id,
                event_type="TOOL_RESULT",
                payload={
                    "actor": "HUMAN_OR_SYSTEM",
                    "source": "DIRECT_BASH",
                    **event,
                },
            )
            _event(
                db,
                current,
                "DIRECT_BASH_ACTIVITY_OBSERVED",
                {
                    "actor": "HUMAN_OR_SYSTEM",
                    "source": "DIRECT_BASH",
                    **event,
                },
            )
    next_backoff = 0 if wakeup is not None else min(backoff_no + 1, 8)
    task = enqueue(
        db,
        task_type="WAIT_CONVERSATION_WAKEUP",
        aggregate_type="CONVERSATION",
        aggregate_id=current.id,
        idempotency_key=(
            f"wait-conversation-wakeup:{current.id}:v{current.state_version}:"
            f"{channel.lower()}:{wakeup_no + 1}"
        ),
        payload={
            "channel": channel,
            "wakeup_no": wakeup_no + 1,
            "backoff_no": next_backoff,
            **(
                {"cursor": wakeup.cursor}
                if wakeup is not None and wakeup.cursor
                else ({"cursor": cursor} if cursor else {})
            ),
        },
        available_at=datetime.now(UTC)
        + timedelta(
            seconds=(
                min(
                    get_settings().runtime_wakeup_backoff_max_seconds,
                    max(get_settings().runtime_poll_seconds, float(2**next_backoff)),
                )
                if next_backoff
                else get_settings().runtime_poll_seconds
            )
        ),
    )
    task.max_attempts = max(task.max_attempts, 100)
    db.commit() if commit else db.flush()


def process_poll_conversation(
    db: Session,
    conversation_id: str,
    poll_no: int,
    lease: Lease,
    *,
    task_usage_recovery_no: int = 0,
    commit: bool = True,
) -> None:
    conversation = _conversation(db, conversation_id)
    active_goal = db.scalar(
        select(RuntimeGoalCommand).where(
            RuntimeGoalCommand.conversation_id == conversation.id,
            RuntimeGoalCommand.state == "RUNNING",
        )
    )
    if conversation.state != ConversationState.GENERATING and active_goal is None:
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
    handle = _runtime_handle(conversation)
    adapter = conversation.runtime_adapter
    runtime_sandbox_id = conversation.runtime_sandbox_id
    active_goal_id = active_goal.id if active_goal is not None else None
    active_goal_error = active_goal.error_code if active_goal is not None else None
    active_goal_cost_limit = (
        float(active_goal.max_cost_usd) if active_goal and active_goal.max_cost_usd else None
    )
    active_goal_token_limit = active_goal.max_tokens if active_goal else None
    active_goal_baseline_tokens = active_goal.baseline_tokens if active_goal else 0
    active_goal_baseline_cost = float(active_goal.baseline_cost_usd or 0) if active_goal else 0.0
    db.rollback()
    runtime = runtime_for(adapter, handle)
    batch = runtime.read_events(handle)
    observed_cost, observed_tokens = _usage_totals(batch.usage)
    goal_tokens = (
        max(0, observed_tokens - active_goal_baseline_tokens) if active_goal_id is not None else 0
    )
    goal_cost = (
        max(0.0, observed_cost - active_goal_baseline_cost) if active_goal_id is not None else 0.0
    )
    goal_budget_exceeded = bool(
        active_goal_id is not None
        and (
            (active_goal_cost_limit is not None and goal_cost >= active_goal_cost_limit)
            or (active_goal_token_limit is not None and goal_tokens >= active_goal_token_limit)
        )
    )
    goal_budget_stop_required = bool(
        goal_budget_exceeded
        and active_goal_id is not None
        and active_goal_error != "GOAL_BUDGET_EXCEEDED"
    )
    budget_stop_batch: RuntimeEventBatch | None = None
    if goal_budget_stop_required:
        runtime.stop_goal(handle)
        budget_stop_batch = runtime.read_events(
            RuntimeHandle(
                handle.job_id,
                handle.conversation_id,
                batch.cursor or handle.cursor,
                runtime_resource_id=handle.runtime_resource_id,
                runtime_resource_name=handle.runtime_resource_name,
            )
        )
        if budget_stop_batch.events:
            batch = RuntimeEventBatch(
                events=(*batch.events, *budget_stop_batch.events),
                cursor=budget_stop_batch.cursor or batch.cursor,
                result=budget_stop_batch.result or batch.result,
                task_usage=budget_stop_batch.task_usage or batch.task_usage,
                usage=budget_stop_batch.usage or batch.usage,
            )
    result = batch.result or runtime.inspect(
        RuntimeHandle(
            handle.job_id,
            handle.conversation_id,
            batch.cursor or handle.cursor,
            runtime_resource_id=handle.runtime_resource_id,
            runtime_resource_name=handle.runtime_resource_name,
        )
    )
    sandboxes.touch_runtime(db, runtime_sandbox_id)
    if not lease_is_current(db, lease):
        raise RuntimeError("task lease was lost during conversation polling")
    current = _conversation(db, conversation_id, lock=True)
    current_attempt = _attempt(db, current.attempt_id)
    current_goal = db.scalar(
        select(RuntimeGoalCommand).where(
            RuntimeGoalCommand.id == active_goal_id,
            RuntimeGoalCommand.conversation_id == current.id,
            RuntimeGoalCommand.state == "RUNNING",
        )
    )
    if current_goal is not None and goal_budget_stop_required:
        current_goal.error_code = "GOAL_BUDGET_EXCEEDED"
        current_goal.error_detail = "The governed Goal token or cost budget was reached"
        _event(
            db,
            current,
            "RUNTIME_GOAL_BUDGET_EXCEEDED",
            {
                "goal_command_id": current_goal.id,
                "observed_cost_usd": goal_cost,
                "observed_tokens": goal_tokens,
                "max_cost_usd": active_goal_cost_limit,
                "max_tokens": active_goal_token_limit,
            },
        )
    if (
        current.state != ConversationState.GENERATING and current_goal is None
    ) or current_attempt.state in TERMINAL_ATTEMPT_STATES:
        db.rollback()
        return
    for event in batch.events:
        _append_runtime_payload(
            db, current, cursor=event.cursor, event_type=event.event_type, payload=event.payload
        )
    project_runtime_task_usage(db, current, batch.task_usage)
    missing_task_usage_ids = (
        missing_runtime_task_usage_ids(
            db,
            current,
            observed_stats_task_ids=tuple(snapshot.task_id for snapshot in batch.task_usage),
        )
        if result.status in {"COMPLETED", "FAILED"}
        else ()
    )
    if missing_task_usage_ids:
        next_recovery_no = task_usage_recovery_no + 1
        exhausted = next_recovery_no >= get_settings().runtime_task_usage_visibility_max_polls
        record_runtime_task_usage_recovery(
            db,
            current,
            missing_task_ids=missing_task_usage_ids,
            recovery_poll=next_recovery_no,
            exhausted=exhausted,
        )
        result = RuntimeResult(
            status="FAILED" if exhausted else "RUNNING",
            cursor=result.cursor or batch.cursor,
            error=(
                "OpenHands terminal Task stats remained unavailable after bounded recovery: "
                + ", ".join(missing_task_usage_ids)
                if exhausted
                else None
            ),
        )
    if result.cursor is None and batch.cursor is not None:
        result = RuntimeResult(
            status=result.status,
            outputs=result.outputs,
            final_message=result.final_message,
            human_question=result.human_question,
            cursor=batch.cursor,
            error=result.error,
        )
    current_goal = db.scalar(
        select(RuntimeGoalCommand).where(
            RuntimeGoalCommand.conversation_id == current.id,
            RuntimeGoalCommand.state == "RUNNING",
        )
    )
    if current_goal is None:
        _apply_conversation_result(
            db, current, result, message_id=f"poll:{batch.cursor or current.runtime_cursor or '0'}"
        )
    else:
        current.runtime_cursor = batch.cursor or current.runtime_cursor
    if result.status == "RUNNING" or current_goal is not None:
        poll_task = enqueue(
            db,
            task_type="POLL_CONVERSATION",
            aggregate_type="CONVERSATION",
            aggregate_id=current.id,
            idempotency_key=(
                f"poll-conversation:{current.id}:v{current.state_version}:{poll_no + 1}"
            ),
            payload={
                "poll_no": poll_no + 1,
                **(
                    {"task_usage_recovery_no": task_usage_recovery_no + 1}
                    if missing_task_usage_ids
                    else {}
                ),
            },
            available_at=datetime.now(UTC) + timedelta(seconds=get_settings().runtime_poll_seconds),
        )
        poll_task.max_attempts = max(poll_task.max_attempts, 10)
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
        conversation.state_version += 1
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
    runtime_selection = cast(dict[str, Any], content_json.get("runtime_selection") or {})
    provider = (
        resolve_runtime_provider(
            db,
            _attempt_node(db, attempt),
            cast(str | None, runtime_selection.get("model_name")),
            cast(str | None, runtime_selection.get("reasoning_effort")),
        )
        if runtime_selection.get("model_name") and get_settings().runtime_adapter != "mock"
        else None
    )
    steering = message.delivery_mode == DeliveryMode.INTERRUPT_AND_RESUME
    db.flush()
    db.rollback()
    content, image_urls = _runtime_message_payload(
        content_json, workspace_root=get_settings().workspace_root.resolve()
    )
    try:
        runtime = get_runtime()
        if steering:
            runtime.interrupt(handle)
        if provider is not None:
            runtime.switch_model(handle, provider)
        result = runtime.send_message(handle, content, image_urls)
        sandboxes.touch_runtime(db, conversation.runtime_sandbox_id)
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
        or current.state != ConversationState.GENERATING
        or current_attempt.state in TERMINAL_ATTEMPT_STATES
    ):
        db.rollback()
        return
    current_message.delivery_state = DeliveryState.DELIVERED
    current_message.content_json = {**current_message.content_json, "presentation": "chat"}
    current_message.delivered_at = now()
    current_message.runtime_cursor = result.cursor
    current.runtime_cursor = result.cursor
    if runtime_selection:
        baseline = copy.deepcopy(current.context_baseline_json or {})
        baseline["runtime_selection"] = runtime_selection
        current.context_baseline_json = baseline
    _event(
        db,
        current,
        "AGENT_MESSAGE_DELIVERY_CHANGED",
        {"message_id": message_id, "delivery_state": current_message.delivery_state},
    )
    if not steering or result.status != "RUNNING":
        _apply_conversation_result(db, current, result, message_id=message_id)
    if result.status == "RUNNING" and not steering:
        poll_task = enqueue(
            db,
            task_type="POLL_CONVERSATION",
            aggregate_type="CONVERSATION",
            aggregate_id=current.id,
            idempotency_key=f"poll-conversation:{current.id}:v{current.state_version}:1",
            payload={"poll_no": 1},
            available_at=datetime.now(UTC) + timedelta(seconds=get_settings().runtime_poll_seconds),
        )
        poll_task.max_attempts = max(poll_task.max_attempts, 10)
        _schedule_conversation_wakeup(db, current, "CONVERSATION", 1)
        _schedule_conversation_wakeup(db, current, "BASH", 1)
    else:
        _schedule_next_message(db, current)
    db.commit() if commit else db.flush()
