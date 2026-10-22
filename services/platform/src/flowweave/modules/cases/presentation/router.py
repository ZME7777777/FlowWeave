from __future__ import annotations

from typing import Any

from fastapi import APIRouter

from flowweave.modules.cases import public as service
from flowweave.shared.http import Db, run_sync
from flowweave.shared.schemas import CaseCreateWrite, CaseStageStateWrite

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


@router.put("/cases/{case_id}/stages/{stage_id}/state")
async def set_case_stage_state(
    case_id: str, stage_id: str, payload: CaseStageStateWrite, db: Db
) -> dict[str, Any]:
    return await run_sync(
        db, lambda session: service.set_case_stage_state(session, case_id, stage_id, payload)
    )
