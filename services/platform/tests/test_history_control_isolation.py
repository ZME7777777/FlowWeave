from __future__ import annotations

import asyncio
import json
import threading
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from types import SimpleNamespace

import pytest
from starlette.requests import Request

from flowweave.modules.agent_sessions.presentation import history_mcp as mcp
from flowweave.shared.http import run_blocking_control


@pytest.fixture(autouse=True)
def database():
    """Use bounded executors and in-memory authorization doubles."""


class _Session:
    def __init__(self):
        self.info = {}

    def commit(self):
        pass

    def rollback(self):
        pass


@contextmanager
def _sessions():
    yield _Session()


@pytest.mark.asyncio
@pytest.mark.parametrize("blocked_lane", ["control", "history"])
async def test_history_authentication_and_pause_do_not_share_capacity(monkeypatch, blocked_lane):
    owner = "00000000-0000-4000-8000-000000000001"
    identity = "00000000-0000-4000-8000-000000000002"
    started, release = threading.Event(), threading.Event()

    def authenticate(*_args):
        if blocked_lane == "history":
            started.set()
            assert release.wait(2)
        return SimpleNamespace(owner_user_id=owner)

    monkeypatch.setattr(mcp.history_access, "authenticate", authenticate)
    monkeypatch.setattr(mcp, "agent_session_principal", lambda *_: SimpleNamespace(user_id=owner))

    async def allow(*_args, **_kwargs):
        return SimpleNamespace(allowed=True)

    with ThreadPoolExecutor(1) as control, ThreadPoolExecutor(1) as history:
        container = SimpleNamespace(
            blocking_control_executor=control,
            blocking_control_slots=asyncio.Semaphore(1),
            history_read_executor=history,
            history_read_slots=asyncio.Semaphore(1),
            database=SimpleNamespace(control_sessions=_sessions, history_sessions=_sessions),
            rate_limiter=SimpleNamespace(allow=allow),
            settings=SimpleNamespace(blocking_pool_timeout_seconds=0.05, history_read_pool_size=1),
        )

        async def initialize():
            async def receive():
                return {
                    "type": "http.request",
                    "body": json.dumps(
                        {"jsonrpc": "2.0", "id": 1, "method": "initialize"}
                    ).encode(),
                    "more_body": False,
                }

            token = f"fwh2.{owner}.{identity}." + "a" * 43
            request = Request(
                {
                    "type": "http",
                    "method": "POST",
                    "path": mcp.MCP_PATH,
                    "headers": [(b"authorization", f"Bearer {token}".encode())],
                    "app": SimpleNamespace(state=SimpleNamespace(container=container)),
                },
                receive=receive,
            )
            return await mcp.history_mcp(request)

        def blocked_control(_session):
            started.set()
            assert release.wait(2)

        pending = asyncio.create_task(
            run_blocking_control(container, blocked_control)
            if blocked_lane == "control"
            else initialize()
        )
        try:
            assert await asyncio.to_thread(started.wait, 1)
            if blocked_lane == "control":
                response = await initialize()
                assert response.status_code == 200
            else:
                assert (
                    await run_blocking_control(container, lambda _: "pause-accepted")
                    == "pause-accepted"
                )
        finally:
            release.set()
            result = await pending
        if blocked_lane == "history":
            assert result.status_code == 200
        assert not container.blocking_control_slots.locked()
        assert not container.history_read_slots.locked()
