from __future__ import annotations

import asyncio
import contextvars
import logging
from collections.abc import AsyncIterator, Callable
from concurrent.futures import ThreadPoolExecutor
from typing import Annotated, TypeVar, cast

from fastapi import Depends, Header, WebSocket, WebSocketException
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import Session
from starlette.requests import HTTPConnection

from flowweave.bootstrap.container import Container
from flowweave.modules.users.application import service as users
from flowweave.modules.users.application.security import (
    FLOWWEAVE_USER_ID,
    bind_principal,
    current_principal,
    reset_principal,
    tenant_bypass,
    tenant_user,
)
from flowweave.shared.application.transactions import (
    mark_uow_owned,
    run_commit_actions,
    run_rollback_actions,
)
from flowweave.shared.errors import DomainError

logger = logging.getLogger(__name__)

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
            await cast(WebSocket, connection).accept()
            raise WebSocketException(code=4401, reason="请先登录")
        raise DomainError("AUTHENTICATION_REQUIRED", "请先登录", 401)
    principal_token = bind_principal(principal)
    try:
        yield
    finally:
        reset_principal(principal_token)


async def shared_business_scope() -> AsyncIterator[None]:
    """Run shared-product routes as the stable platform data owner.

    The authenticated principal remains bound for audit attribution. Only the
    persistence tenant is changed, so shared writes keep one global identity
    while the independent Agent router continues to use the login user's ID.
    """

    with tenant_user(FLOWWEAVE_USER_ID):
        with tenant_bypass():
            yield


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


async def run_blocking(container: Container, operation: Callable[[Session], T]) -> T:
    """Run a synchronous DB/Runtime read in a bounded worker thread.

    Several compatibility services perform synchronous OpenHands HTTP calls while
    holding a SQLAlchemy session. Running them through ``AsyncSession.run_sync``
    blocks the ASGI loop. A stalled conversation could therefore delay unrelated
    authentication and control-plane requests. This helper uses a separate,
    non-overflowing DB pool and a matching semaphore so both threads and database
    connections have a hard process-local ceiling.
    """

    return await _run_blocking_lane(
        container,
        operation,
        executor=container.blocking_executor,
        slots=container.blocking_io_slots,
        session_factory=container.database.blocking_sessions,
        saturation_code="RUNTIME_READ_SATURATED",
        saturation_message="Agent Runtime reads are busy; retry shortly",
        lane_name="read",
        active_limit=container.settings.blocking_pool_size,
    )


async def run_blocking_control(container: Container, operation: Callable[[Session], T]) -> T:
    """Run an Agent Runtime control command on its reserved recovery lane."""

    return await _run_blocking_lane(
        container,
        operation,
        executor=container.blocking_control_executor,
        slots=container.blocking_control_slots,
        session_factory=container.database.control_sessions,
        saturation_code="RUNTIME_CONTROL_SATURATED",
        saturation_message="Agent Runtime recovery is already in progress",
        lane_name="control",
        active_limit=1,
    )


async def _run_blocking_lane(
    container: Container,
    operation: Callable[[Session], T],
    *,
    executor: ThreadPoolExecutor,
    slots: asyncio.Semaphore,
    session_factory: Callable[[], Session],
    saturation_code: str,
    saturation_message: str,
    lane_name: str,
    active_limit: int,
) -> T:
    try:
        await asyncio.wait_for(slots.acquire(), timeout=0.25)
    except TimeoutError as exc:
        logger.warning(
            "blocking Runtime %s pool saturated active_limit=%d",
            lane_name,
            active_limit,
        )
        raise DomainError(
            saturation_code,
            saturation_message,
            503,
        ) from exc

    def execute() -> T:
        with session_factory() as session:
            mark_uow_owned(session)
            try:
                result = operation(session)
                session.commit()
            except BaseException:
                session.rollback()
                run_rollback_actions(session)
                raise
            run_commit_actions(session)
            return result

    context = contextvars.copy_context()
    worker = asyncio.ensure_future(
        asyncio.get_running_loop().run_in_executor(
            executor,
            context.run,
            execute,
        )
    )

    def release_slot(completed: asyncio.Future[T]) -> None:
        slots.release()
        # A disconnected HTTP client cancels the request coroutine, but Python
        # cannot stop an already-running thread. Consume its eventual exception
        # and release capacity only after its bounded Runtime call has exited.
        if not completed.cancelled():
            completed.exception()

    worker.add_done_callback(release_slot)
    return await asyncio.shield(worker)


Db = Annotated[AsyncSession, Depends(get_db)]
IdempotencyKey = Annotated[str | None, Header(alias="Idempotency-Key")]


def command_key(value: str | None, *, fallback: str) -> str:
    return value or fallback
