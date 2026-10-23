from __future__ import annotations

from typing import Any

from fastapi import APIRouter

from flowweave.modules.cases import public as service
from flowweave.shared.http import Db, IdempotencyKey, command_key, run_sync
from flowweave.shared.schemas import (
    CaseControlConversationWrite,
    CaseControlPlanWrite,
    CaseControlSuggestionWrite,
    CaseCreateWrite,
    CaseDecisionProposalWrite,
    CaseFlowRunWrite,
    CaseStageExecutionStartWrite,
    CaseStageFlowRunWrite,
    CaseStageLaunchWrite,
    CaseStageStateWrite,
)

router = APIRouter()


@router.get("/cases")
async def cases(db: Db) -> list[dict[str, Any]]:
    return await run_sync(db, service.list_cases)


@router.post("/cases", status_code=201)
async def create_case(payload: CaseCreateWrite, db: Db) -> dict[str, Any]:
    return await run_sync(db, lambda session: service.create_case(session, payload))


@router.get("/cases/{case_id}")
async def case(case_id: str, db: Db) -> dict[str, Any]:
    return await run_sync(db, lambda session: service.get_case(session, case_id))


@router.get("/cases/{case_id}/stage-templates")
async def stage_templates(case_id: str, db: Db) -> list[dict[str, Any]]:
    return await run_sync(db, lambda session: service.list_case_stage_templates(session, case_id))


@router.put("/cases/{case_id}/stages/{stage_id}/state")
async def set_case_stage_state(
    case_id: str, stage_id: str, payload: CaseStageStateWrite, db: Db
) -> dict[str, Any]:
    return await run_sync(
        db, lambda session: service.set_case_stage_state(session, case_id, stage_id, payload)
    )


@router.put("/cases/{case_id}/stages/{stage_id}/flow-run")
async def bind_stage_flow_run(
    case_id: str, stage_id: str, payload: CaseStageFlowRunWrite, db: Db
) -> dict[str, Any]:
    return await run_sync(
        db,
        lambda session: service.bind_case_stage_flow_run(session, case_id, stage_id, payload),
    )


@router.post("/cases/{case_id}/flow-runs")
async def attach_flow_run(case_id: str, payload: CaseFlowRunWrite, db: Db) -> dict[str, Any]:
    return await run_sync(
        db, lambda session: service.attach_case_flow_run(session, case_id, payload)
    )


@router.post("/cases/{case_id}/stages/{stage_id}/launch", status_code=201)
async def launch_stage(
    case_id: str, stage_id: str, payload: CaseStageLaunchWrite, db: Db
) -> dict[str, Any]:
    return await run_sync(
        db, lambda session: service.launch_case_stage(session, case_id, stage_id, payload)
    )


@router.post("/cases/{case_id}/observation-projection")
async def project_observations(case_id: str, db: Db) -> dict[str, Any]:
    return await run_sync(db, lambda session: service.project_case_observations(session, case_id))


@router.post("/cases/{case_id}/stages/{stage_id}/execution/start", status_code=201)
async def start_stage_execution(
    case_id: str, stage_id: str, payload: CaseStageExecutionStartWrite, db: Db
) -> dict[str, Any]:
    return await run_sync(
        db,
        lambda session: service.start_case_stage_execution(session, case_id, stage_id, payload),
    )


@router.post("/cases/{case_id}/decision-proposals", status_code=201)
async def create_decision_proposal(
    case_id: str, payload: CaseDecisionProposalWrite, db: Db
) -> dict[str, Any]:
    return await run_sync(
        db, lambda session: service.create_case_decision_proposal(session, case_id, payload)
    )


@router.post("/cases/{case_id}/decision-proposals/{proposal_id}/execute")
async def execute_decision_proposal(case_id: str, proposal_id: str, db: Db) -> dict[str, Any]:
    return await run_sync(
        db, lambda session: service.execute_case_decision_proposal(session, case_id, proposal_id)
    )


@router.post("/cases/{case_id}/control-plans", status_code=201)
async def create_control_plan(
    case_id: str, payload: CaseControlPlanWrite, db: Db
) -> dict[str, Any]:
    return await run_sync(
        db, lambda session: service.create_case_control_plan(session, case_id, payload)
    )


@router.post("/cases/{case_id}/control-plans/{plan_id}/execute")
async def execute_control_plan(case_id: str, plan_id: str, db: Db) -> dict[str, Any]:
    return await run_sync(
        db, lambda session: service.execute_case_control_plan(session, case_id, plan_id)
    )


@router.post("/case-control-bindings/{binding_id}/plans", status_code=201)
async def create_control_plan_for_binding(
    binding_id: str, payload: CaseControlPlanWrite, db: Db
) -> dict[str, Any]:
    return await run_sync(
        db,
        lambda session: service.create_case_control_plan_for_binding(session, binding_id, payload),
    )


@router.post("/cases/{case_id}/control-conversation", status_code=201)
async def create_control_conversation(
    case_id: str,
    payload: CaseControlConversationWrite,
    db: Db,
    idempotency_key: IdempotencyKey = None,
) -> dict[str, Any]:
    key = command_key(
        idempotency_key,
        fallback=f"case-control-conversation:{case_id}",
    )
    return await run_sync(
        db,
        lambda session: service.create_case_control_conversation(session, case_id, payload, key),
    )


@router.post("/cases/{case_id}/control-plans/{plan_id}/retry", status_code=201)
async def retry_control_plan(case_id: str, plan_id: str, db: Db) -> dict[str, Any]:
    return await run_sync(
        db, lambda session: service.retry_case_control_plan(session, case_id, plan_id)
    )


@router.post("/case-control-bindings/{binding_id}/suggestions", status_code=201)
async def ingest_control_suggestion(
    binding_id: str, payload: CaseControlSuggestionWrite, db: Db
) -> dict[str, Any]:
    return await run_sync(
        db, lambda session: service.ingest_case_control_suggestion(session, binding_id, payload)
    )
