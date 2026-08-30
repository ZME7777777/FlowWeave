"""FlowRun-owned workspace projection for sessions entered from a node.

The node Attempt authorizes the browser entry only. Files and logical work
directories belong to the FlowRun and remain shared by every node entry.
"""

from __future__ import annotations

import mimetypes
import os
import stat
from pathlib import Path, PurePosixPath
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from flowweave.modules.agent_sessions.application import flow_node_conversations
from flowweave.modules.agent_sessions.application.flow_node_host import (
    resolve_flow_node_session_host,
)
from flowweave.modules.agent_workspaces import public as agent_workspace_host
from flowweave.modules.sandboxes import public as sandboxes
from flowweave.shared.errors import DomainError

_RUNTIME_PROJECT = PurePosixPath("/runtime/workspace/project")
_MAX_INDEX_ENTRIES = 20_000
_MAX_FILE_BYTES = 25 * 1024 * 1024

AgentWorkDirectory = agent_workspace_host.AgentWorkDirectory
AgentWorkDirectoryPath = agent_workspace_host.AgentWorkDirectoryPath
AgentWorkDirectoryVersion = agent_workspace_host.AgentWorkDirectoryVersion


def _authorize_entry(db: Session, *, flow_run_id: str, attempt_id: str) -> Path:
    resolve_flow_node_session_host(
        db,
        flow_run_id=flow_run_id,
        attempt_id=attempt_id,
        require_start_permission=False,
    )
    root = sandboxes.flow_run_workspace_project_path(flow_run_id)
    try:
        metadata = root.lstat()
        resolved = root.resolve(strict=True)
    except OSError as exc:
        raise DomainError(
            "FLOW_RUN_WORKSPACE_UNAVAILABLE", "FlowRun 工作区当前不可用", 503
        ) from exc
    if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISDIR(metadata.st_mode):
        raise DomainError("FLOW_RUN_WORKSPACE_INVALID", "FlowRun 工作区不是普通目录", 409)
    return resolved


def _version_paths(db: Session, version_id: str) -> tuple[str, ...]:
    paths = tuple(
        db.scalars(
            select(AgentWorkDirectoryPath.relative_path)
            .where(AgentWorkDirectoryPath.version_id == version_id)
            .order_by(AgentWorkDirectoryPath.position)
        )
    )
    if not paths:
        raise DomainError("AGENT_WORK_DIRECTORY_VERSION_MISSING", "工作目录版本数据不完整", 409)
    return paths


def _scope(
    db: Session,
    *,
    flow_run_id: str,
    attempt_id: str,
    binding_id: str | None,
    work_directory_id: str | None,
) -> tuple[str, dict[str, Any] | None, tuple[str, ...]]:
    host = resolve_flow_node_session_host(
        db,
        flow_run_id=flow_run_id,
        attempt_id=attempt_id,
        require_start_permission=False,
    )
    attempt_root = host.session.working_directory
    if binding_id and work_directory_id:
        raise DomainError("FLOW_RUN_WORKSPACE_SCOPE_AMBIGUOUS", "不能同时指定会话与工作区", 422)
    if binding_id:
        binding = flow_node_conversations.node_conversation_binding(
            db,
            flow_run_id=flow_run_id,
            attempt_id=attempt_id,
            binding_id=binding_id,
        )
        if binding.work_directory_version_id is None:
            working_directory = binding.working_directory or attempt_root
            return working_directory, None, (working_directory,)
        version = db.get(AgentWorkDirectoryVersion, binding.work_directory_version_id)
        directory = (
            db.get(AgentWorkDirectory, version.work_directory_id) if version is not None else None
        )
        if version is None or directory is None or directory.flow_run_id != flow_run_id:
            raise DomainError("AGENT_WORK_DIRECTORY_VERSION_MISSING", "工作目录版本数据不完整", 409)
        paths = _version_paths(db, version.id)
        details = {
            "id": directory.id,
            "display_name": directory.display_name,
            "state": directory.state,
            "current_version": {
                "id": version.id,
                "version": version.version,
                "selected_paths": list(paths),
                "working_directory": binding.working_directory or str(_RUNTIME_PROJECT),
            },
        }
        roots = tuple(str(_RUNTIME_PROJECT / path) for path in paths)
        return binding.working_directory or str(_RUNTIME_PROJECT), details, roots
    if work_directory_id:
        raise DomainError("NODE_WORK_DIRECTORY_FIXED", "节点会话固定使用当前 Attempt 工作目录", 409)
    return attempt_root, None, (attempt_root,)


