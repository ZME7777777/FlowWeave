from __future__ import annotations

import base64
import hashlib
from dataclasses import asdict, replace
from pathlib import Path, PurePosixPath
from typing import Any, cast
from uuid import UUID, uuid4

from sqlalchemy import func, select
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
    record_message_attachments,
    validate_attachment_owners,
)
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
    ArtifactVersion,
    AttemptInputBinding,
    FlowRun,
    HumanAction,
    NodeAttempt,
    NodeRun,
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
_RUNTIME_PROJECT = PurePosixPath("/runtime/workspace/project")


def _runtime_working_directory(request: Any) -> str:
    """Return the mounted Attempt directory from a validated Runtime request."""

    node_root = PurePosixPath(request.node_workspace_ref)
    relative = PurePosixPath(request.runtime_working_dir_relative)
    result = node_root.joinpath(*relative.parts)
    if (
        not node_root.is_absolute()
        or not result.is_relative_to(_RUNTIME_PROJECT)
        or any(part in {"", ".", ".."} for part in relative.parts)
    ):
        raise DomainError(
            "RUNTIME_WORKSPACE_INVALID",
            "The Runtime request did not resolve an isolated mounted working directory",
            409,
        )
    return str(result)


def _binding(db: Session, binding_id: str, *, lock: bool = False) -> AgentConversationBinding:
    query = select(AgentConversationBinding).where(
        AgentConversationBinding.id == binding_id,
        AgentConversationBinding.host_kind == _FLOW_NODE,
    )
    if lock:
        query = query.with_for_update()
    item = db.scalar(query)
    if item is None:
        raise not_found("flow_run_conversation_binding", binding_id)
    return item


def _binding_for_run(
    db: Session, flow_run_id: str, binding_id: str, *, lock: bool = False
) -> AgentConversationBinding:
    item = _binding(db, binding_id, lock=lock)
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
) -> AgentConversationBinding:
    """Authorize the node entry, then load the FlowRun-owned session."""

    agent_sessions.resolve_flow_node_session_host(
        db, flow_run_id=flow_run_id, attempt_id=attempt_id, require_start_permission=False
    )
    item = _binding_for_run(db, flow_run_id, binding_id, lock=lock)
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
    )
    overview = sandboxes.runtime_overview(db, flow_run_id)
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
        "desired_state": "RUNNING" if overview["write_available"] else "MAINTENANCE",
        "updated_at": now().isoformat(),
    }


def node_runtime_status(db: Session, *, flow_run_id: str, attempt_id: str) -> dict[str, Any]:
    """Project logical FlowRun Runtime health into the shared host DTO.

    Runtime generation and provider endpoint details remain server-only.
    """

    agent_sessions.resolve_flow_node_session_host(
        db,
        flow_run_id=flow_run_id,
        attempt_id=attempt_id,
        require_start_permission=False,
    )
    overview = sandboxes.runtime_overview(db, flow_run_id)
    write_available = bool(overview["write_available"])
    return {
        "state": "ACTIVE" if write_available else "RECOVERING",
        "write_available": write_available,
        "message": overview.get("diagnostic_summary"),
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
            AgentConversationBinding.lifecycle != "DELETED",
        )
        .order_by(AgentConversationBinding.created_at)
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
        )
        .order_by(AgentConversationBinding.created_at)
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
        )
        .order_by(AgentConversationBinding.created_at)
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
        )
        .order_by(AgentConversationBinding.created_at)
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


