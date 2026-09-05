from __future__ import annotations

import stat
from pathlib import Path, PurePosixPath
from typing import Any

from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from flowweave.modules.agent_sessions.public import AgentConversationBinding
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
from flowweave.modules.users.application.security import (
    current_user_id,
    user_runtime_project_root,
)
from flowweave.shared.database import now
from flowweave.shared.errors import DomainError, not_found
from flowweave.shared.models import NodeAttempt, NodeRun
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
    db: Session,
    flow_run_id: str,
    node_attempt_id: str,
    work_directory_id: str,
    *,
    lock: bool = False,
) -> AgentWorkDirectory:
    query = select(AgentWorkDirectory).where(
        AgentWorkDirectory.id == work_directory_id,
        AgentWorkDirectory.flow_run_id == flow_run_id,
        AgentWorkDirectory.node_attempt_id == node_attempt_id,
    )
    if lock:
        query = query.with_for_update()
    item = db.scalar(query)
    if item is None:
        raise DomainError("AGENT_WORK_DIRECTORY_NOT_FOUND", "工作目录不存在", 404)
    return item


def _flow_run_attempt(db: Session, flow_run_id: str, node_attempt_id: str) -> NodeAttempt:
    attempt = db.get(NodeAttempt, node_attempt_id)
    if attempt is None:
        raise not_found("node_attempt", node_attempt_id)
    node_run = db.get(NodeRun, attempt.node_run_id)
    if node_run is None or node_run.flow_run_id != flow_run_id:
        raise DomainError(
            "NODE_CONVERSATION_CONTEXT_MISMATCH",
            "所选节点执行不属于当前 FlowRun",
            409,
        )
    return attempt


def _project_root(db: Session, workspace_id: str) -> Path:
    allocation = runtime_allocation_for_agent_workspace(db, workspace_id)
    shared_root = (
        Path(get_settings().workspace_root).absolute()
        / allocation.relative_root
        / "workspace"
        / "project"
    )
    root = shared_root / "users" / current_user_id()
    try:
        (shared_root / "users").mkdir(mode=0o700, exist_ok=True)
        root.mkdir(mode=0o700, exist_ok=True)
    except OSError as exc:
        raise DomainError(
            "AGENT_WORK_DIRECTORY_ROOT_UNAVAILABLE",
            "Agent 用户项目目录当前不可用",
            503,
        ) from exc
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


def _flow_run_root(db: Session, flow_run_id: str, node_attempt_id: str) -> Path:
    _flow_run_attempt(db, flow_run_id, node_attempt_id)
    return sandboxes.node_attempt_workspace_context(
        db, flow_run_id=flow_run_id, node_attempt_id=node_attempt_id
    ).host_working_directory


def _flow_run_runtime_root(
    db: Session, flow_run_id: str, node_attempt_id: str
) -> PurePosixPath:
    _flow_run_attempt(db, flow_run_id, node_attempt_id)
    return sandboxes.node_attempt_workspace_context(
        db, flow_run_id=flow_run_id, node_attempt_id=node_attempt_id
    ).runtime_working_directory


