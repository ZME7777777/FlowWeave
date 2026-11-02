"""Isolated real-kernel terminal exhaustion and native process-death recovery."""

from __future__ import annotations

import argparse
import errno
import importlib.metadata
import json
import logging
import os
import signal
import socket
import subprocess
import sys
import tempfile
import time
import traceback
from pathlib import Path


def require(ok, message):
    if not ok:
        raise AssertionError(message)


def terminal_pressure():
    from openhands.tools.terminal.impl import TerminalExecutor
    from openhands.tools.terminal.definition import TerminalAction

    cgroup = Path("/sys/fs/cgroup")
    limit = int((cgroup / "pids.max").read_text())
    require(
        32 <= limit <= 256, "Run only in a disposable container with a small PID limit"
    )

    def hits():
        return int((cgroup / "pids.events").read_text().split("max ")[1].split()[0])

    with tempfile.TemporaryDirectory(prefix="incident-terminal-") as directory:
        executor = TerminalExecutor(directory)
        children = []
        before = hits()
        try:
            healthy = executor(TerminalAction(command="printf before-pressure"))
            require(not healthy.is_error, "Baseline terminal failed")
            fork_error = None
            for _ in range(limit + 1):
                try:
                    pid = os.fork()
                except OSError as exc:
                    fork_error = exc.errno
                    break
                if pid == 0:
                    signal.pause()
                    os._exit(0)
                children.append(pid)
            require(fork_error == errno.EAGAIN, "PID exhaustion did not reach EAGAIN")
            failure = {}
            try:
                observation = executor(TerminalAction(command="printf under-pressure"))
                failure = {
                    "observation_error": observation.is_error,
                    "errno11": "Errno 11" in observation.model_dump_json(),
                }
                require(
                    observation.is_error,
                    "Native terminal falsely reported success under PID exhaustion",
                )
            except (OSError, RuntimeError) as exc:
                failure = {
                    "type": type(exc).__name__,
                    "errno": getattr(exc, "errno", None),
                    "frames": [
                        Path(f.filename).name + ":" + f.name
                        for f in traceback.extract_tb(exc.__traceback__)[-8:]
                    ],
                }
            require(
                failure.get("errno") == errno.EAGAIN or failure.get("errno11") is True,
                "Native terminal did not reproduce the expected errno 11 failure",
            )
            require(hits() > before, "Kernel PID-limit hit counter did not advance")
        finally:
            for pid in children:
                try:
                    os.kill(pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
            for pid in children:
                os.waitpid(pid, 0)
        try:
            recovered = executor(TerminalAction(command="printf after-pressure"))
            require(
                not recovered.is_error,
                "Native terminal did not recover after releasing test children",
            )
        finally:
            executor.close()
        return {
            "scenario": "pid_exhaustion",
            "result": "PASS",
            "kernel_errno": fork_error,
            "native_failure": failure,
            "recovery": "PASS",
            "pid_limit": limit,
            "held_children": len(children),
        }


def serve(directory, port):
    os.environ["OH_PERSISTENCE_DIR"] = str(directory / "persistence")
    os.environ["OPENHANDS_AGENT_SERVER_CONFIG_PATH"] = str(directory / "server.json")
    os.environ.pop("SESSION_API_KEY", None)
    from litellm.types.utils import Choices, ModelResponse
    from litellm.types.utils import Message as LiteMessage
    from openhands.sdk import LLM, Message
    from openhands.sdk.llm.llm_response import LLMResponse

    def completion(self, messages, tools=None, **kwargs):
        message = LiteMessage.model_validate(
            {
                "role": "assistant",
                "content": None,
                "tool_calls": [
                    {
                        "id": "incident-call",
                        "type": "function",
                        "function": {
                            "name": "terminal",
                            "arguments": json.dumps({"command": "sleep 120"}),
                        },
                    }
                ],
            }
        )
        return LLMResponse(
            message=Message.from_llm_chat_message(message),
            metrics=self.metrics.get_snapshot(),
            raw_response=ModelResponse(
                id="incident-response",
                created=int(time.time()),
                model="test-model",
                choices=[Choices(index=0, finish_reason="tool_calls", message=message)],
            ),
        )

    async def acompletion(self, messages, tools=None, **kwargs):
        return completion(self, messages, tools, **kwargs)

    LLM.completion = completion
    LLM.acompletion = acompletion
    from openhands.agent_server.api import create_app
    from openhands.agent_server.config import Config
    import uvicorn

    app = create_app(
        Config.model_validate_json((directory / "server.json").read_text())
    )
    uvicorn.run(app, host="127.0.0.1", port=port, log_level="error")


def restart_recovery():
    import httpx

    with tempfile.TemporaryDirectory(prefix="incident-restart-") as temporary:
        directory = Path(temporary)
        workspace = directory / "workspace"
        workspace.mkdir()
        (directory / "server.json").write_text(
            json.dumps(
                {
                    "session_api_keys": [],
                    "conversations_path": str(directory / "conversations"),
                    "workspace_path": str(workspace),
                    "preload_tools": False,
                }
            )
        )
        sock = socket.socket()
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
        sock.close()
        processes = []
        with (
            (directory / "native.log").open("w") as log,
            httpx.Client(base_url=f"http://127.0.0.1:{port}", timeout=10) as client,
        ):

            def start():
                process = subprocess.Popen(
                    [
                        sys.executable,
                        __file__,
                        "--serve",
                        str(directory),
                        "--port",
                        str(port),
                    ],
                    stdout=log,
                    stderr=log,
                    start_new_session=True,
                )
                processes.append(process)
                deadline = time.monotonic() + 30
                while time.monotonic() < deadline:
                    require(
                        process.poll() is None,
                        "Native HTTP server exited during startup",
                    )
                    try:
                        if client.get("/health").status_code == 200:
                            return process
                    except httpx.HTTPError:
                        pass
                    time.sleep(0.1)
                raise AssertionError("Native HTTP server startup timed out")

            def request(method, path, **kwargs):
                response = client.request(method, path, **kwargs)
                response.raise_for_status()
                return response.json()

            try:
                original = start()
                created = request(
                    "POST",
                    "/api/conversations",
                    json={
                        "agent": {
                            "llm": {
                                "model": "gpt-4o-mini",
                                "api_key": "local-no-network",
                            },
                            "tools": [{"name": "terminal"}],
                        },
                        "workspace": {"working_dir": str(workspace)},
                        "autotitle": False,
                    },
                )
                root = f"/api/conversations/{created['id']}"
                request(
                    "POST",
                    root + "/events",
                    json={
                        "role": "user",
                        "content": [
                            {"type": "text", "text": "run the isolated terminal probe"}
                        ],
                        "run": True,
                    },
                )
                deadline = time.monotonic() + 15
                actions = []
                while time.monotonic() < deadline:
                    old_events = request(
                        "GET", root + "/events/search", params={"limit": 100}
                    )["items"]
                    actions = [e for e in old_events if e.get("kind") == "ActionEvent"]
                    if actions:
                        break
                    time.sleep(0.1)
                require(actions, "No formal native ActionEvent before process death")
                before_state = request("GET", root)
                require(
                    before_state["execution_status"] == "running",
                    "Native conversation was not running before process death",
                )
                original.kill()
                original.wait(timeout=10)
                # The real process is dead, but its on-disk native lease remains.
                # Respect takeover TTL rather than bypassing ownership checks.
                start()
                deadline = time.monotonic() + 65
                restored = None
                while time.monotonic() < deadline:
                    response = client.get(root)
                    if response.status_code == 200:
                        restored = response.json()
                        break
                    require(
                        response.status_code in (409, 423, 503),
                        "Unexpected native recovery HTTP status",
                    )
                    time.sleep(0.5)
                require(restored is not None, "Native lease takeover did not finish")
                events = request("GET", root + "/events/search", params={"limit": 100})[
                    "items"
                ]
                recovery = [
                    e
                    for e in events
                    if e.get("kind") == "AgentErrorEvent"
                    and "restart" in str(e.get("error", "")).lower()
                ]
                require(
                    restored["id"] == created["id"],
                    "Native conversation identity changed",
                )
                require(
                    restored["execution_status"] == "error",
                    "Native process-death recovery must expose error",
                )
                require(recovery, "Missing formal native restart recovery event")
                old_by_id = {e["id"]: e for e in old_events}
                new_by_id = {e["id"]: e for e in events}
                require(
                    all(new_by_id.get(k) == v for k, v in old_by_id.items()),
                    "Persisted native events changed during recovery",
                )
                require(
                    all(
                        any(
                            e.get("tool_call_id") == a.get("tool_call_id")
                            for e in recovery
                        )
                        for a in actions
                    ),
                    "Native recovery lost an interrupted tool identity",
                )
                assistant = [
                    e
                    for e in events
                    if e.get("kind") == "MessageEvent"
                    and e.get("llm_message", {}).get("role") == "assistant"
                ]
                require(not assistant, "Recovery fabricated a final assistant message")
                second = request("GET", root + "/events/search", params={"limit": 100})[
                    "items"
                ]
                require(
                    [e["id"] for e in second] == [e["id"] for e in events],
                    "Repeated reads duplicated recovery events",
                )
                return {
                    "scenario": "native_process_death",
                    "result": "PASS",
                    "execution_status": restored["execution_status"],
                    "preserved_events": len(old_events),
                    "recovery_events": len(recovery),
                    "assistant_messages": len(assistant),
                    "original_conversation": "PRESERVED",
                    "tool_call_identity": "PRESERVED",
                }
            finally:
                for process in processes:
                    if process.poll() is None:
                        process.kill()
                    process.wait(timeout=10)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--allow-isolated-fault-injection", action="store_true")
    parser.add_argument("--expected-source-commit")
    parser.add_argument(
        "--scenario", choices=["terminal", "restart", "all"], default="all"
    )
    parser.add_argument("--serve", type=Path)
    parser.add_argument("--port", type=int)
    args = parser.parse_args()
    logging.disable(logging.CRITICAL)
    os.environ["OPENHANDS_SUPPRESS_BANNER"] = "1"
    if args.serve:
        serve(args.serve, args.port)
        return
    require(
        args.allow_isolated_fault_injection and Path("/.dockerenv").is_file(),
        "Explicit disposable Docker fault-injection opt-in required",
    )
    lock = json.loads(Path("/runtime/openhands-source.lock.json").read_text())
    require(
        args.expected_source_commit
        and lock["source_commit"] == args.expected_source_commit,
        "Installed Runtime source identity mismatch",
    )
    print(
        json.dumps(
            {
                "source_commit": lock["source_commit"],
                "package_versions": {
                    name: importlib.metadata.version(name)
                    for name in [
                        "openhands-sdk",
                        "openhands-tools",
                        "openhands-agent-server",
                        "openhands-workspace",
                    ]
                },
            }
        ),
        flush=True,
    )
    if args.scenario in ("terminal", "all"):
        print(json.dumps(terminal_pressure()), flush=True)
    if args.scenario in ("restart", "all"):
        print(json.dumps(restart_recovery()), flush=True)


if __name__ == "__main__":
    main()
