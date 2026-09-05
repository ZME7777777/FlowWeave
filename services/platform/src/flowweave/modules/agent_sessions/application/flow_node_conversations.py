from __future__ import annotations

import base64
import hashlib
from dataclasses import asdict, replace
from pathlib import Path, PurePosixPath
from typing import Any, cast
from uuid import UUID, uuid4

from sqlalchemy import delete, func, select, update
from sqlalchemy.orm import Session

from flowweave.modules.agent_sessions import public as agent_sessions
from flowweave.modules.agent_sessions.application.conversations import (
    AGENT_WORKSPACE_CONDENSER_MAX_EVENTS,
    ATTACHMENT_PATH,
    PROACTIVE_COMPACTION_RATIO,
    enqueue_title_task,
    frozen_runtime_capability,
    initial_user_event_id,
    message_payload,
    normalized_first_sentence,
    project_conversation_references,
    record_message_attachments,
    validate_attachment_owners,
)
from flowweave.modules.agent_sessions.application.deletion import delete_binding_records
from flowweave.modules.agent_sessions.application.flow_node_locator import (
    active_runtime_handle,
    bind_openhands_conversation,
    binding_locator,
)
from flowweave.modules.agent_sessions.application.runtime_config import (
    FrozenSessionConfig,
    build_agent_spec,
    config_from_binding,
    provider_for_config,
    reserve_flow_node_binding,
)
from flowweave.modules.agent_sessions.public import (
    AgentConversationBinding,
    AgentConversationCapability,
    AgentConversationCommand,
    AgentConversationMessageAttachment,
)
from flowweave.modules.agent_workspaces import public as agent_workspace_host
from flowweave.modules.catalog.public import resolve_version
from flowweave.modules.environments.public import (
    lock_referenceable_version,
    validate_runtime_manifest,
)
from flowweave.modules.model_providers.public import has_connected_default_model
from flowweave.modules.sandboxes import public as sandboxes
from flowweave.modules.tasks.public import enqueue
from flowweave.runtime.base import RuntimeCondenser, RuntimeEventBatch, RuntimeHandle, RuntimeResult
from flowweave.runtime.dependencies import get_runtime
from flowweave.runtime.manifest import runtime_node
from flowweave.runtime.request import (
    build_runtime_request,
    runtime_provider,
)
from flowweave.runtime.workspace import (
    agent_workspace_capability_marketplace_name,
    materialize_agent_workspace_capability_marketplace,
)
from flowweave.shared.application.transactions import finish
from flowweave.shared.database import now
from flowweave.shared.errors import DomainError, conflict, not_found
from flowweave.shared.models import (
    AgentWorkDirectoryVersion,
    AttemptState,
    FlowRun,
    FlowRunState,
    HumanAction,
    NodeAttempt,
    NodeRun,
    RunEvent,
    RunSnapshot,
)
from flowweave.shared.schemas import (
    ConversationCreateWrite,
    ConversationQuestionWrite,
    FlowRunConversationCreateWrite,
)
from flowweave.shared.settings import get_settings


def _attempt(db: Session, attempt_id: str) -> NodeAttempt:
    item = db.get(NodeAttempt, attempt_id)
    if item is None:
        raise not_found("node_attempt", attempt_id)
    return item


def _attempt_context(db: Session, attempt: NodeAttempt) -> tuple[NodeRun, FlowRun, RunSnapshot]:
    node_run = db.get(NodeRun, attempt.node_run_id)
    snapshot = db.get(RunSnapshot, attempt.snapshot_id)
    if node_run is None:
        raise not_found("node_run", attempt.node_run_id)
    run = db.get(FlowRun, node_run.flow_run_id)
    if run is None:
        raise not_found("flow_run", node_run.flow_run_id)
    if snapshot is None:
        raise DomainError("SNAPSHOT_INVALID", "Attempt Snapshot is unavailable", 409)
    return node_run, run, snapshot


_FLOW_NODE = "FLOW_NODE"
_MANUAL_NODE_CONTEXT_ID = "__node_context_prompt__"
_RUNTIME_PROJECT = PurePosixPath("/runtime/workspace/project")


def _node_context_suffix(db: Session, *, snapshot: RunSnapshot, attempt_id: str | None) -> str:
    """Render the node's already-frozen Context into the native system suffix."""

    if not attempt_id:
        return ""
    attempt = _attempt(db, attempt_id)
    node_run_id = attempt.node_run_id
    node_run = db.get(NodeRun, node_run_id)
    if node_run is None:
        raise DomainError("NODE_RUN_NOT_FOUND", "节点执行记录不可用", 409)
    node = runtime_node(
        definition=snapshot.definition_json,
        manifest=snapshot.runtime_manifest_json or {},
        expected_hash=snapshot.runtime_manifest_hash,
        snapshot_id=snapshot.id,
        instance_key=node_run.flow_node_snapshot_key,
    )
    asset = cast(dict[str, Any], node.get("asset") or {})
    executor = cast(dict[str, Any], asset.get("executor") or {})
    selected = set(attempt.context_ids_json) if attempt.context_ids_json is not None else None
    parts = [
        str(executor.get("context_prompt") or "").strip()
        if selected is None or _MANUAL_NODE_CONTEXT_ID in selected
        else ""
    ]
    raw_contexts = asset.get("context_capabilities")
    if isinstance(raw_contexts, list):
        for raw_context in cast(list[object], raw_contexts):
            if not isinstance(raw_context, dict):
                raise DomainError("SNAPSHOT_CONTEXT_INVALID", "节点 Context Snapshot 无效", 409)
            item = cast(dict[str, Any], raw_context)
            if selected is not None and str(item.get("id") or "") not in selected:
                continue
            text = str(item.get("text") or "").strip()
            if not text:
                raise DomainError("SNAPSHOT_CONTEXT_INVALID", "节点 Context 内容缺失", 409)
            parts.append(f"[{str(item.get('capability_key') or 'Context')}]\n{text}")
    rendered = [part for part in parts if part]
    return "\n\n".join(("节点上下文（仅作系统级会话背景）：", *rendered)) if rendered else ""


def _runtime_working_directory(request: Any) -> str:
    """Return the shared Agent project, never a node Attempt data path."""

    del request
    return str(_RUNTIME_PROJECT)


def _node_capability_root(
    db: Session,
    *,
    flow_run_id: str,
    attempt_id: str,
    manifest_digest: str,
    relative_parts: tuple[str, ...],
) -> Path:
    """Resolve capability storage from the persisted workspace ownership."""

    workspace = sandboxes.node_attempt_workspace_context(
        db, flow_run_id=flow_run_id, node_attempt_id=attempt_id
    )
    if workspace.attempt_owned:
        sandboxes.runtime_allocation_for_node_attempt(
            db,
            flow_run_id=flow_run_id,
            node_attempt_id=attempt_id,
            manifest_digest=manifest_digest,
        )
        return sandboxes.node_attempt_capability_path(attempt_id, manifest_digest, *relative_parts)
    runtime_owner_id = sandboxes.runtime_owner_flow_run_id(db, flow_run_id)
    sandboxes.runtime_allocation_for_flow_run(db, runtime_owner_id, manifest_digest=manifest_digest)
    return sandboxes.flow_run_capability_path(runtime_owner_id, manifest_digest, *relative_parts)


def _is_gate_sidecar(item: AgentConversationBinding) -> bool:
    return item.create_idempotency_key.startswith("gate-sidecar:")


def _binding(
    db: Session,
    binding_id: str,
    *,
    lock: bool = False,
    allow_gate_sidecar: bool = False,
) -> AgentConversationBinding:
    query = select(AgentConversationBinding).where(
        AgentConversationBinding.id == binding_id,
        AgentConversationBinding.host_kind == _FLOW_NODE,
    )
    if lock:
        query = query.with_for_update()
    item = db.scalar(query)
    if item is None:
        raise not_found("flow_run_conversation_binding", binding_id)
    if _is_gate_sidecar(item) and not allow_gate_sidecar:
        raise not_found("flow_run_conversation_binding", binding_id)
    return item


def _binding_for_run(
    db: Session,
    flow_run_id: str,
    binding_id: str,
    *,
    lock: bool = False,
    allow_gate_sidecar: bool = False,
) -> AgentConversationBinding:
    item = _binding(db, binding_id, lock=lock, allow_gate_sidecar=allow_gate_sidecar)
    if item.flow_run_id != flow_run_id:
        raise not_found("flow_run_conversation_binding", binding_id)
    return item


def _binding_for_attempt(
    db: Session,
    *,
    flow_run_id: str,
    attempt_id: str,
    binding_id: str,
    lock: bool = False,
    allow_gate_sidecar: bool = False,
) -> AgentConversationBinding:
    """Authorize the node entry, then load the FlowRun-owned session."""

    agent_sessions.resolve_flow_node_session_host(
        db, flow_run_id=flow_run_id, attempt_id=attempt_id, require_start_permission=False
    )
    item = _binding_for_run(
        db,
        flow_run_id,
        binding_id,
        lock=lock,
        allow_gate_sidecar=allow_gate_sidecar,
    )
    if item.node_attempt_id != attempt_id:
        raise not_found("flow_run_conversation_binding", binding_id)
    return item


def node_conversation_binding(
    db: Session,
    *,
    flow_run_id: str,
    attempt_id: str,
    binding_id: str,
) -> AgentConversationBinding:
    """Verify and return one shared binding in its node-Attempt scope."""

    return _binding_for_attempt(
        db,
        flow_run_id=flow_run_id,
        attempt_id=attempt_id,
        binding_id=binding_id,
    )


def _binding_dict(item: AgentConversationBinding) -> dict[str, Any]:
    return {
        "id": item.id,
        "flow_run_id": item.flow_run_id,
        "runtime_session_id": item.runtime_session_id,
        "openhands_conversation_id": item.openhands_conversation_id,
        "display_label": item.display_title,
        "created_at": item.created_at.isoformat(),
        "last_connected_at": item.last_connected_at.isoformat() if item.last_connected_at else None,
    }