def _validate_flow_run_paths(
    db: Session, flow_run_id: str, node_attempt_id: str, values: tuple[str, ...]
) -> tuple[str, ...]:
    return _validated_relative_paths(values, _flow_run_root(db, flow_run_id, node_attempt_id))


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
    db: Session,
    flow_run_id: str,
    node_attempt_id: str,
    display_name: str,
    *,
    exclude_id: str | None = None,
) -> None:
    query = select(AgentWorkDirectory.id).where(
        AgentWorkDirectory.flow_run_id == flow_run_id,
        AgentWorkDirectory.node_attempt_id == node_attempt_id,
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


def _agent_working_directory(working_path: str) -> str:
    root = PurePosixPath(user_runtime_project_root())
    if working_path == ".":
        return root.as_posix()
    return (root / working_path).as_posix()


def _flow_run_working_directory(
    db: Session, flow_run_id: str, node_attempt_id: str, working_path: str
) -> str:
    root = _flow_run_runtime_root(db, flow_run_id, node_attempt_id)
    if working_path == ".":
        return root.as_posix()
    return (root / working_path).as_posix()


def _dict(db: Session, item: AgentWorkDirectory) -> dict[str, Any]:
    version = _version(db, item)
    return {
        "id": item.id,
        "display_name": item.display_name,
        "state": "ACTIVE",
        "current_version": {
            "id": version.id,
            "version": version.version,
            "selected_paths": list(_path_values(db, version.id)),
            "working_directory": (
                _agent_working_directory(version.working_path)
                if item.workspace_id is not None
                else _flow_run_working_directory(
                    db,
                    str(item.flow_run_id),
                    str(item.node_attempt_id),
                    version.working_path,
                )
            ),
        },
        "created_at": item.created_at.isoformat(),
        "updated_at": item.updated_at.isoformat(),
    }


def root_context(*, agent_workspace: bool = True) -> dict[str, Any]:
    return {
        "kind": "ROOT",
        "display_name": "根工作区",
        "working_directory": (
            user_runtime_project_root()
            if agent_workspace
            else _RUNTIME_PROJECT_ROOT.as_posix()
        ),
    }


def conversation_context(
    db: Session, workspace_id: str, work_directory_id: str | None
) -> tuple[str | None, str]:
    """Resolve and revalidate the context frozen by a lazy conversation bootstrap."""

    _workspace(db, workspace_id)
    if work_directory_id is None:
        return None, user_runtime_project_root()
    item = _directory(db, workspace_id, work_directory_id, lock=True)
    version = _version(db, item)
    # A version was checked when saved, but the underlying project tree is
    # mutable. Re-check it before handing the path to OpenHands.
    _validate_paths(db, workspace_id, _path_values(db, version.id))
    return version.id, _agent_working_directory(version.working_path)


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
    return _agent_working_directory(version.working_path)


def list_work_directories(db: Session, workspace_id: str) -> dict[str, Any]:
    _workspace(db, workspace_id)
    items = db.scalars(
        select(AgentWorkDirectory)
        .where(AgentWorkDirectory.workspace_id == workspace_id)
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


def delete_work_directory(db: Session, workspace_id: str, work_directory_id: str) -> None:
    _workspace(db, workspace_id)
    item = _directory(db, workspace_id, work_directory_id, lock=True)
    version_ids = list(
        db.scalars(
            select(AgentWorkDirectoryVersion.id).where(
                AgentWorkDirectoryVersion.work_directory_id == item.id
            )
        )
    )
    if version_ids and db.scalar(
        select(AgentConversationBinding.id)
        .where(AgentConversationBinding.work_directory_version_id.in_(version_ids))
        .limit(1)
    ):
        raise DomainError(
            "AGENT_WORK_DIRECTORY_IN_USE",
            "工作目录仍被会话引用，请先删除相关会话",
            409,
        )
    if version_ids:
        db.execute(
            delete(AgentWorkDirectoryPath).where(AgentWorkDirectoryPath.version_id.in_(version_ids))
        )
        db.execute(
            delete(AgentWorkDirectoryVersion).where(AgentWorkDirectoryVersion.id.in_(version_ids))
        )
    db.delete(item)
    db.flush()


def delete_flow_run_work_directory(
    db: Session, flow_run_id: str, node_attempt_id: str, work_directory_id: str
) -> None:
    _flow_run_attempt(db, flow_run_id, node_attempt_id)
    item = _flow_run_directory(db, flow_run_id, node_attempt_id, work_directory_id, lock=True)
    version_ids = list(
        db.scalars(
            select(AgentWorkDirectoryVersion.id).where(
                AgentWorkDirectoryVersion.work_directory_id == item.id
            )
        )
    )
    if version_ids and db.scalar(
        select(AgentConversationBinding.id)
        .where(AgentConversationBinding.work_directory_version_id.in_(version_ids))
        .limit(1)
    ):
        raise DomainError(
            "AGENT_WORK_DIRECTORY_IN_USE",
            "工作目录仍被会话引用，请先删除相关会话",
            409,
        )
    if version_ids:
        db.execute(
            delete(AgentWorkDirectoryPath).where(AgentWorkDirectoryPath.version_id.in_(version_ids))
        )
        db.execute(
            delete(AgentWorkDirectoryVersion).where(AgentWorkDirectoryVersion.id.in_(version_ids))
        )
    db.delete(item)
    db.flush()


def list_flow_run_work_directories(
    db: Session, flow_run_id: str, node_attempt_id: str
) -> dict[str, Any]:
    _flow_run_attempt(db, flow_run_id, node_attempt_id)
    items = db.scalars(
        select(AgentWorkDirectory)
        .where(
            AgentWorkDirectory.flow_run_id == flow_run_id,
            AgentWorkDirectory.node_attempt_id == node_attempt_id,
        )
        .order_by(AgentWorkDirectory.updated_at.desc(), AgentWorkDirectory.created_at.desc())
    )
    return {
        "root": {
            "kind": "ROOT",
            "display_name": "根工作区",
            "working_directory": _flow_run_runtime_root(
                db, flow_run_id, node_attempt_id
            ).as_posix(),
        },
        "items": [_dict(db, item) for item in items],
    }


def get_flow_run_work_directory(
    db: Session, flow_run_id: str, node_attempt_id: str, work_directory_id: str
) -> dict[str, Any]:
    _flow_run_attempt(db, flow_run_id, node_attempt_id)
    return _dict(db, _flow_run_directory(db, flow_run_id, node_attempt_id, work_directory_id))


def create_flow_run_work_directory(
    db: Session,
    flow_run_id: str,
    node_attempt_id: str,
    display_name: str,
    selected_paths: tuple[str, ...],
) -> dict[str, Any]:
    _flow_run_attempt(db, flow_run_id, node_attempt_id)
    name = _display_name(display_name)
    _assert_flow_run_name_available(db, flow_run_id, node_attempt_id, name)
    paths = _validate_flow_run_paths(db, flow_run_id, node_attempt_id, selected_paths)
    item = AgentWorkDirectory(
        flow_run_id=flow_run_id,
        node_attempt_id=node_attempt_id,
        display_name=name,
    )
    db.add(item)
    db.flush()
    _add_version(db, item, 1, paths)
    return _dict(db, item)


def flow_run_conversation_context(
    db: Session, flow_run_id: str, node_attempt_id: str, work_directory_id: str | None
) -> tuple[str | None, str]:
    _flow_run_attempt(db, flow_run_id, node_attempt_id)
    if work_directory_id is None:
        return None, _flow_run_runtime_root(db, flow_run_id, node_attempt_id).as_posix()
    item = _flow_run_directory(db, flow_run_id, node_attempt_id, work_directory_id, lock=True)
    version = _version(db, item)
    _validate_flow_run_paths(db, flow_run_id, node_attempt_id, _path_values(db, version.id))
    return version.id, _flow_run_working_directory(
        db, flow_run_id, node_attempt_id, version.working_path
    )


def frozen_flow_run_conversation_context(
    db: Session,
    flow_run_id: str,
    node_attempt_id: str,
    work_directory_version_id: str,
) -> str:
    _flow_run_attempt(db, flow_run_id, node_attempt_id)
    version = db.scalar(
        select(AgentWorkDirectoryVersion)
        .join(
            AgentWorkDirectory, AgentWorkDirectory.id == AgentWorkDirectoryVersion.work_directory_id
        )
        .where(
            AgentWorkDirectoryVersion.id == work_directory_version_id,
            AgentWorkDirectory.flow_run_id == flow_run_id,
            AgentWorkDirectory.node_attempt_id == node_attempt_id,
        )
    )
    if version is None:
        raise DomainError("AGENT_WORK_DIRECTORY_VERSION_MISSING", "工作目录版本数据不完整", 409)
    _validate_flow_run_paths(db, flow_run_id, node_attempt_id, _path_values(db, version.id))
    return _flow_run_working_directory(
        db, flow_run_id, node_attempt_id, version.working_path
    )


__all__ = (
    "delete_flow_run_work_directory",
    "delete_work_directory",
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
