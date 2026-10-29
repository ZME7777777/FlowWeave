from __future__ import annotations

from collections.abc import Callable
from typing import Annotated, Any
from urllib.parse import quote

from fastapi import APIRouter, File, Query, Response, UploadFile
from sqlalchemy.orm import Session

from flowweave.modules.cases import public as service
from flowweave.shared.errors import DomainError
from flowweave.shared.http import Db, IdempotencyKey, command_key, run_sync
from flowweave.shared.schemas import (
    CaseControlConversationWrite,
    CaseControlPlanWrite,
    CaseControlSuggestionWrite,
    CaseCoordinationStateWrite,
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
)

router = APIRouter()


async def _run_case_action(
    db: Db, operation: Callable[[Session], dict[str, Any]]
) -> dict[str, Any]:
    """Commit the recorded failure, then return its original HTTP error."""

    def dispatch(
        session: Session,
    ) -> tuple[dict[str, Any] | None, service.CaseActionFailure | None]:
        try:
            return operation(session), None
        except service.CaseActionFailure as failure:
            if not session.is_active:
                raise
            return None, failure

    result, failure = await run_sync(db, dispatch)
    if failure is not None:
        raise failure
    assert result is not None
    return result


@router.get("/cases")
async def cases(db: Db) -> list[dict[str, Any]]:
    return await run_sync(db, service.list_cases)


@router.post("/cases", status_code=201)
async def create_case(payload: CaseCreateWrite, db: Db) -> dict[str, Any]:
    return await run_sync(db, lambda session: service.create_case(session, payload))


@router.put("/cases/{case_id}")
async def update_case(case_id: str, payload: CaseUpdateWrite, db: Db) -> dict[str, Any]:
    return await run_sync(db, lambda session: service.update_case(session, case_id, payload))


@router.put("/cases/{case_id}/coordination-state")
async def coordination_state(
    case_id: str, payload: CaseCoordinationStateWrite, db: Db
) -> dict[str, Any]:
    return await run_sync(
        db,
        lambda session: service.set_case_coordination_state(
            session,
            case_id,
            payload.expected_row_version,
            payload.state,
        ),
    )


@router.put("/cases/{case_id}/session-config")
async def save_session_config(
    case_id: str, payload: CaseSessionConfigWrite, db: Db
) -> dict[str, Any]:
    return await run_sync(
        db, lambda session: service.save_case_session_config(session, case_id, payload)
    )


@router.delete("/cases/{case_id}", status_code=204, response_class=Response)
async def delete_case(case_id: str, db: Db) -> Response:
    await run_sync(db, lambda session: service.delete_case(session, case_id))
    return Response(status_code=204)


@router.delete("/cases/{case_id}/control-conversation", status_code=204, response_class=Response)
async def delete_case_control_conversation(
    case_id: str, db: Db, binding_id: str | None = None
) -> Response:
    await run_sync(
        db, lambda session: service.delete_case_control_conversation(session, case_id, binding_id)
    )
    return Response(status_code=204)


@router.get("/cases/{case_id}/artifact-materials")
async def artifact_materials(
    case_id: str,
    db: Db,
    offset: int = Query(default=0, ge=0, le=100_000),
    limit: int = Query(default=20, ge=1, le=50),
) -> dict[str, Any]:
    return await run_sync(
        db, lambda session: service.list_case_artifact_materials(session, case_id, offset, limit)
    )


@router.get("/cases/{case_id}")
async def case(case_id: str, db: Db) -> dict[str, Any]:
    return await run_sync(db, lambda session: service.get_case(session, case_id))


@router.put("/cases/{case_id}/path")
async def save_path(case_id: str, payload: CasePathWrite, db: Db) -> dict[str, Any]:
    return await run_sync(db, lambda session: service.save_case_path(session, case_id, payload))


@router.post("/cases/{case_id}/materials", status_code=201)
async def upload_material(
    case_id: str,
    db: Db,
    file: Annotated[UploadFile, File()],
    expected_row_version: int = Query(ge=1),
) -> dict[str, Any]:
    try:
        content = await file.read(64 * 1024 * 1024 + 1)
        if len(content) > 64 * 1024 * 1024:
            raise DomainError("CASE_MATERIAL_TOO_LARGE", "文件超过 64 MB", 413)
        return await run_sync(
            db,
            lambda session: service.upload_case_material(
                session, case_id, expected_row_version, file.filename or "", content
            ),
        )
    finally:
        await file.close()


@router.get("/cases/{case_id}/materials/file")
async def material_file(case_id: str, db: Db, value: str, download: bool = False) -> Response:
    content, filename, mime = await run_sync(
        db, lambda session: service.read_case_material(session, case_id, value)
    )
    disposition = "attachment" if download else "inline"
    return Response(
        content,
        media_type=mime,
        headers={
            "Content-Disposition": f"{disposition}; filename*=UTF-8''{quote(filename)}",
            "X-Content-Type-Options": "nosniff",
            "Cache-Control": "private, no-store",
            "Content-Security-Policy": "sandbox; default-src 'none'",
        },
    )


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
    return await _run_case_action(
        db,
        lambda session: service.start_case_stage_execution(session, case_id, stage_id, payload),
    )


@router.post("/cases/{case_id}/decision-proposals", status_code=201)
async def create_decision_proposal(
    case_id: str, payload: CaseDecisionProposalWrite, db: Db
) -> dict[str, Any]:
    return await _run_case_action(
        db, lambda session: service.create_case_decision_proposal(session, case_id, payload)
    )


@router.post("/cases/{case_id}/decision-proposals/{proposal_id}/execute")
async def execute_decision_proposal(case_id: str, proposal_id: str, db: Db) -> dict[str, Any]:
    return await _run_case_action(
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
    return await _run_case_action(
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


@router.post("/cases/{case_id}/control-rounds/{round_id}/retry")
async def retry_control_round(case_id: str, round_id: str, db: Db) -> dict[str, Any]:
    from flowweave.modules.cases.application.coordination import retry_control_round

    return await run_sync(db, lambda session: retry_control_round(session, case_id, round_id))


@router.get("/cases/{case_id}/control-rounds/{round_id}")
async def control_round(case_id: str, round_id: str, db: Db) -> dict[str, Any]:
    from flowweave.modules.cases.application.coordination import get_control_round

    return await run_sync(db, lambda session: get_control_round(session, case_id, round_id))


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
