from __future__ import annotations

import mimetypes
import os
import stat
from hashlib import sha256
from pathlib import Path, PurePosixPath
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from flowweave.modules.agent_workspaces.application import service, work_directories
from flowweave.modules.agent_workspaces.application.conversations import (
    terminal_container_details,
    terminal_resource_details,
)
from flowweave.modules.agent_workspaces.infrastructure.models import (
    AgentConversationBinding,
    AgentWorkspace,
)
from flowweave.runtime.base import RuntimeHandle, RuntimeWorkspaceFile
from flowweave.shared.errors import DomainError, not_found
from flowweave.shared.settings import get_settings

_PROJECT_ROOT = "/runtime/workspace/project"
_MAX_INDEX_ENTRIES = 20_000
_MAX_FILE_BYTES = 25 * 1024 * 1024


def terminal_session_name(
    workspace_id: str, container_id: str, terminal_instance_id: str
) -> str:
    """Derive a short opaque tmux identity from server-owned Runtime facts."""

    digest = sha256(f"{workspace_id}:{terminal_instance_id}".encode()).hexdigest()[:24]
    container_short_id = container_id.removeprefix("sha256:")[:12].lower()
    if not container_short_id or any(
        character not in "0123456789abcdef" for character in container_short_id
    ):
        container_short_id = sha256(container_id.encode()).hexdigest()[:12]
    return f"fw-agent-{container_short_id}-{digest}"


def _workspace(db: Session, workspace_id: str) -> AgentWorkspace:
    item = db.get(AgentWorkspace, workspace_id)
    if item is None:
        raise not_found("agent_workspace", workspace_id)
    return item


def _runtime_handle(db: Session, workspace_id: str) -> RuntimeHandle:
    resource_name, resource_id = terminal_resource_details(db, workspace_id)
    return RuntimeHandle(
        job_id=f"agent-workspace:{workspace_id}",
        conversation_id="",
        runtime_resource_id=resource_id,
        runtime_resource_name=resource_name,
    )


def _project_root(db: Session, workspace_id: str) -> Path:
    """Resolve the platform-owned persistent project root without using Runtime."""

    allocation = service.runtime_allocation_for_agent_workspace(db, workspace_id)
    configured_root = Path(get_settings().workspace_root).absolute()
    try:
        storage_metadata = configured_root.lstat()
        storage_root = configured_root.resolve(strict=True)
    except OSError as exc:
        raise DomainError(
            "AGENT_WORKSPACE_STORAGE_UNAVAILABLE",
            "工作区持久化目录不可用",
            503,
        ) from exc
    if stat.S_ISLNK(storage_metadata.st_mode) or not stat.S_ISDIR(storage_metadata.st_mode):
        raise DomainError(
            "AGENT_WORKSPACE_STORAGE_INVALID",
            "工作区持久化根目录无效",
            409,
        )
    project_root = storage_root.joinpath(
        *PurePosixPath(allocation.relative_root).parts, "workspace", "project"
    )
    try:
        metadata = project_root.lstat()
        resolved = project_root.resolve(strict=True)
    except OSError as exc:
        raise DomainError(
            "AGENT_WORKSPACE_STORAGE_UNAVAILABLE",
            "工作区持久化目录不可用",
            503,
        ) from exc
    if (
        stat.S_ISLNK(metadata.st_mode)
        or not stat.S_ISDIR(metadata.st_mode)
        or not resolved.is_relative_to(storage_root)
    ):
        raise DomainError(
            "AGENT_WORKSPACE_STORAGE_INVALID",
            "工作区持久化目录无效",
            409,
        )
    return resolved


def _host_path(project_root: Path, runtime_path: str, *, require_file: bool) -> Path:
    parsed = PurePosixPath(runtime_path)
    if (
        not runtime_path.startswith(_PROJECT_ROOT + "/")
        or parsed.as_posix() != runtime_path
        or ".." in parsed.parts
        or any(part.startswith(".") for part in parsed.parts)
    ):
        raise DomainError(
            "AGENT_WORKSPACE_PATH_INVALID", "文件路径不在工作区范围内", 422
        )
    relative = parsed.relative_to(PurePosixPath(_PROJECT_ROOT))
    candidate = project_root.joinpath(*relative.parts)
    try:
        metadata = candidate.lstat()
        resolved = candidate.resolve(strict=True)
    except (FileNotFoundError, OSError) as exc:
        raise DomainError(
            "AGENT_WORKSPACE_FILE_NOT_FOUND", "文件不存在或不可读取", 404
        ) from exc
    if (
        stat.S_ISLNK(metadata.st_mode)
        or not resolved.is_relative_to(project_root)
        or (require_file and not stat.S_ISREG(metadata.st_mode))
    ):
        raise DomainError(
            "AGENT_WORKSPACE_PATH_INVALID", "文件路径不在工作区范围内", 422
        )
    return resolved


def _workspace_entries(project_root: Path, working_directory: str) -> list[dict[str, Any]]:
    host_root = (
        project_root
        if working_directory == _PROJECT_ROOT
        else _host_path(project_root, working_directory, require_file=False)
    )
    if not host_root.is_dir():
        raise DomainError(
            "AGENT_WORKSPACE_PATH_INVALID", "当前工作目录不存在", 409
        )
    entries: list[dict[str, Any]] = []
    for current, directory_names, file_names in os.walk(host_root, followlinks=False):
        current_path = Path(current)
        directory_names[:] = sorted(
            name
            for name in directory_names
            if not name.startswith(".") and not (current_path / name).is_symlink()
        )
        for name, kind in (
            *((name, "directory") for name in directory_names),
            *((name, "file") for name in sorted(file_names) if not name.startswith(".")),
        ):
            host_path = current_path / name
            try:
                metadata = host_path.lstat()
            except OSError:
                continue
            if stat.S_ISLNK(metadata.st_mode):
                continue
            relative = host_path.relative_to(project_root).as_posix()
            entries.append(
                {
                    "path": f"{_PROJECT_ROOT}/{relative}",
                    "kind": kind,
                    "size": metadata.st_size if kind == "file" else 0,
                }
            )
            if len(entries) >= _MAX_INDEX_ENTRIES:
                return entries
    return entries


