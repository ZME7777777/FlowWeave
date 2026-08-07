from typing import Annotated, Any

from fastapi import APIRouter, Depends, Response

from flowweave.bootstrap.container import Container
from flowweave.modules.model_providers.application import service
from flowweave.modules.model_providers.infrastructure.client import discover_provider_models
from flowweave.shared.errors import DomainError
from flowweave.shared.http import Db, get_container, run_sync
from flowweave.shared.schemas import ModelProviderBulkDeleteWrite, ModelProviderWrite

router = APIRouter()


@router.get("/model-providers")
async def providers(db: Db) -> list[dict[str, Any]]:
    return await run_sync(db, service.list_providers)


@router.post("/model-providers", status_code=201)
async def create_provider(payload: ModelProviderWrite, db: Db) -> dict[str, Any]:
    return await run_sync(db, lambda session: service.save_provider(session, payload))


@router.put("/model-providers/{provider_id}")
async def update_provider(provider_id: str, payload: ModelProviderWrite, db: Db) -> dict[str, Any]:
    return await run_sync(db, lambda session: service.save_provider(session, payload, provider_id))


@router.delete("/model-providers/{provider_id}", status_code=204, response_class=Response)
async def delete_provider(provider_id: str, db: Db) -> Response:
    await run_sync(db, lambda session: service.delete_providers(session, [provider_id]))
    return Response(status_code=204)


@router.delete("/model-providers", status_code=204, response_class=Response)
async def delete_providers(payload: ModelProviderBulkDeleteWrite, db: Db) -> Response:
    await run_sync(db, lambda session: service.delete_providers(session, payload.ids))
    return Response(status_code=204)


ContainerDep = Annotated[Container, Depends(get_container)]


async def _discover(provider_id: str, db: Db, container: Container) -> list[str]:
    snapshot = await run_sync(
        db, lambda session: service.provider_connection_snapshot(session, provider_id)
    )
    return await discover_provider_models(container.http, snapshot)


@router.post("/model-providers/{provider_id}/test")
async def test_provider(provider_id: str, db: Db, container: ContainerDep) -> dict[str, Any]:
    try:
        models = await _discover(provider_id, db, container)
    except DomainError:
        await run_sync(
            db,
            lambda session: service.mark_provider_connection_state(session, provider_id, "FAILED"),
        )
        raise
    await run_sync(
        db,
        lambda session: service.mark_provider_connection_state(session, provider_id, "CONNECTED"),
    )
    return {"connection_state": "CONNECTED", "model_count": len(models)}


@router.post("/model-providers/{provider_id}/discover-models")
async def discover_models(provider_id: str, db: Db, container: ContainerDep) -> dict[str, Any]:
    return {"models": await _discover(provider_id, db, container)}
