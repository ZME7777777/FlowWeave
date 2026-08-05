from __future__ import annotations

from collections.abc import AsyncIterator, Callable
from typing import Annotated, TypeVar

from fastapi import Depends, Header, Request
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import Session

from flowweave.bootstrap.container import Container
from flowweave.shared.application.transactions import (
    mark_uow_owned,
    run_commit_actions,
    run_rollback_actions,
)

T = TypeVar("T")


def get_container(request: Request) -> Container:
    return request.app.state.container


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
