from __future__ import annotations

import stat
from pathlib import Path, PurePosixPath
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from flowweave.modules.agent_workspaces.application.service import (
    runtime_allocation_for_agent_workspace,
)
from flowweave.modules.agent_workspaces.infrastructure.models import (
    AgentWorkDirectory,
    AgentWorkDirectoryPath,
    AgentWorkDirectoryVersion,
    AgentWorkspace,
)
from flowweave.modules.sandboxes import public as sandboxes
from flowweave.shared.database import now
from flowweave.shared.errors import DomainError, not_found
from flowweave.shared.models import FlowRun
from flowweave.shared.settings import get_settings

_RUNTIME_PROJECT_ROOT = PurePosixPath("/runtime/workspace/project")
_MAX_RELATIVE_PATH_LENGTH = 500


def _workspace(db: Session, workspace_id: str) -> AgentWorkspace:
    workspace = db.get(AgentWorkspace, workspace_id)
    if workspace is None:
        raise not_found("agent_workspace", workspace_id)
    return workspace


def _directory(
    db: Session, workspace_id: str, work_directory_id: str, *, lock: bool = False
) -> AgentWorkDirectory:
    query = select(AgentWorkDirectory).where(
        AgentWorkDirectory.id == work_directory_id,
        AgentWorkDirectory.workspace_id == workspace_id,
    )
    if lock:
        query = query.with_for_update()
    item = db.scalar(query)
    if item is None:
        raise DomainError("AGENT_WORK_DIRECTORY_NOT_FOUND", "工作目录不存在", 404)
    return item


def _flow_run_directory(
    db: Session, flow_run_id: str, work_directory_id: str, *, lock: bool = False
) -> AgentWorkDirectory:
    query = select(AgentWorkDirectory).where(
        AgentWorkDirectory.id == work_directory_id,
        AgentWorkDirectory.flow_run_id == flow_run_id,
    )
    if lock:
        query = query.with_for_update()
    item = db.scalar(query)
    if item is None:
        raise DomainError("AGENT_WORK_DIRECTORY_NOT_FOUND", "工作目录不存在", 404)
    return item


def _project_root(db: Session, workspace_id: str) -> Path:
    allocation = runtime_allocation_for_agent_workspace(db, workspace_id)
    root = (
        Path(get_settings().workspace_root).absolute()
        / allocation.relative_root
        / "workspace"
        / "project"
    )
    try:
        metadata = root.lstat()
    except OSError as exc:
        raise DomainError(
            "AGENT_WORK_DIRECTORY_ROOT_UNAVAILABLE",
            "Agent 项目根目录当前不可用",
            503,
        ) from exc
    if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISDIR(metadata.st_mode):
        raise DomainError(
            "AGENT_WORK_DIRECTORY_ROOT_INVALID",
            "Agent 项目根目录不是普通目录",
            409,
        )
    return root


def _canonical_relative_path(value: str) -> PurePosixPath:
    if (
        value != value.strip()
        or not value
        or len(value) > _MAX_RELATIVE_PATH_LENGTH
        or "\\" in value
        or "\x00" in value
    ):
        raise DomainError(
            "AGENT_WORK_DIRECTORY_PATH_INVALID",
            "工作目录路径必须是项目根下的规范相对路径",
            422,
            {"path": value},
        )
    relative = PurePosixPath(value)
    if (
        relative.is_absolute()
        or relative.as_posix() != value
        or any(part in {"", ".", ".."} for part in relative.parts)
    ):
        raise DomainError(
            "AGENT_WORK_DIRECTORY_PATH_INVALID",
            "工作目录路径必须是项目根下的规范相对路径",
            422,
            {"path": value},
        )
    return relative


