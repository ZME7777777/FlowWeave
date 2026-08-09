from __future__ import annotations

import secrets
from typing import Annotated, Any, cast
from urllib.parse import urlencode

import httpx
from fastapi import APIRouter, Depends, Header, Query, Response
from fastapi.responses import RedirectResponse

from flowweave.bootstrap.container import Container
from flowweave.modules.credentials.application import service
from flowweave.shared.http import Db, get_container, run_sync
from flowweave.shared.schemas import OAuthStartWrite

router = APIRouter()
ContainerDep = Annotated[Container, Depends(get_container)]


def _subject(container: Container) -> str:
    # The current product is single-tenant. Replace this fixed bootstrap subject
    # with the authenticated principal when user authentication is introduced.
    return container.settings.credential_subject_key


@router.get("/credential-connections")
async def connections(db: Db, container: ContainerDep) -> list[dict[str, Any]]:
    return await run_sync(
        db, lambda session: service.list_connections(session, _subject(container))
    )


@router.post("/oauth/lark/sessions", status_code=201)
async def begin_lark_oauth(
    payload: OAuthStartWrite, db: Db, container: ContainerDep
) -> dict[str, Any]:
    return await run_sync(
        db,
        lambda session: service.start_lark_oauth(
            session,
            _subject(container),
            payload.scopes or list(container.settings.lark_oauth_default_scopes),
        ),
    )


@router.get("/oauth/lark/callback")
async def lark_oauth_callback(
    db: Db,
    container: ContainerDep,
    code: str = Query(min_length=1, max_length=4096),
    state: str = Query(min_length=20, max_length=512),
) -> Response:
    exchange = await run_sync(db, lambda session: service.consume_oauth_state(session, state))
    settings = container.settings
    try:
        response = await container.http.post(
            settings.lark_oauth_token_url,
            json={
                "grant_type": "authorization_code",
                "client_id": settings.lark_oauth_client_id,
                "client_secret": settings.lark_oauth_client_secret,
                "code": code,
                "redirect_uri": settings.lark_oauth_redirect_url,
                "code_verifier": exchange.verifier,
            },
        )
        response.raise_for_status()
        value = cast(object, response.json())
    except (httpx.HTTPError, ValueError) as exc:
        raise service.oauth_exchange_failed() from exc
    response_object = cast(dict[str, object], value) if isinstance(value, dict) else {}
    raw_token_response = response_object.get("data", response_object)
    token_response = (
        cast(dict[str, Any], raw_token_response) if isinstance(raw_token_response, dict) else {}
    )
    await run_sync(
        db,
        lambda session: service.save_lark_connection(session, exchange, token_response),
    )
    target = f"{settings.public_base_url.rstrip('/')}/?{urlencode({'oauth': 'lark-connected'})}"
    return RedirectResponse(target, status_code=303)


@router.delete("/credential-connections/{connection_id}", status_code=204)
async def disconnect(connection_id: str, db: Db, container: ContainerDep) -> Response:
    await run_sync(
        db,
        lambda session: service.revoke_connection(session, _subject(container), connection_id),
    )
    return Response(status_code=204)


@router.get("/internal/credential-leases/{token}", include_in_schema=False)
async def resolve_credential_lease(
    token: str,
    db: Db,
    container: ContainerDep,
    authorization: str = Header(default="", max_length=512),
) -> Response:
    expected = f"Bearer {container.settings.credential_internal_api_key}"
    if not secrets.compare_digest(authorization, expected):
        raise service.invalid_internal_credential()
    value = await run_sync(db, lambda session: service.consume_runtime_lease(session, token))
    return Response(
        content=value,
        media_type="text/plain",
        headers={"Cache-Control": "no-store", "Pragma": "no-cache"},
    )
