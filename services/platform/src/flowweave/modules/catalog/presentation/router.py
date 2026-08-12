import asyncio
from typing import Any

from fastapi import APIRouter, Query, Response

from flowweave.modules.catalog.application import capability_imports, service, skill_collections
from flowweave.shared.errors import DomainError
from flowweave.shared.http import Db, run_sync
from flowweave.shared.schemas import (
    CapabilityBulkDeleteWrite,
    CapabilityCommitWrite,
    CapabilitySkillRevisionWrite,
    CapabilityValidateWrite,
    DirectoryWrite,
    NodeAssetBulkDeleteWrite,
    NodeAssetWrite,
    SkillCollectionWrite,
)

router = APIRouter()


@router.get("/skill-collections")
async def collections(db: Db) -> list[dict[str, Any]]:
    return await run_sync(db, skill_collections.list_collections)


@router.post("/skill-collections", status_code=201)
async def create_collection(payload: SkillCollectionWrite, db: Db) -> dict[str, Any]:
    return await run_sync(db, lambda session: skill_collections.save_collection(session, payload))


@router.put("/skill-collections/{collection_id}")
async def update_collection(
    collection_id: str, payload: SkillCollectionWrite, db: Db
) -> dict[str, Any]:
    return await run_sync(
        db,
        lambda session: skill_collections.save_collection(session, payload, collection_id),
    )


@router.delete("/skill-collections/{collection_id}", status_code=204, response_class=Response)
async def delete_collection(collection_id: str, db: Db) -> Response:
    await run_sync(db, lambda session: skill_collections.delete_collection(session, collection_id))
    return Response(status_code=204)


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


@router.delete("/node-assets")
async def delete_assets(payload: NodeAssetBulkDeleteWrite, db: Db) -> dict[str, Any]:
    return await run_sync(db, lambda session: service.delete_assets(session, payload.ids))


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


@router.get("/capabilities")
async def capabilities(db: Db) -> list[dict[str, Any]]:
    return await run_sync(db, capability_imports.list_capabilities)


@router.get("/capabilities/{capability_id}/source")
async def capability_source(capability_id: str, db: Db) -> dict[str, Any]:
    return await run_sync(
        db, lambda session: capability_imports.read_skill_source(session, capability_id)
    )


@router.put("/capabilities/{capability_id}/source")
async def update_capability_source(
    capability_id: str, payload: CapabilitySkillRevisionWrite, db: Db
) -> dict[str, Any]:
    update_plan = await run_sync(
        db,
        lambda session: capability_imports.prepare_skill_update(
            session, capability_id, payload.content
        ),
    )
    stored = await asyncio.to_thread(capability_imports.store_skill_update_source, update_plan)
    try:
        final_key = await asyncio.to_thread(capability_imports.finalize_skill_update_source, stored)
        return await run_sync(
            db,
            lambda session: capability_imports.confirm_skill_update(session, stored, final_key),
        )
    except BaseException:
        if stored.prepared.storage_key is not None:
            await asyncio.to_thread(capability_imports.discard_validation_source, stored.prepared)
        raise


@router.delete("/capabilities/{capability_id}", status_code=204, response_class=Response)
async def delete_capability(capability_id: str, db: Db) -> Response:
    result = await run_sync(
        db, lambda session: capability_imports.delete_capabilities(session, [capability_id])
    )
    if result["blocked"]:
        raise DomainError(
            "CAPABILITY_IN_USE",
            "Capability version is referenced by active nodes",
            409,
            {"blocked": result["blocked"]},
        )
    return Response(status_code=204)


@router.delete("/capabilities")
async def delete_capability_batch(payload: CapabilityBulkDeleteWrite, db: Db) -> dict[str, Any]:
    return await run_sync(
        db, lambda session: capability_imports.delete_capabilities(session, payload.ids)
    )
