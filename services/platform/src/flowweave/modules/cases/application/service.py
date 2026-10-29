from __future__ import annotations

import mimetypes
from pathlib import PurePosixPath
from typing import Any
from uuid import uuid4

from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from flowweave.modules.agent_sessions.application.schedules import validate_template_configuration
from flowweave.modules.agent_sessions.infrastructure.models import AgentConversationBinding
from flowweave.modules.agent_sessions.public import conversations
from flowweave.modules.agent_workspaces import public as agent_workspaces
from flowweave.modules.agent_workspaces.application import work_directories
from flowweave.modules.agent_workspaces.infrastructure.models import AgentWorkDirectoryVersion
from flowweave.modules.cases.infrastructure.models import (
    Case,
    CaseAgentTask,
    CaseControlPlan,
    CaseControlRound,
    CaseDecisionProposal,
    CaseFlowRun,
    CaseStage,
)
from flowweave.modules.model_providers.application.service import list_providers
from flowweave.modules.orchestration import public as orchestration
from flowweave.modules.runs.infrastructure.models import (
    ArtifactVersion,
    FlowRun,
    NodeAttempt,
    NodeRun,
)
from flowweave.shared.application.transactions import finish, savepoint
from flowweave.shared.database import now
from flowweave.shared.errors import DomainError, conflict, illegal, not_found
from flowweave.shared.schemas import (
    AttemptStartWrite,
    AutomaticRunCopyWrite,
    AutomaticRunStartWrite,
    CaseControlConversationWrite,
    CaseControlPlanWrite,
    CaseControlSuggestionWrite,
    CaseCreateWrite,
    CaseDecisionProposalWrite,
    CaseFlowRunWrite,
    CasePathWrite,
    CaseSessionConfigWrite,
    CaseStageExecutionStartWrite,
    CaseStageFlowRunWrite,
    CaseStageLaunchWrite,
    CaseStageStateWrite,
    CaseUpdateWrite,
    NodeRunCopyWrite,
    StepwiseNodeDraftStartWrite,
    StepwiseRunRecordCopyWrite,
)


class CaseActionFailure(DomainError):
    """A dispatched action failed after its auditable result was recorded."""