def _node_session_dict(db: Session, item: AgentConversationBinding) -> dict[str, Any]:
    """Project a FlowRun binding into the shared Workbench DTO.

    The FlowRun compatibility API still exposes its historical locator shape.
    The node-host gateway, however, must never force a second client-side
    session model merely because its host lineage differs.
    """

    work_directory_id = (
        db.scalar(
            select(AgentWorkDirectoryVersion.work_directory_id).where(
                AgentWorkDirectoryVersion.id == item.work_directory_version_id
            )
        )
        if item.work_directory_version_id
        else None
    )
    return {
        "id": item.id,
        "display_title": item.display_title,
        "title_state": item.title_state,
        "model_provider_id": item.model_provider_id,
        "model_name": item.model_name,
        "reasoning_effort": item.reasoning_effort,
        "work_directory_version_id": item.work_directory_version_id,
        "work_directory_id": work_directory_id,
        "working_directory": item.working_directory,
        "capabilities": [
            {
                "id": capability.capability_version_id,
                "capability_type": capability.capability_type,
                "capability_key": capability.capability_key,
                "digest": capability.digest,
            }
            for capability in db.scalars(
                select(AgentConversationCapability)
                .where(AgentConversationCapability.binding_id == item.id)
                .order_by(AgentConversationCapability.position)
            )
        ],
        "streaming_callback_ready": item.streaming_callback_ready,
        "lifecycle": item.lifecycle,
        "created_at": item.created_at.isoformat(),
        "updated_at": item.updated_at.isoformat(),
        "last_connected_at": (
            item.last_connected_at.isoformat() if item.last_connected_at else None
        ),
    }


def node_host_details(db: Session, *, flow_run_id: str, attempt_id: str) -> dict[str, Any]:
    """Return the browser-safe identity of one verified node-session host.

    It intentionally contains neither a container identity nor a filesystem
    host path.  The attempt id is the only host id a node Workbench needs.
    """

    host = agent_sessions.resolve_flow_node_session_host(
        db,
        flow_run_id=flow_run_id,
        attempt_id=attempt_id,
        require_start_permission=False,
        ensure_startable_runtime=True,
    )
    asset = cast(dict[str, Any], host.node.get("asset") or {})
    node_name = str(
        host.node.get("display_name")
        or host.node.get("name")
        or asset.get("display_name")
        or asset.get("name")
        or host.node.get("instance_key")
        or "节点"
    ).strip()
    return {
        "id": flow_run_id,
        "display_name": node_name or "节点会话",
        "default_model_provider_id": None,
        # Host resolution has already fenced this specific Attempt to its
        # active generation. Do not project FlowRun-level Runtime status here:
        # a FlowRun may now have several independent Attempt Runtimes.
        "desired_state": "RUNNING",
        "updated_at": now().isoformat(),
    }


def node_runtime_status(db: Session, *, flow_run_id: str, attempt_id: str) -> dict[str, Any]:
    """Project this Attempt Runtime health into the shared host DTO.

    Runtime generation and provider endpoint details remain server-only.
    """

    agent_sessions.resolve_flow_node_session_host(
        db,
        flow_run_id=flow_run_id,
        attempt_id=attempt_id,
        require_start_permission=False,
    )
    return {
        "state": "ACTIVE",
        "write_available": True,
        "message": None,
        "updated_at": now().isoformat(),
    }


def list_node_session_views(
    db: Session, *, flow_run_id: str, attempt_id: str
) -> list[dict[str, Any]]:
    """List all sessions owned by the FlowRun reached through this node."""

    agent_sessions.resolve_flow_node_session_host(
        db,
        flow_run_id=flow_run_id,
        attempt_id=attempt_id,
        require_start_permission=False,
    )
    items = db.scalars(
        select(AgentConversationBinding)
        .where(
            AgentConversationBinding.host_kind == _FLOW_NODE,
            AgentConversationBinding.flow_run_id == flow_run_id,
            AgentConversationBinding.node_attempt_id == attempt_id,
            AgentConversationBinding.lifecycle == "ACTIVE",
            ~AgentConversationBinding.create_idempotency_key.like("gate-sidecar:%"),
        )
        .order_by(
            AgentConversationBinding.updated_at.desc(),
            AgentConversationBinding.created_at.desc(),
            AgentConversationBinding.id.desc(),
        )
    )
    return [_node_session_dict(db, item) for item in items]


def get_node_session_view(
    db: Session, *, flow_run_id: str, attempt_id: str, binding_id: str
) -> dict[str, Any]:
    item = _binding_for_attempt(
        db, flow_run_id=flow_run_id, attempt_id=attempt_id, binding_id=binding_id
    )
    item.last_connected_at = now()
    db.flush()
    return _node_session_dict(db, item)


def list_conversations(db: Session, attempt_id: str) -> list[dict[str, Any]]:
    """List the FlowRun's locators; an Attempt is context, never the owner."""

    attempt = _attempt(db, attempt_id)
    _, run, _ = _attempt_context(db, attempt)
    items = db.scalars(
        select(AgentConversationBinding)
        .where(
            AgentConversationBinding.host_kind == _FLOW_NODE,
            AgentConversationBinding.flow_run_id == run.id,
            AgentConversationBinding.lifecycle == "ACTIVE",
            ~AgentConversationBinding.create_idempotency_key.like("gate-sidecar:%"),
        )
        .order_by(
            AgentConversationBinding.updated_at.desc(),
            AgentConversationBinding.created_at.desc(),
            AgentConversationBinding.id.desc(),
        )
    )
    return [_binding_dict(item) for item in items]


def list_node_conversations(
    db: Session, *, flow_run_id: str, attempt_id: str
) -> list[dict[str, Any]]:
    """List FlowRun-owned sessions through one verified node entry."""

    agent_sessions.resolve_flow_node_session_host(
        db,
        flow_run_id=flow_run_id,
        attempt_id=attempt_id,
        require_start_permission=False,
    )
    items = db.scalars(
        select(AgentConversationBinding)
        .where(
            AgentConversationBinding.host_kind == _FLOW_NODE,
            AgentConversationBinding.flow_run_id == flow_run_id,
            AgentConversationBinding.lifecycle == "ACTIVE",
            ~AgentConversationBinding.create_idempotency_key.like("gate-sidecar:%"),
        )
        .order_by(
            AgentConversationBinding.updated_at.desc(),
            AgentConversationBinding.created_at.desc(),
            AgentConversationBinding.id.desc(),
        )
    )
    return [_binding_dict(item) for item in items]


def get_node_conversation(
    db: Session, *, flow_run_id: str, attempt_id: str, binding_id: str
) -> dict[str, Any]:
    item = _binding_for_attempt(
        db,
        flow_run_id=flow_run_id,
        attempt_id=attempt_id,
        binding_id=binding_id,
    )
    item.last_connected_at = now()
    db.flush()
    return _binding_dict(item)


def patch_node_conversation(
    db: Session, *, flow_run_id: str, attempt_id: str, binding_id: str, title: str
) -> dict[str, Any]:
    item = _binding_for_attempt(
        db,
        flow_run_id=flow_run_id,
        attempt_id=attempt_id,
        binding_id=binding_id,
        lock=True,
    )
    item.display_title = title.strip() or None
    item.last_connected_at = now()
    finish(db)
    return _binding_dict(item)


def list_flow_run_conversations(db: Session, flow_run_id: str) -> list[dict[str, Any]]:
    if db.get(FlowRun, flow_run_id) is None:
        raise not_found("flow_run", flow_run_id)
    items = db.scalars(
        select(AgentConversationBinding)
        .where(
            AgentConversationBinding.host_kind == _FLOW_NODE,
            AgentConversationBinding.flow_run_id == flow_run_id,
            AgentConversationBinding.lifecycle == "ACTIVE",
            ~AgentConversationBinding.create_idempotency_key.like("gate-sidecar:%"),
        )
        .order_by(
            AgentConversationBinding.updated_at.desc(),
            AgentConversationBinding.created_at.desc(),
            AgentConversationBinding.id.desc(),
        )
    )
    return [_binding_dict(item) for item in items]


def get_conversation(db: Session, binding_id: str) -> dict[str, Any]:
    item = _binding(db, binding_id)
    item.last_connected_at = now()
    db.flush()
    return _binding_dict(item)


def get_flow_run_conversation(db: Session, flow_run_id: str, binding_id: str) -> dict[str, Any]:
    item = _binding_for_run(db, flow_run_id, binding_id)
    item.last_connected_at = now()
    db.flush()
    return _binding_dict(item)


def patch_conversation(db: Session, binding_id: str, title: str) -> dict[str, Any]:
    item = _binding(db, binding_id)
    item.display_title = title.strip() or None
    item.last_connected_at = now()
    finish(db)
    return _binding_dict(item)


def patch_flow_run_conversation(
    db: Session, flow_run_id: str, binding_id: str, title: str
) -> dict[str, Any]:
    _binding_for_run(db, flow_run_id, binding_id)
    return patch_conversation(db, binding_id, title)


