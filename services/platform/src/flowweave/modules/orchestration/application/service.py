from __future__ import annotations

import hashlib
import json
import logging
import shutil
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, cast
from urllib.parse import urlparse
from uuid import uuid4

from sqlalchemy import delete, func, select, update
from sqlalchemy.orm import Session

from flowweave.modules.catalog.public import describe_asset
from flowweave.modules.conversations import public as conversations
from flowweave.modules.credentials.public import connected_lark_access_token
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
from flowweave.modules.tasks.public import Lease, enqueue, lease_is_current
from flowweave.runtime.base import RuntimeHandle, RuntimeResult, StartAttemptRequest
from flowweave.runtime.dependencies import get_runtime
from flowweave.runtime.request import build_runtime_request
from flowweave.runtime.routing import runtime_for
from flowweave.runtime.workspace import attempt_workspace_path
from flowweave.shared.application.transactions import (
    finish,
    register_commit_action,
    register_rollback_action,
)
from flowweave.shared.artifact_store import get_artifact_store
from flowweave.shared.errors import DomainError, conflict, illegal, not_found
from flowweave.shared.lark_drive import get_lark_drive
from flowweave.shared.models import (
    AgentConversation,
    AgentMessage,
    ArtifactVersion,
    AttemptInputBinding,
    AttemptState,
    BackgroundTask,
    EnvironmentVersion,
    FlowRun,
    FlowRunState,
    GateEvaluation,
    HumanAction,
    NodeAsset,
    NodeAttempt,
    NodeRun,
    NodeRunState,
    RunEvent,
    RunSnapshot,
    TaskState,
    now,
)
from flowweave.shared.schemas import (
    ArtifactWrite,
    AttemptStartWrite,
    AttemptVersionWrite,
    HumanInputWrite,
    InputBindingsWrite,
    NodeRunStart,
    RejectWrite,
    RunStart,
    SyncSnapshotWrite,
)
from flowweave.shared.settings import get_settings

logger = logging.getLogger(__name__)


def _hash(value: Any) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, default=str).encode()).hexdigest()


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


def _snapshot_definition(db: Session, flow_id: str) -> dict[str, Any]:
    definition = describe_flow(db, load_flow(db, flow_id))
    for node in definition["nodes"]:
        asset = db.get(NodeAsset, node["node_asset_id"])
        if not asset or asset.deleted_at:
            raise not_found("node_asset", node["node_asset_id"])
        node["asset"] = describe_asset(db, asset)
    return definition


def _node(snapshot: RunSnapshot, instance_key: str) -> dict[str, Any]:
    for item in snapshot.definition_json["nodes"]:
        if item["instance_key"] == instance_key:
            return item
    raise not_found("flow_node_snapshot", instance_key)


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


