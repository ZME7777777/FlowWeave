from __future__ import annotations

import io
import json
import os
import re
import zipfile
from pathlib import Path, PurePosixPath
from typing import Any, cast
from uuid import uuid4

from flowweave.runtime.base import RuntimeMCP, RuntimeSkill
from flowweave.shared.artifact_store import get_artifact_store
from flowweave.shared.errors import DomainError
from flowweave.shared.settings import get_settings

_SAFE_SEGMENT = re.compile(r"[^A-Za-z0-9._-]+")
_DEPENDENCY_MAX_FILES = 20_000
_DEPENDENCY_MAX_EXPANDED_BYTES = 100 * 1024 * 1024
_MCP_KEYS = {
    "url",
    "transport",
    "command",
    "args",
    "env",
    "cwd",
    "description",
    "icon",
    "timeout",
    "sse_read_timeout",
    "keep_alive",
    "headers",
    "auth",
    "enabled",
}


def _segment(value: object, fallback: str) -> str:
    normalized = _SAFE_SEGMENT.sub("-", str(value or "").strip()).strip(".-")
    return (normalized or fallback)[:160]


def node_workspace_relative(asset_id: str) -> Path:
    return Path("nodes") / _segment(asset_id, "node")


def node_workspace_path(asset_id: str) -> Path:
    return Path(get_settings().workspace_root).resolve() / node_workspace_relative(asset_id)


def openhands_node_workspace_path(asset_id: str) -> Path:
    return Path(get_settings().openhands_workspace_root) / node_workspace_relative(asset_id)


def attempt_workspace_path(
    *, asset_id: str, run_id: str, node_run_id: str, attempt_no: int
) -> Path:
    return (
        node_workspace_path(asset_id)
        / "sessions"
        / _segment(run_id, "run")
        / _segment(node_run_id, "node-run")
        / str(attempt_no)
    )


def _atomic_write(path: Path, content: bytes, mode: int = 0o644) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{uuid4().hex}.tmp")
    temporary.write_bytes(content)
    temporary.chmod(mode)
    os.replace(temporary, path)


def _extract_dependencies(
    capability: dict[str, Any], host_root: Path, runtime_root: Path, key: str
) -> str:
    normalized = cast(dict[str, Any], capability.get("normalized_config") or {})
    dependencies = normalized.get("dependencies")
    if not isinstance(dependencies, dict) or not dependencies:
        return ""
    if normalized.get("dependency_build_state") != "READY":
        raise DomainError(
            "RUNTIME_CAPABILITY_DEPENDENCIES_UNAVAILABLE",
            "A selected Skill dependency bundle is not ready",
            409,
            {"capability_key": capability.get("capability_key")},
        )
    storage_key = str(normalized.get("dependency_storage_key") or "")
    if not storage_key:
        raise DomainError(
            "RUNTIME_CAPABILITY_DEPENDENCIES_UNAVAILABLE",
            "A selected Skill dependency bundle is missing",
            409,
            {"capability_key": capability.get("capability_key")},
        )
    host_directory = host_root / ".runtime" / key
    runtime_directory = runtime_root / ".runtime" / key
    try:
        bundle = get_artifact_store().read(storage_key)
        with zipfile.ZipFile(io.BytesIO(bundle)) as archive:
            files = [item for item in archive.infolist() if not item.is_dir()]
            if len(files) > _DEPENDENCY_MAX_FILES:
                raise ValueError("dependency bundle contains too many files")
            expanded = 0
            for item in files:
                source = PurePosixPath(item.filename.replace("\\", "/"))
                mode = (item.external_attr >> 16) & 0o170000
                if source.is_absolute() or ".." in source.parts or mode == 0o120000:
                    raise ValueError("unsafe dependency bundle path")
                expanded += item.file_size
                if expanded > _DEPENDENCY_MAX_EXPANDED_BYTES:
                    raise ValueError("dependency bundle is too large")
                destination = (host_directory / Path(*source.parts)).resolve()
                if not destination.is_relative_to(host_directory.resolve()):
                    raise ValueError("unsafe dependency extraction path")
                _atomic_write(destination, archive.read(item), mode=0o444)
        python_dir = runtime_directory / "python"
        node_dir = runtime_directory / "node" / "node_modules"
        bin_dir = host_directory / "bin"
        runtime_bin = runtime_directory / "bin"
        _atomic_write(
            bin_dir / "python",
            (f"#!/bin/sh\nPYTHONPATH='{python_dir}' exec python \"$@\"\n").encode(),
            mode=0o555,
        )
        _atomic_write(
            bin_dir / "node",
            (f"#!/bin/sh\nNODE_PATH='{node_dir}' exec node \"$@\"\n").encode(),
            mode=0o555,
        )
        return str(runtime_bin)
    except (FileNotFoundError, KeyError, OSError, ValueError, zipfile.BadZipFile) as exc:
        raise DomainError(
            "RUNTIME_CAPABILITY_DEPENDENCIES_UNAVAILABLE",
            "A selected Skill dependency bundle cannot be materialized",
            409,
            {"capability_key": capability.get("capability_key")},
        ) from exc


