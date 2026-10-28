"""Probe governed history or Case context MCP with the source-lock SDK, without an LLM.

Run under an isolated environment built from the locked vendor archive. All
mutable state belongs in --state-dir under the caller's repository .tmp/.
The fixture credential is read from HISTORY_MCP_PROBE_TOKEN, never an argument.
"""

from __future__ import annotations

import argparse
import hashlib
import importlib.metadata
import json
import logging
import os
import secrets
import tarfile
from pathlib import Path
from urllib.parse import urlsplit


def verify_source() -> str:
    import openhands.sdk

    root = Path(__file__).parent
    lock = json.loads((root / "source.lock.json").read_text())
    archive = root / "vendor" / f"openhands-source-{lock['source_commit']}.tar.gz"
    assert hashlib.sha256(archive.read_bytes()).hexdigest() == lock["archive_sha256"]
    assert importlib.metadata.version("openhands-sdk") == lock["package_version"]
    sdk_root = Path(openhands.sdk.__file__).resolve().parents[2]
    checked = 0
    with tarfile.open(archive) as source:
        for entry in source.getmembers():
            marker = "openhands-sdk/openhands/sdk/"
            if (
                not entry.isfile()
                or marker not in entry.name
                or not entry.name.endswith(".py")
            ):
                continue
            relative = entry.name[entry.name.index(marker) + len("openhands-sdk/") :]
            stream = source.extractfile(entry)
            assert stream is not None
            assert (sdk_root / relative).read_bytes() == stream.read(), (
                f"SDK source drift: {relative}"
            )
            checked += 1
    assert checked > 100
    return str(lock["source_commit"])


