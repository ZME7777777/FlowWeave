from __future__ import annotations

import hashlib
import io
import json
import os
import re
import shlex
import shutil
import zipfile
from pathlib import Path, PurePosixPath
from typing import Any, cast
from uuid import uuid4

from flowweave.runtime.base import RuntimeMCP, RuntimePlugin, RuntimeSkill
from flowweave.shared.artifact_store import get_artifact_store
from flowweave.shared.errors import DomainError
from flowweave.shared.settings import get_settings

_SAFE_SEGMENT = re.compile(r"[^A-Za-z0-9._-]+")
_DEPENDENCY_MAX_FILES = 20_000
_DEPENDENCY_MAX_EXPANDED_BYTES = 100 * 1024 * 1024
_MCP_SCRIPT_MAX_FILES = 20
_MCP_SCRIPT_MAX_EXPANDED_BYTES = 10 * 1024 * 1024
_HOOK_SCRIPT_MAX_FILES = 20
_HOOK_SCRIPT_MAX_EXPANDED_BYTES = 10 * 1024 * 1024
_PLUGIN_MAX_FILES = 1000
_PLUGIN_MAX_EXPANDED_BYTES = 100 * 1024 * 1024
_HOOK_EVENTS = (
    "pre_tool_use",
    "post_tool_use",
    "user_prompt_submit",
    "session_start",
    "session_end",
    "stop",
)
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


def managed_node_assets_path(asset_id: str) -> Path:
    return (
        Path(get_settings().workspace_root).resolve()
        / ".managed-assets"
        / node_workspace_relative(asset_id)
    )


def openhands_managed_node_assets_path(asset_id: str) -> Path:
    return Path(get_settings().openhands_managed_assets_root) / node_workspace_relative(asset_id)


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


def materialize_runtime_memory(
    *,
    owner_type: str,
    owner_id: str,
    materials: tuple[object, ...],
) -> None:
    """Write governed Memory into an owner-isolated, read-only mount source."""

    if owner_type not in {"ATTEMPT", "CONVERSATION"} or not owner_id:
        raise DomainError(
            "MEMORY_SOURCE_UNAVAILABLE",
            "Enabled Memory requires an isolated managed Runtime owner",
            409,
        )
    grouped: dict[str, list[bytes]] = {"USER": [], "PROJECT": []}
    for material in materials:
        scope = str(getattr(material, "scope", ""))
        content = getattr(material, "content", None)
        digest = str(getattr(material, "digest", ""))
        if scope not in grouped or not isinstance(content, bytes):
            raise DomainError(
                "MEMORY_SOURCE_UNAVAILABLE",
                "Governed Memory material is invalid",
                409,
            )
        if hashlib.sha256(content).hexdigest() != digest:
            raise DomainError(
                "MEMORY_SOURCE_DIGEST_MISMATCH",
                "Governed Memory material changed before it was written",
                409,
            )
        grouped[scope].append(content.rstrip(b"\n"))
    owner_root = (
        Path(get_settings().workspace_root).resolve()
        / ".managed-memory"
        / owner_type.lower()
        / _segment(owner_id, "runtime")
    )
    root = owner_root / "runtime"
    # A previous retry leaves this exact owner directory read-only. Restore
    # only the two fixed tier directories before the normal symlink-safe
    # replacement logic removes it.
    for directory in (root / "user", root / "project", root):
        if directory.is_dir() and not directory.is_symlink():
            directory.chmod(0o700)
    _replace_managed_directory(root, owner_root)
    try:
        for scope, chunks in grouped.items():
            directory = root / scope.lower()
            directory.mkdir(mode=0o700)
            content = b"\n\n".join(chunks) + (b"\n" if chunks else b"")
            target = directory / "MEMORY.md"
            # Both fixed files must exist before Docker creates nested,
            # read-only mounts. An unselected tier is represented by an empty
            # file; OpenHands ignores it while retaining one deterministic
            # mount contract for every enabled Runtime.
            _atomic_write(target, content, mode=0o444)
            if target.read_bytes() != content:
                raise OSError("Memory read-back mismatch")
            target.read_text(encoding="utf-8")
            directory.chmod(0o555)
        root.chmod(0o555)
    except (OSError, UnicodeDecodeError) as exc:
        cleanup_runtime_memory(owner_type=owner_type, owner_id=owner_id)
        raise DomainError(
            "MEMORY_SOURCE_UNAVAILABLE",
            "Governed Memory could not be materialized and verified",
            409,
        ) from exc