def _create_native_conversation(
    db: Session,
    *,
    run: FlowRun,
    snapshot: RunSnapshot,
    title: str | None,
    idempotency_key: str,
    binding_id: str,
    node_run_id: str | None = None,
    attempt_id: str | None = None,
    work_directory_version_id: str | None = None,
    runtime_working_directory: str | None = None,
    session_config: FrozenSessionConfig | None = None,
    session_binding_id: str | None = None,
) -> dict[str, Any]:
    if run.state in {"COMPLETED", "CANCELLED"}:
        raise DomainError(
            "FLOW_RUN_TERMINAL",
            "A completed or cancelled FlowRun cannot create Conversations",
            409,
            {"flow_run_id": run.id, "state": run.state},
        )
    count = db.scalar(
        select(func.count(AgentConversationBinding.id)).where(
            AgentConversationBinding.host_kind == _FLOW_NODE,
            AgentConversationBinding.flow_run_id == run.id,
        )
    )
    if int(count or 0) >= get_settings().conversation_limit_per_flow_run:
        raise DomainError("CONVERSATION_LIMIT_REACHED", "FlowRun conversation limit reached", 422)
    if (
        not run.environment_version_id
        or not snapshot.environment_version_id
        or snapshot.environment_version_id != run.environment_version_id
    ):
        raise DomainError(
            "RUN_ENVIRONMENT_REQUIRED",
            "The FlowRun and Snapshot must share one frozen Environment Version",
            409,
        )
    environment = lock_referenceable_version(db, run.environment_version_id)
    if environment is None:
        raise DomainError(
            "RUN_ENVIRONMENT_VERSION_INVALID",
            "The frozen FlowRun Environment Version is unavailable",
            409,
            {"environment_version_id": run.environment_version_id},
        )
    validate_runtime_manifest(environment.manifest_json, environment_version_id=environment.id)
    if not attempt_id:
        raise DomainError(
            "NODE_CONVERSATION_CONTEXT_REQUIRED",
            "A FlowRun Conversation must belong to a Node Attempt",
            409,
        )
    connection = sandboxes.active_node_runtime_connection(
        db, flow_run_id=run.id, node_attempt_id=attempt_id
    )
    host_root = _node_capability_root(
        db,
        flow_run_id=run.id,
        attempt_id=attempt_id,
        manifest_digest=snapshot.runtime_manifest_hash,
        relative_parts=("conversations", session_binding_id or binding_id),
    )
    working_directory = runtime_working_directory or str(_RUNTIME_PROJECT)
    item = reserve_flow_node_binding(
        db,
        runtime_session_id=connection.runtime_session_id,
        flow_run_id=run.id,
        node_run_id=node_run_id or "",
        node_attempt_id=attempt_id or "",
        working_directory=working_directory,
        create_idempotency_key=idempotency_key,
        display_title=title,
        work_directory_version_id=work_directory_version_id,
        config=session_config,
        binding_id=session_binding_id,
    )
    config = config_from_binding(db, item)
    provider = provider_for_config(db, config)
    if host_root.name != item.id:
        host_root = _node_capability_root(
            db,
            flow_run_id=run.id,
            attempt_id=attempt_id,
            manifest_digest=snapshot.runtime_manifest_hash,
            relative_parts=("conversations", item.id),
        )
    runtime_root = Path(
        sandboxes.openhands_flow_run_capability_path(
            snapshot.runtime_manifest_hash, "conversations", item.id
        )
    )
    agent_spec = build_agent_spec(
        config,
        provider=provider,
        binding_id=item.id,
        working_directory=working_directory,
        host_root=host_root,
        runtime_root=runtime_root,
        system_message_suffix_append=_node_context_suffix(
            db, snapshot=snapshot, attempt_id=attempt_id
        ),
    )
    request = build_runtime_request(
        db,
        flow_run_id=run.id,
        runtime_manifest_hash=snapshot.runtime_manifest_hash,
        attempt_id=binding_id,
        execution_key=f"flow-run:{run.id}:conversation:create",
        node={},
        bindings=[],
        workspace_ref=working_directory,
        interaction_mode="COLLABORATION",
        environment_image=environment.image_digest,
        environment_id=environment.environment_id,
        environment_version_id=environment.id,
        environment_version_no=environment.version_no,
        agent_spec=agent_spec,
        conversation_id=item.openhands_conversation_id,
        node_attempt_id=attempt_id,
    )
    request = replace(
        request,
        workspace_ref=runtime_working_directory or _runtime_working_directory(request),
        runtime_sandbox_id=connection.managed_runtime_id,
        runtime_resource_name=connection.resource_name,
        runtime_base_url=f"http://{connection.resource_name}:8000",
    )
    handle = get_runtime().create_conversation(request)
    if handle.conversation_id != item.openhands_conversation_id:
        raise DomainError("AGENT_CONVERSATION_IDENTITY_DRIFT", "会话身份校验失败", 409)
    get_runtime().reload_conversation(handle)
    item = bind_openhands_conversation(
        db,
        flow_run_id=run.id,
        openhands_conversation_id=handle.conversation_id,
        display_label=title,
        binding_id=binding_id,
        node_run_id=node_run_id,
        node_attempt_id=attempt_id,
        working_directory=runtime_working_directory or _runtime_working_directory(request),
        work_directory_version_id=work_directory_version_id,
    )
    db.add(
        HumanAction(
            flow_run_id=run.id,
            node_run_id=node_run_id,
            attempt_id=attempt_id,
            action_type="CREATE_FLOW_RUN_CONVERSATION",
            idempotency_key=idempotency_key,
            payload_json={"binding_id": item.id},
        )
    )
    finish(db)
    return _binding_dict(item)


def create_conversation(
    db: Session,
    attempt_id: str,
    payload: ConversationCreateWrite,
    idempotency_key: str,
    *,
    host: agent_sessions.FlowNodeSessionHost | None = None,
    session_config: FrozenSessionConfig | None = None,
) -> dict[str, Any]:
    """Create one OpenHands-native Conversation in the FlowRun Runtime.

    No platform Conversation row is created before or after the Runtime call.
    The only durable content-plane fact is the returned OpenHands identity.
    """

    existing = db.scalar(select(HumanAction).where(HumanAction.idempotency_key == idempotency_key))
    if existing is not None:
        binding_id = str((existing.payload_json or {}).get("binding_id") or "")
        if binding_id:
            return get_conversation(db, binding_id)
        raise conflict("conversation creation outcome is unavailable")

    attempt = _attempt(db, attempt_id)
    node_run, run, snapshot = _attempt_context(db, attempt)
    current_run = db.get(FlowRun, run.id)
    if current_run is None:
        raise not_found("flow_run", run.id)
    run = current_run
    if attempt.state_version != payload.expected_attempt_state_version:
        raise conflict(
            "attempt was modified",
            expected=payload.expected_attempt_state_version,
            actual=attempt.state_version,
        )
    if host is not None:
        if host.attempt_id != attempt.id or host.flow_run_id != run.id:
            raise DomainError(
                "NODE_CONVERSATION_CONTEXT_MISMATCH",
                "The selected node Attempt does not belong to this FlowRun",
                409,
                {"flow_run_id": run.id, "node_attempt_id": attempt.id},
            )
    # The FlowRun project root stays shared, while optional logical work
    # directories are owned by this Attempt and cannot be selected by another
    # node entry.
    work_directory_version_id, runtime_working_directory = (
        agent_workspace_host.flow_run_conversation_work_directory_context(
            db, run.id, attempt.id, payload.work_directory_id
        )
    )
    request_owner_id = str(uuid4())
    return _create_native_conversation(
        db,
        run=run,
        snapshot=snapshot,
        title=payload.title,
        idempotency_key=idempotency_key,
        binding_id=request_owner_id,
        node_run_id=node_run.id,
        attempt_id=attempt.id,
        work_directory_version_id=work_directory_version_id,
        runtime_working_directory=runtime_working_directory,
        session_config=session_config,
    )


def create_flow_run_conversation(
    db: Session,
    flow_run_id: str,
    payload: FlowRunConversationCreateWrite,
    idempotency_key: str,
    *,
    session_config: FrozenSessionConfig | None = None,
) -> dict[str, Any]:
    """Create a Conversation only from an explicitly selected node Attempt."""

    existing = db.scalar(select(HumanAction).where(HumanAction.idempotency_key == idempotency_key))
    if existing is not None:
        binding_id = str((existing.payload_json or {}).get("binding_id") or "")
        if binding_id:
            return get_conversation(db, binding_id)
        raise conflict("conversation creation outcome is unavailable")
    host = agent_sessions.resolve_flow_node_session_host(
        db,
        flow_run_id=flow_run_id,
        attempt_id=payload.node_attempt_id,
        require_start_permission=True,
    )
    attempt = _attempt(db, host.attempt_id)
    create_kwargs: dict[str, Any] = {"host": host}
    if session_config is not None:
        create_kwargs["session_config"] = session_config
    return create_conversation(
        db,
        attempt.id,
        ConversationCreateWrite(
            title=payload.title,
            expected_attempt_state_version=attempt.state_version,
            work_directory_id=payload.work_directory_id,
        ),
        idempotency_key,
        **create_kwargs,
    )


def create_node_conversation(
    db: Session,
    *,
    flow_run_id: str,
    attempt_id: str,
    title: str | None,
    work_directory_id: str | None,
    idempotency_key: str,
    session_config: FrozenSessionConfig | None = None,
) -> dict[str, Any]:
    """Create through the node-host route contract."""

    return create_flow_run_conversation(
        db,
        flow_run_id,
        FlowRunConversationCreateWrite(
            node_attempt_id=attempt_id,
            title=title,
            work_directory_id=work_directory_id,
        ),
        idempotency_key,
        session_config=session_config,
    )


def _node_bootstrap_command(
    db: Session, *, flow_run_id: str, attempt_id: str, idempotency_key: str
) -> tuple[AgentConversationBinding | None, AgentConversationCommand | None]:
    binding = db.scalar(
        select(AgentConversationBinding)
        .where(AgentConversationBinding.create_idempotency_key == idempotency_key)
        .with_for_update()
    )
    if binding is None:
        return None, None
    if (
        binding.host_kind != _FLOW_NODE
        or binding.flow_run_id != flow_run_id
        or binding.node_attempt_id != attempt_id
    ):
        raise DomainError("AGENT_CONVERSATION_COMMAND_CONFLICT", "会话创建请求冲突", 409)
    command = db.scalar(
        select(AgentConversationCommand)
        .where(
            AgentConversationCommand.binding_id == binding.id,
            AgentConversationCommand.command_type == "CREATE",
        )
        .with_for_update()
    )
    if command is None:
        raise DomainError("AGENT_CONVERSATION_BOOTSTRAP_INVALID", "会话创建命令数据不完整", 409)
    return binding, command


def _delete_node_bootstrap_reservation(
    db: Session, binding: AgentConversationBinding, command: AgentConversationCommand
) -> None:
    """Remove a definitively failed node bootstrap before it becomes visible."""

    db.execute(
        delete(AgentConversationMessageAttachment).where(
            AgentConversationMessageAttachment.binding_id == binding.id
        )
    )
    db.execute(
        delete(AgentConversationCapability).where(
            AgentConversationCapability.binding_id == binding.id
        )
    )
    db.delete(command)
    db.flush()
    db.delete(binding)
    db.commit()


def _activate_node_bootstrap(
    db: Session,
    *,
    binding: AgentConversationBinding,
    command: AgentConversationCommand,
    initial_event_id: str,
    content: str,
    attachments: tuple[dict[str, str | int], ...],
) -> dict[str, Any]:
    binding.initial_user_event_id = initial_event_id
    binding.display_title = normalized_first_sentence(content)
    binding.title_state = "PENDING"
    binding.lifecycle = "ACTIVE"
    activated_at = now()
    binding.last_connected_at = activated_at
    binding.updated_at = activated_at
    command.state = "SUCCEEDED"
    command.updated_at = binding.updated_at
    record_message_attachments(db, binding, initial_event_id, content, attachments)
    enqueue_title_task(db, binding, content)
    db.add(
        HumanAction(
            flow_run_id=binding.flow_run_id,
            node_run_id=binding.node_run_id,
            attempt_id=binding.node_attempt_id,
            action_type="CREATE_FLOW_RUN_CONVERSATION",
            idempotency_key=binding.create_idempotency_key,
            payload_json={"binding_id": binding.id},
        )
    )
    db.commit()
    return {
        "conversation": _node_session_dict(db, binding),
        "accepted": True,
        "cursor": initial_event_id,
    }


