from __future__ import annotations

import base64
import hashlib
from dataclasses import asdict, replace
from typing import Any, cast
from uuid import uuid4

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from flowweave.modules.catalog.public import resolve_snapshot_memory
from flowweave.modules.conversations.application.locator import (
    active_runtime_handle,
    bind_openhands_conversation,
    binding_locator,
)
from flowweave.modules.conversations.infrastructure.models import (
    FlowRunConversationBinding,
)
from flowweave.modules.environments.public import (
    lock_referenceable_version,
    validate_runtime_manifest,
)
from flowweave.modules.sandboxes import public as sandboxes
from flowweave.runtime.base import RuntimeEventBatch, RuntimeHandle, RuntimeResult
from flowweave.runtime.dependencies import get_runtime
from flowweave.runtime.manifest import runtime_node
from flowweave.runtime.request import (
    build_runtime_request,
    frozen_memory_policy,
    resolve_runtime_selection,
)
from flowweave.runtime.workspace import attempt_workspace_path, materialize_runtime_memory
from flowweave.shared.application.transactions import finish
from flowweave.shared.database import now
from flowweave.shared.errors import DomainError, conflict, not_found
from flowweave.shared.models import (
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


def _binding(db: Session, binding_id: str, *, lock: bool = False) -> FlowRunConversationBinding:
    query = select(FlowRunConversationBinding).where(FlowRunConversationBinding.id == binding_id)
    if lock:
        query = query.with_for_update()
    item = db.scalar(query)
    if item is None:
        raise not_found("flow_run_conversation_binding", binding_id)
    return item


def _binding_for_run(
    db: Session, flow_run_id: str, binding_id: str, *, lock: bool = False
) -> FlowRunConversationBinding:
    item = _binding(db, binding_id, lock=lock)
    if item.flow_run_id != flow_run_id:
        raise not_found("flow_run_conversation_binding", binding_id)
    return item


def _binding_dict(item: FlowRunConversationBinding) -> dict[str, Any]:
    return {
        "id": item.id,
        "flow_run_id": item.flow_run_id,
        "runtime_session_id": item.runtime_session_id,
        "openhands_conversation_id": item.openhands_conversation_id,
        "display_label": item.display_label,
        "created_at": item.created_at.isoformat(),
        "last_connected_at": item.last_connected_at.isoformat(),
    }


def list_conversations(db: Session, attempt_id: str) -> list[dict[str, Any]]:
    """List the FlowRun's locators; an Attempt is context, never the owner."""

    attempt = _attempt(db, attempt_id)
    _, run, _ = _attempt_context(db, attempt)
    items = db.scalars(
        select(FlowRunConversationBinding)
        .where(FlowRunConversationBinding.flow_run_id == run.id)
        .order_by(FlowRunConversationBinding.created_at)
    )
    return [_binding_dict(item) for item in items]


def list_flow_run_conversations(db: Session, flow_run_id: str) -> list[dict[str, Any]]:
    if db.get(FlowRun, flow_run_id) is None:
        raise not_found("flow_run", flow_run_id)
    items = db.scalars(
        select(FlowRunConversationBinding)
        .where(FlowRunConversationBinding.flow_run_id == flow_run_id)
        .order_by(FlowRunConversationBinding.created_at)
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
    item.display_label = title.strip() or None
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
    model_name: str | None,
    reasoning_effort: str | None,
    idempotency_key: str,
    binding_id: str,
    node_run_id: str | None = None,
    attempt_id: str | None = None,
) -> dict[str, Any]:
    if run.state in {"COMPLETED", "CANCELLED"}:
        raise DomainError(
            "FLOW_RUN_TERMINAL",
            "A completed or cancelled FlowRun cannot create Conversations",
            409,
            {"flow_run_id": run.id, "state": run.state},
        )
    count = db.scalar(
        select(func.count(FlowRunConversationBinding.id)).where(
            FlowRunConversationBinding.flow_run_id == run.id
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
    model_name, reasoning_effort = resolve_runtime_selection(db, node, model_name, reasoning_effort)
    memory_enabled, source_refs = frozen_memory_policy(node, runtime_scope="CONVERSATION")
    if memory_enabled:
        materials = resolve_snapshot_memory(
            db,
            snapshot_id=snapshot.id,
            source_refs=source_refs,
            allowed_scopes={"USER", "PROJECT"},
        )
        runtime_allocation = sandboxes.runtime_allocation_for_flow_run(
            db, run.id, manifest_digest=snapshot.runtime_manifest_hash
        )
        with sandboxes.capability_materialization_lock(runtime_allocation):
            materialize_runtime_memory(
                flow_run_id=run.id,
                manifest_digest=snapshot.runtime_manifest_hash,
                workspace_ref=workspace_ref,
                materials=materials,
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
        model_name=model_name,
        reasoning_effort=reasoning_effort,
        environment_image=environment.image_digest,
        environment_id=environment.environment_id,
        environment_version_id=environment.id,
        environment_version_no=environment.version_no,
        memory_materialized=memory_enabled,
    )
    connection = sandboxes.active_flow_run_runtime_connection(db, flow_run_id=run.id)
    request = replace(
        request,
        runtime_sandbox_id=connection.managed_runtime_id,
        runtime_resource_name=connection.resource_name,
        runtime_base_url=f"http://{connection.resource_name}:8000",
    )
    handle = get_runtime().create_conversation(request)
    item = bind_openhands_conversation(
        db,
        flow_run_id=run.id,
        openhands_conversation_id=handle.conversation_id,
        display_label=title,
        binding_id=binding_id,
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
    node = runtime_node(
        definition=snapshot.definition_json,
        manifest=snapshot.runtime_manifest_json or {},
        expected_hash=snapshot.runtime_manifest_hash,
        snapshot_id=snapshot.id,
        instance_key=node_run.flow_node_snapshot_key,
    )
    request_owner_id = str(uuid4())
    return _create_native_conversation(
        db,
        run=run,
        snapshot=snapshot,
        node=node,
        workspace_ref=attempt.workspace_ref or "",
        bindings=_request_bindings(db, attempt, node),
        title=payload.title,
        model_name=payload.model_name,
        reasoning_effort=payload.reasoning_effort,
        idempotency_key=idempotency_key,
        binding_id=request_owner_id,
        node_run_id=node_run.id,
        attempt_id=attempt.id,
    )


def create_flow_run_conversation(
    db: Session,
    flow_run_id: str,
    payload: FlowRunConversationCreateWrite,
    idempotency_key: str,
) -> dict[str, Any]:
    """Create a Conversation from the FlowRun's latest execution context.

    The public client selects a FlowRun, not an Attempt owner.  Attempts remain
    internal execution context used to compile the frozen OpenHands request.
    """

    existing = db.scalar(select(HumanAction).where(HumanAction.idempotency_key == idempotency_key))
    if existing is not None:
        binding_id = str((existing.payload_json or {}).get("binding_id") or "")
        if binding_id:
            return get_conversation(db, binding_id)
        raise conflict("conversation creation outcome is unavailable")
    run = db.get(FlowRun, flow_run_id)
    if run is None:
        raise not_found("flow_run", flow_run_id)
    if sandboxes.runtime_overview(db, flow_run_id)["rerun_required"]:
        raise DomainError(
            "LEGACY_RUNTIME_INCOMPATIBLE",
            "Historical FlowRun Runtime data is incompatible; rerun the Flow",
            409,
            {"flow_run_id": flow_run_id},
        )
    attempt = db.scalar(
        select(NodeAttempt)
        .join(NodeRun, NodeRun.id == NodeAttempt.node_run_id)
        .where(NodeRun.flow_run_id == flow_run_id)
        .order_by(NodeRun.sequence_no.desc(), NodeAttempt.attempt_no.desc())
        .limit(1)
    )
    if attempt is not None:
        return create_conversation(
            db,
            attempt.id,
            ConversationCreateWrite(
                title=payload.title,
                expected_attempt_state_version=attempt.state_version,
                model_name=payload.model_name,
                reasoning_effort=payload.reasoning_effort,
            ),
            idempotency_key,
        )
    snapshot = db.get(RunSnapshot, run.active_snapshot_id) if run.active_snapshot_id else None
    if snapshot is None:
        raise DomainError("SNAPSHOT_INVALID", "FlowRun Snapshot is unavailable", 409)
    nodes = cast(list[dict[str, Any]], snapshot.definition_json.get("nodes") or [])
    default_key = str(snapshot.definition_json.get("default_entry_key") or "")
    selected = next(
        (item for item in nodes if str(item.get("instance_key") or "") == default_key),
        nodes[0] if nodes else None,
    )
    if selected is None:
        raise DomainError(
            "FLOW_RUN_CONVERSATION_CONTEXT_REQUIRED",
            "The FlowRun Snapshot has no node context for a Conversation",
            409,
            {"flow_run_id": flow_run_id},
        )
    node = runtime_node(
        definition=snapshot.definition_json,
        manifest=snapshot.runtime_manifest_json or {},
        expected_hash=snapshot.runtime_manifest_hash,
        snapshot_id=snapshot.id,
        instance_key=str(selected["instance_key"]),
    )
    asset = cast(dict[str, Any], node.get("asset") or {})
    binding_id = str(uuid4())
    workspace_ref = str(
        attempt_workspace_path(
            asset_id=str(asset.get("id") or ""),
            run_id=run.id,
            node_run_id=f"conversation-{binding_id}",
            attempt_no=1,
        )
    )
    return _create_native_conversation(
        db,
        run=run,
        snapshot=snapshot,
        node=node,
        workspace_ref=workspace_ref,
        bindings=[],
        title=payload.title,
        model_name=payload.model_name,
        reasoning_effort=payload.reasoning_effort,
        idempotency_key=idempotency_key,
        binding_id=binding_id,
    )


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
    result = get_runtime().send_message(_handle(db, binding_id), text, image_urls)
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
    return {"accepted": True, "runtime": _result_dict(result)}


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
    "flow_run_runtime_stream_details",
    "flow_run_terminal_resource_details",
    "get_conversation",
    "get_flow_run_conversation",
    "list_conversations",
    "list_flow_run_conversations",
    "patch_conversation",
    "patch_flow_run_conversation",
    "read_conversation_events",
    "read_flow_run_conversation_events",
    "runtime_stream_details",
    "send_question",
    "send_flow_run_question",
    "stop_conversation",
    "stop_flow_run_conversation",
    "terminal_resource_details",
)
