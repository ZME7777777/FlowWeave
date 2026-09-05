from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Request, Response
from pydantic import BaseModel, ConfigDict, Field

from flowweave.modules.users.application import service
from flowweave.modules.users.application.security import current_principal
from flowweave.shared.errors import DomainError
from flowweave.shared.http import Db, run_sync

router = APIRouter(prefix="/auth")


class LoginWrite(BaseModel):
    model_config = ConfigDict(extra="forbid")

    username: str = Field(min_length=1, max_length=80)
    password: str = Field(min_length=1, max_length=200)


def _set_cookie(response: Response, token: str, *, secure: bool) -> None:
    response.set_cookie(
        service.SESSION_COOKIE,
        token,
        max_age=int(service.SESSION_TTL.total_seconds()),
        httponly=True,
        secure=secure,
        samesite="lax",
        path="/",
    )


@router.post("/login")
async def login(
    payload: LoginWrite, request: Request, response: Response, db: Db
) -> dict[str, Any]:
    result = await run_sync(
        db, lambda session: service.login(session, payload.username, payload.password)
    )
    _set_cookie(response, result.token, secure=request.url.scheme == "https")
    request.state.audit_principal = result.principal
    return service.principal_dict(result.principal)


@router.post("/logout", status_code=204)
async def logout(request: Request, response: Response, db: Db) -> Response:
    await run_sync(
        db,
        lambda session: service.logout(session, request.cookies.get(service.SESSION_COOKIE)),
    )
    response.delete_cookie(service.SESSION_COOKIE, path="/")
    response.status_code = 204
    return response


@router.get("/me")
async def me() -> dict[str, Any]:
    principal = current_principal()
    if principal is None:
        raise DomainError("AUTHENTICATION_REQUIRED", "请先登录", 401)
    return service.principal_dict(principal)


__all__ = ("router",)