def _dispatch_poll(db: Session, attempt: NodeAttempt, poll_no: int, *, delayed: bool) -> None:
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
    _validate_input_bindings(db, run, node, artifact_ids)
    existing = db.scalar(
        select(NodeRun)
        .where(
            NodeRun.flow_run_id == run.id,
            NodeRun.flow_node_snapshot_key == instance_key,
        )
        .order_by(NodeRun.sequence_no)
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
    definition = _snapshot_definition(db, flow_id)
    run_no = (
        db.scalar(select(func.max(FlowRun.run_no)).where(FlowRun.flow_definition_id == flow_id))
        or 0
    ) + 1
    environment = None
    if payload.environment_version_id:
        environment = db.scalar(
            select(EnvironmentVersion)
            .where(EnvironmentVersion.id == payload.environment_version_id)
            .with_for_update()
        )
        if environment is None:
            raise not_found("environment_version", payload.environment_version_id)
        if environment.state != "READY" or not environment.image_digest:
            raise DomainError(
                "ENVIRONMENT_VERSION_NOT_READY",
                "The selected terminal environment version is not ready",
                409,
                {"environment_version_id": payload.environment_version_id},
            )
    run_name = payload.name or f"{flow.name} · Run #{run_no}"
    run = FlowRun(
        flow_definition_id=flow_id,
        run_no=run_no,
        name=run_name,
        environment_version_id=environment.id if environment else None,
        lark_folder_token=None,
        lark_folder_url=None,
    )
    db.add(run)
    db.flush()
    snapshot = RunSnapshot(
        flow_run_id=run.id,
        version=1,
        definition_json=definition,
        definition_hash=_hash(definition),
    )
    db.add(snapshot)
    db.flush()
    run.active_snapshot_id = snapshot.id
    _event(
        db,
        run.id,
        "FLOW_RUN_CREATED",
        {
            "snapshot_version": 1,
            "environment_version_id": environment.id if environment else None,
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
    conversations.ensure_auto_conversation(db, attempt)
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


def _url_token(value: str, marker: str) -> str:
    path = urlparse(value).path
    if marker not in path:
        raise DomainError(
            "LARK_RESOURCE_URL_INVALID",
            "The configured Lark resource URL is invalid",
            422,
            {"url": value},
        )
    token = path.split(marker, 1)[1].split("/", 1)[0]
    if not token:
        raise DomainError(
            "LARK_RESOURCE_URL_INVALID",
            "The configured Lark resource token is missing",
            422,
            {"url": value},
        )
    return token


def _create_output_targets(
    db: Session, run: FlowRun, attempt: NodeAttempt, node: dict[str, Any]
) -> dict[str, dict[str, str]]:
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
    try:
        access_token = connected_lark_access_token(db)
    except DomainError as exc:
        if exc.code in {"CREDENTIAL_CONNECTION_REQUIRED", "CREDENTIAL_REAUTH_REQUIRED"}:
            return {}
        raise
    drive = get_lark_drive()
    is_wiki = "/wiki/" in urlparse(root_url).path
    if not run.lark_folder_token or not run.lark_folder_url:
        if is_wiki:
            folder = drive.create_wiki_node(
                access_token=access_token, parent_url=root_url, name=run.name
            )
            register_rollback_action(
                db,
                lambda url=folder.url: drive.delete_wiki_node(
                    access_token=access_token, node_url=url
                ),
            )
        else:
            folder = drive.create_folder(
                access_token=access_token,
                parent_token=_url_token(root_url, "/drive/folder/"),
                parent_url=root_url,
                name=run.name,
            )
            register_rollback_action(
                db,
                lambda token=folder.token: drive.delete(
                    access_token=access_token, token=token, resource_type="folder"
                ),
            )
        run.lark_folder_token = folder.token
        run.lark_folder_url = folder.url

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
        if is_wiki:
            resource = drive.create_wiki_node(
                access_token=access_token,
                parent_url=run.lark_folder_url or root_url,
                name=title,
            )
            register_rollback_action(
                db,
                lambda url=resource.url: drive.delete_wiki_node(
                    access_token=access_token, node_url=url
                ),
            )
            token = resource.object_token or resource.token
        elif template_url:
            resource = drive.copy_docx(
                access_token=access_token,
                source_token=_url_token(template_url, "/docx/"),
                folder_token=run.lark_folder_token or "",
                name=title,
            )
            token = resource.token
            register_rollback_action(
                db,
                lambda token=token: drive.delete(
                    access_token=access_token, token=token, resource_type="docx"
                ),
            )
        else:
            resource = drive.create_docx(
                access_token=access_token,
                folder_token=run.lark_folder_token or "",
                folder_url=run.lark_folder_url or root_url,
                name=title,
            )
            token = resource.token
            register_rollback_action(
                db,
                lambda token=token: drive.delete(
                    access_token=access_token, token=token, resource_type="docx"
                ),
            )
        targets[field_key] = {
            "url": resource.url,
            "token": token,
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

        active = db.scalar(
            select(BackgroundTask.id).where(
                BackgroundTask.aggregate_id == attempt.id,
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
            enqueue(
                db,
                task_type=task_type,
                aggregate_type="ATTEMPT",
                aggregate_id=attempt.id,
                idempotency_key=recovery_key,
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
    finish(db)
    return recovered


def _runtime_request(db: Session, attempt: NodeAttempt) -> StartAttemptRequest:
    node_run = _node_run(db, attempt.node_run_id)
    run = _run(db, node_run.flow_run_id)
    environment = (
        db.get(EnvironmentVersion, run.environment_version_id)
        if run.environment_version_id
        else None
    )
    node = _node(_snapshot(db, attempt.snapshot_id), node_run.flow_node_snapshot_key)
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
    return build_runtime_request(
        db,
        attempt_id=attempt.id,
        execution_key=f"attempt:{attempt.id}:start",
        node=node,
        bindings=bindings,
        workspace_ref=attempt.workspace_ref or "",
        startup_prompt=attempt.startup_prompt,
        startup_capability_key=attempt.startup_capability_key,
        output_targets=cast(dict[str, dict[str, str]], attempt.output_targets_json or {}),
        environment_image=environment.image_digest if environment else None,
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
    if attempt.runtime_job_id:
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
            _finish_transaction(db, commit)
        return

    current_attempt_id = attempt.id
    request = _runtime_request(db, attempt)
    _release_worker_read_transaction(db, lease)
    handle = get_runtime().start(request)
    _require_current_lease(db, lease)
    claimed = _claim_runtime_phase(
        db,
        current_attempt_id,
        expected_version,
        AttemptState.EXECUTING,
        "STARTING",
        runtime_job_id=handle.job_id,
        conversation_id=handle.conversation_id,
        runtime_cursor=handle.cursor,
        runtime_adapter=get_settings().runtime_adapter,
        runtime_phase="RUNNING",
    )
    if claimed is None:
        _release_worker_read_transaction(db, lease)
        get_runtime().cancel(handle)
        _require_current_lease(db, lease)
        return
    conversations.bind_auto_runtime(
        db,
        claimed.id,
        runtime_job_id=handle.job_id,
        runtime_conversation_id=handle.conversation_id,
        runtime_cursor=handle.cursor,
        runtime_adapter=get_settings().runtime_adapter,
    )
    if _inline_execution():
        process_poll_runtime(db, claimed.id, 1, lease, commit=commit)
    else:
        _dispatch_poll(db, claimed, 1, delayed=False)
        _finish_transaction(db, commit)


def _apply_runtime_result(
    db: Session,
    attempt: NodeAttempt,
    result: RuntimeResult,
    *,
    prepared_outputs: list[PreparedArtifact],
    result_key: str,
    commit: bool = True,
) -> dict[str, Any]:
    node_run = _node_run(db, attempt.node_run_id)
    run = _run(db, node_run.flow_run_id)
    attempt.runtime_cursor = result.cursor
    conversations.project_auto_runtime_result(db, attempt.id, result, result_key=result_key)
    if result.status == "HUMAN_INPUT_REQUIRED":
        attempt.state = AttemptState.WAITING_HUMAN
        conversations.set_auto_conversation_state(
            db, attempt.id, conversations.ConversationState.WAITING_HUMAN
        )
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
        conversations.set_auto_conversation_state(
            db, attempt.id, conversations.ConversationState.IDLE
        )
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
        conversations.set_auto_conversation_state(
            db, attempt.id, conversations.ConversationState.GENERATING
        )
    elif result.status == "FAILED":
        conversations.set_auto_conversation_state(
            db, attempt.id, conversations.ConversationState.FAILED
        )
        attempt.state = AttemptState.END_BLOCKED
        attempt.runtime_phase = "FAILED"
        attempt.error_code = "RUNTIME_FAILED"
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
    return [
        prepare_artifact(
            ArtifactWrite(
                field_key=field_key,
                artifact_type="URL",
                uri=content,
                metadata={"runtime_artifact_type": "URL"},
            )
        )
        for field_key, raw_target in output_targets.items()
        if isinstance(raw_target, dict)
        for target in [cast(dict[str, Any], raw_target)]
        for content in [str(target.get("url") or "")]
        if content
    ]


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
    handle = RuntimeHandle(
        attempt.runtime_job_id or "", attempt.conversation_id or "", attempt.runtime_cursor
    )
    _release_worker_read_transaction(db, lease)
    runtime = get_runtime()
    batch = runtime.read_events(handle)
    result = batch.result or runtime.inspect(
        RuntimeHandle(handle.job_id, handle.conversation_id, batch.cursor or handle.cursor)
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
    node_run = _node_run(db, claimed.node_run_id)
    for runtime_event in batch.events:
        _event(
            db,
            node_run.flow_run_id,
            f"RUNTIME_EVENT_{runtime_event.event_type}",
            {"runtime_cursor": runtime_event.cursor, **runtime_event.payload},
            node_run.id,
            claimed.id,
        )
        conversations.project_runtime_event(
            db,
            claimed.id,
            cursor=runtime_event.cursor,
            event_type=runtime_event.event_type,
            payload=runtime_event.payload,
        )
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
        commit=commit,
    )
    if result.status == "RUNNING":
        _dispatch_poll(db, claimed, poll_no + 1, delayed=True)
        _finish_transaction(db, commit)


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
    conversations.record_auto_human_input(
        db, attempt.id, action_id=action.id, content=payload.content
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
    handle = RuntimeHandle(
        attempt.runtime_job_id or "", attempt.conversation_id or "", attempt.runtime_cursor
    )
    _release_worker_read_transaction(db, lease)
    result = get_runtime().resume(handle, content)
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
    conversations.mark_auto_human_input_delivered(db, claimed.id, action_id=action.id)
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
) -> list[tuple[str | None, RuntimeHandle]]:
    targets: list[tuple[str | None, RuntimeHandle]] = []
    seen: set[str] = set()
    if attempt.runtime_job_id and attempt.conversation_id:
        seen.add(attempt.conversation_id)
        targets.append(
            (
                attempt.runtime_adapter,
                RuntimeHandle(
                    attempt.runtime_job_id, attempt.conversation_id, attempt.runtime_cursor
                ),
            )
        )
    for conversation in db.scalars(
        select(AgentConversation).where(AgentConversation.attempt_id == attempt.id)
    ):
        conversation_id = conversation.runtime_conversation_id
        if not conversation_id or conversation_id in seen:
            continue
        seen.add(conversation_id)
        targets.append(
            (
                conversation.runtime_adapter,
                RuntimeHandle(
                    conversation.runtime_job_id or conversation_id,
                    conversation_id,
                    conversation.runtime_cursor,
                ),
            )
        )
    return targets


def process_cancel_runtime(
    db: Session,
    attempt_id: str,
    lease: Lease | None = None,
    *,
    commit: bool = True,
) -> None:
    attempt = _attempt(db, attempt_id)
    if attempt.state != AttemptState.CANCELLED or attempt.runtime_phase != "CANCELLING":
        return
    expected_version = attempt.state_version
    current_attempt_id = attempt.id
    targets = _runtime_cancel_targets(db, attempt)
    if targets:
        _release_worker_read_transaction(db, lease)
        for adapter, handle in targets:
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
    db: Session, attempt_id: str, payload: AttemptVersionWrite, idempotency_key: str
) -> dict[str, Any]:
    attempt = _attempt(db, attempt_id)
    node_run = _node_run(db, attempt.node_run_id)
    run = _run(db, node_run.flow_run_id)
    _action(
        db,
        run.id,
        "RETRY_RUNTIME_CANCEL",
        idempotency_key,
        node_run_id=node_run.id,
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
    enqueue(
        db,
        task_type="CANCEL_RUNTIME",
        aggregate_type="ATTEMPT",
        aggregate_id=claimed.id,
        idempotency_key=f"retry-cancel-runtime:{claimed.id}:v{claimed.state_version}",
    )
    _event(db, run.id, "ATTEMPT_CANCEL_RETRIED", {}, node_run.id, claimed.id)
    finish(db)
    return attempt_detail(db, claimed.id)


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
    conversations.set_attempt_conversations_state(
        db, attempt.id, conversations.ConversationState.READ_ONLY
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
    conversations.set_attempt_conversations_state(
        db, attempt.id, conversations.ConversationState.READ_ONLY
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
    definition = _snapshot_definition(db, run.flow_definition_id)
    if _hash(definition) == current.definition_hash:
        return run_detail(db, run.id)
    action = _action(db, run.id, "SYNC_SNAPSHOT", idempotency_key)
    snapshot = RunSnapshot(
        flow_run_id=run.id,
        version=current.version + 1,
        definition_json=definition,
        definition_hash=_hash(definition),
        created_by_action_id=action.id,
    )
    db.add(snapshot)
    db.flush()
    run.active_snapshot_id = snapshot.id
    run.row_version += 1
    _event(db, run.id, "SNAPSHOT_SYNCED", {"version": snapshot.version})
    finish(db)
    return run_detail(db, run.id)


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
        conversations.set_attempt_conversations_state(
            db, attempt.id, conversations.ConversationState.READ_ONLY
        )
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
            conversations.set_attempt_conversations_state(
                db, attempt.id, conversations.ConversationState.READ_ONLY
            )
            continue
        attempt.state = AttemptState.CANCELLED
        conversations.set_attempt_conversations_state(
            db, attempt.id, conversations.ConversationState.READ_ONLY
        )
        attempt.state_version += 1
        if _runtime_cancel_targets(db, attempt):
            attempt.runtime_phase = "CANCELLING"
            enqueue(
                db,
                task_type="CANCEL_RUNTIME",
                aggregate_type="ATTEMPT",
                aggregate_id=attempt.id,
                idempotency_key=f"cancel-runtime:{attempt.id}:{attempt.runtime_job_id}",
            )
        else:
            attempt.runtime_phase = "CANCELLED"
    _event(db, run.id, "FLOW_RUN_CANCELLED")
    finish(db)
    return run_detail(db, run.id)


def delete_run(db: Session, run_id: str) -> None:
    """Permanently remove a run and all durable execution data it owns.

    Runtime jobs and object storage are cleaned only after the database transaction
    commits. Runtime cleanup is best-effort because an unavailable executor must not
    prevent deletion of an already-terminal durable run.
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
    agent_conversations = (
        list(
            db.scalars(
                select(AgentConversation).where(AgentConversation.attempt_id.in_(attempt_ids))
            )
        )
        if attempt_ids
        else []
    )
    runtime_handles: list[tuple[str | None, RuntimeHandle]] = []
    attempt_runtime_ids = {attempt.conversation_id for attempt in attempts}
    for attempt in attempts:
        if attempt.runtime_job_id and attempt.conversation_id:
            runtime_handles.append(
                (
                    attempt.runtime_adapter,
                    RuntimeHandle(
                        job_id=attempt.runtime_job_id,
                        conversation_id=attempt.conversation_id,
                        cursor=attempt.runtime_cursor,
                    ),
                )
            )
    for conversation in agent_conversations:
        if (
            conversation.runtime_conversation_id
            and conversation.runtime_conversation_id not in attempt_runtime_ids
        ):
            runtime_handles.append(
                (
                    conversation.runtime_adapter,
                    RuntimeHandle(
                        job_id=conversation.runtime_job_id or conversation.runtime_conversation_id,
                        conversation_id=conversation.runtime_conversation_id,
                        cursor=conversation.runtime_cursor,
                    ),
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
    conversation_ids = [item.id for item in agent_conversations]
    message_ids = (
        list(
            db.scalars(
                select(AgentMessage.id).where(AgentMessage.conversation_id.in_(conversation_ids))
            )
        )
        if conversation_ids
        else []
    )
    task_aggregate_ids = [*attempt_ids, *conversation_ids, *message_ids]
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
    if conversation_ids:
        db.execute(delete(AgentConversation).where(AgentConversation.id.in_(conversation_ids)))
    db.execute(delete(ArtifactVersion).where(ArtifactVersion.flow_run_id == run.id))
    if attempt_ids:
        db.execute(delete(NodeAttempt).where(NodeAttempt.id.in_(attempt_ids)))
    if node_run_ids:
        db.execute(delete(NodeRun).where(NodeRun.id.in_(node_run_ids)))
    db.execute(delete(RunSnapshot).where(RunSnapshot.flow_run_id == run.id))
    db.delete(run)
    for adapter, handle in runtime_handles:
        register_commit_action(
            db,
            lambda adapter=adapter, handle=handle: _cancel_runtime_after_delete(
                adapter, handle, run_id
            ),
        )
    store = get_artifact_store()
    for key in storage_keys:
        register_commit_action(db, lambda key=key: store.delete(key))
    for path in workspace_paths:
        register_commit_action(db, lambda path=path: shutil.rmtree(path, ignore_errors=True))
    finish(db)


def _cancel_runtime_after_delete(adapter: str | None, handle: RuntimeHandle, run_id: str) -> None:
    try:
        runtime_for(adapter, handle).cancel(handle)
    except Exception:
        logger.warning(
            "Runtime cleanup failed after flow run %s was permanently deleted (conversation_id=%s)",
            run_id,
            handle.conversation_id,
            exc_info=True,
        )


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
    return {
        "id": attempt.id,
        "node_run_id": attempt.node_run_id,
        "attempt_no": attempt.attempt_no,
        "snapshot_id": attempt.snapshot_id,
        "state": attempt.state,
        "state_version": attempt.state_version,
        "runtime_phase": attempt.runtime_phase,
        "runtime_adapter": attempt.runtime_adapter,
        "runtime_job_id": attempt.runtime_job_id,
        "conversation_id": attempt.conversation_id,
        "runtime_cursor": attempt.runtime_cursor,
        "workspace_ref": attempt.workspace_ref,
        "startup_mode": attempt.startup_mode,
        "startup_capability_key": attempt.startup_capability_key,
        "startup_prompt": attempt.startup_prompt,
        "output_targets": attempt.output_targets_json,
        "error_code": attempt.error_code,
        "error_detail": attempt.error_detail,
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
