from __future__ import annotations

import asyncio
import contextvars
import logging
import threading
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from types import SimpleNamespace

import pytest

from flowweave.bootstrap import api as api_module
from flowweave.bootstrap.settings import Settings
from flowweave.runtime.read_budget import hydration_read_budget
from flowweave.shared.errors import DomainError
from flowweave.shared.http import (
    run_blocking,
    run_blocking_control,
    run_blocking_history,
    run_blocking_hydration,
    run_blocking_message,
    run_blocking_mutation,
)
from flowweave.shared.infrastructure.database import Database


@pytest.fixture(autouse=True)
def database():
    """These executor tests use in-memory session doubles, not PostgreSQL."""

    yield


@pytest.mark.asyncio
async def test_hydration_pool_partitions_existing_blocking_database_budget() -> None:
    resources = Database(Settings(blocking_pool_size=4, pool_max_overflow=0), hydration_pool_size=2)
    try:
        assert resources.blocking_engine.pool.size() == 2
        assert resources.hydration_engine is not None
        assert resources.hydration_engine.pool.size() == 2
        assert (
            resources.pool_metrics()["blocking"]["size"]
            + resources.pool_metrics()["hydration"]["size"]
            == 4
        )
    finally:
        await resources.dispose()


def test_message_pool_partitions_existing_blocking_database_budget() -> None:
    resources = Database(
        Settings(blocking_pool_size=4, pool_max_overflow=0),
        hydration_pool_size=2,
        message_pool_size=1,
    )
    try:
        assert resources.blocking_engine.pool.size() == 1
        assert resources.hydration_engine is not None
        assert resources.hydration_engine.pool.size() == 2
        assert resources.message_engine is not None
        assert resources.message_engine.pool.size() == 1
        metrics = resources.pool_metrics()
        assert (
            metrics["blocking"]["size"] + metrics["hydration"]["size"] + metrics["message"]["size"]
            == 4
        )
    finally:
        asyncio.run(resources.dispose())


class _Session:
    def __init__(self) -> None:
        self.info: dict[str, object] = {}

    def commit(self) -> None:
        return None

    def rollback(self) -> None:
        return None


class _Database:
    @contextmanager
    def blocking_sessions(self):
        yield _Session()

    @contextmanager
    def control_sessions(self):
        yield _Session()

    @contextmanager
    def history_sessions(self):
        yield _Session()

    @contextmanager
    def hydration_sessions(self):
        yield _Session()

    @contextmanager
    def message_sessions(self):
        yield _Session()


@pytest.mark.asyncio
async def test_hydration_keeps_reserved_capacity_when_ordinary_read_is_stalled() -> None:
    started = threading.Event()
    release = threading.Event()

    def ordinary(_session: _Session) -> str:
        started.set()
        assert release.wait(timeout=2)
        return "ordinary"

    with (
        ThreadPoolExecutor(max_workers=1) as ordinary_executor,
        ThreadPoolExecutor(max_workers=1) as hydration_executor,
    ):
        container = SimpleNamespace(
            blocking_executor=ordinary_executor,
            blocking_io_slots=asyncio.Semaphore(1),
            hydration_executor=hydration_executor,
            hydration_io_slots=asyncio.Semaphore(1),
            hydration_capacity=1,
            blocking_capacity=1,
            database=_Database(),
            settings=SimpleNamespace(blocking_pool_size=2, blocking_pool_timeout_seconds=0.05),
        )
        blocked = asyncio.create_task(run_blocking(container, ordinary))
        assert await asyncio.to_thread(started.wait, 1)
        try:
            with hydration_read_budget(0.5):
                assert await run_blocking_hydration(container, lambda _session: "first-screen") == (
                    "first-screen"
                )
        finally:
            release.set()
        assert await blocked == "ordinary"


