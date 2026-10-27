"""Isolated native server for the opt-in FlowWeave transport test.

Run with the four noneditable packages installed from source.lock.json. Only
LLM completion is controlled; native tools, provenance, routes and persistence
are real. No FlowWeave authorization service is installed in this fixture.
"""

from __future__ import annotations

import hashlib
import json
import os
import secrets
import socket
import sys
import tarfile
import threading
import time
from importlib.metadata import distribution
from pathlib import Path
from uuid import uuid4


def main():
    state, repo = map(Path, sys.argv[1:])
    lock = json.loads((repo / "infra/openhands/source.lock.json").read_text())
    archive = repo / "infra/openhands/vendor" / f"openhands-source-{lock['source_commit']}.tar.gz"
    assert hashlib.sha256(archive.read_bytes()).hexdigest() == lock["archive_sha256"]
    checked = 0
    with tarfile.open(archive) as source:
        for package in lock["packages"]:
            installed = distribution(package)
            assert installed.version == lock["package_version"]
            prefix = f"software-agent-sdk-{lock['source_commit']}/{package}/"
            for member in source.getmembers():
                if member.name.startswith(prefix + "openhands/") and member.name.endswith(".py"):
                    stream = source.extractfile(member)
                    assert stream is not None
                    assert (
                        stream.read()
                        == installed.locate_file(member.name[len(prefix) :]).read_bytes()
                    )
                    checked += 1
    assert checked >= 500

    runtime_key = secrets.token_urlsafe(32)
    workspace = state / "workspace"
    workspace.mkdir()
    config = {
        "session_api_keys": [runtime_key],
        "secret_key": secrets.token_urlsafe(32),
        "conversations_path": str(state / "conversations"),
        "workspace_path": str(workspace),
        "bash_events_dir": str(state / "bash-events"),
        "preload_tools": False,
    }
    config_file = state / "config.json"
    config_file.write_text(json.dumps(config))
    config_file.chmod(0o600)
    os.environ["OPENHANDS_AGENT_SERVER_CONFIG_PATH"] = str(config_file)
    os.environ["OH_PERSISTENCE_DIR"] = str(state / ".openhands")
    os.environ["OH_SESSION_API_KEYS_0"] = runtime_key

    import httpx
    import uvicorn
    from litellm.types.utils import Choices, ModelResponse
    from litellm.types.utils import Message as LiteLLMMessage
    from openhands.agent_server.api import create_app
    from openhands.agent_server.config import Config
    from openhands.sdk import LLM, Message, TextContent
    from openhands.sdk.conversation.child_launcher import GOVERNED_CHILD_TOOL
    from openhands.sdk.llm import MessageToolCall
    from openhands.sdk.llm.llm_response import LLMResponse
    from openhands.sdk.llm.utils.metrics import MetricsSnapshot
    from openhands.tools.child_conversation import governed

    signed = []
    original_issue = governed.issue_child_launcher_proof

    def capture(invocation, cipher):
        token = original_issue(invocation, cipher)
        signed.append((invocation, token))
        return token

    governed.issue_child_launcher_proof = capture

    async def completion(self, messages, tools=None, **kwargs):
        user_index = max(i for i, message in enumerate(messages) if message.role == "user")
        text = "".join(
            part.text for part in messages[user_index].content if isinstance(part, TextContent)
        )
        delegate = text == "delegate" and not any(
            m.role == "tool" for m in messages[user_index + 1 :]
        )
        call = (
            MessageToolCall(
                id=str(uuid4()),
                name=GOVERNED_CHILD_TOOL,
                arguments=json.dumps({"task": "review child 🌳", "title": "child"}),
                origin="completion",
            )
            if delegate
            else None
        )
        message = Message(
            role="assistant",
            content=[] if call else [TextContent(text="done")],
            tool_calls=[call] if call else None,
        )
        raw = ModelResponse(
            id=str(uuid4()),
            choices=[
                Choices(
                    index=0,
                    finish_reason="tool_calls" if call else "stop",
                    message=LiteLLMMessage(
                        role="assistant",
                        content=None if call else "done",
                        tool_calls=[
                            {
                                "id": call.id,
                                "type": "function",
                                "function": {
                                    "name": call.name,
                                    "arguments": call.arguments,
                                },
                            }
                        ]
                        if call
                        else None,
                    ),
                )
            ],
        )
        return LLMResponse(message=message, raw_response=raw, metrics=MetricsSnapshot())

    LLM.acompletion = completion
    with socket.socket() as bound:
        bound.bind(("127.0.0.1", 0))
        port = bound.getsockname()[1]
    host = f"http://127.0.0.1:{port}"
    app = create_app(Config.model_validate(config))
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="error"))
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    try:
        with httpx.Client(
            base_url=host, headers={"X-Session-API-Key": runtime_key}, trust_env=False, timeout=10
        ) as client:
            deadline = time.monotonic() + 20
            while True:
                try:
                    if client.get("/health").status_code == 200:
                        break
                except httpx.HTTPError:
                    pass
                assert time.monotonic() < deadline, "Native server startup timeout"
                time.sleep(0.05)
            parent = str(uuid4())
            root = f"/api/conversations/{parent}"
            policy = {
                "parent_conversation_id": parent,
                "scope_id": str(uuid4()),
                "runtime_session_id": str(uuid4()),
                "generation": 1,
                "manifest_digest": "c" * 64,
            }
            created = client.post(
                "/api/conversations",
                json={
                    "conversation_id": parent,
                    "autotitle": False,
                    "agent": {
                        "kind": "Agent",
                        "llm": {"model": "openai/test", "api_key": "unused"},
                        "tools": [
                            {
                                "name": GOVERNED_CHILD_TOOL,
                                "params": {
                                    "policy": policy,
                                    "launch_url": host + root + "/children/launcher/verify",
                                },
                            }
                        ],
                    },
                    "tool_module_qualnames": {
                        GOVERNED_CHILD_TOOL: "openhands.tools.child_conversation.governed"
                    },
                    "workspace": {"kind": "LocalWorkspace", "working_dir": str(workspace)},
                },
            )
            assert created.status_code == 201, "Native parent creation failed"
            sent = client.post(
                root + "/events",
                json={
                    "role": "user",
                    "content": [{"type": "text", "text": "delegate"}],
                    "run": True,
                },
            )
            assert sent.status_code == 200, "Native parent invocation failed"
            deadline = time.monotonic() + 20
            while client.get(root).json()["execution_status"] != "finished":
                assert time.monotonic() < deadline, "Native parent did not finish"
                time.sleep(0.05)
            assert len(signed) == 1, "Native tool did not issue exactly one proof"
            invocation, token = signed[0]
            # Fixture-only native property; NOT a field in verifier HTTP JSON.
            ready = {
                "host": host,
                "runtime_key": runtime_key,
                "parent": parent,
                "creation": str(invocation.creation_id),
                "token": token.get_secret_value(),
                "workspace": str(workspace),
                "source_commit": lock["source_commit"],
                "verified_python_files": checked,
            }
            ready_file = state / "ready.json"
            ready_file.write_text(json.dumps(ready))
            ready_file.chmod(0o600)
        sys.stdin.readline()
    finally:
        server.should_exit = True
        thread.join(timeout=15)
        assert not thread.is_alive(), "Native server failed to stop"


if __name__ == "__main__":
    main()