def cleanup_runtime_memory(*, owner_type: str, owner_id: str) -> None:
    """Remove one owner-isolated Memory source without following links."""

    if owner_type not in {"ATTEMPT", "CONVERSATION"} or not owner_id:
        return
    workspace_root = Path(get_settings().workspace_root).resolve()
    owner_root = (
        workspace_root / ".managed-memory" / owner_type.lower() / _segment(owner_id, "runtime")
    )
    try:
        if owner_root.is_symlink() or owner_root.is_file():
            owner_root.unlink(missing_ok=True)
            return
        if not owner_root.exists():
            return
        resolved = owner_root.resolve()
        expected_parent = (workspace_root / ".managed-memory" / owner_type.lower()).resolve()
        if resolved.parent != expected_parent:
            raise OSError("Memory owner directory escaped its managed parent")
        root = owner_root / "runtime"
        for directory in (root / "user", root / "project", root):
            if directory.is_dir() and not directory.is_symlink():
                directory.chmod(0o700)
        owner_root.chmod(0o700)
        shutil.rmtree(owner_root)
    except OSError as exc:
        raise DomainError(
            "MEMORY_SOURCE_CLEANUP_FAILED",
            "Governed Memory cleanup failed after Runtime deletion",
            503,
        ) from exc


def isolated_runtime_workspace_paths(
    workspace_ref: str, node_workspace_ref: str
) -> tuple[str, str]:
    """Return proven node and working-directory Runtime-relative paths."""

    settings = get_settings()
    host_root = settings.workspace_root.resolve()
    attempt_root = Path(workspace_ref).resolve()
    if attempt_root == host_root or not attempt_root.is_relative_to(host_root):
        raise DomainError(
            "RUNTIME_WORKSPACE_INVALID",
            "The Attempt workspace is outside the managed workspace root",
            422,
        )

    runtime_root = PurePosixPath(str(settings.openhands_workspace_root))
    runtime_node = PurePosixPath(node_workspace_ref)
    if not runtime_root.is_absolute() or not runtime_node.is_absolute():
        raise DomainError(
            "RUNTIME_WORKSPACE_INVALID",
            "The Runtime workspace roots must be absolute",
            422,
        )
    try:
        relative = runtime_node.relative_to(runtime_root)
    except ValueError as exc:
        raise DomainError(
            "RUNTIME_WORKSPACE_INVALID",
            "The node workspace is outside the Runtime workspace root",
            422,
        ) from exc
    if not relative.parts or any(part in {"", ".", ".."} for part in relative.parts):
        raise DomainError(
            "RUNTIME_WORKSPACE_INVALID",
            "The node workspace path is not an isolated subdirectory",
            422,
        )

    host_node = host_root.joinpath(*relative.parts)
    cursor = host_root
    for part in relative.parts:
        cursor = cursor / part
        if cursor.is_symlink():
            raise DomainError(
                "RUNTIME_WORKSPACE_INVALID",
                "The node workspace path cannot contain symbolic links",
                422,
            )
    resolved_node = host_node.resolve()
    if (
        not resolved_node.is_relative_to(host_root)
        or not resolved_node.is_dir()
        or not attempt_root.is_relative_to(resolved_node)
    ):
        raise DomainError(
            "RUNTIME_WORKSPACE_INVALID",
            "The Attempt workspace does not belong to the selected node workspace",
            422,
        )
    working_relative = attempt_root.relative_to(resolved_node)
    if not working_relative.parts or any(
        part in {"", ".", ".."} for part in working_relative.parts
    ):
        raise DomainError(
            "RUNTIME_WORKSPACE_INVALID",
            "The Attempt working directory is not an isolated node subdirectory",
            422,
        )
    cursor = resolved_node
    for part in working_relative.parts:
        cursor = cursor / part
        if cursor.is_symlink():
            raise DomainError(
                "RUNTIME_WORKSPACE_INVALID",
                "The Attempt working directory cannot contain symbolic links",
                422,
            )
    return relative.as_posix(), working_relative.as_posix()