@pytest.mark.asyncio
async def test_cancelled_hydration_retains_slot_until_worker_exits() -> None:
    started = threading.Event()
    release = threading.Event()

    def slow(_session: _Session) -> str:
        started.set()
        assert release.wait(timeout=2)
        return "finished"

    with ThreadPoolExecutor(max_workers=1) as executor:
        container = SimpleNamespace(
            hydration_executor=executor,
            hydration_io_slots=asyncio.Semaphore(1),
            hydration_capacity=1,
            blocking_capacity=1,
            database=_Database(),
            settings=SimpleNamespace(blocking_pool_timeout_seconds=0.05),
        )
        with hydration_read_budget(0.5):
            request = asyncio.create_task(run_blocking_hydration(container, slow))
            assert await asyncio.to_thread(started.wait, 1)
            request.cancel()
            await asyncio.gather(request, return_exceptions=True)
            with pytest.raises(DomainError) as caught:
                await run_blocking_hydration(container, lambda _session: "incorrect")
            assert caught.value.code == "RUNTIME_READ_SATURATED"
            release.set()
            for _ in range(100):
                if not container.hydration_io_slots.locked():
                    break
                await asyncio.sleep(0.001)
            assert await run_blocking_hydration(container, lambda _session: "ready") == "ready"


@pytest.mark.asyncio
async def test_message_delivery_keeps_capacity_when_mutation_is_stalled() -> None:
    started = threading.Event()
    release = threading.Event()

    def slow_mutation(_session: _Session) -> str:
        started.set()
        assert release.wait(timeout=2)
        return "mutation"

    with (
        ThreadPoolExecutor(max_workers=1) as mutation_executor,
        ThreadPoolExecutor(max_workers=1) as message_executor,
    ):
        container = SimpleNamespace(
            blocking_executor=mutation_executor,
            blocking_io_slots=asyncio.Semaphore(1),
            blocking_mutation_slots=asyncio.Semaphore(1),
            blocking_capacity=1,
            message_executor=message_executor,
            message_io_slots=asyncio.Semaphore(1),
            message_capacity=1,
            database=_Database(),
            settings=SimpleNamespace(blocking_pool_size=2, blocking_pool_timeout_seconds=0.05),
        )
        blocked = asyncio.create_task(run_blocking_mutation(container, slow_mutation))
        assert await asyncio.to_thread(started.wait, 1)
        try:
            assert await run_blocking_message(container, lambda _session: "message") == "message"
        finally:
            release.set()
        assert await blocked == "mutation"


@pytest.mark.asyncio
async def test_run_blocking_keeps_cancelled_thread_counted_until_it_exits() -> None:
    started = threading.Event()
    release = threading.Event()

    def blocked(_session: _Session) -> str:
        started.set()
        assert release.wait(timeout=2)
        return "finished"

    with (
        ThreadPoolExecutor(max_workers=1) as executor,
        ThreadPoolExecutor(max_workers=1) as control_executor,
    ):
        container = SimpleNamespace(
            blocking_executor=executor,
            blocking_io_slots=asyncio.Semaphore(1),
            history_read_executor=executor,
            history_read_slots=asyncio.Semaphore(1),
            blocking_control_executor=control_executor,
            blocking_control_slots=asyncio.Semaphore(1),
            database=_Database(),
            settings=SimpleNamespace(
                blocking_pool_size=1,
                blocking_pool_timeout_seconds=0.05,
                history_read_pool_size=1,
            ),
        )
        request = asyncio.create_task(run_blocking(container, blocked))
        for _ in range(100):
            if started.is_set():
                break
            await asyncio.sleep(0.001)
        assert started.is_set()

        # The synchronous operation runs off the ASGI loop, so unrelated async
        # work still receives execution while the Runtime read is blocked.
        await asyncio.wait_for(asyncio.sleep(0.01), timeout=0.1)
        request.cancel()
        await asyncio.gather(request, return_exceptions=True)

        with pytest.raises(DomainError) as caught:
            await run_blocking(container, lambda _session: "must not run")
        assert caught.value.code == "RUNTIME_READ_SATURATED"

        # Recovery commands have an independent worker, semaphore and DB
        # connection, so a wedged Runtime read cannot deny its own isolation.
        assert await run_blocking_control(container, lambda _session: "control-ready") == (
            "control-ready"
        )

        release.set()
        for _ in range(100):
            if not container.blocking_io_slots.locked():
                break
            await asyncio.sleep(0.001)
        principal_context = contextvars.ContextVar("principal_context", default="missing")
        token = principal_context.set("bound")
        try:
            result = await run_blocking(container, lambda _session: principal_context.get())
        finally:
            principal_context.reset(token)
        assert result == "bound"


