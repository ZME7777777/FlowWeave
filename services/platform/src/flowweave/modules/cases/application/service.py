from __future__ import annotations

from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from flowweave.modules.agent_sessions.public import conversations
from flowweave.modules.agent_workspaces import public as agent_workspaces
from flowweave.modules.cases.infrastructure.models import (
    Case,
    CaseAgentTask,
    CaseFlowRun,
    CaseStage,
)
from flowweave.modules.runs.infrastructure.models import ArtifactVersion, FlowRun, NodeAttempt
from flowweave.shared.application.transactions import finish
from flowweave.shared.errors import conflict, illegal, not_found
from flowweave.shared.schemas import (
    CaseControlConversationWrite,
    CaseCreateWrite,
    CaseFlowRunWrite,
    CaseStageStateWrite,
)


def _case(db: Session, case_id: str) -> Case:
    item = db.get(Case, case_id)
    if item is None:
        raise not_found("case", case_id)
    return item


def _stage(db: Session, item: Case, stage_id: str) -> CaseStage:
    stage = db.get(CaseStage, stage_id)
    if stage is None or stage.case_id != item.id:
        raise not_found("case_stage", stage_id)
    return stage


def _stage_dict(item: CaseStage) -> dict[str, Any]:
    return {
        "id": item.id,
        "case_id": item.case_id,
        "stage_key": item.stage_key,
        "name": item.name,
        "objective": item.objective,
        "position": item.position,
        "state": item.state,
        "created_at": item.created_at.isoformat(),
        "updated_at": item.updated_at.isoformat(),
    }


def _task_dict(item: CaseAgentTask) -> dict[str, Any]:
    return {
        "id": item.id,
        "case_id": item.case_id,
        "stage_id": item.stage_id,
        "task_key": item.task_key,
        "task_type": item.task_type,
        "state": item.state,
        "flow_run_id": item.flow_run_id,
        "node_attempt_id": item.node_attempt_id,
        "input_context": item.input_context_json,
        "result": item.result_json,
        "created_at": item.created_at.isoformat(),
        "updated_at": item.updated_at.isoformat(),
    }


def _case_flow_runs(db: Session, case_id: str) -> list[str]:
    return list(
        db.scalars(
            select(CaseFlowRun.flow_run_id)
            .where(CaseFlowRun.case_id == case_id)
            .order_by(CaseFlowRun.created_at)
        )
    )


def _case_dict(db: Session, item: Case) -> dict[str, Any]:
    stages = db.scalars(
        select(CaseStage).where(CaseStage.case_id == item.id).order_by(CaseStage.position)
    ).all()
    tasks = db.scalars(
        select(CaseAgentTask)
        .where(CaseAgentTask.case_id == item.id)
        .order_by(CaseAgentTask.created_at.desc())
    ).all()
    return {
        "id": item.id,
        "name": item.name,
        "objective": item.objective,
        "state": item.state,
        "control_conversation_binding_id": item.control_conversation_binding_id,
        "control_work_directory_id": item.control_work_directory_id,
        "row_version": item.row_version,
        "stages": [_stage_dict(stage) for stage in stages],
        "flow_run_ids": _case_flow_runs(db, item.id),
        "tasks": [_task_dict(task) for task in tasks],
        "created_at": item.created_at.isoformat(),
        "updated_at": item.updated_at.isoformat(),
    }


def list_cases(db: Session) -> list[dict[str, Any]]:
    return [
        _case_dict(db, item) for item in db.scalars(select(Case).order_by(Case.updated_at.desc()))
    ]


def get_case(db: Session, case_id: str) -> dict[str, Any]:
    return _case_dict(db, _case(db, case_id))


def create_case(db: Session, payload: CaseCreateWrite) -> dict[str, Any]:
    item = Case(name=payload.name.strip(), objective=payload.objective.strip())
    db.add(item)
    db.flush()
    for position, stage in enumerate(payload.stages):
        db.add(
            CaseStage(
                case_id=item.id,
                stage_key=stage.stage_key,
                name=stage.name.strip(),
                objective=stage.objective.strip(),
                position=position,
                state="ACTIVE" if position == 0 else "PENDING",
            )
        )
    item.state = "ACTIVE" if payload.stages else "DRAFT"
    finish(db)
    return _case_dict(db, item)


def set_case_stage_state(
    db: Session, case_id: str, stage_id: str, payload: CaseStageStateWrite
) -> dict[str, Any]:
    item = _case(db, case_id)
    if item.row_version != payload.expected_case_row_version:
        raise conflict(
            "case was modified",
            expected=payload.expected_case_row_version,
            actual=item.row_version,
        )
    if item.state in {"COMPLETED", "ARCHIVED"}:
        raise illegal("terminal case cannot change stages", state=item.state)
    stage = _stage(db, item, stage_id)
    if payload.state == "ACTIVE":
        db.execute(
            CaseStage.__table__.update()
            .where(
                CaseStage.case_id == item.id, CaseStage.id != stage.id, CaseStage.state == "ACTIVE"
            )
            .values(state="PENDING")
        )
    stage.state = payload.state
    item.row_version += 1
    finish(db)
    return _case_dict(db, item)


