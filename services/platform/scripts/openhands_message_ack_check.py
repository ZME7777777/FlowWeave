"""Exercise the platform acknowledgement code over actual isolated native HTTP.

Run inside the fixed OpenHands image with the platform source and
infra/openhands/read_capacity_check.py available. No external network or model
is required. The production SDK is unchanged; only this process's LLM completion
returns a deterministic finish. Bootstrap ancestry is loaded from its exact
source function to avoid importing unrelated platform/database integrations.
"""

from __future__ import annotations

import ast
import asyncio
import json
import logging
import os
import runpy
import socket
import sys
import tempfile
import threading
import time
from contextlib import contextmanager
from pathlib import Path
from unittest.mock import patch


@contextmanager
def native_server(directory: Path):
    import httpx
    import uvicorn

    config = directory / "config.json"
    config.write_text(
        json.dumps(
            {
                "session_api_keys": [],
                "conversations_path": "/runtime/state/conversations",
                "workspace_path": "/runtime/workspace/project",
                "preload_tools": False,
            }
        )
    )
    os.environ["OPENHANDS_AGENT_SERVER_CONFIG_PATH"] = str(config)
    os.environ["OH_PERSISTENCE_DIR"] = str(directory / "persistence")
    os.environ.pop("SESSION_API_KEY", None)
    from openhands.agent_server.api import create_app
    from openhands.agent_server.config import Config
    from openhands.agent_server.persistence import reset_stores

    reset_stores()
    server = uvicorn.Server(
        uvicorn.Config(
            create_app(Config.model_validate_json(config.read_text())),
            log_level="error",
        )
    )
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    thread = threading.Thread(target=lambda: server.run(sockets=[sock]), daemon=True)
    thread.start()
    try:
        deadline = time.monotonic() + 15
        while not server.started and time.monotonic() < deadline:
            assert thread.is_alive(), "Native HTTP server exited"
            time.sleep(0.025)
        assert server.started, "Native HTTP server startup timeout"
        with httpx.Client(
            base_url=f"http://127.0.0.1:{sock.getsockname()[1]}", timeout=10
        ) as client:
            yield client
    finally:
        server.should_exit = True
        thread.join(timeout=15)
        assert not thread.is_alive()
        sock.close()
        reset_stores()


def check(repo: Path) -> dict[str, object]:
    sys.path.insert(0, str(repo / "services/platform/src"))
    helper = runpy.run_path(str(repo / "infra/openhands/read_capacity_check.py"))
    from litellm.types.utils import Choices, ModelResponse
    from litellm.types.utils import Message as LiteLLMMessage
    from openhands.sdk import LLM, Message
    from openhands.sdk.llm.llm_response import LLMResponse

    from flowweave.bootstrap.settings import Settings
    from flowweave.runtime.base import RuntimeHandle
    from flowweave.runtime.openhands import OpenHandsRuntime

    def completion(self, messages, tools=None, **kwargs):
        message = LiteLLMMessage.model_validate(
            {
                "role": "assistant",
                "content": None,
                "tool_calls": [
                    {
                        "id": "ack-finish",
                        "type": "function",
                        "function": {
                            "name": "finish",
                            "arguments": json.dumps({"message": "acknowledged"}),
                        },
                    }
                ],
            }
        )
        return LLMResponse(
            message=Message.from_llm_chat_message(message),
            metrics=self.metrics.get_snapshot(),
            raw_response=ModelResponse(
                id="ack-response",
                created=int(time.time()),
                model="test-model",
                choices=[Choices(index=0, finish_reason="tool_calls", message=message)],
            ),
        )

    async def acompletion(self, messages, tools=None, **kwargs):
        return completion(self, messages, tools, **kwargs)

    source = (
        repo / "services/platform/src/flowweave/modules/agent_sessions/application/conversations.py"
    )
    definition = next(
        node
        for node in ast.parse(source.read_text()).body
        if isinstance(node, ast.FunctionDef) and node.name == "_initial_user_event_id"
    )
    namespace: dict = {}
    with (
        tempfile.TemporaryDirectory(prefix="message-ack-") as temporary,
        patch.object(LLM, "completion", completion),
        patch.object(LLM, "acompletion", acompletion),
        native_server(Path(temporary)) as client,
    ):
        workspace = Path("/runtime/workspace/project/users/00000000-0000-0000-0000-000000000001")
        workspace.mkdir(parents=True, exist_ok=True)
        runtime = OpenHandsRuntime(Settings())
        # Routing is test-local; all acknowledgement and HTTP transport methods
        # remain the real platform implementation.
        runtime._base_url_for_handle = lambda _handle: str(client.base_url).rstrip("/")
        runtime._session_key_for_handle = lambda _handle: ""
        namespace["get_runtime"] = lambda: runtime
        exec(
            compile(ast.Module(body=[definition], type_ignores=[]), str(source), "exec"), namespace
        )
        try:
            created = helper["request"](
                client,
                "POST",
                "/api/conversations",
                json={
                    "agent": {
                        "llm": {"model": "gpt-4o-mini", "api_key": "no-external-call"},
                        "tools": [],
                    },
                    "workspace": {"working_dir": str(workspace)},
                    "autotitle": False,
                },
            ).json()
            handle = RuntimeHandle(
                "agent-workspace:ack-check", created["id"], workspace_root=str(workspace)
            )
            root = f"/api/conversations/{created['id']}"
            before = helper["request"](client, "GET", root).json().get("leaf_event_id")
            assert before is None
            first = runtime.send_message(handle, "first")
            helper["wait_status"](client, root, "finished")
            assert first.cursor
            recovered = namespace["_initial_user_event_id"](handle, before)
            assert recovered == first.cursor, "lost-response bootstrap recovery failed"
            # Reconciliation itself must remain read-only and never append.
            assert runtime._message_user_event_ids(handle) == frozenset({first.cursor})
            second = runtime.send_message(handle, "continue")
            helper["wait_status"](client, root, "finished")
            assert second.cursor and second.cursor != first.cursor
            assert runtime._message_user_event_ids(handle) == frozenset(
                {first.cursor, second.cursor}
            )
            assert namespace["_initial_user_event_id"](handle, before) == first.cursor
            return {
                "native_http_first_message": "PASS",
                "native_http_continue": "PASS",
                "lazy_system_prompt_recovery": "PASS",
                "no_replayed_message": "PASS",
                "formal_user_event_count": 2,
            }
        finally:
            asyncio.run(runtime.aclose())


if __name__ == "__main__":
    logging.disable(logging.CRITICAL)
    print(json.dumps(check(Path(__file__).resolve().parents[3])))
