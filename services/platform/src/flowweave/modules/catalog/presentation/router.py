import asyncio
from typing import Any

from fastapi import APIRouter, Query, Response

from flowweave.modules.catalog.application import capability_imports, service
from flowweave.shared.http import Db, run_sync
from flowweave.shared.schemas import (
    CapabilityCommitWrite,
    CapabilityValidateWrite,
    DirectoryWrite,
    NodeAssetBulkDeleteWrite,
    NodeAssetWrite,
)

router = APIRouter()


@router.get("/node-directories")
async def directories(db: Db) -> list[dict[str, Any]]:
    return await run_sync(db, service.list_directories)


@router.post("/node-directories", status_code=201)
async def create_directory(payload: DirectoryWrite, db: Db) -> dict[str, Any]:
    return await run_sync(db, lambda session: service.create_directory(session, payload))


@router.get("/node-assets")
async def assets(
    db: Db, directory_id: str | None = None, q: str | None = Query(default=None, max_length=200)
) -> list[dict[str, Any]]:
    return await run_sync(db, lambda session: service.list_assets(session, directory_id, q))


@router.post("/node-assets", status_code=201)
async def create_asset(payload: NodeAssetWrite, db: Db) -> dict[str, Any]:
    return await run_sync(db, lambda session: service.save_asset(session, payload))


@router.get("/node-assets/{asset_id}")
async def asset(asset_id: str, db: Db) -> dict[str, Any]:
    return await run_sync(db, lambda session: service.read_asset(session, asset_id))


@router.put("/node-assets/{asset_id}")
async def update_asset(asset_id: str, payload: NodeAssetWrite, db: Db) -> dict[str, Any]:
    return await run_sync(db, lambda session: service.save_asset(session, payload, asset_id))


@router.delete("/node-assets/{asset_id}", status_code=204, response_class=Response)
async def delete_asset(asset_id: str, db: Db) -> Response:
    await run_sync(db, lambda session: service.delete_asset(session, asset_id))
    return Response(status_code=204)


@router.delete("/node-assets", status_code=204, response_class=Response)
async def delete_assets(payload: NodeAssetBulkDeleteWrite, db: Db) -> Response:
    await run_sync(db, lambda session: service.delete_assets(session, payload.ids))
    return Response(status_code=204)


@router.post("/capability-imports/validate")
async def validate_capability(payload: CapabilityValidateWrite, db: Db) -> dict[str, Any]:
    await db.rollback()
    prepared = await asyncio.to_thread(capability_imports.prepare_validation, payload)
    stored = await asyncio.to_thread(capability_imports.store_validation_source, prepared)
    try:
        return await run_sync(
            db, lambda session: capability_imports.register_validation(session, stored)
        )
    except BaseException:
        await asyncio.to_thread(capability_imports.discard_validation_source, stored)
        raise


@router.post("/capability-imports", status_code=201)
async def commit_capability(payload: CapabilityCommitWrite, db: Db) -> dict[str, Any]:
    plan = await run_sync(
        db, lambda session: capability_imports.prepare_commit(session, payload.import_token)
    )
    final_key = await asyncio.to_thread(capability_imports.finalize_commit_source, plan)
    return await run_sync(
        db, lambda session: capability_imports.confirm_commit(session, plan, final_key)
    )
