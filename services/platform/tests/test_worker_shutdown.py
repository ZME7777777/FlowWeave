from __future__ import annotations

import asyncio
import threading
from concurrent.futures import ThreadPoolExecutor
from contextlib import nullcontext
from types import SimpleNamespace

import pytest

from flowweave.bootstrap import worker as worker_module
from flowweave.bootstrap.container import Container
from flowweave.bootstrap.worker import LeaseHeartbeat, TaskWorker
from flowweave.modules.tasks.application.service import Lease


@pytest.fixture(autouse=True)
def database() -> None:
    """These fault injections use in-memory sessions, not the PostgreSQL fixture."""


class _AsyncSession:
    async def __aenter__(self):
        return self

    async def __aexit__(self, *_args):
        return None

    async def run_sync(self, callback):
        return callback(None)

    async def commit(self):
        return None

    async def rollback(self):
        return None


class _SyncSession:
    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return None

    def commit(self):
        return None

    def rollback(self):
        return None


@pytest.mark.asyncio
async def test_repeated_and_direct_cancel_keep_lease_until_thread_completes(monkeypatch) -> None:
    started = threading.Event()
    release = threading.Event()
    lease = Lease(task_id="task", owner="worker", generation=1)
    task = SimpleNamespace(task_type="START_RUNTIME")
    claims = 0
    succeeded = 0
    renewers = []

    def claim_once(*_args, **_kwargs):
        nonlocal claims
        claims += 1
        return (task, lease) if claims == 1 else None

    def handle_blocked(*_args):
        started.set()
        assert release.wait(3)

    def succeed_once(*_args, **_kwargs):
        nonlocal succeeded
        succeeded += 1
        return True

    class FakeRenewer:
        def __init__(self, *_args, **_kwargs):
            self.lost = threading.Event()
            self.stopped = False
            renewers.append(self)

        def start(self):
            return None

        def stop(self):
            self.stopped = True

    monkeypatch.setattr(worker_module, "claim", claim_once)
    monkeypatch.setattr(worker_module, "handle", handle_blocked)
    monkeypatch.setattr(worker_module, "succeed", succeed_once)
    monkeypatch.setattr(worker_module, "mark_uow_owned", lambda _db: None)
    monkeypatch.setattr(worker_module, "run_commit_actions", lambda _db: None)
    monkeypatch.setattr(worker_module, "LeaseHeartbeat", FakeRenewer)

    executor = ThreadPoolExecutor(max_workers=1)
    container = SimpleNamespace(
        settings=SimpleNamespace(
            worker_id="worker", task_lease_seconds=10, task_heartbeat_seconds=1
        ),
        database=SimpleNamespace(session=_AsyncSession, blocking_sessions=lambda: _SyncSession()),
        blocking_executor=executor,
        blocking_io_slots=asyncio.Semaphore(1),
        heartbeat_slots=threading.BoundedSemaphore(1),
    )
    worker = TaskWorker(container)
    monkeypatch.setattr(worker, "_contexts", lambda: (nullcontext(),) * 6)
    pending = asyncio.create_task(worker.run_once())
    try:
        assert await asyncio.to_thread(started.wait, 2)
        pending.cancel()
        await asyncio.sleep(0)
        pending.cancel()
        await asyncio.sleep(0)
        execution = next(
            task
            for task in asyncio.all_tasks()
            if task.get_coro().__qualname__.endswith("TaskWorker._execute_claimed_task")
        )
        execution.cancel()
        await asyncio.sleep(0)
        assert not pending.done()
        assert not renewers[0].stopped
        assert await worker.run_once() is False
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(pending, 2)
        assert succeeded == 1
        assert renewers[0].stopped
    finally:
        release.set()
        executor.shutdown(wait=True)


