from __future__ import annotations

from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from flowweave.modules.cases.infrastructure.models import Case, CaseAgentTask, CaseStage
from flowweave.shared.application.transactions import finish
from flowweave.shared.errors import conflict, illegal, not_found
from flowweave.shared.schemas import CaseCreateWrite, CaseStageStateWrite


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
        "row_version": item.row_version,
        "stages": [_stage_dict(stage) for stage in stages],
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
