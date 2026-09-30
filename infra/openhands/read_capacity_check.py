"""Isolated installed-image HTTP concurrency and recovery acceptance.

Run inside the locked image with no host mounts, published ports, or external
network. Fault injection changes only this test process, never installed source.
The two-second target applies to deterministic native reads, not production LLM
latency or the FlowWeave/PostgreSQL/browser end-to-end path.
"""

from __future__ import annotations

import asyncio
import json
import math
import os
import socket
import tempfile
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from pathlib import Path
from unittest.mock import patch
from uuid import UUID

import httpx
import uvicorn

TARGET_SECONDS = 2.0


def require(condition: bool, message: str) -> None:
    if not condition:
        raise AssertionError(message)


def request(client: httpx.Client, method: str, path: str, **kwargs):
    response = client.request(method, path, **kwargs)
    response.raise_for_status()
    return response


def wait_status(client: httpx.Client, root: str, expected: str) -> None:
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        if request(client, "GET", root).json()["execution_status"] == expected:
            return
        time.sleep(0.025)
    raise AssertionError(f"native lifecycle did not reach {expected}")


@contextmanager
def server_environment(directory: Path):
    # Every path belongs to this newly allocated test directory.
    config_path = directory / "config.json"
    workspace = directory / "workspace"
    workspace.mkdir(exist_ok=True)
    config_path.write_text(
        json.dumps(
            {
                "session_api_keys": [],
                "conversations_path": str(directory / "conversations"),
                "workspace_path": str(workspace),
                "max_concurrent_reads": 8,
                "preload_tools": False,
            }
        )
    )
    os.environ["OPENHANDS_AGENT_SERVER_CONFIG_PATH"] = str(config_path)
    os.environ["OH_PERSISTENCE_DIR"] = str(directory / "persistence")
    os.environ.pop("SESSION_API_KEY", None)

    from openhands.agent_server.api import create_app
    from openhands.agent_server.config import Config
    from openhands.agent_server.persistence import reset_stores

    reset_stores()
    app = create_app(Config.model_validate_json(config_path.read_text()))
    # Retain the bound socket through startup, avoiding a free-port race.
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    server = uvicorn.Server(uvicorn.Config(app, log_level="error"))
    thread = threading.Thread(target=lambda: server.run(sockets=[sock]), daemon=True)
    thread.start()
    try:
        with httpx.Client(base_url=f"http://127.0.0.1:{port}", timeout=10) as client:
            deadline = time.monotonic() + 15
            while not server.started and time.monotonic() < deadline:
                require(thread.is_alive(), "native HTTP server exited")
                time.sleep(0.025)
            require(server.started, "native HTTP server startup timed out")
            request(client, "GET", "/health")
            yield app, client, workspace
    finally:
        server.should_exit = True
        thread.join(timeout=15)
        require(not thread.is_alive(), "native HTTP server failed to shut down")
        sock.close()
        reset_stores()