def _runtime_path(project_root: Path, candidate: Path) -> str:
    relative = candidate.relative_to(project_root)
    return str(_RUNTIME_PROJECT.joinpath(*relative.parts))


def _validate_scope_roots(project_root: Path, roots: tuple[str, ...]) -> None:
    """Revalidate frozen directory paths against the mutable project tree.

    A directory was safe when its immutable version was created, but a later
    filesystem mutation must not turn it into a symlink to another scope.
    """

    for runtime_root in roots:
        parsed = PurePosixPath(runtime_root)
        if (
            not parsed.is_absolute()
            or not parsed.is_relative_to(_RUNTIME_PROJECT)
            or parsed.as_posix() != runtime_root
            or any(part in {"", ".", ".."} for part in parsed.parts)
        ):
            raise DomainError(
                "FLOW_RUN_WORKSPACE_PATH_INVALID",
                "工作区目录不在 FlowRun 项目范围内",
                409,
            )
        current = project_root
        for part in parsed.relative_to(_RUNTIME_PROJECT).parts:
            current = current / part
            try:
                metadata = current.lstat()
            except OSError as exc:
                raise DomainError(
                    "FLOW_RUN_WORKSPACE_UNAVAILABLE",
                    "工作区目录当前不可用",
                    409,
                ) from exc
            if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISDIR(metadata.st_mode):
                raise DomainError(
                    "FLOW_RUN_WORKSPACE_PATH_INVALID",
                    "工作区目录不能经过符号链接且必须是普通目录",
                    409,
                )


def _entries(project_root: Path, roots: tuple[str, ...]) -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = []
    for runtime_root in roots:
        relative = PurePosixPath(runtime_root).relative_to(_RUNTIME_PROJECT)
        host_root = project_root.joinpath(*relative.parts)
        if host_root != project_root:
            items.append({"path": runtime_root, "kind": "directory", "size": 0})
        for current, directories, files in os.walk(host_root, followlinks=False):
            current_path = Path(current)
            directories[:] = sorted(
                name
                for name in directories
                if not name.startswith(".") and not (current_path / name).is_symlink()
            )
            for name, kind in (
                *((name, "directory") for name in directories),
                *((name, "file") for name in sorted(files) if not name.startswith(".")),
            ):
                candidate = current_path / name
                try:
                    metadata = candidate.lstat()
                except OSError:
                    continue
                if stat.S_ISLNK(metadata.st_mode):
                    continue
                items.append(
                    {
                        "path": _runtime_path(project_root, candidate),
                        "kind": kind,
                        "size": metadata.st_size if kind == "file" else 0,
                    }
                )
                if len(items) >= _MAX_INDEX_ENTRIES:
                    return items
    return items


