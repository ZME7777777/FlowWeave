from __future__ import annotations

from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from flowweave.modules.agent_sessions.public import conversations
from flowweave.modules.agent_workspaces import public as agent_workspaces
from flowweave.modules.cases.infrastructure.models import (
    Case,
    CaseAgentTask,
    CaseDecisionProposal,
    CaseFlowRun,
    CaseStage,
)
from flowweave.modules.orchestration import public as orchestration
from flowweave.modules.runs.infrastructure.models import (
    ArtifactVersion,
    FlowRun,
    NodeAttempt,
    NodeRun,
)
from flowweave.shared.application.transactions import finish
from flowweave.shared.database import now
from flowweave.shared.errors import DomainError, conflict, illegal, not_found
from flowweave.shared.schemas import (
    AttemptStartWrite,
    AutomaticRunCopyWrite,
    AutomaticRunStartWrite,
    CaseControlConversationWrite,
    CaseCreateWrite,
    CaseDecisionProposalWrite,
    CaseFlowRunWrite,
    CaseStageExecutionStartWrite,
    CaseStageFlowRunWrite,
    CaseStageLaunchWrite,
    CaseStageStateWrite,
    FlowRunRevisionDraftApplyWrite,
    FlowRunRevisionDraftCreateWrite,
    FlowRunRevisionDraftWrite,
    NodeRunCopyWrite,
    StepwiseNodeDraftStartWrite,
    StepwiseRunRecordCopyWrite,
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
        "flow_run_id": item.flow_run_id,
        "current_execution_flow_run_id": item.current_execution_flow_run_id,
        "execution_mode": item.execution_mode,
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


def _proposal_dict(item: CaseDecisionProposal) -> dict[str, Any]:
    return {
        "id": item.id,
        "case_id": item.case_id,
        "flow_run_id": item.flow_run_id,
        "node_run_id": item.node_run_id,
        "node_attempt_id": item.node_attempt_id,
        "proposal_key": item.proposal_key,
        "source_task_id": item.source_task_id,
        "action": item.action,
        "state": item.state,
        "reason": item.reason,
        "payload": item.payload_json,
        "result": item.result_json,
        "created_at": item.created_at.isoformat(),
        "executed_at": item.executed_at.isoformat() if item.executed_at else None,
    }


def _case_linked_run(db: Session, case_id: str, flow_run_id: str) -> FlowRun:
    linked = db.scalar(
        select(CaseFlowRun.id).where(
            CaseFlowRun.case_id == case_id,
            CaseFlowRun.flow_run_id == flow_run_id,
        )
    )
    if linked is None:
        raise conflict("FlowRun is not linked to this Case", flow_run_id=flow_run_id)
    run = db.get(FlowRun, flow_run_id)
    if run is None:
        raise not_found("flow_run", flow_run_id)
    return run


def _proposal(db: Session, case_id: str, proposal_id: str) -> CaseDecisionProposal:
    item = db.get(CaseDecisionProposal, proposal_id)
    if item is None or item.case_id != case_id:
        raise not_found("case_decision_proposal", proposal_id)
    return item


def _case_flow_runs(db: Session, case_id: str) -> list[str]:
    return list(
        db.scalars(
            select(CaseFlowRun.flow_run_id)
            .where(CaseFlowRun.case_id == case_id)
            .order_by(CaseFlowRun.created_at)
        )
    )


def _write_case_files(db: Session, item: Case) -> None:
    if not item.control_work_directory_id:
        return
    workspace = agent_workspaces.ensure_default_agent_workspace(db)
    stages = tuple(
        {
            "id": stage.id,
            "stage_key": stage.stage_key,
            "name": stage.name,
            "objective": stage.objective,
            "position": stage.position,
            "state": stage.state,
            "execution_mode": stage.execution_mode,
            "flow_run_id": stage.flow_run_id,
            "current_execution_flow_run_id": stage.current_execution_flow_run_id,
        }
        for stage in db.scalars(
            select(CaseStage).where(CaseStage.case_id == item.id).order_by(CaseStage.position)
        )
    )
    agent_workspaces.write_case_files(
        db,
        workspace.id,
        item.control_work_directory_id,
        case_id=item.id,
        name=item.name,
        objective=item.objective,
        state=item.state,
        stages=stages,
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
    proposals = db.scalars(
        select(CaseDecisionProposal)
        .where(CaseDecisionProposal.case_id == item.id)
        .order_by(CaseDecisionProposal.created_at.desc())
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
        "decision_proposals": [_proposal_dict(proposal) for proposal in proposals],
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
    _write_case_files(db, item)
    finish(db)
    return _case_dict(db, item)


def list_case_stage_templates(db: Session, case_id: str) -> list[dict[str, Any]]:
    """List reusable run records that can become an isolated Case execution."""

    item = _case(db, case_id)
    bound_ids = set(db.scalars(select(CaseStage.flow_run_id).where(CaseStage.case_id == item.id)))
    templates: list[dict[str, Any]] = []
    for run in db.scalars(
        select(FlowRun)
        .where(FlowRun.run_mode.in_(("MANUAL", "AUTOMATIC")))
        .order_by(FlowRun.started_at.desc())
    ):
        if run.id in bound_ids:
            continue
        if run.run_mode == "MANUAL":
            if run.parent_flow_run_id is None:
                continue
            try:
                readiness = orchestration.validate_stepwise_record_template(db, run.id)
            except DomainError:
                continue
            templates.append(
                {
                    "id": run.id,
                    "name": run.name,
                    "execution_mode": "STEPWISE",
                    "state": run.state,
                    "parent_flow_run_id": run.parent_flow_run_id,
                    **readiness,
                }
            )
        else:
            plan = dict(run.automation_plan_json or {})
            if not plan.get("start_node_key") or not plan.get("readiness", {}).get("ready"):
                continue
            templates.append(
                {
                    "id": run.id,
                    "name": run.name,
                    "execution_mode": "CONTINUOUS",
                    "state": run.state,
                    "parent_flow_run_id": run.parent_flow_run_id,
                    "start_node_key": plan["start_node_key"],
                    "reachable_node_keys": list(plan.get("reachable_node_keys") or []),
                }
            )
    return templates


def bind_case_stage_flow_run(
    db: Session, case_id: str, stage_id: str, payload: CaseStageFlowRunWrite
) -> dict[str, Any]:
    item = _case(db, case_id)
    if item.state in {"COMPLETED", "ARCHIVED"}:
        raise illegal("terminal case cannot bind stage FlowRuns", state=item.state)
    stage = _stage(db, item, stage_id)
    run = db.get(FlowRun, payload.flow_run_id)
    if run is None:
        raise not_found("flow_run", payload.flow_run_id)
    expected_mode = "MANUAL" if payload.execution_mode == "STEPWISE" else "AUTOMATIC"
    if run.run_mode != expected_mode:
        raise conflict(
            "FlowRun mode does not match Case stage execution mode",
            expected=expected_mode,
            actual=run.run_mode,
        )
    if payload.execution_mode == "STEPWISE":
        if run.parent_flow_run_id is None:
            raise conflict("Case stepwise templates must be existing stepwise run records")
        orchestration.validate_stepwise_record_template(db, run.id)
    if stage.flow_run_id and stage.flow_run_id != run.id:
        raise conflict("Case stage is already bound to a FlowRun", flow_run_id=stage.flow_run_id)
    stage.flow_run_id = run.id
    stage.execution_mode = payload.execution_mode
    item.row_version += 1
    _write_case_files(db, item)
    finish(db)
    return _case_dict(db, item)


def launch_case_stage(
    db: Session, case_id: str, stage_id: str, payload: CaseStageLaunchWrite
) -> dict[str, Any]:
    """Clone a configured stage template into this Case's next execution record."""

    item = _case(db, case_id)
    if item.state in {"COMPLETED", "ARCHIVED"}:
        raise illegal("terminal case cannot launch stages", state=item.state)
    stage = _stage(db, item, stage_id)
    if stage.state != "ACTIVE":
        raise conflict("only the active Case stage can launch", state=stage.state)
    if not stage.flow_run_id:
        raise conflict("Case stage has no configured FlowRun template", stage_id=stage.id)
    template = db.get(FlowRun, stage.flow_run_id)
    if template is None:
        raise not_found("flow_run", stage.flow_run_id)
    if stage.execution_mode == "STEPWISE":
        if template.parent_flow_run_id is None:
            raise conflict(
                "Case stepwise template is not a stepwise record", flow_run_id=template.id
            )
        orchestration.validate_stepwise_record_template(db, template.id)
        execution = orchestration.copy_nested_stepwise_run_record(
            db,
            template.parent_flow_run_id,
            template.id,
            StepwiseRunRecordCopyWrite(name=payload.name),
        )
    else:
        if template.run_mode != "AUTOMATIC":
            raise conflict(
                "Case continuous template is not an automatic run", flow_run_id=template.id
            )
        execution = orchestration.copy_automatic_run_draft(
            db, template.id, AutomaticRunCopyWrite(name=payload.name)
        )
    execution_id = str(execution["id"])
    stage.current_execution_flow_run_id = execution_id
    if (
        db.scalar(
            select(CaseFlowRun.id).where(
                CaseFlowRun.case_id == item.id,
                CaseFlowRun.flow_run_id == execution_id,
            )
        )
        is None
    ):
        db.add(CaseFlowRun(case_id=item.id, flow_run_id=execution_id))
    item.row_version += 1
    _write_case_files(db, item)
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


def project_flow_run_event(
    db: Session,
    flow_run_id: str,
    event_type: str,
    *,
    node_run_id: str | None = None,
    node_attempt_id: str | None = None,
    payload: dict[str, Any] | None = None,
) -> None:
    """Expose terminal runtime facts to Case coordination without executing decisions."""

    if event_type not in {
        "NODE_RUN_COMPLETED",
        "RUNTIME_FAILED",
        "FLOW_RUN_COMPLETED",
        "FLOW_RUN_CANCELLED",
    }:
        return
    case_ids = db.scalars(
        select(CaseFlowRun.case_id).where(CaseFlowRun.flow_run_id == flow_run_id)
    ).all()
    for case_id in case_ids:
        task_key = f"event:{event_type}:{node_attempt_id or flow_run_id}"
        if (
            db.scalar(
                select(CaseAgentTask.id).where(
                    CaseAgentTask.case_id == case_id,
                    CaseAgentTask.task_key == task_key,
                )
            )
            is not None
        ):
            continue
        db.add(
            CaseAgentTask(
                case_id=case_id,
                task_key=task_key,
                task_type="REVIEW",
                flow_run_id=flow_run_id,
                node_attempt_id=node_attempt_id,
                input_context_json={
                    "event_type": event_type,
                    "node_run_id": node_run_id,
                    "node_attempt_id": node_attempt_id,
                    "event": payload or {},
                },
            )
        )
    db.flush()


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


def start_case_stage_execution(
    db: Session,
    case_id: str,
    stage_id: str,
    payload: CaseStageExecutionStartWrite,
) -> dict[str, Any]:
    """Start only the current Case-owned execution copy through audited services."""

    item = _case(db, case_id)
    if item.state in {"COMPLETED", "ARCHIVED"}:
        raise illegal("terminal case cannot start stages", state=item.state)
    stage = _stage(db, item, stage_id)
    if stage.state != "ACTIVE":
        raise conflict("only the active Case stage can start", state=stage.state)
    if not stage.current_execution_flow_run_id:
        raise conflict("Case stage has no execution copy", stage_id=stage.id)
    run = _case_linked_run(db, item.id, stage.current_execution_flow_run_id)
    proposal_key = f"stage-start:{stage.id}:{run.id}"
    existing = db.scalar(
        select(CaseDecisionProposal).where(
            CaseDecisionProposal.case_id == item.id,
            CaseDecisionProposal.proposal_key == proposal_key,
        )
    )
    if existing is not None:
        return _proposal_dict(existing)
    proposal = CaseDecisionProposal(
        case_id=item.id,
        flow_run_id=run.id,
        proposal_key=proposal_key,
        action="START_STAGE_EXECUTION",
        reason=payload.reason.strip(),
        payload_json={"stage_id": stage.id, "execution_mode": stage.execution_mode},
    )
    db.add(proposal)
    db.flush()
    return _execute_case_stage_start(db, item, stage, run, proposal)


def _execute_case_stage_start(
    db: Session,
    item: Case,
    stage: CaseStage,
    run: FlowRun,
    proposal: CaseDecisionProposal,
) -> dict[str, Any]:
    try:
        if stage.execution_mode == "STEPWISE":
            template = orchestration.validate_stepwise_record_template(db, run.id)
            start_node_key = str(template["start_node_key"])
            draft = dict(
                (run.automation_plan_json or {}).get("node_drafts", {}).get(start_node_key) or {}
            )
            result: dict[str, Any] = {
                "attempt": orchestration.start_stepwise_node_draft(
                    db,
                    run.id,
                    start_node_key,
                    StepwiseNodeDraftStartWrite(
                        expected_row_version=int(draft.get("row_version") or 1)
                    ),
                    f"case-stage-start:{proposal.id}",
                )
            }
        else:
            result = {
                "flow_run": orchestration.start_automatic_run(
                    db,
                    run.id,
                    AutomaticRunStartWrite(expected_row_version=run.row_version),
                    f"case-stage-start:{proposal.id}",
                )
            }
    except Exception as exc:
        proposal.state = "FAILED"
        proposal.result_json = {"code": getattr(exc, "code", "CASE_STAGE_START_FAILED")}
        proposal.executed_at = now()
        finish(db)
        raise
    proposal.state = "EXECUTED"
    proposal.result_json = result
    proposal.executed_at = now()
    item.row_version += 1
    _write_case_files(db, item)
    finish(db)
    return _proposal_dict(proposal)


def create_case_decision_proposal(
    db: Session, case_id: str, payload: CaseDecisionProposalWrite
) -> dict[str, Any]:
    item = _case(db, case_id)
    if item.state in {"COMPLETED", "ARCHIVED"}:
        raise illegal("terminal case cannot create decisions", state=item.state)
    existing = db.scalar(
        select(CaseDecisionProposal).where(
            CaseDecisionProposal.case_id == item.id,
            CaseDecisionProposal.proposal_key == payload.proposal_key,
        )
    )
    if existing is not None:
        return _proposal_dict(existing)
    run = _case_linked_run(db, item.id, payload.flow_run_id)
    if payload.source_task_id:
        task = db.get(CaseAgentTask, payload.source_task_id)
        if task is None or task.case_id != item.id:
            raise not_found("case_agent_task", payload.source_task_id)
    node_run = None
    if payload.node_run_id:
        node_run = db.get(NodeRun, payload.node_run_id)
        if node_run is None or node_run.flow_run_id != run.id:
            raise not_found("node_run", payload.node_run_id)
    manual_actions = {"CONTINUE_STEPWISE", "RERUN_STEPWISE_NODE", "REVISE_UNSTARTED_RUNTIME"}
    if payload.action in manual_actions and run.run_mode != "MANUAL":
        raise illegal("decision is only available for stepwise FlowRuns", state=run.state)
    if payload.action == "COPY_AUTOMATIC_RUN" and run.run_mode != "AUTOMATIC":
        raise illegal("copy decision is only available for continuous FlowRuns", state=run.state)
    if payload.action in {"CONTINUE_STEPWISE", "RERUN_STEPWISE_NODE"} and node_run is None:
        raise conflict("decision requires a node run", action=payload.action)
    proposal = CaseDecisionProposal(
        case_id=item.id,
        flow_run_id=run.id,
        node_run_id=node_run.id if node_run else None,
        node_attempt_id=payload.node_attempt_id,
        proposal_key=payload.proposal_key,
        source_task_id=payload.source_task_id,
        action=payload.action,
        reason=payload.reason.strip(),
        payload_json=payload.payload,
    )
    db.add(proposal)
    finish(db)
    return execute_case_decision_proposal(db, item.id, proposal.id)


def execute_case_decision_proposal(db: Session, case_id: str, proposal_id: str) -> dict[str, Any]:
    proposal = _proposal(db, _case(db, case_id).id, proposal_id)
    if proposal.state != "PROPOSED":
        raise illegal("Case decision has already executed", state=proposal.state)
    run = _case_linked_run(db, proposal.case_id, proposal.flow_run_id)
    try:
        if proposal.action == "CONTINUE_STEPWISE":
            if proposal.node_attempt_id is None:
                raise conflict("continue decision has no node attempt")
            attempt = db.get(NodeAttempt, proposal.node_attempt_id)
            if attempt is None or (
                proposal.node_run_id is not None and attempt.node_run_id != proposal.node_run_id
            ):
                raise not_found("node_attempt", proposal.node_attempt_id)
            start_payload = AttemptStartWrite.model_validate(proposal.payload_json or {})
            result: dict[str, Any] = {
                "attempt": orchestration.confirm_start(
                    db,
                    attempt.id,
                    start_payload,
                    f"case-continue:{proposal.id}",
                )
            }
        elif proposal.action == "STOP_FLOW_RUN":
            result = {"flow_run": orchestration.cancel_run(db, run.id, f"case-stop:{proposal.id}")}
        elif proposal.action == "RERUN_STEPWISE_NODE":
            if proposal.node_run_id is None:
                raise conflict("rerun decision has no node run")
            copy_payload = NodeRunCopyWrite.model_validate(proposal.payload_json or {})
            result = {
                "node_run": orchestration.copy_node_run(
                    db, run.id, proposal.node_run_id, copy_payload
                )
            }
        elif proposal.action == "REVISE_UNSTARTED_RUNTIME":
            definition = proposal.payload_json.get("definition")
            if not isinstance(definition, dict):
                raise conflict("runtime revision decision requires a definition")
            draft = orchestration.create_flow_run_revision_draft(
                db,
                run.id,
                FlowRunRevisionDraftCreateWrite(reason=proposal.reason),
            )
            updated = orchestration.update_flow_run_revision_draft(
                db,
                run.id,
                str(draft["id"]),
                FlowRunRevisionDraftWrite(
                    expected_row_version=int(draft["row_version"]),
                    definition=definition,
                    reason=proposal.reason,
                ),
            )
            result = {
                "revision": orchestration.apply_flow_run_revision_draft(
                    db,
                    run.id,
                    str(updated["id"]),
                    FlowRunRevisionDraftApplyWrite(
                        expected_row_version=int(updated["row_version"])
                    ),
                    f"case-revision:{proposal.id}",
                )
            }
        elif proposal.action == "COPY_AUTOMATIC_RUN":
            copy_payload = AutomaticRunCopyWrite.model_validate(proposal.payload_json or {})
            copied = orchestration.copy_automatic_run_draft(db, run.id, copy_payload)
            if (
                db.scalar(
                    select(CaseFlowRun.id).where(
                        CaseFlowRun.case_id == proposal.case_id,
                        CaseFlowRun.flow_run_id == copied["id"],
                    )
                )
                is None
            ):
                db.add(CaseFlowRun(case_id=proposal.case_id, flow_run_id=copied["id"]))
            result = {"flow_run": copied}
    except Exception as exc:
        proposal.state = "FAILED"
        proposal.result_json = {"code": getattr(exc, "code", "CASE_DECISION_EXECUTION_FAILED")}
        proposal.executed_at = now()
        finish(db)
        raise
    proposal.state = "EXECUTED"
    proposal.result_json = result
    proposal.executed_at = now()
    finish(db)
    return _proposal_dict(proposal)


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
    _write_case_files(db, item)
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
            "当前工作目录是此事项的专属目录。先读取 case.toml 和 memory.md；"
            "case.toml 是平台投影的当前事项、阶段和 FlowRun 摘要，"
            "memory.md 用于沉淀可复用工作记忆。\n\n"
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