def _validated_relative_paths(values: tuple[str, ...], project_root: Path) -> tuple[str, ...]:
    if not values:
        raise DomainError("AGENT_WORK_DIRECTORY_PATH_REQUIRED", "至少选择一个项目根子目录", 422)
    if len(values) > 20:
        raise DomainError(
            "AGENT_WORK_DIRECTORY_PATH_LIMIT_EXCEEDED",
            "一个工作目录最多选择 20 个子目录",
            422,
        )
    paths = tuple(_canonical_relative_path(value) for value in values)
    if len(set(paths)) != len(paths):
        raise DomainError(
            "AGENT_WORK_DIRECTORY_PATH_DUPLICATE", "工作目录不能重复选择同一路径", 422
        )
    ordered = sorted(paths, key=lambda item: (len(item.parts), item.parts))
    for index, parent in enumerate(ordered):
        for child in ordered[index + 1 :]:
            if child.parts[: len(parent.parts)] == parent.parts:
                raise DomainError(
                    "AGENT_WORK_DIRECTORY_PATH_OVERLAP",
                    "工作目录不能同时选择父目录及其子目录",
                    422,
                    {"parent": parent.as_posix(), "child": child.as_posix()},
                )
    for relative in paths:
        current = project_root
        for part in relative.parts:
            current = current / part
            try:
                metadata = current.lstat()
            except OSError as exc:
                raise DomainError(
                    "AGENT_WORK_DIRECTORY_PATH_NOT_FOUND",
                    "所选工作目录不存在",
                    422,
                    {"path": relative.as_posix()},
                ) from exc
            if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISDIR(metadata.st_mode):
                raise DomainError(
                    "AGENT_WORK_DIRECTORY_PATH_NOT_DIRECTORY",
                    "所选路径必须是普通目录且不能经过符号链接",
                    422,
                    {"path": relative.as_posix()},
                )
    return tuple(path.as_posix() for path in paths)


def _validate_paths(db: Session, workspace_id: str, values: tuple[str, ...]) -> tuple[str, ...]:
    return _validated_relative_paths(values, _project_root(db, workspace_id))


def _flow_run_root(db: Session, flow_run_id: str) -> Path:
    if db.get(FlowRun, flow_run_id) is None:
        raise not_found("flow_run", flow_run_id)
    return sandboxes.flow_run_workspace_project_path(flow_run_id)


def _validate_flow_run_paths(
    db: Session, flow_run_id: str, values: tuple[str, ...]
) -> tuple[str, ...]:
    return _validated_relative_paths(values, _flow_run_root(db, flow_run_id))


def _display_name(value: str) -> str:
    name = value.strip()
    if not name:
        raise DomainError("AGENT_WORK_DIRECTORY_NAME_REQUIRED", "工作目录名称不能为空", 422)
    if len(name) > 160:
        raise DomainError(
            "AGENT_WORK_DIRECTORY_NAME_TOO_LONG", "工作目录名称不能超过 160 个字符", 422
        )
    return name


def _assert_name_available(
    db: Session, workspace_id: str, display_name: str, *, exclude_id: str | None = None
) -> None:
    query = select(AgentWorkDirectory.id).where(
        AgentWorkDirectory.workspace_id == workspace_id,
        AgentWorkDirectory.display_name == display_name,
    )
    if exclude_id is not None:
        query = query.where(AgentWorkDirectory.id != exclude_id)
    if db.scalar(query) is not None:
        raise DomainError(
            "AGENT_WORK_DIRECTORY_NAME_CONFLICT", "当前工作空间已存在同名工作目录", 409
        )


def _assert_flow_run_name_available(
    db: Session, flow_run_id: str, display_name: str, *, exclude_id: str | None = None
) -> None:
    query = select(AgentWorkDirectory.id).where(
        AgentWorkDirectory.flow_run_id == flow_run_id,
        AgentWorkDirectory.display_name == display_name,
    )
    if exclude_id is not None:
        query = query.where(AgentWorkDirectory.id != exclude_id)
    if db.scalar(query) is not None:
        raise DomainError(
            "AGENT_WORK_DIRECTORY_NAME_CONFLICT", "当前 FlowRun 已存在同名工作目录", 409
        )


def _version(
    db: Session, item: AgentWorkDirectory, version_number: int | None = None
) -> AgentWorkDirectoryVersion:
    number = version_number or item.current_version
    version = db.scalar(
        select(AgentWorkDirectoryVersion).where(
            AgentWorkDirectoryVersion.work_directory_id == item.id,
            AgentWorkDirectoryVersion.version == number,
        )
    )
    if version is None:
        raise DomainError(
            "AGENT_WORK_DIRECTORY_VERSION_MISSING",
            "工作目录版本数据不完整",
            409,
        )
    return version


def _path_values(db: Session, version_id: str) -> tuple[str, ...]:
    return tuple(
        db.scalars(
            select(AgentWorkDirectoryPath.relative_path)
            .where(AgentWorkDirectoryPath.version_id == version_id)
            .order_by(AgentWorkDirectoryPath.position)
        )
    )


def _working_directory(working_path: str) -> str:
    if working_path == ".":
        return _RUNTIME_PROJECT_ROOT.as_posix()
    return (_RUNTIME_PROJECT_ROOT / working_path).as_posix()