def isolated_runtime_workspace_relative(workspace_ref: str, node_workspace_ref: str) -> str:
    """Compatibility wrapper returning the proven node-workspace path."""

    return isolated_runtime_workspace_paths(workspace_ref, node_workspace_ref)[0]


def _atomic_write(path: Path, content: bytes, mode: int = 0o644) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{uuid4().hex}.tmp")
    temporary.write_bytes(content)
    temporary.chmod(mode)
    os.replace(temporary, path)


def _replace_managed_directory(path: Path, managed_root: Path) -> None:
    """Create a clean directory without following pre-existing links."""

    workspace_root = Path(get_settings().workspace_root).resolve()
    resolved_managed_root = managed_root.resolve()
    if not resolved_managed_root.is_relative_to(workspace_root):
        raise DomainError(
            "RUNTIME_CAPABILITY_UNAVAILABLE",
            "The managed capability directory is outside the workspace root",
            422,
        )
    managed_root.mkdir(parents=True, exist_ok=True)
    if path.parent != managed_root or path.name in {"", ".", ".."}:
        raise DomainError(
            "RUNTIME_CAPABILITY_UNAVAILABLE",
            "The managed capability directory is invalid",
            422,
        )
    if path.is_symlink() or path.is_file():
        path.unlink()
    elif path.exists():
        shutil.rmtree(path)
    path.mkdir(mode=0o700)


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
        content = get_artifact_store().read(storage_key)
        expected_hash = str(normalized.get("content_hash") or "")
        if not expected_hash or hashlib.sha256(content).hexdigest() != expected_hash:
            raise ValueError("capability package digest mismatch")
        return content, normalized, entry
    except (FileNotFoundError, ValueError) as exc:
        raise DomainError(
            "RUNTIME_CAPABILITY_UNAVAILABLE",
            "A selected Skill package cannot be loaded",
            422,
            {"capability_key": capability.get("capability_key")},
        ) from exc