def _create_or_reload_node_bootstrap(
    db: Session,
    *,
    binding: AgentConversationBinding,
    run: FlowRun,
    snapshot: RunSnapshot,
    allow_existing: bool,
) -> tuple[RuntimeHandle, str | None]:
    """Create one reserved native Conversation, or recover its fixed UUID."""

    if (
        not run.environment_version_id
        or not snapshot.environment_version_id
        or snapshot.environment_version_id != run.environment_version_id
    ):
        raise DomainError(
            "RUN_ENVIRONMENT_REQUIRED",
            "The FlowRun and Snapshot must share one frozen Environment Version",
            409,
        )
    environment = lock_referenceable_version(db, run.environment_version_id)
    if environment is None:
        raise DomainError(
            "RUN_ENVIRONMENT_VERSION_INVALID",
            "The frozen FlowRun Environment Version is unavailable",
            409,
            {"environment_version_id": run.environment_version_id},
        )
    validate_runtime_manifest(environment.manifest_json, environment_version_id=environment.id)
    if not binding.node_attempt_id:
        raise DomainError(
            "RUNTIME_CONVERSATION_SESSION_DRIFT",
            "The Conversation reservation has no Node Attempt Runtime owner",
            409,
        )
    connection = sandboxes.active_node_runtime_connection(
        db, flow_run_id=run.id, node_attempt_id=binding.node_attempt_id
    )
    host_root = _node_capability_root(
        db,
        flow_run_id=run.id,
        attempt_id=binding.node_attempt_id,
        manifest_digest=snapshot.runtime_manifest_hash,
        relative_parts=("conversations", binding.id),
    )
    if connection.runtime_session_id != binding.runtime_session_id:
        raise DomainError(
            "RUNTIME_CONVERSATION_SESSION_DRIFT",
            "The Conversation reservation no longer matches the FlowRun Runtime Session",
            409,
        )
    handle = RuntimeHandle(
        job_id=f"env-chat:{connection.resource_name}",
        conversation_id=binding.openhands_conversation_id,
        runtime_resource_id=connection.managed_runtime_id,
        runtime_resource_name=connection.resource_name,
        workspace_root=str(
            sandboxes.node_attempt_workspace_context(
                db, flow_run_id=run.id, node_attempt_id=binding.node_attempt_id
            ).runtime_mount_root
        ),
    )
    runtime = get_runtime()
    if allow_existing:
        try:
            identity = runtime.reload_conversation(handle)
        except DomainError as exc:
            if exc.code != "RUNTIME_CONVERSATION_MISSING":
                raise
        else:
            return handle, identity.event_id

    working_directory = binding.working_directory or str(_RUNTIME_PROJECT)
    config = config_from_binding(db, binding)
    provider = provider_for_config(db, config)
    runtime_root = Path(
        sandboxes.openhands_flow_run_capability_path(
            snapshot.runtime_manifest_hash, "conversations", binding.id
        )
    )
    agent_spec = build_agent_spec(
        config,
        provider=provider,
        binding_id=binding.id,
        working_directory=working_directory,
        host_root=host_root,
        runtime_root=runtime_root,
        system_message_suffix_append=_node_context_suffix(
            db, snapshot=snapshot, attempt_id=binding.node_attempt_id
        ),
    )
    request = build_runtime_request(
        db,
        flow_run_id=run.id,
        runtime_manifest_hash=snapshot.runtime_manifest_hash,
        attempt_id=binding.id,
        execution_key=f"flow-run:{run.id}:conversation:create",
        node={},
        bindings=[],
        workspace_ref=working_directory,
        interaction_mode="COLLABORATION",
        environment_image=environment.image_digest,
        environment_id=environment.environment_id,
        environment_version_id=environment.id,
        environment_version_no=environment.version_no,
        agent_spec=agent_spec,
        conversation_id=binding.openhands_conversation_id,
        node_attempt_id=binding.node_attempt_id,
    )
    request = replace(
        request,
        workspace_ref=working_directory,
        runtime_sandbox_id=connection.managed_runtime_id,
        runtime_resource_name=connection.resource_name,
        runtime_base_url=f"http://{connection.resource_name}:8000",
    )
    created = runtime.create_conversation(request)
    if created.conversation_id != binding.openhands_conversation_id:
        raise DomainError("AGENT_CONVERSATION_IDENTITY_DRIFT", "会话身份校验失败", 409)
    identity = runtime.reload_conversation(handle)
    return handle, identity.event_id


def bootstrap_node_conversation(
    db: Session,
    *,
    flow_run_id: str,
    attempt_id: str,
    content: str,
    attachments: tuple[dict[str, str | int], ...],
    references: tuple[dict[str, str], ...] = (),
    legacy_image_urls: tuple[str, ...] = (),
    conversation_id: str | None,
    work_directory_id: str | None,
    idempotency_key: str,
    session_config: FrozenSessionConfig | None = None,
) -> dict[str, Any]:
    """Lazily create a node Conversation and durably reconcile its first event."""

    text = content.strip()
    if not text and not attachments and not references and not legacy_image_urls:
        raise DomainError("AGENT_MESSAGE_EMPTY", "消息不能为空", 422)
    try:
        binding_id = str(UUID(conversation_id)) if conversation_id else str(uuid4())
    except ValueError as exc:
        raise DomainError("AGENT_CONVERSATION_ID_INVALID", "会话标识无效", 422) from exc
    validate_attachment_owners(binding_id, attachments)
    if legacy_image_urls:
        for value in legacy_image_urls:
            if not value.startswith("data:image/"):
                raise DomainError("AGENT_ATTACHMENT_INVALID", "图片附件无效", 422)
            base64.b64decode(value.partition(",")[2], validate=True)
    prompt, image_urls = message_payload(text, attachments, references)
    if legacy_image_urls:
        image_urls = legacy_image_urls
    agent_sessions.resolve_flow_node_session_host(
        db, flow_run_id=flow_run_id, attempt_id=attempt_id, require_start_permission=True
    )
    attempt = _attempt(db, attempt_id)
    node_run, run, snapshot = _attempt_context(db, attempt)
    binding, command = _node_bootstrap_command(
        db,
        flow_run_id=flow_run_id,
        attempt_id=attempt_id,
        idempotency_key=idempotency_key,
    )
    if binding is not None and command is not None:
        if binding.lifecycle == "ACTIVE" and binding.initial_user_event_id is not None:
            return {
                "conversation": _node_session_dict(db, binding),
                "accepted": True,
                "cursor": binding.initial_user_event_id,
            }
        if command.state == "AMBIGUOUS":
            try:
                reconciled = initial_user_event_id(
                    _handle(db, binding.id), binding.bootstrap_parent_event_id
                )
            except DomainError:
                reconciled = None
            if reconciled is not None:
                return _activate_node_bootstrap(
                    db,
                    binding=binding,
                    command=command,
                    initial_event_id=reconciled,
                    content=text,
                    attachments=attachments,
                )
            raise DomainError(
                "AGENT_BOOTSTRAP_DELIVERY_AMBIGUOUS",
                "首条消息正在安全对账，请稍后重试；系统不会重复发送",
                504,
                {"binding_id": binding.id},
            )
        if binding.lifecycle != "PROVISIONING" or command.state != "PENDING":
            raise DomainError("AGENT_CONVERSATION_PROVISIONING", "会话创建仍在处理中", 409)
        command.attempt_count += 1
        db.commit()
        allow_existing = True
    else:
        count = db.scalar(
            select(func.count(AgentConversationBinding.id)).where(
                AgentConversationBinding.host_kind == _FLOW_NODE,
                AgentConversationBinding.flow_run_id == run.id,
            )
        )
        if int(count or 0) >= get_settings().conversation_limit_per_flow_run:
            raise DomainError(
                "CONVERSATION_LIMIT_REACHED", "FlowRun conversation limit reached", 422
            )
        work_directory_version_id, working_directory = (
            agent_workspace_host.flow_run_conversation_work_directory_context(
                db, run.id, attempt.id, work_directory_id
            )
        )
        connection = sandboxes.active_node_runtime_connection(
            db, flow_run_id=run.id, node_attempt_id=attempt.id
        )
        binding = reserve_flow_node_binding(
            db,
            runtime_session_id=connection.runtime_session_id,
            flow_run_id=run.id,
            node_run_id=node_run.id,
            node_attempt_id=attempt.id,
            working_directory=working_directory,
            create_idempotency_key=idempotency_key,
            work_directory_version_id=work_directory_version_id,
            config=session_config,
            binding_id=binding_id,
        )
        command = AgentConversationCommand(
            workspace_id=None,
            host_kind=_FLOW_NODE,
            host_id=run.id,
            binding_id=binding.id,
            command_type="CREATE",
            idempotency_key=idempotency_key,
            attempt_count=1,
        )
        db.add(command)
        db.commit()
        allow_existing = False

    try:
        handle, previous_event_id = _create_or_reload_node_bootstrap(
            db,
            binding=binding,
            run=run,
            snapshot=snapshot,
            allow_existing=allow_existing,
        )
        binding.bootstrap_parent_event_id = previous_event_id
        db.commit()
    except DomainError as exc:
        if exc.details.get("outcome_unknown") is True:
            command.last_error_code = exc.code
            command.failure_summary = "Conversation creation requires retry with the original UUID"
            db.commit()
            raise DomainError(
                "AGENT_BOOTSTRAP_CREATION_AMBIGUOUS",
                "会话创建结果不确定，请使用同一请求标识重试",
                504,
            ) from exc
        _delete_node_bootstrap_reservation(db, binding, command)
        raise

    runtime = get_runtime()
    try:
        delivered = runtime.send_message(handle, prompt, image_urls)
    except DomainError as exc:
        if exc.details.get("outcome_unknown") is True:
            try:
                reconciled = initial_user_event_id(handle, previous_event_id)
            except DomainError:
                reconciled = None
            if reconciled is not None:
                return _activate_node_bootstrap(
                    db,
                    binding=binding,
                    command=command,
                    initial_event_id=reconciled,
                    content=text,
                    attachments=attachments,
                )
            command.state = "AMBIGUOUS"
            command.last_error_code = exc.code
            command.failure_summary = (
                "First user event delivery requires native identity reconciliation"
            )
            db.commit()
            raise DomainError(
                "AGENT_BOOTSTRAP_DELIVERY_AMBIGUOUS",
                "首条消息正在安全对账，请稍后重试；系统不会重复发送",
                504,
                {"binding_id": binding.id},
            ) from exc
        try:
            runtime.delete_conversation(handle)
        except DomainError:
            pass
        _delete_node_bootstrap_reservation(db, binding, command)
        raise

    try:
        initial_event_id = delivered.cursor or initial_user_event_id(handle, previous_event_id)
    except DomainError:
        initial_event_id = None
    if initial_event_id is None:
        command.state = "AMBIGUOUS"
        command.failure_summary = "First user event ID was unavailable after accepted delivery"
        db.commit()
        raise DomainError(
            "AGENT_BOOTSTRAP_DELIVERY_AMBIGUOUS",
            "首条消息正在安全对账，请稍后重试；系统不会重复发送",
            504,
            {"binding_id": binding.id},
        )
    return _activate_node_bootstrap(
        db,
        binding=binding,
        command=command,
        initial_event_id=initial_event_id,
        content=text,
        attachments=attachments,
    )