@pytest.mark.asyncio
async def test_cancelled_worker_finishes_failure_record_before_stopping_lease(monkeypatch) -> None:
    executing = asyncio.Event()
    release_execution = asyncio.Event()
    recording = asyncio.Event()
    release_record = asyncio.Event()
    renewers = []
    failures = []

    class FakeRenewer:
        def __init__(self, *_args, **_kwargs):
            self.lost = threading.Event()
            self.stopped = False
            renewers.append(self)

        def start(self):
            return None

        def stop(self):
            self.stopped = True

    monkeypatch.setattr(worker_module, "LeaseHeartbeat", FakeRenewer)
    monkeypatch.setattr(
        worker_module,
        "claim",
        lambda *_args, **_kwargs: (
            SimpleNamespace(task_type="START_RUNTIME"),
            Lease(task_id="task", owner="worker", generation=1),
        ),
    )
    worker = TaskWorker(
        SimpleNamespace(
            settings=SimpleNamespace(
                worker_id="worker", task_lease_seconds=10, task_heartbeat_seconds=1
            ),
            database=SimpleNamespace(session=_AsyncSession),
            heartbeat_slots=threading.BoundedSemaphore(1),
        )
    )
    monkeypatch.setattr(worker, "_contexts", lambda: (nullcontext(),) * 6)

    async def execute(*_args):
        executing.set()
        await release_execution.wait()
        raise ValueError("injected failure")

    async def fail(*_args):
        failures.append("started")
        recording.set()
        await release_record.wait()
        failures.append("committed")

    monkeypatch.setattr(worker, "_execute_claimed_task", execute)
    monkeypatch.setattr(worker, "_fail_task", fail)
    pending = asyncio.create_task(worker.run_once())
    try:
        await asyncio.wait_for(executing.wait(), 2)
        pending.cancel()
        await asyncio.sleep(0)
        release_execution.set()
        await asyncio.wait_for(recording.wait(), 2)
        pending.cancel()
        await asyncio.sleep(0)
        assert not renewers[0].stopped
        release_record.set()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(pending, 2)
        assert failures == ["started", "committed"]
        assert renewers[0].stopped
    finally:
        release_execution.set()
        release_record.set()


@pytest.mark.asyncio
async def test_lost_lease_does_not_record_failure_with_stale_generation(monkeypatch) -> None:
    failure_calls = []
    renewers = []

    class LostRenewer:
        def __init__(self, *_args, **_kwargs):
            self.lost = threading.Event()
            self.lost.set()
            self.stopped = False
            renewers.append(self)

        def start(self):
            return None

        def stop(self):
            self.stopped = True

    monkeypatch.setattr(worker_module, "LeaseHeartbeat", LostRenewer)
    monkeypatch.setattr(
        worker_module,
        "claim",
        lambda *_args, **_kwargs: (
            SimpleNamespace(task_type="START_RUNTIME"),
            Lease(task_id="task", owner="worker", generation=1),
        ),
    )
    worker = TaskWorker(
        SimpleNamespace(
            settings=SimpleNamespace(
                worker_id="worker", task_lease_seconds=10, task_heartbeat_seconds=1
            ),
            database=SimpleNamespace(session=_AsyncSession),
            heartbeat_slots=threading.BoundedSemaphore(1),
        )
    )
    monkeypatch.setattr(worker, "_contexts", lambda: (nullcontext(),) * 6)

    async def execute(*_args):
        raise ValueError("injected failure")

    async def fail(*_args):
        failure_calls.append(True)

    monkeypatch.setattr(worker, "_execute_claimed_task", execute)
    monkeypatch.setattr(worker, "_fail_task", fail)
    assert await worker.run_once() is True
    assert failure_calls == []
    assert renewers[0].stopped


@pytest.mark.asyncio
async def test_repeated_shutdown_cancel_waits_for_worker_lane(monkeypatch) -> None:
    started = asyncio.Event()
    release = asyncio.Event()
    worker = TaskWorker(SimpleNamespace(settings=SimpleNamespace(worker_id="worker")))

    async def no_op():
        return None

    async def blocked_run_once(**_kwargs):
        started.set()
        await release.wait()
        return True

    async def maintenance():
        await asyncio.Event().wait()

    monkeypatch.setattr(worker, "recover_startup", no_op)
    monkeypatch.setattr(worker, "run_once", blocked_run_once)
    monkeypatch.setattr(worker, "_maintenance_loop", maintenance)
    monkeypatch.setattr(worker, "_lane_specs", lambda: (("delivery", frozenset(), 1),))
    pending = asyncio.create_task(worker.run_until_stopped())
    try:
        await asyncio.wait_for(started.wait(), 2)
        pending.cancel()
        await asyncio.sleep(0)
        pending.cancel()
        await asyncio.sleep(0)
        assert not pending.done()
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(pending, 2)
    finally:
        release.set()