def check() -> dict:
    from litellm.types.utils import Choices, ModelResponse
    from litellm.types.utils import Message as LiteLLMMessage
    from openhands.agent_server.event_service import EventService
    from openhands.sdk import LLM, Message
    from openhands.sdk.llm.llm_response import LLMResponse

    llm_entered = threading.Event()
    llm_release = threading.Event()
    server_loop = []
    timings = []
    report = {"target_seconds": TARGET_SECONDS, "read_capacity": 8}

    def completion(self, messages, tools=None, **kwargs):
        message = LiteLLMMessage.model_validate(
            {
                "role": "assistant",
                "content": None,
                "tool_calls": [
                    {
                        "id": "acceptance-finish",
                        "type": "function",
                        "function": {
                            "name": "finish",
                            "arguments": json.dumps({"message": "acceptance complete"}),
                        },
                    }
                ],
            }
        )
        return LLMResponse(
            message=Message.from_llm_chat_message(message),
            metrics=self.metrics.get_snapshot(),
            raw_response=ModelResponse(
                id="acceptance-response",
                created=int(time.time()),
                model="test-model",
                choices=[Choices(index=0, finish_reason="tool_calls", message=message)],
            ),
        )

    async def acompletion(self, messages, tools=None, **kwargs):
        server_loop[:] = [asyncio.get_running_loop()]
        llm_entered.set()
        while not llm_release.is_set():
            await asyncio.sleep(0.01)
        return completion(self, messages, tools, **kwargs)

    def timed(client, method, path, **kwargs):
        started = time.monotonic()
        result = request(client, method, path, timeout=TARGET_SECONDS, **kwargs)
        elapsed = time.monotonic() - started
        require(elapsed < TARGET_SECONDS, "native HTTP responsiveness target exceeded")
        timings.append(elapsed)
        return result

    with (
        tempfile.TemporaryDirectory(prefix="fr563-") as temporary,
        patch.object(LLM, "completion", completion),
        patch.object(LLM, "acompletion", acompletion),
    ):
        directory = Path(temporary)
        with server_environment(directory) as (app, client, workspace):
            created = request(
                client,
                "POST",
                "/api/conversations",
                json={
                    "agent": {
                        "llm": {"model": "gpt-4o-mini", "api_key": "test"},
                        "tools": [],
                    },
                    "workspace": {"working_dir": str(workspace)},
                    "autotitle": False,
                },
            ).json()
            root = f"/api/conversations/{created['id']}"
            request(
                client,
                "POST",
                f"{root}/events",
                json={
                    "role": "user",
                    "content": [{"type": "text", "text": "acceptance"}],
                    "run": False,
                },
            )
            event = request(client, "GET", f"{root}/events/search", params={"limit": 1}).json()[
                "items"
            ][0]
            event_path = f"{root}/events/{event['id']}"
            # Search returns a window projection; compare the complete formal
            # event against the same single-event route before/after recovery.
            event = request(client, "GET", event_path).json()
            request(client, "POST", f"{root}/run")
            require(llm_entered.wait(5), "deterministic native LLM did not start")
            wait_status(client, root, "running")

            background_entered = threading.Barrier(3)
            reads_entered = threading.Barrier(8)
            release = threading.Event()
            counter_lock = threading.Lock()
            read_count = 0
            original_context = EventService._get_view_context_sync
            original_event = EventService._get_event_sync

            def blocked_context(self):
                background_entered.wait(timeout=5)
                require(release.wait(30), "background injection release timed out")
                return original_context(self)

            def blocked_event(self, event_id):
                nonlocal read_count
                with counter_lock:
                    read_count += 1
                    blocked = read_count <= 7
                if blocked:
                    reads_entered.wait(timeout=5)
                    require(release.wait(30), "formal injection release timed out")
                return original_event(self, event_id)

            with (
                patch.object(EventService, "_get_view_context_sync", blocked_context),
                patch.object(EventService, "_get_event_sync", blocked_event),
                ThreadPoolExecutor(max_workers=9) as pool,
            ):
                pending = []
                try:
                    pending.extend(
                        pool.submit(request, client, "GET", f"{root}/context") for _ in range(2)
                    )
                    background_entered.wait(timeout=5)
                    pending.extend(
                        pool.submit(request, client, "GET", event_path) for _ in range(7)
                    )
                    reads_entered.wait(timeout=5)
                    for _ in range(20):
                        require(
                            timed(client, "GET", event_path).json() == event,
                            "formal event identity/content changed",
                        )
                        timed(client, "GET", f"{root}/events/search", params={"limit": 1})
                    timed(client, "GET", root)
                    timed(client, "GET", "/health")
                    timed(client, "POST", f"{root}/interrupt")
                    wait_status(client, root, "paused")
                    require(not any(f.done() for f in pending), "injection ended early")
                finally:
                    release.set()
                    for future in pending:
                        future.result(timeout=10)
            report["running_background_and_seven_reads"] = "passed"
            report["interrupt_while_reads_blocked"] = "passed"

            # Cancel eight native awaiting coroutines. Physical synchronous work
            # must still occupy all eight threads, while HTTP control stays live.
            entered = threading.Barrier(9)
            release = threading.Event()
            calls = 0

            def saturated_event(self, event_id):
                nonlocal calls
                with counter_lock:
                    calls += 1
                entered.wait(timeout=5)
                require(release.wait(15), "cancel injection release timed out")
                return original_event(self, event_id)

            events = app.state.conversation_service._event_services[UUID(created["id"])]
            with (
                patch.object(EventService, "_get_event_sync", saturated_event),
                ThreadPoolExecutor(max_workers=1) as pool,
            ):
                pending_window = None
                cancelled = []
                try:
                    cancelled = [
                        asyncio.run_coroutine_threadsafe(
                            events.get_event(event["id"]), server_loop[0]
                        )
                        for _ in range(8)
                    ]
                    entered.wait(timeout=5)
                    for future in cancelled:
                        require(future.cancel(), "native read cancellation failed")
                    pending_window = pool.submit(
                        request, client, "GET", f"{root}/events/search", params={"limit": 1}
                    )
                    time.sleep(0.2)
                    require(not pending_window.done(), "cancel released physical capacity early")
                    require(calls == 8, "physical read capacity exceeded eight")
                    timed(client, "GET", "/health")
                    timed(client, "GET", root)
                finally:
                    release.set()
                    if pending_window is not None:
                        require(
                            bool(pending_window.result(5).json()["items"]), "read recovery failed"
                        )
            report["cancel_holds_threads_and_release_restores_http"] = "passed"

            llm_release.set()
            timed(client, "POST", f"{root}/run")
            wait_status(client, root, "finished")
            timed(client, "POST", "/api/conversations/prepare-for-sandbox-pause")
            require(
                UUID(created["id"]) not in app.state.conversation_service._event_services,
                "formal drain retained live event service",
            )
            require(timed(client, "GET", event_path).json() == event, "drain/reload lost event")
            report["drain_reload_preserves_event"] = "passed"

        # New HTTP server and service owner reuse only this test's persistence.
        with server_environment(directory) as (_, client, _):
            require(
                timed(client, "GET", root).json()["id"] == created["id"],
                "restart lost conversation",
            )
            require(
                timed(client, "GET", event_path).json() == event, "restart changed persisted event"
            )
            require(
                request(client, "GET", root).json()["execution_status"] == "finished",
                "restart lost native terminal lifecycle",
            )
        report["server_restart_preserves_conversation_and_event"] = "passed"

    timings.sort()
    report.update(
        status="ok",
        measured_requests=len(timings),
        p95_seconds=round(timings[math.ceil(len(timings) * 0.95) - 1], 4),
        max_seconds=round(max(timings), 4),
    )
    return report


if __name__ == "__main__":
    print(json.dumps(check(), sort_keys=True))