def project_case_observations(db: Session, case_id: str, *, commit: bool = True) -> dict[str, Any]:
    """Project linked Run observation outputs into idempotent Case tasks."""

    item = _case(db, case_id)
    flow_run_ids = _case_flow_runs(db, item.id)
    if not flow_run_ids:
        return _case_dict(db, item)
    artifacts = db.execute(
        select(ArtifactVersion, NodeAttempt)
        .join(NodeAttempt, NodeAttempt.id == ArtifactVersion.producer_attempt_id)
        .where(
            ArtifactVersion.flow_run_id.in_(flow_run_ids),
            ArtifactVersion.source == "RUNTIME",
        )
        .order_by(ArtifactVersion.created_at)
    ).all()
    for artifact, attempt in artifacts:
        target = (attempt.output_targets_json or {}).get(artifact.field_key)
        if not isinstance(target, dict):
            continue
        scope = str(target.get("output_scope") or "FLOW")
        if scope not in {"OBSERVATION", "CASE"}:
            continue
        task_key = f"observation:{artifact.id}"
        if (
            db.scalar(
                select(CaseAgentTask.id).where(
                    CaseAgentTask.case_id == item.id,
                    CaseAgentTask.task_key == task_key,
                )
            )
            is not None
        ):
            continue
        db.add(
            CaseAgentTask(
                case_id=item.id,
                task_key=task_key,
                task_type="OBSERVE",
                flow_run_id=artifact.flow_run_id,
                node_attempt_id=attempt.id,
                input_context_json={
                    "artifact_id": artifact.id,
                    "field_key": artifact.field_key,
                    "display_name": str(target.get("display_name") or artifact.field_key),
                    "output_scope": scope,
                    "artifact_type": artifact.artifact_type,
                    "mime_type": artifact.mime_type,
                    "content_hash": artifact.content_hash,
                    "byte_size": artifact.byte_size,
                    "uri": artifact.uri,
                    "storage_key": artifact.storage_key,
                    "metadata": artifact.metadata_json or {},
                },
            )
        )
    if commit:
        finish(db)
    else:
        db.flush()
    return _case_dict(db, item)


def project_flow_run_observations(db: Session, flow_run_id: str) -> None:
    case_ids = db.scalars(
        select(CaseFlowRun.case_id).where(CaseFlowRun.flow_run_id == flow_run_id)
    ).all()
    for case_id in case_ids:
        project_case_observations(db, case_id, commit=False)


def attach_case_flow_run(db: Session, case_id: str, payload: CaseFlowRunWrite) -> dict[str, Any]:
    item = _case(db, case_id)
    run = db.get(FlowRun, payload.flow_run_id)
    if run is None or run.parent_flow_run_id is not None:
        raise not_found("flow_run", payload.flow_run_id)
    if item.state in {"COMPLETED", "ARCHIVED"}:
        raise illegal("terminal case cannot attach flow runs", state=item.state)
    existing = db.scalar(
        select(CaseFlowRun).where(
            CaseFlowRun.case_id == item.id,
            CaseFlowRun.flow_run_id == run.id,
        )
    )
    if existing is None:
        db.add(CaseFlowRun(case_id=item.id, flow_run_id=run.id))
        db.flush()
    return project_case_observations(db, item.id)


def create_case_control_conversation(
    db: Session,
    case_id: str,
    payload: CaseControlConversationWrite,
    idempotency_key: str,
) -> dict[str, Any]:
    item = _case(db, case_id)
    if item.control_conversation_binding_id:
        return _case_dict(db, item)
    workspace = agent_workspaces.ensure_default_agent_workspace(db)
    if item.control_work_directory_id is None:
        directory = agent_workspaces.create_case_work_directory(
            db, workspace.id, item.id, f"事项 · {item.name}"
        )
        item.control_work_directory_id = str(directory["id"])
    created = conversations.bootstrap_conversation(
        db,
        workspace.id,
        work_directory_id=item.control_work_directory_id,
        model_provider_id=payload.model_provider_id,
        model_name=payload.model_name,
        reasoning_effort=payload.reasoning_effort,
        capability_version_ids=tuple(payload.capability_version_ids),
        content=(
            f"你是事项“{item.name}”的协调 Agent。\n\n"
            f"事项目标：{item.objective or '尚未填写'}\n\n"
            "仅在当前事项工作目录中工作。基于可审计事实协助梳理阶段、风险和后续建议；"
            "不得直接修改流程模板或已启动的流程节点。"
        ),
        idempotency_key=idempotency_key,
    )
    item = _case(db, case_id)
    item.control_conversation_binding_id = str(created["conversation"]["id"])
    item.row_version += 1
    finish(db)
    return _case_dict(db, item)