def _working_directory(
    db: Session,
    workspace_id: str,
    work_directory_id: str | None,
    binding_id: str | None,
) -> tuple[str, dict[str, Any] | None]:
    if binding_id and work_directory_id:
        raise DomainError(
            "AGENT_WORKSPACE_SCOPE_AMBIGUOUS",
            "不能同时指定会话与工作目录",
            422,
        )
    if binding_id:
        binding = db.scalar(
            select(AgentConversationBinding).where(
                AgentConversationBinding.id == binding_id,
                AgentConversationBinding.workspace_id == workspace_id,
                AgentConversationBinding.lifecycle == "ACTIVE",
            )
        )
        if binding is None:
            raise DomainError("AGENT_CONVERSATION_NOT_FOUND", "会话不存在或已删除", 404)
        return binding.working_directory or _PROJECT_ROOT, None
    if not work_directory_id:
        return _PROJECT_ROOT, None
    directory = work_directories.get_work_directory(db, workspace_id, work_directory_id)
    if directory["state"] != "ACTIVE":
        raise DomainError("AGENT_WORK_DIRECTORY_ARCHIVED", "工作目录已归档", 409)
    # A browser draft contains only an ID. Revalidate the active directory and
    # its real project-tree path before exposing it to file or terminal APIs.
    _, working_directory = work_directories.conversation_context(
        db, workspace_id, work_directory_id
    )
    return working_directory, directory


def terminal_details(
    db: Session,
    workspace_id: str,
    *,
    work_directory_id: str | None = None,
    binding_id: str | None = None,
) -> tuple[str, str, str, str]:
    """Resolve the only directory a workspace terminal may start in.

    The browser never supplies a filesystem path.  A bound conversation keeps its
    frozen working directory, while an unsent draft can only use an active work
    directory selected by its server-side identifier.
    """
    _workspace(db, workspace_id)
    working_directory, _ = _working_directory(db, workspace_id, work_directory_id, binding_id)
    resource_name, resource_id, container_id = terminal_container_details(db, workspace_id)
    return resource_name, resource_id, working_directory, container_id


def details(
    db: Session,
    workspace_id: str,
    *,
    work_directory_id: str | None = None,
    binding_id: str | None = None,
) -> dict[str, Any]:
    _workspace(db, workspace_id)
    working_directory, directory = _working_directory(
        db, workspace_id, work_directory_id, binding_id
    )
    project_root = _project_root(db, workspace_id)
    repositories = []
    host_working_directory = (
        project_root
        if working_directory == _PROJECT_ROOT
        else _host_path(project_root, working_directory, require_file=False)
    )
    if (host_working_directory / ".git").is_dir():
        repositories.append({"path": working_directory})
    try:
        _, _, container_id = terminal_container_details(db, workspace_id)
        container_short_id = container_id.removeprefix("sha256:")[:12]
    except DomainError:
        container_short_id = None
    return {
        "root": _PROJECT_ROOT,
        "working_directory": working_directory,
        "work_directory": directory,
        "files": _workspace_entries(project_root, working_directory),
        "repositories": repositories,
        "runtime": {"container_id": container_short_id},
        "ide": {
            "workspace_path": working_directory,
            "gateway": {
                "supported": False,
                "status": "需要部署 Gateway",
                "note": (
                    "当前平台未提供可验证的 IDEA/Gateway 地址或凭据；部署方配置受保护"
                    "入口后，可使用此工作目录连接。"
                ),
            },
        },
    }


def download(
    db: Session,
    workspace_id: str,
    path: str,
    binding_id: str | None = None,
    work_directory_id: str | None = None,
) -> Any:
    _workspace(db, workspace_id)
    parsed = PurePosixPath(path)
    if (
        not path.startswith(_PROJECT_ROOT + "/")
        or parsed.as_posix() != path
        or ".." in parsed.parts
        or any(part in {".git", ".openhands"} for part in parsed.parts)
    ):
        raise DomainError("AGENT_WORKSPACE_PATH_INVALID", "文件路径不在工作区范围内", 422)
    working_directory, _ = _working_directory(db, workspace_id, work_directory_id, binding_id)
    if not (path == working_directory or path.startswith(working_directory.rstrip("/") + "/")):
        raise DomainError("AGENT_WORKSPACE_PATH_INVALID", "文件路径不在当前工作目录范围内", 422)
    host_path = _host_path(_project_root(db, workspace_id), path, require_file=True)
    try:
        size = host_path.stat().st_size
    except OSError as exc:
        raise DomainError(
            "AGENT_WORKSPACE_FILE_UNAVAILABLE", "文件暂时无法读取", 503
        ) from exc
    if size > _MAX_FILE_BYTES:
        raise DomainError(
            "AGENT_WORKSPACE_FILE_TOO_LARGE",
            "文件超过浏览器读取上限，请在终端或 IDE 中查看",
            413,
            {"max_bytes": _MAX_FILE_BYTES},
        )
    try:
        content = host_path.read_bytes()
    except OSError as exc:
        raise DomainError(
            "AGENT_WORKSPACE_FILE_UNAVAILABLE", "文件暂时无法读取", 503
        ) from exc
    return RuntimeWorkspaceFile(
        filename=host_path.name,
        content_type=mimetypes.guess_type(host_path.name)[0] or "application/octet-stream",
        content=content,
    )