def _case(db: Session, case_id: str, *, lock: bool = False) -> Case:
    item = (
        db.scalar(
            select(Case)
            .where(Case.id == case_id)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
        if lock
        else db.get(Case, case_id)
    )
    if item is None:
        raise not_found("case", case_id)
    return item


def require_case_action(item: Case, action: str) -> None:
    """Keep every Case command on the same pause and terminal boundary."""

    if item.state in {"COMPLETED", "ARCHIVED"}:
        raise illegal("terminal Case cannot execute actions", state=item.state, action=action)
    if item.state == "PAUSED" and action not in {"STOP_EXECUTION", "STOP_FLOW_RUN"}:
        raise DomainError(
            "CASE_PAUSED",
            "场景已暂停，请恢复后再执行此动作",
            409,
            {"case_id": item.id, "action": action},
        )


def _stage_start_result(proposal: CaseDecisionProposal) -> dict[str, Any]:
    if proposal.state == "EXECUTED":
        return _proposal_dict(proposal)
    code = str(proposal.result_json.get("code") or "CASE_STAGE_START_NOT_EXECUTED")
    raise DomainError(
        code,
        "阶段启动未成功，请检查执行记录后显式重试",
        409,
        {"decision_id": proposal.id, "state": proposal.state},
    )


def _stage(db: Session, item: Case, stage_id: str) -> CaseStage:
    stage = db.get(CaseStage, stage_id)
    if stage is None or stage.case_id != item.id:
        raise not_found("case_stage", stage_id)
    return stage


def _stage_execution_summary(db: Session, stage: CaseStage) -> dict[str, Any] | None:
    if not stage.current_execution_flow_run_id:
        return None
    run = db.get(FlowRun, stage.current_execution_flow_run_id)
    if run is None:
        return {"state": "MISSING", "message": "当前执行副本已不可用"}
    detail = orchestration.run_detail(db, run.id, include_artifacts=False)
    active_snapshot = next(
        (
            snapshot
            for snapshot in detail["snapshots"]
            if snapshot["id"] == detail["active_snapshot_id"]
        ),
        detail["snapshots"][-1] if detail["snapshots"] else None,
    )
    nodes = {
        str(node.get("instance_key") or ""): node
        for node in (active_snapshot or {}).get("definition", {}).get("nodes", [])
    }
    node_runs = list(detail["node_runs"])
    accepted_keys = {
        str(node_run["flow_node_snapshot_key"])
        for node_run in node_runs
        if node_run["state"] == "ACCEPTED"
    }
    terminal_attempt_states = {"ACCEPTED", "REJECTED", "CANCELLED"}
    active_node = next(
        (
            node_run
            for node_run in reversed(node_runs)
            if not node_run["attempts"]
            or node_run["attempts"][-1]["state"] not in terminal_attempt_states
        ),
        None,
    )
    active = None
    if active_node is not None:
        key = str(active_node["flow_node_snapshot_key"])
        node = nodes.get(key, {})
        attempt = active_node["attempts"][-1] if active_node["attempts"] else None
        active = {
            "node_key": key,
            "node_name": str(node.get("alias") or node.get("instance_key") or key),
            "state": attempt["state"] if attempt else active_node["state"],
            "error_code": attempt.get("error_code") if attempt else None,
        }
    ready_node_keys: list[str] = []
    if stage.execution_mode == "STEPWISE" and detail["state"] not in {"COMPLETED", "CANCELLED"}:
        definition = (active_snapshot or {}).get("definition", {})
        drafts = detail.get("stepwise_node_drafts") or {}
        started_keys = {str(node_run["flow_node_snapshot_key"]) for node_run in node_runs}
        for node_key in drafts:
            if node_key in started_keys:
                continue
            predecessors = {
                str(edge.get("source_instance_key") or "")
                for edge in definition.get("edges", [])
                if str(edge.get("target_instance_key") or "") == node_key
            }
            if predecessors.issubset(accepted_keys):
                ready_node_keys.append(node_key)
    return {
        "flow_run_id": run.id,
        "state": detail["state"],
        "progress": detail["progress"],
        "active_node": active,
        "ready_node_keys": sorted(ready_node_keys),
        "automatic_block": detail["automatic_block"],
        "finished_at": detail.get("finished_at"),
    }


def _stage_dict(db: Session, item: CaseStage) -> dict[str, Any]:
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
        "execution": _stage_execution_summary(db, item),
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


def _control_plan_dict(item: CaseControlPlan) -> dict[str, Any]:
    return {
        "id": item.id,
        "case_id": item.case_id,
        "stage_id": item.stage_id,
        "source_binding_id": item.source_binding_id,
        "execution_flow_run_id": item.execution_flow_run_id,
        "node_run_id": item.node_run_id,
        "node_attempt_id": item.node_attempt_id,
        "action": item.action,
        "state": item.state,
        "idempotency_key": item.idempotency_key,
        "retry_of_plan_id": item.retry_of_plan_id,
        "reason": item.reason,
        "input_snapshot": item.input_snapshot_json,
        "result": item.result_json,
        "error_code": item.error_code,
        "created_at": item.created_at.isoformat(),
        "validated_at": item.validated_at.isoformat() if item.validated_at else None,
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


def resolve_case_control_binding_scope(
    db: Session, binding_id: str, *, require_active_stage: bool = True
) -> dict[str, Any]:
    """Resolve a Binding to its one authorized Case scope without model input."""

    binding = db.get(AgentConversationBinding, binding_id)
    if binding is None or binding.host_kind != "AGENT_WORKSPACE":
        raise not_found("case_control_binding", binding_id)
    item = db.scalar(select(Case).where(Case.control_conversation_binding_id == binding.id))
    if item is None:
        raise not_found("case_control_binding", binding_id)
    if item.state in {"COMPLETED", "ARCHIVED"}:
        raise illegal("terminal Case has no control scope", state=item.state)
    if item.control_work_directory_id is None:
        raise conflict("Case control Binding has no working directory", case_id=item.id)
    directory_version = db.get(AgentWorkDirectoryVersion, binding.work_directory_version_id)
    if (
        directory_version is None
        or directory_version.work_directory_id != item.control_work_directory_id
    ):
        raise conflict("Case control Binding is outside its Case work directory", case_id=item.id)
    stage = db.scalar(
        select(CaseStage)
        .where(CaseStage.case_id == item.id, CaseStage.state == "ACTIVE")
        .order_by(CaseStage.position)
    )
    if require_active_stage and stage is None:
        raise conflict("Case has no active stage", case_id=item.id)
    return {
        "case": item,
        "stage": stage,
        "binding": binding,
        "execution_flow_run_id": stage.current_execution_flow_run_id if stage else None,
    }


def _current_stage_execution(db: Session, item: Case, run: FlowRun) -> CaseStage:
    stage = db.scalar(
        select(CaseStage).where(
            CaseStage.case_id == item.id,
            CaseStage.current_execution_flow_run_id == run.id,
        )
    )
    if stage is None or stage.state != "ACTIVE":
        raise conflict(
            "Case remediation may target only its active stage execution copy",
            flow_run_id=run.id,
        )
    return stage


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


def write_case_files(db: Session, item: Case) -> None:
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
    plans = tuple(
        _control_plan_dict(plan)
        for plan in db.scalars(
            select(CaseControlPlan)
            .where(CaseControlPlan.case_id == item.id)
            .order_by(CaseControlPlan.created_at.desc())
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
        control_plans=plans,
        description=item.description,
        materials=tuple(item.materials_json or []),
    )


def _case_dict(db: Session, item: Case) -> dict[str, Any]:
    from flowweave.modules.cases.application.feedback import delivery_status

    current_round = (
        db.get(CaseControlRound, item.current_control_round_id)
        if item.current_control_round_id
        else None
    )
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
    control_plans = db.scalars(
        select(CaseControlPlan)
        .where(CaseControlPlan.case_id == item.id)
        .order_by(CaseControlPlan.created_at.desc())
    ).all()
    return {
        "id": item.id,
        "name": item.name,
        "objective": item.objective,
        "description": item.description,
        "materials": item.materials_json or [],
        "work_directory_id": item.work_directory_id,
        "session_config": item.session_config_json or {},
        "state": item.state,
        "control_conversation_binding_id": (
            item.control_conversation_binding_id
            if current_round is None or current_round.state == "STARTED"
            else None
        ),
        "control_work_directory_id": item.control_work_directory_id,
        "current_control_round_id": item.current_control_round_id,
        "control_rounds": [
            {
                "id": entry.id,
                "binding_id": entry.binding_id if entry.state == "STARTED" else None,
                "state": entry.state,
                "is_current": entry.id == item.current_control_round_id,
                "error_code": entry.error_code,
                "created_at": entry.created_at.isoformat(),
                "started_at": entry.started_at.isoformat() if entry.started_at else None,
            }
            for entry in db.scalars(
                select(CaseControlRound)
                .where(CaseControlRound.case_id == item.id)
                .order_by(CaseControlRound.created_at.desc(), CaseControlRound.id)
            )
        ],
        "row_version": item.row_version,
        "stages": [_stage_dict(db, stage) for stage in stages],
        "flow_run_ids": _case_flow_runs(db, item.id),
        "tasks": [_task_dict(task) for task in tasks],
        "decision_proposals": [_proposal_dict(proposal) for proposal in proposals],
        "feedback_deliveries": delivery_status(db, item.id),
        "control_plans": [_control_plan_dict(plan) for plan in control_plans],
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
    workspace = agent_workspaces.ensure_default_agent_workspace(db)
    if payload.work_directory_id:
        work_directories.get_work_directory(db, workspace.id, payload.work_directory_id)
    if any(material.kind == "FILE" for material in payload.materials):
        raise DomainError("CASE_MATERIAL_INVALID", "请先保存 Case，再上传文件资料", 422)
    item = Case(
        name=payload.name.strip(),
        description=payload.description.strip(),
        objective=payload.objective.strip(),
        work_directory_id=payload.work_directory_id,
        materials_json=[material.model_dump() for material in payload.materials],
    )
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
    directory = agent_workspaces.create_case_work_directory(
        db, workspace.id, item.id, f"Case · {item.name[:120]} · {item.id[:8]}"
    )
    item.control_work_directory_id = str(directory["id"])
    write_case_files(db, item)
    finish(db)
    return _case_dict(db, item)


def _editable_case(db: Session, case_id: str, expected_row_version: int) -> Case:
    item = db.scalar(select(Case).where(Case.id == case_id).with_for_update())
    if item is None:
        raise not_found("case", case_id)
    if item.row_version != expected_row_version:
        raise conflict("Case 已更新，请刷新后重试")
    return item


def update_case(db: Session, case_id: str, payload: CaseUpdateWrite) -> dict[str, Any]:
    item = _editable_case(db, case_id, payload.expected_row_version)
    if payload.name.strip() != item.name or payload.work_directory_id != item.work_directory_id:
        raise DomainError("CASE_IDENTITY_FROZEN", "名称和工作区创建后不可修改", 409)
    if (
        payload.description.strip() != item.description
        or payload.objective.strip() != item.objective
    ):
        raise DomainError("CASE_IDENTITY_FROZEN", "说明和目标创建后不可修改", 409)
    known_files = {
        material["value"] for material in item.materials_json or [] if material["kind"] == "FILE"
    }
    if any(
        material.kind == "FILE" and material.value not in known_files
        for material in payload.materials
    ):
        raise DomainError("CASE_MATERIAL_INVALID", "文件资料必须已上传到当前 Case", 422)
    item.name = payload.name.strip()
    item.description = payload.description.strip()
    item.objective = payload.objective.strip()
    item.work_directory_id = payload.work_directory_id
    changed = item.materials_json != [material.model_dump() for material in payload.materials]
    item.materials_json = [material.model_dump() for material in payload.materials]
    item.row_version += 1
    if changed:
        from flowweave.modules.cases.application.feedback import record_case_change

        record_case_change(db, item, "MATERIALS_CHANGED")
    write_case_files(db, item)
    finish(db)
    return _case_dict(db, item)


def upload_case_material(
    db: Session, case_id: str, expected_row_version: int, filename: str, content: bytes
) -> dict[str, Any]:
    item = _editable_case(db, case_id, expected_row_version)
    if len(content) > 64 * 1024 * 1024:
        raise DomainError("CASE_MATERIAL_TOO_LARGE", "文件超过 64 MB", 413)
    if len(item.materials_json or []) >= 100:
        raise DomainError("CASE_MATERIAL_LIMIT", "最多添加 100 项资料", 422)
    if (
        not filename
        or len(filename) > 220
        or PurePosixPath(filename).name != filename
        or "\\" in filename
        or "\x00" in filename
    ):
        raise DomainError("CASE_MATERIAL_INVALID", "文件名无效", 422)
    workspace = agent_workspaces.ensure_default_agent_workspace(db)
    if not item.control_work_directory_id:
        directory = agent_workspaces.create_case_work_directory(
            db, workspace.id, item.id, f"Case · {item.name[:120]} · {item.id[:8]}"
        )
        item.control_work_directory_id = str(directory["id"])
    stored_path = agent_workspaces.write_case_material_file(db, workspace.id, item.id, content)
    item.materials_json = [
        *(item.materials_json or []),
        {
            "kind": "FILE",
            "name": filename,
            "value": stored_path,
        },
    ]
    item.row_version += 1
    from flowweave.modules.cases.application.feedback import record_case_change

    record_case_change(db, item, "MATERIALS_CHANGED")
    write_case_files(db, item)
    finish(db)
    return _case_dict(db, item)


def read_case_material(db: Session, case_id: str, value: str) -> tuple[bytes, str, str]:
    item = _case(db, case_id)
    material = next(
        (
            entry
            for entry in item.materials_json or []
            if entry["kind"] == "FILE" and entry["value"] == value
        ),
        None,
    )
    if material is None:
        raise not_found("case_material", value)
    workspace = agent_workspaces.ensure_default_agent_workspace(db)
    content = agent_workspaces.read_case_material_file(db, workspace.id, item.id, value)
    mime = mimetypes.guess_type(material["name"])[0] or "application/octet-stream"
    if mime in {"text/html", "application/xhtml+xml", "image/svg+xml"}:
        mime = "text/plain"
    return content, material["name"], mime


def validate_case_session_config(
    db: Session, payload: CaseControlConversationWrite
) -> dict[str, Any]:
    provider = next((p for p in list_providers(db) if p["id"] == payload.model_provider_id), None)
    model = next(
        (
            m
            for m in (provider or {}).get("models", [])
            if m["model_name"] == payload.model_name and m["enabled"]
        ),
        None,
    )
    if not provider or not provider["available_for_nodes"] or not model:
        raise DomainError("CASE_MODEL_UNAVAILABLE", "请选择可用的供应商和模型", 422)
    if payload.reasoning_effort and payload.reasoning_effort not in model.get(
        "supported_reasoning_efforts", []
    ):
        raise DomainError("CASE_REASONING_INVALID", "模型不支持所选思考程度", 422)
    capabilities, credentials = validate_template_configuration(
        db, tuple(payload.capability_version_ids), tuple(payload.credential_ids)
    )
    return {
        **payload.model_dump(),
        "capability_version_ids": list(capabilities),
        "credential_ids": list(credentials),
    }


def save_case_session_config(
    db: Session, case_id: str, payload: CaseSessionConfigWrite
) -> dict[str, Any]:
    item = _editable_case(db, case_id, payload.expected_row_version)
    config = CaseControlConversationWrite.model_validate(
        payload.model_dump(exclude={"expected_row_version"})
    )
    validated = validate_case_session_config(db, config)
    changed = item.session_config_json != validated
    item.session_config_json = validated
    item.row_version += 1
    if changed:
        from flowweave.modules.cases.application.feedback import record_case_change

        record_case_change(db, item, "SESSION_CONFIG_CHANGED")
    finish(db)
    return _case_dict(db, item)


def set_case_coordination_state(
    db: Session, case_id: str, expected_row_version: int, state: str
) -> dict[str, Any]:
    item = _editable_case(db, case_id, expected_row_version)
    if state not in {"ACTIVE", "PAUSED"} or item.state not in {"ACTIVE", "PAUSED"}:
        raise DomainError("CASE_STATE_TRANSITION_INVALID", "当前场景不可暂停或恢复", 409)
    if item.state != state:
        item.state = state
        item.row_version += 1
        if state == "ACTIVE":
            from flowweave.modules.cases.application.feedback import record_case_change

            record_case_change(db, item, "CASE_RESUMED")
        write_case_files(db, item)
    finish(db)
    return _case_dict(db, item)


def save_case_path(db: Session, case_id: str, payload: CasePathWrite) -> dict[str, Any]:
    item = _editable_case(db, case_id, payload.expected_row_version)
    if item.state in {"COMPLETED", "ARCHIVED"}:
        raise illegal("terminal case cannot change its path", state=item.state)
    stages = list(db.scalars(select(CaseStage).where(CaseStage.case_id == item.id)))
    by_id = {stage.id: stage for stage in stages}
    ids = [card.id for card in payload.cards if card.id]
    if len(ids) != len(set(ids)) or any(id_ not in by_id for id_ in ids):
        raise DomainError("CASE_PATH_INVALID", "路径卡片重复或不属于当前 Case", 422)
    removed = [stage for stage in stages if stage.id not in ids]
    removed_ids = [stage.id for stage in removed]
    if any(stage.current_execution_flow_run_id for stage in removed) or (
        removed_ids
        and (
            db.scalar(
                select(CaseControlPlan.id).where(CaseControlPlan.stage_id.in_(removed_ids)).limit(1)
            )
            or db.scalar(
                select(CaseAgentTask.id).where(CaseAgentTask.stage_id.in_(removed_ids)).limit(1)
            )
        )
    ):
        raise DomainError("CASE_PATH_IN_USE", "已有执行历史的阶段不可移除", 409)
    candidates = {
        candidate["id"]: candidate for candidate in list_case_stage_templates(db, item.id)
    }
    new_ids = [card.flow_run_id for card in payload.cards if card.flow_run_id]
    if len(new_ids) != len(set(new_ids)):
        raise DomainError("CASE_PATH_INVALID", "不能重复添加同一运行记录", 422)
    ordered: list[CaseStage] = []
    for card in payload.cards:
        if card.id:
            ordered.append(by_id[card.id])
            continue
        candidate = candidates.get(card.flow_run_id)
        if candidate is None:
            raise DomainError(
                "CASE_TEMPLATE_UNAVAILABLE", "运行记录不可用或已被关联，请重新选择", 409
            )
        ordered.append(
            CaseStage(
                case_id=item.id,
                stage_key=f"run-{uuid4()}",
                flow_run_id=candidate["id"],
                name=candidate["name"],
                objective="",
                execution_mode=candidate["execution_mode"],
                state="PENDING",
            )
        )
    # Unique positions stay valid throughout reordering, including a complete swap.
    temporary_position = max((stage.position for stage in stages), default=0) + len(stages) + 1
    for index, stage in enumerate(stages):
        stage.position = temporary_position + index
    db.flush()
    for stage in removed:
        db.delete(stage)
    db.flush()
    for position, stage in enumerate(ordered):
        stage.position = position
        db.add(stage)
    if ordered and not any(stage.state == "ACTIVE" for stage in ordered):
        next((stage for stage in ordered if stage.state == "PENDING"), ordered[0]).state = "ACTIVE"
    item.state = "ACTIVE" if ordered else "DRAFT"
    item.row_version += 1
    db.flush()
    write_case_files(db, item)
    finish(db)
    return _case_dict(db, item)


def _case_has_active_execution(db: Session, case_id: str) -> bool:
    execution_ids = list(
        db.scalars(
            select(CaseStage.current_execution_flow_run_id).where(
                CaseStage.case_id == case_id,
                CaseStage.current_execution_flow_run_id.is_not(None),
            )
        )
    )
    return bool(
        execution_ids
        and db.scalar(
            select(FlowRun.id).where(
                FlowRun.id.in_(execution_ids), FlowRun.state.in_(("ACTIVE", "WAITING_HUMAN"))
            )
        )
    )


def _case_has_unresolved_control_plan(db: Session, case_id: str) -> bool:
    return (
        db.scalar(
            select(CaseControlPlan.id).where(
                CaseControlPlan.case_id == case_id,
                CaseControlPlan.state.in_(("PENDING", "VALIDATED", "EXECUTING")),
            )
        )
        is not None
    )


def delete_case_control_conversation(
    db: Session, case_id: str, expected_binding_id: str | None = None
) -> None:
    item = _case(db, case_id, lock=True)
    if expected_binding_id and expected_binding_id != item.control_conversation_binding_id:
        raise conflict("Case coordinator has changed; refresh before deleting")
    current_round = (
        db.get(CaseControlRound, item.current_control_round_id)
        if item.current_control_round_id
        else None
    )
    if current_round and current_round.state == "PENDING":
        raise DomainError("CASE_AGENT_START_PENDING", "协调会话仍在创建，请稍后重试", 409)
    binding_id = item.control_conversation_binding_id
    if not binding_id:
        return
    workspace = agent_workspaces.ensure_default_agent_workspace(db)
    binding = db.get(AgentConversationBinding, binding_id)
    if binding is not None:
        conversations.delete_conversation(
            db,
            workspace.id,
            binding.id,
            f"delete-case-control-conversation:{case_id}:{binding.id}",
        )
    item.control_conversation_binding_id = None
    if current_round:
        current_round.state = "DELETED"
    item.current_control_round_id = None
    item.row_version += 1
    finish(db)


def delete_case(db: Session, case_id: str) -> None:
    item = _case(db, case_id, lock=True)
    if _case_has_active_execution(db, item.id):
        raise DomainError("CASE_EXECUTION_RUNNING", "事项仍有进行中的执行，停止后才能删除", 409)
    if _case_has_unresolved_control_plan(db, item.id):
        raise DomainError(
            "CASE_CONTROL_PLAN_PENDING", "事项仍有未完成的受控计划，完成或取消后才能删除", 409
        )
    workspace = agent_workspaces.ensure_default_agent_workspace(db)
    rounds = list(db.scalars(select(CaseControlRound).where(CaseControlRound.case_id == item.id)))
    if any(entry.state == "PENDING" for entry in rounds):
        raise DomainError("CASE_AGENT_START_PENDING", "协调会话仍在创建，请稍后重试", 409)
    binding_ids = {entry.binding_id for entry in rounds if entry.state != "DELETED"}
    if item.control_conversation_binding_id:
        binding_ids.add(item.control_conversation_binding_id)
    work_directory_id = item.control_work_directory_id
    for binding_id in binding_ids:
        binding = db.get(AgentConversationBinding, binding_id)
        if binding is not None:
            conversations.assert_conversation_stopped(db, workspace.id, binding.id)
    for binding_id in binding_ids:
        binding = db.get(AgentConversationBinding, binding_id)
        if binding is not None:
            conversations.delete_conversation(
                db,
                workspace.id,
                binding.id,
                f"delete-case:{item.id}:{binding.id}",
            )
    if work_directory_id:
        agent_workspaces.delete_case_work_directory(db, workspace.id, item.id, work_directory_id)
    db.execute(delete(CaseControlRound).where(CaseControlRound.case_id == item.id))
    db.execute(delete(CaseControlPlan).where(CaseControlPlan.case_id == item.id))
    db.execute(delete(CaseDecisionProposal).where(CaseDecisionProposal.case_id == item.id))
    db.execute(delete(CaseAgentTask).where(CaseAgentTask.case_id == item.id))
    from flowweave.modules.cases.application.materials import delete_case_artifact_references

    delete_case_artifact_references(db, item.id)
    db.execute(delete(CaseFlowRun).where(CaseFlowRun.case_id == item.id))
    db.execute(delete(CaseStage).where(CaseStage.case_id == item.id))
    db.delete(item)
    finish(db)


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
    write_case_files(db, item)
    finish(db)
    return _case_dict(db, item)


def list_case_stage_templates(db: Session, case_id: str) -> list[dict[str, Any]]:
    """List reusable run records that can become an isolated Case execution."""

    _case(db, case_id)
    bound_ids = set(db.scalars(select(CaseStage.flow_run_id)))
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
    write_case_files(db, item)
    finish(db)
    return _case_dict(db, item)


def launch_case_stage(
    db: Session, case_id: str, stage_id: str, payload: CaseStageLaunchWrite
) -> dict[str, Any]:
    """Clone a configured stage template into this Case's next execution record."""

    item = _case(db, case_id)
    require_case_action(item, "CREATE_EXECUTION_COPY")
    stage = _stage(db, item, stage_id)
    if stage.state != "ACTIVE":
        raise conflict("only the active Case stage can launch", state=stage.state)
    if stage.current_execution_flow_run_id:
        raise conflict(
            "Case stage already has a current execution copy",
            flow_run_id=stage.current_execution_flow_run_id,
        )
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
    write_case_files(db, item)
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
    run_event_cursor: int | None = None,
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
        task_key = (
            f"event:{run_event_cursor}"
            if run_event_cursor is not None
            else f"event:{event_type}:{node_attempt_id or flow_run_id}"
        )
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
                    "run_event_cursor": run_event_cursor,
                    "node_run_id": node_run_id,
                    "node_attempt_id": node_attempt_id,
                    "event": payload or {},
                },
            )
        )
    reconcile_case_execution_state(db, flow_run_id)
    db.flush()


def reconcile_case_execution_state(db: Session, flow_run_id: str) -> None:
    """Advance only the owning active Case stage from durable execution facts."""

    stages = list(
        db.scalars(select(CaseStage).where(CaseStage.current_execution_flow_run_id == flow_run_id))
    )
    for stage in stages:
        item = _case(db, stage.case_id)
        run = db.get(FlowRun, flow_run_id)
        if run is None or item.state in {"COMPLETED", "ARCHIVED"}:
            continue
        changed = False
        if run.state == "COMPLETED" and stage.state == "ACTIVE":
            stage.state = "COMPLETED"
            changed = True
            next_stage = db.scalar(
                select(CaseStage)
                .where(CaseStage.case_id == item.id, CaseStage.position > stage.position)
                .order_by(CaseStage.position)
            )
            if next_stage is None:
                item.state = "COMPLETED"
            elif next_stage.state == "PENDING":
                next_stage.state = "ACTIVE"
        elif run.state == "CANCELLED" and stage.state == "ACTIVE":
            item.state = "PAUSED"
            changed = True
        if changed:
            item.row_version += 1
            write_case_files(db, item)
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


def create_case_control_plan(
    db: Session, case_id: str, payload: CaseControlPlanWrite
) -> dict[str, Any]:
    """Persist a platform-owned Case command before a later validator executes it."""

    item = _case(db, case_id)
    if item.state in {"COMPLETED", "ARCHIVED"}:
        raise illegal("terminal case cannot create control plans", state=item.state)
    return _create_case_control_plan(
        db, item, _stage(db, item, payload.stage_id), payload, source_binding_id=None
    )


def ingest_case_control_suggestion(
    db: Session, binding_id: str, payload: CaseControlSuggestionWrite
) -> dict[str, Any]:
    """Translate a bounded control-session suggestion into one platform plan."""

    scope = resolve_case_control_binding_scope(db, binding_id)
    item = scope["case"]
    stage = scope["stage"]
    if not isinstance(item, Case) or not isinstance(stage, CaseStage):
        raise conflict("Case control Binding has no active stage")
    if payload.action == "RERUN_STEPWISE_NODE":
        raise illegal("suggested remediation action requires explicit human plan creation")
    if payload.action == "START_STEPWISE_NODE":
        raise illegal("stepwise continuation is derived from predecessor acceptance")
    return _create_case_control_plan(
        db,
        item,
        stage,
        CaseControlPlanWrite(
            stage_id=stage.id,
            action=payload.action,
            idempotency_key=f"suggestion:{binding_id}:{payload.suggestion_key}",
            reason=payload.reason,
            node_run_id=payload.node_run_id,
            node_attempt_id=payload.node_attempt_id,
            input_snapshot={"suggestion_key": payload.suggestion_key, "evidence": payload.evidence},
        ),
        source_binding_id=binding_id,
    )


def create_case_control_plan_for_binding(
    db: Session,
    binding_id: str,
    payload: CaseControlPlanWrite,
    *,
    retry_of_plan_id: str | None = None,
) -> dict[str, Any]:
    """Create a plan only inside the Case scope proven by its control Binding."""

    scope = resolve_case_control_binding_scope(db, binding_id)
    item = scope["case"]
    stage = scope["stage"]
    if not isinstance(item, Case) or not isinstance(stage, CaseStage):
        raise conflict("Case control Binding has no active stage")
    if payload.stage_id != stage.id:
        raise conflict(
            "control Binding may plan only for its active Case stage",
            expected_stage_id=stage.id,
            actual_stage_id=payload.stage_id,
        )
    return _create_case_control_plan(
        db, item, stage, payload, source_binding_id=binding_id, retry_of_plan_id=retry_of_plan_id
    )


def _create_case_control_plan(
    db: Session,
    item: Case,
    stage: CaseStage,
    payload: CaseControlPlanWrite,
    *,
    source_binding_id: str | None,
    retry_of_plan_id: str | None = None,
) -> dict[str, Any]:
    require_case_action(item, payload.action)
    existing = db.scalar(
        select(CaseControlPlan).where(
            CaseControlPlan.case_id == item.id,
            CaseControlPlan.idempotency_key == payload.idempotency_key,
        )
    )
    if existing is not None:
        if existing.source_binding_id != source_binding_id:
            raise conflict("control plan idempotency key belongs to another control scope")
        return _control_plan_dict(existing)
    execution_flow_run_id = stage.current_execution_flow_run_id
    if payload.action != "CREATE_EXECUTION_COPY" and not execution_flow_run_id:
        raise conflict("control plan requires a Case execution copy", action=payload.action)
    plan = CaseControlPlan(
        case_id=item.id,
        stage_id=stage.id,
        source_binding_id=source_binding_id,
        execution_flow_run_id=execution_flow_run_id,
        node_run_id=payload.node_run_id,
        node_attempt_id=payload.node_attempt_id,
        action=payload.action,
        idempotency_key=payload.idempotency_key,
        retry_of_plan_id=retry_of_plan_id,
        reason=payload.reason.strip(),
        input_snapshot_json={
            "case_state": item.state,
            "stage_state": stage.state,
            "stage_execution_mode": stage.execution_mode,
            "stage_template_flow_run_id": stage.flow_run_id,
            "stage_execution_flow_run_id": execution_flow_run_id,
            "requested": payload.input_snapshot,
        },
    )
    db.add(plan)
    write_case_files(db, item)
    finish(db)
    return _control_plan_dict(plan)


def execute_case_control_plan(db: Session, case_id: str, plan_id: str) -> dict[str, Any]:
    """Validate and dispatch one Case plan through established orchestration services."""

    item = _case(db, case_id)
    plan = db.get(CaseControlPlan, plan_id)
    if plan is None or plan.case_id != item.id:
        raise not_found("case_control_plan", plan_id)
    if plan.state in {"SUCCEEDED", "CANCELLED"}:
        return _control_plan_dict(plan)
    if plan.state == "FAILED":
        raise DomainError(
            plan.error_code or "CASE_CONTROL_PLAN_FAILED",
            "此控制动作已失败，请创建关联重试，不会重复执行原计划",
            409,
            {"plan_id": plan.id, "state": plan.state},
        )
    if plan.state == "EXECUTING":
        raise conflict("Case control plan is already executing", plan_id=plan.id)
    require_case_action(item, plan.action)
    stage = _stage(db, item, plan.stage_id)
    if stage.state != "ACTIVE":
        raise conflict("only the active Case stage can execute plans", state=stage.state)
    if plan.execution_flow_run_id != stage.current_execution_flow_run_id:
        raise conflict("plan no longer targets the current Case execution copy", plan_id=plan.id)
    plan.state = "VALIDATED"
    plan.validated_at = now()
    finish(db)
    try:
        plan.state = "EXECUTING"
        from flowweave.modules.cases.application.flow_tools import (
            configure_execution,
            validate_action_preconditions,
        )

        validate_action_preconditions(db, item, stage, plan)
        if plan.action == "CONFIGURE_EXECUTION":
            with savepoint(db):
                result = configure_execution(db, stage, plan)
        elif plan.action == "CREATE_EXECUTION_COPY":
            with savepoint(db):
                result = {"case": launch_case_stage(db, item.id, stage.id, CaseStageLaunchWrite())}
        elif plan.action == "START_EXECUTION":
            result = {
                "decision": start_case_stage_execution(
                    db,
                    item.id,
                    stage.id,
                    CaseStageExecutionStartWrite(reason=plan.reason),
                    retry_plan_id=plan.id if plan.retry_of_plan_id else None,
                )
            }
            decision = result["decision"]
            if decision.get("state") != "EXECUTED":
                raise DomainError(
                    "CASE_STAGE_START_NOT_EXECUTED",
                    "阶段启动动作未执行成功",
                    409,
                    {"decision_id": decision.get("id"), "state": decision.get("state")},
                )
        elif plan.action == "START_STEPWISE_NODE":
            if not stage.current_execution_flow_run_id:
                raise conflict("Case stage has no execution copy", stage_id=stage.id)
            run = _case_linked_run(db, item.id, stage.current_execution_flow_run_id)
            requested = plan.input_snapshot_json.get("requested", {})
            node_key = str(requested.get("flow_node_key") or "")
            draft_row_version = requested.get("draft_row_version")
            if not node_key or not isinstance(draft_row_version, int):
                raise conflict("stepwise plan lacks a frozen node draft reference", plan_id=plan.id)
            result = {
                "attempt": orchestration.start_stepwise_node_draft(
                    db,
                    run.id,
                    node_key,
                    StepwiseNodeDraftStartWrite(expected_row_version=draft_row_version),
                    f"case-plan-stepwise-start:{plan.id}",
                )
            }
        elif plan.action == "STOP_EXECUTION":
            if not stage.current_execution_flow_run_id:
                raise conflict("Case stage has no execution copy", stage_id=stage.id)
            result = {
                "flow_run": orchestration.cancel_run(
                    db, stage.current_execution_flow_run_id, f"case-plan-stop:{plan.id}"
                )
            }
        else:
            raise illegal("Case control plan action is not yet dispatchable", action=plan.action)
    except Exception as exc:
        plan.state = "FAILED"
        plan.error_code = str(getattr(exc, "code", "CASE_CONTROL_PLAN_FAILED"))
        plan.result_json = {"code": plan.error_code}
        if (
            isinstance(exc, DomainError)
            and plan.input_snapshot_json.get("requested", {}).get("expected_case_row_version")
            is not None
        ):
            plan.result_json["validation"] = {
                key: exc.details[key]
                for key in ("field", "fields", "field_key", "field_keys", "node_key", "node_keys")
                if key in exc.details
            }
        if isinstance(exc, DomainError) and exc.details.get("decision_id"):
            plan.result_json = {**plan.result_json, "decision_id": exc.details["decision_id"]}
        plan.executed_at = now()
        write_case_files(db, item)
        finish(db)
        if isinstance(exc, DomainError):
            raise CaseActionFailure(exc.code, exc.message, exc.status, exc.details) from exc
        raise CaseActionFailure(
            plan.error_code or "CASE_CONTROL_PLAN_FAILED",
            "场景控制动作执行失败",
            500,
            {"plan_id": plan.id},
        ) from exc
    plan.state = "SUCCEEDED"
    plan.result_json = result
    plan.error_code = None
    plan.executed_at = now()
    write_case_files(db, item)
    finish(db)
    return _control_plan_dict(plan)


def retry_case_control_plan(db: Session, case_id: str, plan_id: str) -> dict[str, Any]:
    """Create a new auditable attempt for one failed, still-authorized plan."""

    item = _case(db, case_id)
    failed = db.scalar(
        select(CaseControlPlan)
        .where(
            CaseControlPlan.id == plan_id,
            CaseControlPlan.case_id == item.id,
        )
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    if failed is None or failed.case_id != item.id:
        raise not_found("case_control_plan", plan_id)
    require_case_action(item, failed.action)
    if failed.state != "FAILED":
        raise conflict("only failed Case control plans can be retried", state=failed.state)
    if failed.action not in {
        "CREATE_EXECUTION_COPY",
        "START_EXECUTION",
        "START_STEPWISE_NODE",
        "STOP_EXECUTION",
    }:
        raise illegal("failed plan action is not eligible for human retry", action=failed.action)
    if failed.action == "CREATE_EXECUTION_COPY":
        stage = _stage(db, item, failed.stage_id)
        if stage.state != "ACTIVE" or stage.current_execution_flow_run_id is not None:
            raise conflict("copy retry no longer targets an unstarted active Case stage")
    else:
        stage = _current_stage_execution(
            db, item, _case_linked_run(db, item.id, str(failed.execution_flow_run_id or ""))
        )
    existing_retry = db.scalar(
        select(CaseControlPlan)
        .where(
            CaseControlPlan.case_id == item.id,
            CaseControlPlan.retry_of_plan_id == failed.id,
            CaseControlPlan.state.in_(("PENDING", "VALIDATED", "EXECUTING", "SUCCEEDED")),
        )
        .order_by(CaseControlPlan.created_at.desc(), CaseControlPlan.id.desc())
        .limit(1)
    )
    if existing_retry is not None:
        return _control_plan_dict(existing_retry)
    retry_number = (
        len(
            list(
                db.scalars(
                    select(CaseControlPlan.id).where(CaseControlPlan.retry_of_plan_id == failed.id)
                )
            )
        )
        + 1
    )
    requested = failed.input_snapshot_json.get("requested")
    return _create_case_control_plan(
        db,
        item,
        stage,
        CaseControlPlanWrite(
            stage_id=stage.id,
            action=failed.action,
            idempotency_key=f"retry:{failed.id}:{retry_number}",
            reason=failed.reason,
            node_run_id=failed.node_run_id,
            node_attempt_id=failed.node_attempt_id,
            input_snapshot=requested if isinstance(requested, dict) else {},
        ),
        source_binding_id=failed.source_binding_id,
        retry_of_plan_id=failed.id,
    )


def advance_case_stepwise_execution(db: Session, flow_run_id: str) -> None:
    """Plan and dispatch every newly-ready node of active Case stepwise copies."""

    stages = list(
        db.scalars(
            select(CaseStage).where(
                CaseStage.current_execution_flow_run_id == flow_run_id,
                CaseStage.execution_mode == "STEPWISE",
                CaseStage.state == "ACTIVE",
            )
        )
    )
    for stage in stages:
        item = _case(db, stage.case_id)
        if item.state != "ACTIVE":
            continue
        run = _case_linked_run(db, item.id, flow_run_id)
        detail = orchestration.run_detail(db, run.id, include_artifacts=False)
        snapshot = next(
            (
                candidate
                for candidate in detail["snapshots"]
                if candidate["id"] == detail["active_snapshot_id"]
            ),
            detail["snapshots"][-1] if detail["snapshots"] else None,
        )
        if snapshot is None:
            continue
        started_keys = {str(node_run["flow_node_snapshot_key"]) for node_run in detail["node_runs"]}
        accepted_keys = {
            str(node_run["flow_node_snapshot_key"])
            for node_run in detail["node_runs"]
            if node_run["state"] == "ACCEPTED"
        }
        for node_key, draft in (detail.get("stepwise_node_drafts") or {}).items():
            if node_key in started_keys:
                continue
            predecessors = {
                str(edge.get("source_instance_key") or "")
                for edge in snapshot["definition"].get("edges", [])
                if str(edge.get("target_instance_key") or "") == node_key
            }
            if not predecessors.issubset(accepted_keys):
                continue
            plan = _create_case_control_plan(
                db,
                item,
                stage,
                CaseControlPlanWrite(
                    stage_id=stage.id,
                    action="START_STEPWISE_NODE",
                    idempotency_key=f"stepwise-next:{run.id}:{node_key}",
                    reason=f"直接前驱已接受，启动已预配置节点 {node_key}。",
                    input_snapshot={
                        "flow_node_key": node_key,
                        "draft_row_version": int(draft.get("row_version") or 1),
                    },
                ),
                source_binding_id=None,
            )
            try:
                execute_case_control_plan(db, item.id, str(plan["id"]))
            except Exception:
                # The plan records its dispatch failure; accepting a predecessor
                # must not be rolled back by an autonomous successor failure.
                continue


def start_case_stage_execution(
    db: Session,
    case_id: str,
    stage_id: str,
    payload: CaseStageExecutionStartWrite,
    *,
    retry_plan_id: str | None = None,
) -> dict[str, Any]:
    """Start once; retries preserve failed decisions and cannot duplicate partial starts."""

    item = _case(db, case_id)
    require_case_action(item, "START_EXECUTION")
    stage = _stage(db, item, stage_id)
    if stage.state != "ACTIVE":
        raise conflict("only the active Case stage can start", state=stage.state)
    if not stage.current_execution_flow_run_id:
        raise conflict("Case stage has no execution copy", stage_id=stage.id)
    run = _case_linked_run(db, item.id, stage.current_execution_flow_run_id)
    # Match the orchestration lock so different retry requests cannot launch the
    # same stage concurrently with different command identities.
    db.refresh(run, with_for_update=True)
    db.refresh(item)
    db.refresh(stage)
    require_case_action(item, "START_EXECUTION")
    if stage.state != "ACTIVE" or stage.current_execution_flow_run_id != run.id:
        raise conflict("Case stage changed while waiting to start")
    proposal_key = f"stage-start:{stage.id}:{run.id}"
    previous = db.scalar(
        select(CaseDecisionProposal)
        .where(
            CaseDecisionProposal.case_id == item.id,
            CaseDecisionProposal.flow_run_id == run.id,
            CaseDecisionProposal.action == "START_STAGE_EXECUTION",
        )
        .order_by(CaseDecisionProposal.created_at.desc(), CaseDecisionProposal.id.desc())
        .limit(1)
    )
    retry_of_proposal_id: str | None = None
    if retry_plan_id:
        retry = db.get(CaseControlPlan, retry_plan_id)
        if retry is None or retry.case_id != item.id:
            raise not_found("case_control_plan", retry_plan_id)
        failed = db.get(CaseControlPlan, retry.retry_of_plan_id) if retry.retry_of_plan_id else None
        if (
            failed is None
            or any(
                plan.case_id != item.id
                or plan.stage_id != stage.id
                or plan.execution_flow_run_id != run.id
                or plan.action != "START_EXECUTION"
                for plan in (retry, failed)
            )
            or failed.state != "FAILED"
        ):
            raise conflict("stage start retry no longer matches its failed plan")
        proposal_key = f"stage-start:retry:{retry.id}"
        repeated = db.scalar(
            select(CaseDecisionProposal).where(
                CaseDecisionProposal.case_id == item.id,
                CaseDecisionProposal.proposal_key == proposal_key,
            )
        )
        if repeated is not None:
            return _stage_start_result(repeated)
        previous_id = failed.result_json.get("decision_id")
        if isinstance(previous_id, str):
            previous = _proposal(db, item.id, previous_id)
            if previous.flow_run_id != run.id or previous.action != "START_STAGE_EXECUTION":
                raise conflict("failed plan references another stage start")
        if previous is not None:
            if previous.state != "FAILED":
                return _stage_start_result(previous)
            retry_of_proposal_id = previous.id
        # An earlier request may have committed before its response failed. Do not
        # use a fresh start key against an existing attempt/frozen automatic plan.
        started = db.scalar(select(NodeRun.id).where(NodeRun.flow_run_id == run.id).limit(1))
        if started or (stage.execution_mode == "CONTINUOUS" and run.state != "DRAFT"):
            raise DomainError(
                "CASE_START_RECONCILIATION_REQUIRED",
                "当前执行已有启动事实，请先核对原执行，不能重复启动",
                409,
                {"flow_run_id": run.id, "decision_id": retry_of_proposal_id},
            )
    elif previous is not None:
        return _stage_start_result(previous)
    proposal = CaseDecisionProposal(
        case_id=item.id,
        flow_run_id=run.id,
        proposal_key=proposal_key,
        action="START_STAGE_EXECUTION",
        reason=payload.reason.strip(),
        payload_json={
            "stage_id": stage.id,
            "execution_mode": stage.execution_mode,
            **({"retry_of_proposal_id": retry_of_proposal_id} if retry_of_proposal_id else {}),
        },
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
        if isinstance(exc, DomainError):
            raise CaseActionFailure(
                exc.code,
                exc.message,
                exc.status,
                {**exc.details, "decision_id": proposal.id},
            ) from exc
        raise CaseActionFailure(
            "CASE_STAGE_START_FAILED", "阶段启动失败", 500, {"decision_id": proposal.id}
        ) from exc
    proposal.state = "EXECUTED"
    proposal.result_json = result
    proposal.executed_at = now()
    item.row_version += 1
    write_case_files(db, item)
    finish(db)
    return _proposal_dict(proposal)


def create_case_decision_proposal(
    db: Session, case_id: str, payload: CaseDecisionProposalWrite
) -> dict[str, Any]:
    item = _case(db, case_id)
    require_case_action(item, payload.action)
    existing = db.scalar(
        select(CaseDecisionProposal).where(
            CaseDecisionProposal.case_id == item.id,
            CaseDecisionProposal.proposal_key == payload.proposal_key,
        )
    )
    if existing is not None:
        return _proposal_dict(existing)
    run = _case_linked_run(db, item.id, payload.flow_run_id)
    _current_stage_execution(db, item, run)
    if payload.source_task_id:
        task = db.get(CaseAgentTask, payload.source_task_id)
        if task is None or task.case_id != item.id:
            raise not_found("case_agent_task", payload.source_task_id)
    node_run = None
    if payload.node_run_id:
        node_run = db.get(NodeRun, payload.node_run_id)
        if node_run is None or node_run.flow_run_id != run.id:
            raise not_found("node_run", payload.node_run_id)
    manual_actions = {"CONTINUE_STEPWISE", "RERUN_STEPWISE_NODE"}
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
    item = _case(db, case_id)
    proposal = _proposal(db, item.id, proposal_id)
    require_case_action(item, proposal.action)
    if proposal.state != "PROPOSED":
        raise illegal("Case decision has already executed", state=proposal.state)
    run = _case_linked_run(db, proposal.case_id, proposal.flow_run_id)
    _current_stage_execution(db, item, run)
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
        else:
            raise illegal("Case decision action is not dispatchable", action=proposal.action)
    except Exception as exc:
        proposal.state = "FAILED"
        proposal.result_json = {"code": getattr(exc, "code", "CASE_DECISION_EXECUTION_FAILED")}
        proposal.executed_at = now()
        finish(db)
        if isinstance(exc, DomainError):
            raise CaseActionFailure(exc.code, exc.message, exc.status, exc.details) from exc
        raise CaseActionFailure(
            "CASE_DECISION_EXECUTION_FAILED",
            "场景决策执行失败",
            500,
            {"decision_id": proposal.id},
        ) from exc
    proposal.state = "EXECUTED"
    proposal.result_json = result
    proposal.executed_at = now()
    finish(db)
    return _proposal_dict(proposal)


def validate_case_start_path(db: Session, stages: list[CaseStage]) -> None:
    if not stages or any(not stage.flow_run_id for stage in stages):
        raise DomainError("CASE_PATH_INCOMPLETE", "请先配置完整的 Case 路径", 409)
    for stage in stages:
        template = db.get(FlowRun, stage.flow_run_id)
        if template is None:
            raise DomainError("CASE_TEMPLATE_UNAVAILABLE", "流程模板已不可用，请重新配置路径", 409)
        if stage.execution_mode == "STEPWISE":
            if template.run_mode != "MANUAL" or not template.parent_flow_run_id:
                raise DomainError("CASE_TEMPLATE_UNAVAILABLE", "逐步流程模板不可用", 409)
            orchestration.validate_stepwise_record_template(db, template.id)
        else:
            plan = template.automation_plan_json or {}
            if (
                template.run_mode != "AUTOMATIC"
                or not plan.get("start_node_key")
                or not plan.get("readiness", {}).get("ready")
            ):
                raise DomainError("CASE_TEMPLATE_UNAVAILABLE", "连续流程模板配置不完整", 409)


def create_case_control_conversation(
    db: Session,
    case_id: str,
    payload: CaseControlConversationWrite,
    idempotency_key: str,
) -> dict[str, Any]:
    from flowweave.modules.cases.application.coordination import reserve_control_round

    return reserve_control_round(db, case_id, payload, idempotency_key)
