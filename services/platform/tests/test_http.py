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
from flowweave.shared.errors import DomainError
from flowweave.shared.http import run_blocking, run_blocking_control, run_blocking_history


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
            settings=SimpleNamespace(blocking_pool_size=1, history_read_pool_size=1),
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
            settings=SimpleNamespace(blocking_pool_size=1, history_read_pool_size=1),
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