def _package(capability: dict[str, Any]) -> tuple[bytes, dict[str, Any], str]:
    normalized = cast(dict[str, Any], capability.get("normalized_config") or {})
    storage_key = str(normalized.get("storage_key") or "")
    entry = str(normalized.get("entry") or "")
    if not storage_key or not entry:
        raise DomainError(
            "RUNTIME_CAPABILITY_UNAVAILABLE",
            "A selected Skill does not have a persisted package",
            422,
            {"capability_key": capability.get("capability_key")},
        )
    try:
        return get_artifact_store().read(storage_key), normalized, entry
    except FileNotFoundError as exc:
        raise DomainError(
            "RUNTIME_CAPABILITY_UNAVAILABLE",
            "A selected Skill package cannot be loaded",
            422,
            {"capability_key": capability.get("capability_key")},
        ) from exc


def _extract_skill(capability: dict[str, Any], host_root: Path, runtime_root: Path) -> RuntimeSkill:
    archive_bytes, normalized, entry = _package(capability)
    key = _segment(capability.get("capability_key"), "skill")
    dependency_runtime_path = _extract_dependencies(capability, host_root, runtime_root, key)
    target_root = host_root / "skills" / key
    runtime_target_root = runtime_root / "skills" / key
    entry_path = PurePosixPath(entry)
    prefix = entry_path.parent
    try:
        with zipfile.ZipFile(io.BytesIO(archive_bytes)) as archive:
            for item in archive.infolist():
                if item.is_dir():
                    continue
                source = PurePosixPath(item.filename.replace("\\", "/"))
                if source.is_absolute() or ".." in source.parts:
                    raise ValueError("unsafe skill package path")
                if (
                    "__MACOSX" in source.parts
                    or source.name == ".DS_Store"
                    or source.name.startswith("._")
                ):
                    continue
                if prefix != PurePosixPath("."):
                    try:
                        relative = source.relative_to(prefix)
                    except ValueError:
                        continue
                else:
                    relative = source
                destination = (target_root / Path(*relative.parts)).resolve()
                if not destination.is_relative_to(target_root.resolve()):
                    raise ValueError("unsafe skill extraction path")
                archived_mode = (item.external_attr >> 16) & 0o777
                mode = archived_mode or 0o644
                if source.suffix == ".sh":
                    mode |= 0o111
                _atomic_write(destination, archive.read(item), mode=mode)
            relative_entry = (
                entry_path.relative_to(prefix) if prefix != PurePosixPath(".") else entry_path
            )
            content = (target_root / Path(*relative_entry.parts)).read_text(encoding="utf-8")
    except (KeyError, OSError, UnicodeDecodeError, ValueError, zipfile.BadZipFile) as exc:
        raise DomainError(
            "RUNTIME_CAPABILITY_UNAVAILABLE",
            "A selected Skill package cannot be materialized",
            422,
            {"capability_key": capability.get("capability_key")},
        ) from exc
    return RuntimeSkill(
        name=str(capability.get("capability_key") or key),
        content=content,
        description=str(normalized.get("description") or ""),
        source=str(runtime_target_root / Path(*relative_entry.parts)),
        workspace_path=str(runtime_target_root),
        dependency_runtime_path=dependency_runtime_path,
    )


