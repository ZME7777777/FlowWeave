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

from flowweave.modules.sandboxes.application.runtime_allocation import (
    flow_run_capability_path,
    flow_run_workspace_nodes_path,
    flow_run_workspace_project_path,
    openhands_flow_run_capability_path,
    openhands_flow_run_nodes_path,
    openhands_flow_run_project_path,
)
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
_HOOK_METADATA_KEYS = {
    "description",
    "hook_set_schema_version",
    "openhands_version",
    "source_commit",
    "allowed_events",
    "runtime_mutation",
    "script_files",
    "script_hashes",
    "script_archive_prefix",
    "package_format",
    "storage_key",
    "capability_id",
    "capability_version_id",
    "package_id",
    "version_no",
    "digest",
    "filename",
    "content_hash",
}
_HOOK_OPENHANDS_VERSION = "1.44.0"
_HOOK_SOURCE_COMMIT = "9a24f6c8866f353042a57df0514ccc900e3a0691"
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


def cleanup_node_workspace(asset_id: str) -> None:
    """Remove one physically deleted node's managed workspace trees safely."""

    workspace_root = Path(get_settings().workspace_root).resolve()
    candidates = (node_workspace_path(asset_id), managed_node_assets_path(asset_id))
    for candidate in candidates:
        if candidate.is_symlink() or candidate.is_file():
            candidate.unlink(missing_ok=True)
            continue
        if not candidate.exists():
            continue
        resolved = candidate.resolve()
        if resolved == workspace_root or not resolved.is_relative_to(workspace_root):
            raise OSError("Node workspace escaped the managed workspace root")
        shutil.rmtree(resolved)


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


def flow_run_node_workspace_path(flow_run_id: str, asset_id: str) -> Path:
    return flow_run_workspace_nodes_path(flow_run_id) / _segment(asset_id, "node")


def openhands_flow_run_node_workspace_path(asset_id: str) -> Path:
    return Path(openhands_flow_run_nodes_path()) / _segment(asset_id, "node")


def flow_run_managed_node_assets_path(
    flow_run_id: str, manifest_digest: str, asset_id: str
) -> Path:
    return flow_run_capability_path(
        flow_run_id, manifest_digest, *node_workspace_relative(asset_id).parts
    )


def openhands_flow_run_managed_node_assets_path(manifest_digest: str, asset_id: str) -> Path:
    return Path(
        openhands_flow_run_capability_path(
            manifest_digest, *node_workspace_relative(asset_id).parts
        )
    )


def attempt_workspace_path(
    *, asset_id: str, run_id: str, node_run_id: str, attempt_no: int
) -> Path:
    return (
        flow_run_node_workspace_path(run_id, asset_id)
        / "sessions"
        / _segment(node_run_id, "node-run")
        / str(attempt_no)
    )


def ensure_flow_run_attempt_workspace(
    *, flow_run_id: str, asset_id: str, workspace_ref: str
) -> Path:
    """Materialize only the server-derived Attempt path without following links."""

    nodes_root = flow_run_workspace_nodes_path(flow_run_id)
    node_root = flow_run_node_workspace_path(flow_run_id, asset_id)
    candidate = Path(workspace_ref)
    if not candidate.is_absolute():
        raise DomainError(
            "RUNTIME_WORKSPACE_INVALID",
            "The FlowRun Attempt workspace must be an absolute server path",
            422,
        )
    try:
        relative = candidate.relative_to(node_root)
    except ValueError as exc:
        raise DomainError(
            "RUNTIME_WORKSPACE_INVALID",
            "The Attempt workspace is outside its FlowRun node directory",
            422,
        ) from exc
    if (
        len(relative.parts) != 3
        or relative.parts[0] != "sessions"
        or any(part in {"", ".", ".."} for part in relative.parts)
    ):
        raise DomainError(
            "RUNTIME_WORKSPACE_INVALID",
            "The Attempt workspace does not match its server-derived layout",
            422,
        )
    try:
        nodes_root.lstat()
    except OSError as exc:
        raise DomainError(
            "RUNTIME_WORKSPACE_INVALID",
            "The FlowRun node workspace is unavailable",
            409,
        ) from exc
    if nodes_root.is_symlink() or not nodes_root.is_dir():
        raise DomainError(
            "RUNTIME_WORKSPACE_INVALID",
            "The FlowRun node workspace is not a plain directory",
            409,
        )
    # A browser can inspect a waiting Attempt before a Runtime request is
    # built. Create the complete server-derived tree here rather than relying
    # on capability materialization to have created the node directory first.
    try:
        node_relative = node_root.relative_to(nodes_root)
    except ValueError as exc:  # Defensive: both paths are server-derived.
        raise DomainError(
            "RUNTIME_WORKSPACE_INVALID",
            "The FlowRun node workspace escaped its node directory",
            422,
        ) from exc
    cursor = nodes_root
    for part in (*node_relative.parts, *relative.parts):
        cursor = cursor / part
        if cursor.is_symlink():
            raise DomainError(
                "RUNTIME_WORKSPACE_INVALID",
                "The Attempt workspace cannot contain symbolic links",
                422,
            )
        if cursor.exists() and not cursor.is_dir():
            raise DomainError(
                "RUNTIME_WORKSPACE_INVALID",
                "The Attempt workspace path is not a directory",
                422,
            )
        try:
            cursor.mkdir(mode=0o700, exist_ok=True)
        except OSError as exc:
            raise DomainError(
                "RUNTIME_WORKSPACE_INVALID",
                "The Attempt workspace could not be materialized",
                409,
            ) from exc
    return candidate