@pytest.mark.asyncio
async def test_lease_renewal_continues_until_stop_and_disposes_its_connection(monkeypatch) -> None:
    renewed = threading.Event()
    disposed = threading.Event()
    count = 0

    class Engine:
        async def dispose(self):
            disposed.set()

    class Session(_AsyncSession):
        async def run_sync(self, callback):
            return callback(None)

    def heartbeat(*_args, **_kwargs):
        nonlocal count
        count += 1
        if count >= 2:
            renewed.set()
        return True

    monkeypatch.setattr(worker_module, "create_async_engine", lambda *_args, **_kwargs: Engine())
    monkeypatch.setattr(worker_module, "async_sessionmaker", lambda *_args, **_kwargs: Session)
    monkeypatch.setattr(worker_module, "heartbeat", heartbeat)
    renewer = LeaseHeartbeat(
        SimpleNamespace(database_url="unused", statement_timeout_ms=100),
        Lease(task_id="task", owner="worker", generation=1),
        interval_seconds=0.01,
        lease_seconds=1,
    )
    renewer.start()
    try:
        assert await asyncio.to_thread(renewed.wait, 2)
        assert not renewer.lost.is_set()
    finally:
        await asyncio.to_thread(renewer.stop)
    assert disposed.is_set()
    assert not renewer._thread.is_alive()


@pytest.mark.asyncio
async def test_lease_renewal_failure_marks_lease_lost_and_releases_connection(monkeypatch) -> None:
    disposed = threading.Event()

    class Engine:
        async def dispose(self):
            disposed.set()

    monkeypatch.setattr(worker_module, "create_async_engine", lambda *_args, **_kwargs: Engine())
    monkeypatch.setattr(
        worker_module, "async_sessionmaker", lambda *_args, **_kwargs: _AsyncSession
    )
    monkeypatch.setattr(worker_module, "heartbeat", lambda *_args, **_kwargs: False)
    renewer = LeaseHeartbeat(
        SimpleNamespace(database_url="unused", statement_timeout_ms=100),
        Lease(task_id="task", owner="worker", generation=1),
        interval_seconds=0.01,
        lease_seconds=1,
    )
    renewer.start()
    try:
        assert await asyncio.to_thread(renewer.lost.wait, 2)
    finally:
        await asyncio.to_thread(renewer.stop)
    assert disposed.is_set()
    assert not renewer._thread.is_alive()


@pytest.mark.asyncio
async def test_container_waits_for_sync_work_before_closing_transport_and_sql() -> None:
    started = threading.Event()
    release = threading.Event()
    closed = []
    executor = ThreadPoolExecutor(max_workers=1)

    def blocked_work():
        started.set()
        assert release.wait(3)
        assert closed == []

    class Transport:
        async def aclose(self):
            closed.append("transport")

    class Database:
        async def dispose(self):
            closed.append("database")

    class AsyncResource:
        async def close(self):
            return None

    class Cache:
        async def close(self):
            return None

    container = object.__new__(Container)
    container.settings = SimpleNamespace()
    container.run_event_listener = AsyncResource()
    container.audit_writer = AsyncResource()
    container.rate_limiter = AsyncResource()
    container.conversation_hydration_cache = Cache()
    container.http_transport = Transport()
    container.database = Database()
    for name in (
        "blocking_executor",
        "hydration_executor",
        "message_executor",
        "lifecycle_executor",
        "workspace_executor",
        "auxiliary_executor",
        "admin_executor",
        "poll_executor",
        "history_read_executor",
        "terminal_stream_executor",
        "terminal_control_executor",
        "blocking_control_executor",
    ):
        setattr(container, name, executor)
    monkeypatch_target = "flowweave.bootstrap.container.unregister_http_transport"
    from unittest.mock import patch

    with patch(monkeypatch_target):
        executor.submit(blocked_work)
        assert await asyncio.to_thread(started.wait, 2)
        closing = asyncio.create_task(container.close())
        try:
            await asyncio.sleep(0.05)
            assert closed == []
        finally:
            release.set()
            await asyncio.wait_for(closing, 2)
    assert closed == ["transport", "database"]