def record_attempt_input_attachments(
    db: Session,
    *,
    attempt_id: str,
    event_id: str,
    attachments: tuple[dict[str, Any], ...],
) -> None:
    """Project automatic-start FILE inputs onto the formal initial user event."""

    binding = agent_sessions.flow_node_binding_for_attempt(
        db, attempt_id, require_provisioning=False
    )
    normalized = tuple(
        {
            "path": str(item.get("path") or ""),
            "filename": str(item.get("filename") or "attachment"),
            "mime_type": str(item.get("mime_type") or "application/octet-stream"),
            "byte_size": int(item.get("byte_size") or 0),
        }
        for item in attachments
    )
    validate_attachment_owners(binding.id, normalized)
    record_message_attachments(db, binding, event_id, "", normalized)


def _handle(db: Session, binding_id: str, *, cursor: str | None = None) -> RuntimeHandle:
    locator = binding_locator(db, binding_id)
    return active_runtime_handle(
        db,
        flow_run_id=locator.flow_run_id,
        openhands_conversation_id=locator.openhands_conversation_id,
        cursor=cursor,
        route_kind="COLLABORATION",
    )


def _flow_run_handle(
    db: Session,
    flow_run_id: str,
    binding_id: str,
    *,
    cursor: str | None = None,
) -> RuntimeHandle:
    _binding_for_run(db, flow_run_id, binding_id)
    return _handle(db, binding_id, cursor=cursor)


def runtime_stream_details(db: Session, binding_id: str) -> tuple[str | None, RuntimeHandle]:
    return get_settings().runtime_adapter, _handle(db, binding_id)


def flow_run_runtime_stream_details(
    db: Session, flow_run_id: str, binding_id: str
) -> tuple[str | None, RuntimeHandle]:
    return get_settings().runtime_adapter, _flow_run_handle(db, flow_run_id, binding_id)


def terminal_resource_details(db: Session, binding_id: str) -> tuple[str, str]:
    handle = _handle(db, binding_id)
    if not handle.runtime_resource_id or not handle.runtime_resource_name:
        raise DomainError(
            "AGENT_TERMINAL_UNAVAILABLE",
            "This FlowRun Conversation has no active managed Runtime",
            409,
        )
    # This is an Agent Runtime, not an Environment setup container. Supplying
    # its Environment id switches the terminal controller to the setup
    # contract and rejects the required Runtime working directory.
    return handle.runtime_resource_name, handle.runtime_resource_id


def flow_run_terminal_resource_details(
    db: Session, flow_run_id: str, binding_id: str
) -> tuple[str, str]:
    _binding_for_run(db, flow_run_id, binding_id)
    return terminal_resource_details(db, binding_id)


def read_conversation_events(
    db: Session, binding_id: str, *, cursor: str | None = None
) -> dict[str, Any]:
    """Read a node Conversation and project its product-owned attachments."""

    binding = _binding(db, binding_id)
    batch = get_runtime().read_events(_handle(db, binding_id, cursor=cursor))
    return _event_batch_dict(db, binding, batch)


def read_flow_run_conversation_events(
    db: Session,
    flow_run_id: str,
    binding_id: str,
    *,
    cursor: str | None = None,
) -> dict[str, Any]:
    binding = _binding_for_run(db, flow_run_id, binding_id)
    batch = get_runtime().read_events(_flow_run_handle(db, flow_run_id, binding_id, cursor=cursor))
    return _event_batch_dict(db, binding, batch)


def read_node_conversation_events(
    db: Session,
    *,
    flow_run_id: str,
    attempt_id: str,
    binding_id: str,
    cursor: str | None = None,
) -> dict[str, Any]:
    _binding_for_attempt(db, flow_run_id=flow_run_id, attempt_id=attempt_id, binding_id=binding_id)
    return read_flow_run_conversation_events(db, flow_run_id, binding_id, cursor=cursor)


def read_gate_sidecar_events(
    db: Session,
    *,
    flow_run_id: str,
    attempt_id: str,
    binding_id: str,
    cursor: str | None = None,
) -> dict[str, Any]:
    """Read a Gate Agent transcript without exposing a writable locator."""

    binding = _binding_for_attempt(
        db,
        flow_run_id=flow_run_id,
        attempt_id=attempt_id,
        binding_id=binding_id,
        allow_gate_sidecar=True,
    )
    if not _is_gate_sidecar(binding):
        raise not_found("gate_evaluation_conversation", binding_id)
    batch = get_runtime().read_events(
        active_runtime_handle(
            db,
            flow_run_id=flow_run_id,
            openhands_conversation_id=binding.openhands_conversation_id,
            cursor=cursor,
            route_kind="COLLABORATION",
        )
    )
    return _event_batch_dict(db, binding, batch)


def _event_batch_dict(
    db: Session, binding: AgentConversationBinding, batch: RuntimeEventBatch
) -> dict[str, Any]:
    """Add FlowWeave attachment metadata to immutable OpenHands events.

    Automatic node starts upload frozen FILE inputs before OpenHands creates
    the initial user event.  OpenHands owns that event, while FlowWeave owns
    the attachment metadata, so the projection must happen when events are
    read just as it does for ordinary Agent Workspace conversations.
    """

    event_ids = [event.cursor for event in batch.events if event.event_type == "MESSAGE"]
    stored = (
        list(
            db.scalars(
                select(AgentConversationMessageAttachment).where(
                    AgentConversationMessageAttachment.binding_id == binding.id,
                    AgentConversationMessageAttachment.event_id.in_(event_ids),
                )
            ).all()
        )
        if event_ids
        else []
    )
    attachments_by_event: dict[str, list[AgentConversationMessageAttachment]] = {}
    for attachment in stored:
        attachments_by_event.setdefault(attachment.event_id, []).append(attachment)

    def project(event: Any) -> dict[str, Any]:
        payload = dict(event.payload)
        display_content, references = project_conversation_references(
            str(payload.get("content") or "")
        )
        if references:
            payload["conversation_references"] = list(references)
        attachments = attachments_by_event.get(event.cursor, [])
        if attachments:
            # Automatic starts record an empty display override: retain the
            # native startup prompt and merely attach the input files.
            display_content = next((item.content for item in attachments if item.content), None)
            if display_content is not None:
                payload["display_content"] = display_content
            payload["attachments"] = [
                {
                    "filename": item.filename,
                    "mime_type": item.mime_type,
                    "byte_size": item.byte_size,
                    "path": item.path,
                }
                for item in attachments
            ]
        elif references:
            payload["display_content"] = display_content
        return {"id": event.cursor, "event_type": event.event_type, "payload": payload}

    return {
        "events": [project(event) for event in batch.events],
        "next_cursor": batch.cursor,
        "result": batch.result.as_dict() if batch.result is not None else None,
    }


def send_question(
    db: Session,
    binding_id: str,
    payload: ConversationQuestionWrite,
    idempotency_key: str,
    actor: str | None,
) -> dict[str, Any]:
    """Send a formal OpenHands user message and store audit metadata only."""

    existing = db.scalar(select(HumanAction).where(HumanAction.idempotency_key == idempotency_key))
    if existing is not None:
        return {"accepted": True, "idempotent": True}
    item = _binding(db, binding_id, lock=True)
    text = "\n".join(part.text for part in payload.content if part.type == "text")
    if len(text) > get_settings().conversation_message_max_chars:
        raise DomainError("MESSAGE_TOO_LARGE", "Question is too large", 422)
    image_urls = tuple(
        f"data:{part.mime_type};base64,{part.content_base64}"
        for part in payload.content
        if part.type == "attachment" and part.mime_type.startswith("image/")
    )
    unsupported = [
        part.filename
        for part in payload.content
        if part.type == "attachment" and not part.mime_type.startswith("image/")
    ]
    if unsupported:
        raise DomainError(
            "ATTACHMENT_TYPE_UNSUPPORTED",
            "Non-image attachments require the FlowRun workspace upload API",
            422,
            {"filenames": unsupported},
        )
    # Decode once at the trust boundary so malformed input never reaches the Runtime.
    for value in image_urls:
        base64.b64decode(value.partition(",")[2], validate=True)
    handle = _handle(db, binding_id)
    runtime = get_runtime()
    if not runtime.can_accept_input(handle):
        raise DomainError("AGENT_CONVERSATION_BUSY", "Agent 正在处理上一条消息，请稍候", 409)
    provider = provider_for_config(db, config_from_binding(db, item))
    if provider is not None:
        runtime.switch_model(handle, provider)
    result = runtime.send_message(handle, text, image_urls)
    db.add(
        HumanAction(
            flow_run_id=item.flow_run_id,
            action_type="ASK_FLOW_RUN_CONVERSATION",
            idempotency_key=idempotency_key,
            payload_json={
                "binding_id": item.id,
                "actor": actor,
                "content_digest": hashlib.sha256(text.encode()).hexdigest(),
                "content_length": len(text),
                "image_count": len(image_urls),
                "client_question_id": payload.client_question_id,
            },
        )
    )
    activity_at = now()
    item.last_connected_at = activity_at
    item.updated_at = activity_at
    finish(db)
    return {"accepted": True, "cursor": result.cursor, "runtime": _result_dict(result)}