def _extract_plugin(
    capability: dict[str, Any], host_root: Path, runtime_root: Path
) -> RuntimePlugin:
    """Materialize an immutable, locally published OpenHands Plugin directory."""

    archive_bytes, normalized, prefix_value = _package(capability)
    expected_hashes_raw = normalized.get("file_hashes")
    if not isinstance(expected_hashes_raw, dict) or not expected_hashes_raw:
        raise DomainError(
            "RUNTIME_CAPABILITY_UNAVAILABLE",
            "A selected Plugin does not have a frozen file manifest",
            422,
            {"capability_key": capability.get("capability_key")},
        )
    expected_hashes = {
        str(path): str(digest)
        for path, digest in cast(dict[object, object], expected_hashes_raw).items()
    }
    version_id = str(normalized.get("capability_version_id") or "")
    content_hash = str(normalized.get("content_hash") or "")
    key = _segment(f"{capability.get('capability_key')}-{version_id}", "plugin")
    target_root = host_root / "plugins" / key
    runtime_target_root = runtime_root / "plugins" / key
    prefix = PurePosixPath(prefix_value) if prefix_value else PurePosixPath(".")
    extracted: set[str] = set()
    total = 0
    try:
        with zipfile.ZipFile(io.BytesIO(archive_bytes)) as archive:
            files = [item for item in archive.infolist() if not item.is_dir()]
            if len(files) > _PLUGIN_MAX_FILES:
                raise ValueError("plugin package contains too many files")
            for item in files:
                source = PurePosixPath(item.filename.replace("\\", "/"))
                if (
                    "__MACOSX" in source.parts
                    or source.name == ".DS_Store"
                    or source.name.startswith("._")
                ):
                    continue
                try:
                    relative = (
                        source.relative_to(prefix) if prefix != PurePosixPath(".") else source
                    )
                except ValueError as exc:
                    raise ValueError("plugin file is outside the frozen root") from exc
                relative_name = relative.as_posix()
                if (
                    source.is_absolute()
                    or ".." in source.parts
                    or not relative.parts
                    or relative_name not in expected_hashes
                ):
                    raise ValueError("unsafe or unexpected plugin path")
                mode_type = (item.external_attr >> 16) & 0o170000
                if mode_type not in {0, 0o100000}:
                    raise ValueError("unsupported plugin file type")
                content = archive.read(item)
                total += len(content)
                if total > _PLUGIN_MAX_EXPANDED_BYTES:
                    raise ValueError("plugin package is too large")
                if hashlib.sha256(content).hexdigest() != expected_hashes[relative_name]:
                    raise ValueError("plugin file digest mismatch")
                destination = (target_root / Path(*relative.parts)).resolve()
                if not destination.is_relative_to(target_root.resolve()):
                    raise ValueError("unsafe plugin destination")
                executable = bool((item.external_attr >> 16) & 0o111)
                _atomic_write(destination, content, mode=0o555 if executable else 0o444)
                extracted.add(relative_name)
        if extracted != set(expected_hashes):
            raise ValueError("plugin package does not match its frozen manifest")
    except (KeyError, OSError, ValueError, zipfile.BadZipFile) as exc:
        raise DomainError(
            "RUNTIME_CAPABILITY_UNAVAILABLE",
            "A selected Plugin package cannot be materialized",
            422,
            {"capability_key": capability.get("capability_key")},
        ) from exc
    return RuntimePlugin(
        name=str(capability.get("capability_key") or key),
        source=str(runtime_target_root),
        content_hash=content_hash,
    )


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
        activation_keywords=(f"${str(capability.get('capability_key') or key)}",),
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


def _extract_mcp_scripts(
    capability: dict[str, Any],
    host_directory: Path,
) -> None:
    normalized = cast(dict[str, Any], capability.get("normalized_config") or {})
    raw_files = normalized.get("script_files")
    if not isinstance(raw_files, list) or not raw_files:
        return
    filenames = [str(value) for value in cast(list[object], raw_files)]
    hashes = normalized.get("script_hashes")
    expected_hashes = (
        {str(key): str(value) for key, value in cast(dict[object, object], hashes).items()}
        if isinstance(hashes, dict)
        else {}
    )
    if (
        len(filenames) > _MCP_SCRIPT_MAX_FILES
        or len(filenames) != len(set(filenames))
        or set(filenames) != set(expected_hashes)
    ):
        raise DomainError(
            "RUNTIME_CAPABILITY_UNAVAILABLE",
            "An MCP script package has an invalid manifest",
            422,
            {"capability_key": capability.get("capability_key")},
        )
    storage_key = str(normalized.get("storage_key") or "")
    prefix = PurePosixPath(str(normalized.get("script_archive_prefix") or ""))
    if not storage_key or not prefix.parts:
        raise DomainError(
            "RUNTIME_CAPABILITY_UNAVAILABLE",
            "An MCP script package is missing",
            422,
            {"capability_key": capability.get("capability_key")},
        )
    expected = set(filenames)
    extracted: set[str] = set()
    total = 0
    try:
        bundle = get_artifact_store().read(storage_key)
        with zipfile.ZipFile(io.BytesIO(bundle)) as archive:
            for item in archive.infolist():
                if item.is_dir():
                    continue
                source = PurePosixPath(item.filename.replace("\\", "/"))
                try:
                    relative = source.relative_to(prefix)
                except ValueError:
                    continue
                if len(relative.parts) != 1 or relative.name not in expected:
                    raise ValueError("unexpected MCP script path")
                mode_type = (item.external_attr >> 16) & 0o170000
                if source.is_absolute() or ".." in source.parts or mode_type not in {0, 0o100000}:
                    raise ValueError("unsafe MCP script path")
                content = archive.read(item)
                total += len(content)
                if total > _MCP_SCRIPT_MAX_EXPANDED_BYTES:
                    raise ValueError("MCP script package is too large")
                if hashlib.sha256(content).hexdigest() != expected_hashes[relative.name]:
                    raise ValueError("MCP script digest mismatch")
                destination = (host_directory / "scripts" / relative.name).resolve()
                scripts_root = (host_directory / "scripts").resolve()
                if not destination.is_relative_to(scripts_root):
                    raise ValueError("unsafe MCP script destination")
                suffix = Path(relative.name).suffix.lower()
                mode = 0o555 if suffix == ".sh" else 0o444
                _atomic_write(destination, content, mode=mode)
                extracted.add(relative.name)
        if extracted != expected:
            raise ValueError("MCP script package is incomplete")
    except (FileNotFoundError, KeyError, OSError, ValueError, zipfile.BadZipFile) as exc:
        raise DomainError(
            "RUNTIME_CAPABILITY_UNAVAILABLE",
            "An MCP script package cannot be materialized",
            422,
            {"capability_key": capability.get("capability_key")},
        ) from exc


