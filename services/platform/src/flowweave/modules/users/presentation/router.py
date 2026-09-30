from __future__ import annotations

import asyncio
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Request, Response
from pydantic import BaseModel, ConfigDict, Field

from flowweave.bootstrap.container import Container
from flowweave.modules.users.application import service
from flowweave.modules.users.application.ldap import LdapDirectory
from flowweave.modules.users.application.security import FLOWWEAVE_USER_ID, current_principal
from flowweave.shared.errors import DomainError
from flowweave.shared.http import Db, get_container, run_sync

router = APIRouter(prefix="/auth")
ContainerDep = Annotated[Container, Depends(get_container)]


class LoginWrite(BaseModel):
    model_config = ConfigDict(extra="forbid")

    username: str = Field(min_length=1, max_length=80)
    password: str = Field(min_length=1, max_length=200)


class LdapUserEnabledWrite(BaseModel):
    model_config = ConfigDict(extra="forbid")

    external_subject: str = Field(min_length=1, max_length=200)
    enabled: bool


def _require_super_admin() -> None:
    principal = current_principal()
    if principal is None or not principal.is_super_admin:
        raise DomainError("AUTHORIZATION_REQUIRED", "只有超级管理员可以管理用户", 403)


def _ldap_directory(container: Container) -> LdapDirectory:
    if container.settings.auth_provider != "ldap":
        raise DomainError("LDAP_NOT_ENABLED", "当前未启用 LDAP 登录", 409)
    return LdapDirectory(container.settings)


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
    payload: LoginWrite,
    request: Request,
    response: Response,
    db: Db,
    container: ContainerDep,
) -> dict[str, Any]:
    normalized_username = payload.username.strip()
    if container.settings.auth_provider == "ldap":
        local_admin = await run_sync(
            db,
            lambda session: service.local_user_id_for_login(
                session, normalized_username, payload.password
            ),
        )
        if local_admin != FLOWWEAVE_USER_ID:
            identity = await asyncio.to_thread(
                _ldap_directory(container).authenticate, payload.username, payload.password
            )
            result = await run_sync(db, lambda session: service.login_ldap(session, identity))
        else:
            result = await run_sync(
                db, lambda session: service.login_local(session, payload.username, payload.password)
            )
    else:
        result = await run_sync(
            db, lambda session: service.login_local(session, payload.username, payload.password)
        )
    _set_cookie(response, result.token, secure=request.url.scheme == "https")
    request.state.audit_principal = result.principal
    return service.principal_dict(result.principal)


@router.get("/ldap-users")
async def ldap_users(db: Db, container: ContainerDep) -> list[dict[str, object]]:
    _require_super_admin()
    identities = await asyncio.to_thread(_ldap_directory(container).list_users)
    return await run_sync(db, lambda session: service.list_ldap_users(session, identities))


@router.put("/ldap-users/enabled")
async def set_ldap_user_enabled(
    payload: LdapUserEnabledWrite,
    db: Db,
    container: ContainerDep,
) -> dict[str, object]:
    _require_super_admin()
    identities = await asyncio.to_thread(_ldap_directory(container).list_users)
    identity = next(
        (item for item in identities if item.external_subject == payload.external_subject),
        None,
    )
    if identity is None:
        raise DomainError("LDAP_USER_NOT_FOUND", "LDAP 用户不存在或已无法读取", 404)
    return await run_sync(
        db,
        lambda session: service.set_ldap_user_enabled(session, identity, enabled=payload.enabled),
    )


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