def send_flow_run_question(
    db: Session,
    flow_run_id: str,
    binding_id: str,
    payload: ConversationQuestionWrite,
    idempotency_key: str,
    actor: str | None,
) -> dict[str, Any]:
    _binding_for_run(db, flow_run_id, binding_id)
    run = db.get(FlowRun, flow_run_id)
    if run is None:
        raise not_found("flow_run", flow_run_id)
    if run.state in {"COMPLETED", "CANCELLED"}:
        raise DomainError(
            "FLOW_RUN_TERMINAL",
            "A completed or cancelled FlowRun cannot accept new questions",
            409,
            {"flow_run_id": flow_run_id, "state": run.state},
        )
    return send_question(db, binding_id, payload, idempotency_key, actor)


def send_node_question(
    db: Session,
    *,
    flow_run_id: str,
    attempt_id: str,
    binding_id: str,
    payload: ConversationQuestionWrite,
    idempotency_key: str,
    actor: str | None,
) -> dict[str, Any]:
    _binding_for_attempt(db, flow_run_id=flow_run_id, attempt_id=attempt_id, binding_id=binding_id)
    return send_flow_run_question(db, flow_run_id, binding_id, payload, idempotency_key, actor)


def send_node_message(
    db: Session,
    *,
    flow_run_id: str,
    attempt_id: str,
    binding_id: str,
    content: str,
    attachments: tuple[dict[str, str | int], ...] = (),
    references: tuple[dict[str, str], ...] = (),
) -> dict[str, Any]:
    """Send the same attachment-aware native message as the outer workbench."""

    if not content.strip() and not attachments and not references:
        raise DomainError("AGENT_MESSAGE_EMPTY", "消息不能为空", 422)
    binding = _binding_for_attempt(
        db, flow_run_id=flow_run_id, attempt_id=attempt_id, binding_id=binding_id, lock=True
    )
    validate_attachment_owners(binding.id, attachments)
    prompt, image_urls = message_payload(content, attachments, references)
    handle = _node_handle(db, flow_run_id=flow_run_id, attempt_id=attempt_id, binding_id=binding_id)
    runtime = get_runtime()
    if not runtime.can_accept_input(handle):
        raise DomainError("AGENT_CONVERSATION_BUSY", "Agent 正在处理上一条消息，请稍候", 409)
    provider = provider_for_config(db, config_from_binding(db, binding))
    if provider is not None:
        runtime.switch_model(handle, provider)
    result = runtime.send_message(handle, prompt, image_urls)
    if result.cursor:
        record_message_attachments(db, binding, result.cursor, content.strip(), attachments)
    activity_at = now()
    binding.last_connected_at = activity_at
    binding.updated_at = activity_at
    finish(db)
    return {"accepted": True, "cursor": result.cursor, "compacted": False}


def upload_node_attachment(
    db: Session,
    *,
    flow_run_id: str,
    attempt_id: str,
    filename: str,
    content_type: str,
    content: bytes,
    binding_id: str | None = None,
    attachment_owner_id: str | None = None,
) -> dict[str, str | int | None]:
    """Upload a node draft/bound attachment into its existing FlowRun Runtime."""

    if not filename or len(filename) > 240 or "\x00" in filename:
        raise DomainError("AGENT_ATTACHMENT_INVALID", "附件文件名无效", 422)
    if not content or len(content) > 25 * 1024 * 1024:
        raise DomainError("AGENT_ATTACHMENT_TOO_LARGE", "单个附件不能超过 25 MiB", 422)
    mime_type = content_type.lower().strip() or "application/octet-stream"
    if len(mime_type) > 200:
        raise DomainError("AGENT_ATTACHMENT_INVALID", "附件类型无效", 422)
    agent_sessions.resolve_flow_node_session_host(
        db, flow_run_id=flow_run_id, attempt_id=attempt_id, require_start_permission=False
    )
    bound: AgentConversationBinding | None = None
    if binding_id:
        bound = _binding_for_attempt(
            db, flow_run_id=flow_run_id, attempt_id=attempt_id, binding_id=binding_id
        )
        owner_id = bound.id
        handle = _node_handle(
            db, flow_run_id=flow_run_id, attempt_id=attempt_id, binding_id=binding_id
        )
    else:
        try:
            owner_id = str(UUID(attachment_owner_id or ""))
        except ValueError as exc:
            raise DomainError("AGENT_CONVERSATION_ID_INVALID", "附件必须关联有效会话", 422) from exc
        connection = sandboxes.active_node_runtime_connection(
            db, flow_run_id=flow_run_id, node_attempt_id=attempt_id
        )
        handle = RuntimeHandle(
            job_id=f"flow-run:{flow_run_id}",
            conversation_id="",
            runtime_resource_id=connection.managed_runtime_id,
            runtime_resource_name=connection.resource_name,
            workspace_root=str(
                sandboxes.node_attempt_workspace_context(
                    db, flow_run_id=flow_run_id, node_attempt_id=attempt_id
                ).runtime_mount_root
            ),
        )
    path = get_runtime().upload_workspace_file(
        handle,
        filename=filename,
        content_type=mime_type,
        content=content,
        attachment_owner_id=owner_id,
    )
    if (matched := ATTACHMENT_PATH.fullmatch(path)) is None or matched.group("owner") != owner_id:
        raise DomainError("RUNTIME_PROTOCOL_ERROR", "OpenHands 返回了无效附件路径", 502)
    if bound is not None:
        db.add(
            AgentConversationMessageAttachment(
                binding_id=bound.id,
                event_id=f"pending:{uuid4()}",
                content="",
                filename=filename,
                mime_type=mime_type,
                byte_size=len(content),
                path=path,
            )
        )
        db.flush()
    image_data_url = (
        f"data:{mime_type};base64,{base64.b64encode(content).decode('ascii')}"
        if mime_type.startswith("image/")
        else None
    )
    return {
        "filename": filename,
        "mime_type": mime_type,
        "byte_size": len(content),
        "path": path,
        "image_data_url": image_data_url,
    }


def add_node_conversation_capability(
    db: Session,
    *,
    flow_run_id: str,
    attempt_id: str,
    binding_id: str,
    capability_version_id: str,
) -> dict[str, Any]:
    """Load a governed capability through the same native marketplace lifecycle."""

    binding = _binding_for_attempt(
        db, flow_run_id=flow_run_id, attempt_id=attempt_id, binding_id=binding_id, lock=True
    )
    if binding.lifecycle != "ACTIVE" or not binding.working_directory:
        raise DomainError("AGENT_CONVERSATION_NOT_READY", "会话当前无法加载能力", 409)
    existing = db.scalar(
        select(AgentConversationCapability).where(
            AgentConversationCapability.binding_id == binding.id,
            AgentConversationCapability.capability_version_id == capability_version_id,
        )
    )
    if existing is not None:
        return _node_session_dict(db, binding)
    published = resolve_version(db, capability_version_id)
    capability_type = published.package.capability_type
    if capability_type not in {"SKILL", "MCP", "PLUGIN"}:
        raise DomainError("AGENT_CAPABILITY_INVALID", "该能力不能加载到 Agent 会话", 422)
    same_name = db.scalar(
        select(AgentConversationCapability).where(
            AgentConversationCapability.binding_id == binding.id,
            AgentConversationCapability.capability_type == capability_type,
            AgentConversationCapability.capability_key == published.package.capability_key,
        )
    )
    if same_name is not None:
        raise DomainError(
            "AGENT_CONVERSATION_CAPABILITY_CONFLICT", "同类型同名称能力已加载其他版本", 409
        )
    handle = _node_handle(db, flow_run_id=flow_run_id, attempt_id=attempt_id, binding_id=binding_id)
    runtime = get_runtime()
    if not runtime.can_accept_input(handle):
        raise DomainError("AGENT_CONVERSATION_RUNNING", "会话运行中，完成当前回复后再加载能力", 409)
    attempt = _attempt(db, attempt_id)
    _, _, snapshot = _attempt_context(db, attempt)
    host_root = _node_capability_root(
        db,
        flow_run_id=flow_run_id,
        attempt_id=attempt_id,
        manifest_digest=snapshot.runtime_manifest_hash,
        relative_parts=("conversations", binding.id),
    )
    runtime_root = Path(
        sandboxes.openhands_flow_run_capability_path(
            snapshot.runtime_manifest_hash, "conversations", binding.id
        )
    )
    marketplace_name = agent_workspace_capability_marketplace_name(binding.id)
    plugin_name = materialize_agent_workspace_capability_marketplace(
        frozen_runtime_capability(db, published, capability_type),
        host_root=host_root,
        runtime_root=runtime_root,
        marketplace_name=marketplace_name,
    )
    if plugin_name is None:
        raise DomainError("RUNTIME_CAPABILITY_UNAVAILABLE", "能力插件物化失败", 409)
    runtime.load_plugin(handle, f"{plugin_name}@{marketplace_name}")
    position = db.scalar(
        select(AgentConversationCapability.position)
        .where(AgentConversationCapability.binding_id == binding.id)
        .order_by(AgentConversationCapability.position.desc())
        .limit(1)
    )
    db.add(
        AgentConversationCapability(
            binding_id=binding.id,
            capability_version_id=published.version.id,
            capability_type=capability_type,
            capability_key=published.package.capability_key,
            digest=published.version.digest,
            position=(position + 1) if position is not None else 0,
        )
    )
    binding.updated_at = now()
    db.flush()
    return _node_session_dict(db, binding)


def node_pending_confirmation(
    db: Session, *, flow_run_id: str, attempt_id: str, binding_id: str
) -> dict[str, Any]:
    pending = get_runtime().get_pending_confirmation(
        _node_handle(db, flow_run_id=flow_run_id, attempt_id=attempt_id, binding_id=binding_id)
    )
    if pending is None:
        return {"pending": False}
    return {
        "pending": True,
        "pending_actions_digest": pending.pending_actions_digest,
        "cursor": pending.cursor,
        "actions": [
            {
                "action_id": item.action_id,
                "tool_call_id": item.tool_call_id,
                "tool_name": item.tool_name,
                "arguments": item.arguments,
                "security_risk": item.security_risk,
                "summary": item.summary,
                "digest": item.digest,
            }
            for item in pending.actions
        ],
    }


