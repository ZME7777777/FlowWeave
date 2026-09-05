from __future__ import annotations

import asyncio
import logging
from dataclasses import asdict, dataclass

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from flowweave.modules.users.infrastructure.models import UserOperationLog

logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class AuditRecord:
    user_id: str
    username: str
    request_id: str
    method: str
    route: str
    status_code: int
    duration_ms: int
    client_ip: str | None


class AuditWriter:
    """Best-effort bounded audit writer outside the response transaction."""

    def __init__(self, sessions: async_sessionmaker[AsyncSession], *, capacity: int = 4096) -> None:
        self._sessions = sessions
        self._queue: asyncio.Queue[AuditRecord | None] = asyncio.Queue(maxsize=capacity)
        self._task: asyncio.Task[None] | None = None

    def start(self) -> None:
        if self._task is None:
            self._task = asyncio.create_task(self._run(), name="user-operation-audit")

    def submit(self, record: AuditRecord) -> None:
        try:
            self._queue.put_nowait(record)
        except asyncio.QueueFull:
            logger.error("User operation audit queue is full; request_id=%s", record.request_id)

    async def close(self) -> None:
        if self._task is None:
            return
        await self._queue.put(None)
        await self._task
        self._task = None

    async def _run(self) -> None:
        while True:
            item = await self._queue.get()
            if item is None:
                self._queue.task_done()
                return
            try:
                async with self._sessions() as session:
                    session.add(UserOperationLog(**asdict(item)))
                    await session.commit()
            except Exception:
                logger.exception("Failed to persist user operation audit")
            finally:
                self._queue.task_done()


__all__ = ("AuditRecord", "AuditWriter")
