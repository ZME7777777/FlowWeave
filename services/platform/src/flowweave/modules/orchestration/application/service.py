from __future__ import annotations

import copy
import hashlib
import json
import shutil
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Literal, cast
from uuid import uuid4

from sqlalchemy import delete, func, select, update
from sqlalchemy.orm import Session

from flowweave.modules.catalog.public import (
    describe_agent_profile_version,
    describe_asset,
    hold_snapshot_memory_references,
    resolve_snapshot_memory,
)
from flowweave.modules.conversations import public as conversations
from flowweave.modules.environments.public import (
    lock_referenceable_version,
    validate_runtime_manifest,
)
from flowweave.modules.flows.public import describe_flow, load_flow
from flowweave.modules.gates.public import (
    GateExecutionPlan,
    GateResult,
    execute_gate_plan,
    prepare_gate,
)
from flowweave.modules.runs.public import (
    Artifact,
    Binding,
    InputField,
    evaluate_readiness,
)
from flowweave.modules.sandboxes import public as sandboxes
from flowweave.modules.tasks.public import Lease, enqueue, lease_is_current
from flowweave.runtime.base import (
    RuntimeHandle,
    RuntimePendingConfirmation,
    RuntimeResult,
    StartAttemptRequest,
)
from flowweave.runtime.contract import compile_runtime_contract
from flowweave.runtime.dependencies import get_runtime
from flowweave.runtime.manifest import (
    runtime_manifest_hash as _runtime_manifest_hash,
)
from flowweave.runtime.manifest import (
    runtime_node,
)
from flowweave.runtime.request import (
    build_runtime_request,
    frozen_memory_policy,
    resolve_runtime_provider,
    resolve_runtime_selection,
)
from flowweave.runtime.routing import runtime_for
from flowweave.runtime.workspace import (
    attempt_workspace_path,
    materialize_runtime_memory,
)
from flowweave.shared.application.transactions import (
    finish,
    register_commit_action,
    register_rollback_action,
)
from flowweave.shared.artifact_store import get_artifact_store
from flowweave.shared.domain.tool_policy import OPENHANDS_VERSION
from flowweave.shared.errors import DomainError, conflict, illegal, not_found
from flowweave.shared.models import (
    ArtifactVersion,
    AttemptInputBinding,
    AttemptState,
    BackgroundTask,
    EnvironmentVersion,
    FlowRun,
    FlowRunConversationBinding,
    FlowRunState,
    GateEvaluation,
    HumanAction,
    NodeAsset,
    NodeAttempt,
    NodeRun,
    NodeRunState,
    RunEvent,
    RunSnapshot,
    RuntimeConfirmationApproval,
    TaskState,
    now,
)
from flowweave.shared.schemas import (
    AgentProfileSwitchWrite,
    ArtifactWrite,
    AttemptStartWrite,
    AttemptVersionWrite,
    CondenserWrite,
    HumanInputWrite,
    InputBindingsWrite,
    NodeRunStart,
    RejectWrite,
    RunStart,
    RuntimeCancelRecoveryWrite,
    RuntimeConfirmationDecisionWrite,
    SyncSnapshotWrite,
)
from flowweave.shared.settings import get_settings

def _hash(value: Any) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, default=str).encode()).hexdigest()


def _compile_runtime_manifest(definition: dict[str, Any]) -> dict[str, Any]:
    """Compile the complete, replayable Agent specification for every node."""

    nodes: dict[str, dict[str, Any]] = {}
    raw_nodes: object = definition.get("nodes", [])
    if not isinstance(raw_nodes, list):
        raise DomainError("SNAPSHOT_INVALID", "Snapshot nodes are invalid", 409)
    for raw_node in cast(list[object], raw_nodes):
        if not isinstance(raw_node, dict):
            raise DomainError("SNAPSHOT_INVALID", "Snapshot node is invalid", 409)
        node = cast(dict[str, Any], raw_node)
        instance_key = str(node.get("instance_key") or "")
        raw_asset: object = node.get("asset")
        if not instance_key or not isinstance(raw_asset, dict):
            raise DomainError("SNAPSHOT_INVALID", "Snapshot node asset is invalid", 409)
        asset = cast(dict[str, Any], raw_asset)
        raw_capabilities: object = asset.get("capabilities", [])
        if not isinstance(raw_capabilities, list):
            raise DomainError("SNAPSHOT_INVALID", "Snapshot capabilities are invalid", 409)
        capabilities: list[dict[str, Any]] = []
        tool_policies: list[dict[str, Any]] = []
        context_policies: list[dict[str, Any]] = []
        memory_policies: list[dict[str, Any]] = []
        critic_policies: list[dict[str, Any]] = []
        agent_profiles: list[dict[str, Any]] = []
        agent_definitions: list[dict[str, Any]] = []
        plugins: list[dict[str, Any]] = []
        for raw_capability in cast(list[object], raw_capabilities):
            if not isinstance(raw_capability, dict):
                raise DomainError("SNAPSHOT_INVALID", "Snapshot capability is invalid", 409)
            capability = cast(dict[str, Any], raw_capability)
            raw_config: object = capability.get("normalized_config")
            if not isinstance(raw_config, dict):
                raise DomainError("SNAPSHOT_INVALID", "Snapshot capability config is invalid", 409)
            config = cast(dict[str, Any], raw_config)
            version_id = str(
                capability.get("capability_id") or config.get("capability_version_id") or ""
            )
            digest = str(config.get("digest") or "")
            content_hash = str(config.get("content_hash") or "")
            if len(version_id) != 36 or len(digest) != 64 or len(content_hash) != 64:
                raise DomainError(
                    "SNAPSHOT_INVALID",
                    "Snapshot capability lacks an immutable version",
                    409,
                    {"instance_key": instance_key},
                )
            frozen = {
                "capability_version_id": version_id,
                "capability_type": str(capability.get("capability_type") or ""),
                "capability_key": str(capability.get("capability_key") or ""),
                "digest": digest,
                "content_hash": content_hash,
                "runtime_config": copy.deepcopy(config),
            }
            if frozen["capability_type"] == "TOOL_POLICY":
                tool_policies.append(frozen)
            elif frozen["capability_type"] == "CONTEXT_POLICY":
                context_policies.append(frozen)
            elif frozen["capability_type"] == "MEMORY_POLICY":
                memory_policies.append(frozen)
            elif frozen["capability_type"] == "CRITIC_POLICY":
                critic_policies.append(frozen)
            elif frozen["capability_type"] == "AGENT_PROFILE":
                agent_profiles.append(frozen)
            elif frozen["capability_type"] == "AGENT_DEFINITION":
                agent_definitions.append(frozen)
            elif frozen["capability_type"] == "PLUGIN":
                plugins.append(frozen)
            else:
                capabilities.append(frozen)
        if len(tool_policies) != 1:
            raise DomainError(
                "SNAPSHOT_INVALID",
                "Snapshot node must freeze exactly one Tool Policy",
                409,
                {"instance_key": instance_key, "tool_policy_count": len(tool_policies)},
            )
        if len(context_policies) != 1:
            raise DomainError(
                "SNAPSHOT_INVALID",
                "Snapshot node must freeze exactly one Context Policy",
                409,
                {
                    "instance_key": instance_key,
                    "context_policy_count": len(context_policies),
                },
            )
        if len(memory_policies) != 1:
            raise DomainError(
                "SNAPSHOT_INVALID",
                "Snapshot node must freeze exactly one Memory Policy",
                409,
                {
                    "instance_key": instance_key,
                    "memory_policy_count": len(memory_policies),
                },
            )
        if len(critic_policies) != 1:
            raise DomainError(
                "SNAPSHOT_INVALID",
                "Snapshot node must freeze exactly one Critic Policy",
                409,
                {
                    "instance_key": instance_key,
                    "critic_policy_count": len(critic_policies),
                },
            )
        if len(agent_profiles) > 1:
            raise DomainError(
                "SNAPSHOT_INVALID",
                "Snapshot node can freeze at most one Agent Profile",
                409,
                {
                    "instance_key": instance_key,
                    "agent_profile_count": len(agent_profiles),
                },
            )
        executor = asset.get("executor")
        if not isinstance(executor, dict):
            raise DomainError("SNAPSHOT_INVALID", "Snapshot executor is invalid", 409)
        executor_config = cast(dict[str, Any], executor)
        raw_tool_config = cast(dict[str, Any], tool_policies[0]["runtime_config"])
        raw_tools = raw_tool_config.get("tools")
        if not isinstance(raw_tools, list):
            raise DomainError(
                "SNAPSHOT_INVALID",
                "Snapshot Tool Policy tools are invalid",
                409,
                {"instance_key": instance_key},
            )
        required_tools = tuple(
            str(cast(dict[str, Any], item).get("name") or "")
            for item in cast(list[object], raw_tools)
            if isinstance(item, dict)
        )
        nodes[instance_key] = {
            "node_asset_id": str(node.get("node_asset_id") or asset.get("id") or ""),
            "capabilities": capabilities,
            "agent_spec": {
                "schema_version": 1,
                "agent_kind": "OPENHANDS",
                "openhands_version": OPENHANDS_VERSION,
                "runtime_contract": compile_runtime_contract(required_tools),
                "tool_policy": tool_policies[0],
                "context_policy": context_policies[0],
                "memory_policy": memory_policies[0],
                "critic_policy": critic_policies[0],
                "agent_profile": agent_profiles[0] if agent_profiles else None,
                "agent_definitions": agent_definitions,
                "plugins": plugins,
                "confirmation_policy": str(executor_config.get("confirmation_policy") or "ALWAYS"),
                "condenser": copy.deepcopy(executor_config.get("condenser") or {"kind": "NO_OP"}),
                "budgets": {"max_iterations": int(executor_config.get("max_iterations") or 100)},
            },
        }
    return {"schema_version": 2, "openhands_version": OPENHANDS_VERSION, "nodes": nodes}


def _runtime_node(snapshot: RunSnapshot, instance_key: str) -> dict[str, Any]:
    return runtime_node(
        definition=snapshot.definition_json,
        manifest=snapshot.runtime_manifest_json or {},
        expected_hash=snapshot.runtime_manifest_hash,
        snapshot_id=snapshot.id,
        instance_key=instance_key,
    )


def _event(
    db: Session,
    run_id: str,
    event_type: str,
    payload: dict[str, Any] | None = None,
    node_run_id: str | None = None,
    attempt_id: str | None = None,
) -> None:
    db.add(
        RunEvent(
            flow_run_id=run_id,
            node_run_id=node_run_id,
            attempt_id=attempt_id,
            event_type=event_type,
            payload_json=payload or {},
        )
    )


def _action(
    db: Session,
    run_id: str,
    action_type: str,
    key: str,
    payload: dict[str, Any] | None = None,
    node_run_id: str | None = None,
    attempt_id: str | None = None,
) -> HumanAction:
    existing = db.scalar(select(HumanAction).where(HumanAction.idempotency_key == key))
    if existing:
        return existing
    item = HumanAction(
        flow_run_id=run_id,
        node_run_id=node_run_id,
        attempt_id=attempt_id,
        action_type=action_type,
        idempotency_key=key,
        payload_json=payload or {},
    )
    db.add(item)
    db.flush()
    return item


def _snapshot_definition(
    db: Session, flow_id: str, *, environment_version_id: str | None = None
) -> dict[str, Any]:
    definition = describe_flow(db, load_flow(db, flow_id))
    if environment_version_id is not None:
        definition["environment_version_id"] = environment_version_id
    for node in definition["nodes"]:
        asset = db.get(NodeAsset, node["node_asset_id"])
        if not asset:
            raise not_found("node_asset", node["node_asset_id"])
        node["asset"] = describe_asset(db, asset)
    return definition


def _node(snapshot: RunSnapshot, instance_key: str) -> dict[str, Any]:
    for item in snapshot.definition_json["nodes"]:
        if item["instance_key"] == instance_key:
            return item
    raise not_found("flow_node_snapshot", instance_key)


def _confirmation_policy(value: object, *, source: str) -> Literal["ALWAYS", "NEVER"]:
    policy = str(value or "ALWAYS")
    if policy not in {"ALWAYS", "NEVER"}:
        raise DomainError(
            "SNAPSHOT_INVALID",
            "OpenHands confirmation policy is invalid",
            409,
            {"source": source, "confirmation_policy": policy},
        )
    return cast(Literal["ALWAYS", "NEVER"], policy)


def _snapshot_confirmation_policy(node: dict[str, Any]) -> Literal["ALWAYS", "NEVER"]:
    asset = cast(dict[str, Any], node.get("asset") or {})
    executor = cast(dict[str, Any], asset.get("executor") or {})
    return _confirmation_policy(
        executor.get("confirmation_policy"), source="snapshot.node.asset.executor"
    )


def _snapshot_condenser_config(node: dict[str, Any]) -> dict[str, Any]:
    """Validate and freeze the governed condenser policy from a Run Snapshot."""

    asset = cast(dict[str, Any], node.get("asset") or {})
    executor = cast(dict[str, Any], asset.get("executor") or {})
    raw = executor.get("condenser") or {"kind": "NO_OP"}
    try:
        return CondenserWrite.model_validate(raw).model_dump(mode="json")
    except ValueError as exc:
        raise DomainError(
            "SNAPSHOT_INVALID",
            "OpenHands condenser policy is invalid",
            409,
            {"source": "snapshot.node.asset.executor.condenser"},
        ) from exc


def _run(db: Session, run_id: str) -> FlowRun:
    item = db.get(FlowRun, run_id)
    if not item:
        raise not_found("flow_run", run_id)
    return item


def _attempt(db: Session, attempt_id: str) -> NodeAttempt:
    item = db.get(NodeAttempt, attempt_id)
    if not item:
        raise not_found("node_attempt", attempt_id)
    return item


def _active_attempt_runtime_handle(
    db: Session,
    attempt: NodeAttempt,
    *,
    conversation_id: str | None = None,
    cursor: str | None = None,
    route_kind: str = "EXECUTION",
) -> RuntimeHandle:
    openhands_conversation_id = conversation_id or attempt.conversation_id
    if not openhands_conversation_id:
        raise DomainError(
            "RUNTIME_CONVERSATION_UNAVAILABLE",
            "The Attempt has no OpenHands Conversation identity",
            409,
            {"attempt_id": attempt.id},
        )
    flow_run_id = _node_run(db, attempt.node_run_id).flow_run_id
    return conversations.active_runtime_handle(
        db,
        flow_run_id=flow_run_id,
        openhands_conversation_id=openhands_conversation_id,
        # Runtime cursors are transport-local and are never persisted by FlowWeave.
        cursor=cursor,
        route_kind=route_kind,
    )


def _node_run(db: Session, node_run_id: str) -> NodeRun:
    item = db.get(NodeRun, node_run_id)
    if not item:
        raise not_found("node_run", node_run_id)
    return item


def _snapshot(db: Session, snapshot_id: str) -> RunSnapshot:
    item = db.get(RunSnapshot, snapshot_id)
    if not item:
        raise not_found("run_snapshot", snapshot_id)
    return item


def _active_snapshot(db: Session, run: FlowRun) -> RunSnapshot:
    if not run.active_snapshot_id:
        raise DomainError("INTERNAL_ERROR", "run has no active snapshot", 500)
    return _snapshot(db, run.active_snapshot_id)


def _artifact_dict(item: ArtifactVersion) -> dict[str, Any]:
    return {
        "id": item.id,
        "flow_run_id": item.flow_run_id,
        "producer_attempt_id": item.producer_attempt_id,
        "field_key": item.field_key,
        "version_no": item.version_no,
        "artifact_type": item.artifact_type,
        "storage_key": item.storage_key,
        "uri": item.uri,
        "inline_content": item.inline_content,
        "content_hash": item.content_hash,
        "byte_size": item.byte_size,
        "mime_type": item.mime_type,
        "source": item.source,
        "metadata": item.metadata_json,
        "created_at": item.created_at.isoformat(),
    }


@dataclass(frozen=True, slots=True)
class PreparedArtifact:
    id: str
    payload: ArtifactWrite
    inline_content: str | None
    storage_key: str | None
    content_hash: str
    byte_size: int


@dataclass(frozen=True, slots=True)
class ArtifactContentReference:
    inline_content: str | None
    storage_key: str | None
    uri: str | None
    mime_type: str
    filename: str


