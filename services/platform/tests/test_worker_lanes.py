from __future__ import annotations

import asyncio
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from types import SimpleNamespace

import pytest

from flowweave.bootstrap.settings import Settings
from flowweave.bootstrap.worker import (
    _ALL_TASK_TYPES,
    _AUXILIARY_TASK_TYPES,
    _DELIVERY_TASK_TYPES,
    _MAINTENANCE_TASK_TYPES,
    _POLL_TASK_TYPES,
    _RUNTIME_CONTROL_TASK_TYPES,
    _RUNTIME_TASK_TYPES,
    TaskWorker,
)
from flowweave.modules.tasks.application.handlers import HANDLERS


def test_worker_lanes_cover_each_handler_once_with_bounded_total_concurrency() -> None:
    worker = object.__new__(TaskWorker)
    worker.container = SimpleNamespace(settings=Settings(worker_concurrency=4))

    lanes = worker._lane_specs()
    assert sum(slots for _name, _task_types, slots in lanes) == 4
    assert set().union(*(task_types for _name, task_types, _slots in lanes)) == _ALL_TASK_TYPES
    assert _ALL_TASK_TYPES == frozenset(HANDLERS)
    assert _RUNTIME_TASK_TYPES == _RUNTIME_CONTROL_TASK_TYPES | _POLL_TASK_TYPES
    assert not (_RUNTIME_CONTROL_TASK_TYPES & _POLL_TASK_TYPES)
    assert not (_RUNTIME_TASK_TYPES & _DELIVERY_TASK_TYPES)
    assert not (_RUNTIME_TASK_TYPES & _AUXILIARY_TASK_TYPES)
    assert not (_RUNTIME_TASK_TYPES & _MAINTENANCE_TASK_TYPES)
    assert not (_DELIVERY_TASK_TYPES & _AUXILIARY_TASK_TYPES)
    assert not (_DELIVERY_TASK_TYPES & _MAINTENANCE_TASK_TYPES)
    assert not (_AUXILIARY_TASK_TYPES & _MAINTENANCE_TASK_TYPES)


def test_single_worker_uses_one_generic_lane() -> None:
    worker = object.__new__(TaskWorker)
    worker.container = SimpleNamespace(settings=Settings(worker_concurrency=1))

    assert worker._lane_specs() == (("all", _ALL_TASK_TYPES, 1),)


def test_default_worker_reserves_a_poll_lane_from_runtime_control() -> None:
    worker = object.__new__(TaskWorker)
    worker.container = SimpleNamespace(settings=Settings(worker_concurrency=4))

    lanes = dict((name, (task_types, slots)) for name, task_types, slots in worker._lane_specs())

    assert lanes["runtime-poll"] == (_POLL_TASK_TYPES, 1)
    assert lanes["runtime-control"] == (_RUNTIME_CONTROL_TASK_TYPES, 1)
    assert not (lanes["runtime-poll"][0] & lanes["runtime-control"][0])


def test_default_worker_reserves_auxiliary_lane_from_delivery() -> None:
    worker = object.__new__(TaskWorker)
    worker.container = SimpleNamespace(settings=Settings(worker_concurrency=4))

    lanes = dict((name, (task_types, slots)) for name, task_types, slots in worker._lane_specs())

    assert lanes["delivery"] == (_DELIVERY_TASK_TYPES, 1)
    assert lanes["auxiliary-maintenance"] == (
        _AUXILIARY_TASK_TYPES | _MAINTENANCE_TASK_TYPES,
        1,
    )
    assert not (lanes["delivery"][0] & lanes["auxiliary-maintenance"][0])


def test_auxiliary_tasks_use_their_dedicated_executor_and_database_pool() -> None:
    worker = object.__new__(TaskWorker)
    auxiliary_executor = object()
    auxiliary_slots = object()
    auxiliary_sessions = object()
    worker.container = SimpleNamespace(
        auxiliary_executor=auxiliary_executor,
        auxiliary_io_slots=auxiliary_slots,
        poll_executor=object(),
        poll_io_slots=object(),
        blocking_executor=object(),
        blocking_io_slots=object(),
        database=SimpleNamespace(
            auxiliary_sessions=auxiliary_sessions,
            poll_sessions=object(),
            blocking_sessions=object(),
        ),
    )

    executor, slots, sessions = worker._task_execution_resources(
        SimpleNamespace(task_type="BUILD_CAPABILITY_DEPENDENCIES")
    )

    assert (executor, slots, sessions) == (
        auxiliary_executor,
        auxiliary_slots,
        auxiliary_sessions,
    )


def test_poll_tasks_use_their_dedicated_executor_and_database_pool() -> None:
    worker = object.__new__(TaskWorker)
    poll_executor = object()
    poll_slots = object()
    poll_sessions = object()
    worker.container = SimpleNamespace(
        poll_executor=poll_executor,
        poll_io_slots=poll_slots,
        blocking_executor=object(),
        blocking_io_slots=object(),
        database=SimpleNamespace(poll_sessions=poll_sessions, blocking_sessions=object()),
    )

    executor, slots, sessions = worker._task_execution_resources(
        SimpleNamespace(task_type="POLL_RUNTIME")
    )

    assert (executor, slots, sessions) == (poll_executor, poll_slots, poll_sessions)


def test_lease_heartbeat_uses_one_unpooled_connection_and_shared_slot() -> None:
    from flowweave.bootstrap.worker import LeaseHeartbeat

    source = Path(LeaseHeartbeat.__module__.replace(".", "/") + ".py")
    del source
    worker_source = Path("src/flowweave/bootstrap/worker.py").read_text()

    assert "poolclass=NullPool" in worker_source
    assert "with self.slots:" in worker_source
    assert "slots=self.container.heartbeat_slots" in worker_source


def test_admin_control_routes_use_their_reserved_database_lane() -> None:
    admin_router = Path("src/flowweave/modules/admin_control/router.py").read_text()
    http = Path("src/flowweave/shared/http.py").read_text()
    container = Path("src/flowweave/bootstrap/container.py").read_text()
    database = Path("src/flowweave/shared/infrastructure/database.py").read_text()

    assert "run_blocking_admin" in admin_router
    assert "run_sync" not in admin_router
    assert "admin_executor" in container and "admin_io_slots" in container
    assert 'admin_pool_size=1 if role == "api" else 0' in container
    assert "self.admin_sessions" in database
    assert 'lane_name="admin"' in http


@pytest.mark.asyncio
async def test_stalled_poll_executor_does_not_block_runtime_control_executor() -> None:
    poll_started = asyncio.Event()
    release_poll = asyncio.Event()

    def blocked_poll() -> str:
        poll_started_loop.call_soon_threadsafe(poll_started.set)
        asyncio.run_coroutine_threadsafe(release_poll.wait(), poll_started_loop).result(timeout=2)
        return "poll-released"

    def runtime_control() -> str:
        return "control-ran"

    poll_started_loop = asyncio.get_running_loop()
    with (
        ThreadPoolExecutor(max_workers=1) as poll_executor,
        ThreadPoolExecutor(max_workers=1) as control_executor,
    ):
        blocked = poll_started_loop.run_in_executor(poll_executor, blocked_poll)
        await asyncio.wait_for(poll_started.wait(), timeout=1)
        assert (
            await asyncio.wait_for(
                poll_started_loop.run_in_executor(control_executor, runtime_control), timeout=0.2
            )
            == "control-ran"
        )
        release_poll.set()
        assert await asyncio.wait_for(blocked, timeout=1) == "poll-released"
