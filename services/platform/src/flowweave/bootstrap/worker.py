from __future__ import annotations

import asyncio
import signal
import threading
from collections.abc import Coroutine
from typing import Any
from uuid import uuid4

from flowweave.bootstrap.container import Container, build_container
from flowweave.bootstrap.settings import Settings
from flowweave.modules.conversations.public import recover_conversation_tasks
from flowweave.modules.orchestration.public import recover_runtime_deliveries
from flowweave.modules.tasks.application.handlers import handle, record_terminal_failure
from flowweave.modules.tasks.application.service import (
    Lease,
    claim,
    fail,
    heartbeat,
    recover_expired,
    succeed,
)
from flowweave.runtime.dependencies import runtime_context
from flowweave.shared.application.transactions import (
    mark_uow_owned,
    run_commit_actions,
    run_rollback_actions,
)
from flowweave.shared.artifact_store import artifact_store_context
from flowweave.shared.infrastructure.database import Database
from flowweave.shared.sandbox import sandbox_context
from flowweave.shared.settings import settings_context


class LeaseHeartbeat:
    """Renew one task lease through independent AsyncSession transactions."""

    def __init__(
        self,
        settings: Settings,
        lease: Lease,
        *,
        interval_seconds: int,
        lease_seconds: int,
    ) -> None:
        self.settings = settings
        self.lease = lease
        self.interval_seconds = interval_seconds
        self.lease_seconds = lease_seconds
        self.lost = threading.Event()
        self._stop = threading.Event()
        self._thread = threading.Thread(
            target=self._run,
            name=f"lease-heartbeat-{lease.task_id}",
            daemon=True,
        )

    def start(self) -> None:
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        self._thread.join(timeout=self.interval_seconds + 1)

    def _run(self) -> None:
        asyncio.run(self._run_async())

    async def _run_async(self) -> None:
        database = Database(self.settings)
        try:
            while not self._stop.wait(self.interval_seconds):
                try:
                    async with database.session() as session:
                        renewed = await session.run_sync(
                            lambda db: heartbeat(
                                db,
                                self.lease,
                                lease_seconds=self.lease_seconds,
                                commit=False,
                            )
                        )
                        if renewed:
                            await session.commit()
                        else:
                            await session.rollback()
                except Exception:
                    self.lost.set()
                    return
                if not renewed:
                    self.lost.set()
                    return
        finally:
            await database.dispose()


class TaskWorker:
    """Worker process shell with AsyncSession-owned transactions and graceful stop."""

    def __init__(self, container: Container) -> None:
        self.container = container
        self.owner = container.settings.worker_id or f"worker-{uuid4()}"
        self._stopping = asyncio.Event()
        self._sync_loop: asyncio.AbstractEventLoop | None = None

    def stop(self) -> None:
        self._stopping.set()

    def _contexts(self):
        return (
            settings_context(self.container.settings),
            runtime_context(self.container.runtime),
            artifact_store_context(self.container.artifact_store),
            sandbox_context(self.container.sandbox),
        )

    async def recover_startup(self) -> None:
        settings, runtime, artifacts, sandbox = self._contexts()
        with settings, runtime, artifacts, sandbox:
            async with self.container.database.session() as session:
                try:
                    await session.run_sync(
                        lambda db: (mark_uow_owned(db), recover_expired(db, commit=False))[1]
                    )
                    await session.run_sync(
                        lambda db: (mark_uow_owned(db), recover_runtime_deliveries(db))[1]
                    )
                    await session.run_sync(
                        lambda db: (mark_uow_owned(db), recover_conversation_tasks(db))[1]
                    )
                    await session.commit()
                except BaseException:
                    await session.rollback()
                    raise

    async def run_once(self) -> bool:
        settings, runtime, artifacts, sandbox = self._contexts()
        with settings, runtime, artifacts, sandbox:
            async with self.container.database.session() as session:
                claimed = await session.run_sync(
                    lambda db: claim(
                        db,
                        self.owner,
                        lease_seconds=self.container.settings.task_lease_seconds,
                        commit=False,
                    )
                )
                if claimed is None:
                    await session.rollback()
                    return False
                await session.commit()

            task, lease = claimed
            renewer = LeaseHeartbeat(
                self.container.settings,
                lease,
                interval_seconds=self.container.settings.task_heartbeat_seconds,
                lease_seconds=self.container.settings.task_lease_seconds,
            )
            renewer.start()
            async with self.container.database.session() as session:
                try:
                    await session.run_sync(
                        lambda db: (mark_uow_owned(db), handle(db, task, lease))[1]
                    )
                except Exception as exc:
                    error = str(exc)
                    renewer.stop()
                    await session.rollback()
                    await session.run_sync(run_rollback_actions)
                    if not renewer.lost.is_set():
                        failed = await session.run_sync(
                            lambda db: fail(db, lease, error, commit=False)
                        )
                        if failed:
                            await session.run_sync(
                                lambda db: record_terminal_failure(db, lease.task_id, error)
                            )
                            await session.commit()
                        else:
                            await session.rollback()
                else:
                    renewer.stop()
                    if renewer.lost.is_set():
                        await session.rollback()
                        await session.run_sync(run_rollback_actions)
                    elif await session.run_sync(lambda db: succeed(db, lease, commit=False)):
                        await session.commit()
                        await session.run_sync(run_commit_actions)
                    else:
                        await session.rollback()
                        await session.run_sync(run_rollback_actions)
            return True

    def _run_sync(self, operation: Coroutine[Any, Any, Any]) -> Any:
        if self._sync_loop is None:
            self._sync_loop = asyncio.new_event_loop()
        return self._sync_loop.run_until_complete(operation)

    def _recover_startup(self) -> None:
        """Synchronous test adapter; production calls ``recover_startup`` directly."""

        self._run_sync(self.recover_startup())

    def _run_once_sync(self) -> bool:
        """Synchronous test adapter backed by the same persistent async event loop."""

        return bool(self._run_sync(self.run_once()))

    async def run_until_stopped(self) -> None:
        await self.recover_startup()
        while not self._stopping.is_set():
            if not await self.run_once():
                try:
                    await asyncio.wait_for(self._stopping.wait(), timeout=0.5)
                except TimeoutError:
                    pass


async def run_worker(settings: Settings) -> None:
    container = build_container(settings, role="worker")
    worker = TaskWorker(container)
    loop = asyncio.get_running_loop()
    for signal_name in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(signal_name, worker.stop)
    try:
        await worker.run_until_stopped()
    finally:
        await container.close()


def main() -> None:
    asyncio.run(run_worker(Settings()))


if __name__ == "__main__":
    main()