def _materialize_mcp(capability: dict[str, Any], host_root: Path, runtime_root: Path) -> RuntimeMCP:
    key = _segment(capability.get("capability_key"), "mcp")
    host_directory = host_root / "mcp" / key
    runtime_directory = runtime_root / "mcp" / key
    raw = capability.get("normalized_config")
    _extract_mcp_scripts(capability, host_directory)
    config = _mcp_config(
        cast(dict[str, Any], raw) if isinstance(raw, dict) else {}, runtime_directory
    )
    _atomic_write(
        host_directory / "config.json",
        json.dumps(config, ensure_ascii=False, indent=2).encode("utf-8"),
        mode=0o444,
    )
    return RuntimeMCP(
        name=str(capability.get("capability_key") or key),
        config=config,
        workspace_path=str(runtime_directory),
    )


def materialize_mcp_probe(capability: dict[str, Any], validation_id: str) -> tuple[RuntimeMCP, str]:
    """Materialize one immutable MCP package for a target-environment probe."""

    probe_key = _segment(f"mcp-probe-{validation_id}", "mcp-probe")
    host_root = managed_node_assets_path(probe_key)
    runtime_root = openhands_managed_node_assets_path(probe_key)
    workspace = node_workspace_path(probe_key)
    workspace.mkdir(parents=True, exist_ok=True)
    _replace_managed_directory(host_root / "mcp", host_root)
    return _materialize_mcp(capability, host_root, runtime_root), node_workspace_relative(
        probe_key
    ).as_posix()


def materialize_plugin_probe(
    capability: dict[str, Any], validation_id: str
) -> tuple[RuntimePlugin, str]:
    """Materialize one immutable Plugin for a target-environment loader probe."""

    probe_key = _segment(f"plugin-probe-{validation_id}", "plugin-probe")
    host_root = managed_node_assets_path(probe_key)
    runtime_root = openhands_managed_node_assets_path(probe_key)
    workspace = node_workspace_path(probe_key)
    workspace.mkdir(parents=True, exist_ok=True)
    _replace_managed_directory(host_root / "plugins", host_root)
    return _extract_plugin(capability, host_root, runtime_root), node_workspace_relative(
        probe_key
    ).as_posix()


def cleanup_mcp_probe(validation_id: str) -> None:
    """Remove host-side probe material after Runtime cleanup intent is durable."""

    probe_key = _segment(f"mcp-probe-{validation_id}", "mcp-probe")
    for path in (managed_node_assets_path(probe_key), node_workspace_path(probe_key)):
        root = Path(get_settings().workspace_root).resolve()
        resolved = path.resolve()
        if resolved != root and resolved.is_relative_to(root):
            shutil.rmtree(resolved, ignore_errors=True)


