from __future__ import annotations

import base64
import copy
import hashlib
import json
import shutil
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from pathlib import Path, PurePosixPath
from typing import Any, cast
from uuid import UUID, uuid4

from sqlalchemy import delete, func, select, update
from sqlalchemy.orm import Session

from flowweave.modules.agent_sessions import public as agent_sessions
from flowweave.modules.agent_sessions.public import (
    AgentConversationBinding,
    AgentConversationCapability,
    AgentConversationCommand,
    AgentConversationMessageAttachment,
)
from flowweave.modules.agent_workspaces.public import (
    AgentWorkDirectory,
    AgentWorkDirectoryPath,
    AgentWorkDirectoryVersion,
)
from flowweave.modules.catalog.public import (
    describe_asset,
    hold_snapshot_memory_references,
)
from flowweave.modules.environments.public import (
    lock_referenceable_version,
    validate_runtime_manifest,
)
from flowweave.modules.flows.public import describe_flow, load_flow
from flowweave.modules.gates.public import (
    GateExecutionPlan,
    GateResult,
    execute_gate_plan,
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
from flowweave.runtime.dependencies import get_runtime
from flowweave.runtime.manifest import (
    runtime_manifest_hash as _runtime_manifest_hash,
)
from flowweave.runtime.manifest import (
    runtime_node,
)
from flowweave.runtime.request import (
    build_runtime_request,
)
from flowweave.runtime.routing import runtime_for
from flowweave.runtime.workspace import (
    attempt_workspace_path,
    ensure_flow_run_attempt_workspace,
)
from flowweave.shared.application.transactions import (
    finish,
    register_commit_action,
    register_rollback_action,
)
from flowweave.shared.artifact_store import get_artifact_store
from flowweave.shared.domain.openhands import OPENHANDS_VERSION
from flowweave.shared.errors import DomainError, conflict, illegal, not_found
from flowweave.shared.models import (
    ArtifactVersion,
    AttemptInputBinding,
    AttemptState,
    BackgroundTask,
    EnvironmentVersion,
    FlowRun,
    FlowRunRuntime,
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
    ArtifactWrite,
    AttemptStartWrite,
    AttemptVersionWrite,
    AutomaticRunCopyWrite,
    AutomaticRunDraftUpdateWrite,
    AutomaticRunDraftWrite,
    AutomaticRunStartWrite,
    HumanInputWrite,
    InputBindingsWrite,
    ManualAttemptOutputsWrite,
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
    """Freeze only Flow-owned node identity.

    Agent model, capabilities and OpenHands policy are frozen on each shared
    Conversation binding when that Conversation is explicitly started.
    """

    nodes: dict[str, dict[str, str]] = {}
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
        asset_id = str(node.get("node_asset_id") or asset.get("id") or "")
        if not asset_id:
            raise DomainError("SNAPSHOT_INVALID", "Snapshot node asset id is missing", 409)
        nodes[instance_key] = {"node_asset_id": asset_id}
    return {
        "schema_version": 3,
        "openhands_version": OPENHANDS_VERSION,
        "nodes": nodes,
    }


def _preserve_runtime_oracle_profile(
    current_manifest: dict[str, Any], candidate_manifest: dict[str, Any]
) -> dict[str, Any]:
    """Keep the Runtime-global ``oracle`` name immutable for one FlowRun.

    OpenHands 1.44 resolves auxiliary LLM profiles from a Runtime-global store,
    not from a Conversation-scoped store.  Once any Snapshot binds that name,
    later Snapshots may stop using the Tool but cannot recycle the name for a
    different provider/model while historical Conversations remain resumable.
    """

    if candidate_manifest.get("schema_version") == 3:
        return candidate_manifest
    current = current_manifest.get("oracle_profile")
    candidate = candidate_manifest.get("oracle_profile")
    if current is None:
        return candidate_manifest
    if not isinstance(current, dict):
        raise DomainError(
            "SNAPSHOT_MANIFEST_INVALID",
            "The active Snapshot Oracle profile is invalid",
            409,
        )
    current_profile = cast(dict[str, Any], current)
    if candidate is not None and candidate != current_profile:
        raise DomainError(
            "RUNTIME_ORACLE_PROFILE_IMMUTABLE",
            "The FlowRun Runtime Oracle profile cannot change across Snapshots",
            409,
        )
    candidate_manifest["oracle_profile"] = copy.deepcopy(current_profile)
    return candidate_manifest


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


def _reachable_node_keys(definition: dict[str, Any], start_node_key: str) -> list[str]:
    ordered = [str(item.get("instance_key") or "") for item in definition.get("nodes", [])]
    known = {key for key in ordered if key}
    if start_node_key not in known:
        raise not_found("flow_node_snapshot", start_node_key)
    targets: dict[str, list[str]] = {key: [] for key in known}
    for edge in definition.get("edges", []):
        source = str(edge.get("source_instance_key") or "")
        target = str(edge.get("target_instance_key") or "")
        if source in known and target in known and target not in targets[source]:
            targets[source].append(target)
    reached: set[str] = set()
    pending = [start_node_key]
    while pending:
        current = pending.pop(0)
        if current in reached:
            continue
        reached.add(current)
        pending.extend(targets[current])
    return [key for key in ordered if key in reached]


def _freeze_draft_agent_preset(db: Session, raw: dict[str, Any]) -> dict[str, Any]:
    config = agent_sessions.resolve_session_config(
        db,
        model_provider_id=raw.get("model_provider_id"),
        model_name=raw.get("model_name"),
        reasoning_effort=raw.get("reasoning_effort"),
        capability_version_ids=tuple(raw.get("capability_version_ids") or ()),
    )
    return {
        "model_provider_id": config.model_provider_id,
        "model_name": config.model_name,
        "reasoning_effort": config.reasoning_effort,
        "node_context_enabled": bool(raw.get("node_context_enabled")),
        "node_context_prompt": raw.get("node_context_prompt"),
        "capability_version_ids": [item.version_id for item in config.capabilities],
        "capabilities": [
            {
                "version_id": item.version_id,
                "capability_type": item.capability_type,
                "capability_key": item.capability_key,
                "digest": item.digest,
            }
            for item in config.capabilities
        ],
    }


def _freeze_automatic_plan(
    db: Session,
    run: FlowRun,
    definition: dict[str, Any],
    *,
    start_node_key: str,
    node_plans: dict[str, Any],
) -> dict[str, Any]:
    reachable = _reachable_node_keys(definition, start_node_key)
    known_nodes = {
        str(item.get("instance_key") or ""): cast(dict[str, Any], item)
        for item in definition.get("nodes", [])
    }
    unknown = sorted(set(node_plans) - set(known_nodes))
    if unknown:
        raise DomainError(
            "AUTOMATION_PLAN_NODE_INVALID",
            "Automatic plan contains nodes outside the frozen Flow",
            422,
            {"node_keys": unknown},
        )
    unreachable = sorted(set(node_plans) - set(reachable))
    if unreachable:
        raise DomainError(
            "AUTOMATION_PLAN_NODE_UNREACHABLE",
            "Automatic plan contains nodes outside the selected start node's reachable flow",
            422,
            {"node_keys": unreachable},
        )
    frozen_nodes: dict[str, Any] = {}
    for node_key, plan_model in node_plans.items():
        raw = plan_model.model_dump(mode="json")
        node = known_nodes[node_key]
        asset = cast(dict[str, Any], node.get("asset") or {})
        input_types = {
            str(item.get("field_key") or ""): str(item.get("data_type") or "")
            for item in asset.get("inputs", [])
        }
        unknown_inputs = sorted(
            (set(raw["artifact_ids"]) | set(raw["input_urls"])) - set(input_types)
        )
        if unknown_inputs:
            raise DomainError(
                "AUTOMATION_PLAN_INPUT_INVALID",
                "Automatic plan input is not declared by the target node",
                422,
                {"node_key": node_key, "field_keys": unknown_inputs},
            )
        duplicate_inputs = sorted(set(raw["artifact_ids"]) & set(raw["input_urls"]))
        if duplicate_inputs:
            raise DomainError(
                "AUTOMATION_PLAN_INPUT_INVALID",
                "Automatic plan input has more than one configured source",
                422,
                {"node_key": node_key, "field_keys": duplicate_inputs},
            )
        non_url_inputs = sorted(
            field_key for field_key in raw["input_urls"] if input_types[field_key] != "URL"
        )
        if non_url_inputs:
            raise DomainError(
                "AUTOMATION_PLAN_INPUT_INVALID",
                "Automatic plan URL input does not match the declared input type",
                422,
                {"node_key": node_key, "field_keys": non_url_inputs},
            )
        _validate_input_bindings(db, run, node, dict(raw["artifact_ids"]))
        frozen_gates: list[dict[str, Any]] = []
        for gate in raw["gates"]:
            gate_preset = dict(gate["agent_preset"])
            gate_config = agent_sessions.resolve_session_config(
                db,
                model_provider_id=gate_preset.get("model_provider_id"),
                model_name=gate_preset.get("model_name"),
                reasoning_effort=gate_preset.get("reasoning_effort"),
                capability_version_ids=(),
            )
            frozen_gates.append(
                {
                    **gate,
                    "agent_preset": {
                        "model_provider_id": gate_config.model_provider_id,
                        "model_name": gate_config.model_name,
                        "reasoning_effort": gate_config.reasoning_effort,
                    },
                }
            )
        frozen_nodes[node_key] = {
            "startup_prompt": raw["startup_prompt"],
            "agent_preset": _freeze_draft_agent_preset(db, dict(raw["agent_preset"])),
            "gates": frozen_gates,
            "artifact_ids": dict(raw["artifact_ids"]),
            "input_urls": dict(raw["input_urls"]),
        }
    issues: list[dict[str, str]] = []
    for key in reachable:
        if key not in frozen_nodes:
            issues.append(
                {
                    "code": "NODE_PLAN_REQUIRED",
                    "node_key": key,
                    "message": "请配置此节点的自动执行预设",
                }
            )
    for node_key, node_plan in frozen_nodes.items():
        node = known_nodes[node_key]
        asset = cast(dict[str, Any], node.get("asset") or {})
        declared_inputs = {
            str(field.get("field_key") or "")
            for field in cast(list[dict[str, Any]], asset.get("inputs") or [])
            if str(field.get("field_key") or "")
        }
        mapped_inputs = {
            str(mapping.get("target_input_key") or "")
            for mapping in definition.get("port_mappings", [])
            if str(mapping.get("target_instance_key") or "") == node_key
            and str(mapping.get("source_instance_key") or "") in reachable
        }
        explicit_inputs = set(node_plan["artifact_ids"]) | set(node_plan["input_urls"])
        missing_inputs = sorted(declared_inputs - mapped_inputs - explicit_inputs)
        if missing_inputs:
            issues.append(
                {
                    "code": "NODE_INPUT_REQUIRED",
                    "node_key": node_key,
                    "message": f"请配置未映射输入：{', '.join(missing_inputs)}",
                }
            )
    return {
        "status": "DRAFT",
        "start_node_key": start_node_key,
        "reachable_node_keys": reachable,
        "node_plans": frozen_nodes,
        "readiness": {
            "ready": not issues,
            "issues": issues,
        },
    }


def _run(db: Session, run_id: str) -> FlowRun:
    item = db.get(FlowRun, run_id)
    if not item:
        raise not_found("flow_run", run_id)
    return item


def _locked_run(db: Session, run_id: str) -> FlowRun:
    item = db.scalar(select(FlowRun).where(FlowRun.id == run_id).with_for_update())
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
    if get_settings().runtime_adapter == "mock":
        return RuntimeHandle(
            job_id=f"mock-job-{attempt.id}",
            conversation_id=openhands_conversation_id,
            cursor=cursor,
        )
    return agent_sessions.flow_node_locator.active_runtime_handle(
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
        "consumer_node_key": item.consumer_node_key,
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


_MAX_ARTIFACT_FILE_BYTES = 25 * 1024 * 1024


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


def prepare_file_artifact(
    *,
    field_key: str,
    filename: str,
    mime_type: str,
    content: bytes,
    metadata: dict[str, Any] | None = None,
    artifact_id: str | None = None,
) -> PreparedArtifact:
    """Freeze one arbitrary uploaded file without trusting a client path."""

    safe_name = filename.strip()
    if (
        not safe_name
        or len(safe_name) > 240
        or "\x00" in safe_name
        or Path(safe_name).name != safe_name
        or "\\" in safe_name
    ):
        raise DomainError("ARTIFACT_FILE_INVALID", "Artifact filename is invalid", 422)
    if not content or len(content) > _MAX_ARTIFACT_FILE_BYTES:
        raise DomainError(
            "ARTIFACT_FILE_TOO_LARGE",
            "Artifact file must be between 1 byte and 25 MiB",
            422,
            {"max_bytes": _MAX_ARTIFACT_FILE_BYTES},
        )
    normalized_mime = mime_type.lower().strip() or "application/octet-stream"
    if len(normalized_mime) > 100:
        raise DomainError("ARTIFACT_FILE_INVALID", "Artifact MIME type is invalid", 422)
    identifier = artifact_id or str(uuid4())
    storage_key = get_artifact_store().put(f"artifacts/versions/{identifier}", content)
    merged_metadata = dict(metadata or {})
    merged_metadata["filename"] = safe_name
    payload = ArtifactWrite.model_construct(
        field_key=field_key,
        artifact_type="FILE",
        inline_content=None,
        uri=None,
        mime_type=normalized_mime,
        metadata=merged_metadata,
    )
    return PreparedArtifact(
        id=identifier,
        payload=payload,
        inline_content=None,
        storage_key=storage_key,
        content_hash=hashlib.sha256(content).hexdigest(),
        byte_size=len(content),
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
    consumer_node_key: str | None = None,
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
        consumer_node_key=consumer_node_key,
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


def create_node_input_artifact(
    db: Session,
    run_id: str,
    instance_key: str,
    prepared: PreparedArtifact,
) -> dict[str, Any]:
    """Create one direct human input tied to one snapshot node contract."""

    run = _run(db, run_id)
    if run.state in {FlowRunState.COMPLETED, FlowRunState.CANCELLED}:
        raise illegal("cannot add input to terminal run", state=run.state)
    node = _node(_active_snapshot(db, run), instance_key)
    field_types = {field.key: field.data_type for field in _input_fields(node)}
    field_key = prepared.payload.field_key
    if field_key not in field_types or prepared.payload.artifact_type != field_types[field_key]:
        raise DomainError(
            "INPUT_BINDING_INVALID",
            "input does not match the declared node field",
            422,
            {"field": field_key},
        )
    item = _register_artifact(
        db,
        run_id,
        prepared,
        source="HUMAN_INPUT",
        consumer_node_key=instance_key,
    )
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
    if item.producer_attempt_id is not None or item.source not in {"HUMAN", "HUMAN_INPUT"}:
        raise DomainError(
            "ARTIFACT_DELETE_BLOCKED",
            "Only human-provided artifacts can be removed from the artifact pool",
            409,
            {"id": artifact_id},
        )
    plan_nodes = cast(dict[str, Any], (run.automation_plan_json or {}).get("node_plans") or {})
    plan_references = sorted(
        node_key
        for node_key, node_plan in plan_nodes.items()
        if artifact_id in set(cast(dict[str, str], node_plan.get("artifact_ids") or {}).values())
    )
    if plan_references:
        raise DomainError(
            "ARTIFACT_DELETE_BLOCKED",
            "Artifact is frozen by an automatic run draft",
            409,
            {"id": artifact_id, "node_keys": plan_references},
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
        if artifact.producer_attempt_id is None and artifact.consumer_node_key != str(
            node["instance_key"]
        ):
            raise DomainError(
                "INPUT_BINDING_INVALID",
                "human input is bound to a different node",
                422,
                {"id": artifact_id, "field": field_key},
            )


_MANUAL_NODE_CONTEXT_ID = "__node_context_prompt__"


def _node_with_selected_context(
    node: dict[str, Any],
    context_ids: list[str] | None,
    *,
    node_context_prompt: str | None = None,
) -> dict[str, Any]:
    """Keep legacy Attempts unchanged, but filter new explicit selections."""

    if context_ids is None:
        return node
    asset = cast(dict[str, Any], node.get("asset") or {})
    executor = cast(dict[str, Any], asset.get("executor") or {})
    selected = set(context_ids)
    next_executor = dict(executor)
    saved_context_prompt = str(executor.get("context_prompt") or "")
    next_executor["context_prompt"] = (
        (node_context_prompt if node_context_prompt is not None else saved_context_prompt)
        if _MANUAL_NODE_CONTEXT_ID in selected
        else ""
    )
    next_executor["context_capability_ids"] = [
        item
        for item in cast(list[object], executor.get("context_capability_ids") or [])
        if isinstance(item, str) and item in selected
    ]
    raw_contexts = asset.get("context_capabilities")
    next_asset = dict(asset)
    next_asset["executor"] = next_executor
    selected_contexts: list[dict[str, object]] = []
    if isinstance(raw_contexts, list):
        for item_value in cast(list[object], raw_contexts):
            if not isinstance(item_value, dict):
                continue
            item = cast(dict[str, object], item_value)
            if str(item.get("id") or "") in selected:
                selected_contexts.append(item)
    next_asset["context_capabilities"] = selected_contexts
    next_node = dict(node)
    next_node["asset"] = next_asset
    return next_node


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
    # Wake-up frames reduce latency, but are not a durable terminal-state
    # guarantee.  On a timeout or unavailable channel, enqueue one bounded
    # REST reconciliation through the normal state machine so a persisted
    # OpenHands Finish lifecycle cannot strand this Attempt in EXECUTING.
    poll_kind = "wakeup" if wakeup is not None and wakeup.notified else "reconcile"
    poll_task = enqueue(
        db,
        task_type="POLL_RUNTIME",
        aggregate_type="ATTEMPT",
        aggregate_id=current.id,
        idempotency_key=(
            f"poll-runtime-{poll_kind}:{current.id}:v{current.state_version}:{wakeup_no}"
        ),
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
    conversation = agent_sessions.flow_node_locator.conversation_binding(
        db,
        flow_run_id=flow_run_id,
        openhands_conversation_id=attempt.conversation_id,
    )
    active = db.scalar(
        select(RuntimeConfirmationApproval)
        .where(
            RuntimeConfirmationApproval.flow_run_conversation_binding_id == conversation.id,
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


def _review_artifact(item: ArtifactVersion) -> dict[str, Any]:
    """Add a bounded, local-only preview for semantic output review."""

    value = _gate_artifact(item)
    if item.artifact_type == "URL":
        value["review_preview"] = {"kind": "URL", "value": item.uri}
        return value
    raw: bytes | None = None
    if item.inline_content is not None:
        raw = item.inline_content.encode()
    elif item.storage_key:
        raw = get_artifact_store().read(item.storage_key)
    if raw is None:
        return value
    preview = raw[: 64 * 1024]
    try:
        text = preview.decode("utf-8")
    except UnicodeDecodeError:
        value["review_preview"] = {
            "kind": "BINARY",
            "byte_count": len(raw),
            "truncated": len(raw) > len(preview),
        }
        return value
    value["review_preview"] = {
        "kind": "TEXT",
        "content": text,
        "truncated": len(raw) > len(preview),
    }
    return value


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
        # Candidate content is exposed only as a bounded local preview. The
        # reviewer never receives a credentialed Artifact-store handle or an
        # unrestricted workspace path.
        "outputs": [_review_artifact(item) for item in outputs],
        "artifacts": [item["artifact"] for item in bindings]
        + [_gate_artifact(item) for item in outputs],
    }


_PLATFORM_OUTPUT_REVIEW_POLICY_ID = "__platform_output_contract__"
_PLATFORM_OUTPUT_REVIEW_POSITION = -10_000
_PLATFORM_OUTPUT_REVIEW_PROMPT = """
You are the platform's mandatory output-contract reviewer. Evaluate whether the
candidate outputs actually satisfy the node's declared output contract, not
merely whether every field exists. Use only the supplied frozen node definition,
input bindings, candidate artifacts, and evidence. Check each required output's
purpose, type, completeness, and whether the candidate is usable by the next
node. Do not infer success from the executing Agent's claim.

Return PASS only when every declared output is substantively fit for its stated
purpose. Return FAIL when revision or human review is needed. Your reasons must
identify the missing or inadequate field(s); include the selected artifact ids in
details.selected_output_artifact_ids when they are usable.
"""


def _platform_output_review_policy(db: Session, attempt: NodeAttempt) -> dict[str, Any]:
    """Build the unskippable first END gate from the execution binding.

    This is deliberately a normal isolated Gate Agent invocation: it gets a new
    native Conversation, no primary-Agent history or capabilities, and its result
    is persisted in ``GateEvaluation``.  The only special property is ownership:
    the platform injects it from the output contract, so a flow author cannot
    disable, reorder, or replace it with a weaker end gate.
    """

    binding = agent_sessions.flow_node_binding_for_attempt(db, attempt.id)
    if binding.model_provider_id is None and get_settings().runtime_adapter != "mock":
        raise DomainError(
            "OUTPUT_REVIEWER_CONFIGURATION_MISSING",
            "The execution conversation has no frozen model for output review",
            409,
            {"attempt_id": attempt.id},
        )
    return {
        "id": _PLATFORM_OUTPUT_REVIEW_POLICY_ID,
        "stage": "END",
        "position": _PLATFORM_OUTPUT_REVIEW_POSITION,
        "gate_type": "PROMPT",
        "enabled": True,
        "timeout_seconds": 300,
        "config": {
            "prompt": _PLATFORM_OUTPUT_REVIEW_PROMPT,
            "system_owned": True,
            "review_kind": "OUTPUT_CONTRACT",
        },
        # The review is a separate Agent call, but its model identity comes
        # from the node's already-frozen execution binding rather than a mutable
        # workspace default or an author-supplied gate preset.
        "agent_preset": {
            "model_provider_id": binding.model_provider_id,
            "model_name": binding.model_name,
            "reasoning_effort": binding.reasoning_effort,
        },
    }


@dataclass(frozen=True, slots=True)
class _PreparedGate:
    policy: dict[str, Any]
    execution_no: int
    plan: GateExecutionPlan


@dataclass(frozen=True, slots=True)
class _SidecarRuntimeConnection:
    runtime_session_id: str
    managed_runtime_id: str
    resource_name: str


def _flow_run_sidecar_connection(db: Session, flow_run_id: str) -> _SidecarRuntimeConnection:
    """Resolve a fenced production connection or the explicit MockRuntime locator."""

    runtime_owner_id = sandboxes.runtime_owner_flow_run_id(db, flow_run_id)
    if get_settings().runtime_adapter == "mock":
        session = db.scalar(
            select(FlowRunRuntime).where(FlowRunRuntime.flow_run_id == runtime_owner_id)
        )
        if session is None:
            raise DomainError(
                "RUNTIME_SESSION_NOT_ACTIVE",
                "The FlowRun has no Runtime Session",
                409,
                {"flow_run_id": flow_run_id},
            )
        return _SidecarRuntimeConnection(
            runtime_session_id=session.id,
            managed_runtime_id=f"mock-runtime:{runtime_owner_id}",
            resource_name=f"mock-runtime-{runtime_owner_id}",
        )
    connection = sandboxes.active_flow_run_runtime_connection(db, flow_run_id=runtime_owner_id)
    return _SidecarRuntimeConnection(
        runtime_session_id=connection.runtime_session_id,
        managed_runtime_id=connection.managed_runtime_id,
        resource_name=connection.resource_name,
    )


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
    run = _run(db, node_run.flow_run_id)
    if run.run_mode == "AUTOMATIC":
        if next_state == AttemptState.WAITING_START_CONFIRMATION:
            _dispatch_automatic_start(db, attempt)
            return
        if next_state == AttemptState.WAITING_ACCEPTANCE:
            _dispatch_automatic_advance(db, attempt)
            return
        # A failed automatic gate is intentionally not silently retried or
        # bypassed. It is a visible operator intervention point.
        run.state = FlowRunState.WAITING_HUMAN
        _event(
            db,
            run.id,
            "AUTOMATIC_GATE_REVIEW_REQUIRED",
            {"stage": stage, "state": next_state},
            node_run.id,
            attempt.id,
        )
        return
    if next_state in {
        AttemptState.WAITING_START_CONFIRMATION,
        AttemptState.WAITING_ACCEPTANCE,
    }:
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
    policies = [
        x for x in attempt.gate_policies_json if x["stage"] == stage and x.get("enabled", True)
    ]
    if stage == "END":
        # Output acceptance is always reviewed before author-configured end
        # gates.  The latter answer additional policy questions; they cannot
        # substitute for the platform's output-contract decision.
        policies.append(_platform_output_review_policy(db, attempt))
    policies.sort(key=lambda x: int(x["position"]))
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
        frozen_policy = dict(policy)
        prepared.append(
            _PreparedGate(
                policy=frozen_policy,
                execution_no=execution_no,
                plan=_prepare_gate_plan(
                    db,
                    attempt=attempt,
                    node_run=node_run,
                    policy=frozen_policy,
                    context=context,
                    execution_no=execution_no,
                ),
            )
        )
    return context, prepared


def _prepare_gate_plan(
    db: Session,
    *,
    attempt: NodeAttempt,
    node_run: NodeRun,
    policy: dict[str, Any],
    context: dict[str, Any],
    execution_no: int,
) -> GateExecutionPlan:
    """Prepare a gate without ever attaching it to the primary Agent.

    Every gate receives a separate native Conversation and a model-only,
    independently frozen preset.  It never receives primary-Agent
    capabilities or node context.
    """

    config = dict(policy.get("config") or {})
    timeout = int(policy.get("timeout_seconds", 30))
    preset = policy.get("agent_preset")
    if not isinstance(preset, dict):
        return GateExecutionPlan(
            str(policy["gate_type"]),
            config,
            timeout,
            preparation_error=GateResult(
                "ERROR",
                "Gate Agent configuration is required",
                ["Gate Agent configuration is required"],
                [],
                {},
                error_code="GATE_CONFIG_INVALID",
            ),
        )
    typed_preset = cast(dict[str, object], preset)

    run = _run(db, node_run.flow_run_id)
    snapshot = _snapshot(db, attempt.snapshot_id)
    if (
        not run.environment_version_id
        or snapshot.environment_version_id != run.environment_version_id
    ):
        return GateExecutionPlan(
            str(policy["gate_type"]),
            config,
            timeout,
            preparation_error=GateResult(
                "ERROR",
                "Gate Runtime Environment is unavailable",
                ["Gate Runtime Environment is unavailable"],
                [],
                {},
                error_code="GATE_CONFIG_INVALID",
            ),
        )
    environment = lock_referenceable_version(db, run.environment_version_id)
    if environment is None:
        return GateExecutionPlan(
            str(policy["gate_type"]),
            config,
            timeout,
            preparation_error=GateResult(
                "ERROR",
                "Gate Runtime Environment is unavailable",
                ["Gate Runtime Environment is unavailable"],
                [],
                {},
                error_code="GATE_CONFIG_INVALID",
            ),
        )
    try:
        connection = _flow_run_sidecar_connection(db, run.id)
        if config.get("system_owned") is True:
            # The mandatory review must use exactly the model frozen on the
            # execution Conversation.  In particular it must not fall back to
            # a mutable workspace default if that binding is malformed.
            session_config = replace(
                agent_sessions.config_from_binding(
                    db, agent_sessions.flow_node_binding_for_attempt(db, attempt.id)
                ),
                capabilities=(),
            )
        else:
            session_config = agent_sessions.resolve_session_config(
                db,
                model_provider_id=(
                    str(typed_preset["model_provider_id"])
                    if typed_preset.get("model_provider_id")
                    else None
                ),
                model_name=(
                    str(typed_preset["model_name"]) if typed_preset.get("model_name") else None
                ),
                reasoning_effort=(
                    str(typed_preset["reasoning_effort"])
                    if typed_preset.get("reasoning_effort")
                    else None
                ),
                # A gate never inherits the primary Agent's skills, plugins or
                # context. Its only inputs are the explicit gate payload below.
                capability_version_ids=(),
            )
        binding = agent_sessions.reserve_flow_node_binding(
            db,
            runtime_session_id=connection.runtime_session_id,
            flow_run_id=run.id,
            node_run_id=node_run.id,
            node_attempt_id=attempt.id,
            working_directory="/runtime/workspace/project",
            create_idempotency_key=(f"gate-sidecar:{attempt.id}:{policy['id']}:{execution_no}"),
            display_title=f"门禁 {policy['id']} · 第 {execution_no} 次",
            config=session_config,
        )
        provider = agent_sessions.provider_for_config(db, session_config)
        runtime_owner_id = sandboxes.runtime_owner_flow_run_id(db, run.id)
        host_root = sandboxes.flow_run_capability_path(
            runtime_owner_id, snapshot.runtime_manifest_hash, "gate-sidecars", binding.id
        )
        runtime_root = Path(
            sandboxes.openhands_flow_run_capability_path(
                snapshot.runtime_manifest_hash, "gate-sidecars", binding.id
            )
        )
        agent_spec = agent_sessions.build_agent_spec(
            session_config,
            provider=provider,
            binding_id=binding.id,
            working_directory=binding.working_directory or "/runtime/workspace/project",
            host_root=host_root,
            runtime_root=runtime_root,
        )
        request = build_runtime_request(
            db,
            flow_run_id=runtime_owner_id,
            runtime_manifest_hash=snapshot.runtime_manifest_hash,
            attempt_id=binding.id,
            execution_key=f"gate-sidecar:{attempt.id}:{policy['id']}:{execution_no}",
            node={},
            bindings=[],
            workspace_ref=binding.working_directory or "/runtime/workspace/project",
            interaction_mode="COLLABORATION",
            environment_image=environment.image_digest,
            environment_id=environment.environment_id,
            environment_version_id=environment.id,
            environment_version_no=environment.version_no,
            agent_spec=agent_spec,
            conversation_id=binding.openhands_conversation_id,
        )
        request = replace(
            request,
            runtime_sandbox_id=connection.managed_runtime_id,
            runtime_resource_name=connection.resource_name,
            runtime_base_url=f"http://{connection.resource_name}:8000",
        )
    except Exception as exc:
        return GateExecutionPlan(
            str(policy["gate_type"]),
            config,
            timeout,
            preparation_error=GateResult(
                "ERROR",
                "Gate Agent configuration is unavailable",
                ["Gate Agent configuration is unavailable"],
                [],
                {},
                log_excerpt=str(exc)[:4000],
                error_code="GATE_CONFIG_INVALID",
            ),
        )

    instructions = str(config.get("prompt") or "").strip()
    code = str(config.get("code") or "").strip()
    question = (
        "You are an isolated workflow gate Agent. Do not access any other "
        "Conversation history. Evaluate only the supplied gate context. Return "
        "only a JSON object with decision (PASS, FAIL, or ERROR), summary, "
        "reasons (array), evidence (array), and details (object).\n\n"
        f"Gate instructions:\n{instructions or '(No additional prose instructions.)'}\n\n"
        + (
            f"Optional Python to inspect or execute safely as part of your analysis:\n{code}\n\n"
            if code
            else ""
        )
        + "Gate context:\n"
        + json.dumps(context, ensure_ascii=False)
    )
    return GateExecutionPlan(
        str(policy["gate_type"]),
        config,
        timeout,
        sidecar_request=request,
        sidecar_question=question,
        sidecar_binding_id=binding.id,
    )


def _execute_gate_stage(
    db: Session, context: dict[str, Any], prepared: list[_PreparedGate]
) -> tuple[list[tuple[_PreparedGate, GateResult]], bool]:
    evaluations: list[tuple[_PreparedGate, GateResult]] = []
    blocked = False
    for position, item in enumerate(prepared):
        try:
            result = execute_gate_plan(item.plan, context)
        finally:
            if item.plan.sidecar_binding_id:
                agent_sessions.delete_binding_records(db, item.plan.sidecar_binding_id)
        evaluations.append((item, result))
        if result.decision != "PASS":
            blocked = True
            # All sidecar bindings are reserved before external I/O so their
            # frozen configuration survives a worker failure.  A short-circuit
            # must nevertheless clean reservations for policies never run.
            for remaining in prepared[position + 1 :]:
                if remaining.plan.sidecar_binding_id:
                    agent_sessions.delete_binding_records(db, remaining.plan.sidecar_binding_id)
            break
    return evaluations, blocked


def _run_gates_inline(db: Session, attempt: NodeAttempt, stage: str) -> None:
    context, prepared = _prepare_gate_stage(db, attempt, stage)
    evaluations, blocked = _execute_gate_stage(db, context, prepared)
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

    # The sidecar binding and its frozen configuration are durable before the
    # isolated Agent performs external I/O.  A gate may be inspected even if a
    # worker dies while the Runtime call is in flight.
    finish(db)
    evaluations, blocked = _execute_gate_stage(db, context, prepared)
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


def _dispatch_automatic_start(db: Session, attempt: NodeAttempt) -> None:
    # Automatic orchestration always crosses a durable task boundary. Unlike
    # manual inline tests, recursively starting a newly created Attempt inside
    # the draft-start transaction can observe an uncommitted work item. It also
    # would make recovery after a worker restart impossible to audit.
    enqueue(
        db,
        task_type="START_AUTOMATIC_ATTEMPT",
        aggregate_type="ATTEMPT",
        aggregate_id=attempt.id,
        idempotency_key=_task_key("START_AUTOMATIC_ATTEMPT", attempt),
    )


def _dispatch_automatic_advance(db: Session, attempt: NodeAttempt) -> None:
    # Keep platform acceptance/routing durable and replayable for the same
    # reason as automatic starts.
    enqueue(
        db,
        task_type="ADVANCE_AUTOMATIC_ATTEMPT",
        aggregate_type="ATTEMPT",
        aggregate_id=attempt.id,
        idempotency_key=_task_key("ADVANCE_AUTOMATIC_ATTEMPT", attempt),
    )


def process_start_automatic_run(db: Session, run_id: str, *, commit: bool = True) -> None:
    """Materialize one frozen automatic plan through a durable Worker task.

    The HTTP command only freezes the plan. This transition owns every
    scheduling side effect and is safe to replay after a Worker restart.
    """

    run = _locked_run(db, run_id)
    plan = dict(run.automation_plan_json or {})
    if (
        run.run_mode != "AUTOMATIC"
        or run.state != FlowRunState.ACTIVE
        or plan.get("status") != "FROZEN"
    ):
        return
    existing = db.scalar(select(NodeRun.id).where(NodeRun.flow_run_id == run.id).limit(1))
    if existing is not None:
        return
    if run.environment_version_id is None:
        raise DomainError(
            "RUN_ENVIRONMENT_REQUIRED", "automatic run has no Environment Version", 409
        )
    environment = lock_referenceable_version(db, run.environment_version_id)
    if environment is None:
        raise DomainError(
            "RUN_ENVIRONMENT_VERSION_INVALID",
            "automatic run Environment Version is unavailable",
            409,
        )
    snapshot = _active_snapshot(db, run)
    runtime_owner_id = sandboxes.runtime_owner_flow_run_id(db, run.id)
    sandboxes.allocate_flow_run_runtime(db, runtime_owner_id)
    allocation = sandboxes.runtime_allocation_for_flow_run(
        db, runtime_owner_id, manifest_digest=snapshot.runtime_manifest_hash
    )
    sandboxes.ensure_flow_run_runtime_session(
        db,
        flow_run_id=runtime_owner_id,
        environment_version_id=environment.id,
        runtime_image_digest=environment.image_digest,
        workspace_allocation=allocation,
    )
    if get_settings().runtime_adapter != "mock" and runtime_owner_id == run.id:
        task = enqueue(
            db,
            task_type="PROVISION_FLOW_RUN_RUNTIME",
            aggregate_type="FLOW_RUN",
            aggregate_id=run.id,
            idempotency_key=f"provision-flow-run-runtime:{run.id}",
        )
        task.max_attempts = max(task.max_attempts, 20)
    start_node_key = str(plan.get("start_node_key") or "")
    node_plans = cast(dict[str, Any], plan.get("node_plans") or {})
    first_plan = _automatic_node_plan(node_plans, start_node_key)
    artifact_ids = _automatic_plan_artifacts(db, run, start_node_key, first_plan)
    node_run, attempt = _create_node_run(
        db,
        run,
        start_node_key,
        artifact_ids,
        "AUTOMATIC_START",
        cast(list[dict[str, Any]], first_plan.get("gates") or []),
        context_ids=[],
        agent_preset=cast(dict[str, Any], first_plan.get("agent_preset") or {}),
    )
    _event(
        db,
        run.id,
        "AUTOMATIC_RUN_STARTED",
        {"start_node_key": start_node_key},
        node_run.id,
        attempt.id,
    )
    _finish_transaction(db, commit)


def process_start_automatic_attempt(db: Session, attempt_id: str, *, commit: bool = True) -> None:
    """Start a ready automatic Attempt from its immutable node plan.

    This durable worker transition deliberately delegates the Runtime start to
    the normal Attempt state machine.  Automatic mode therefore has the same
    Conversation, Runtime replacement and output-contract guarantees as a
    manual node execution, without an HTTP caller confirming each node.
    """

    attempt = _attempt(db, attempt_id)
    node_run = _node_run(db, attempt.node_run_id)
    run = _run(db, node_run.flow_run_id)
    if run.run_mode != "AUTOMATIC" or attempt.state != AttemptState.WAITING_START_CONFIRMATION:
        return
    plan_root: dict[str, Any] = dict(run.automation_plan_json or {})
    node_plans = cast(dict[str, Any], plan_root.get("node_plans") or {})
    node_plan = _automatic_node_plan(node_plans, node_run.flow_node_snapshot_key)
    confirm_start(
        db,
        attempt.id,
        AttemptStartWrite(
            expected_state_version=attempt.state_version,
            startup_mode="PROMPT",
            prompt=str(node_plan.get("startup_prompt") or ""),
        ),
        f"automatic-start:{attempt.id}:v{attempt.state_version}",
    )
    _event(
        db,
        run.id,
        "AUTOMATIC_ATTEMPT_STARTED",
        {"flow_node_key": node_run.flow_node_snapshot_key},
        node_run.id,
        attempt.id,
    )
    _finish_transaction(db, commit)


def process_advance_automatic_attempt(
    db: Session,
    attempt_id: str,
    lease: Lease | None = None,
    *,
    commit: bool = True,
) -> None:
    """Accept one automatic node and apply a governed transition decision.

    A distinct, capability-free Agent sees only the frozen successor action
    package. External I/O runs outside a database transaction. The returned
    node keys are advisory until the platform validates the task lease, the
    Attempt CAS version, and the immutable Snapshot topology.
    """

    attempt = _attempt(db, attempt_id)
    node_run = _node_run(db, attempt.node_run_id)
    run = _run(db, node_run.flow_run_id)
    if run.run_mode != "AUTOMATIC" or attempt.state != AttemptState.WAITING_ACCEPTANCE:
        return
    expected_version = attempt.state_version
    allowed = _automatic_successor_keys(db, run, node_run)
    prepared: GateExecutionPlan | None = None
    if allowed:
        prepared = _prepare_automatic_transition_plan(db, run, node_run, attempt, allowed)
        # Persist the isolated Conversation locator/configuration before slow
        # Runtime I/O. A retry reuses the same idempotent binding.
        db.commit()
        result = execute_gate_plan(prepared, {})
        _require_current_lease(db, lease)
        selected, error = _automatic_transition_selection(result, allowed)
    else:
        selected, error = [], None

    claimed_id = db.scalar(
        update(NodeAttempt)
        .where(
            NodeAttempt.id == attempt_id,
            NodeAttempt.state == AttemptState.WAITING_ACCEPTANCE,
            NodeAttempt.state_version == expected_version,
        )
        .values(
            state=AttemptState.END_BLOCKED if error else AttemptState.ACCEPTED,
            state_version=NodeAttempt.state_version + 1,
            error_code="AUTOMATIC_TRANSITION_INVALID" if error else None,
            error_detail=error,
        )
        .returning(NodeAttempt.id)
        .execution_options(synchronize_session=False)
    )
    if claimed_id is None:
        db.expire_all()
        return
    db.expire_all()
    attempt = _attempt(db, attempt_id)
    node_run = _node_run(db, attempt.node_run_id)
    run = _locked_run(db, node_run.flow_run_id)
    if prepared is not None and prepared.sidecar_binding_id:
        agent_sessions.delete_binding_records(db, prepared.sidecar_binding_id)
    if error:
        run.state = FlowRunState.WAITING_HUMAN
        _event(
            db,
            run.id,
            "AUTOMATIC_TRANSITION_REVIEW_REQUIRED",
            {"error": error, "allowed_node_keys": allowed},
            node_run.id,
            attempt.id,
        )
        _finish_transaction(db, commit)
        return
    node_run.state = NodeRunState.ACCEPTED
    node_run.accepted_attempt_id = attempt.id
    _action(
        db,
        run.id,
        "ADVANCE_AUTOMATIC_ATTEMPT",
        f"automatic-advance:{attempt.id}:v{expected_version}",
        node_run_id=node_run.id,
        attempt_id=attempt.id,
        payload={"selected_node_keys": selected},
    )
    _event(
        db,
        run.id,
        "AUTOMATIC_NODE_ACCEPTED",
        {"selected_node_keys": selected},
        node_run.id,
        attempt.id,
    )
    _advance_automatic_targets(db, run, node_run, selected)
    _recompute_run(db, run)
    _finish_transaction(db, commit)


def _automatic_successor_keys(db: Session, run: FlowRun, node_run: NodeRun) -> list[str]:
    definition = _active_snapshot(db, run).definition_json
    return sorted(
        {
            str(edge["target_instance_key"])
            for edge in definition.get("edges", [])
            if edge.get("source_instance_key") == node_run.flow_node_snapshot_key
        }
    )


def _prepare_automatic_transition_plan(
    db: Session,
    run: FlowRun,
    node_run: NodeRun,
    attempt: NodeAttempt,
    allowed: list[str],
) -> GateExecutionPlan:
    plan = dict(run.automation_plan_json or {})
    node_plans = cast(dict[str, Any], plan.get("node_plans") or {})
    source_plan = _automatic_node_plan(node_plans, node_run.flow_node_snapshot_key)
    preset = dict(cast(dict[str, Any], source_plan.get("agent_preset") or {}))
    context = {
        "schema_version": 1,
        "source_node_key": node_run.flow_node_snapshot_key,
        "allowed_node_keys": allowed,
        "allowed_actions": [{"action": "SELECT_SUCCESSOR", "node_key": key} for key in allowed],
        "outputs": [
            _gate_artifact(item)
            for item in db.scalars(
                select(ArtifactVersion)
                .where(ArtifactVersion.producer_attempt_id == attempt.id)
                .order_by(ArtifactVersion.field_key, ArtifactVersion.version_no)
            )
        ],
        "port_mappings": [
            mapping
            for mapping in _active_snapshot(db, run).definition_json.get("port_mappings", [])
            if mapping.get("source_instance_key") == node_run.flow_node_snapshot_key
            and mapping.get("target_instance_key") in allowed
        ],
    }
    policy = {
        "id": "automatic-transition",
        "gate_type": "PROMPT",
        "position": 0,
        "timeout_seconds": 60,
        "agent_preset": preset,
        "config": {
            "prompt": (
                "You are an isolated workflow transition Agent. Select one or more "
                "successors only from allowed_node_keys. Return PASS with "
                "details.selected_node_keys as a non-empty JSON array. Do not invent "
                "nodes and do not perform platform writes."
            )
        },
    }
    return _prepare_gate_plan(
        db,
        attempt=attempt,
        node_run=node_run,
        policy=policy,
        context=context,
        execution_no=attempt.state_version,
    )


def _automatic_transition_selection(
    result: GateResult, allowed: list[str]
) -> tuple[list[str], str | None]:
    raw = result.details.get("selected_node_keys")
    if result.decision != "PASS":
        return [], result.summary or "流转 Agent 未形成通过决定"
    if not isinstance(raw, list) or not raw:
        return [], "流转 Agent 未选择任何冻结后继节点"
    raw_items = cast(list[object], raw)
    if any(not isinstance(item, str) or not item for item in raw_items):
        return [], "流转 Agent 返回了无效节点标识"
    selected = [str(item) for item in raw_items]
    if len(selected) != len(set(selected)):
        return [], "流转 Agent 重复选择了同一节点"
    unauthorized = sorted(set(selected) - set(allowed))
    if unauthorized:
        return [], f"流转 Agent 选择了未授权节点：{', '.join(unauthorized)}"
    return sorted(selected), None


def _create_node_run(
    db: Session,
    run: FlowRun,
    instance_key: str,
    artifact_ids: dict[str, str],
    created_from: str,
    gate_policies: list[dict[str, Any]] | None = None,
    *,
    session_only: bool = False,
    context_ids: list[str] | None = None,
    agent_preset: dict[str, Any] | None = None,
) -> tuple[NodeRun, NodeAttempt]:
    snapshot = _active_snapshot(db, run)
    node = _node(snapshot, instance_key)
    asset = cast(dict[str, Any], node.get("asset") or {})
    asset_id = str(asset.get("id") or "")
    if not asset_id:
        raise DomainError("SNAPSHOT_INVALID", "node asset id is missing", 409)
    _validate_input_bindings(db, run, node, artifact_ids)
    # A manual FlowRun owns at most one non-cancelled NodeRun for each frozen
    # graph node. Cancelled records remain independent history and do not block
    # a fresh run from the neutral Workbench. Downstream transitions create a
    # WAITING_INPUT work item; revisions after rejection remain Attempts.
    existing = db.scalar(
        select(NodeRun).where(
            NodeRun.flow_run_id == run.id,
            NodeRun.flow_node_snapshot_key == instance_key,
            NodeRun.state != NodeRunState.CANCELLED,
        )
    )
    if existing:
        latest = db.scalar(
            select(NodeAttempt)
            .where(NodeAttempt.node_run_id == existing.id)
            .order_by(NodeAttempt.attempt_no.desc())
        )
        if latest is None:
            raise DomainError("RUN_STATE_INVALID", "node work item has no attempt", 409)
        if latest.state != AttemptState.WAITING_INPUT or existing.created_from != "FLOW_TRANSITION":
            raise illegal("node already exists in this manual run group", state=latest.state)
        if session_only:
            latest.startup_mode = "CHAT"
            latest.context_ids_json = []
            latest.agent_preset_json = agent_preset
            latest.gate_policies_json = gate_policies or []
            latest.state = AttemptState.WAITING_START_CONFIRMATION
            latest.state_version += 1
            run.state = FlowRunState.WAITING_HUMAN
            _event(
                db,
                run.id,
                "ATTEMPT_SESSION_READY",
                {"flow_node_key": instance_key},
                existing.id,
                latest.id,
            )
            return existing, latest
        current_bindings = {row.input_field_key: row for row in _bindings(db, latest.id)}
        for field_key, artifact_id in artifact_ids.items():
            binding = current_bindings.get(field_key)
            if binding:
                if (
                    binding.binding_source == "PORT_MAPPING"
                    and binding.artifact_version_id != artifact_id
                ):
                    raise DomainError(
                        "INPUT_BINDING_IMMUTABLE",
                        "a frozen port mapping cannot be replaced by manual input",
                        409,
                        {"field": field_key},
                    )
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
        latest.context_ids_json = context_ids
        latest.agent_preset_json = agent_preset
        latest.gate_policies_json = gate_policies or []
        latest.state_version += 1
        latest.error_code = None
        latest.error_detail = None
        db.flush()
        _dispatch_readiness(db, latest)
        _event(
            db,
            run.id,
            "REACHED_NODE_CONFIGURED",
            {"fields": sorted(artifact_ids), "flow_node_key": instance_key},
            existing.id,
            latest.id,
        )
        return existing, latest
    runtime_owner_id = sandboxes.runtime_owner_flow_run_id(db, run.id)
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
        state=(
            AttemptState.WAITING_START_CONFIRMATION if session_only else AttemptState.WAITING_INPUT
        ),
        startup_mode="CHAT" if session_only else "PROMPT",
        context_ids_json=context_ids,
        agent_preset_json=agent_preset,
        gate_policies_json=gate_policies or [],
        workspace_ref=str(
            attempt_workspace_path(
                asset_id=asset_id,
                run_id=runtime_owner_id,
                node_run_id=node_run.id,
                attempt_no=1,
            )
        ),
    )
    db.add(attempt)
    db.flush()
    ensure_flow_run_attempt_workspace(
        flow_run_id=runtime_owner_id,
        asset_id=asset_id,
        workspace_ref=attempt.workspace_ref or "",
    )
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
    if session_only:
        run.state = FlowRunState.WAITING_HUMAN
        _event(
            db,
            run.id,
            "ATTEMPT_SESSION_READY",
            {"flow_node_key": instance_key},
            node_run.id,
            attempt.id,
        )
    else:
        _dispatch_readiness(db, attempt)
    return node_run, attempt


def start_flow(
    db: Session,
    flow_id: str,
    payload: RunStart,
) -> dict[str, Any]:
    flow = load_flow(db, flow_id)
    environment = lock_referenceable_version(db, payload.environment_version_id)
    if environment is None:
        raise DomainError(
            "RUN_ENVIRONMENT_VERSION_INVALID",
            "The selected FlowRun Environment Version is not READY",
            422,
            {"environment_version_id": payload.environment_version_id},
        )
    validate_runtime_manifest(environment.manifest_json, environment_version_id=environment.id)
    definition = _snapshot_definition(db, flow_id, environment_version_id=environment.id)
    run_no = (
        db.scalar(select(func.max(FlowRun.run_no)).where(FlowRun.flow_definition_id == flow_id))
        or 0
    ) + 1
    run_name = payload.name or f"{flow.name} · Run #{run_no}"
    run = FlowRun(
        flow_definition_id=flow_id,
        run_no=run_no,
        name=run_name,
        run_mode="MANUAL",
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
    if get_settings().runtime_adapter != "mock":
        task = enqueue(
            db,
            task_type="PROVISION_FLOW_RUN_RUNTIME",
            aggregate_type="FLOW_RUN",
            aggregate_id=run.id,
            idempotency_key=f"provision-flow-run-runtime:{run.id}",
        )
        task.max_attempts = max(task.max_attempts, 20)
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
            artifact = _register_artifact(
                db,
                run.id,
                prepared,
                source="HUMAN_INPUT",
                consumer_node_key=payload.flow_node_key,
            )
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


def create_automatic_run_draft(
    db: Session,
    flow_id: str,
    payload: AutomaticRunDraftWrite,
    *,
    parent_flow_run_id: str | None = None,
) -> dict[str, Any]:
    flow = load_flow(db, flow_id)
    environment = lock_referenceable_version(db, payload.environment_version_id)
    if environment is None:
        raise DomainError(
            "RUN_ENVIRONMENT_VERSION_INVALID",
            "The selected automatic-run Environment Version is not READY",
            422,
            {"environment_version_id": payload.environment_version_id},
        )
    validate_runtime_manifest(environment.manifest_json, environment_version_id=environment.id)
    definition = _snapshot_definition(db, flow_id, environment_version_id=environment.id)
    run_no = (
        db.scalar(select(func.max(FlowRun.run_no)).where(FlowRun.flow_definition_id == flow_id))
        or 0
    ) + 1
    run = FlowRun(
        flow_definition_id=flow_id,
        run_no=run_no,
        name=(payload.name or "").strip() or f"{flow.name} · 自动运行 #{run_no}",
        run_mode="AUTOMATIC",
        state=FlowRunState.DRAFT,
        environment_version_id=environment.id,
        parent_flow_run_id=parent_flow_run_id,
    )
    db.add(run)
    db.flush()
    run.automation_plan_json = _freeze_automatic_plan(
        db,
        run,
        definition,
        start_node_key=payload.start_node_key,
        node_plans=payload.node_plans,
    )
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
    hold_snapshot_memory_references(db, snapshot_id=snapshot.id, runtime_manifest=runtime_manifest)
    run.active_snapshot_id = snapshot.id
    _event(
        db,
        run.id,
        "AUTOMATIC_RUN_DRAFT_CREATED",
        {
            "snapshot_version": 1,
            "environment_version_id": environment.id,
            "start_node_key": payload.start_node_key,
        },
    )
    finish(db)
    return run_detail(db, run.id)


def create_nested_automatic_run_draft(
    db: Session, parent_run_id: str, payload: AutomaticRunDraftWrite
) -> dict[str, Any]:
    parent = _run(db, parent_run_id)
    if parent.run_mode != "MANUAL":
        raise illegal("automatic records require a standard parent FlowRun", state=parent.state)
    if parent.environment_version_id != payload.environment_version_id:
        raise DomainError(
            "RUN_ENVIRONMENT_MISMATCH",
            "Automatic records must use their parent FlowRun Environment Version",
            422,
        )
    return create_automatic_run_draft(
        db, parent.flow_definition_id, payload, parent_flow_run_id=parent.id
    )


def list_nested_automatic_runs(db: Session, parent_run_id: str) -> list[dict[str, Any]]:
    parent = _run(db, parent_run_id)
    children = list(
        db.scalars(
            select(FlowRun)
            .where(
                FlowRun.parent_flow_run_id == parent.id,
                FlowRun.run_mode == "AUTOMATIC",
            )
            .order_by(FlowRun.started_at.desc())
        )
    )
    return [run_detail(db, child.id) for child in children]


def nested_automatic_run(db: Session, parent_run_id: str, run_id: str) -> FlowRun:
    child = _run(db, run_id)
    if child.parent_flow_run_id != parent_run_id or child.run_mode != "AUTOMATIC":
        raise not_found("automatic_run", run_id)
    return child


def update_automatic_run_draft(
    db: Session, run_id: str, payload: AutomaticRunDraftUpdateWrite
) -> dict[str, Any]:
    run = _run(db, run_id)
    if run.run_mode != "AUTOMATIC" or run.state != FlowRunState.DRAFT:
        raise illegal("only an automatic run draft can be edited", state=run.state)
    if run.row_version != payload.expected_row_version:
        raise conflict(
            "automatic run draft was modified",
            expected=payload.expected_row_version,
            actual=run.row_version,
        )
    snapshot = _active_snapshot(db, run)
    run.automation_plan_json = _freeze_automatic_plan(
        db,
        run,
        snapshot.definition_json,
        start_node_key=payload.start_node_key,
        node_plans=payload.node_plans,
    )
    if payload.name is not None:
        run.name = payload.name.strip() or run.name
    run.row_version += 1
    _event(
        db,
        run.id,
        "AUTOMATIC_RUN_DRAFT_UPDATED",
        {"start_node_key": payload.start_node_key, "row_version": run.row_version},
    )
    finish(db)
    return run_detail(db, run.id)


def copy_automatic_run_draft(
    db: Session, source_run_id: str, payload: AutomaticRunCopyWrite
) -> dict[str, Any]:
    """Create an independent editable draft from any frozen automatic plan.

    The new FlowRun deliberately reuses the immutable Flow snapshot, but it
    never retains an ArtifactVersion identifier from the source FlowRun.
    This keeps input ownership, deletion protection, and future edits scoped
    to the copied plan.
    """

    source = _run(db, source_run_id)
    if source.run_mode != "AUTOMATIC":
        raise illegal("only an automatic run can be copied", state=source.state)
    if source.environment_version_id is None:
        raise DomainError(
            "RUN_ENVIRONMENT_REQUIRED", "automatic run has no Environment Version", 409
        )
    source_snapshot = _active_snapshot(db, source)
    source_plan = copy.deepcopy(dict(source.automation_plan_json or {}))
    if not source_plan.get("start_node_key"):
        raise DomainError("AUTOMATION_PLAN_INVALID", "automatic plan is missing a start node", 409)
    run_no = (
        db.scalar(
            select(func.max(FlowRun.run_no)).where(
                FlowRun.flow_definition_id == source.flow_definition_id
            )
        )
        or 0
    ) + 1
    copied = FlowRun(
        flow_definition_id=source.flow_definition_id,
        run_no=run_no,
        name=(payload.name or "").strip() or f"{source.name} · 副本 #{run_no}",
        run_mode="AUTOMATIC",
        state=FlowRunState.DRAFT,
        environment_version_id=source.environment_version_id,
    )
    db.add(copied)
    db.flush()
    snapshot = RunSnapshot(
        flow_run_id=copied.id,
        version=1,
        schema_version=source_snapshot.schema_version,
        definition_json=copy.deepcopy(source_snapshot.definition_json),
        definition_hash=source_snapshot.definition_hash,
        runtime_manifest_json=copy.deepcopy(source_snapshot.runtime_manifest_json),
        runtime_manifest_hash=source_snapshot.runtime_manifest_hash,
        environment_version_id=source.environment_version_id,
    )
    db.add(snapshot)
    db.flush()
    copied.active_snapshot_id = snapshot.id
    node_plans = cast(dict[str, Any], source_plan.get("node_plans") or {})
    for node_key in node_plans:
        plan = _automatic_node_plan(node_plans, str(node_key))
        raw_ids = cast(dict[str, Any], plan.get("artifact_ids") or {})
        copied_ids: dict[str, str] = {}
        for field_key, artifact_id in raw_ids.items():
            copied_ids[str(field_key)] = _copy_automatic_plan_artifact(
                db, source, copied, str(node_key), str(artifact_id)
            )
        plan["artifact_ids"] = copied_ids
        node_plans[str(node_key)] = plan
    source_plan["node_plans"] = node_plans
    source_plan["status"] = "DRAFT"
    copied.automation_plan_json = source_plan
    hold_snapshot_memory_references(
        db, snapshot_id=snapshot.id, runtime_manifest=snapshot.runtime_manifest_json
    )
    _event(
        db,
        copied.id,
        "AUTOMATIC_RUN_DRAFT_COPIED",
        {"source_run_id": source.id, "snapshot_version": snapshot.version},
    )
    finish(db)
    return run_detail(db, copied.id)


def _copy_automatic_plan_artifact(
    db: Session, source_run: FlowRun, copied_run: FlowRun, node_key: str, artifact_id: str
) -> str:
    artifact = db.get(ArtifactVersion, artifact_id)
    if artifact is None or artifact.flow_run_id != source_run.id:
        raise DomainError(
            "AUTOMATION_PLAN_INPUT_INVALID",
            "automatic plan references an artifact outside its source run",
            409,
            {"artifact_id": artifact_id},
        )
    metadata = dict(artifact.metadata_json or {})
    if artifact.artifact_type == "URL":
        prepared = prepare_artifact(
            ArtifactWrite(
                field_key=artifact.field_key,
                artifact_type="URL",
                uri=artifact.uri,
                metadata=metadata,
            )
        )
    else:
        if artifact.inline_content is not None:
            content = artifact.inline_content.encode("utf-8")
        elif artifact.storage_key:
            try:
                content = get_artifact_store().read(artifact.storage_key)
            except FileNotFoundError as exc:
                raise DomainError(
                    "ARTIFACT_UNAVAILABLE", "Artifact content is unavailable", 409
                ) from exc
        else:
            raise DomainError("ARTIFACT_UNAVAILABLE", "Artifact content is unavailable", 409)
        prepared = prepare_file_artifact(
            field_key=artifact.field_key,
            filename=str(metadata.get("filename") or f"{artifact.field_key}.bin"),
            mime_type=artifact.mime_type,
            content=content,
            metadata=metadata,
        )
    copied_artifact = _register_artifact(
        db,
        copied_run.id,
        prepared,
        source="AUTOMATIC_PLAN_COPY",
        consumer_node_key=node_key,
    )
    return copied_artifact.id


def start_automatic_run(
    db: Session,
    run_id: str,
    payload: AutomaticRunStartWrite,
    idempotency_key: str,
) -> dict[str, Any]:
    """Freeze a ready plan and durably request Worker scheduling."""

    run = _locked_run(db, run_id)
    existing = db.scalar(select(HumanAction).where(HumanAction.idempotency_key == idempotency_key))
    if existing is not None:
        if existing.flow_run_id != run.id or existing.action_type != "FREEZE_AUTOMATIC_RUN":
            raise conflict(
                "automatic-run idempotency key is already used",
                flow_run_id=existing.flow_run_id,
            )
        if existing.payload_json.get("expected_row_version") != payload.expected_row_version:
            raise conflict(
                "automatic-run freeze request does not match the idempotent request",
                flow_run_id=run.id,
            )
        return run_detail(db, run.id)
    if run.run_mode != "AUTOMATIC" or run.state != FlowRunState.DRAFT:
        raise illegal("only an editable automatic run draft can be frozen", state=run.state)
    if run.row_version != payload.expected_row_version:
        raise conflict(
            "automatic run draft was modified",
            expected=payload.expected_row_version,
            actual=run.row_version,
        )
    plan = copy.deepcopy(dict(run.automation_plan_json or {}))
    readiness = cast(dict[str, Any], plan.get("readiness") or {})
    if not readiness.get("ready"):
        raise DomainError(
            "AUTOMATION_PLAN_NOT_READY",
            "Automatic run cannot be frozen until every reachable node is configured",
            422,
            {"issues": readiness.get("issues", [])},
        )
    if plan.get("status") != "DRAFT":
        raise illegal("automatic run plan is already frozen", state=run.state)
    _action(
        db,
        run.id,
        "FREEZE_AUTOMATIC_RUN",
        idempotency_key,
        {"expected_row_version": payload.expected_row_version},
    )
    plan["status"] = "FROZEN"
    run.automation_plan_json = plan
    run.state = FlowRunState.ACTIVE
    run.row_version += 1
    enqueue(
        db,
        task_type="START_AUTOMATIC_RUN",
        aggregate_type="FLOW_RUN",
        aggregate_id=run.id,
        idempotency_key=f"start-automatic-run:{run.id}:v{run.row_version}",
    )
    _event(
        db,
        run.id,
        "AUTOMATIC_RUN_PLAN_FROZEN",
        {"start_node_key": plan.get("start_node_key"), "row_version": run.row_version},
    )
    finish(db)
    return run_detail(db, run.id)


def _automatic_plan_artifacts(
    db: Session, run: FlowRun, node_key: str, plan: dict[str, Any]
) -> dict[str, str]:
    """Materialize explicit automatic URL inputs as frozen Artifacts once."""

    artifact_ids = {
        str(key): str(value) for key, value in dict(plan.get("artifact_ids") or {}).items()
    }
    for field_key, uri in dict(plan.get("input_urls") or {}).items():
        prepared = prepare_artifact(
            ArtifactWrite(
                field_key=str(field_key),
                artifact_type="URL",
                uri=str(uri),
                metadata={"source": "AUTOMATIC_PLAN"},
            )
        )
        artifact = _register_artifact(
            db,
            run.id,
            prepared,
            source="AUTOMATIC_PLAN",
            consumer_node_key=node_key,
        )
        artifact_ids[str(field_key)] = artifact.id
    return artifact_ids


def _automatic_node_plan(node_plans: dict[str, Any], node_key: str) -> dict[str, Any]:
    raw_plan = node_plans.get(node_key)
    if not isinstance(raw_plan, dict):
        raise DomainError(
            "AUTOMATION_PLAN_INVALID",
            "automatic node plan is missing",
            409,
            {"node_key": node_key},
        )
    return dict(cast(dict[str, Any], raw_plan))


def process_provision_flow_run_runtime(
    db: Session,
    run_id: str,
    lease: Lease,
    *,
    commit: bool = True,
) -> None:
    """Provision a FlowRun Agent Server through the Worker controller principal."""

    run = _run(db, run_id)
    if run.state == FlowRunState.DRAFT:
        raise illegal("automatic run draft cannot provision a Runtime", state=run.state)
    if run.state in {FlowRunState.COMPLETED, FlowRunState.CANCELLED}:
        _finish_transaction(db, commit)
        return
    if not run.environment_version_id:
        raise DomainError(
            "RUN_ENVIRONMENT_REQUIRED",
            "The FlowRun must freeze an Environment Version before Runtime provisioning",
            409,
            {"flow_run_id": run.id},
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
    if not lease_is_current(db, lease):
        raise RuntimeError("task lease was lost before Runtime provisioning")
    sandboxes.ensure_flow_run_runtime(
        db,
        flow_run_id=run.id,
        image=environment.image_digest,
        environment_id=environment.environment_id,
        environment_version_id=environment.id,
        environment_version_no=environment.version_no,
    )
    if not lease_is_current(db, lease):
        raise RuntimeError("task lease was lost during Runtime provisioning")
    _finish_transaction(db, commit)


def start_node_run(
    db: Session, run_id: str, instance_key: str, payload: NodeRunStart
) -> dict[str, Any]:
    # Serializing on the FlowRun closes the application-level race between two
    # starts or two branch completions that target the same frozen node.
    run = _locked_run(db, run_id)
    if run.run_mode != "MANUAL":
        raise illegal("automatic runs cannot use the manual node start command", state=run.state)
    if run.state in {FlowRunState.COMPLETED, FlowRunState.CANCELLED}:
        raise illegal("terminal run cannot activate node", state=run.state)
    existing_node = db.scalar(
        select(NodeRun).where(
            NodeRun.flow_run_id == run.id,
            NodeRun.flow_node_snapshot_key == instance_key,
            NodeRun.state != NodeRunState.CANCELLED,
        )
    )
    has_group_work = (
        db.scalar(
            select(NodeRun.id)
            .where(
                NodeRun.flow_run_id == run.id,
                NodeRun.state != NodeRunState.CANCELLED,
            )
            .limit(1)
        )
        is not None
    )
    if has_group_work and existing_node is None:
        raise DomainError(
            "NODE_NOT_REACHED",
            "node is not available in this manual run group",
            409,
            {"flow_node_key": instance_key},
        )
    # Once every prior record is cancelled, the manual Workbench is neutral
    # again and any frozen node may start a fresh independent NodeRun.
    if payload.startup_mode == "CHAT":
        # A session-only launch is human-directed collaboration, not an
        # automated node execution. Its Attempt is only an authorization and
        # Snapshot context for the shared Agent Workbench: it must not add or
        # replace declared node inputs, run either gate stage, reserve output
        # targets, enqueue Runtime work, or trigger flow port propagation.
        # A reached node can already carry immutable PORT_MAPPING bindings;
        # those remain attached as transition audit facts but are not consumed
        # by this session-only launch.
        node = _node(_active_snapshot(db, run), instance_key)
        completion_gates: list[dict[str, Any]] = []
        for frozen_gate in cast(list[dict[str, Any]], node.get("gates") or []):
            if frozen_gate.get("stage") != "END":
                continue
            completion_gate = copy.deepcopy(frozen_gate)
            # Flow-definition gates predate the per-Attempt Gate Agent
            # contract. CHAT does not expose a gate editor, so retain the
            # frozen END policy with an explicit zero-capability/default-model
            # preset instead of inheriting the primary Conversation.
            completion_gate["agent_preset"] = dict(
                cast(dict[str, Any], completion_gate.get("agent_preset") or {})
            )
            completion_gates.append(completion_gate)
        node_run, _ = _create_node_run(
            db,
            run,
            instance_key,
            {},
            "HUMAN_CHAT",
            completion_gates,
            session_only=True,
            context_ids=[],
            agent_preset=(payload.agent_preset.model_dump() if payload.agent_preset else None),
        )
        finish(db)
        return node_run_detail(db, node_run.id)
    node = _node(_active_snapshot(db, run), instance_key)
    # Node-owned prose is selected exclusively by the launch Agent preset.
    # Repository Context is represented by its frozen capability versions.
    if payload.agent_preset is None:
        raise DomainError(
            "AGENT_PRESET_REQUIRED",
            "Prompt node execution requires an Agent configuration",
            422,
        )
    preset = payload.agent_preset.model_dump()
    node_context_prompt = preset.get("node_context_prompt")
    node_asset = cast(dict[str, Any], node.get("asset") or {})
    node_executor = cast(dict[str, Any], node_asset.get("executor") or {})
    effective_context_prompt = (
        str(node_context_prompt)
        if node_context_prompt is not None
        else str(node_executor.get("context_prompt") or "")
    )
    context_ids = (
        [_MANUAL_NODE_CONTEXT_ID]
        if preset["node_context_enabled"] and effective_context_prompt.strip()
        else []
    )
    artifact_ids = dict(payload.artifact_ids)
    if payload.input_urls:
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
            artifact = _register_artifact(
                db,
                run.id,
                prepared,
                source="HUMAN_INPUT",
                consumer_node_key=instance_key,
            )
            artifact_ids[field_key] = artifact.id
    gates = [
        {
            "id": str(uuid4()),
            "stage": gate.stage,
            "position": gate.position,
            "gate_type": gate.gate_type,
            "enabled": gate.enabled,
            "timeout_seconds": gate.timeout_seconds,
            "config": gate.config,
            "agent_preset": gate.agent_preset.model_dump(),
        }
        for gate in payload.gates
    ]
    node_run, _ = _create_node_run(
        db,
        run,
        instance_key,
        artifact_ids,
        "HUMAN_START",
        gates,
        context_ids=context_ids,
        agent_preset=preset,
    )
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
    host = agent_sessions.resolve_flow_node_session_host(
        db,
        flow_run_id=current_node_run.flow_run_id,
        attempt_id=current.id,
        require_start_permission=True,
    )
    preset = current.agent_preset_json
    session_config = agent_sessions.resolve_session_config(
        db,
        model_provider_id=(
            str(preset["model_provider_id"]) if preset and preset.get("model_provider_id") else None
        ),
        model_name=(str(preset["model_name"]) if preset and preset.get("model_name") else None),
        reasoning_effort=(
            str(preset["reasoning_effort"]) if preset and preset.get("reasoning_effort") else None
        ),
        capability_version_ids=(
            tuple(str(value) for value in preset.get("capability_version_ids", []))
            if preset is not None
            else None
        ),
    )
    if payload.startup_mode == "SKILL" and not any(
        item.capability_type == "SKILL" and item.capability_key == payload.capability_key
        for item in session_config.capabilities
    ):
        raise DomainError(
            "STARTUP_CAPABILITY_INVALID",
            "Selected Skill is not available on this node",
            422,
            {"capability_key": payload.capability_key},
        )
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
    node_run = _node_run(db, attempt.node_run_id)
    run = _run(db, node_run.flow_run_id)
    agent_sessions.reserve_flow_node_binding(
        db,
        runtime_session_id=host.runtime_session_id,
        flow_run_id=run.id,
        node_run_id=node_run.id,
        node_attempt_id=attempt.id,
        working_directory=host.session.working_directory,
        create_idempotency_key=f"attempt-runtime:{attempt.id}",
        display_title=f"运行 {attempt.id}",
        config=session_config,
    )
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
    """Freeze the node's declared output fields and their exact artifact types."""

    del db, attempt

    asset = cast(dict[str, Any], node.get("asset") or {})
    raw_outputs = cast(list[object], asset.get("outputs") or [])
    if not raw_outputs:
        return {}
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
        targets[field_key] = {
            "run_name": run.name,
            "title": title,
            "display_name": display_name,
            "description": str(output.get("description") or ""),
            "artifact_type": str(output.get("data_type") or "URL"),
        }
    return targets


def recover_runtime_tasks(db: Session) -> int:
    """Restore missing runtime work for attempts left in an executable phase.

    Attempt rows are the source of truth. Task rows only provide delivery, so a
    deleted or terminal delivery record must not strand a persisted runtime.
    Rows are locked with SKIP LOCKED so multiple workers may recover concurrently.
    """

    recovered = _recover_automatic_run_starts(db)
    recovered += _recover_automatic_attempt_tasks(db)
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
    now_utc = datetime.now(UTC)
    active_states = [TaskState.PENDING, TaskState.RETRY, TaskState.RUNNING]
    for attempt in attempts:
        task_type: str
        payload: dict[str, Any] = {}
        if attempt.runtime_phase == "STARTING":
            task_type = "START_RUNTIME"
        elif attempt.runtime_phase == "RUNNING":
            # Event wakeups are the normal driver. Recovery only reinstates the
            # long-poll subscription; it does not restore the old poll loop.
            task_type = "WAIT_RUNTIME_WAKEUP"
            payload = {"wakeup_no": 1}
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
            previous_payload: dict[str, Any] = (
                latest_cancel_task.payload_json if latest_cancel_task is not None else {}
            )
            frozen_sandbox_ids = previous_payload.get("sandbox_ids")
            sandbox_ids: set[str] = (
                {
                    str(item)
                    for item in cast(list[object], frozen_sandbox_ids)
                    if isinstance(item, str) and item
                }
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


def _recover_automatic_attempt_tasks(db: Session) -> int:
    """Restore orchestration deliveries from durable automatic Attempt state.

    A WAITING_INPUT row is recoverable only before readiness has recorded a
    result. INPUTS_MISSING/INPUTS_INCOMPATIBLE are stable operator-visible
    outcomes and must not generate a task on every maintenance sweep.
    """

    attempts = list(
        db.scalars(
            select(NodeAttempt)
            .join(NodeRun, NodeRun.id == NodeAttempt.node_run_id)
            .join(FlowRun, FlowRun.id == NodeRun.flow_run_id)
            .where(
                FlowRun.run_mode == "AUTOMATIC",
                FlowRun.state.in_([FlowRunState.ACTIVE, FlowRunState.WAITING_HUMAN]),
                NodeRun.state == NodeRunState.ACTIVE,
                NodeAttempt.state.in_(
                    [
                        AttemptState.WAITING_INPUT,
                        AttemptState.START_GATES,
                        AttemptState.WAITING_START_CONFIRMATION,
                        AttemptState.END_GATES,
                        AttemptState.WAITING_ACCEPTANCE,
                    ]
                ),
            )
            .order_by(NodeAttempt.updated_at, NodeAttempt.id)
            .with_for_update(skip_locked=True)
        )
    )
    active_states = [TaskState.PENDING, TaskState.RETRY, TaskState.RUNNING]
    now_utc = datetime.now(UTC)
    recovered = 0
    for attempt in attempts:
        task_type: str
        payload: dict[str, Any] = {}
        suffix = ""
        if attempt.state == AttemptState.WAITING_INPUT:
            if attempt.error_code is not None:
                continue
            task_type = "EVALUATE_READINESS"
        elif attempt.state == AttemptState.START_GATES:
            task_type = "RUN_GATE_POLICY"
            payload = {"stage": "START"}
            suffix = ":start"
        elif attempt.state == AttemptState.WAITING_START_CONFIRMATION:
            task_type = "START_AUTOMATIC_ATTEMPT"
        elif attempt.state == AttemptState.END_GATES:
            task_type = "RUN_GATE_POLICY"
            payload = {"stage": "END"}
            suffix = ":end"
        else:
            task_type = "ADVANCE_AUTOMATIC_ATTEMPT"

        active = db.scalar(
            select(BackgroundTask.id).where(
                BackgroundTask.aggregate_id == attempt.id,
                BackgroundTask.task_type == task_type,
                BackgroundTask.state.in_(active_states),
            )
        )
        if active is not None:
            continue
        key = f"recovery:{task_type.lower()}:{attempt.id}:v{attempt.state_version}{suffix}"
        existing = db.scalar(select(BackgroundTask).where(BackgroundTask.idempotency_key == key))
        if existing is None:
            enqueue(
                db,
                task_type=task_type,
                aggregate_type="ATTEMPT",
                aggregate_id=attempt.id,
                idempotency_key=key,
                payload=payload,
                available_at=now_utc,
            )
        else:
            existing.state = TaskState.RETRY
            existing.available_at = now_utc
            existing.lease_owner = None
            existing.lease_until = None
            existing.last_error = "STARTUP_RECOVERY"
            existing.payload_json = payload
        recovered += 1
    return recovered


def _recover_automatic_run_starts(db: Session) -> int:
    """Restore the Worker handoff for frozen plans with no materialized work."""

    active_states = [TaskState.PENDING, TaskState.RETRY, TaskState.RUNNING]
    recovered = 0
    runs = list(
        db.scalars(
            select(FlowRun)
            .where(
                FlowRun.run_mode == "AUTOMATIC",
                FlowRun.state == FlowRunState.ACTIVE,
            )
            .order_by(FlowRun.started_at, FlowRun.id)
            .with_for_update(skip_locked=True)
        )
    )
    for run in runs:
        if (run.automation_plan_json or {}).get("status") != "FROZEN":
            continue
        if db.scalar(select(NodeRun.id).where(NodeRun.flow_run_id == run.id).limit(1)):
            continue
        if db.scalar(
            select(BackgroundTask.id).where(
                BackgroundTask.aggregate_id == run.id,
                BackgroundTask.task_type == "START_AUTOMATIC_RUN",
                BackgroundTask.state.in_(active_states),
            )
        ):
            continue
        key = f"recovery:start-automatic-run:{run.id}:v{run.row_version}"
        existing = db.scalar(select(BackgroundTask).where(BackgroundTask.idempotency_key == key))
        if existing is None:
            enqueue(
                db,
                task_type="START_AUTOMATIC_RUN",
                aggregate_type="FLOW_RUN",
                aggregate_id=run.id,
                idempotency_key=key,
            )
        else:
            existing.state = TaskState.RETRY
            existing.available_at = datetime.now(UTC)
            existing.lease_owner = None
            existing.lease_until = None
            existing.last_error = "STARTUP_RECOVERY"
        recovered += 1
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
    preset = attempt.agent_preset_json or {}
    node_context_prompt = preset.get("node_context_prompt")
    node = _node_with_selected_context(
        _runtime_node(snapshot, node_run.flow_node_snapshot_key),
        attempt.context_ids_json,
        node_context_prompt=(str(node_context_prompt) if node_context_prompt is not None else None),
    )
    session_binding = agent_sessions.flow_node_binding_for_attempt(
        db, attempt.id, require_provisioning=True
    )
    session_config = agent_sessions.config_from_binding(db, session_binding)
    provider = agent_sessions.provider_for_config(db, session_config)
    runtime_owner_id = sandboxes.runtime_owner_flow_run_id(db, run.id)
    host_root = sandboxes.flow_run_capability_path(
        runtime_owner_id,
        snapshot.runtime_manifest_hash,
        "conversations",
        session_binding.id,
    )
    runtime_root = Path(
        sandboxes.openhands_flow_run_capability_path(
            snapshot.runtime_manifest_hash, "conversations", session_binding.id
        )
    )
    agent_spec = agent_sessions.build_agent_spec(
        session_config,
        provider=provider,
        binding_id=session_binding.id,
        working_directory=session_binding.working_directory or "/runtime/workspace/project",
        host_root=host_root,
        runtime_root=runtime_root,
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
                "artifact": _artifact_dict(artifact),
            }
        )
    request = build_runtime_request(
        db,
        flow_run_id=runtime_owner_id,
        runtime_manifest_hash=snapshot.runtime_manifest_hash,
        attempt_id=attempt.id,
        execution_key=f"attempt:{attempt.id}:start",
        node=node,
        bindings=bindings,
        workspace_ref=attempt.workspace_ref or "",
        startup_prompt=attempt.startup_prompt,
        startup_capability_key=attempt.startup_capability_key,
        output_targets=cast(dict[str, dict[str, str]], attempt.output_targets_json or {}),
        environment_image=environment.image_digest,
        environment_id=environment.environment_id,
        environment_version_id=environment.id,
        environment_version_no=environment.version_no,
        agent_spec=agent_spec,
        conversation_id=session_binding.openhands_conversation_id,
    )
    # Attempt-local paths remain provenance/materialization roots.  The
    # FlowRun Agent writes relative outputs in its frozen shared project
    # working directory, which is the only valid FILE parser root.
    return replace(
        request,
        output_workspace_root=session_binding.working_directory or "/runtime/workspace/project",
    )


def _upload_runtime_input_attachments(
    db: Session,
    request: StartAttemptRequest,
) -> StartAttemptRequest:
    """Upload frozen FILE artifacts through OpenHands' formal workspace API."""

    binding = agent_sessions.flow_node_binding_for_attempt(
        db, request.attempt_id, require_provisioning=True
    )
    handle = _runtime_input_upload_handle(request)
    runtime = get_runtime()
    attachments: list[dict[str, Any]] = []
    bindings: list[dict[str, Any]] = []
    for original in request.bindings:
        item = copy.deepcopy(original)
        artifact = cast(dict[str, Any], item.get("artifact") or {})
        if artifact.get("artifact_type") != "FILE":
            bindings.append(item)
            continue
        storage_key = str(artifact.get("storage_key") or "")
        inline_content = artifact.get("inline_content")
        if storage_key:
            try:
                content = get_artifact_store().read(storage_key)
            except FileNotFoundError as exc:
                raise DomainError(
                    "ARTIFACT_UNAVAILABLE", "A bound file artifact is unavailable", 409
                ) from exc
        elif isinstance(inline_content, str):
            content = inline_content.encode()
        else:
            raise DomainError("ARTIFACT_UNAVAILABLE", "A bound file artifact has no content", 409)
        metadata = cast(dict[str, Any], artifact.get("metadata") or {})
        filename = str(metadata.get("filename") or f"{item.get('field_key')}.bin")
        mime_type = str(artifact.get("mime_type") or "application/octet-stream")
        path = runtime.upload_workspace_file(
            handle,
            filename=filename,
            content_type=mime_type,
            content=content,
            attachment_owner_id=binding.id,
        )
        attachment: dict[str, Any] = {
            "path": path,
            "filename": filename,
            "mime_type": mime_type,
            "byte_size": len(content),
        }
        if mime_type.startswith("image/"):
            attachment["image_data_url"] = (
                f"data:{mime_type};base64,{base64.b64encode(content).decode()}"
            )
        attachments.append(attachment)
        artifact["runtime_path"] = path
        artifact.pop("storage_key", None)
        artifact.pop("inline_content", None)
        item["artifact"] = artifact
        bindings.append(item)
    return replace(
        request,
        bindings=bindings,
        input_attachments=tuple(attachments),
    )


def _runtime_input_upload_handle(request: StartAttemptRequest) -> RuntimeHandle:
    """Route pre-start input uploads through the frozen FlowRun generation."""

    try:
        conversation_id = str(UUID(request.conversation_id or ""))
    except ValueError as exc:
        raise DomainError(
            "RUNTIME_CONVERSATION_ID_INVALID",
            "The frozen Attempt Conversation identity is invalid",
            409,
            {"attempt_id": request.attempt_id},
        ) from exc
    if conversation_id != request.conversation_id:
        raise DomainError(
            "RUNTIME_CONVERSATION_ID_INVALID",
            "The frozen Attempt Conversation identity is not canonical",
            409,
            {"attempt_id": request.attempt_id},
        )
    if request.runtime_resource_name and request.runtime_sandbox_id:
        return RuntimeHandle(
            job_id=f"env-exec:{request.runtime_resource_name}",
            conversation_id=conversation_id,
            runtime_resource_id=request.runtime_sandbox_id,
            runtime_resource_name=request.runtime_resource_name,
        )
    if get_settings().runtime_adapter == "mock":
        return RuntimeHandle(
            job_id=f"mock-job-{request.attempt_id}",
            conversation_id=conversation_id,
        )
    raise DomainError(
        "RUNTIME_SANDBOX_REQUIRED",
        "Runtime input attachments require the active FlowRun generation",
        409,
        {"attempt_id": request.attempt_id},
    )


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
    if request.input_attachments or any(
        cast(dict[str, Any], item.get("artifact") or {}).get("artifact_type") == "FILE"
        for item in request.bindings
    ):
        request = _upload_runtime_input_attachments(db, request)
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
    agent_sessions.flow_node_locator.bind_openhands_conversation(
        db,
        flow_run_id=flow_run_id,
        openhands_conversation_id=handle.conversation_id,
        display_label=f"运行 {claimed.id}",
        allow_inactive_session=get_settings().runtime_adapter == "mock",
    )
    input_event_id = handle.cursor
    if request.input_attachments and not input_event_id:
        input_event_id = get_runtime().reload_conversation(handle).event_id
    if request.input_attachments and input_event_id:
        agent_sessions.flow_node_conversations.record_attempt_input_attachments(
            db,
            attempt_id=claimed.id,
            event_id=input_event_id,
            attachments=request.input_attachments,
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
    result: RuntimeResult, output_targets: dict[str, Any], handle: RuntimeHandle
) -> list[PreparedArtifact]:
    if result.status != "COMPLETED":
        return []
    missing = sorted(set(output_targets) - set(result.outputs))
    if missing:
        raise DomainError(
            "RUNTIME_OUTPUT_MISSING",
            "The Agent completed without returning every required output",
            422,
            {"fields": missing},
        )
    prepared: list[PreparedArtifact] = []
    try:
        for field_key in output_targets:
            artifact_type, content = result.outputs[field_key]
            expected_type = str(
                cast(dict[str, Any], output_targets.get(field_key) or {}).get("artifact_type")
                or "URL"
            )
            if artifact_type != expected_type or not content.strip():
                raise DomainError(
                    "RUNTIME_OUTPUT_INVALID",
                    "The Agent returned an invalid output type or value",
                    422,
                    {"field": field_key},
                )
            if artifact_type == "URL":
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
                continue
            if artifact_type != "FILE":
                raise DomainError("RUNTIME_OUTPUT_INVALID", "Unsupported runtime output type", 422)
            path = content.strip()
            workspace_path = PurePosixPath(path)
            nodes_root = PurePosixPath("/runtime/workspace/nodes")
            if (
                not workspace_path.is_absolute()
                or ".." in workspace_path.parts
                or not workspace_path.is_relative_to(nodes_root)
                or workspace_path == nodes_root
            ):
                raise DomainError(
                    "RUNTIME_OUTPUT_INVALID",
                    "The Agent returned a file outside the managed node workspace",
                    422,
                    {"field": field_key},
                )
            workspace_file = get_runtime().download_workspace_file(handle, path)
            prepared.append(
                prepare_file_artifact(
                    field_key=field_key,
                    filename=workspace_file.filename,
                    mime_type=workspace_file.content_type,
                    content=workspace_file.content,
                    metadata={"runtime_artifact_type": "FILE", "runtime_path": path},
                )
            )
    except BaseException:
        discard_prepared_artifacts(prepared)
        raise
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
    _require_current_lease(db, lease)
    if result.status == "RUNNING":
        # A reconciliation observation is not an Attempt command or business
        # transition.  In particular, it must not consume ``state_version``
        # and race a user pause/cancel/accept action merely because the Runtime
        # has not changed.  ``WAIT_RUNTIME_WAKEUP`` is the normal driver for
        # the next projection; a periodic recovery/reconciliation task may
        # later invoke this function, but this observation must never recreate
        # a tight polling loop.
        current = _attempt(db, current_attempt_id)
        if (
            current.state == AttemptState.EXECUTING
            and current.runtime_phase == "RUNNING"
            and current.state_version == expected_version
        ):
            _finish_transaction(db, commit)
        return
    claimed = _claim_runtime_phase(
        db,
        current_attempt_id,
        expected_version,
        AttemptState.EXECUTING,
        "RUNNING",
    )
    if claimed is None:
        return
    prepared_outputs = _prepare_runtime_outputs(result, claimed.output_targets_json or {}, handle)
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


def human_input(
    db: Session, attempt_id: str, payload: HumanInputWrite, idempotency_key: str
) -> dict[str, Any]:
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
        {"content": payload.content},
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


def submit_manual_outputs(
    db: Session, attempt_id: str, payload: ManualAttemptOutputsWrite
) -> dict[str, Any]:
    """Freeze explicit CHAT-session outputs and enter the normal end gates.

    Conversation text remains OpenHands-owned and is never interpreted here.
    The operator supplies the frozen output contract values explicitly; files
    are re-authorized in the Attempt's shared project scope and copied into the
    immutable Artifact store before the workflow can advance.
    """

    current = _attempt(db, attempt_id)
    if current.startup_mode != "CHAT":
        raise illegal("only a CHAT attempt accepts manual session outputs", state=current.state)
    node_run = _node_run(db, current.node_run_id)
    run = _locked_run(db, node_run.flow_run_id)
    if run.run_mode != "MANUAL":
        raise illegal(
            "automatic attempts cannot submit manual session outputs", state=current.state
        )
    node = _node(_snapshot(db, current.snapshot_id), node_run.flow_node_snapshot_key)
    targets = _create_output_targets(db, run, current, node)
    expected_fields = set(targets)
    provided_fields = set(payload.outputs)
    if provided_fields != expected_fields:
        raise DomainError(
            "MANUAL_OUTPUT_CONTRACT_INVALID",
            "Manual session outputs must match every declared output field",
            422,
            {
                "missing_fields": sorted(expected_fields - provided_fields),
                "unknown_fields": sorted(provided_fields - expected_fields),
            },
        )

    prepared: list[PreparedArtifact] = []
    try:
        for field_key, value in payload.outputs.items():
            expected_type = str(targets[field_key]["artifact_type"])
            if value.artifact_type != expected_type:
                raise DomainError(
                    "MANUAL_OUTPUT_CONTRACT_INVALID",
                    "Manual session output type does not match the declared field",
                    422,
                    {"field": field_key, "expected_type": expected_type},
                )
            if value.artifact_type == "URL":
                prepared.append(
                    prepare_artifact(
                        ArtifactWrite(
                            field_key=field_key,
                            artifact_type="URL",
                            uri=value.uri,
                            metadata={"manual_session_output": True},
                        )
                    )
                )
                continue
            content, mime_type, filename = agent_sessions.flow_node_workspace.read_file(
                db,
                flow_run_id=run.id,
                attempt_id=current.id,
                binding_id=None,
                work_directory_id=None,
                path=value.path or "",
            )
            prepared.append(
                prepare_file_artifact(
                    field_key=field_key,
                    filename=filename,
                    mime_type=mime_type,
                    content=content,
                    metadata={
                        "manual_session_output": True,
                        "runtime_path": value.path,
                    },
                )
            )

        attempt = _claim_attempt_version(
            db,
            attempt_id,
            payload.expected_state_version,
            {AttemptState.WAITING_START_CONFIRMATION},
            next_state=AttemptState.END_GATES,
            runtime_phase="MANUAL_OUTPUTS_SUBMITTED",
        )
        attempt.output_targets_json = targets
        for item in prepared:
            _register_artifact(db, run.id, item, source="HUMAN_SESSION", attempt_id=attempt.id)
        run.state = FlowRunState.ACTIVE
        _event(
            db,
            run.id,
            "MANUAL_SESSION_OUTPUTS_SUBMITTED",
            {"fields": sorted(payload.outputs)},
            node_run.id,
            attempt.id,
        )
        _dispatch_gates(db, attempt, "END")
        finish(db)
        # The Artifact rows and their object-store references are committed.
        # From this point onward, an unexpected response-projection failure
        # must not compensate storage that is already durably referenced.
        prepared = []
        return attempt_detail(db, attempt.id)
    except BaseException:
        discard_prepared_artifacts(prepared)
        raise


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
    _release_worker_read_transaction(db, lease)
    runtime = get_runtime()
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
    prepared_outputs = _prepare_runtime_outputs(result, claimed.output_targets_json or {}, handle)
    action.payload_json = {
        "content_digest": hashlib.sha256(content.encode()).hexdigest(),
        "content_length": len(content),
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


def record_automatic_task_failure(
    db: Session,
    aggregate_id: str,
    task_type: str,
    payload: dict[str, Any],
    error: str,
) -> None:
    """Project an exhausted automatic delivery into visible workflow state."""

    if task_type == "START_AUTOMATIC_RUN":
        run = db.get(FlowRun, aggregate_id)
        if (
            run is None
            or run.run_mode != "AUTOMATIC"
            or run.state in {FlowRunState.COMPLETED, FlowRunState.CANCELLED}
        ):
            return
        run.state = FlowRunState.WAITING_HUMAN
        _event(
            db,
            run.id,
            "AUTOMATIC_SCHEDULER_FAILED",
            {"task_type": task_type, "error": error[:2000]},
        )
        db.flush()
        return

    attempt = db.get(NodeAttempt, aggregate_id)
    if attempt is None:
        return
    node_run = _node_run(db, attempt.node_run_id)
    run = _run(db, node_run.flow_run_id)
    if (
        run.run_mode != "AUTOMATIC"
        or run.state in {FlowRunState.COMPLETED, FlowRunState.CANCELLED}
        or attempt.state
        in {
            AttemptState.ACCEPTED,
            AttemptState.REJECTED,
            AttemptState.CANCELLED,
        }
    ):
        return
    start_side = task_type in {
        "EVALUATE_READINESS",
        "START_AUTOMATIC_ATTEMPT",
        "START_RUNTIME",
    } or (task_type == "RUN_GATE_POLICY" and payload.get("stage") == "START")
    if task_type == "RUN_GATE_POLICY":
        error_code = "AUTOMATIC_GATE_DELIVERY_FAILED"
    elif task_type == "START_AUTOMATIC_ATTEMPT":
        error_code = "AUTOMATIC_START_DELIVERY_FAILED"
    elif task_type == "ADVANCE_AUTOMATIC_ATTEMPT":
        error_code = "AUTOMATIC_TRANSITION_DELIVERY_FAILED"
    elif task_type == "EVALUATE_READINESS":
        error_code = "AUTOMATIC_READINESS_DELIVERY_FAILED"
    else:
        error_code = "AUTOMATIC_RUNTIME_DELIVERY_FAILED"
    attempt.state = AttemptState.START_BLOCKED if start_side else AttemptState.END_BLOCKED
    attempt.error_code = error_code
    attempt.error_detail = error[:2000]
    attempt.state_version += 1
    run.state = FlowRunState.WAITING_HUMAN
    _event(
        db,
        run.id,
        "AUTOMATIC_SCHEDULER_FAILED",
        {"task_type": task_type, "error": attempt.error_detail},
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
    if run.run_mode == "MANUAL":
        # A manual FlowRun is a reusable neutral workspace. Cancelling one
        # node execution must not terminate the parent run.
        run.state = FlowRunState.ACTIVE
        run.completion_mode = None
        run.finished_at = None
    else:
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


def delete_node_run(db: Session, flow_run_id: str, node_run_id: str) -> None:
    """Delete a manual execution that has not started, or has stopped.

    A chat-only Attempt pauses at ``WAITING_START_CONFIRMATION`` before it
    claims a Runtime phase.  It is only a draft execution record at that
    point, so requiring a cancel round-trip makes its delete action appear to
    do nothing.  Its FlowRun-owned Conversation persistence is still retained
    below, exactly as it is for a cancelled record.
    """

    run = _locked_run(db, flow_run_id)
    if run.run_mode != "MANUAL":
        raise illegal("automatic execution records are deleted through their automatic run")
    node_run = _node_run(db, node_run_id)
    if node_run.flow_run_id != run.id:
        raise not_found("node_run", node_run_id)
    attempts = list(
        db.scalars(
            select(NodeAttempt)
            .where(NodeAttempt.node_run_id == node_run.id)
            .order_by(NodeAttempt.attempt_no)
        )
    )
    latest = attempts[-1] if attempts else None
    terminal_states = {
        AttemptState.ACCEPTED,
        AttemptState.REJECTED,
        AttemptState.CANCELLED,
    }
    cancelled_and_stopped = (
        node_run.state == NodeRunState.CANCELLED
        and latest is not None
        and latest.state == AttemptState.CANCELLED
        and latest.runtime_phase == "CANCELLED"
        and all(attempt.state in terminal_states for attempt in attempts)
    )
    waiting_to_start = (
        node_run.state == NodeRunState.ACTIVE
        and latest is not None
        and latest.state == AttemptState.WAITING_START_CONFIRMATION
        and latest.runtime_phase is None
        and all(attempt.state in terminal_states for attempt in attempts[:-1])
    )
    if not (cancelled_and_stopped or waiting_to_start):
        raise DomainError(
            "NODE_RUN_DELETE_REQUIRES_CANCELLED",
            "运行中的单节点记录请先取消，并等待运行时停止后再删除",
            409,
        )

    attempt_ids = [attempt.id for attempt in attempts]
    bindings = list(
        db.scalars(
            select(AgentConversationBinding).where(
                AgentConversationBinding.host_kind == "FLOW_NODE",
                AgentConversationBinding.flow_run_id == run.id,
                AgentConversationBinding.node_run_id == node_run.id,
            )
        )
    )
    for binding in bindings:
        # The OpenHands conversation remains in the FlowRun-owned persistence
        # root until the parent FlowRun is deleted. Remove only FlowWeave's
        # locator/projection graph so no product record points at a deleted
        # NodeRun or Attempt.
        agent_sessions.delete_binding_records(db, binding.id)

    work_directories = (
        list(
            db.scalars(
                select(AgentWorkDirectory).where(
                    AgentWorkDirectory.flow_run_id == run.id,
                    AgentWorkDirectory.node_attempt_id.in_(attempt_ids),
                )
            )
        )
        if attempt_ids
        else []
    )
    work_directory_ids = [item.id for item in work_directories]
    work_directory_version_ids = (
        list(
            db.scalars(
                select(AgentWorkDirectoryVersion.id).where(
                    AgentWorkDirectoryVersion.work_directory_id.in_(work_directory_ids)
                )
            )
        )
        if work_directory_ids
        else []
    )
    if work_directory_version_ids:
        db.execute(
            delete(AgentWorkDirectoryPath).where(
                AgentWorkDirectoryPath.version_id.in_(work_directory_version_ids)
            )
        )
        db.execute(
            delete(AgentWorkDirectoryVersion).where(
                AgentWorkDirectoryVersion.id.in_(work_directory_version_ids)
            )
        )
    if work_directory_ids:
        db.execute(delete(AgentWorkDirectory).where(AgentWorkDirectory.id.in_(work_directory_ids)))

    artifacts = (
        list(
            db.scalars(
                select(ArtifactVersion).where(ArtifactVersion.producer_attempt_id.in_(attempt_ids))
            )
        )
        if attempt_ids
        else []
    )
    artifact_ids = [item.id for item in artifacts]
    if artifact_ids and db.scalar(
        select(AttemptInputBinding.id)
        .where(
            AttemptInputBinding.artifact_version_id.in_(artifact_ids),
            AttemptInputBinding.attempt_id.not_in(attempt_ids),
        )
        .limit(1)
    ):
        raise DomainError(
            "NODE_RUN_DELETE_HAS_DOWNSTREAM_REFERENCES",
            "该单节点运行的产物仍被其他执行引用，不能删除",
            409,
        )

    if attempt_ids:
        db.execute(delete(BackgroundTask).where(BackgroundTask.aggregate_id.in_(attempt_ids)))
        db.execute(
            delete(RuntimeConfirmationApproval).where(
                RuntimeConfirmationApproval.attempt_id.in_(attempt_ids)
            )
        )
        db.execute(
            delete(AttemptInputBinding).where(AttemptInputBinding.attempt_id.in_(attempt_ids))
        )
        db.execute(delete(GateEvaluation).where(GateEvaluation.attempt_id.in_(attempt_ids)))
    db.execute(delete(HumanAction).where(HumanAction.node_run_id == node_run.id))
    db.execute(delete(RunEvent).where(RunEvent.node_run_id == node_run.id))
    if artifact_ids:
        db.execute(delete(ArtifactVersion).where(ArtifactVersion.id.in_(artifact_ids)))
    if attempt_ids:
        db.execute(delete(NodeAttempt).where(NodeAttempt.id.in_(attempt_ids)))
    db.delete(node_run)
    run.state = FlowRunState.ACTIVE
    run.completion_mode = None
    run.finished_at = None
    run.row_version += 1

    store = get_artifact_store()
    for artifact in artifacts:
        if artifact.storage_key:
            register_commit_action(db, lambda key=artifact.storage_key: store.delete(key))
    # Physical OpenHands persistence and Workspace content remain FlowRun-owned
    # and are reclaimed only when the parent FlowRun is deleted.
    finish(db)


def _create_configurable_targets(db: Session, run: FlowRun, accepted: NodeRun) -> None:
    """Expose frozen downstream work without starting its Agent or gates."""

    snapshot = _active_snapshot(db, run)
    definition = snapshot.definition_json
    outputs = list(
        db.scalars(
            select(ArtifactVersion).where(
                ArtifactVersion.producer_attempt_id == accepted.accepted_attempt_id
            )
        )
    )
    by_field = {item.field_key: item.id for item in outputs}
    targets = sorted(
        {
            str(edge["target_instance_key"])
            for edge in definition.get("edges", [])
            if edge.get("source_instance_key") == accepted.flow_node_snapshot_key
        }
    )
    for target_key in targets:
        bindings = {
            str(mapping["target_input_key"]): artifact_id
            for mapping in definition.get("port_mappings", [])
            if mapping.get("source_instance_key") == accepted.flow_node_snapshot_key
            and mapping.get("target_instance_key") == target_key
            and (artifact_id := by_field.get(str(mapping.get("source_output_key") or "")))
        }
        existing = db.scalar(
            select(NodeRun).where(
                NodeRun.flow_run_id == run.id,
                NodeRun.flow_node_snapshot_key == target_key,
                NodeRun.state != NodeRunState.CANCELLED,
            )
        )
        if existing is not None:
            attempt = db.scalar(
                select(NodeAttempt)
                .where(NodeAttempt.node_run_id == existing.id)
                .order_by(NodeAttempt.attempt_no.desc())
            )
            if attempt is None:
                raise DomainError("RUN_STATE_INVALID", "node work item has no attempt", 409)
            if attempt.state != AttemptState.WAITING_INPUT:
                raise DomainError(
                    "MANUAL_FLOW_CYCLE_UNSUPPORTED",
                    "manual run groups cannot revisit a node",
                    409,
                    {"flow_node_key": target_key},
                )
            current = {item.input_field_key: item for item in _bindings(db, attempt.id)}
            for field_key, artifact_id in bindings.items():
                if field_key in current:
                    current[field_key].artifact_version_id = artifact_id
                    current[field_key].binding_source = "PORT_MAPPING"
                else:
                    db.add(
                        AttemptInputBinding(
                            attempt_id=attempt.id,
                            input_field_key=field_key,
                            artifact_version_id=artifact_id,
                            binding_source="PORT_MAPPING",
                        )
                    )
            attempt.state_version += 1
            _event(
                db,
                run.id,
                "DOWNSTREAM_INPUTS_BOUND",
                {"fields": sorted(bindings), "source_node_key": accepted.flow_node_snapshot_key},
                existing.id,
                attempt.id,
            )
            continue
        node = _node(snapshot, target_key)
        asset = cast(dict[str, Any], node.get("asset") or {})
        asset_id = str(asset.get("id") or "")
        if not asset_id:
            raise DomainError("SNAPSHOT_INVALID", "node asset id is missing", 409)
        _validate_input_bindings(db, run, node, bindings)
        sequence = (
            db.scalar(select(func.max(NodeRun.sequence_no)).where(NodeRun.flow_run_id == run.id))
            or 0
        ) + 1
        node_run = NodeRun(
            flow_run_id=run.id,
            flow_node_snapshot_key=target_key,
            sequence_no=sequence,
            created_from="FLOW_TRANSITION",
        )
        db.add(node_run)
        db.flush()
        attempt = NodeAttempt(
            node_run_id=node_run.id,
            attempt_no=1,
            snapshot_id=snapshot.id,
            state=AttemptState.WAITING_INPUT,
            workspace_ref=str(
                attempt_workspace_path(
                    asset_id=asset_id, run_id=run.id, node_run_id=node_run.id, attempt_no=1
                )
            ),
        )
        db.add(attempt)
        db.flush()
        ensure_flow_run_attempt_workspace(
            flow_run_id=run.id, asset_id=asset_id, workspace_ref=attempt.workspace_ref or ""
        )
        for field_key, artifact_id in bindings.items():
            db.add(
                AttemptInputBinding(
                    attempt_id=attempt.id,
                    input_field_key=field_key,
                    artifact_version_id=artifact_id,
                    binding_source="PORT_MAPPING",
                )
            )
        _event(
            db,
            run.id,
            "DOWNSTREAM_NODE_AVAILABLE",
            {"source_node_key": accepted.flow_node_snapshot_key, "target_node_key": target_key},
            node_run.id,
            attempt.id,
        )


def _advance_automatic_targets(
    db: Session, run: FlowRun, accepted: NodeRun, selected_targets: list[str]
) -> None:
    """Advance only frozen graph successors after an automatic acceptance.

    This is intentionally platform-owned: the primary Agent cannot select an
    arbitrary target or write bindings.  Multiple predecessors merge into one
    durable waiting NodeRun, and the regular readiness pipeline starts it only
    when every declared input has a valid Artifact.
    """

    snapshot = _active_snapshot(db, run)
    definition = snapshot.definition_json
    plan: dict[str, Any] = dict(run.automation_plan_json or {})
    node_plans = cast(dict[str, Any], plan.get("node_plans") or {})
    source_outputs = {
        item.field_key: item.id
        for item in db.scalars(
            select(ArtifactVersion).where(
                ArtifactVersion.producer_attempt_id == accepted.accepted_attempt_id
            )
        )
    }
    allowed = set(_automatic_successor_keys(db, run, accepted))
    unauthorized = sorted(set(selected_targets) - allowed)
    if unauthorized:
        raise DomainError(
            "AUTOMATIC_TRANSITION_UNAUTHORIZED",
            "automatic transition selected a node outside the frozen topology",
            409,
            {"node_keys": unauthorized},
        )
    for target_key in selected_targets:
        target_plan = _automatic_node_plan(node_plans, target_key)
        mapped = {
            str(mapping["target_input_key"]): artifact_id
            for mapping in definition.get("port_mappings", [])
            if mapping.get("source_instance_key") == accepted.flow_node_snapshot_key
            and mapping.get("target_instance_key") == target_key
            and (artifact_id := source_outputs.get(str(mapping.get("source_output_key") or "")))
        }
        existing = db.scalar(
            select(NodeRun).where(
                NodeRun.flow_run_id == run.id,
                NodeRun.flow_node_snapshot_key == target_key,
            )
        )
        if existing is None:
            explicit = _automatic_plan_artifacts(db, run, target_key, target_plan)
            # A mapped source is authoritative for its frozen target port.
            explicit.update(mapped)
            created, created_attempt = _create_node_run(
                db,
                run,
                target_key,
                explicit,
                "AUTOMATIC_TRANSITION",
                cast(list[dict[str, Any]], target_plan.get("gates") or []),
                context_ids=[],
                agent_preset=cast(dict[str, Any], target_plan.get("agent_preset") or {}),
            )
            # Keep the mapped fields auditable as platform-owned port flow;
            # explicit plan inputs retain their original creation provenance.
            for binding in _bindings(db, created_attempt.id):
                if binding.input_field_key in mapped:
                    binding.binding_source = "AUTOMATIC_PORT_MAPPING"
            _event(
                db,
                run.id,
                "AUTOMATIC_DOWNSTREAM_AVAILABLE",
                {
                    "source_node_key": accepted.flow_node_snapshot_key,
                    "target_node_key": target_key,
                },
                created.id,
                created_attempt.id,
            )
            continue
        current_attempt = db.scalar(
            select(NodeAttempt)
            .where(NodeAttempt.node_run_id == existing.id)
            .order_by(NodeAttempt.attempt_no.desc())
        )
        if current_attempt is None:
            raise DomainError("RUN_STATE_INVALID", "automatic node has no Attempt", 409)
        if current_attempt.state != AttemptState.WAITING_INPUT:
            # A completed/started target cannot be retroactively re-bound by a
            # late predecessor.  The topology itself remains immutable.
            continue
        current = {row.input_field_key: row for row in _bindings(db, current_attempt.id)}
        for field_key, artifact_id in mapped.items():
            binding = current.get(field_key)
            if binding is None:
                db.add(
                    AttemptInputBinding(
                        attempt_id=current_attempt.id,
                        input_field_key=field_key,
                        artifact_version_id=artifact_id,
                        binding_source="AUTOMATIC_PORT_MAPPING",
                    )
                )
            else:
                binding.artifact_version_id = artifact_id
                binding.binding_source = "AUTOMATIC_PORT_MAPPING"
        current_attempt.state_version += 1
        _event(
            db,
            run.id,
            "AUTOMATIC_DOWNSTREAM_INPUTS_BOUND",
            {"fields": sorted(mapped)},
            existing.id,
            current_attempt.id,
        )
        db.flush()
        _dispatch_readiness(db, current_attempt)


def accept_attempt(
    db: Session,
    attempt_id: str,
    payload: AttemptVersionWrite,
    idempotency_key: str,
) -> dict[str, Any]:
    current = _attempt(db, attempt_id)
    current_node_run = _node_run(db, current.node_run_id)
    run = _locked_run(db, current_node_run.flow_run_id)
    if run.run_mode == "AUTOMATIC":
        raise illegal(
            "automatic attempts are accepted only by the platform scheduler",
            state=current.state,
        )
    existing = db.scalar(select(HumanAction).where(HumanAction.idempotency_key == idempotency_key))
    if existing is not None:
        if existing.action_type != "ACCEPT_ATTEMPT" or existing.attempt_id != attempt_id:
            raise conflict(
                "accept idempotency key is already used",
                attempt_id=existing.attempt_id,
            )
        if existing.payload_json.get("expected_state_version") != payload.expected_state_version:
            raise conflict(
                "accept request does not match the idempotent request",
                attempt_id=attempt_id,
            )
        return run_detail(db, run.id)
    attempt = _claim_attempt_version(
        db,
        attempt_id,
        payload.expected_state_version,
        {AttemptState.WAITING_ACCEPTANCE},
        next_state=AttemptState.ACCEPTED,
    )
    node_run = _node_run(db, attempt.node_run_id)
    _action(
        db,
        run.id,
        "ACCEPT_ATTEMPT",
        idempotency_key,
        {"expected_state_version": payload.expected_state_version},
        node_run_id=node_run.id,
        attempt_id=attempt.id,
    )
    node_run.state = NodeRunState.ACCEPTED
    node_run.accepted_attempt_id = attempt.id
    _event(db, run.id, "NODE_RUN_COMPLETED", {}, node_run.id, attempt.id)
    _create_configurable_targets(db, run, node_run)
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
    next_attempt = NodeAttempt(
        node_run_id=node_run.id,
        attempt_no=next_no,
        snapshot_id=run.active_snapshot_id,
        context_ids_json=attempt.context_ids_json,
        agent_preset_json=attempt.agent_preset_json,
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
    ensure_flow_run_attempt_workspace(
        flow_run_id=run.id, asset_id=next_asset_id, workspace_ref=next_attempt.workspace_ref or ""
    )
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
    retryable_error_codes = {
        None,
        "GATE_CONFIG_INVALID",
        "AUTOMATIC_GATE_DELIVERY_FAILED",
        "AUTOMATIC_START_DELIVERY_FAILED",
        "AUTOMATIC_TRANSITION_DELIVERY_FAILED",
        "AUTOMATIC_TRANSITION_INVALID",
    }
    if current.error_code not in retryable_error_codes:
        raise illegal(
            "attempt failure cannot be recovered by rerunning gates",
            state=current.state,
            error_code=current.error_code,
        )
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
    if run.run_mode == "AUTOMATIC":
        raise illegal(
            "automatic runs keep their creation snapshot and cannot use manual snapshot sync",
            state=run.state,
        )
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
    runtime_manifest = _preserve_runtime_oracle_profile(
        current.runtime_manifest_json or {}, _compile_runtime_manifest(definition)
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


def complete_run(db: Session, run_id: str, idempotency_key: str) -> dict[str, Any]:
    run = _run(db, run_id)
    if run.run_mode == "AUTOMATIC":
        raise illegal(
            "automatic runs can only be completed by the platform scheduler",
            state=run.state,
        )
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


def _delete_run_records(db: Session, run_id: str) -> None:
    """Permanently remove a run and all durable execution data it owns.

    The Runtime Provider owns physical cleanup. The application explicitly
    removes Conversation bindings and their children before the FlowRun row.
    """

    run = _run(db, run_id)
    child_run_ids = list(
        db.scalars(
            select(FlowRun.id)
            .where(FlowRun.parent_flow_run_id == run.id)
            .order_by(FlowRun.started_at, FlowRun.id)
        )
    )
    for child_run_id in child_run_ids:
        _delete_run_records(db, child_run_id)
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
            select(AgentConversationBinding.id).where(
                AgentConversationBinding.host_kind == "FLOW_NODE",
                AgentConversationBinding.flow_run_id == run.id,
            )
        )
    )
    work_directory_ids = list(
        db.scalars(select(AgentWorkDirectory.id).where(AgentWorkDirectory.flow_run_id == run.id))
    )
    work_directory_version_ids = (
        list(
            db.scalars(
                select(AgentWorkDirectoryVersion.id).where(
                    AgentWorkDirectoryVersion.work_directory_id.in_(work_directory_ids)
                )
            )
        )
        if work_directory_ids
        else []
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
    if conversation_ids:
        db.execute(
            delete(RuntimeConfirmationApproval).where(
                RuntimeConfirmationApproval.flow_run_conversation_binding_id.in_(conversation_ids)
            )
        )
        db.execute(
            delete(AgentConversationMessageAttachment).where(
                AgentConversationMessageAttachment.binding_id.in_(conversation_ids)
            )
        )
        db.execute(
            delete(AgentConversationCapability).where(
                AgentConversationCapability.binding_id.in_(conversation_ids)
            )
        )
        db.execute(
            delete(AgentConversationCommand).where(
                AgentConversationCommand.binding_id.in_(conversation_ids)
            )
        )
        db.execute(
            delete(AgentConversationBinding).where(
                AgentConversationBinding.id.in_(conversation_ids)
            )
        )
    # Logical work directories are FlowRun-owned product records.  Delete their
    # complete graph explicitly before deleting Attempts so that the service,
    # rather than a database cascade or RESTRICT constraint, defines the
    # FlowRun deletion contract.
    if work_directory_version_ids:
        db.execute(
            delete(AgentWorkDirectoryPath).where(
                AgentWorkDirectoryPath.version_id.in_(work_directory_version_ids)
            )
        )
        db.execute(
            delete(AgentWorkDirectoryVersion).where(
                AgentWorkDirectoryVersion.id.in_(work_directory_version_ids)
            )
        )
    if work_directory_ids:
        db.execute(delete(AgentWorkDirectory).where(AgentWorkDirectory.id.in_(work_directory_ids)))
    if attempt_ids:
        db.execute(
            delete(RuntimeConfirmationApproval).where(
                RuntimeConfirmationApproval.attempt_id.in_(attempt_ids)
            )
        )
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


def delete_run(db: Session, run_id: str) -> None:
    """Permanently remove a run and every nested execution record it owns."""

    _delete_run_records(db, run_id)
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
        "context_ids": attempt.context_ids_json,
        "agent_preset": attempt.agent_preset_json,
        "gate_policies": attempt.gate_policies_json,
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
        "run_mode": run.run_mode,
        "automation_plan": run.automation_plan_json,
        "parent_flow_run_id": run.parent_flow_run_id,
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
    # The outer Runs page remains the historical FlowRun overview. Automatic
    # executions are managed only inside their owning FlowRun workbench.
    runs = list(
        db.scalars(
            select(FlowRun)
            .where(FlowRun.parent_flow_run_id.is_(None))
            .order_by(FlowRun.started_at.desc())
        )
    )
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
    runtime_readiness = sandboxes.runtime_readiness_by_flow_run(db, run_ids)

    nodes_by_run: dict[str, list[NodeRun]] = {item.id: [] for item in runs}
    for node_run in node_runs:
        nodes_by_run[node_run.flow_run_id].append(node_run)
    attempts_by_node: dict[str, list[NodeAttempt]] = {item.id: [] for item in node_runs}
    for attempt in attempts:
        attempts_by_node[attempt.node_run_id].append(attempt)
    snapshots_by_id = {item.id: item for item in snapshots}
    pending_states = {
        AttemptState.WAITING_START_CONFIRMATION,
        AttemptState.PAUSED,
        AttemptState.WAITING_HUMAN,
        AttemptState.WAITING_ACCEPTANCE,
        AttemptState.START_BLOCKED,
        AttemptState.END_BLOCKED,
    }

    result: list[dict[str, Any]] = []
    for run in runs:
        runtime = runtime_readiness.get(run.id)
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
        if runtime and isinstance(runtime.get("updated_at"), datetime):
            activity_times.append(cast(datetime, runtime["updated_at"]))
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
                "run_mode": run.run_mode,
                "automation_plan": run.automation_plan_json,
                "state": run.state,
                "completion_mode": run.completion_mode,
                "environment_version_id": run.environment_version_id,
                "active_snapshot_version": snapshot.version if snapshot else None,
                "current_node_key": (current_node.flow_node_snapshot_key if current_node else None),
                "current_node_name": current_name,
                "current_attempt_state": current_attempt.state if current_attempt else None,
                "runtime_status": (
                    "DRAFT"
                    if run.run_mode == "AUTOMATIC" and run.state == FlowRunState.DRAFT
                    else runtime.get("status")
                    if runtime
                    else "ARCHIVED"
                ),
                "runtime_write_available": bool(
                    run.state != FlowRunState.DRAFT and runtime and runtime.get("write_available")
                ),
                "runtime_message": (
                    "自动运行尚未启动，可继续编辑编排"
                    if run.run_mode == "AUTOMATIC" and run.state == FlowRunState.DRAFT
                    else runtime.get("message")
                    if runtime
                    else None
                ),
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