def prepare_artifact(payload: ArtifactWrite, artifact_id: str | None = None) -> PreparedArtifact:
    """Write large content before the database transaction and freeze its metadata."""

    content = payload.inline_content
    encoded = (content or payload.uri or "").encode()
    identifier = artifact_id or str(uuid4())
    storage_key: str | None = None
    if content is not None and len(encoded) > get_settings().inline_artifact_limit:
        storage_key = get_artifact_store().put(f"artifacts/versions/{identifier}", encoded)
        content = None
    return PreparedArtifact(
        id=identifier,
        payload=payload,
        inline_content=content,
        storage_key=storage_key,
        content_hash=hashlib.sha256(encoded).hexdigest(),
        byte_size=len(encoded),
    )


def discard_prepared_artifacts(items: list[PreparedArtifact]) -> None:
    """Idempotently compensate objects whose database transaction did not commit."""

    store = get_artifact_store()
    for item in items:
        if item.storage_key is not None:
            store.delete(item.storage_key)


def _register_artifact(
    db: Session,
    run_id: str,
    prepared: PreparedArtifact,
    *,
    source: str = "HUMAN",
    attempt_id: str | None = None,
) -> ArtifactVersion:
    payload = prepared.payload
    if prepared.storage_key is not None:
        key = prepared.storage_key
        register_rollback_action(db, lambda key=key: get_artifact_store().delete(key))
    version = (
        db.scalar(
            select(func.max(ArtifactVersion.version_no)).where(
                ArtifactVersion.flow_run_id == run_id,
                ArtifactVersion.field_key == payload.field_key,
            )
        )
        or 0
    ) + 1
    item = ArtifactVersion(
        id=prepared.id,
        flow_run_id=run_id,
        producer_attempt_id=attempt_id,
        field_key=payload.field_key,
        version_no=version,
        artifact_type=payload.artifact_type,
        storage_key=prepared.storage_key,
        uri=payload.uri,
        inline_content=prepared.inline_content,
        content_hash=prepared.content_hash,
        byte_size=prepared.byte_size,
        mime_type=payload.mime_type,
        source=source,
        metadata_json=payload.metadata,
    )
    db.add(item)
    db.flush()
    _event(
        db,
        run_id,
        "ARTIFACT_VERSION_CREATED",
        {"artifact_id": item.id, "field_key": item.field_key, "version_no": version},
        attempt_id=attempt_id,
    )
    return item


def create_artifact(db: Session, run_id: str, prepared: PreparedArtifact) -> dict[str, Any]:
    run = _run(db, run_id)
    if run.state in {FlowRunState.COMPLETED, FlowRunState.CANCELLED}:
        raise illegal("cannot add artifact to terminal run", state=run.state)
    item = _register_artifact(db, run_id, prepared)
    finish(db)
    return _artifact_dict(item)


def delete_artifact(db: Session, run_id: str, artifact_id: str) -> None:
    """Delete an unbound human-provided artifact from a run's artifact pool."""

    run = _run(db, run_id)
    if run.state in {FlowRunState.COMPLETED, FlowRunState.CANCELLED}:
        raise illegal("cannot remove artifact from terminal run", state=run.state)
    item = db.get(ArtifactVersion, artifact_id)
    if item is None or item.flow_run_id != run.id:
        raise not_found("artifact_version", artifact_id)
    if item.producer_attempt_id is not None or item.source != "HUMAN":
        raise DomainError(
            "ARTIFACT_DELETE_BLOCKED",
            "Only human-provided artifacts can be removed from the artifact pool",
            409,
            {"id": artifact_id},
        )
    binding_count = (
        db.scalar(
            select(func.count())
            .select_from(AttemptInputBinding)
            .where(AttemptInputBinding.artifact_version_id == artifact_id)
        )
        or 0
    )
    if binding_count:
        raise DomainError(
            "ARTIFACT_DELETE_BLOCKED",
            "Artifact is already bound to a node attempt",
            409,
            {"id": artifact_id, "binding_count": binding_count},
        )
    storage_key = item.storage_key
    db.delete(item)
    _event(db, run.id, "ARTIFACT_VERSION_DELETED", {"artifact_id": artifact_id})
    if storage_key:
        register_commit_action(db, lambda key=storage_key: get_artifact_store().delete(key))
    finish(db)


def artifact_content_reference(db: Session, artifact_id: str) -> ArtifactContentReference:
    """Read an immutable content pointer inside a short database transaction."""

    item = db.get(ArtifactVersion, artifact_id)
    if not item:
        raise not_found("artifact_version", artifact_id)
    configured_name = str(item.metadata_json.get("filename", ""))
    filename = (
        Path(configured_name).name
        if configured_name
        else f"{item.field_key}-v{item.version_no}.txt"
    )
    return ArtifactContentReference(
        inline_content=item.inline_content,
        storage_key=item.storage_key,
        uri=item.uri,
        mime_type=item.mime_type,
        filename=filename,
    )


def read_artifact_content(reference: ArtifactContentReference) -> tuple[bytes, str, str]:
    """Read object storage after the database transaction has ended."""

    if reference.inline_content is not None:
        content = reference.inline_content.encode("utf-8")
    elif reference.storage_key:
        try:
            content = get_artifact_store().read(reference.storage_key)
        except FileNotFoundError as exc:
            raise DomainError(
                "ARTIFACT_UNAVAILABLE", "Artifact content is unavailable", 404
            ) from exc
    elif reference.uri:
        raise DomainError(
            "ARTIFACT_EXTERNAL",
            "External artifact content must be opened at its source URI",
            409,
            {"uri": reference.uri},
        )
    else:
        raise DomainError("ARTIFACT_UNAVAILABLE", "Artifact content is unavailable", 404)
    return content, reference.mime_type, reference.filename


def _input_fields(node: dict[str, Any]) -> tuple[InputField, ...]:
    return tuple(InputField(x["field_key"], x["data_type"]) for x in node["asset"]["inputs"])


def _validate_input_bindings(
    db: Session,
    run: FlowRun,
    node: dict[str, Any],
    artifact_ids: dict[str, str],
) -> None:
    input_types = {field.key: field.data_type for field in _input_fields(node)}
    unknown_fields = sorted(set(artifact_ids) - set(input_types))
    if unknown_fields:
        raise DomainError(
            "INPUT_BINDING_INVALID",
            "binding field is not an input of the target node",
            422,
            {"fields": unknown_fields},
        )
    for field_key, artifact_id in artifact_ids.items():
        artifact = db.get(ArtifactVersion, artifact_id)
        if artifact is None or artifact.flow_run_id != run.id:
            raise DomainError(
                "INPUT_BINDING_INVALID",
                "artifact does not belong to run",
                422,
                {"id": artifact_id, "field": field_key},
            )
        if artifact.artifact_type != input_types[field_key]:
            raise DomainError(
                "INPUT_BINDING_INVALID",
                "artifact type does not match the target input",
                422,
                {
                    "id": artifact_id,
                    "field": field_key,
                    "expected": input_types[field_key],
                    "actual": artifact.artifact_type,
                },
            )


def _bindings(db: Session, attempt_id: str) -> list[AttemptInputBinding]:
    return list(
        db.scalars(
            select(AttemptInputBinding)
            .where(AttemptInputBinding.attempt_id == attempt_id)
            .order_by(AttemptInputBinding.input_field_key)
        )
    )


def _inline_execution() -> bool:
    return get_settings().execution_mode == "inline"


def _task_key(task_type: str, attempt: NodeAttempt, suffix: str = "") -> str:
    tail = f":{suffix}" if suffix else ""
    return f"{task_type.lower()}:{attempt.id}:v{attempt.state_version}{tail}"


def _dispatch_readiness(db: Session, attempt: NodeAttempt) -> None:
    if _inline_execution():
        _evaluate_readiness(db, attempt)
        return
    enqueue(
        db,
        task_type="EVALUATE_READINESS",
        aggregate_type="ATTEMPT",
        aggregate_id=attempt.id,
        idempotency_key=_task_key("EVALUATE_READINESS", attempt),
    )


def _dispatch_gates(db: Session, attempt: NodeAttempt, stage: str) -> None:
    if _inline_execution():
        _run_gates_inline(db, attempt, stage)
        return
    enqueue(
        db,
        task_type="RUN_GATE_POLICY",
        aggregate_type="ATTEMPT",
        aggregate_id=attempt.id,
        idempotency_key=_task_key("RUN_GATE_POLICY", attempt, stage),
        payload={"stage": stage},
    )


def _dispatch_poll(
    db: Session,
    attempt: NodeAttempt,
    poll_no: int,
    *,
    delayed: bool,
) -> None:
    enqueue(
        db,
        task_type="POLL_RUNTIME",
        aggregate_type="ATTEMPT",
        aggregate_id=attempt.id,
        idempotency_key=f"poll-runtime:{attempt.id}:{poll_no}",
        payload={"poll_no": poll_no},
        available_at=(
            datetime.now(UTC) + timedelta(seconds=get_settings().runtime_poll_seconds)
            if delayed
            else None
        ),
    )


def _dispatch_runtime_wakeup(
    db: Session,
    attempt: NodeAttempt,
    wakeup_no: int,
    *,
    delayed: bool = False,
) -> None:
    task = enqueue(
        db,
        task_type="WAIT_RUNTIME_WAKEUP",
        aggregate_type="ATTEMPT",
        aggregate_id=attempt.id,
        idempotency_key=f"wait-runtime-wakeup:{attempt.id}:v{attempt.state_version}:{wakeup_no}",
        payload={"wakeup_no": wakeup_no},
        available_at=(
            datetime.now(UTC) + timedelta(seconds=get_settings().runtime_poll_seconds)
            if delayed
            else None
        ),
    )
    task.max_attempts = max(task.max_attempts, 100)