def _dict(db: Session, item: AgentWorkDirectory) -> dict[str, Any]:
    version = _version(db, item)
    return {
        "id": item.id,
        "display_name": item.display_name,
        "state": item.state,
        "current_version": {
            "id": version.id,
            "version": version.version,
            "selected_paths": list(_path_values(db, version.id)),
            "working_directory": _working_directory(version.working_path),
        },
        "created_at": item.created_at.isoformat(),
        "updated_at": item.updated_at.isoformat(),
        "archived_at": item.archived_at.isoformat() if item.archived_at else None,
    }


def root_context() -> dict[str, Any]:
    return {
        "kind": "ROOT",
        "display_name": "根工作区",
        "working_directory": _RUNTIME_PROJECT_ROOT.as_posix(),
    }


def conversation_context(
    db: Session, workspace_id: str, work_directory_id: str | None
) -> tuple[str | None, str]:
    """Resolve and revalidate the context frozen by a lazy conversation bootstrap."""

    _workspace(db, workspace_id)
    if work_directory_id is None:
        return None, _RUNTIME_PROJECT_ROOT.as_posix()
    item = _directory(db, workspace_id, work_directory_id, lock=True)
    if item.state != "ACTIVE":
        raise DomainError("AGENT_WORK_DIRECTORY_ARCHIVED", "工作目录已归档", 409)
    version = _version(db, item)
    # A version was checked when saved, but the underlying project tree is
    # mutable. Re-check it before handing the path to OpenHands.
    _validate_paths(db, workspace_id, _path_values(db, version.id))
    return version.id, _working_directory(version.working_path)


def frozen_conversation_context(
    db: Session, workspace_id: str, work_directory_version_id: str
) -> str:
    """Revalidate a previously frozen version without consulting mutable current state."""

    _workspace(db, workspace_id)
    version = db.scalar(
        select(AgentWorkDirectoryVersion)
        .join(
            AgentWorkDirectory, AgentWorkDirectory.id == AgentWorkDirectoryVersion.work_directory_id
        )
        .where(
            AgentWorkDirectoryVersion.id == work_directory_version_id,
            AgentWorkDirectory.workspace_id == workspace_id,
        )
    )
    if version is None:
        raise DomainError("AGENT_WORK_DIRECTORY_VERSION_MISSING", "工作目录版本数据不完整", 409)
    _validate_paths(db, workspace_id, _path_values(db, version.id))
    return _working_directory(version.working_path)


def list_work_directories(db: Session, workspace_id: str) -> dict[str, Any]:
    _workspace(db, workspace_id)
    items = db.scalars(
        select(AgentWorkDirectory)
        .where(
            AgentWorkDirectory.workspace_id == workspace_id,
            AgentWorkDirectory.state == "ACTIVE",
        )
        .order_by(AgentWorkDirectory.updated_at.desc(), AgentWorkDirectory.created_at.desc())
    )
    return {"root": root_context(), "items": [_dict(db, item) for item in items]}


def get_work_directory(db: Session, workspace_id: str, work_directory_id: str) -> dict[str, Any]:
    _workspace(db, workspace_id)
    return _dict(db, _directory(db, workspace_id, work_directory_id))


def _add_version(
    db: Session, item: AgentWorkDirectory, version_number: int, paths: tuple[str, ...]
) -> None:
    version = AgentWorkDirectoryVersion(
        work_directory_id=item.id,
        version=version_number,
        working_path=paths[0] if len(paths) == 1 else ".",
    )
    db.add(version)
    db.flush()
    for position, relative_path in enumerate(paths):
        db.add(
            AgentWorkDirectoryPath(
                version_id=version.id,
                relative_path=relative_path,
                position=position,
            )
        )
    db.flush()


def create_work_directory(
    db: Session, workspace_id: str, display_name: str, selected_paths: tuple[str, ...]
) -> dict[str, Any]:
    _workspace(db, workspace_id)
    name = _display_name(display_name)
    _assert_name_available(db, workspace_id, name)
    paths = _validate_paths(db, workspace_id, selected_paths)
    item = AgentWorkDirectory(workspace_id=workspace_id, display_name=name)
    db.add(item)
    db.flush()
    _add_version(db, item, 1, paths)
    return _dict(db, item)


