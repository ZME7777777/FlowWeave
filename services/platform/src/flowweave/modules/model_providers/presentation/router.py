from typing import Annotated, Any

from fastapi import APIRouter, Depends, Response

from flowweave.bootstrap.container import Container
from flowweave.modules.model_providers.application import service
from flowweave.modules.model_providers.infrastructure.client import discover_provider_models
from flowweave.modules.model_providers.infrastructure.codex_oauth import (
    CodexModelProfile,
    discover_codex_model_profiles,
    poll_device_authorization,
    request_device_authorization,
)
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
    result = await run_sync(db, lambda session: service.delete_providers(session, [provider_id]))
    if result["blocked"]:
        raise DomainError(
            "MODEL_PROVIDER_IN_USE",
            "Model provider is referenced by active nodes",
            409,
            {"blocked": result["blocked"]},
        )
    return Response(status_code=204)


@router.delete("/model-providers")
async def delete_providers(payload: ModelProviderBulkDeleteWrite, db: Db) -> dict[str, Any]:
    return await run_sync(db, lambda session: service.delete_providers(session, payload.ids))


ContainerDep = Annotated[Container, Depends(get_container)]


async def _discover(provider_id: str, db: Db, container: Container) -> list[str]:
    snapshot = await run_sync(
        db, lambda session: service.provider_connection_snapshot(session, provider_id)
    )
    return await discover_provider_models(container.http, snapshot)


async def _discover_codex(
    provider_id: str, db: Db, container: Container
) -> list[CodexModelProfile]:
    credentials = await run_sync(
        db, lambda session: service.codex_runtime_credentials(session, provider_id)
    )
    return await discover_codex_model_profiles(
        container.http, credentials.access_token, credentials.account_id
    )


@router.post("/model-providers/{provider_id}/oauth/device/start")
async def start_codex_oauth(provider_id: str, db: Db, container: ContainerDep) -> dict[str, Any]:
    await run_sync(db, lambda session: service.require_codex_oauth_provider(session, provider_id))
    authorization = await request_device_authorization(container.http)
    return await run_sync(
        db,
        lambda session: service.save_device_authorization(session, provider_id, authorization),
    )


@router.post("/model-providers/{provider_id}/oauth/device/poll")
async def poll_codex_oauth(provider_id: str, db: Db, container: ContainerDep) -> dict[str, Any]:
    snapshot = await run_sync(
        db, lambda session: service.device_authorization_snapshot(session, provider_id)
    )
    tokens = await poll_device_authorization(
        container.http, snapshot.device_auth_id, snapshot.user_code
    )
    if tokens is None:
        return {"state": "AUTHORIZING", "connected": False}
    provider = await run_sync(
        db, lambda session: service.save_oauth_tokens(session, provider_id, tokens)
    )
    try:
        models = await discover_codex_model_profiles(
            container.http, tokens.access_token, tokens.account_id
        )
        provider = await run_sync(
            db, lambda session: service.sync_codex_models(session, provider_id, models)
        )
    except DomainError as exc:
        return {
            "state": "CONNECTED",
            "connected": True,
            "account_email": provider["oauth_account_email"],
            "model_count": len(provider["models"]),
            "model_sync_error": exc.message,
        }
    return {
        "state": "CONNECTED",
        "connected": True,
        "account_email": provider["oauth_account_email"],
        "model_count": len(provider["models"]),
        "model_sync_error": None,
    }


@router.get("/model-providers/{provider_id}/oauth/status")
async def codex_oauth_status(provider_id: str, db: Db) -> dict[str, Any]:
    return await run_sync(db, lambda session: service.oauth_status(session, provider_id))


@router.delete("/model-providers/{provider_id}/oauth")
async def disconnect_codex_oauth(provider_id: str, db: Db) -> dict[str, Any]:
    return await run_sync(db, lambda session: service.disconnect_oauth(session, provider_id))


@router.post("/model-providers/{provider_id}/test")
async def test_provider(provider_id: str, db: Db, container: ContainerDep) -> dict[str, Any]:
    provider = await run_sync(
        db,
        lambda session: service.provider_dict(session, service.get_provider(session, provider_id)),
    )
    if provider["auth_type"] == "CODEX_OAUTH":
        status = await run_sync(db, lambda session: service.oauth_status(session, provider_id))
        if not status["connected"]:
            raise DomainError("CODEX_OAUTH_REQUIRED", "Codex OAuth login is required", 409)
        try:
            models = await _discover_codex(provider_id, db, container)
        except DomainError:
            await run_sync(
                db,
                lambda session: service.mark_provider_connection_state(
                    session, provider_id, "FAILED"
                ),
            )
            raise
        await run_sync(db, lambda session: service.sync_codex_models(session, provider_id, models))
        return {"connection_state": "CONNECTED", "model_count": len(models)}
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
    provider = await run_sync(
        db,
        lambda session: service.provider_dict(session, service.get_provider(session, provider_id)),
    )
    if provider["auth_type"] == "CODEX_OAUTH":
        models = await _discover_codex(provider_id, db, container)
        synchronized = await run_sync(
            db, lambda session: service.sync_codex_models(session, provider_id, models)
        )
        return {
            "models": [profile.model_name for profile in models],
            "provider": synchronized,
        }
    return {"models": await _discover(provider_id, db, container)}
