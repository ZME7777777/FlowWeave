from __future__ import annotations

import hashlib
import io
import zipfile
from pathlib import Path

import pytest

from flowweave.bootstrap.settings import Settings
from flowweave.runtime.workspace import materialize_hook_config, materialize_node_workspace
from flowweave.shared.artifact_store import artifact_store_context
from flowweave.shared.errors import DomainError
from flowweave.shared.infrastructure.artifact_store import LocalArtifactStore
from flowweave.shared.settings import settings_context


def _bundle(filename: str, content: bytes) -> bytes:
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("config.json", b"{}")
        info = zipfile.ZipInfo(f"scripts/0/{filename}")
        info.external_attr = 0o755 << 16
        archive.writestr(info, content)
    return output.getvalue()


def _asset(storage_key: str, filename: str, digest: str) -> dict[str, object]:
    return {
        "id": "node-1",
        "capabilities": [
            {
                "capability_type": "HOOK",
                "capability_key": "guardrails",
                "normalized_config": {
                    "pre_tool_use": [
                        {
                            "matcher": "terminal",
                            "hooks": [
                                {
                                    "type": "script",
                                    "script": filename,
                                    "timeout": 30,
                                }
                            ],
                        }
                    ],
                    "script_files": [filename],
                    "script_hashes": {filename: digest},
                    "script_archive_prefix": "scripts/0",
                    "storage_key": storage_key,
                },
            }
        ],
    }


def test_hook_script_is_materialized_read_only_and_converted_to_openhands_command(
    tmp_path: Path,
):
    content = b"print('allow')\n"
    filename = "check.py"
    settings = Settings(
        workspace_root=tmp_path / "workspaces",
        openhands_workspace_root=Path("/workspaces"),
    )
    store = LocalArtifactStore(tmp_path / "artifacts")
    storage_key = store.put("capability-imports/hook.zip", _bundle(filename, content))
    asset = _asset(storage_key, filename, hashlib.sha256(content).hexdigest())

    with settings_context(settings), artifact_store_context(store):
        config = materialize_hook_config(asset)

    script_path = (
        settings.workspace_root / ".managed-assets/nodes/node-1/hooks/guardrails/scripts/check.py"
    )
    assert script_path.read_bytes() == content
    assert script_path.stat().st_mode & 0o777 == 0o555
    assert config == {
        "pre_tool_use": [
            {
                "matcher": "terminal",
                "hooks": [
                    {
                        "type": "command",
                        "command": (
                            "python /runtime/capabilities/nodes/node-1/"
                            "hooks/guardrails/scripts/check.py"
                        ),
                        "timeout": 30,
                    }
                ],
            }
        ]
    }


def test_hook_script_materialization_rejects_digest_mismatch(tmp_path: Path):
    content = b"print('tampered')\n"
    settings = Settings(
        workspace_root=tmp_path / "workspaces",
        openhands_workspace_root=Path("/workspaces"),
    )
    store = LocalArtifactStore(tmp_path / "artifacts")
    storage_key = store.put("capability-imports/hook.zip", _bundle("check.py", content))
    asset = _asset(storage_key, "check.py", hashlib.sha256(b"expected").hexdigest())

    with (
        settings_context(settings),
        artifact_store_context(store),
        pytest.raises(DomainError) as raised,
    ):
        materialize_hook_config(asset)

    assert raised.value.code == "RUNTIME_CAPABILITY_UNAVAILABLE"


def test_hook_materialization_ignores_workspace_controlled_symlink(tmp_path: Path):
    content = b"print('allow')\n"
    settings = Settings(
        workspace_root=tmp_path / "workspaces",
        openhands_workspace_root=Path("/workspaces"),
    )
    store = LocalArtifactStore(tmp_path / "artifacts")
    storage_key = store.put("capability-imports/hook.zip", _bundle("check.py", content))
    asset = _asset(storage_key, "check.py", hashlib.sha256(content).hexdigest())
    attacker_target = tmp_path / "attacker-target"
    attacker_target.mkdir()
    node_root = settings.workspace_root / "nodes/node-1"
    node_root.mkdir(parents=True)
    (node_root / "hooks").symlink_to(attacker_target, target_is_directory=True)

    with settings_context(settings), artifact_store_context(store):
        config = materialize_hook_config(asset)

    assert list(attacker_target.iterdir()) == []
    assert (
        settings.workspace_root / ".managed-assets/nodes/node-1/hooks/guardrails/scripts/check.py"
    ).read_bytes() == content
    assert config["pre_tool_use"][0]["hooks"][0]["command"].startswith(
        "python /runtime/capabilities/"
    )


def test_mcp_script_materialization_rejects_digest_mismatch(tmp_path: Path):
    content = b"print('tampered')\n"
    settings = Settings(
        workspace_root=tmp_path / "workspaces",
        openhands_workspace_root=Path("/workspaces"),
    )
    store = LocalArtifactStore(tmp_path / "artifacts")
    storage_key = store.put("capability-imports/mcp.zip", _bundle("server.py", content))
    asset = {
        "id": "node-1",
        "capabilities": [
            {
                "capability_type": "MCP",
                "capability_key": "local-tools",
                "normalized_config": {
                    "transport": "stdio",
                    "command": "python",
                    "args": ["scripts/server.py"],
                    "script_files": ["server.py"],
                    "script_hashes": {"server.py": hashlib.sha256(b"expected").hexdigest()},
                    "script_archive_prefix": "scripts/0",
                    "storage_key": storage_key,
                },
            }
        ],
    }

    with (
        settings_context(settings),
        artifact_store_context(store),
        pytest.raises(DomainError) as raised,
    ):
        materialize_node_workspace(asset)

    assert raised.value.code == "RUNTIME_CAPABILITY_UNAVAILABLE"