def _mcp_config(config: dict[str, Any], runtime_directory: Path) -> dict[str, Any]:
    value = {key: child for key, child in config.items() if key in _MCP_KEYS}
    transport = str(value.get("transport") or config.get("type") or "").strip()
    if transport == "shttp":
        transport = "streamable-http"
    if transport:
        value["transport"] = transport
    if value.get("command") and not value.get("cwd"):
        value["cwd"] = str(runtime_directory)
    elif value.get("cwd") and not Path(str(value["cwd"])).is_absolute():
        value["cwd"] = str(runtime_directory / str(value["cwd"]))
    return value


def _materialize_mcp(capability: dict[str, Any], host_root: Path, runtime_root: Path) -> RuntimeMCP:
    key = _segment(capability.get("capability_key"), "mcp")
    host_directory = host_root / "mcp" / key
    runtime_directory = runtime_root / "mcp" / key
    raw = capability.get("normalized_config")
    config = _mcp_config(
        cast(dict[str, Any], raw) if isinstance(raw, dict) else {}, runtime_directory
    )
    _atomic_write(
        host_directory / "config.json",
        json.dumps(config, ensure_ascii=False, indent=2).encode("utf-8"),
    )
    return RuntimeMCP(
        name=str(capability.get("capability_key") or key),
        config=config,
        workspace_path=str(runtime_directory),
    )


def materialize_node_workspace(
    asset: dict[str, Any],
) -> tuple[tuple[RuntimeSkill, ...], tuple[RuntimeMCP, ...], str]:
    asset_id = str(asset.get("id") or "")
    if not asset_id:
        raise DomainError("RUNTIME_NODE_INVALID", "Node asset id is missing", 422)
    host_root = node_workspace_path(asset_id)
    runtime_root = openhands_node_workspace_path(asset_id)
    for directory in ("skills", "mcp", "files", "repositories", "sessions", ".runtime"):
        (host_root / directory).mkdir(parents=True, exist_ok=True)
    raw_capabilities: object = asset.get("capabilities")
    capabilities = (
        [
            cast(dict[str, Any], item)
            for item in cast(list[object], raw_capabilities)
            if isinstance(item, dict)
        ]
        if isinstance(raw_capabilities, list)
        else []
    )
    skills = tuple(
        _extract_skill(capability, host_root, runtime_root)
        for capability in capabilities
        if capability.get("capability_type") == "SKILL"
    )
    mcp_servers = tuple(
        _materialize_mcp(capability, host_root, runtime_root)
        for capability in capabilities
        if capability.get("capability_type") == "MCP"
    )
    manifest = {
        "schema_version": 1,
        "node_asset_id": asset_id,
        "node_name": asset.get("name"),
        "runtime_path": str(runtime_root),
        "skills": [
            {
                "name": skill.name,
                "entry": skill.source,
                "directory": skill.workspace_path,
                "dependency_bin": skill.dependency_runtime_path or None,
            }
            for skill in skills
        ],
        "mcp_servers": [
            {"name": server.name, "directory": server.workspace_path} for server in mcp_servers
        ],
        "user_directories": {
            "files": str(runtime_root / "files"),
            "repositories": str(runtime_root / "repositories"),
        },
    }
    _atomic_write(
        host_root / ".flowweave-manifest.json",
        json.dumps(manifest, ensure_ascii=False, indent=2).encode("utf-8"),
    )
    return skills, mcp_servers, str(runtime_root)