def update_work_directory(
    db: Session,
    workspace_id: str,
    work_directory_id: str,
    *,
    display_name: str | None,
    selected_paths: tuple[str, ...] | None,
) -> dict[str, Any]:
    _workspace(db, workspace_id)
    item = _directory(db, workspace_id, work_directory_id, lock=True)
    if item.state != "ACTIVE":
        raise DomainError("AGENT_WORK_DIRECTORY_ARCHIVED", "工作目录已归档", 409)
    changed = False
    if display_name is not None:
        name = _display_name(display_name)
        _assert_name_available(db, workspace_id, name, exclude_id=item.id)
        if name != item.display_name:
            item.display_name = name
            changed = True
    if selected_paths is not None:
        paths = _validate_paths(db, workspace_id, selected_paths)
        current = _version(db, item)
        if paths != _path_values(db, current.id):
            item.current_version += 1
            _add_version(db, item, item.current_version, paths)
            changed = True
    if changed:
        item.row_version += 1
        item.updated_at = now()
        db.flush()
    return _dict(db, item)


def archive_work_directory(db: Session, workspace_id: str, work_directory_id: str) -> None:
    _workspace(db, workspace_id)
    item = _directory(db, workspace_id, work_directory_id, lock=True)
    if item.state == "ARCHIVED":
        return
    archived_at = now()
    item.state = "ARCHIVED"
    item.archived_at = archived_at
    item.updated_at = archived_at
    item.row_version += 1
    db.flush()


def list_flow_run_work_directories(db: Session, flow_run_id: str) -> dict[str, Any]:
    _flow_run_root(db, flow_run_id)
    items = db.scalars(
        select(AgentWorkDirectory)
        .where(
            AgentWorkDirectory.flow_run_id == flow_run_id,
            AgentWorkDirectory.state == "ACTIVE",
        )
        .order_by(AgentWorkDirectory.updated_at.desc(), AgentWorkDirectory.created_at.desc())
    )
    return {"root": root_context(), "items": [_dict(db, item) for item in items]}


def get_flow_run_work_directory(
    db: Session, flow_run_id: str, work_directory_id: str
) -> dict[str, Any]:
    _flow_run_root(db, flow_run_id)
    return _dict(db, _flow_run_directory(db, flow_run_id, work_directory_id))


def create_flow_run_work_directory(
    db: Session, flow_run_id: str, display_name: str, selected_paths: tuple[str, ...]
) -> dict[str, Any]:
    _flow_run_root(db, flow_run_id)
    name = _display_name(display_name)
    _assert_flow_run_name_available(db, flow_run_id, name)
    paths = _validate_flow_run_paths(db, flow_run_id, selected_paths)
    item = AgentWorkDirectory(flow_run_id=flow_run_id, display_name=name)
    db.add(item)
    db.flush()
    _add_version(db, item, 1, paths)
    return _dict(db, item)


def flow_run_conversation_context(
    db: Session, flow_run_id: str, work_directory_id: str | None
) -> tuple[str | None, str]:
    _flow_run_root(db, flow_run_id)
    if work_directory_id is None:
        return None, _RUNTIME_PROJECT_ROOT.as_posix()
    item = _flow_run_directory(db, flow_run_id, work_directory_id, lock=True)
    if item.state != "ACTIVE":
        raise DomainError("AGENT_WORK_DIRECTORY_ARCHIVED", "工作目录已归档", 409)
    version = _version(db, item)
    _validate_flow_run_paths(db, flow_run_id, _path_values(db, version.id))
    return version.id, _working_directory(version.working_path)


def frozen_flow_run_conversation_context(
    db: Session, flow_run_id: str, work_directory_version_id: str
) -> str:
    _flow_run_root(db, flow_run_id)
    version = db.scalar(
        select(AgentWorkDirectoryVersion)
        .join(
            AgentWorkDirectory, AgentWorkDirectory.id == AgentWorkDirectoryVersion.work_directory_id
        )
        .where(
            AgentWorkDirectoryVersion.id == work_directory_version_id,
            AgentWorkDirectory.flow_run_id == flow_run_id,
        )
    )
    if version is None:
        raise DomainError("AGENT_WORK_DIRECTORY_VERSION_MISSING", "工作目录版本数据不完整", 409)
    _validate_flow_run_paths(db, flow_run_id, _path_values(db, version.id))
    return _working_directory(version.working_path)


__all__ = (
    "archive_work_directory",
    "conversation_context",
    "create_work_directory",
    "create_flow_run_work_directory",
    "flow_run_conversation_context",
    "frozen_flow_run_conversation_context",
    "frozen_conversation_context",
    "get_work_directory",
    "get_flow_run_work_directory",
    "list_work_directories",
    "list_flow_run_work_directories",
    "root_context",
    "update_work_directory",
)
