from typing import Any

from fastapi import APIRouter, Response

from flowweave.modules.flows.application import service
from flowweave.shared.http import Db, run_sync
from flowweave.shared.schemas import FlowWrite

router = APIRouter()


@router.get("/flows")
async def flows(db: Db) -> list[dict[str, Any]]:
    return await run_sync(db, service.list_flows)


@router.post("/flows", status_code=201)
async def create_flow(payload: FlowWrite, db: Db) -> dict[str, Any]:
    return await run_sync(db, lambda session: service.save_flow(session, payload))


@router.get("/flows/{flow_id}")
async def flow(flow_id: str, db: Db) -> dict[str, Any]:
    return await run_sync(
        db, lambda session: service.flow_dict(session, service.get_flow(session, flow_id))
    )


@router.put("/flows/{flow_id}")
async def update_flow(flow_id: str, payload: FlowWrite, db: Db) -> dict[str, Any]:
    return await run_sync(db, lambda session: service.save_flow(session, payload, flow_id))


@router.post("/flows/{flow_id}/validate")
async def validate_saved_flow(flow_id: str, db: Db) -> dict[str, Any]:
    return await run_sync(db, lambda session: service.validate_saved_flow(session, flow_id))


@router.delete("/flows/{flow_id}", status_code=204, response_class=Response)
async def delete_flow(flow_id: str, db: Db) -> Response:
    await run_sync(db, lambda session: service.delete_flow(session, flow_id))
    return Response(status_code=204)