def _ensure_plain_directory_tree(base: Path, target: Path) -> None:
    if base.is_symlink() or not base.is_dir():
        raise DomainError(
            "RUNTIME_WORKSPACE_INVALID",
            "A managed Runtime allocation root is not a plain directory",
            422,
        )
    try:
        relative = target.relative_to(base)
    except ValueError as exc:
        raise DomainError(
            "RUNTIME_WORKSPACE_INVALID",
            "A managed Runtime directory escaped its allocation",
            422,
        ) from exc
    cursor = base
    for part in relative.parts:
        if part in {"", ".", ".."}:
            raise DomainError(
                "RUNTIME_WORKSPACE_INVALID",
                "A managed Runtime directory contains an invalid path segment",
                422,
            )
        cursor = cursor / part
        if cursor.is_symlink():
            raise DomainError(
                "RUNTIME_WORKSPACE_INVALID",
                "A managed Runtime directory cannot contain symbolic links",
                422,
            )
        if cursor.exists() and not cursor.is_dir():
            raise DomainError(
                "RUNTIME_WORKSPACE_INVALID",
                "A managed Runtime path is not a directory",
                422,
            )
        cursor.mkdir(mode=0o700, exist_ok=True)


def materialize_runtime_memory(
    *,
    flow_run_id: str,
    manifest_digest: str,
    workspace_ref: str,
    materials: tuple[object, ...],
) -> None:
    """Expose governed Memory through OpenHands' native project-memory loader."""

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
    sections = [
        b"# FlowWeave governed " + scope.lower().encode("ascii") + b" memory\n" + body
        for scope in ("USER", "PROJECT")
        if (body := b"\n\n".join(grouped[scope]))
    ]
    if not sections:
        raise DomainError(
            "MEMORY_SOURCE_UNAVAILABLE",
            "Enabled Memory has no governed content",
            409,
        )
    content = b"\n\n".join(sections) + b"\n"
    bundle_digest = hashlib.sha256(content).hexdigest()
    project_root = flow_run_workspace_project_path(flow_run_id)
    working_dir = Path(workspace_ref)
    capability_root = flow_run_capability_path(flow_run_id, manifest_digest)
    source_root = flow_run_capability_path(flow_run_id, manifest_digest, "memory", bundle_digest)
    source_index = source_root / "MEMORY.md"
    runtime_index = openhands_flow_run_capability_path(
        manifest_digest, "memory", bundle_digest, "MEMORY.md"
    )
    try:
        if not working_dir.is_absolute() or not working_dir.is_relative_to(project_root):
            raise ValueError("Memory working directory escaped its FlowRun project")
        _ensure_plain_directory_tree(project_root, working_dir)
        _ensure_plain_directory_tree(capability_root, source_root)
        if source_index.is_symlink() or (source_index.exists() and not source_index.is_file()):
            raise ValueError("Memory source index is not a plain file")
        if source_index.exists():
            if source_index.read_bytes() != content:
                raise ValueError("Memory source digest directory contains different content")
        else:
            _atomic_write(source_index, content, mode=0o444)
        source_index.chmod(0o444)
        source_root.chmod(0o555)

        loader_root = working_dir / ".openhands" / "memory"
        _ensure_plain_directory_tree(working_dir, loader_root)
        loader_index = loader_root / "MEMORY.md"
        if loader_index.is_symlink():
            if os.readlink(loader_index) != str(runtime_index):
                raise ValueError("Memory loader index points at a different frozen bundle")
        elif loader_index.exists():
            raise ValueError("Memory loader index is not a managed symbolic link")
        else:
            loader_index.symlink_to(runtime_index)
        if source_index.read_bytes() != content:
            raise ValueError("Memory source read-back mismatch")
        content.decode("utf-8")
    except (OSError, UnicodeDecodeError, ValueError) as exc:
        raise DomainError(
            "MEMORY_SOURCE_UNAVAILABLE",
            "Governed Memory could not be exposed to the OpenHands loader",
            409,
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
    flow_run_runtime_root = openhands_flow_run_nodes_path()
    if runtime_node.is_relative_to(flow_run_runtime_root):
        relative = runtime_node.relative_to(flow_run_runtime_root)
        allocations_root = host_root / ".flow-run-runtimes"
        try:
            allocation_relative = attempt_root.relative_to(allocations_root)
        except ValueError as exc:
            raise DomainError(
                "RUNTIME_WORKSPACE_INVALID",
                "The Attempt workspace is outside its FlowRun allocation",
                422,
            ) from exc
        if len(allocation_relative.parts) < 7 or allocation_relative.parts[2:4] != (
            "workspace",
            "nodes",
        ):
            raise DomainError(
                "RUNTIME_WORKSPACE_INVALID",
                "The Attempt workspace does not match the FlowRun node layout",
                422,
            )
        host_project = allocations_root.joinpath(*allocation_relative.parts[:4])
    else:
        try:
            relative = runtime_node.relative_to(runtime_root)
        except ValueError as exc:
            raise DomainError(
                "RUNTIME_WORKSPACE_INVALID",
                "The node workspace is outside the Runtime workspace root",
                422,
            ) from exc
        host_project = host_root
    if not relative.parts or any(part in {"", ".", ".."} for part in relative.parts):
        raise DomainError(
            "RUNTIME_WORKSPACE_INVALID",
            "The node workspace path is not an isolated subdirectory",
            422,
        )

    host_node = host_project.joinpath(*relative.parts)
    cursor = host_project
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
        disable_model_invocation=normalized.get("disable_model_invocation") is True,
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


def materialize_hook_config(
    asset: dict[str, Any], *, flow_run_id: str = "", manifest_digest: str = ""
) -> dict[str, list[dict[str, Any]]]:
    """Materialize Hook scripts and return an OpenHands-native Hook config."""

    asset_id = str(asset.get("id") or "")
    if not asset_id:
        raise DomainError("RUNTIME_NODE_INVALID", "Node asset id is missing", 422)
    if bool(flow_run_id) != bool(manifest_digest):
        raise DomainError(
            "RUNTIME_CAPABILITY_UNAVAILABLE",
            "FlowRun capability materialization requires a manifest identity",
            409,
        )
    host_root = (
        flow_run_managed_node_assets_path(flow_run_id, manifest_digest, asset_id)
        if flow_run_id
        else managed_node_assets_path(asset_id)
    )
    runtime_root = (
        openhands_flow_run_managed_node_assets_path(manifest_digest, asset_id)
        if flow_run_id
        else openhands_managed_node_assets_path(asset_id)
    )
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
            if (
                normalized.get("hook_set_schema_version") != 1
                or normalized.get("openhands_version") != _HOOK_OPENHANDS_VERSION
                or normalized.get("source_commit") != _HOOK_SOURCE_COMMIT
                or normalized.get("runtime_mutation") != "FORBIDDEN"
                or normalized.get("allowed_events") != sorted(_HOOK_EVENTS)
                or set(normalized) - set(_HOOK_EVENTS) - _HOOK_METADATA_KEYS
            ):
                raise ValueError("Hook Set must be republished against OpenHands 1.44.0")
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
    *,
    flow_run_id: str = "",
    manifest_digest: str = "",
) -> tuple[tuple[RuntimeSkill, ...], tuple[RuntimePlugin, ...], tuple[RuntimeMCP, ...], str]:
    asset_id = str(asset.get("id") or "")
    if not asset_id:
        raise DomainError("RUNTIME_NODE_INVALID", "Node asset id is missing", 422)
    if bool(flow_run_id) != bool(manifest_digest):
        raise DomainError(
            "RUNTIME_CAPABILITY_UNAVAILABLE",
            "FlowRun capability materialization requires a manifest identity",
            409,
        )
    host_root = (
        flow_run_node_workspace_path(flow_run_id, asset_id)
        if flow_run_id
        else node_workspace_path(asset_id)
    )
    runtime_root = (
        openhands_flow_run_node_workspace_path(asset_id)
        if flow_run_id
        else openhands_node_workspace_path(asset_id)
    )
    managed_root = (
        flow_run_managed_node_assets_path(flow_run_id, manifest_digest, asset_id)
        if flow_run_id
        else managed_node_assets_path(asset_id)
    )
    managed_runtime_root = (
        openhands_flow_run_managed_node_assets_path(manifest_digest, asset_id)
        if flow_run_id
        else openhands_managed_node_assets_path(asset_id)
    )
    if flow_run_id:
        nodes_root = flow_run_workspace_nodes_path(flow_run_id)
        capability_root = flow_run_capability_path(flow_run_id, manifest_digest)
        _ensure_plain_directory_tree(nodes_root, host_root)
        _ensure_plain_directory_tree(capability_root, managed_root)
    else:
        managed_root.mkdir(parents=True, exist_ok=True)
    mcp_root = managed_root / "mcp"
    plugin_root = managed_root / "plugins"
    _replace_managed_directory(mcp_root, managed_root)
    _replace_managed_directory(plugin_root, managed_root)
    if flow_run_id:
        _replace_managed_directory(managed_root / "skills", managed_root)
        _replace_managed_directory(managed_root / ".runtime", managed_root)
    for directory in ("skills", "files", "repositories", "sessions", ".runtime"):
        if flow_run_id:
            _ensure_plain_directory_tree(host_root, host_root / directory)
        else:
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
    skill_host_root = managed_root if flow_run_id else host_root
    skill_runtime_root = managed_runtime_root if flow_run_id else runtime_root
    skills = tuple(
        _extract_skill(capability, skill_host_root, skill_runtime_root)
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


def materialize_agent_workspace_capabilities(
    capabilities: tuple[dict[str, Any], ...],
    *,
    host_root: Path,
    runtime_root: Path,
) -> tuple[tuple[RuntimeSkill, ...], tuple[RuntimePlugin, ...], tuple[RuntimeMCP, ...]]:
    """Materialize immutable Agent Workspace capability versions.

    ``host_root`` is a binding-specific child of the Runtime's already
    read-only capability mount.  Inputs have been resolved from catalog
    versions by the caller; this function only reuses the same package/hash
    validation path as FlowRun materialization.
    """

    if host_root.name in {"", ".", ".."} or host_root.parent.is_symlink():
        raise DomainError(
            "RUNTIME_CAPABILITY_UNAVAILABLE",
            "The Agent Workspace capability directory is invalid",
            409,
        )
    _replace_managed_directory(host_root, host_root.parent)
    expected = {"SKILL", "MCP", "PLUGIN"}
    if any(str(item.get("capability_type") or "") not in expected for item in capabilities):
        raise DomainError(
            "RUNTIME_CAPABILITY_UNAVAILABLE",
            "An Agent Workspace capability type is unsupported",
            409,
        )
    skills = tuple(
        _extract_skill(item, host_root, runtime_root)
        for item in capabilities
        if item["capability_type"] == "SKILL"
    )
    plugins = tuple(
        _extract_plugin(item, host_root, runtime_root)
        for item in capabilities
        if item["capability_type"] == "PLUGIN"
    )
    mcp_servers = tuple(
        _materialize_mcp(item, host_root, runtime_root)
        for item in capabilities
        if item["capability_type"] == "MCP"
    )
    manifest = {
        "schema_version": 1,
        "runtime_path": str(runtime_root),
        "capabilities": [
            {
                "type": item["capability_type"],
                "key": item["capability_key"],
                "version_id": item["capability_version_id"],
                "digest": item["digest"],
            }
            for item in capabilities
        ],
    }
    _atomic_write(
        host_root / ".flowweave-manifest.json",
        json.dumps(manifest, ensure_ascii=False, indent=2).encode("utf-8"),
    )
    # The sandbox bind-mounts the allocation's ``capabilities`` root read-only.
    # Do not chmod the control-plane files here: the host must retain enough
    # write permission to atomically replace a conversation manifest and to
    # reclaim it after deletion.  Docker's read-only mount is the Runtime
    # isolation boundary, while package digests and the frozen manifest protect
    # identity at materialization time.
    return skills, plugins, mcp_servers


def agent_workspace_capability_marketplace_name(binding_id: str) -> str:
    """Return the stable native Marketplace registration for one Conversation."""

    return _segment(f"flowweave-{binding_id}", "flowweave-conversation").lower()


def _agent_workspace_capability_plugin_name(capability: dict[str, Any]) -> str:
    """Build an immutable marketplace entry name from governed provenance."""

    capability_type = _segment(capability.get("capability_type"), "capability").lower()
    capability_key = _segment(capability.get("capability_key"), "capability").lower()
    version_id = _segment(capability.get("capability_version_id"), "version").lower()
    return _segment(
        f"flowweave-{capability_type}-{capability_key}-{version_id}", "flowweave-capability"
    ).lower()


def _replace_agentskills_frontmatter(
    skill_path: Path, *, capability_key: str, description: str, disable_model_invocation: bool
) -> None:
    """Make a wrapped AgentSkills package natively triggerable with ``$name``.

    The catalog package may have been authored without OpenHands trigger
    metadata.  The wrapper keeps its complete body/resources but supplies the
    governed invocation trigger required by the session capability contract.
    Existing frontmatter is replaced rather than duplicated so OpenHands'
    normal AgentSkills parser sees one unambiguous document.
    """

    content = skill_path.read_text(encoding="utf-8")
    body = content
    if content.startswith("---\n"):
        closing = content.find("\n---", 4)
        if closing >= 0:
            remainder = content.find("\n", closing + 4)
            body = content[remainder + 1 :] if remainder >= 0 else ""
    normalized_description = " ".join(description.split()) or capability_key
    frontmatter = [
        "---",
        f"name: {json.dumps(capability_key, ensure_ascii=False)}",
        f"description: {json.dumps(normalized_description, ensure_ascii=False)}",
        "triggers:",
        f"  - {json.dumps(f'${capability_key}', ensure_ascii=False)}",
    ]
    if disable_model_invocation:
        frontmatter.append("disable-model-invocation: true")
    frontmatter.append("---")
    _atomic_write(skill_path, ("\n".join(frontmatter) + "\n" + body).encode("utf-8"), mode=0o444)


def _load_marketplace_plugins(marketplace_root: Path) -> list[dict[str, Any]]:
    manifest_path = marketplace_root / ".plugin" / "marketplace.json"
    if not manifest_path.exists():
        return []
    try:
        decoded = cast(object, json.loads(manifest_path.read_text(encoding="utf-8")))
    except (OSError, ValueError) as exc:
        raise DomainError(
            "RUNTIME_CAPABILITY_UNAVAILABLE",
            "The Agent Conversation Marketplace manifest is invalid",
            409,
        ) from exc
    raw = cast(dict[object, object], decoded) if isinstance(decoded, dict) else None
    plugins = raw.get("plugins") if raw is not None else None
    if not isinstance(plugins, list):
        raise DomainError(
            "RUNTIME_CAPABILITY_UNAVAILABLE",
            "The Agent Conversation Marketplace manifest is invalid",
            409,
        )
    parsed: list[dict[str, Any]] = []
    for item in cast(list[object], plugins):
        if not isinstance(item, dict):
            raise DomainError(
                "RUNTIME_CAPABILITY_UNAVAILABLE",
                "The Agent Conversation Marketplace manifest is invalid",
                409,
            )
        parsed.append({str(key): value for key, value in cast(dict[object, object], item).items()})
    return parsed


def materialize_agent_workspace_capability_marketplace(
    capability: dict[str, Any] | None,
    *,
    host_root: Path,
    runtime_root: Path,
    marketplace_name: str,
) -> str | None:
    """Create/update the read-only native Marketplace for one Conversation.

    FlowWeave owns the immutable catalog version and its digest verification.
    OpenHands owns Plugin/Skill/MCP parsing and runtime activation: every
    dynamic capability becomes a normal Marketplace Plugin entry.
    """

    if host_root.name in {"", ".", ".."} or host_root.parent.is_symlink():
        raise DomainError(
            "RUNTIME_CAPABILITY_UNAVAILABLE",
            "The Agent Workspace capability directory is invalid",
            409,
        )
    marketplace_root = host_root / "marketplace"
    if marketplace_root.exists() and (
        marketplace_root.is_symlink() or not marketplace_root.is_dir()
    ):
        raise DomainError(
            "RUNTIME_CAPABILITY_UNAVAILABLE",
            "The Agent Conversation Marketplace directory is invalid",
            409,
        )
    marketplace_root.mkdir(parents=True, exist_ok=True)
    plugins = _load_marketplace_plugins(marketplace_root)
    plugin_ref: str | None = None
    if capability is not None:
        capability_type = str(capability.get("capability_type") or "")
        if capability_type not in {"SKILL", "MCP", "PLUGIN"}:
            raise DomainError(
                "RUNTIME_CAPABILITY_UNAVAILABLE",
                "An Agent Workspace capability type is unsupported",
                409,
            )
        plugin_ref = _agent_workspace_capability_plugin_name(capability)
        if not any(item.get("name") == plugin_ref for item in plugins):
            plugin_root = marketplace_root / "plugins" / plugin_ref
            runtime_plugin_root = runtime_root / "marketplace" / "plugins" / plugin_ref
            if capability_type == "SKILL":
                materialized = _extract_skill(capability, plugin_root, runtime_plugin_root)
                skill_path = (
                    plugin_root
                    / "skills"
                    / _segment(capability.get("capability_key"), "skill")
                    / "SKILL.md"
                )
                # _extract_skill preserves the package entry filename. Rename a
                # non-standard entry only inside our wrapper so Plugin.load()
                # uses the standard AgentSkills discovery path.
                if not skill_path.exists():
                    candidates = tuple((plugin_root / "skills").rglob("*.md"))
                    if len(candidates) != 1:
                        raise DomainError(
                            "RUNTIME_CAPABILITY_UNAVAILABLE",
                            "A selected Skill package has an invalid entry",
                            422,
                        )
                    skill_path.parent.mkdir(parents=True, exist_ok=True)
                    candidates[0].replace(skill_path)
                _replace_agentskills_frontmatter(
                    skill_path,
                    capability_key=str(capability.get("capability_key") or "skill"),
                    description=materialized.description,
                    disable_model_invocation=materialized.disable_model_invocation,
                )
                _atomic_write(
                    plugin_root / ".plugin" / "plugin.json",
                    json.dumps(
                        {
                            "name": plugin_ref,
                            "version": str(capability.get("capability_version_id") or "1"),
                            "description": materialized.description,
                        },
                        ensure_ascii=False,
                    ).encode("utf-8"),
                    mode=0o444,
                )
            elif capability_type == "MCP":
                server = _materialize_mcp(capability, plugin_root, runtime_plugin_root)
                _atomic_write(
                    plugin_root / ".plugin" / "plugin.json",
                    json.dumps(
                        {
                            "name": plugin_ref,
                            "version": str(capability.get("capability_version_id") or "1"),
                            "description": str(capability.get("capability_key") or "MCP"),
                        },
                        ensure_ascii=False,
                    ).encode("utf-8"),
                    mode=0o444,
                )
                _atomic_write(
                    plugin_root / ".mcp.json",
                    json.dumps(
                        {"mcpServers": {server.name: server.config}}, ensure_ascii=False
                    ).encode("utf-8"),
                    mode=0o444,
                )
            else:
                plugin = _extract_plugin(capability, marketplace_root, runtime_root / "marketplace")
                plugins.append(
                    {
                        "name": plugin_ref,
                        "source": f"./plugins/{Path(plugin.source).name}",
                    }
                )
                plugin = None
            if capability_type != "PLUGIN":
                plugins.append({"name": plugin_ref, "source": f"./plugins/{plugin_ref}"})
    manifest = {
        "name": marketplace_name,
        "owner": {"name": "FlowWeave"},
        "description": "FlowWeave governed conversation capabilities",
        "plugins": plugins,
    }
    _atomic_write(
        marketplace_root / ".plugin" / "marketplace.json",
        json.dumps(manifest, ensure_ascii=False, indent=2).encode("utf-8"),
        mode=0o444,
    )
    return plugin_ref
