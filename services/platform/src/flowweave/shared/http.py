from __future__ import annotations

from collections.abc import AsyncIterator, Callable
from typing import Annotated, TypeVar

from fastapi import Depends, Header, WebSocketException
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import Session
from starlette.requests import HTTPConnection

from flowweave.bootstrap.container import Container
from flowweave.modules.users.application import service as users
from flowweave.modules.users.application.security import (
    bind_principal,
    current_principal,
    reset_principal,
)
from flowweave.shared.application.transactions import (
    mark_uow_owned,
    run_commit_actions,
    run_rollback_actions,
)
from flowweave.shared.errors import DomainError

T = TypeVar("T")


def get_container(connection: HTTPConnection) -> Container:
    """Resolve the application container for both HTTP and WebSocket scopes."""

    return connection.app.state.container


async def require_authenticated_connection(
    connection: HTTPConnection,
    container: Annotated[Container, Depends(get_container)],
) -> AsyncIterator[None]:
    """Authenticate WebSockets; HTTP requests are already bound by middleware."""

    if current_principal() is not None:
        yield
        return
    token = connection.cookies.get(users.SESSION_COOKIE)
    async with container.database.session() as session:
        principal = await session.run_sync(lambda db: users.authenticate(db, token))
        if principal is not None:
            await session.commit()
    if principal is None:
        if connection.scope["type"] == "websocket":
            raise WebSocketException(code=4401, reason="请先登录")
        raise DomainError("AUTHENTICATION_REQUIRED", "请先登录", 401)
    principal_token = bind_principal(principal)
    try:
        yield
    finally:
        reset_principal(principal_token)


async def get_db(
    container: Annotated[Container, Depends(get_container)],
) -> AsyncIterator[AsyncSession]:
    async with container.database.uow() as uow:
        mark_uow_owned(uow.session.sync_session)
        yield uow.session


async def run_sync(db: AsyncSession, operation: Callable[[Session], T]) -> T:
    """Execute and commit one synchronous application command in the async UoW."""

    try:
        result = await db.run_sync(operation)
        await db.commit()
    except BaseException:
        await db.rollback()
        await db.run_sync(run_rollback_actions)
        raise
    await db.run_sync(run_commit_actions)
    return result


Db = Annotated[AsyncSession, Depends(get_db)]
IdempotencyKey = Annotated[str | None, Header(alias="Idempotency-Key")]


def command_key(value: str | None, *, fallback: str) -> str:
    return value or fallback