def decide_node_confirmation(
    db: Session,
    *,
    flow_run_id: str,
    attempt_id: str,
    binding_id: str,
    expected_pending_digest: str,
    accept: bool,
    reason: str,
) -> dict[str, Any]:
    if not reason.strip():
        raise DomainError("AGENT_CONFIRMATION_REASON_REQUIRED", "请填写确认理由", 422)
    result = get_runtime().respond_to_confirmation(
        _node_handle(db, flow_run_id=flow_run_id, attempt_id=attempt_id, binding_id=binding_id),
        expected_pending_digest,
        accept,
        reason.strip(),
    )
    return {"accepted": True, "cursor": result.cursor}


def delete_node_conversation(
    db: Session, *, flow_run_id: str, attempt_id: str, binding_id: str
) -> None:
    binding = _binding_for_attempt(
        db, flow_run_id=flow_run_id, attempt_id=attempt_id, binding_id=binding_id, lock=True
    )
    get_runtime().delete_conversation(
        _node_handle(db, flow_run_id=flow_run_id, attempt_id=attempt_id, binding_id=binding_id)
    )
    delete_binding_records(db, binding.id)
    finish(db)


def rerun_node_message(
    db: Session, *, flow_run_id: str, attempt_id: str, binding_id: str, event_id: str, content: str
) -> dict[str, Any]:
    if not content.strip():
        raise DomainError("AGENT_MESSAGE_EMPTY", "消息不能为空", 422)
    binding = _binding_for_attempt(
        db,
        flow_run_id=flow_run_id,
        attempt_id=attempt_id,
        binding_id=binding_id,
        lock=True,
    )
    handle = _node_handle(db, flow_run_id=flow_run_id, attempt_id=attempt_id, binding_id=binding_id)
    runtime = get_runtime()
    if not runtime.can_accept_input(handle):
        raise DomainError("AGENT_CONVERSATION_BUSY", "请先暂停当前回复", 409)
    events = runtime.read_active_events(handle).events
    user_events = [
        event
        for event in events
        if event.event_type == "MESSAGE"
        and str(event.payload.get("source") or "").lower() in {"user", "human"}
    ]
    target = next((event for event in user_events if event.cursor == event_id), None)
    if target is None or not user_events or user_events[-1].cursor != event_id:
        raise DomainError(
            "AGENT_MESSAGE_REWRITE_UNAVAILABLE", "只能编辑当前活动分支中最近发送的消息", 409
        )
    parent_id = target.payload.get("parent_id")
    if parent_id is not None and not isinstance(parent_id, str):
        raise DomainError("RUNTIME_EVENT_IDENTITY_INVALID", "消息事件身份无效", 409)
    runtime.navigate(handle, parent_id)
    result = runtime.send_message(handle, content.strip())
    activity_at = now()
    binding.last_connected_at = activity_at
    binding.updated_at = activity_at
    finish(db)
    return {"accepted": True, "cursor": result.cursor}


def fork_node_conversation(
    db: Session,
    *,
    flow_run_id: str,
    attempt_id: str,
    binding_id: str,
    event_id: str,
    title: str | None,
    idempotency_key: str,
) -> dict[str, Any]:
    """Use OpenHands' native fork while preserving the Attempt-only directory."""

    source = _binding_for_attempt(
        db, flow_run_id=flow_run_id, attempt_id=attempt_id, binding_id=binding_id, lock=True
    )
    existing = db.scalar(
        select(AgentConversationBinding).where(
            AgentConversationBinding.create_idempotency_key == idempotency_key
        )
    )
    if existing is not None:
        if existing.host_kind != _FLOW_NODE or existing.node_attempt_id != attempt_id:
            raise DomainError("AGENT_CONVERSATION_COMMAND_CONFLICT", "会话分叉请求冲突", 409)
        return _node_session_dict(db, existing)
    source_handle = _node_handle(
        db, flow_run_id=flow_run_id, attempt_id=attempt_id, binding_id=binding_id
    )
    runtime = get_runtime()
    if not runtime.can_accept_input(source_handle):
        raise DomainError("AGENT_CONVERSATION_BUSY", "请在当前回复完成或暂停后分叉会话", 409)
    source_identity = runtime.reload_conversation(source_handle)
    if not source_identity.event_id:
        raise DomainError("RUNTIME_EVENT_IDENTITY_INVALID", "当前会话缺少可分叉的事件身份", 409)
    fork_event_id = runtime.resolve_fork_boundary(source_handle, event_id)
    source_identity = runtime.reload_conversation(source_handle)
    config = config_from_binding(db, source)
    target = reserve_flow_node_binding(
        db,
        runtime_session_id=source.runtime_session_id,
        flow_run_id=flow_run_id,
        node_run_id=source.node_run_id or "",
        node_attempt_id=attempt_id,
        working_directory=source.working_directory or str(_RUNTIME_PROJECT),
        create_idempotency_key=idempotency_key,
        display_title=(title or "").strip()[:240] or f"Fork · {source.display_title or '会话'}",
        work_directory_version_id=source.work_directory_version_id,
        config=config,
    )
    if source_identity.event_id is None:
        raise DomainError("AGENT_CONVERSATION_EVENT_NOT_FOUND", "分叉来源事件不存在", 404)
    provider = provider_for_config(db, config)
    result = runtime.fork_conversation(
        source_handle,
        target_conversation_id=target.openhands_conversation_id,
        title=target.display_title or "未命名会话",
        from_event_id=fork_event_id,
        expected_source_leaf_event_id=source_identity.event_id,
        reset_metrics=True,
        condenser=RuntimeCondenser(
            kind="LLM_SUMMARIZING",
            max_size=AGENT_WORKSPACE_CONDENSER_MAX_EVENTS,
            max_tokens_ratio=PROACTIVE_COMPACTION_RATIO,
            keep_first=4,
        ),
        condenser_provider=provider,
    )
    if result.handle.conversation_id != target.openhands_conversation_id:
        raise DomainError("RUNTIME_FORK_IDENTITY_DRIFT", "会话分叉身份校验失败", 409)
    target.lifecycle = "ACTIVE"
    finish(db)
    return _node_session_dict(db, target)


def _result_dict(result: RuntimeResult) -> dict[str, Any]:
    value = result.as_dict()
    # A Runtime cursor is returned as transient transport data, never persisted.
    return value


def stop_conversation(db: Session, binding_id: str) -> dict[str, Any]:
    get_runtime().cancel(_handle(db, binding_id))
    return {"accepted": True}


def stop_flow_run_conversation(db: Session, flow_run_id: str, binding_id: str) -> dict[str, Any]:
    get_runtime().cancel(_flow_run_handle(db, flow_run_id, binding_id))
    return {"accepted": True}


def stop_node_conversation(
    db: Session, *, flow_run_id: str, attempt_id: str, binding_id: str
) -> dict[str, Any]:
    _binding_for_attempt(db, flow_run_id=flow_run_id, attempt_id=attempt_id, binding_id=binding_id)
    return stop_flow_run_conversation(db, flow_run_id, binding_id)


def node_runtime_stream_details(
    db: Session, *, flow_run_id: str, attempt_id: str, binding_id: str
) -> tuple[str | None, RuntimeHandle]:
    _binding_for_attempt(db, flow_run_id=flow_run_id, attempt_id=attempt_id, binding_id=binding_id)
    return flow_run_runtime_stream_details(db, flow_run_id, binding_id)


def node_terminal_resource_details(
    db: Session, *, flow_run_id: str, attempt_id: str, binding_id: str
) -> tuple[str, str]:
    _binding_for_attempt(db, flow_run_id=flow_run_id, attempt_id=attempt_id, binding_id=binding_id)
    return flow_run_terminal_resource_details(db, flow_run_id, binding_id)


def node_draft_terminal_resource_details(
    db: Session, *, flow_run_id: str, attempt_id: str
) -> tuple[str, str, str]:
    """Open a terminal before first message in the Attempt's fixed directory."""

    host = agent_sessions.resolve_flow_node_session_host(
        db, flow_run_id=flow_run_id, attempt_id=attempt_id, require_start_permission=False
    )
    connection = sandboxes.active_node_runtime_connection(
        db, flow_run_id=flow_run_id, node_attempt_id=attempt_id
    )
    return (
        connection.resource_name,
        connection.managed_runtime_id,
        host.session.working_directory,
    )


def _node_handle(
    db: Session, *, flow_run_id: str, attempt_id: str, binding_id: str
) -> RuntimeHandle:
    """Resolve one active Runtime handle only after lineage authorization."""

    _binding_for_attempt(db, flow_run_id=flow_run_id, attempt_id=attempt_id, binding_id=binding_id)
    return _flow_run_handle(db, flow_run_id, binding_id)


def node_input_readiness(
    db: Session, *, flow_run_id: str, attempt_id: str, binding_id: str
) -> dict[str, bool | str]:
    return (
        get_runtime()
        .input_readiness(
            _node_handle(db, flow_run_id=flow_run_id, attempt_id=attempt_id, binding_id=binding_id)
        )
        .as_dict()
    )


def node_conversation_context(
    db: Session, *, flow_run_id: str, attempt_id: str, binding_id: str
) -> dict[str, Any]:
    """Return only native Runtime context for the authorized node session."""

    return dict(
        get_runtime().conversation_context(
            _node_handle(db, flow_run_id=flow_run_id, attempt_id=attempt_id, binding_id=binding_id)
        )
    )


def switch_node_conversation_model(
    db: Session,
    *,
    flow_run_id: str,
    attempt_id: str,
    binding_id: str,
    model_provider_id: str,
    model_name: str,
    reasoning_effort: str | None,
) -> dict[str, str | None]:
    """Apply and freeze a selected model for one scoped FlowRun session."""

    binding = _binding_for_attempt(
        db,
        flow_run_id=flow_run_id,
        attempt_id=attempt_id,
        binding_id=binding_id,
        lock=True,
    )
    if not has_connected_default_model(db, model_provider_id):
        raise DomainError(
            "AGENT_MODEL_CONFIGURATION_REQUIRED",
            "请选择已测试成功且存在启用默认模型的模型供应商",
            409,
        )
    handle = _node_handle(db, flow_run_id=flow_run_id, attempt_id=attempt_id, binding_id=binding_id)
    runtime = get_runtime()
    if not runtime.can_accept_input(handle):
        raise DomainError("AGENT_CONVERSATION_BUSY", "请在当前回复完成或暂停后切换模型", 409)
    provider = runtime_provider(
        db,
        {"asset": {"executor": {"model_provider_id": model_provider_id}}},
        model_name=model_name,
        reasoning_effort=reasoning_effort,
    )
    runtime.switch_model(handle, provider)
    binding.model_provider_id = provider.provider_id
    binding.model_name = provider.model
    binding.reasoning_effort = provider.reasoning_effort
    binding.updated_at = now()
    finish(db)
    return {
        "model_provider_id": provider.provider_id,
        "model_name": provider.model,
        "reasoning_effort": provider.reasoning_effort,
    }


