from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Response

from flowweave.modules.credentials.application import service
from flowweave.shared.http import Db, run_sync
from flowweave.shared.schemas import WebsiteCredentialWrite

router = APIRouter()


@router.get("/website-credentials")
async def credentials(db: Db) -> list[dict[str, Any]]:
    return await run_sync(db, service.list_credentials)


@router.post("/website-credentials", status_code=201)
async def create_credential(payload: WebsiteCredentialWrite, db: Db) -> dict[str, Any]:
    return await run_sync(db, lambda session: service.save_credential(session, payload))


@router.put("/website-credentials/{credential_id}")
async def update_credential(
    credential_id: str, payload: WebsiteCredentialWrite, db: Db
) -> dict[str, Any]:
    return await run_sync(
        db, lambda session: service.save_credential(session, payload, credential_id)
    )


@router.delete("/website-credentials/{credential_id}", status_code=204, response_class=Response)
async def delete_credential(credential_id: str, db: Db) -> Response:
    await run_sync(db, lambda session: service.delete_credential(session, credential_id))
    return Response(status_code=204)