def _request_bindings(
    db: Session, attempt: NodeAttempt, node: dict[str, Any]
) -> list[dict[str, Any]]:
    asset = cast(dict[str, Any], node.get("asset") or {})
    input_contracts = {
        str(item.get("field_key") or ""): item
        for raw in cast(list[object], asset.get("inputs") or [])
        if isinstance(raw, dict)
        for item in [cast(dict[str, Any], raw)]
    }
    result: list[dict[str, Any]] = []
    for binding in db.scalars(
        select(AttemptInputBinding).where(AttemptInputBinding.attempt_id == attempt.id)
    ):
        artifact = db.get(ArtifactVersion, binding.artifact_version_id)
        if artifact is None:
            continue
        contract = input_contracts.get(binding.input_field_key, {})
        result.append(
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
    return result


def _create_native_conversation(
    db: Session,
    *,
    run: FlowRun,
    snapshot: RunSnapshot,
    node: dict[str, Any],
    workspace_ref: str,
    bindings: list[dict[str, Any]],
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
    connection = sandboxes.active_flow_run_runtime_connection(db, flow_run_id=run.id)
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
    host_root = sandboxes.flow_run_capability_path(
        run.id, snapshot.runtime_manifest_hash, "conversations", item.id
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
    )
    request = build_runtime_request(
        db,
        flow_run_id=run.id,
        runtime_manifest_hash=snapshot.runtime_manifest_hash,
        attempt_id=binding_id,
        execution_key=f"flow-run:{run.id}:conversation:create",
        node=node,
        bindings=bindings,
        workspace_ref=workspace_ref,
        interaction_mode="COLLABORATION",
        environment_image=environment.image_digest,
        environment_id=environment.environment_id,
        environment_version_id=environment.id,
        environment_version_no=environment.version_no,
        agent_spec=agent_spec,
        conversation_id=item.openhands_conversation_id,
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
        node = host.node
        workspace_ref = host.working_directory
    else:
        node = runtime_node(
            definition=snapshot.definition_json,
            manifest=snapshot.runtime_manifest_json or {},
            expected_hash=snapshot.runtime_manifest_hash,
            snapshot_id=snapshot.id,
            instance_key=node_run.flow_node_snapshot_key,
        )
        workspace_ref = attempt.workspace_ref or ""
    if host is not None:
        # Node sessions never select a logical workspace: their one permitted
        # directory is the server-frozen Attempt mount.
        work_directory_version_id, runtime_working_directory = None, host.session.working_directory
    else:
        work_directory_version_id, runtime_working_directory = (
            agent_workspace_host.flow_run_conversation_work_directory_context(
                db, run.id, payload.work_directory_id
            )
        )
    request_owner_id = str(uuid4())
    return _create_native_conversation(
        db,
        run=run,
        snapshot=snapshot,
        node=node,
        workspace_ref=workspace_ref,
        bindings=_request_bindings(db, attempt, node),
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


def bootstrap_node_conversation(
    db: Session,
    *,
    flow_run_id: str,
    attempt_id: str,
    content: str,
    attachments: tuple[dict[str, str | int], ...],
    legacy_image_urls: tuple[str, ...] = (),
    conversation_id: str | None,
    work_directory_id: str | None,
    idempotency_key: str,
    session_config: FrozenSessionConfig | None = None,
) -> dict[str, Any]:
    """Create a node binding only while delivering its first user event.

    The browser's node draft has no server representation.  This command is
    intentionally the only node entrypoint that creates a binding for an
    interactive draft, and it reloads the native Conversation before sending
    so the first event cannot race OpenHands initialization.
    """

    text = content.strip()
    if not text and not attachments and not legacy_image_urls:
        raise DomainError("AGENT_MESSAGE_EMPTY", "消息不能为空", 422)
    try:
        binding_id = str(UUID(conversation_id)) if conversation_id else str(uuid4())
    except ValueError as exc:
        raise DomainError("AGENT_CONVERSATION_ID_INVALID", "会话标识无效", 422) from exc
    host = agent_sessions.resolve_flow_node_session_host(
        db, flow_run_id=flow_run_id, attempt_id=attempt_id, require_start_permission=True
    )
    attempt = _attempt(db, attempt_id)
    node_run, run, snapshot = _attempt_context(db, attempt)
    if work_directory_id:
        raise DomainError("NODE_WORK_DIRECTORY_FIXED", "节点会话固定使用当前 Attempt 工作目录", 409)
    work_directory_version_id, working_directory = None, host.session.working_directory
    created = _create_native_conversation(
        db,
        run=run,
        snapshot=snapshot,
        node=host.node,
        workspace_ref=host.working_directory,
        bindings=_request_bindings(db, attempt, host.node),
        title=None,
        idempotency_key=idempotency_key,
        binding_id=str(uuid4()),
        node_run_id=node_run.id,
        attempt_id=attempt.id,
        work_directory_version_id=work_directory_version_id,
        runtime_working_directory=working_directory,
        session_config=session_config,
        session_binding_id=binding_id,
    )
    binding_id = str(created["id"])
    binding = _binding_for_attempt(
        db, flow_run_id=flow_run_id, attempt_id=attempt_id, binding_id=binding_id, lock=True
    )
    handle = _handle(db, binding_id)
    previous_event_id = get_runtime().reload_conversation(handle).event_id
    if legacy_image_urls:
        for value in legacy_image_urls:
            if not value.startswith("data:image/"):
                raise DomainError("AGENT_ATTACHMENT_INVALID", "图片附件无效", 422)
            base64.b64decode(value.partition(",")[2], validate=True)
        runtime = get_runtime()
        if not runtime.can_accept_input(handle):
            raise DomainError("AGENT_CONVERSATION_BUSY", "Agent 正在处理上一条消息，请稍候", 409)
        delivered = {
            "accepted": True,
            "cursor": runtime.send_message(handle, text, legacy_image_urls).cursor,
        }
    else:
        delivered = send_node_message(
            db,
            flow_run_id=flow_run_id,
            attempt_id=attempt_id,
            binding_id=binding_id,
            content=text,
            attachments=attachments,
        )
    initial_event_id = (
        binding.initial_user_event_id
        or cast(str | None, delivered.get("cursor"))
        or initial_user_event_id(handle, previous_event_id)
    )
    if initial_event_id is None:
        initial_event_id = get_runtime().reload_conversation(handle).event_id
    if initial_event_id is None:
        raise DomainError(
            "AGENT_BOOTSTRAP_DELIVERY_AMBIGUOUS",
            "首条消息正在安全对账，请稍后重试；系统不会重复发送",
            504,
            {"binding_id": binding.id},
        )
    if binding.initial_user_event_id is None:
        binding.initial_user_event_id = initial_event_id
        binding.display_title = normalized_first_sentence(text)
        binding.title_state = "PENDING"
        binding.updated_at = now()
        enqueue_title_task(db, binding, text)
        finish(db)
    return {
        "conversation": _node_session_dict(db, binding),
        "accepted": bool(delivered.get("accepted", True)),
        "cursor": initial_event_id,
    }


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


def terminal_resource_details(db: Session, binding_id: str) -> tuple[str, str, str]:
    handle = _handle(db, binding_id)
    if not handle.runtime_resource_id or not handle.runtime_resource_name:
        raise DomainError(
            "AGENT_TERMINAL_UNAVAILABLE",
            "This FlowRun Conversation has no active managed Runtime",
            409,
        )
    sandbox = sandboxes.sandbox_snapshot(db, handle.runtime_resource_id)
    raw_spec = sandbox.get("spec") if sandbox is not None else None
    spec = cast(dict[str, Any], raw_spec) if isinstance(raw_spec, dict) else {}
    environment_id = str(spec.get("environment_id") or "")
    if not environment_id:
        raise DomainError(
            "AGENT_TERMINAL_UNAVAILABLE",
            "This FlowRun Runtime has no Environment binding",
            409,
        )
    return handle.runtime_resource_name, handle.runtime_resource_id, environment_id


def flow_run_terminal_resource_details(
    db: Session, flow_run_id: str, binding_id: str
) -> tuple[str, str, str]:
    _binding_for_run(db, flow_run_id, binding_id)
    return terminal_resource_details(db, binding_id)


def read_conversation_events(
    db: Session, binding_id: str, *, cursor: str | None = None
) -> dict[str, Any]:
    """Read OpenHands events live without storing a cursor or event projection."""

    batch = get_runtime().read_events(_handle(db, binding_id, cursor=cursor))
    return _event_batch_dict(batch)


def read_flow_run_conversation_events(
    db: Session,
    flow_run_id: str,
    binding_id: str,
    *,
    cursor: str | None = None,
) -> dict[str, Any]:
    batch = get_runtime().read_events(_flow_run_handle(db, flow_run_id, binding_id, cursor=cursor))
    return _event_batch_dict(batch)


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


def _event_batch_dict(batch: RuntimeEventBatch) -> dict[str, Any]:
    return {
        "events": [
            {"id": event.cursor, "event_type": event.event_type, "payload": event.payload}
            for event in batch.events
        ],
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
    item.last_connected_at = now()
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
) -> dict[str, Any]:
    """Send the same attachment-aware native message as the outer workbench."""

    if not content.strip() and not attachments:
        raise DomainError("AGENT_MESSAGE_EMPTY", "消息不能为空", 422)
    binding = _binding_for_attempt(
        db, flow_run_id=flow_run_id, attempt_id=attempt_id, binding_id=binding_id, lock=True
    )
    validate_attachment_owners(binding.id, attachments)
    prompt, image_urls = message_payload(content, attachments)
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
    binding.last_connected_at = now()
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
        connection = sandboxes.active_flow_run_runtime_connection(db, flow_run_id=flow_run_id)
        handle = RuntimeHandle(
            job_id=f"flow-run:{flow_run_id}",
            conversation_id="",
            runtime_resource_id=connection.managed_runtime_id,
            runtime_resource_name=connection.resource_name,
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
    host_root = sandboxes.flow_run_capability_path(
        flow_run_id, snapshot.runtime_manifest_hash, "conversations", binding.id
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
    binding.lifecycle = "DELETED"
    binding.deleted_at = now()
    finish(db)


def rerun_node_message(
    db: Session, *, flow_run_id: str, attempt_id: str, binding_id: str, event_id: str, content: str
) -> dict[str, Any]:
    if not content.strip():
        raise DomainError("AGENT_MESSAGE_EMPTY", "消息不能为空", 422)
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
) -> tuple[str, str, str]:
    _binding_for_attempt(db, flow_run_id=flow_run_id, attempt_id=attempt_id, binding_id=binding_id)
    return flow_run_terminal_resource_details(db, flow_run_id, binding_id)


def node_draft_terminal_resource_details(
    db: Session, *, flow_run_id: str, attempt_id: str
) -> tuple[str, str, str, str]:
    """Open a terminal before first message in the Attempt's fixed directory."""

    host = agent_sessions.resolve_flow_node_session_host(
        db, flow_run_id=flow_run_id, attempt_id=attempt_id, require_start_permission=False
    )
    connection = sandboxes.active_flow_run_runtime_connection(db, flow_run_id=flow_run_id)
    sandbox = sandboxes.sandbox_snapshot(db, connection.managed_runtime_id)
    spec = cast(dict[str, Any], sandbox.get("spec") if sandbox else {})
    environment_id = str(spec.get("environment_id") or "")
    if not environment_id:
        raise DomainError(
            "AGENT_TERMINAL_UNAVAILABLE", "This FlowRun Runtime has no Environment binding", 409
        )
    return (
        connection.resource_name,
        connection.managed_runtime_id,
        environment_id,
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
) -> dict[str, bool]:
    return {
        "ready": get_runtime().can_accept_input(
            _node_handle(db, flow_run_id=flow_run_id, attempt_id=attempt_id, binding_id=binding_id)
        )
    }


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
    get_runtime().interrupt(
        _node_handle(db, flow_run_id=flow_run_id, attempt_id=attempt_id, binding_id=binding_id)
    )
    return {"accepted": True}


def resume_node_conversation(
    db: Session, *, flow_run_id: str, attempt_id: str, binding_id: str
) -> dict[str, Any]:
    result = get_runtime().run(
        _node_handle(db, flow_run_id=flow_run_id, attempt_id=attempt_id, binding_id=binding_id)
    )
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