def condense_node_conversation(
    db: Session, *, flow_run_id: str, attempt_id: str, binding_id: str
) -> dict[str, Any]:
    runtime = get_runtime()
    handle = _node_handle(db, flow_run_id=flow_run_id, attempt_id=attempt_id, binding_id=binding_id)
    if not runtime.can_accept_input(handle):
        raise DomainError("AGENT_CONVERSATION_BUSY", "请在当前回复完成或暂停后压缩上下文", 409)
    return {"accepted": True, "cursor": runtime.condense(handle).cursor}


def interrupt_node_conversation(
    db: Session, *, flow_run_id: str, attempt_id: str, binding_id: str
) -> dict[str, bool]:
    binding = _binding_for_attempt(
        db, flow_run_id=flow_run_id, attempt_id=attempt_id, binding_id=binding_id
    )
    attempt = _attempt(db, attempt_id)
    expected_version = attempt.state_version
    should_pause = attempt.state == AttemptState.EXECUTING and attempt.runtime_phase == "RUNNING"
    get_runtime().interrupt(
        _node_handle(db, flow_run_id=flow_run_id, attempt_id=attempt_id, binding_id=binding_id)
    )
    if should_pause:
        claimed_id = db.scalar(
            update(NodeAttempt)
            .where(
                NodeAttempt.id == attempt_id,
                NodeAttempt.state == AttemptState.EXECUTING,
                NodeAttempt.runtime_phase == "RUNNING",
                NodeAttempt.state_version == expected_version,
            )
            .values(
                state=AttemptState.PAUSED,
                runtime_phase="PAUSED",
                state_version=NodeAttempt.state_version + 1,
            )
            .returning(NodeAttempt.id)
            .execution_options(synchronize_session=False)
        )
        if claimed_id is None:
            db.expire_all()
            return {"accepted": True}
        db.expire_all()
        paused = _attempt(db, attempt_id)
        node_run, run, _ = _attempt_context(db, paused)
        run.state = FlowRunState.WAITING_HUMAN
        db.add(
            RunEvent(
                flow_run_id=run.id,
                node_run_id=node_run.id,
                attempt_id=paused.id,
                event_type="ATTEMPT_PAUSED",
                payload_json={"conversation_id": binding.openhands_conversation_id},
            )
        )
        finish(db)
    return {"accepted": True}


def _record_native_pause(
    db: Session,
    *,
    attempt_id: str,
    binding: AgentConversationBinding,
    expected_version: int,
) -> NodeAttempt | None:
    """CAS-project an already-confirmed native pause into the Attempt.

    OpenHands owns Conversation execution state.  FlowWeave keeps only the
    Attempt-level orchestration projection needed to gate a FlowRun.  An
    interrupt can therefore succeed upstream while a concurrent worker wins
    the local state race.  Project only the unambiguous native ``paused``
    state; never infer a pause from a transport error or a merely writable
    Conversation.
    """

    claimed_id = db.scalar(
        update(NodeAttempt)
        .where(
            NodeAttempt.id == attempt_id,
            NodeAttempt.state == AttemptState.EXECUTING,
            NodeAttempt.runtime_phase == "RUNNING",
            NodeAttempt.state_version == expected_version,
        )
        .values(
            state=AttemptState.PAUSED,
            runtime_phase="PAUSED",
            state_version=NodeAttempt.state_version + 1,
        )
        .returning(NodeAttempt.id)
        .execution_options(synchronize_session=False)
    )
    if claimed_id is None:
        db.expire_all()
        return None
    db.expire_all()
    paused = _attempt(db, attempt_id)
    node_run, run, _ = _attempt_context(db, paused)
    run.state = FlowRunState.WAITING_HUMAN
    db.add(
        RunEvent(
            flow_run_id=run.id,
            node_run_id=node_run.id,
            attempt_id=paused.id,
            event_type="ATTEMPT_PAUSED",
            payload_json={"conversation_id": binding.openhands_conversation_id},
        )
    )
    return paused


def resume_node_conversation(
    db: Session, *, flow_run_id: str, attempt_id: str, binding_id: str
) -> dict[str, Any]:
    binding = _binding_for_attempt(
        db, flow_run_id=flow_run_id, attempt_id=attempt_id, binding_id=binding_id
    )
    attempt = _attempt(db, attempt_id)
    if attempt.state != AttemptState.PAUSED or attempt.runtime_phase != "PAUSED":
        # Recover only the precise split-brain state caused when the native
        # interrupt succeeded but its local Attempt projection lost a race.
        # A ready Conversation is not enough: OpenHands must explicitly say it
        # is paused, otherwise this remains a normal optimistic-lock conflict.
        handle = _node_handle(
            db, flow_run_id=flow_run_id, attempt_id=attempt_id, binding_id=binding_id
        )
        readiness = get_runtime().input_readiness(handle)
        if (
            attempt.state != AttemptState.EXECUTING
            or attempt.runtime_phase != "RUNNING"
            or readiness.execution_status.lower() != "paused"
            or _record_native_pause(
                db,
                attempt_id=attempt_id,
                binding=binding,
                expected_version=attempt.state_version,
            )
            is None
        ):
            raise conflict(
                "node attempt is not paused",
                attempt_id=attempt.id,
                state=attempt.state,
            )
        attempt = _attempt(db, attempt_id)
    expected_version = attempt.state_version
    claimed_id = db.scalar(
        update(NodeAttempt)
        .where(
            NodeAttempt.id == attempt_id,
            NodeAttempt.state == AttemptState.PAUSED,
            NodeAttempt.runtime_phase == "PAUSED",
            NodeAttempt.state_version == expected_version,
        )
        .values(
            state=AttemptState.EXECUTING,
            runtime_phase="RESUMING",
            state_version=NodeAttempt.state_version + 1,
        )
        .returning(NodeAttempt.id)
        .execution_options(synchronize_session=False)
    )
    if claimed_id is None:
        db.expire_all()
        raise conflict("node attempt changed while resuming", attempt_id=attempt_id)
    db.expire_all()
    resuming = _attempt(db, attempt_id)
    result = get_runtime().run(
        _node_handle(db, flow_run_id=flow_run_id, attempt_id=attempt_id, binding_id=binding_id)
    )
    running_id = db.scalar(
        update(NodeAttempt)
        .where(
            NodeAttempt.id == attempt_id,
            NodeAttempt.state == AttemptState.EXECUTING,
            NodeAttempt.runtime_phase == "RESUMING",
            NodeAttempt.state_version == resuming.state_version,
        )
        .values(
            runtime_phase="RUNNING",
            state_version=NodeAttempt.state_version + 1,
        )
        .returning(NodeAttempt.id)
        .execution_options(synchronize_session=False)
    )
    if running_id is None:
        db.expire_all()
        return {"accepted": True, "cursor": result.cursor}
    db.expire_all()
    running = _attempt(db, attempt_id)
    node_run, run, _ = _attempt_context(db, running)
    run.state = FlowRunState.ACTIVE
    db.add(
        RunEvent(
            flow_run_id=run.id,
            node_run_id=node_run.id,
            attempt_id=running.id,
            event_type="ATTEMPT_RESUMED",
            payload_json={"conversation_id": binding.openhands_conversation_id},
        )
    )
    task = enqueue(
        db,
        task_type="WAIT_RUNTIME_WAKEUP",
        aggregate_type="ATTEMPT",
        aggregate_id=running.id,
        idempotency_key=f"wait-runtime-wakeup:{running.id}:v{running.state_version}:1",
        payload={"wakeup_no": 1},
    )
    task.max_attempts = max(task.max_attempts, 100)
    finish(db)
    return {"accepted": True, "cursor": result.cursor}


def condense_conversation(db: Session, binding_id: str) -> dict[str, Any]:
    result = get_runtime().condense(_handle(db, binding_id))
    return {"accepted": True, "runtime": _result_dict(result)}


def control_goal(
    db: Session,
    binding_id: str,
    *,
    action: str,
    objective: str | None,
    max_iterations: int,
) -> dict[str, Any]:
    runtime = get_runtime()
    handle = _handle(db, binding_id)
    if action == "START":
        runtime.start_goal(handle, objective or "", max_iterations)
    elif action == "STOP":
        runtime.stop_goal(handle)
    elif action == "RESUME":
        runtime.resume_goal(handle)
    else:
        raise DomainError("GOAL_ACTION_INVALID", "Unsupported Goal action", 422)
    return {"accepted": True}


def ask_agent(
    db: Session, binding_id: str, *, question: str, timeout_seconds: int
) -> dict[str, Any]:
    result = get_runtime().ask_agent(
        _handle(db, binding_id), question, timeout_seconds=float(timeout_seconds)
    )
    return {
        "response": result.response,
        "before_usage": asdict(result.before_usage) if result.before_usage else None,
        "after_usage": asdict(result.after_usage) if result.after_usage else None,
    }


__all__ = (
    "ask_agent",
    "condense_conversation",
    "control_goal",
    "create_conversation",
    "create_flow_run_conversation",
    "create_node_conversation",
    "bootstrap_node_conversation",
    "flow_run_runtime_stream_details",
    "flow_run_terminal_resource_details",
    "get_conversation",
    "get_flow_run_conversation",
    "get_node_conversation",
    "list_conversations",
    "list_flow_run_conversations",
    "list_node_conversations",
    "patch_conversation",
    "patch_flow_run_conversation",
    "patch_node_conversation",
    "read_conversation_events",
    "read_flow_run_conversation_events",
    "read_node_conversation_events",
    "runtime_stream_details",
    "send_question",
    "send_flow_run_question",
    "send_node_question",
    "stop_conversation",
    "stop_flow_run_conversation",
    "stop_node_conversation",
    "node_runtime_stream_details",
    "node_conversation_binding",
    "node_conversation_context",
    "node_input_readiness",
    "node_terminal_resource_details",
    "condense_node_conversation",
    "interrupt_node_conversation",
    "resume_node_conversation",
    "switch_node_conversation_model",
    "terminal_resource_details",
)