def cleanup_plugin_probe(validation_id: str) -> None:
    """Remove host-side Plugin probe material after cleanup is durable."""

    probe_key = _segment(f"plugin-probe-{validation_id}", "plugin-probe")
    for path in (managed_node_assets_path(probe_key), node_workspace_path(probe_key)):
        root = Path(get_settings().workspace_root).resolve()
        resolved = path.resolve()
        if resolved != root and resolved.is_relative_to(root):
            shutil.rmtree(resolved, ignore_errors=True)


def _hook_script_command(runtime_path: Path) -> str:
    quoted = shlex.quote(str(runtime_path))
    suffix = runtime_path.suffix.lower()
    if suffix == ".sh":
        return f"sh {quoted}"
    if suffix == ".py":
        return f"python {quoted}"
    if suffix in {".js", ".mjs", ".cjs"}:
        return f"node {quoted}"
    raise ValueError("unsupported Hook script extension")


def _extract_hook_scripts(
    capability: dict[str, Any], host_directory: Path, runtime_directory: Path
) -> dict[str, str]:
    normalized = cast(dict[str, Any], capability.get("normalized_config") or {})
    raw_files = normalized.get("script_files")
    if not isinstance(raw_files, list) or not raw_files:
        return {}
    filenames = [str(value) for value in cast(list[object], raw_files)]
    hashes = normalized.get("script_hashes")
    expected_hashes = (
        {str(key): str(value) for key, value in cast(dict[object, object], hashes).items()}
        if isinstance(hashes, dict)
        else {}
    )
    if len(filenames) > _HOOK_SCRIPT_MAX_FILES or set(filenames) != set(expected_hashes):
        raise ValueError("invalid Hook script manifest")
    storage_key = str(normalized.get("storage_key") or "")
    prefix = PurePosixPath(str(normalized.get("script_archive_prefix") or ""))
    if not storage_key or not prefix.parts:
        raise ValueError("missing Hook script package")

    scripts_directory = host_directory / "scripts"
    if scripts_directory.is_symlink() or scripts_directory.is_file():
        scripts_directory.unlink()
    elif scripts_directory.exists():
        shutil.rmtree(scripts_directory)
    expected = set(filenames)
    extracted: set[str] = set()
    total = 0
    try:
        bundle = get_artifact_store().read(storage_key)
        with zipfile.ZipFile(io.BytesIO(bundle)) as archive:
            for item in archive.infolist():
                if item.is_dir():
                    continue
                source = PurePosixPath(item.filename.replace("\\", "/"))
                try:
                    relative = source.relative_to(prefix)
                except ValueError:
                    continue
                mode_type = (item.external_attr >> 16) & 0o170000
                if (
                    len(relative.parts) != 1
                    or relative.name not in expected
                    or source.is_absolute()
                    or ".." in source.parts
                    or mode_type not in {0, 0o100000}
                ):
                    raise ValueError("unsafe Hook script path")
                content = archive.read(item)
                total += len(content)
                if total > _HOOK_SCRIPT_MAX_EXPANDED_BYTES:
                    raise ValueError("Hook script package is too large")
                if hashlib.sha256(content).hexdigest() != expected_hashes[relative.name]:
                    raise ValueError("Hook script digest mismatch")
                destination = (scripts_directory / relative.name).resolve()
                if not destination.is_relative_to(scripts_directory.resolve()):
                    raise ValueError("unsafe Hook script destination")
                _atomic_write(destination, content, mode=0o555)
                extracted.add(relative.name)
        if extracted != expected:
            raise ValueError("Hook script package is incomplete")
    except (FileNotFoundError, KeyError, OSError, ValueError, zipfile.BadZipFile) as exc:
        raise DomainError(
            "RUNTIME_CAPABILITY_UNAVAILABLE",
            "A Hook script package cannot be materialized",
            422,
            {"capability_key": capability.get("capability_key")},
        ) from exc
    return {
        filename: _hook_script_command(runtime_directory / "scripts" / filename)
        for filename in filenames
    }