def _host_file(project_root: Path, path: str, roots: tuple[str, ...]) -> Path:
    parsed = PurePosixPath(path)
    if (
        not parsed.is_absolute()
        or not parsed.is_relative_to(_RUNTIME_PROJECT)
        or parsed.as_posix() != path
        or any(part in {"", ".", ".."} or part.startswith(".") for part in parsed.parts)
        or not any(path == root or path.startswith(root.rstrip("/") + "/") for root in roots)
    ):
        raise DomainError("FLOW_RUN_WORKSPACE_PATH_INVALID", "文件路径不在当前工作区范围内", 422)
    relative = parsed.relative_to(_RUNTIME_PROJECT)
    candidate = project_root.joinpath(*relative.parts)
    try:
        metadata = candidate.lstat()
        resolved = candidate.resolve(strict=True)
    except OSError as exc:
        raise DomainError("FLOW_RUN_WORKSPACE_FILE_NOT_FOUND", "文件不存在或不可读取", 404) from exc
    authorized_roots: list[Path] = []
    try:
        for root in roots:
            root_relative = PurePosixPath(root).relative_to(_RUNTIME_PROJECT)
            authorized_roots.append(
                project_root.joinpath(*root_relative.parts).resolve(strict=True)
            )
    except (OSError, ValueError) as exc:
        raise DomainError(
            "FLOW_RUN_WORKSPACE_PATH_INVALID",
            "工作区目录当前不可用",
            409,
        ) from exc
    if (
        stat.S_ISLNK(metadata.st_mode)
        or not stat.S_ISREG(metadata.st_mode)
        or not resolved.is_relative_to(project_root)
        or not any(resolved.is_relative_to(root) for root in authorized_roots)
    ):
        raise DomainError("FLOW_RUN_WORKSPACE_PATH_INVALID", "文件路径不在当前工作区范围内", 422)
    return resolved


def details(
    db: Session,
    *,
    flow_run_id: str,
    attempt_id: str,
    binding_id: str | None = None,
    work_directory_id: str | None = None,
) -> dict[str, Any]:
    project_root = _authorize_entry(db, flow_run_id=flow_run_id, attempt_id=attempt_id)
    working_directory, directory, roots = _scope(
        db,
        flow_run_id=flow_run_id,
        attempt_id=attempt_id,
        binding_id=binding_id,
        work_directory_id=work_directory_id,
    )
    _validate_scope_roots(project_root, roots)
    scope = (
        {"kind": "ROOT", "display_name": "根工作区"}
        if directory is None
        else {
            "kind": "WORK_DIRECTORY",
            "id": directory["id"],
            "display_name": directory["display_name"],
        }
    )
    return {
        "root": str(_RUNTIME_PROJECT),
        "scope": scope,
        "working_directory": working_directory,
        "work_directory": directory,
        "files": _entries(project_root, roots),
        "repositories": [],
        "runtime": {"container_id": None},
        "ide": {
            "workspace_path": working_directory,
            "gateway": {
                "supported": False,
                "status": "需要部署 Gateway",
                "note": "当前平台未配置可验证的 IDEA/Gateway 入口。",
            },
        },
    }


def read_file(
    db: Session,
    *,
    flow_run_id: str,
    attempt_id: str,
    binding_id: str | None,
    work_directory_id: str | None,
    path: str,
) -> tuple[bytes, str, str]:
    project_root = _authorize_entry(db, flow_run_id=flow_run_id, attempt_id=attempt_id)
    _, _, roots = _scope(
        db,
        flow_run_id=flow_run_id,
        attempt_id=attempt_id,
        binding_id=binding_id,
        work_directory_id=work_directory_id,
    )
    _validate_scope_roots(project_root, roots)
    candidate = _host_file(project_root, path, roots)
    try:
        content = candidate.read_bytes()
    except OSError as exc:
        raise DomainError("FLOW_RUN_WORKSPACE_FILE_NOT_FOUND", "文件不存在或不可读取", 404) from exc
    if len(content) > _MAX_FILE_BYTES:
        raise DomainError("FLOW_RUN_WORKSPACE_FILE_TOO_LARGE", "文件超过预览大小限制", 422)
    return (
        content,
        mimetypes.guess_type(candidate.name)[0] or "application/octet-stream",
        candidate.name,
    )


def conversation_working_directory(
    db: Session, *, flow_run_id: str, attempt_id: str, binding_id: str
) -> str:
    project_root = _authorize_entry(db, flow_run_id=flow_run_id, attempt_id=attempt_id)
    working_directory, _, roots = _scope(
        db,
        flow_run_id=flow_run_id,
        attempt_id=attempt_id,
        binding_id=binding_id,
        work_directory_id=None,
    )
    _validate_scope_roots(project_root, roots)
    return working_directory


__all__ = ("conversation_working_directory", "details", "read_file")