@pytest.mark.asyncio
async def test_run_blocking_waits_for_configured_runtime_read_budget() -> None:
    started = threading.Event()
    release = threading.Event()

    def blocked(_session: _Session) -> str:
        started.set()
        assert release.wait(timeout=2)
        return "released"

    with (
        ThreadPoolExecutor(max_workers=1) as executor,
        ThreadPoolExecutor(max_workers=1) as control_executor,
    ):
        container = SimpleNamespace(
            blocking_executor=executor,
            blocking_io_slots=asyncio.Semaphore(1),
            history_read_executor=executor,
            history_read_slots=asyncio.Semaphore(1),
            blocking_control_executor=control_executor,
            blocking_control_slots=asyncio.Semaphore(1),
            database=_Database(),
            settings=SimpleNamespace(
                blocking_pool_size=1,
                # Deliberately longer than the former fixed 250ms deadline.
                blocking_pool_timeout_seconds=0.4,
                history_read_pool_size=1,
            ),
        )
        first = asyncio.create_task(run_blocking(container, blocked))
        for _ in range(100):
            if started.is_set():
                break
            await asyncio.sleep(0.001)
        assert started.is_set()

        waiting = asyncio.create_task(run_blocking(container, lambda _session: "queued"))
        await asyncio.sleep(0.3)
        assert not waiting.done()
        release.set()
        assert await first == "released"
        assert await waiting == "queued"


@pytest.mark.asyncio
async def test_history_reads_do_not_saturate_interactive_runtime_lane() -> None:
    history_started = threading.Event()
    history_release = threading.Event()

    def history_read(_session: _Session) -> str:
        history_started.set()
        assert history_release.wait(timeout=2)
        return "history"

    with (
        ThreadPoolExecutor(max_workers=1) as interactive_executor,
        ThreadPoolExecutor(max_workers=1) as history_executor,
        ThreadPoolExecutor(max_workers=1) as control_executor,
    ):
        container = SimpleNamespace(
            blocking_executor=interactive_executor,
            blocking_io_slots=asyncio.Semaphore(1),
            history_read_executor=history_executor,
            history_read_slots=asyncio.Semaphore(1),
            blocking_control_executor=control_executor,
            blocking_control_slots=asyncio.Semaphore(1),
            database=_Database(),
            settings=SimpleNamespace(
                blocking_pool_size=1,
                blocking_pool_timeout_seconds=0.05,
                history_read_pool_size=1,
            ),
        )
        history_task = asyncio.create_task(run_blocking_history(container, history_read))
        for _ in range(100):
            if history_started.is_set():
                break
            await asyncio.sleep(0.001)
        assert history_started.is_set()

        # An interactive native-state read must remain available even while an
        # older page is waiting on OpenHands.
        assert await run_blocking(container, lambda _session: "interactive") == "interactive"
        history_release.set()
        assert await history_task == "history"


def test_api_slow_request_log_excludes_query_values(anonymous_client, monkeypatch, caplog) -> None:
    ticks = iter((0.0, 0.0, 2.0))
    monkeypatch.setattr(api_module, "monotonic", lambda: next(ticks))
    caplog.set_level(logging.WARNING, logger=api_module.__name__)

    response = anonymous_client.get(
        "/health?credential=must-not-appear",
        headers={"X-Request-ID": "slow-request-1"},
    )

    assert response.status_code == 200
    record = next(item for item in caplog.records if item.message.startswith("slow API request"))
    assert "route=/health" in record.message
    assert "duration_ms=2000" in record.message
    assert "request_id=slow-request-1" in record.message
    assert "must-not-appear" not in record.message


def test_all_user_message_routes_use_reserved_message_lane() -> None:
    """Initial, normal, and rerun user messages cannot share slow mutations."""

    import ast
    from pathlib import Path

    paths = (
        Path("src/flowweave/modules/agent_workspaces/presentation/router.py"),
        Path("src/flowweave/modules/agent_sessions/presentation/router.py"),
    )
    expected = {
        "agent_workspaces": {
            "create_agent_conversation",
            "agent_message",
            "agent_rerun_edited_message",
        },
        "agent_sessions": {"bootstrap_node_session", "node_session_message", "rerun_node_message"},
    }
    for path in paths:
        module = ast.parse(path.read_text())
        functions = {
            node.name: node
            for node in module.body
            if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef)
        }
        group = "agent_workspaces" if "agent_workspaces" in str(path) else "agent_sessions"
        for name in expected[group]:
            calls = [
                call.func.id
                for call in ast.walk(functions[name])
                if isinstance(call, ast.Call) and isinstance(call.func, ast.Name)
            ]
            assert "run_blocking_message" in calls
