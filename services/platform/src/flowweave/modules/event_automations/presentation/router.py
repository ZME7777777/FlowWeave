from __future__ import annotations

from typing import Any

from fastapi import APIRouter

from flowweave.modules.event_automations.application import service
from flowweave.shared.http import Db, run_sync
from flowweave.shared.schemas import EventTriggerWrite

router = APIRouter()


@router.get("/event-triggers")
async def event_triggers(db: Db) -> list[dict[str, Any]]:
    return await run_sync(db, service.list_trigger_versions)


@router.post("/event-triggers", status_code=201)
async def create_event_trigger(payload: EventTriggerWrite, db: Db) -> dict[str, Any]:
    return await run_sync(db, lambda session: service.create_trigger_version(session, payload))


@router.get("/event-triggers/{trigger_key}")
async def event_trigger(trigger_key: str, db: Db) -> dict[str, Any]:
    return await run_sync(db, lambda session: service.read_latest_trigger(session, trigger_key))


@router.post("/event-triggers/{trigger_key}/versions", status_code=201)
async def create_event_trigger_version(
    trigger_key: str, payload: EventTriggerWrite, db: Db
) -> dict[str, Any]:
    return await run_sync(
        db,
        lambda session: service.create_trigger_version(session, payload, trigger_key=trigger_key),
    )