def check(
    marketplace: Path,
    plugin_name: str,
    state_dir: Path,
    source_binding_id: str | None = None,
    restored_source_binding_id: str | None = None,
    initial_config: bool = False,
    case_context: bool = False,
) -> dict[str, object]:
    os.environ["OPENHANDS_SUPPRESS_BANNER"] = "1"
    logging.disable(logging.CRITICAL)
    state_dir.mkdir(parents=True, exist_ok=False)
    os.environ["OH_PERSISTENCE_DIR"] = str(state_dir / "auxiliary")
    commit = verify_source()

    from pydantic import SecretStr

    from openhands.sdk import Agent, LLM
    from openhands.sdk.context import AgentContext
    from openhands.sdk.conversation import LocalConversation
    from openhands.sdk.plugin import Plugin
    from openhands.sdk.secret import StaticSecret
    from openhands.sdk.utils.cipher import Cipher

    token = os.environ.pop("HISTORY_MCP_PROBE_TOKEN")
    secret_name = "FLOWWEAVE_CASE_ACCESS" if case_context else "FLOWWEAVE_HISTORY_ACCESS"
    tool_name = "read_case_context" if case_context else "read_referenced_conversation"
    plugin_root = marketplace / "plugins" / plugin_name
    plugin = Plugin.load(plugin_root)
    assert len(plugin.mcp_config) == 1, (
        "Native loader did not expose exactly one MCP server"
    )
    server_name, server = next(iter(plugin.mcp_config.items()))
    assert server.url and urlsplit(server.url).hostname in {"127.0.0.1", "localhost"}
    assert server.headers and server.headers["Authorization"].get_secret_value() == (
        "Bearer ${" + secret_name + "}"
    )
    cipher = Cipher(secrets.token_hex(32))

    def agent() -> Agent:
        return Agent(
            llm=LLM(
                model="openai/gpt-4o-mini",
                api_key=SecretStr("no-model-calls"),
                base_url="http://127.0.0.1:9",
            ),
            tools=[],
            agent_context=AgentContext(
                registered_marketplaces=[
                    {
                        "name": "history-probe",
                        "source": str(marketplace),
                        "auto_load": False,
                    }
                ]
            ),
        )

    initial_agent = agent()
    if initial_config:
        initial_agent = initial_agent.model_copy(
            update={"mcp_config": plugin.mcp_config}
        )
    conversation = LocalConversation(
        agent=initial_agent,
        workspace=state_dir / "workspace",
        persistence_dir=state_dir / "state",
        visualizer=None,
        cipher=cipher,
        delete_on_close=False,
    )
    try:
        if initial_config:
            conversation.update_secrets(
                {secret_name: StaticSecret(value=SecretStr(token))}
            )
            conversation.send_message(
                "Initial MCP contract probe; do not execute a model turn."
            )
        else:
            # A real initialized existing conversation, with no model execution.
            conversation.send_message(
                "Local contract probe; do not execute a model turn."
            )
            conversation.update_secrets(
                {secret_name: StaticSecret(value=SecretStr(token))}
            )
            conversation.load_plugin(f"{plugin_name}@history-probe")
        configured = conversation.agent.mcp_config[server_name]
        assert (
            configured.headers["Authorization"].get_secret_value() == f"Bearer {token}"
        )
        tools = [
            tool
            for name, tool in conversation.agent.tools_map.items()
            if name.endswith(tool_name)
        ]
        assert len(tools) == 1
        tool = tools[0]
        arguments = (
            {"source_binding_id": source_binding_id} if source_binding_id else {}
        )
        result = tool.as_executable().executor(
            tool.action_from_arguments(arguments), conversation
        )
        assert not result.is_error, "Native MCP invocation failed"
        identity = conversation.id
        event_ids = tuple(event.id for event in conversation.state.events)
        assert event_ids
    finally:
        conversation.close()

    files = tuple((state_dir / "state").rglob("*.json"))
    assert files
    for path in (*files, *tuple(marketplace.rglob("*.json"))):
        assert token.encode() not in path.read_bytes(), (
            f"Plaintext credential in {path.name}"
        )
    restored = LocalConversation(
        agent=None,
        workspace=state_dir / "workspace",
        persistence_dir=state_dir / "state",
        conversation_id=identity,
        visualizer=None,
        cipher=cipher,
        delete_on_close=False,
    )
    try:
        assert restored.id == identity
        assert tuple(event.id for event in restored.state.events) == event_ids
        assert (
            restored.state.secret_registry.get_secret_value(secret_name)
            == token
        )
        assert restored.agent.mcp_config[server_name].headers[
            "Authorization"
        ].get_secret_value() == (f"Bearer {token}")
        restored.send_message("Verify restored native tools; do not run a model turn.")
        restored_tools = [
            tool
            for name, tool in restored.agent.tools_map.items()
            if name.endswith(tool_name)
        ]
        assert len(restored_tools) == 1
        tool = restored_tools[0]
        restored_source = restored_source_binding_id or source_binding_id
        arguments = {"source_binding_id": restored_source} if restored_source else {}
        result = tool.as_executable().executor(
            tool.action_from_arguments(arguments), restored
        )
        assert not result.is_error, "Restored native MCP invocation failed"
    finally:
        restored.close()
    return {
        "source_commit": commit,
        "status": "passed",
        "events": len(event_ids),
        "checks": [
            "native_initial_mcp" if initial_config else "native_load_plugin",
            "native_mcp_call",
            "encrypted_restore",
            "restored_native_mcp_call",
            "event_identity",
        ],
    }


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--marketplace", type=Path, required=True)
    parser.add_argument("--plugin-name", required=True)
    parser.add_argument("--state-dir", type=Path, required=True)
    parser.add_argument("--source-binding-id")
    parser.add_argument("--restored-source-binding-id")
    parser.add_argument("--initial-config", action="store_true")
    parser.add_argument("--case-context", action="store_true")
    args = parser.parse_args()
    print(
        json.dumps(
            check(
                args.marketplace.resolve(),
                args.plugin_name,
                args.state_dir.resolve(),
                args.source_binding_id,
                args.restored_source_binding_id,
                args.initial_config,
                args.case_context,
            )
        )
    )