def process_runtime_wakeup(
    db: Session,
    attempt_id: str,
    wakeup_no: int,
    lease: Lease,
    *,
    backoff_no: int = 0,
    commit: bool = True,
) -> None:
    attempt = _attempt(db, attempt_id)
    if attempt.state != AttemptState.EXECUTING or attempt.runtime_phase != "RUNNING":
        return
    expected_version = attempt.state_version
    handle = _active_attempt_runtime_handle(db, attempt)
    _release_worker_read_transaction(db, lease)
    try:
        wakeup = get_runtime().wait_for_wakeup(
            handle,
            channel="CONVERSATION",
            timeout_seconds=get_settings().runtime_wakeup_timeout_seconds,
        )
    except DomainError:
        wakeup = None
    _require_current_lease(db, lease)
    current = _attempt(db, attempt_id)
    if (
        current.state != AttemptState.EXECUTING
        or current.runtime_phase != "RUNNING"
        or current.state_version != expected_version
    ):
        return
    if wakeup is not None and wakeup.notified:
        poll_task = enqueue(
            db,
            task_type="POLL_RUNTIME",
            aggregate_type="ATTEMPT",
            aggregate_id=current.id,
            idempotency_key=f"poll-runtime-wakeup:{current.id}:v{current.state_version}:{wakeup_no}",
            payload={"poll_no": wakeup_no},
        )
        poll_task.max_attempts = max(poll_task.max_attempts, 10)
    next_backoff = 0 if wakeup is not None else min(backoff_no + 1, 8)
    task = enqueue(
        db,
        task_type="WAIT_RUNTIME_WAKEUP",
        aggregate_type="ATTEMPT",
        aggregate_id=current.id,
        idempotency_key=(
            f"wait-runtime-wakeup:{current.id}:v{current.state_version}:{wakeup_no + 1}"
        ),
        payload={"wakeup_no": wakeup_no + 1, "backoff_no": next_backoff},
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
    _finish_transaction(db, commit)


def _confirmation_dict(item: RuntimeConfirmationApproval) -> dict[str, Any]:
    return {
        "id": item.id,
        "attempt_id": item.attempt_id,
        "conversation_id": item.flow_run_conversation_binding_id,
        "pending_actions_digest": item.pending_actions_digest,
        "pending_actions": item.pending_actions_json,
        "risk_summary": item.risk_summary_json,
        "action_count": item.action_count,
        "state": item.state,
        "decision_accept": item.decision_accept,
        "decision_reason": item.decision_reason,
        "decided_by": item.decided_by,
        "decided_at": item.decided_at.isoformat() if item.decided_at else None,
        "state_version": item.state_version,
        "created_at": item.created_at.isoformat(),
        "updated_at": item.updated_at.isoformat(),
    }


def _freeze_runtime_confirmation(
    db: Session, attempt: NodeAttempt, pending: RuntimePendingConfirmation
) -> RuntimeConfirmationApproval:
    if not attempt.conversation_id:
        raise DomainError(
            "RUNTIME_PROTOCOL_ERROR",
            "OpenHands confirmation is missing its durable conversation mapping",
            502,
        )
    flow_run_id = _node_run(db, attempt.node_run_id).flow_run_id
    conversation = conversations.conversation_binding(
        db,
        flow_run_id=flow_run_id,
        openhands_conversation_id=attempt.conversation_id,
    )
    active = db.scalar(
        select(RuntimeConfirmationApproval)
        .where(
            RuntimeConfirmationApproval.flow_run_conversation_binding_id
            == conversation.id,
            RuntimeConfirmationApproval.state.in_(["PENDING", "DECIDING"]),
        )
        .with_for_update()
    )
    if active is not None:
        if active.pending_actions_digest == pending.pending_actions_digest:
            return active
        active.state = "EXPIRED"
        active.state_version += 1

    actions = [
        {
            "action_id": action.action_id,
            "tool_call_id": action.tool_call_id,
            "tool_name": action.tool_name,
            "arguments": action.arguments,
            "security_risk": action.security_risk,
            "summary": action.summary,
            "digest": action.digest,
        }
        for action in pending.actions
    ]
    item = RuntimeConfirmationApproval(
        attempt_id=attempt.id,
        flow_run_conversation_binding_id=conversation.id,
        pending_actions_digest=pending.pending_actions_digest,
        pending_actions_json=actions,
        risk_summary_json=[
            {
                "action_id": action.action_id,
                "security_risk": action.security_risk,
                "summary": action.summary,
            }
            for action in pending.actions
        ],
        action_count=len(actions),
        state="PENDING",
    )
    db.add(item)
    db.flush()
    return item


def _require_current_lease(db: Session, lease: Lease | None) -> None:
    if lease is not None and not lease_is_current(db, lease):
        raise RuntimeError("task lease was lost during external runtime operation")


def _evaluate_readiness(db: Session, attempt: NodeAttempt) -> None:
    node_run = _node_run(db, attempt.node_run_id)
    snapshot = _snapshot(db, attempt.snapshot_id)
    node = _node(snapshot, node_run.flow_node_snapshot_key)
    rows = _bindings(db, attempt.id)
    ids = [x.artifact_version_id for x in rows]
    artifacts = {
        x.id: Artifact(x.id, x.artifact_type)
        for x in db.scalars(select(ArtifactVersion).where(ArtifactVersion.id.in_(ids)))
    }
    result = evaluate_readiness(
        _input_fields(node),
        tuple(Binding(x.input_field_key, x.artifact_version_id) for x in rows),
        artifacts,
    )
    if result.ready:
        attempt.state = AttemptState.START_GATES
        attempt.state_version += 1
        _dispatch_gates(db, attempt, "START")
    else:
        attempt.state = AttemptState.WAITING_INPUT
        attempt.error_code = "INPUTS_MISSING" if result.missing else "INPUTS_INCOMPATIBLE"
        attempt.error_detail = json.dumps(
            {"missing": result.missing, "incompatible": result.incompatible}
        )
        _event(
            db,
            node_run.flow_run_id,
            "ATTEMPT_WAITING_INPUT",
            {"missing": result.missing, "incompatible": result.incompatible},
            node_run.id,
            attempt.id,
        )


def _gate_artifact(item: ArtifactVersion) -> dict[str, Any]:
    return {
        "id": item.id,
        "field_key": item.field_key,
        "version_no": item.version_no,
        "artifact_type": item.artifact_type,
        "inline_content": item.inline_content,
        "uri": item.uri,
        "content_hash": item.content_hash,
        "byte_size": item.byte_size,
        "mime_type": item.mime_type,
        "source": item.source,
        "metadata": item.metadata_json,
    }


def _gate_context(
    db: Session, attempt: NodeAttempt, node_run: NodeRun, node: dict[str, Any], stage: str
) -> dict[str, Any]:
    binding_rows = _bindings(db, attempt.id)
    input_artifacts = {
        item.id: item
        for item in db.scalars(
            select(ArtifactVersion).where(
                ArtifactVersion.id.in_([row.artifact_version_id for row in binding_rows])
            )
        )
    }
    outputs = list(
        db.scalars(
            select(ArtifactVersion)
            .where(ArtifactVersion.producer_attempt_id == attempt.id)
            .order_by(ArtifactVersion.field_key, ArtifactVersion.version_no)
        )
    )
    bindings = [
        {
            "input_field_key": row.input_field_key,
            "binding_source": row.binding_source,
            "artifact": _gate_artifact(input_artifacts[row.artifact_version_id]),
        }
        for row in binding_rows
        if row.artifact_version_id in input_artifacts
    ]
    return {
        "schema_version": 1,
        "stage": stage,
        "attempt": {"id": attempt.id, "attempt_no": attempt.attempt_no},
        "node": {
            "instance_key": node_run.flow_node_snapshot_key,
            "alias": node.get("alias"),
            "asset_name": node.get("asset", {}).get("name"),
            "inputs": node.get("asset", {}).get("inputs", []),
            "outputs": node.get("asset", {}).get("outputs", []),
        },
        "input_bindings": bindings,
        "outputs": [_gate_artifact(item) for item in outputs],
        "artifacts": [item["artifact"] for item in bindings]
        + [_gate_artifact(item) for item in outputs],
    }


@dataclass(frozen=True, slots=True)
class _PreparedGate:
    policy: dict[str, Any]
    execution_no: int
    plan: GateExecutionPlan


def _next_gate_state(stage: str, blocked: bool) -> str:
    if stage == "START":
        return AttemptState.START_BLOCKED if blocked else AttemptState.WAITING_START_CONFIRMATION
    return AttemptState.END_BLOCKED if blocked else AttemptState.WAITING_ACCEPTANCE


def _record_gate_results(
    db: Session,
    attempt: NodeAttempt,
    stage: str,
    context: dict[str, Any],
    evaluations: list[tuple[_PreparedGate, GateResult]],
    next_state: str,
) -> None:
    node_run = _node_run(db, attempt.node_run_id)
    for prepared, result in evaluations:
        policy = prepared.policy
        db.add(
            GateEvaluation(
                attempt_id=attempt.id,
                policy_snapshot_key=str(policy["id"]),
                stage=stage,
                policy_position=int(policy["position"]),
                evaluation_attempt=prepared.execution_no,
                state="FINISHED",
                decision=result.decision,
                input_hash=_hash(
                    {
                        "context": context,
                        "policy": policy,
                        "evaluation_attempt": prepared.execution_no,
                    }
                ),
                result_json=result.as_dict(),
                log_excerpt=result.log_excerpt,
                error_code=result.error_code,
            )
        )
    attempt.state = next_state
    _event(
        db,
        node_run.flow_run_id,
        "GATE_STAGE_FINISHED",
        {"stage": stage, "state": next_state},
        node_run.id,
        attempt.id,
    )
    if next_state in {
        AttemptState.WAITING_START_CONFIRMATION,
        AttemptState.WAITING_ACCEPTANCE,
    }:
        run = _run(db, node_run.flow_run_id)
        run.state = FlowRunState.WAITING_HUMAN
        _event(
            db,
            run.id,
            "HUMAN_CONFIRM_REQUIRED",
            {"stage": stage},
            node_run.id,
            attempt.id,
        )


def _prepare_gate_stage(
    db: Session, attempt: NodeAttempt, stage: str
) -> tuple[dict[str, Any], list[_PreparedGate]]:
    node_run = _node_run(db, attempt.node_run_id)
    node = _node(_snapshot(db, attempt.snapshot_id), node_run.flow_node_snapshot_key)
    context = _gate_context(db, attempt, node_run, node, stage)
    policies = sorted(
        [x for x in node.get("gates", []) if x["stage"] == stage and x.get("enabled", True)],
        key=lambda x: x["position"],
    )
    prepared: list[_PreparedGate] = []
    for policy in policies:
        execution_no = (
            db.scalar(
                select(func.max(GateEvaluation.evaluation_attempt)).where(
                    GateEvaluation.attempt_id == attempt.id,
                    GateEvaluation.policy_snapshot_key == policy["id"],
                    GateEvaluation.stage == stage,
                )
            )
            or 0
        ) + 1
        prepared.append(
            _PreparedGate(
                policy=dict(policy),
                execution_no=execution_no,
                plan=prepare_gate(
                    db,
                    str(policy["gate_type"]),
                    dict(policy.get("config") or {}),
                    int(policy.get("timeout_seconds", 30)),
                ),
            )
        )
    return context, prepared


def _execute_gate_stage(
    context: dict[str, Any], prepared: list[_PreparedGate]
) -> tuple[list[tuple[_PreparedGate, GateResult]], bool]:
    evaluations: list[tuple[_PreparedGate, GateResult]] = []
    blocked = False
    for item in prepared:
        result = execute_gate_plan(item.plan, context)
        evaluations.append((item, result))
        if result.decision != "PASS":
            blocked = True
            break
    return evaluations, blocked


def _run_gates_inline(db: Session, attempt: NodeAttempt, stage: str) -> None:
    context, prepared = _prepare_gate_stage(db, attempt, stage)
    evaluations, blocked = _execute_gate_stage(context, prepared)
    attempt.state_version += 1
    _record_gate_results(
        db,
        attempt,
        stage,
        context,
        evaluations,
        _next_gate_state(stage, blocked),
    )


def _run_gates_worker(db: Session, attempt: NodeAttempt, stage: str, lease: Lease) -> None:
    expected_state = AttemptState.START_GATES if stage == "START" else AttemptState.END_GATES
    expected_version = attempt.state_version
    attempt_id = attempt.id
    context, prepared = _prepare_gate_stage(db, attempt, stage)

    # No database transaction is held while Sandbox or model-provider I/O runs.
    _release_worker_read_transaction(db, lease)
    evaluations, blocked = _execute_gate_stage(context, prepared)
    _require_current_lease(db, lease)
    next_state = _next_gate_state(stage, blocked)
    claimed_id = db.scalar(
        update(NodeAttempt)
        .where(
            NodeAttempt.id == attempt_id,
            NodeAttempt.state == expected_state,
            NodeAttempt.state_version == expected_version,
        )
        .values(state=next_state, state_version=NodeAttempt.state_version + 1)
        .returning(NodeAttempt.id)
        .execution_options(synchronize_session=False)
    )
    if claimed_id is None:
        db.expire_all()
        return
    db.expire_all()
    claimed = _attempt(db, attempt_id)
    _record_gate_results(db, claimed, stage, context, evaluations, next_state)


def _finish_transaction(db: Session, commit: bool) -> None:
    if commit:
        finish(db)
    else:
        db.flush()


def process_readiness(db: Session, attempt_id: str, *, commit: bool = True) -> None:
    attempt = _attempt(db, attempt_id)
    if attempt.state not in {AttemptState.WAITING_INPUT, AttemptState.START_BLOCKED}:
        return
    _evaluate_readiness(db, attempt)
    _finish_transaction(db, commit)


def process_gate_stage(
    db: Session,
    attempt_id: str,
    stage: str,
    lease: Lease | None = None,
    *,
    commit: bool = True,
) -> None:
    attempt = _attempt(db, attempt_id)
    expected = AttemptState.START_GATES if stage == "START" else AttemptState.END_GATES
    if attempt.state != expected:
        return
    if lease is None:
        _run_gates_inline(db, attempt, stage)
    else:
        _run_gates_worker(db, attempt, stage, lease)
    _finish_transaction(db, commit)


def _create_node_run(
    db: Session,
    run: FlowRun,
    instance_key: str,
    artifact_ids: dict[str, str],
    created_from: str,
) -> tuple[NodeRun, NodeAttempt]:
    snapshot = _active_snapshot(db, run)
    node = _node(snapshot, instance_key)
    asset = cast(dict[str, Any], node.get("asset") or {})
    asset_id = str(asset.get("id") or "")
    if not asset_id:
        raise DomainError("SNAPSHOT_INVALID", "node asset id is missing", 409)
    confirmation_policy = _snapshot_confirmation_policy(node)
    condenser_config = _snapshot_condenser_config(node)
    _validate_input_bindings(db, run, node, artifact_ids)
    # Automatic port propagation targets the latest waiting work item so the
    # same mapping stays idempotent. A human start is intentionally different:
    # every click represents an independent execution record, even when another
    # execution of the same snapshot node is still active.
    existing = (
        db.scalar(
            select(NodeRun)
            .where(
                NodeRun.flow_run_id == run.id,
                NodeRun.flow_node_snapshot_key == instance_key,
            )
            .order_by(NodeRun.sequence_no.desc())
        )
        if created_from == "PORT_MAPPING"
        else None
    )
    if existing:
        latest = db.scalar(
            select(NodeAttempt)
            .where(NodeAttempt.node_run_id == existing.id)
            .order_by(NodeAttempt.attempt_no.desc())
        )
        if latest is None:
            raise DomainError("RUN_STATE_INVALID", "node work item has no attempt", 409)
        if created_from == "PORT_MAPPING" and latest.state != AttemptState.WAITING_INPUT:
            return existing, latest
        if latest.state == AttemptState.WAITING_INPUT:
            current_bindings = {row.input_field_key: row for row in _bindings(db, latest.id)}
            for field_key, artifact_id in artifact_ids.items():
                binding = current_bindings.get(field_key)
                if binding:
                    binding.artifact_version_id = artifact_id
                    binding.binding_source = created_from
                else:
                    db.add(
                        AttemptInputBinding(
                            attempt_id=latest.id,
                            input_field_key=field_key,
                            artifact_version_id=artifact_id,
                            binding_source=created_from,
                        )
                    )
            latest.state_version += 1
            latest.error_code = None
            latest.error_detail = None
            db.flush()
            _dispatch_readiness(db, latest)
            _event(
                db,
                run.id,
                "INPUT_BINDING_CHANGED",
                {"fields": list(artifact_ids), "source": created_from},
                existing.id,
                latest.id,
            )
            return existing, latest
        if existing.state == NodeRunState.ACTIVE:
            raise illegal("node work item already has an active attempt", state=latest.state)
        next_attempt_no = latest.attempt_no + 1
        existing.state = NodeRunState.ACTIVE
        existing.accepted_attempt_id = None
        attempt = NodeAttempt(
            node_run_id=existing.id,
            attempt_no=next_attempt_no,
            snapshot_id=snapshot.id,
            confirmation_policy=confirmation_policy,
            condenser_config_json=copy.deepcopy(condenser_config),
            workspace_ref=str(
                attempt_workspace_path(
                    asset_id=asset_id,
                    run_id=run.id,
                    node_run_id=existing.id,
                    attempt_no=next_attempt_no,
                )
            ),
        )
        db.add(attempt)
        db.flush()
        for field_key, artifact_id in artifact_ids.items():
            db.add(
                AttemptInputBinding(
                    attempt_id=attempt.id,
                    input_field_key=field_key,
                    artifact_version_id=artifact_id,
                    binding_source=created_from,
                )
            )
        db.flush()
        _event(
            db,
            run.id,
            "ATTEMPT_CREATED",
            {"attempt_no": next_attempt_no, "flow_node_key": instance_key},
            existing.id,
            attempt.id,
        )
        _dispatch_readiness(db, attempt)
        return existing, attempt
    sequence = (
        db.scalar(select(func.max(NodeRun.sequence_no)).where(NodeRun.flow_run_id == run.id)) or 0
    ) + 1
    node_run = NodeRun(
        flow_run_id=run.id,
        flow_node_snapshot_key=instance_key,
        sequence_no=sequence,
        created_from=created_from,
    )
    db.add(node_run)
    db.flush()
    attempt = NodeAttempt(
        node_run_id=node_run.id,
        attempt_no=1,
        snapshot_id=snapshot.id,
        confirmation_policy=confirmation_policy,
        condenser_config_json=copy.deepcopy(condenser_config),
        workspace_ref=str(
            attempt_workspace_path(
                asset_id=asset_id,
                run_id=run.id,
                node_run_id=node_run.id,
                attempt_no=1,
            )
        ),
    )
    db.add(attempt)
    db.flush()
    for field_key, artifact_id in artifact_ids.items():
        db.add(
            AttemptInputBinding(
                attempt_id=attempt.id,
                input_field_key=field_key,
                artifact_version_id=artifact_id,
                binding_source=created_from,
            )
        )
    # AsyncSession uses autoflush=False; readiness queries must see new bindings.
    db.flush()
    _event(
        db,
        run.id,
        "ATTEMPT_CREATED",
        {"attempt_no": 1, "flow_node_key": instance_key},
        node_run.id,
        attempt.id,
    )
    _dispatch_readiness(db, attempt)
    return node_run, attempt


def start_flow(
    db: Session,
    flow_id: str,
    payload: RunStart,
) -> dict[str, Any]:
    flow = load_flow(db, flow_id)
    if not flow.environment_version_id:
        raise DomainError(
            "FLOW_ENVIRONMENT_REQUIRED",
            "This historical Flow has no Environment Version; bind a READY version before running",
            409,
            {"flow_id": flow.id},
        )
    environment = lock_referenceable_version(db, flow.environment_version_id)
    if environment is None:
        raise DomainError(
            "FLOW_ENVIRONMENT_VERSION_INVALID",
            "The Flow Environment Version is not READY",
            409,
            {"environment_version_id": flow.environment_version_id},
        )
    validate_runtime_manifest(environment.manifest_json, environment_version_id=environment.id)
    definition = _snapshot_definition(
        db, flow_id, environment_version_id=environment.id
    )
    run_no = (
        db.scalar(select(func.max(FlowRun.run_no)).where(FlowRun.flow_definition_id == flow_id))
        or 0
    ) + 1
    run_name = payload.name or f"{flow.name} · Run #{run_no}"
    run = FlowRun(
        flow_definition_id=flow_id,
        run_no=run_no,
        name=run_name,
        environment_version_id=environment.id,
        lark_folder_token=None,
        lark_folder_url=None,
    )
    db.add(run)
    db.flush()
    runtime_manifest = _compile_runtime_manifest(definition)
    snapshot = RunSnapshot(
        flow_run_id=run.id,
        version=1,
        schema_version=2,
        definition_json=definition,
        definition_hash=_hash(definition),
        runtime_manifest_json=runtime_manifest,
        runtime_manifest_hash=_runtime_manifest_hash(runtime_manifest),
        environment_version_id=environment.id,
    )
    db.add(snapshot)
    db.flush()
    sandboxes.allocate_flow_run_runtime(db, run.id)
    runtime_allocation = sandboxes.runtime_allocation_for_flow_run(
        db, run.id, manifest_digest=snapshot.runtime_manifest_hash
    )
    sandboxes.ensure_flow_run_runtime_session(
        db,
        flow_run_id=run.id,
        environment_version_id=environment.id,
        runtime_image_digest=environment.image_digest,
        workspace_allocation=runtime_allocation,
    )
    hold_snapshot_memory_references(
        db,
        snapshot_id=snapshot.id,
        runtime_manifest=runtime_manifest,
    )
    run.active_snapshot_id = snapshot.id
    _event(
        db,
        run.id,
        "FLOW_RUN_CREATED",
        {
            "snapshot_version": 1,
            "environment_version_id": environment.id,
        },
    )
    if payload.flow_node_key:
        artifact_ids = dict(payload.input_bindings)
        for artifact_payload in payload.artifacts:
            prepared = prepare_artifact(artifact_payload)
            artifact = _register_artifact(db, run.id, prepared, source="HUMAN_INPUT")
            artifact_ids[artifact.field_key] = artifact.id
        _create_node_run(
            db,
            run,
            payload.flow_node_key,
            artifact_ids,
            "RUN_START",
        )
    finish(db)
    return run_detail(db, run.id)


def start_node_run(
    db: Session, run_id: str, instance_key: str, payload: NodeRunStart
) -> dict[str, Any]:
    run = _run(db, run_id)
    if run.state in {FlowRunState.COMPLETED, FlowRunState.CANCELLED}:
        raise illegal("terminal run cannot activate node", state=run.state)
    artifact_ids = dict(payload.artifact_ids)
    if payload.input_urls:
        node = _node(_active_snapshot(db, run), instance_key)
        input_fields = {field.key for field in _input_fields(node)}
        unknown_fields = sorted(set(payload.input_urls) - input_fields)
        if unknown_fields:
            raise DomainError(
                "INPUT_BINDING_INVALID",
                "input URL field is not an input of the target node",
                422,
                {"fields": unknown_fields},
            )
        for field_key, uri in payload.input_urls.items():
            prepared = prepare_artifact(
                ArtifactWrite(
                    field_key=field_key,
                    artifact_type="URL",
                    uri=uri,
                    metadata={"source": "HUMAN_INPUT"},
                )
            )
            artifact = _register_artifact(db, run.id, prepared, source="HUMAN_INPUT")
            artifact_ids[field_key] = artifact.id
    node_run, _ = _create_node_run(db, run, instance_key, artifact_ids, "HUMAN_START")
    finish(db)
    return node_run_detail(db, node_run.id)


def _claim_attempt_version(
    db: Session,
    attempt_id: str,
    expected_version: int,
    allowed_states: set[str],
    *,
    next_state: str | None = None,
    runtime_phase: str | None = None,
) -> NodeAttempt:
    values: dict[str, Any] = {"state_version": NodeAttempt.state_version + 1}
    if next_state is not None:
        values["state"] = next_state
    if runtime_phase is not None:
        values["runtime_phase"] = runtime_phase
    claimed_id = db.scalar(
        update(NodeAttempt)
        .where(
            NodeAttempt.id == attempt_id,
            NodeAttempt.state_version == expected_version,
            NodeAttempt.state.in_(allowed_states),
        )
        .values(**values)
        .returning(NodeAttempt.id)
        .execution_options(synchronize_session=False)
    )
    if claimed_id is None:
        db.rollback()
        current = _attempt(db, attempt_id)
        if current.state_version != expected_version:
            raise conflict(
                "attempt was modified",
                expected=expected_version,
                actual=current.state_version,
            )
        raise illegal("attempt does not allow this command", state=current.state)
    attempt = _attempt(db, attempt_id)
    db.refresh(attempt)
    return attempt


def replace_bindings(db: Session, attempt_id: str, payload: InputBindingsWrite) -> dict[str, Any]:
    current = _attempt(db, attempt_id)
    if current.state not in {AttemptState.WAITING_INPUT, AttemptState.START_BLOCKED}:
        raise illegal("input bindings are frozen", state=current.state)
    node_run = _node_run(db, current.node_run_id)
    run = _run(db, node_run.flow_run_id)
    node = _node(_snapshot(db, current.snapshot_id), node_run.flow_node_snapshot_key)
    _validate_input_bindings(db, run, node, payload.bindings)
    attempt = _claim_attempt_version(
        db,
        attempt_id,
        payload.expected_state_version,
        {AttemptState.WAITING_INPUT, AttemptState.START_BLOCKED},
    )
    for row in _bindings(db, attempt.id):
        db.delete(row)
    db.flush()
    for field_key, artifact_id in payload.bindings.items():
        db.add(
            AttemptInputBinding(
                attempt_id=attempt.id,
                input_field_key=field_key,
                artifact_version_id=artifact_id,
                binding_source="HUMAN",
            )
        )
    db.flush()
    _dispatch_readiness(db, attempt)
    _event(
        db,
        node_run.flow_run_id,
        "INPUT_BINDING_CHANGED",
        {"fields": list(payload.bindings)},
        node_run.id,
        attempt.id,
    )
    finish(db)
    return attempt_detail(db, attempt.id)


def confirm_start(
    db: Session,
    attempt_id: str,
    payload: AttemptStartWrite,
    idempotency_key: str,
) -> dict[str, Any]:
    current = _attempt(db, attempt_id)
    current_node_run = _node_run(db, current.node_run_id)
    node = _node(_snapshot(db, current.snapshot_id), current_node_run.flow_node_snapshot_key)
    asset = cast(dict[str, Any], node.get("asset") or {})
    capabilities = cast(list[dict[str, Any]], asset.get("capabilities") or [])
    if payload.startup_mode == "SKILL" and not any(
        item.get("capability_type") == "SKILL"
        and item.get("capability_key") == payload.capability_key
        for item in capabilities
    ):
        raise DomainError(
            "STARTUP_CAPABILITY_INVALID",
            "Selected Skill is not available on this node",
            422,
            {"capability_key": payload.capability_key},
        )
    selected_model, selected_effort = resolve_runtime_selection(
        db, node, payload.model_name, payload.reasoning_effort
    )
    _confirmation_policy(current.confirmation_policy, source="node_attempt")
    attempt = _claim_attempt_version(
        db,
        attempt_id,
        payload.expected_state_version,
        {AttemptState.WAITING_START_CONFIRMATION},
        next_state=AttemptState.EXECUTING,
        runtime_phase="STARTING",
    )
    attempt.startup_mode = payload.startup_mode
    attempt.startup_capability_key = payload.capability_key
    attempt.startup_prompt = payload.prompt
    attempt.model_name = selected_model
    attempt.reasoning_effort = selected_effort
    node_run = _node_run(db, attempt.node_run_id)
    run = _run(db, node_run.flow_run_id)
    # Starting an attempt must not call a provider or acquire capability credentials.
    # Skills/MCPs request their own dependencies only when they are actually invoked.
    attempt.output_targets_json = _create_output_targets(db, run, attempt, node)
    _action(
        db,
        run.id,
        "CONFIRM_START",
        idempotency_key,
        node_run_id=node_run.id,
        attempt_id=attempt.id,
    )
    run.state = FlowRunState.ACTIVE
    _event(db, run.id, "ATTEMPT_EXECUTING", {}, node_run.id, attempt.id)
    if not _inline_execution():
        enqueue(
            db,
            task_type="START_RUNTIME",
            aggregate_type="ATTEMPT",
            aggregate_id=attempt.id,
            idempotency_key=f"start-runtime:{attempt.id}",
        )
    finish(db)
    if _inline_execution():
        process_start_runtime(db, attempt.id)
    return attempt_detail(db, attempt.id)


def _create_output_targets(
    db: Session, run: FlowRun, attempt: NodeAttempt, node: dict[str, Any]
) -> dict[str, dict[str, str]]:
    """Describe outputs for the environment-local Lark CLI.

    FlowWeave intentionally does not acquire an account token or create remote
    resources here. The Agent creates each document with the terminal
    environment's isolated Lark CLI login and returns the resulting URL.
    """

    asset = cast(dict[str, Any], node.get("asset") or {})
    raw_outputs = cast(list[object], asset.get("outputs") or [])
    if not raw_outputs:
        return {}
    snapshot = _snapshot(db, attempt.snapshot_id)
    root_url = str(snapshot.definition_json.get("lark_root_folder_url") or "")
    if not root_url:
        raise DomainError(
            "LARK_ROOT_REQUIRED",
            "The flow must configure a Lark root before creating outputs",
            422,
        )
    targets: dict[str, dict[str, str]] = {}
    node_name = str(node.get("alias") or asset.get("name") or "Node")
    for raw in raw_outputs:
        if not isinstance(raw, dict):
            continue
        output = cast(dict[str, Any], raw)
        field_key = str(output.get("field_key") or "")
        if not field_key:
            continue
        display_name = str(output.get("display_name") or field_key)
        title = f"{node_name} · {display_name}"
        template_url = str(output.get("template_url") or "")
        targets[field_key] = {
            "root_url": root_url,
            "run_name": run.name,
            "title": title,
            "display_name": display_name,
            "description": str(output.get("description") or ""),
            "template_url": template_url,
        }
    return targets


def recover_runtime_tasks(db: Session) -> int:
    """Restore missing runtime work for attempts left in an executable phase.

    Attempt rows are the source of truth. Task rows only provide delivery, so a
    deleted or terminal delivery record must not strand a persisted runtime.
    Rows are locked with SKIP LOCKED so multiple workers may recover concurrently.
    """

    attempts = list(
        db.scalars(
            select(NodeAttempt)
            .where(
                (
                    (NodeAttempt.state == AttemptState.EXECUTING)
                    & NodeAttempt.runtime_phase.in_(["STARTING", "RUNNING", "RESUMING"])
                )
                | (
                    (NodeAttempt.state == AttemptState.CANCELLED)
                    & (NodeAttempt.runtime_phase == "CANCELLING")
                )
                | (
                    (NodeAttempt.state == AttemptState.WAITING_CONFIRMATION)
                    & (NodeAttempt.runtime_phase == "CONFIRMING")
                )
            )
            .order_by(NodeAttempt.updated_at, NodeAttempt.id)
            .with_for_update(skip_locked=True)
        )
    )
    recovered = 0
    now_utc = datetime.now(UTC)
    active_states = [TaskState.PENDING, TaskState.RETRY, TaskState.RUNNING]
    for attempt in attempts:
        task_type: str
        payload: dict[str, Any] = {}
        if attempt.runtime_phase == "STARTING":
            task_type = "START_RUNTIME"
        elif attempt.runtime_phase == "RUNNING":
            task_type = "POLL_RUNTIME"
            payload = {"poll_no": 1}
        elif attempt.runtime_phase == "CANCELLING":
            task_type = "CANCEL_RUNTIME"
            latest_cancel_task = db.scalar(
                select(BackgroundTask)
                .where(
                    BackgroundTask.aggregate_id == attempt.id,
                    BackgroundTask.task_type == "CANCEL_RUNTIME",
                )
                .order_by(BackgroundTask.created_at.desc(), BackgroundTask.id.desc())
                .limit(1)
            )
            previous_payload = (
                latest_cancel_task.payload_json
                if latest_cancel_task is not None
                and isinstance(latest_cancel_task.payload_json, dict)
                else {}
            )
            frozen_sandbox_ids = previous_payload.get("sandbox_ids")
            sandbox_ids = (
                {str(item) for item in frozen_sandbox_ids if isinstance(item, str) and item}
                if isinstance(frozen_sandbox_ids, list)
                else set()
            )
            sandbox_ids.update(_managed_runtime_sandbox_ids(db, attempt))
            payload = {"sandbox_ids": sorted(sandbox_ids)}
            recovery_action = db.scalar(
                select(HumanAction)
                .where(
                    HumanAction.attempt_id == attempt.id,
                    HumanAction.action_type == "RETRY_RUNTIME_CANCEL",
                )
                .order_by(HumanAction.created_at.desc(), HumanAction.id.desc())
                .limit(1)
            )
            recovery_mode = (
                str((recovery_action.payload_json or {}).get("mode") or "")
                if recovery_action is not None
                else ""
            )
            if recovery_mode in {"RECONCILE_PARENT", "DELETE_MANAGED_RUNTIME"}:
                payload["recovery_mode"] = recovery_mode
        elif attempt.runtime_phase == "CONFIRMING":
            confirmation = db.scalar(
                select(RuntimeConfirmationApproval)
                .where(
                    RuntimeConfirmationApproval.attempt_id == attempt.id,
                    RuntimeConfirmationApproval.state == "DECIDING",
                )
                .order_by(RuntimeConfirmationApproval.created_at.desc())
                .limit(1)
            )
            if confirmation is None:
                continue
            task_type = "RESPOND_RUNTIME_CONFIRMATION"
            payload = {"confirmation_batch_id": confirmation.id}
        else:
            action = db.scalar(
                select(HumanAction)
                .where(
                    HumanAction.attempt_id == attempt.id,
                    HumanAction.action_type == "HUMAN_INPUT",
                )
                .order_by(HumanAction.created_at.desc(), HumanAction.id.desc())
                .limit(1)
            )
            if action is None:
                continue
            task_type = "RESUME_RUNTIME"
            payload = {"action_id": action.id}

        aggregate_id = (
            str(payload["confirmation_batch_id"])
            if task_type == "RESPOND_RUNTIME_CONFIRMATION"
            else attempt.id
        )
        active = db.scalar(
            select(BackgroundTask.id).where(
                BackgroundTask.aggregate_id == aggregate_id,
                BackgroundTask.task_type == task_type,
                BackgroundTask.state.in_(active_states),
            )
        )
        if active is not None:
            continue

        recovery_key = f"recovery:{task_type.lower()}:{attempt.id}:v{attempt.state_version}"
        existing = db.scalar(
            select(BackgroundTask).where(BackgroundTask.idempotency_key == recovery_key)
        )
        if existing is None:
            recovered_task = enqueue(
                db,
                task_type=task_type,
                aggregate_type=(
                    "RUNTIME_CONFIRMATION"
                    if task_type == "RESPOND_RUNTIME_CONFIRMATION"
                    else "ATTEMPT"
                ),
                aggregate_id=aggregate_id,
                idempotency_key=recovery_key,
                payload=payload,
                available_at=now_utc,
            )
            if task_type == "CANCEL_RUNTIME":
                recovered_task.max_attempts = 20
        else:
            existing.state = TaskState.RETRY
            existing.available_at = now_utc
            existing.lease_owner = None
            existing.lease_until = None
            existing.last_error = "STARTUP_RECOVERY"
            existing.payload_json = payload
            if task_type == "CANCEL_RUNTIME":
                existing.max_attempts = 20
        recovered += 1
        if attempt.runtime_phase == "RUNNING":
            wakeup_active = db.scalar(
                select(BackgroundTask.id).where(
                    BackgroundTask.aggregate_id == attempt.id,
                    BackgroundTask.task_type == "WAIT_RUNTIME_WAKEUP",
                    BackgroundTask.state.in_(active_states),
                )
            )
            if wakeup_active is None:
                _dispatch_runtime_wakeup(db, attempt, 1)
                recovered += 1
    finish(db)
    return recovered


def _runtime_request(db: Session, attempt: NodeAttempt) -> StartAttemptRequest:
    node_run = _node_run(db, attempt.node_run_id)
    run = _run(db, node_run.flow_run_id)
    snapshot = _snapshot(db, attempt.snapshot_id)
    if (
        not run.environment_version_id
        or not snapshot.environment_version_id
        or snapshot.environment_version_id != run.environment_version_id
    ):
        raise DomainError(
            "RUN_ENVIRONMENT_REQUIRED",
            "The FlowRun and Snapshot must share one frozen Environment Version",
            409,
            {"flow_run_id": run.id, "snapshot_id": snapshot.id},
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
    node = _runtime_node(snapshot, node_run.flow_node_snapshot_key)
    memory_enabled, source_refs = frozen_memory_policy(node, runtime_scope="ATTEMPT")
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
    for binding in _bindings(db, attempt.id):
        artifact = db.get(ArtifactVersion, binding.artifact_version_id)
        if artifact is None:
            raise DomainError(
                "ARTIFACT_UNAVAILABLE",
                "A bound artifact is no longer available",
                409,
                {"artifact_version_id": binding.artifact_version_id},
            )
        contract = input_contracts.get(binding.input_field_key, {})
        bindings.append(
            {
                "field_key": binding.input_field_key,
                "display_name": contract.get("display_name"),
                "description": contract.get("description"),
                "template_url": contract.get("template_url"),
                "artifact": _artifact_dict(artifact),
            }
        )
    request = build_runtime_request(
        db,
        flow_run_id=run.id,
        runtime_manifest_hash=snapshot.runtime_manifest_hash,
        attempt_id=attempt.id,
        execution_key=f"attempt:{attempt.id}:start",
        node=node,
        bindings=bindings,
        workspace_ref=attempt.workspace_ref or "",
        startup_prompt=attempt.startup_prompt,
        startup_capability_key=attempt.startup_capability_key,
        model_name=attempt.model_name,
        reasoning_effort=attempt.reasoning_effort,
        output_targets=cast(dict[str, dict[str, str]], attempt.output_targets_json or {}),
        environment_image=environment.image_digest,
        environment_id=environment.environment_id,
        environment_version_id=environment.id,
        environment_version_no=environment.version_no,
        memory_materialized=memory_enabled,
    )
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
                workspace_ref=attempt.workspace_ref or "",
                materials=materials,
            )
    return request


def _release_worker_read_transaction(db: Session, lease: Lease | None) -> None:
    """End a worker read transaction before potentially slow external runtime I/O.

    Inline API execution keeps its outer command transaction intact. Worker handlers
    pass a lease and can safely discard their read-only transaction because all
    durable writes happen after lease validation and Attempt CAS.
    """

    if lease is not None and db.in_transaction():
        db.rollback()


def _claim_runtime_phase(
    db: Session,
    attempt_id: str,
    expected_version: int,
    expected_state: str,
    expected_phase: str,
    **values: object,
) -> NodeAttempt | None:
    """Atomically fence a runtime callback against concurrent Attempt changes."""

    claimed_id = db.scalar(
        update(NodeAttempt)
        .where(
            NodeAttempt.id == attempt_id,
            NodeAttempt.state == expected_state,
            NodeAttempt.runtime_phase == expected_phase,
            NodeAttempt.state_version == expected_version,
        )
        .values(state_version=NodeAttempt.state_version + 1, **values)
        .returning(NodeAttempt.id)
        .execution_options(synchronize_session=False)
    )
    if claimed_id is None:
        db.expire_all()
        return None
    db.expire_all()
    return _attempt(db, attempt_id)


def process_start_runtime(
    db: Session,
    attempt_id: str,
    lease: Lease | None = None,
    *,
    commit: bool = True,
) -> None:
    attempt = _attempt(db, attempt_id)
    if attempt.state != AttemptState.EXECUTING or attempt.runtime_phase != "STARTING":
        return
    expected_version = attempt.state_version
    if attempt.conversation_id:
        claimed = _claim_runtime_phase(
            db,
            attempt.id,
            expected_version,
            AttemptState.EXECUTING,
            "STARTING",
            runtime_phase="RUNNING",
        )
        if claimed is None:
            return
        if _inline_execution():
            process_poll_runtime(db, claimed.id, 1, lease, commit=commit)
        else:
            _dispatch_poll(db, claimed, 1, delayed=False)
            _dispatch_runtime_wakeup(db, claimed, 1)
            _finish_transaction(db, commit)
        return

    current_attempt_id = attempt.id
    flow_run_id = _node_run(db, attempt.node_run_id).flow_run_id
    request = _runtime_request(db, attempt)
    allocation = None
    if request.environment_image and get_settings().runtime_adapter != "mock":
        allocation = sandboxes.ensure_flow_run_runtime(
            db,
            flow_run_id=flow_run_id,
            image=request.environment_image,
            environment_id=request.environment_id,
            environment_version_id=request.environment_version_id,
            environment_version_no=request.environment_version_no,
        )
        request = replace(
            request,
            runtime_sandbox_id=allocation.id,
            runtime_resource_name=allocation.resource_name,
            runtime_base_url=allocation.base_url,
        )
    _release_worker_read_transaction(db, lease)
    handle: RuntimeHandle | None = None
    try:
        handle = get_runtime().start(request)
        _require_current_lease(db, lease)
    except BaseException:
        if handle is not None:
            try:
                get_runtime().cancel(handle)
            except Exception:
                pass
        raise
    claimed = _claim_runtime_phase(
        db,
        current_attempt_id,
        expected_version,
        AttemptState.EXECUTING,
        "STARTING",
        conversation_id=handle.conversation_id,
        runtime_phase="RUNNING",
    )
    if claimed is None:
        _release_worker_read_transaction(db, lease)
        get_runtime().cancel(handle)
        _require_current_lease(db, lease)
        return
    conversations.bind_openhands_conversation(
        db,
        flow_run_id=flow_run_id,
        openhands_conversation_id=handle.conversation_id,
        display_label=f"运行 {claimed.id}",
    )
    if _inline_execution():
        process_poll_runtime(db, claimed.id, 1, lease, commit=commit)
    else:
        _dispatch_poll(db, claimed, 1, delayed=False)
        _dispatch_runtime_wakeup(db, claimed, 1)
        _finish_transaction(db, commit)


def _apply_runtime_result(
    db: Session,
    attempt: NodeAttempt,
    result: RuntimeResult,
    *,
    prepared_outputs: list[PreparedArtifact],
    result_key: str,
    pending_confirmation: RuntimePendingConfirmation | None = None,
    failure_code: str = "RUNTIME_FAILED",
    commit: bool = True,
) -> dict[str, Any]:
    node_run = _node_run(db, attempt.node_run_id)
    run = _run(db, node_run.flow_run_id)
    if result.status == "CONFIRMATION_REQUIRED":
        if pending_confirmation is None:
            raise DomainError(
                "RUNTIME_PROTOCOL_ERROR",
                "OpenHands requested confirmation without a pending action batch",
                502,
            )
        confirmation = _freeze_runtime_confirmation(db, attempt, pending_confirmation)
        attempt.state = AttemptState.WAITING_CONFIRMATION
        attempt.runtime_phase = "WAITING_CONFIRMATION"
        run.state = FlowRunState.WAITING_HUMAN
        _event(
            db,
            run.id,
            "RUNTIME_CONFIRMATION_REQUIRED",
            {
                "confirmation_batch_id": confirmation.id,
                "pending_actions_digest": confirmation.pending_actions_digest,
                "action_count": confirmation.action_count,
            },
            node_run.id,
            attempt.id,
        )
    elif result.status == "HUMAN_INPUT_REQUIRED":
        attempt.state = AttemptState.WAITING_HUMAN
        attempt.error_detail = result.human_question
        run.state = FlowRunState.WAITING_HUMAN
        _event(
            db,
            run.id,
            "RUNTIME_HUMAN_INPUT_REQUIRED",
            {"question": result.human_question},
            node_run.id,
            attempt.id,
        )
    elif result.status == "COMPLETED":
        for prepared in prepared_outputs:
            _register_artifact(
                db,
                run.id,
                prepared,
                source="RUNTIME",
                attempt_id=attempt.id,
            )
        attempt.state = AttemptState.END_GATES
        attempt.runtime_phase = "COMPLETED"
        _dispatch_gates(db, attempt, "END")
    elif result.status == "RUNNING":
        attempt.runtime_phase = "RUNNING"
    elif result.status == "FAILED":
        attempt.state = AttemptState.END_BLOCKED
        attempt.runtime_phase = "FAILED"
        attempt.error_code = failure_code
        attempt.error_detail = result.error
        run.state = FlowRunState.WAITING_HUMAN
        _event(db, run.id, "RUNTIME_FAILED", {"error": result.error}, node_run.id, attempt.id)
    _finish_transaction(db, commit)
    return attempt_detail(db, attempt.id)


def _prepare_runtime_outputs(
    result: RuntimeResult, output_targets: dict[str, Any]
) -> list[PreparedArtifact]:
    if result.status != "COMPLETED":
        return []
    missing = sorted(set(output_targets) - set(result.outputs))
    if missing:
        raise DomainError(
            "RUNTIME_OUTPUT_MISSING",
            "The Agent completed without returning every required output URL",
            422,
            {"fields": missing},
        )
    prepared: list[PreparedArtifact] = []
    for field_key in output_targets:
        artifact_type, content = result.outputs[field_key]
        if artifact_type != "URL" or not content.strip():
            raise DomainError(
                "RUNTIME_OUTPUT_INVALID",
                "The Agent returned an invalid output URL",
                422,
                {"field": field_key},
            )
        prepared.append(
            prepare_artifact(
                ArtifactWrite(
                    field_key=field_key,
                    artifact_type="URL",
                    uri=content.strip(),
                    metadata={"runtime_artifact_type": "URL"},
                )
            )
        )
    return prepared


def process_poll_runtime(
    db: Session,
    attempt_id: str,
    poll_no: int,
    lease: Lease | None = None,
    *,
    commit: bool = True,
) -> None:
    attempt = _attempt(db, attempt_id)
    if attempt.state != AttemptState.EXECUTING or attempt.runtime_phase != "RUNNING":
        return
    expected_version = attempt.state_version
    current_attempt_id = attempt.id
    handle = _active_attempt_runtime_handle(db, attempt)
    _release_worker_read_transaction(db, lease)
    runtime = get_runtime()
    batch = runtime.read_events(handle)
    result = batch.result or runtime.inspect(
        RuntimeHandle(handle.job_id, handle.conversation_id, batch.cursor or handle.cursor)
    )
    pending_confirmation = (
        runtime.get_pending_confirmation(
            RuntimeHandle(handle.job_id, handle.conversation_id, batch.cursor or handle.cursor)
        )
        if result.status == "CONFIRMATION_REQUIRED"
        else None
    )
    try:
        _require_current_lease(db, lease)
    except Exception:
        raise
    claimed = _claim_runtime_phase(
        db,
        current_attempt_id,
        expected_version,
        AttemptState.EXECUTING,
        "RUNNING",
    )
    if claimed is None:
        return
    prepared_outputs = _prepare_runtime_outputs(result, claimed.output_targets_json or {})
    failure_code = "RUNTIME_FAILED"
    if result.cursor is None and batch.cursor is not None:
        result = RuntimeResult(
            status=result.status,
            outputs=result.outputs,
            human_question=result.human_question,
            cursor=batch.cursor,
            error=result.error,
        )
    _apply_runtime_result(
        db,
        claimed,
        result,
        prepared_outputs=prepared_outputs,
        result_key=f"poll:{poll_no}:{result.cursor or batch.cursor or '0'}",
        pending_confirmation=pending_confirmation,
        failure_code=failure_code,
        commit=commit,
    )
    if result.status == "RUNNING":
        _dispatch_poll(
            db,
            claimed,
            poll_no + 1,
            delayed=True,
        )
        _finish_transaction(db, commit)


def human_input(
    db: Session, attempt_id: str, payload: HumanInputWrite, idempotency_key: str
) -> dict[str, Any]:
    current = _attempt(db, attempt_id)
    current_node_run = _node_run(db, current.node_run_id)
    node = _node(_snapshot(db, current.snapshot_id), current_node_run.flow_node_snapshot_key)
    selected_model, selected_effort = resolve_runtime_selection(
        db,
        node,
        payload.model_name or current.model_name,
        payload.reasoning_effort
        if "reasoning_effort" in payload.model_fields_set
        else current.reasoning_effort,
    )
    attempt = _claim_attempt_version(
        db,
        attempt_id,
        payload.expected_state_version,
        {AttemptState.WAITING_HUMAN},
        next_state=AttemptState.EXECUTING,
        runtime_phase="RESUMING",
    )
    node_run = _node_run(db, attempt.node_run_id)
    action = _action(
        db,
        node_run.flow_run_id,
        "HUMAN_INPUT",
        idempotency_key,
        {
            "content": payload.content,
            "runtime_selection": {
                "model_name": selected_model,
                "reasoning_effort": selected_effort,
            },
        },
        node_run.id,
        attempt.id,
    )
    if not _inline_execution():
        enqueue(
            db,
            task_type="RESUME_RUNTIME",
            aggregate_type="ATTEMPT",
            aggregate_id=attempt.id,
            idempotency_key=f"resume-runtime:{action.id}",
            payload={"action_id": action.id},
        )
    finish(db)
    if _inline_execution():
        process_resume_runtime(db, attempt.id, action.id)
    return attempt_detail(db, attempt.id)


def decide_runtime_confirmation(
    db: Session,
    batch_id: str,
    payload: RuntimeConfirmationDecisionWrite,
    idempotency_key: str,
) -> dict[str, Any]:
    """Durably freeze one batch-level decision before external Runtime I/O."""

    duplicate = db.scalar(
        select(RuntimeConfirmationApproval).where(
            RuntimeConfirmationApproval.decision_idempotency_key == idempotency_key
        )
    )
    if duplicate is not None:
        if duplicate.id != batch_id:
            raise conflict(
                "confirmation idempotency key is already used",
                confirmation_batch_id=duplicate.id,
            )
        if (
            duplicate.decision_accept != payload.accept
            or duplicate.decision_reason != payload.reason
        ):
            raise conflict(
                "confirmation decision does not match the idempotent request",
                confirmation_batch_id=batch_id,
            )
        return _confirmation_dict(duplicate)

    batch = db.scalar(
        select(RuntimeConfirmationApproval)
        .where(RuntimeConfirmationApproval.id == batch_id)
        .with_for_update()
    )
    if batch is None:
        raise not_found("runtime_confirmation_batch", batch_id)
    if batch.state != "PENDING":
        raise conflict(
            "runtime confirmation batch is no longer pending",
            confirmation_batch_id=batch.id,
            state=batch.state,
        )
    attempt = db.scalar(
        select(NodeAttempt).where(NodeAttempt.id == batch.attempt_id).with_for_update()
    )
    if attempt is None:
        raise not_found("node_attempt", batch.attempt_id)
    if (
        attempt.state != AttemptState.WAITING_CONFIRMATION
        or attempt.runtime_phase != "WAITING_CONFIRMATION"
    ):
        raise conflict(
            "attempt is no longer waiting for this confirmation",
            attempt_id=attempt.id,
            state=attempt.state,
            runtime_phase=attempt.runtime_phase,
        )

    node_run = _node_run(db, attempt.node_run_id)
    run = _run(db, node_run.flow_run_id)
    batch.state = "DECIDING"
    batch.decision_accept = payload.accept
    batch.decision_reason = payload.reason
    batch.decision_idempotency_key = idempotency_key
    batch.decided_by = "API_USER"
    batch.decided_at = now()
    batch.state_version += 1
    attempt.runtime_phase = "CONFIRMING"
    attempt.state_version += 1
    _action(
        db,
        run.id,
        "RUNTIME_CONFIRMATION_DECISION",
        idempotency_key,
        {
            "confirmation_batch_id": batch.id,
            "accept": payload.accept,
            "reason": payload.reason,
            "pending_actions_digest": batch.pending_actions_digest,
        },
        node_run.id,
        attempt.id,
    )
    enqueue(
        db,
        task_type="RESPOND_RUNTIME_CONFIRMATION",
        aggregate_type="RUNTIME_CONFIRMATION",
        aggregate_id=batch.id,
        idempotency_key=f"respond-runtime-confirmation:{batch.id}:v{batch.state_version}",
    )
    _event(
        db,
        run.id,
        "RUNTIME_CONFIRMATION_DECISION_RECORDED",
        {
            "confirmation_batch_id": batch.id,
            "accept": payload.accept,
            "pending_actions_digest": batch.pending_actions_digest,
        },
        node_run.id,
        attempt.id,
    )
    finish(db)
    if _inline_execution():
        process_runtime_confirmation(db, batch.id)
    return _confirmation_dict(db.get(RuntimeConfirmationApproval, batch.id) or batch)


def process_runtime_confirmation(
    db: Session,
    batch_id: str,
    lease: Lease | None = None,
    *,
    commit: bool = True,
) -> None:
    batch = db.get(RuntimeConfirmationApproval, batch_id)
    if batch is None or batch.state != "DECIDING":
        return
    attempt = _attempt(db, batch.attempt_id)
    if attempt.state != AttemptState.WAITING_CONFIRMATION or attempt.runtime_phase != "CONFIRMING":
        return
    if batch.decision_accept is None or batch.decision_reason is None:
        raise DomainError(
            "RUNTIME_CONFIRMATION_INVALID",
            "Runtime confirmation decision is incomplete",
            409,
        )

    batch_version = batch.state_version
    attempt_version = attempt.state_version
    attempt_id = attempt.id
    handle = _active_attempt_runtime_handle(
        db,
        attempt,
        conversation_id=attempt.conversation_id,
    )
    expected_digest = batch.pending_actions_digest
    accept = batch.decision_accept
    reason = batch.decision_reason
    _release_worker_read_transaction(db, lease)
    runtime = get_runtime()
    pending = runtime.get_pending_confirmation(handle)
    response_cursor = pending.cursor if pending is not None else handle.cursor
    if pending is not None and pending.pending_actions_digest != expected_digest:
        _require_current_lease(db, lease)
        current = db.scalar(
            select(RuntimeConfirmationApproval)
            .where(
                RuntimeConfirmationApproval.id == batch_id,
                RuntimeConfirmationApproval.state == "DECIDING",
                RuntimeConfirmationApproval.state_version == batch_version,
            )
            .with_for_update()
        )
        current_attempt = db.scalar(
            select(NodeAttempt).where(NodeAttempt.id == attempt_id).with_for_update()
        )
        if current_attempt is None:
            db.rollback()
            return
        if current is None or current_attempt.state_version != attempt_version:
            db.rollback()
            return
        current.state = "EXPIRED"
        current.state_version += 1
        current_attempt.runtime_phase = "WAITING_CONFIRMATION"
        current_attempt.state_version += 1
        replacement = _freeze_runtime_confirmation(db, current_attempt, pending)
        node_run = _node_run(db, current_attempt.node_run_id)
        _event(
            db,
            node_run.flow_run_id,
            "RUNTIME_CONFIRMATION_DRIFTED",
            {
                "expired_batch_id": current.id,
                "replacement_batch_id": replacement.id,
                "expected_pending_digest": expected_digest,
                "actual_pending_digest": pending.pending_actions_digest,
            },
            node_run.id,
            current_attempt.id,
        )
        _finish_transaction(db, commit)
        return

    runtime_decision_reconciled = pending is None
    if pending is not None:
        result = runtime.respond_to_confirmation(handle, expected_digest, accept, reason)
        response_cursor = result.cursor or response_cursor
    # OpenHands 1.40.0 restarts automatically on accept. Reject only appends
    # UserRejectObservation, so continue through the formal run endpoint.
    if not accept:
        result = runtime.run(RuntimeHandle(handle.job_id, handle.conversation_id, response_cursor))
        response_cursor = result.cursor or response_cursor
    _require_current_lease(db, lease)

    current = db.scalar(
        select(RuntimeConfirmationApproval)
        .where(
            RuntimeConfirmationApproval.id == batch_id,
            RuntimeConfirmationApproval.state == "DECIDING",
            RuntimeConfirmationApproval.state_version == batch_version,
        )
        .with_for_update()
    )
    if current is None:
        db.rollback()
        return
    claimed = _claim_runtime_phase(
        db,
        attempt_id,
        attempt_version,
        AttemptState.WAITING_CONFIRMATION,
        "CONFIRMING",
        state=AttemptState.EXECUTING,
        runtime_phase="RUNNING",
    )
    if claimed is None:
        db.rollback()
        return
    current.state = "APPROVED" if accept else "REJECTED"
    current.state_version += 1
    node_run = _node_run(db, claimed.node_run_id)
    run = _run(db, node_run.flow_run_id)
    run.state = FlowRunState.ACTIVE
    _event(
        db,
        run.id,
        "RUNTIME_CONFIRMATION_RESOLVED",
        {
            "confirmation_batch_id": current.id,
            "accept": accept,
            "pending_actions_digest": expected_digest,
            "runtime_decision_reconciled": runtime_decision_reconciled,
        },
        node_run.id,
        claimed.id,
    )
    _dispatch_poll(db, claimed, 1, delayed=True)
    _finish_transaction(db, commit)


def process_resume_runtime(
    db: Session,
    attempt_id: str,
    action_id: str,
    lease: Lease | None = None,
    *,
    commit: bool = True,
) -> None:
    attempt = _attempt(db, attempt_id)
    if attempt.state != AttemptState.EXECUTING or attempt.runtime_phase != "RESUMING":
        return
    expected_version = attempt.state_version
    action = db.get(HumanAction, action_id)
    if not action or action.attempt_id != attempt.id:
        raise not_found("human_action", action_id)
    current_attempt_id = attempt.id
    content = str(action.payload_json.get("content", ""))
    handle = _active_attempt_runtime_handle(db, attempt)
    selection = cast(dict[str, Any], action.payload_json.get("runtime_selection") or {})
    node_run = _node_run(db, attempt.node_run_id)
    node = _node(_snapshot(db, attempt.snapshot_id), node_run.flow_node_snapshot_key)
    provider = (
        resolve_runtime_provider(
            db,
            node,
            cast(str | None, selection.get("model_name")),
            cast(str | None, selection.get("reasoning_effort")),
        )
        if selection.get("model_name") and get_settings().runtime_adapter != "mock"
        else None
    )
    _release_worker_read_transaction(db, lease)
    runtime = get_runtime()
    if provider is not None:
        runtime.switch_model(handle, provider)
    result = runtime.resume(handle, content)
    try:
        _require_current_lease(db, lease)
    except Exception:
        raise
    claimed = _claim_runtime_phase(
        db,
        current_attempt_id,
        expected_version,
        AttemptState.EXECUTING,
        "RESUMING",
        runtime_phase="RUNNING",
    )
    if claimed is None:
        return
    prepared_outputs = _prepare_runtime_outputs(result, claimed.output_targets_json or {})
    action.payload_json = {
        "content_digest": hashlib.sha256(content.encode()).hexdigest(),
        "content_length": len(content),
        "runtime_selection": selection,
    }
    _apply_runtime_result(
        db,
        claimed,
        result,
        prepared_outputs=prepared_outputs,
        result_key=f"resume:{action.id}",
        commit=commit,
    )
    if result.status == "RUNNING":
        _dispatch_poll(db, claimed, 1, delayed=True)
        _finish_transaction(db, commit)


def _runtime_cancel_targets(
    db: Session, attempt: NodeAttempt
) -> list[tuple[str | None, RuntimeHandle, str | None]]:
    if not attempt.conversation_id:
        return []
    return [(None, _active_attempt_runtime_handle(db, attempt), None)]


def _managed_runtime_sandbox_ids(db: Session, attempt: NodeAttempt) -> set[str]:
    # A FlowRun Runtime can host other Conversations and is never an Attempt
    # cancellation target.
    return set()


def _runtime_cancel_recovery_modes(db: Session, attempt: NodeAttempt) -> list[str]:
    if attempt.state != AttemptState.CANCELLED or attempt.runtime_phase != "CANCEL_FAILED":
        return []
    return ["RECONCILE_PARENT"]


def process_cancel_runtime(
    db: Session,
    attempt_id: str,
    lease: Lease | None = None,
    *,
    recovery_mode: str | None = None,
    sandbox_ids: tuple[str, ...] = (),
    commit: bool = True,
) -> None:
    attempt = _attempt(db, attempt_id)
    if attempt.state != AttemptState.CANCELLED or attempt.runtime_phase != "CANCELLING":
        return
    expected_version = attempt.state_version
    current_attempt_id = attempt.id
    if recovery_mode == "DELETE_MANAGED_RUNTIME" or sandbox_ids:
        raise DomainError(
            "FLOW_RUN_RUNTIME_DELETE_FORBIDDEN",
            "Attempt cancellation cannot delete the shared FlowRun Runtime",
            409,
        )
    targets = _runtime_cancel_targets(db, attempt)
    if targets:
        _release_worker_read_transaction(db, lease)
        for adapter, handle, _ in targets:
            runtime_for(adapter, handle).cancel(handle)
        _require_current_lease(db, lease)
    claimed = _claim_runtime_phase(
        db,
        current_attempt_id,
        expected_version,
        AttemptState.CANCELLED,
        "CANCELLING",
        runtime_phase="CANCELLED",
    )
    if claimed is not None:
        claimed.error_code = None
        claimed.error_detail = None
        _finish_transaction(db, commit)


def record_runtime_task_failure(
    db: Session, attempt_id: str, error: str, *, terminal: bool
) -> None:
    if not terminal:
        return
    attempt = _attempt(db, attempt_id)
    if attempt.state != AttemptState.CANCELLED or attempt.runtime_phase != "CANCELLING":
        return
    attempt.runtime_phase = "CANCEL_FAILED"
    attempt.error_code = "EXECUTOR_CANCEL_FAILED"
    attempt.error_detail = error[:2000]
    attempt.state_version += 1
    node_run = _node_run(db, attempt.node_run_id)
    _event(
        db,
        node_run.flow_run_id,
        "ATTEMPT_CANCEL_FAILED",
        {"error": attempt.error_detail},
        node_run.id,
        attempt.id,
    )
    db.flush()


def retry_runtime_cancel(
    db: Session, attempt_id: str, payload: RuntimeCancelRecoveryWrite, idempotency_key: str
) -> dict[str, Any]:
    existing_action = db.scalar(
        select(HumanAction).where(HumanAction.idempotency_key == idempotency_key)
    )
    if existing_action is not None:
        return attempt_detail(db, attempt_id)
    attempt = _attempt(db, attempt_id)
    node_run = _node_run(db, attempt.node_run_id)
    run = _run(db, node_run.flow_run_id)
    available_modes = _runtime_cancel_recovery_modes(db, attempt)
    if payload.mode not in available_modes:
        raise illegal(
            "attempt runtime cancellation recovery mode is unavailable",
            mode=payload.mode,
            available_modes=available_modes,
        )
    _action(
        db,
        run.id,
        "RETRY_RUNTIME_CANCEL",
        idempotency_key,
        node_run_id=node_run.id,
        payload={"mode": payload.mode},
        attempt_id=attempt.id,
    )
    claimed_id = db.scalar(
        update(NodeAttempt)
        .where(
            NodeAttempt.id == attempt.id,
            NodeAttempt.state == AttemptState.CANCELLED,
            NodeAttempt.runtime_phase == "CANCEL_FAILED",
            NodeAttempt.state_version == payload.expected_state_version,
        )
        .values(
            runtime_phase="CANCELLING",
            error_code=None,
            error_detail=None,
            state_version=NodeAttempt.state_version + 1,
        )
        .returning(NodeAttempt.id)
        .execution_options(synchronize_session=False)
    )
    if claimed_id is None:
        db.rollback()
        current = _attempt(db, attempt_id)
        if current.state_version != payload.expected_state_version:
            raise conflict(
                "attempt was modified",
                expected=payload.expected_state_version,
                actual=current.state_version,
            )
        raise illegal(
            "attempt runtime cancellation is not retryable", runtime_phase=current.runtime_phase
        )
    db.expire_all()
    claimed = _attempt(db, attempt_id)
    managed_sandbox_ids = _managed_runtime_sandbox_ids(db, claimed)
    enqueue(
        db,
        task_type="CANCEL_RUNTIME",
        aggregate_type="ATTEMPT",
        aggregate_id=claimed.id,
        idempotency_key=f"retry-cancel-runtime:{claimed.id}:v{claimed.state_version}",
        payload={
            "recovery_mode": payload.mode,
            "sandbox_ids": sorted(managed_sandbox_ids),
        },
    ).max_attempts = 20
    if payload.mode == "DELETE_MANAGED_RUNTIME":
        for sandbox_id in managed_sandbox_ids:
            sandboxes.request_delete_durable(db, sandbox_id)
    _event(
        db,
        run.id,
        "ATTEMPT_CANCEL_RECOVERY_REQUESTED",
        {"mode": payload.mode},
        node_run.id,
        claimed.id,
    )
    finish(db)
    return attempt_detail(db, claimed.id)


def cancel_attempt(
    db: Session, attempt_id: str, payload: AttemptVersionWrite, idempotency_key: str
) -> dict[str, Any]:
    """Cancel one node Attempt without cancelling the entire flow run."""

    existing = db.scalar(select(HumanAction).where(HumanAction.idempotency_key == idempotency_key))
    if existing is not None:
        return attempt_detail(db, attempt_id)
    attempt = _attempt(db, attempt_id)
    if attempt.state in {
        AttemptState.ACCEPTED,
        AttemptState.REJECTED,
        AttemptState.CANCELLED,
    }:
        return attempt_detail(db, attempt.id)
    if attempt.state_version != payload.expected_state_version:
        raise conflict(
            "attempt was modified",
            expected=payload.expected_state_version,
            actual=attempt.state_version,
        )
    node_run = _node_run(db, attempt.node_run_id)
    run = _run(db, node_run.flow_run_id)
    _action(
        db,
        run.id,
        "CANCEL_NODE_ATTEMPT",
        idempotency_key,
        node_run_id=node_run.id,
        attempt_id=attempt.id,
    )
    targets = _runtime_cancel_targets(db, attempt)
    attempt.state = AttemptState.CANCELLED
    attempt.state_version += 1
    for confirmation in db.scalars(
        select(RuntimeConfirmationApproval).where(
            RuntimeConfirmationApproval.attempt_id == attempt.id,
            RuntimeConfirmationApproval.state.in_(["PENDING", "DECIDING"]),
        )
    ):
        confirmation.state = "CANCELLED"
        confirmation.state_version += 1
    node_run.state = NodeRunState.CANCELLED
    if targets:
        attempt.runtime_phase = "CANCELLING"
        sandbox_ids = _managed_runtime_sandbox_ids(db, attempt)
        enqueue(
            db,
            task_type="CANCEL_RUNTIME",
            aggregate_type="ATTEMPT",
            aggregate_id=attempt.id,
            idempotency_key=f"cancel-runtime:{attempt.id}:v{attempt.state_version}",
            payload={"sandbox_ids": sorted(sandbox_ids)},
        ).max_attempts = 20
    else:
        attempt.runtime_phase = "CANCELLED"
    _event(db, run.id, "ATTEMPT_CANCELLED", {}, node_run.id, attempt.id)
    _recompute_run(db, run)
    finish(db)
    return attempt_detail(db, attempt.id)


def _recompute_run(db: Session, run: FlowRun) -> None:
    node_runs = list(db.scalars(select(NodeRun).where(NodeRun.flow_run_id == run.id)))
    if node_runs and all(
        x.state in {NodeRunState.ACCEPTED, NodeRunState.CANCELLED} for x in node_runs
    ):
        run.state = FlowRunState.COMPLETED
        run.completion_mode = "AUTO"
        run.finished_at = now()
        _event(db, run.id, "FLOW_RUN_COMPLETED", {"mode": "AUTO"})
    elif any(x.state == NodeRunState.ACTIVE for x in node_runs):
        attempts = list(
            db.scalars(
                select(NodeAttempt).where(NodeAttempt.node_run_id.in_([x.id for x in node_runs]))
            )
        )
        if any(
            x.state
            in {
                AttemptState.WAITING_INPUT,
                AttemptState.WAITING_START_CONFIRMATION,
                AttemptState.WAITING_ACCEPTANCE,
                AttemptState.WAITING_HUMAN,
                AttemptState.WAITING_CONFIRMATION,
                AttemptState.START_BLOCKED,
                AttemptState.END_BLOCKED,
            }
            for x in attempts
        ):
            run.state = FlowRunState.WAITING_HUMAN
        else:
            run.state = FlowRunState.ACTIVE


def _activate_mapped_targets(db: Session, run: FlowRun, accepted: NodeRun) -> None:
    snapshot = _active_snapshot(db, run)
    definition = snapshot.definition_json
    outputs = list(
        db.scalars(
            select(ArtifactVersion).where(
                ArtifactVersion.producer_attempt_id == accepted.accepted_attempt_id
            )
        )
    )
    by_field = {x.field_key: x.id for x in outputs}
    for mapping in definition.get("port_mappings", []):
        if mapping["source_instance_key"] != accepted.flow_node_snapshot_key:
            continue
        artifact_id = by_field.get(mapping["source_output_key"])
        if artifact_id:
            _create_node_run(
                db,
                run,
                mapping["target_instance_key"],
                {mapping["target_input_key"]: artifact_id},
                "PORT_MAPPING",
            )


def accept_attempt(
    db: Session,
    attempt_id: str,
    payload: AttemptVersionWrite,
    idempotency_key: str,
) -> dict[str, Any]:
    attempt = _claim_attempt_version(
        db,
        attempt_id,
        payload.expected_state_version,
        {AttemptState.WAITING_ACCEPTANCE},
        next_state=AttemptState.ACCEPTED,
    )
    node_run = _node_run(db, attempt.node_run_id)
    run = _run(db, node_run.flow_run_id)
    _action(
        db,
        run.id,
        "ACCEPT_ATTEMPT",
        idempotency_key,
        node_run_id=node_run.id,
        attempt_id=attempt.id,
    )
    node_run.state = NodeRunState.ACCEPTED
    node_run.accepted_attempt_id = attempt.id
    _event(db, run.id, "NODE_RUN_ACCEPTED", {}, node_run.id, attempt.id)
    _activate_mapped_targets(db, run, node_run)
    _recompute_run(db, run)
    finish(db)
    return run_detail(db, run.id)


def reject_attempt(
    db: Session, attempt_id: str, payload: RejectWrite, idempotency_key: str
) -> dict[str, Any]:
    attempt = _claim_attempt_version(
        db,
        attempt_id,
        payload.expected_state_version,
        {AttemptState.WAITING_ACCEPTANCE, AttemptState.END_BLOCKED},
        next_state=AttemptState.REJECTED,
    )
    node_run = _node_run(db, attempt.node_run_id)
    run = _run(db, node_run.flow_run_id)
    _action(
        db,
        run.id,
        "REJECT_ATTEMPT",
        idempotency_key,
        {"reason": payload.reason},
        node_run.id,
        attempt.id,
    )
    next_no = (
        db.scalar(
            select(func.max(NodeAttempt.attempt_no)).where(NodeAttempt.node_run_id == node_run.id)
        )
        or 0
    ) + 1
    if not run.active_snapshot_id:
        raise DomainError("SNAPSHOT_INVALID", "active snapshot is missing", 409)
    next_node = _node(_snapshot(db, run.active_snapshot_id), node_run.flow_node_snapshot_key)
    next_asset = cast(dict[str, Any], next_node.get("asset") or {})
    next_asset_id = str(next_asset.get("id") or "")
    if not next_asset_id:
        raise DomainError("SNAPSHOT_INVALID", "node asset id is missing", 409)
    confirmation_policy = _snapshot_confirmation_policy(next_node)
    condenser_config = _snapshot_condenser_config(next_node)
    next_attempt = NodeAttempt(
        node_run_id=node_run.id,
        attempt_no=next_no,
        snapshot_id=run.active_snapshot_id,
        confirmation_policy=confirmation_policy,
        condenser_config_json=copy.deepcopy(condenser_config),
        workspace_ref=str(
            attempt_workspace_path(
                asset_id=next_asset_id,
                run_id=run.id,
                node_run_id=node_run.id,
                attempt_no=next_no,
            )
        ),
    )
    db.add(next_attempt)
    db.flush()
    if payload.copy_input_bindings:
        for binding in _bindings(db, attempt.id):
            db.add(
                AttemptInputBinding(
                    attempt_id=next_attempt.id,
                    input_field_key=binding.input_field_key,
                    artifact_version_id=binding.artifact_version_id,
                    binding_source="COPIED_FROM_REJECTED",
                )
            )
    db.flush()
    _event(
        db,
        run.id,
        "ATTEMPT_CREATED",
        {"attempt_no": next_no, "reason": payload.reason},
        node_run.id,
        next_attempt.id,
    )
    _dispatch_readiness(db, next_attempt)
    finish(db)
    return attempt_detail(db, next_attempt.id)


def retry_gates(db: Session, attempt_id: str, payload: AttemptVersionWrite) -> dict[str, Any]:
    current = _attempt(db, attempt_id)
    if current.state == AttemptState.START_BLOCKED:
        next_state, stage = AttemptState.START_GATES, "START"
    elif current.state == AttemptState.END_BLOCKED:
        next_state, stage = AttemptState.END_GATES, "END"
    else:
        raise illegal("attempt has no blocked gate stage", state=current.state)
    attempt = _claim_attempt_version(
        db,
        attempt_id,
        payload.expected_state_version,
        {current.state},
        next_state=next_state,
    )
    _dispatch_gates(db, attempt, stage)
    finish(db)
    return attempt_detail(db, attempt.id)


def sync_snapshot(
    db: Session, run_id: str, payload: SyncSnapshotWrite, idempotency_key: str
) -> dict[str, Any]:
    run = _run(db, run_id)
    current = _active_snapshot(db, run)
    if (
        payload.expected_active_version is not None
        and current.version != payload.expected_active_version
    ):
        raise conflict(
            "active snapshot changed",
            expected=payload.expected_active_version,
            actual=current.version,
        )
    if not run.environment_version_id:
        raise DomainError(
            "RUN_ENVIRONMENT_REQUIRED",
            "This historical FlowRun has no Environment Version and cannot create snapshots",
            409,
            {"flow_run_id": run.id},
        )
    environment = lock_referenceable_version(db, run.environment_version_id)
    if environment is None:
        raise DomainError(
            "RUN_ENVIRONMENT_VERSION_INVALID",
            "The FlowRun Environment Version is unavailable",
            409,
            {"environment_version_id": run.environment_version_id},
        )
    validate_runtime_manifest(environment.manifest_json, environment_version_id=environment.id)
    definition = _snapshot_definition(
        db,
        run.flow_definition_id,
        environment_version_id=run.environment_version_id,
    )
    if _hash(definition) == current.definition_hash:
        return run_detail(db, run.id)
    action = _action(db, run.id, "SYNC_SNAPSHOT", idempotency_key)
    runtime_manifest = _compile_runtime_manifest(definition)
    snapshot = RunSnapshot(
        flow_run_id=run.id,
        version=current.version + 1,
        schema_version=2,
        definition_json=definition,
        definition_hash=_hash(definition),
        runtime_manifest_json=runtime_manifest,
        runtime_manifest_hash=_runtime_manifest_hash(runtime_manifest),
        environment_version_id=run.environment_version_id,
        created_by_action_id=action.id,
    )
    db.add(snapshot)
    db.flush()
    sandboxes.allocate_flow_run_runtime(db, run.id)
    runtime_allocation = sandboxes.runtime_allocation_for_flow_run(
        db, run.id, manifest_digest=snapshot.runtime_manifest_hash
    )
    sandboxes.ensure_flow_run_runtime_session(
        db,
        flow_run_id=run.id,
        environment_version_id=environment.id,
        runtime_image_digest=environment.image_digest,
        workspace_allocation=runtime_allocation,
    )
    hold_snapshot_memory_references(
        db,
        snapshot_id=snapshot.id,
        runtime_manifest=runtime_manifest,
    )
    run.active_snapshot_id = snapshot.id
    run.row_version += 1
    _event(db, run.id, "SNAPSHOT_SYNCED", {"version": snapshot.version})
    finish(db)
    return run_detail(db, run.id)


def _profile_entry_from_snapshot(node: dict[str, Any]) -> dict[str, Any] | None:
    asset = cast(dict[str, Any], node.get("asset") or {})
    capabilities = cast(list[object], asset.get("capabilities") or [])
    matches: list[dict[str, Any]] = []
    for item in capabilities:
        if not isinstance(item, dict):
            continue
        entry = cast(dict[str, Any], item)
        if entry.get("capability_type") == "AGENT_PROFILE":
            matches.append(entry)
    if len(matches) > 1:
        raise DomainError("SNAPSHOT_INVALID", "Snapshot node has multiple Agent Profiles", 409)
    return matches[0] if matches else None


def _profile_diff(
    source: dict[str, Any] | None, target: dict[str, Any]
) -> dict[str, dict[str, object]]:
    source_config = cast(dict[str, object], source.get("runtime_config") or {}) if source else {}
    target_config = cast(dict[str, object], target.get("runtime_config") or {})
    fields = sorted(set(source_config) | set(target_config))
    return {
        field: {"from": source_config.get(field), "to": target_config.get(field)}
        for field in fields
        if source_config.get(field) != target_config.get(field) and field not in {"storage_key"}
    }


def preview_agent_profile_switch(
    db: Session, run_id: str, flow_node_key: str, profile_version_id: str
) -> dict[str, Any]:
    run = _run(db, run_id)
    current = _active_snapshot(db, run)
    node = _node(current, flow_node_key)
    current_profile = _profile_entry_from_snapshot(node)
    try:
        target = cast(dict[str, Any], describe_agent_profile_version(db, profile_version_id))
    except ValueError as exc:
        raise DomainError("AGENT_PROFILE_INVALID", str(exc), 422) from exc
    return {
        "flow_run_id": run.id,
        "flow_node_key": flow_node_key,
        "active_snapshot_id": current.id,
        "active_snapshot_version": current.version,
        "source_profile_version_id": (
            str(current_profile.get("capability_id") or "") if current_profile else None
        ),
        "target_profile_version_id": target["capability_version_id"],
        "target_profile_digest": target["digest"],
        "changes": _profile_diff(current_profile, target),
        "requires_new_snapshot": True,
        "existing_attempts_unchanged": True,
    }


def switch_agent_profile(
    db: Session,
    run_id: str,
    payload: AgentProfileSwitchWrite,
    idempotency_key: str,
) -> dict[str, Any]:
    """Select an immutable Profile for a new Snapshot and a new Attempt."""

    run = _run(db, run_id)
    current = _active_snapshot(db, run)
    if current.version != payload.expected_active_version:
        raise conflict(
            "active snapshot changed",
            expected=payload.expected_active_version,
            actual=current.version,
        )
    node = _node(current, payload.flow_node_key)
    current_profile = _profile_entry_from_snapshot(node)
    current_profile_id = (
        str(current_profile.get("capability_id") or "") if current_profile else None
    )
    if payload.source_profile_version_id != current_profile_id:
        raise DomainError(
            "AGENT_PROFILE_SWITCH_CONFLICT",
            "The active Profile changed before the switch",
            409,
            {"expected": payload.source_profile_version_id, "actual": current_profile_id},
        )
    try:
        target = cast(
            dict[str, Any], describe_agent_profile_version(db, payload.profile_version_id)
        )
    except ValueError as exc:
        raise DomainError("AGENT_PROFILE_INVALID", str(exc), 422) from exc
    if target["digest"] != payload.expected_profile_digest:
        raise DomainError(
            "AGENT_PROFILE_VERSION_CONFLICT",
            "Target Agent Profile digest changed",
            409,
            {"expected": payload.expected_profile_digest, "actual": target["digest"]},
        )

    definition = copy.deepcopy(current.definition_json)
    target_node: dict[str, Any] | None = None
    for item in cast(list[object], definition.get("nodes") or []):
        if not isinstance(item, dict):
            continue
        candidate = cast(dict[str, Any], item)
        if candidate.get("instance_key") == payload.flow_node_key:
            target_node = candidate
            break
    if target_node is None:
        raise DomainError("SNAPSHOT_INVALID", "Target node is missing", 409)
    asset = cast(dict[str, Any], target_node.get("asset") or {})
    capabilities = cast(list[dict[str, Any]], asset.get("capabilities") or [])
    capabilities = [item for item in capabilities if item.get("capability_type") != "AGENT_PROFILE"]
    target_config = cast(dict[str, Any], target["runtime_config"])
    referenced = {
        "TOOL_POLICY": target_config["tool_policy_version_id"],
        "CONTEXT_POLICY": target_config["context_policy_version_id"],
        "MEMORY_POLICY": target_config["memory_policy_version_id"],
        "CRITIC_POLICY": target_config["critic_policy_version_id"],
    }
    for capability_type, version_id in referenced.items():
        matches = [item for item in capabilities if item.get("capability_type") == capability_type]
        if len(matches) != 1 or matches[0].get("capability_id") != version_id:
            raise DomainError(
                "AGENT_PROFILE_REFERENCE_MISMATCH",
                "Target Profile references do not match the active Snapshot node",
                422,
                {"capability_type": capability_type, "profile": version_id},
            )
    capabilities.append(
        {
            "id": None,
            "capability_id": target["capability_version_id"],
            "capability_type": "AGENT_PROFILE",
            "capability_key": target["capability_key"],
            "normalized_config": target["runtime_config"],
            "position": len(capabilities),
        }
    )
    asset["capabilities"] = capabilities
    executor = cast(dict[str, Any], asset.get("executor") or {})
    executor["confirmation_policy"] = target_config["confirmation_policy"]
    executor["max_iterations"] = target_config["max_iterations"]
    profile_condenser = target_config.get("condenser")
    if profile_condenser is not None:
        if not isinstance(profile_condenser, dict):
            raise DomainError("AGENT_PROFILE_INVALID", "Profile condenser is invalid", 422)
        upstream_condenser = cast(dict[str, Any], profile_condenser)
        kind = str(upstream_condenser.get("condenser_kind") or "llm_summarizing")
        if kind == "no_op":
            executor["condenser"] = {"kind": "NO_OP"}
        elif kind == "llm_summarizing":
            executor["condenser"] = {
                "kind": "LLM_SUMMARIZING",
                "model_provider_id": executor.get("model_provider_id"),
                "model_name": executor.get("model_name"),
                "max_size": upstream_condenser.get("max_size", 240),
                "max_tokens": upstream_condenser.get("max_tokens"),
                "keep_first": upstream_condenser.get("keep_first", 2),
                "minimum_progress": upstream_condenser.get("minimum_progress", 0.1),
                "hard_context_reset_max_retries": upstream_condenser.get(
                    "hard_context_reset_max_retries", 5
                ),
                "hard_context_reset_context_scaling": upstream_condenser.get(
                    "hard_context_reset_context_scaling", 0.8
                ),
            }
        else:
            raise DomainError("AGENT_PROFILE_INVALID", "Profile condenser kind is unsupported", 422)

    action = _action(
        db,
        run.id,
        "SWITCH_AGENT_PROFILE",
        idempotency_key,
        {
            "flow_node_key": payload.flow_node_key,
            "source_profile_version_id": current_profile_id,
            "target_profile_version_id": target["capability_version_id"],
            "target_profile_digest": target["digest"],
            "changes": _profile_diff(current_profile, target),
            "model_cost_comparison": payload.model_cost_comparison,
            "rollback_profile_version_id": current_profile_id,
        },
    )
    runtime_manifest = _compile_runtime_manifest(definition)
    # Compile-time validation must reject a Profile whose compatibility
    # declarations drift from the frozen node policies before anything is
    # persisted or a new Attempt is created.
    runtime_node(
        definition=definition,
        manifest=runtime_manifest,
        expected_hash=_runtime_manifest_hash(runtime_manifest),
        snapshot_id="profile-switch-preview",
        instance_key=payload.flow_node_key,
    )
    snapshot = RunSnapshot(
        flow_run_id=run.id,
        version=current.version + 1,
        schema_version=2,
        definition_json=definition,
        definition_hash=_hash(definition),
        runtime_manifest_json=runtime_manifest,
        runtime_manifest_hash=_runtime_manifest_hash(runtime_manifest),
        environment_version_id=run.environment_version_id,
        created_by_action_id=action.id,
    )
    db.add(snapshot)
    db.flush()
    hold_snapshot_memory_references(db, snapshot_id=snapshot.id, runtime_manifest=runtime_manifest)
    run.active_snapshot_id = snapshot.id
    run.row_version += 1

    artifact_ids: dict[str, str] = {}
    if payload.copy_input_bindings_from_attempt_id:
        source_attempt = _attempt(db, payload.copy_input_bindings_from_attempt_id)
        source_node_run = _node_run(db, source_attempt.node_run_id)
        if (
            source_node_run.flow_run_id != run.id
            or source_node_run.flow_node_snapshot_key != payload.flow_node_key
        ):
            raise DomainError(
                "AGENT_PROFILE_SWITCH_INPUT_INVALID",
                "Input bindings must come from the same Run and node",
                422,
            )
        artifact_ids = {
            binding.input_field_key: binding.artifact_version_id
            for binding in _bindings(db, source_attempt.id)
        }
    node_run, attempt = _create_node_run(
        db, run, payload.flow_node_key, artifact_ids, "PROFILE_SWITCH"
    )
    _event(
        db,
        run.id,
        "AGENT_PROFILE_SWITCHED",
        {
            "snapshot_version": snapshot.version,
            "source_profile_version_id": current_profile_id,
            "target_profile_version_id": target["capability_version_id"],
            "target_profile_digest": target["digest"],
            "model_cost_comparison": payload.model_cost_comparison,
            "existing_attempts_unchanged": True,
        },
        node_run.id,
        attempt.id,
    )
    finish(db)
    return {
        "snapshot_id": snapshot.id,
        "snapshot_version": snapshot.version,
        "attempt": attempt_detail(db, attempt.id),
        "rollback_profile_version_id": current_profile_id,
    }


def complete_run(db: Session, run_id: str, idempotency_key: str) -> dict[str, Any]:
    run = _run(db, run_id)
    attempts = list(
        db.scalars(
            select(NodeAttempt)
            .join(NodeRun, NodeRun.id == NodeAttempt.node_run_id)
            .where(NodeRun.flow_run_id == run.id)
        )
    )
    if any(x.state == AttemptState.EXECUTING for x in attempts):
        raise illegal("cannot complete while attempt is executing")
    _action(db, run.id, "COMPLETE_FLOW_RUN", idempotency_key)
    for node_run in db.scalars(select(NodeRun).where(NodeRun.flow_run_id == run.id)):
        if node_run.state == NodeRunState.ACTIVE:
            node_run.state = NodeRunState.CANCELLED
    for attempt in attempts:
        if attempt.state not in {
            AttemptState.ACCEPTED,
            AttemptState.REJECTED,
            AttemptState.CANCELLED,
        }:
            attempt.state = AttemptState.CANCELLED
    run.state = FlowRunState.COMPLETED
    run.completion_mode = "HUMAN"
    run.finished_at = now()
    _event(db, run.id, "FLOW_RUN_COMPLETED", {"mode": "HUMAN"})
    finish(db)
    return run_detail(db, run.id)


def cancel_run(db: Session, run_id: str, idempotency_key: str) -> dict[str, Any]:
    run = _run(db, run_id)
    if run.state in {FlowRunState.COMPLETED, FlowRunState.CANCELLED}:
        return run_detail(db, run.id)
    _action(db, run.id, "CANCEL_FLOW_RUN", idempotency_key)
    run.state = FlowRunState.CANCELLED
    run.finished_at = now()
    node_runs = list(db.scalars(select(NodeRun).where(NodeRun.flow_run_id == run.id)))
    attempts = (
        list(
            db.scalars(
                select(NodeAttempt).where(
                    NodeAttempt.node_run_id.in_([item.id for item in node_runs])
                )
            )
        )
        if node_runs
        else []
    )
    for node_run in node_runs:
        if node_run.state == NodeRunState.ACTIVE:
            node_run.state = NodeRunState.CANCELLED
    for attempt in attempts:
        if attempt.state in {
            AttemptState.ACCEPTED,
            AttemptState.REJECTED,
            AttemptState.CANCELLED,
        }:
            continue
        attempt.state = AttemptState.CANCELLED
        attempt.state_version += 1
        if _runtime_cancel_targets(db, attempt):
            attempt.runtime_phase = "CANCELLING"
            sandbox_ids = _managed_runtime_sandbox_ids(db, attempt)
            enqueue(
                db,
                task_type="CANCEL_RUNTIME",
                aggregate_type="ATTEMPT",
                aggregate_id=attempt.id,
                idempotency_key=f"cancel-runtime:{attempt.id}:{attempt.conversation_id}",
                payload={"sandbox_ids": sorted(sandbox_ids)},
            ).max_attempts = 20
        else:
            attempt.runtime_phase = "CANCELLED"
    _event(db, run.id, "FLOW_RUN_CANCELLED")
    finish(db)
    return run_detail(db, run.id)


def delete_run(db: Session, run_id: str) -> None:
    """Permanently remove a run and all durable execution data it owns.

    The Runtime Provider owns physical cleanup. Conversation bindings cascade
    with the FlowRun; OpenHands state is deleted only through that Run lifecycle.
    """

    run = _run(db, run_id)
    node_run_ids = list(db.scalars(select(NodeRun.id).where(NodeRun.flow_run_id == run.id)))
    attempt_ids = (
        list(db.scalars(select(NodeAttempt.id).where(NodeAttempt.node_run_id.in_(node_run_ids))))
        if node_run_ids
        else []
    )
    attempts = (
        list(db.scalars(select(NodeAttempt).where(NodeAttempt.id.in_(attempt_ids))))
        if attempt_ids
        else []
    )
    conversation_ids = list(
        db.scalars(
            select(FlowRunConversationBinding.id).where(
                FlowRunConversationBinding.flow_run_id == run.id
            )
        )
    )
    artifacts = list(
        db.scalars(select(ArtifactVersion).where(ArtifactVersion.flow_run_id == run.id))
    )
    storage_keys = [item.storage_key for item in artifacts if item.storage_key]
    workspace_root = Path(get_settings().workspace_root).resolve()
    workspace_paths: set[Path] = set()
    for attempt in attempts:
        if not attempt.workspace_ref:
            continue
        path = Path(attempt.workspace_ref).resolve()
        if path != workspace_root and path.is_relative_to(workspace_root):
            workspace_paths.add(path)
    task_aggregate_ids = [*attempt_ids, *conversation_ids]
    if task_aggregate_ids:
        db.execute(
            delete(BackgroundTask).where(BackgroundTask.aggregate_id.in_(task_aggregate_ids))
        )
    if attempt_ids:
        db.execute(
            delete(AttemptInputBinding).where(AttemptInputBinding.attempt_id.in_(attempt_ids))
        )
        db.execute(delete(GateEvaluation).where(GateEvaluation.attempt_id.in_(attempt_ids)))
    db.execute(delete(HumanAction).where(HumanAction.flow_run_id == run.id))
    db.execute(delete(RunEvent).where(RunEvent.flow_run_id == run.id))
    db.execute(delete(ArtifactVersion).where(ArtifactVersion.flow_run_id == run.id))
    if attempt_ids:
        db.execute(delete(NodeAttempt).where(NodeAttempt.id.in_(attempt_ids)))
    if node_run_ids:
        db.execute(delete(NodeRun).where(NodeRun.id.in_(node_run_ids)))
    db.execute(delete(RunSnapshot).where(RunSnapshot.flow_run_id == run.id))
    sandboxes.delete_flow_run_runtimes_now(db, run.id)
    sandboxes.delete_flow_run_runtime_allocation(db, run.id)
    db.delete(run)
    store = get_artifact_store()
    for key in storage_keys:
        register_commit_action(db, lambda key=key: store.delete(key))
    for path in workspace_paths:
        register_commit_action(db, lambda path=path: shutil.rmtree(path, ignore_errors=True))
    finish(db)


def attempt_detail(db: Session, attempt_id: str) -> dict[str, Any]:
    attempt = _attempt(db, attempt_id)
    bindings = _bindings(db, attempt.id)
    artifacts = list(
        db.scalars(
            select(ArtifactVersion)
            .where(ArtifactVersion.producer_attempt_id == attempt.id)
            .order_by(ArtifactVersion.field_key, ArtifactVersion.version_no)
        )
    )
    gates = list(
        db.scalars(
            select(GateEvaluation)
            .where(GateEvaluation.attempt_id == attempt.id)
            .order_by(GateEvaluation.stage, GateEvaluation.policy_position)
        )
    )
    confirmation_batches = list(
        db.scalars(
            select(RuntimeConfirmationApproval)
            .where(RuntimeConfirmationApproval.attempt_id == attempt.id)
            .order_by(RuntimeConfirmationApproval.created_at.desc())
        )
    )
    return {
        "id": attempt.id,
        "node_run_id": attempt.node_run_id,
        "attempt_no": attempt.attempt_no,
        "snapshot_id": attempt.snapshot_id,
        "state": attempt.state,
        "state_version": attempt.state_version,
        "runtime_phase": attempt.runtime_phase,
        "conversation_id": attempt.conversation_id,
        "workspace_ref": attempt.workspace_ref,
        "startup_mode": attempt.startup_mode,
        "startup_capability_key": attempt.startup_capability_key,
        "startup_prompt": attempt.startup_prompt,
        "model_name": attempt.model_name,
        "reasoning_effort": attempt.reasoning_effort,
        "confirmation_policy": attempt.confirmation_policy,
        "condenser": copy.deepcopy(attempt.condenser_config_json),
        "output_targets": attempt.output_targets_json,
        "error_code": attempt.error_code,
        "error_detail": attempt.error_detail,
        "runtime_cancel_recovery_modes": _runtime_cancel_recovery_modes(db, attempt),
        "input_bindings": [
            {
                "id": x.id,
                "input_field_key": x.input_field_key,
                "artifact_version_id": x.artifact_version_id,
                "binding_source": x.binding_source,
            }
            for x in bindings
        ],
        "artifacts": [_artifact_dict(x) for x in artifacts],
        "gate_evaluations": [
            {
                "id": x.id,
                "stage": x.stage,
                "policy_snapshot_key": x.policy_snapshot_key,
                "policy_position": x.policy_position,
                "evaluation_attempt": x.evaluation_attempt,
                "state": x.state,
                "decision": x.decision,
                "result": x.result_json,
                "error_code": x.error_code,
                "created_at": x.created_at.isoformat(),
            }
            for x in gates
        ],
        "runtime_confirmation_batches": [_confirmation_dict(item) for item in confirmation_batches],
        "created_at": attempt.created_at.isoformat(),
        "updated_at": attempt.updated_at.isoformat(),
    }


def node_run_detail(db: Session, node_run_id: str) -> dict[str, Any]:
    item = _node_run(db, node_run_id)
    attempts = list(
        db.scalars(
            select(NodeAttempt)
            .where(NodeAttempt.node_run_id == item.id)
            .order_by(NodeAttempt.attempt_no)
        )
    )
    return {
        "id": item.id,
        "flow_run_id": item.flow_run_id,
        "flow_node_snapshot_key": item.flow_node_snapshot_key,
        "sequence_no": item.sequence_no,
        "state": item.state,
        "accepted_attempt_id": item.accepted_attempt_id,
        "created_from": item.created_from,
        "activated_at": item.activated_at.isoformat(),
        "attempts": [attempt_detail(db, x.id) for x in attempts],
    }


def run_detail(db: Session, run_id: str) -> dict[str, Any]:
    run = _run(db, run_id)
    environment = (
        db.get(EnvironmentVersion, run.environment_version_id)
        if run.environment_version_id
        else None
    )
    snapshots = list(
        db.scalars(
            select(RunSnapshot)
            .where(RunSnapshot.flow_run_id == run.id)
            .order_by(RunSnapshot.version)
        )
    )
    node_runs = list(
        db.scalars(
            select(NodeRun).where(NodeRun.flow_run_id == run.id).order_by(NodeRun.sequence_no)
        )
    )
    artifacts = list(
        db.scalars(
            select(ArtifactVersion)
            .where(ArtifactVersion.flow_run_id == run.id)
            .order_by(ArtifactVersion.created_at)
        )
    )
    accepted = sum(x.state == NodeRunState.ACCEPTED for x in node_runs)
    terminal = sum(x.state in {NodeRunState.ACCEPTED, NodeRunState.CANCELLED} for x in node_runs)
    return {
        "id": run.id,
        "flow_definition_id": run.flow_definition_id,
        "flow_name": (snapshots[-1].definition_json.get("name") if snapshots else None),
        "flow_row_version": (
            snapshots[-1].definition_json.get("row_version") if snapshots else None
        ),
        "run_no": run.run_no,
        "name": run.name,
        "state": run.state,
        "row_version": run.row_version,
        "completion_mode": run.completion_mode,
        "environment_version_id": run.environment_version_id,
        "environment_version": (
            {
                "id": environment.id,
                "environment_id": environment.environment_id,
                "version_no": environment.version_no,
                "state": environment.state,
                "image_reference": environment.image_reference,
                "image_digest": environment.image_digest,
                "manifest": environment.manifest_json or {},
                "created_at": environment.created_at.isoformat(),
            }
            if environment
            else None
        ),
        "lark_folder_token": run.lark_folder_token,
        "lark_folder_url": run.lark_folder_url,
        "active_snapshot_id": run.active_snapshot_id,
        "active_snapshot_version": next(
            (x.version for x in snapshots if x.id == run.active_snapshot_id), None
        ),
        "progress": {"accepted": accepted, "terminal": terminal, "active": len(node_runs)},
        "snapshots": [
            {
                "id": x.id,
                "version": x.version,
                "schema_version": x.schema_version,
                "definition_hash": x.definition_hash,
                "definition": x.definition_json,
                "runtime_manifest_hash": x.runtime_manifest_hash,
                "runtime_manifest": x.runtime_manifest_json,
                "environment_version_id": x.environment_version_id,
                "created_at": x.created_at.isoformat(),
            }
            for x in snapshots
        ],
        "node_runs": [node_run_detail(db, x.id) for x in node_runs],
        "artifacts": [_artifact_dict(x) for x in artifacts],
        "started_at": run.started_at.isoformat(),
        "finished_at": run.finished_at.isoformat() if run.finished_at else None,
    }


def list_runs(db: Session) -> list[dict[str, Any]]:
    runs = list(db.scalars(select(FlowRun).order_by(FlowRun.started_at.desc())))
    if not runs:
        return []

    run_ids = [item.id for item in runs]
    node_runs = list(
        db.scalars(
            select(NodeRun)
            .where(NodeRun.flow_run_id.in_(run_ids))
            .order_by(NodeRun.flow_run_id, NodeRun.sequence_no)
        )
    )
    node_run_ids = [item.id for item in node_runs]
    attempts = (
        list(
            db.scalars(
                select(NodeAttempt)
                .where(NodeAttempt.node_run_id.in_(node_run_ids))
                .order_by(NodeAttempt.node_run_id, NodeAttempt.attempt_no)
            )
        )
        if node_run_ids
        else []
    )
    snapshot_ids = [item.active_snapshot_id for item in runs if item.active_snapshot_id]
    snapshots = (
        list(db.scalars(select(RunSnapshot).where(RunSnapshot.id.in_(snapshot_ids))))
        if snapshot_ids
        else []
    )

    nodes_by_run: dict[str, list[NodeRun]] = {item.id: [] for item in runs}
    for node_run in node_runs:
        nodes_by_run[node_run.flow_run_id].append(node_run)
    attempts_by_node: dict[str, list[NodeAttempt]] = {item.id: [] for item in node_runs}
    for attempt in attempts:
        attempts_by_node[attempt.node_run_id].append(attempt)
    snapshots_by_id = {item.id: item for item in snapshots}
    pending_states = {
        AttemptState.WAITING_START_CONFIRMATION,
        AttemptState.WAITING_HUMAN,
        AttemptState.WAITING_ACCEPTANCE,
        AttemptState.START_BLOCKED,
        AttemptState.END_BLOCKED,
    }

    result: list[dict[str, Any]] = []
    for run in runs:
        run_nodes = nodes_by_run[run.id]
        current_node = run_nodes[-1] if run_nodes else None
        current_attempts = attempts_by_node.get(current_node.id, []) if current_node else []
        current_attempt = current_attempts[-1] if current_attempts else None
        snapshot = snapshots_by_id.get(run.active_snapshot_id or "")
        snapshot_node = None
        if snapshot and current_node:
            snapshot_node = next(
                (
                    item
                    for item in snapshot.definition_json.get("nodes", [])
                    if item.get("instance_key") == current_node.flow_node_snapshot_key
                ),
                None,
            )
        current_name = None
        if snapshot_node:
            current_name = (
                snapshot_node.get("alias")
                or snapshot_node.get("asset", {}).get("name")
                or snapshot_node.get("instance_key")
            )
        accepted = sum(item.state == NodeRunState.ACCEPTED for item in run_nodes)
        terminal = sum(
            item.state in {NodeRunState.ACCEPTED, NodeRunState.CANCELLED} for item in run_nodes
        )
        activity_times = [run.started_at]
        if run.finished_at:
            activity_times.append(run.finished_at)
        activity_times.extend(item.activated_at for item in run_nodes)
        activity_times.extend(
            attempt.updated_at
            for node_run in run_nodes
            for attempt in attempts_by_node.get(node_run.id, [])
        )
        result.append(
            {
                "id": run.id,
                "flow_definition_id": run.flow_definition_id,
                "flow_name": (snapshot.definition_json.get("name") if snapshot else None),
                "flow_row_version": (
                    snapshot.definition_json.get("row_version") if snapshot else None
                ),
                "run_no": run.run_no,
                "name": run.name,
                "state": run.state,
                "completion_mode": run.completion_mode,
                "environment_version_id": run.environment_version_id,
                "active_snapshot_version": snapshot.version if snapshot else None,
                "current_node_key": (current_node.flow_node_snapshot_key if current_node else None),
                "current_node_name": current_name,
                "current_attempt_state": current_attempt.state if current_attempt else None,
                "has_pending_action": bool(
                    current_attempt and current_attempt.state in pending_states
                ),
                "progress": {
                    "accepted": accepted,
                    "terminal": terminal,
                    "active": len(run_nodes),
                },
                "started_at": run.started_at.isoformat(),
                "updated_at": max(activity_times).isoformat(),
                "finished_at": run.finished_at.isoformat() if run.finished_at else None,
            }
        )
    return result


def events(db: Session, run_id: str, after: int = 0, *, limit: int = 500) -> list[dict[str, Any]]:
    _run(db, run_id)
    bounded_limit = max(1, min(limit, 500))
    rows = list(
        db.scalars(
            select(RunEvent)
            .where(RunEvent.flow_run_id == run_id, RunEvent.cursor > after)
            .order_by(RunEvent.cursor)
            .limit(bounded_limit)
        )
    )
    return [
        {
            "cursor": x.cursor,
            "flow_run_id": x.flow_run_id,
            "node_run_id": x.node_run_id,
            "attempt_id": x.attempt_id,
            "event_type": x.event_type,
            "payload": x.payload_json,
            "occurred_at": x.occurred_at.isoformat(),
        }
        for x in rows
    ]