def materialize_hook_config(asset: dict[str, Any]) -> dict[str, list[dict[str, Any]]]:
    """Materialize Hook scripts and return an OpenHands-native Hook config."""

    asset_id = str(asset.get("id") or "")
    if not asset_id:
        raise DomainError("RUNTIME_NODE_INVALID", "Node asset id is missing", 422)
    host_root = managed_node_assets_path(asset_id)
    runtime_root = openhands_managed_node_assets_path(asset_id)
    hooks_root = host_root / "hooks"
    _replace_managed_directory(hooks_root, host_root)
    merged: dict[str, list[dict[str, Any]]] = {}
    raw_capabilities = asset.get("capabilities")
    if not isinstance(raw_capabilities, list):
        return merged
    for raw_capability in cast(list[object], raw_capabilities):
        if not isinstance(raw_capability, dict):
            continue
        capability = cast(dict[str, Any], raw_capability)
        if capability.get("capability_type") != "HOOK":
            continue
        key = _segment(capability.get("capability_key"), "hook")
        host_directory = host_root / "hooks" / key
        runtime_directory = runtime_root / "hooks" / key
        try:
            commands = _extract_hook_scripts(capability, host_directory, runtime_directory)
            normalized = cast(dict[str, Any], capability.get("normalized_config") or {})
            for event in _HOOK_EVENTS:
                raw_matchers = normalized.get(event)
                if not isinstance(raw_matchers, list):
                    continue
                for raw_matcher in cast(list[object], raw_matchers):
                    if not isinstance(raw_matcher, dict):
                        continue
                    matcher = cast(dict[str, Any], raw_matcher)
                    converted_hooks: list[dict[str, Any]] = []
                    for raw_hook in cast(list[object], matcher.get("hooks") or []):
                        if not isinstance(raw_hook, dict):
                            continue
                        hook = dict(cast(dict[str, Any], raw_hook))
                        if hook.get("type") == "script":
                            filename = str(hook.pop("script", ""))
                            command = commands.get(filename)
                            if command is None:
                                raise ValueError("Hook script action is not materialized")
                            hook["type"] = "command"
                            hook["command"] = command
                        converted_hooks.append(hook)
                    merged.setdefault(event, []).append(
                        {
                            "matcher": str(matcher.get("matcher") or "*"),
                            "hooks": converted_hooks,
                        }
                    )
        except ValueError as exc:
            raise DomainError(
                "RUNTIME_CAPABILITY_UNAVAILABLE",
                "A Hook configuration cannot be materialized",
                422,
                {"capability_key": capability.get("capability_key")},
            ) from exc
    return merged


def materialize_node_workspace(
    asset: dict[str, Any],
) -> tuple[tuple[RuntimeSkill, ...], tuple[RuntimePlugin, ...], tuple[RuntimeMCP, ...], str]:
    asset_id = str(asset.get("id") or "")
    if not asset_id:
        raise DomainError("RUNTIME_NODE_INVALID", "Node asset id is missing", 422)
    host_root = node_workspace_path(asset_id)
    runtime_root = openhands_node_workspace_path(asset_id)
    managed_root = managed_node_assets_path(asset_id)
    managed_runtime_root = openhands_managed_node_assets_path(asset_id)
    managed_root.mkdir(parents=True, exist_ok=True)
    mcp_root = managed_root / "mcp"
    plugin_root = managed_root / "plugins"
    _replace_managed_directory(mcp_root, managed_root)
    _replace_managed_directory(plugin_root, managed_root)
    for directory in ("skills", "files", "repositories", "sessions", ".runtime"):
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
    plugins = tuple(
        _extract_plugin(capability, managed_root, managed_runtime_root)
        for capability in capabilities
        if capability.get("capability_type") == "PLUGIN"
    )
    mcp_servers = tuple(
        _materialize_mcp(capability, managed_root, managed_runtime_root)
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
        "plugins": [
            {"name": plugin.name, "source": plugin.source, "content_hash": plugin.content_hash}
            for plugin in plugins
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
    return skills, plugins, mcp_servers, str(runtime_root)
