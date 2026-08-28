from __future__ import annotations

import hashlib
import io
import zipfile
from pathlib import Path
from types import SimpleNamespace

import pytest

from flowweave.bootstrap.settings import Settings
from flowweave.runtime import workspace as runtime_workspace
from flowweave.runtime.workspace import (
    materialize_hook_config,
    materialize_node_workspace,
    materialize_runtime_memory,
)
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
                    "hook_set_schema_version": 1,
                    "openhands_version": "1.44.0",
                    "source_commit": "9a24f6c8866f353042a57df0514ccc900e3a0691",
                    "allowed_events": [
                        "post_tool_use",
                        "pre_tool_use",
                        "session_end",
                        "session_start",
                        "stop",
                        "user_prompt_submit",
                    ],
                    "runtime_mutation": "FORBIDDEN",
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


def test_governed_memory_uses_openhands_project_loader_and_read_only_source(
    tmp_path: Path,
    monkeypatch,
):
    settings = Settings(workspace_root=tmp_path / "workspaces")
    project_root = settings.workspace_root / ".flow-run-runtimes/scope/run/workspace/project"
    working_dir = project_root / "nodes/node-1/sessions/run-1/1"
    capability_root = (
        settings.workspace_root / ".flow-run-runtimes/scope/run/capabilities" / ("a" * 64)
    )
    working_dir.mkdir(parents=True)
    capability_root.mkdir(parents=True)
    monkeypatch.setattr(
        runtime_workspace,
        "flow_run_workspace_project_path",
        lambda _flow_run_id: project_root,
    )
    monkeypatch.setattr(
        runtime_workspace,
        "flow_run_capability_path",
        lambda _flow_run_id, _manifest_digest, *parts: capability_root.joinpath(*parts),
    )
    user_content = b"# User memory\n"
    project_content = b"# Project memory\n"
    materials = (
        SimpleNamespace(
            scope="USER",
            content=user_content,
            digest=hashlib.sha256(user_content).hexdigest(),
        ),
        SimpleNamespace(
            scope="PROJECT",
            content=project_content,
            digest=hashlib.sha256(project_content).hexdigest(),
        ),
    )

    with settings_context(settings):
        materialize_runtime_memory(
            flow_run_id="run",
            manifest_digest="a" * 64,
            workspace_ref=str(working_dir),
            materials=materials,
        )

        loader_index = working_dir / ".openhands/memory/MEMORY.md"
        assert loader_index.is_symlink()
        assert (
            loader_index.readlink()
            .as_posix()
            .startswith("/runtime/capabilities/" + "a" * 64 + "/memory/")
        )
        bundle_dir = next((capability_root / "memory").iterdir())
        source_index = bundle_dir / "MEMORY.md"
        assert source_index.read_bytes() == (
            b"# FlowWeave governed user memory\n# User memory\n\n"
            b"# FlowWeave governed project memory\n# Project memory\n"
        )
        assert source_index.stat().st_mode & 0o777 == 0o444
        assert bundle_dir.stat().st_mode & 0o777 == 0o555

        with pytest.raises(DomainError) as raised:
            materialize_runtime_memory(
                flow_run_id="run",
                manifest_digest="a" * 64,
                workspace_ref=str(working_dir),
                materials=(SimpleNamespace(scope="USER", content=b"tampered", digest="0" * 64),),
            )
        assert raised.value.code == "MEMORY_SOURCE_DIGEST_MISMATCH"
        assert source_index.is_file()


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


def test_hook_materialization_rejects_unversioned_hook_set(tmp_path: Path):
    content = b"print('allow')\n"
    settings = Settings(
        workspace_root=tmp_path / "workspaces",
        openhands_workspace_root=Path("/workspaces"),
    )
    store = LocalArtifactStore(tmp_path / "artifacts")
    storage_key = store.put("capability-imports/hook.zip", _bundle("check.py", content))
    asset = _asset(storage_key, "check.py", hashlib.sha256(content).hexdigest())
    normalized = asset["capabilities"][0]["normalized_config"]  # type: ignore[index]
    normalized.pop("hook_set_schema_version")  # type: ignore[union-attr]

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


def _plugin_bundle() -> tuple[bytes, dict[str, str]]:
    files = {
        ".plugin/plugin.json": b'{"name":"governed-review","version":"1.0.0"}',
        "skills/review/SKILL.md": b"# Review\n",
        "commands/check.md": b"# Check\n",
    }
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for filename, content in files.items():
            archive.writestr(filename, content)
    return output.getvalue(), {
        filename: hashlib.sha256(content).hexdigest() for filename, content in files.items()
    }


def test_plugin_is_materialized_read_only_at_frozen_runtime_path(tmp_path: Path):
    bundle, file_hashes = _plugin_bundle()
    content_hash = hashlib.sha256(bundle).hexdigest()
    settings = Settings(
        workspace_root=tmp_path / "workspaces",
        openhands_workspace_root=Path("/workspaces"),
        openhands_managed_assets_root=Path("/runtime/capabilities"),
    )
    store = LocalArtifactStore(tmp_path / "artifacts")
    storage_key = store.put("capability-imports/plugin.zip", bundle)
    asset = {
        "id": "node-1",
        "capabilities": [
            {
                "capability_type": "PLUGIN",
                "capability_key": "governed-review",
                "normalized_config": {
                    "entry": ".",
                    "package_format": "openhands-plugin-v1",
                    "capability_version_id": "11111111-1111-4111-8111-111111111111",
                    "content_hash": content_hash,
                    "storage_key": storage_key,
                    "file_hashes": file_hashes,
                },
            }
        ],
    }

    with settings_context(settings), artifact_store_context(store):
        skills, plugins, mcp_servers, workspace = materialize_node_workspace(asset)

    assert skills == ()
    assert mcp_servers == ()
    assert workspace == "/workspaces/nodes/node-1"
    assert len(plugins) == 1
    plugin = plugins[0]
    assert plugin.name == "governed-review"
    assert plugin.source == (
        "/runtime/capabilities/nodes/node-1/plugins/"
        "governed-review-11111111-1111-4111-8111-111111111111"
    )
    host_plugin = (
        settings.workspace_root / ".managed-assets/nodes/node-1/plugins/"
        "governed-review-11111111-1111-4111-8111-111111111111"
    )
    assert (host_plugin / ".plugin/plugin.json").is_file()
    assert (host_plugin / ".plugin/plugin.json").stat().st_mode & 0o777 == 0o444
    assert (host_plugin / "skills/review/SKILL.md").stat().st_mode & 0o777 == 0o444


def test_plugin_materialization_rejects_object_store_content_drift(tmp_path: Path):
    bundle, file_hashes = _plugin_bundle()
    settings = Settings(
        workspace_root=tmp_path / "workspaces",
        openhands_workspace_root=Path("/workspaces"),
    )
    store = LocalArtifactStore(tmp_path / "artifacts")
    storage_key = store.put("capability-imports/plugin.zip", b"tampered")
    asset = {
        "id": "node-1",
        "capabilities": [
            {
                "capability_type": "PLUGIN",
                "capability_key": "governed-review",
                "normalized_config": {
                    "entry": ".",
                    "package_format": "openhands-plugin-v1",
                    "capability_version_id": "11111111-1111-4111-8111-111111111111",
                    "content_hash": hashlib.sha256(bundle).hexdigest(),
                    "storage_key": storage_key,
                    "file_hashes": file_hashes,
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
