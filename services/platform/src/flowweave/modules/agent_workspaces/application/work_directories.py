from __future__ import annotations

import hashlib
import json
import os
import shutil
import stat
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path, PurePosixPath
from typing import Any
from uuid import uuid4

from sqlalchemy import delete, func, select
from sqlalchemy.orm import Session

from flowweave.modules.agent_sessions.infrastructure.models import (
    AgentAttachmentUpload,
    AgentAttachmentUploadPart,
    AgentConversationMessageAttachment,
    AgentConversationSchedule,
    AgentSidebarConversation,
)
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
from flowweave.modules.cases.infrastructure.models import Case
from flowweave.modules.sandboxes import public as sandboxes
from flowweave.modules.users.application.security import (
    current_user_id,
    user_runtime_project_root,
)
from flowweave.shared.application.transactions import (
    register_commit_action,
    register_rollback_action,
)
from flowweave.shared.database import now
from flowweave.shared.errors import DomainError, not_found
from flowweave.shared.models import NodeAttempt, NodeRun
from flowweave.shared.settings import get_settings

_RUNTIME_PROJECT_ROOT = PurePosixPath("/runtime/workspace/project")
_MAX_RELATIVE_PATH_LENGTH = 500


def _lock_directory_paths(db: Session, workspace_id: str) -> None:
    # Serialize path references with deletions without blocking other users or
    # unrelated session operations on the shared Workspace row.
    if db.get_bind().dialect.name == "postgresql":
        scope = f"work-directory:{workspace_id}:{current_user_id()}"
        digest = hashlib.sha256(scope.encode()).digest()
        db.execute(select(func.pg_advisory_xact_lock(int.from_bytes(digest[:8], signed=True))))


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
        or not relative.parts
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


def _flow_run_runtime_root(db: Session, flow_run_id: str, node_attempt_id: str) -> PurePosixPath:
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


def _agent_working_directory(workspace_id: str, working_path: str) -> str:
    root = PurePosixPath(user_runtime_project_root(workspace_id))
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
        "state": "ARCHIVED" if item.archived_at else "ACTIVE",
        "archived_at": item.archived_at.isoformat() if item.archived_at else None,
        "current_version": {
            "id": version.id,
            "version": version.version,
            "selected_paths": list(_path_values(db, version.id)),
            "working_directory": (
                _agent_working_directory(str(item.workspace_id), version.working_path)
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


def root_context(
    *, workspace_id: str | None = None, agent_workspace: bool = True
) -> dict[str, Any]:
    return {
        "kind": "ROOT",
        "display_name": "根工作区",
        "working_directory": (
            user_runtime_project_root(workspace_id)
            if workspace_id is not None
            else None
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
        return None, user_runtime_project_root(workspace_id)
    item = _directory(db, workspace_id, work_directory_id, lock=True)
    version = _version(db, item)
    # A version was checked when saved, but the underlying project tree is
    # mutable. Re-check it before handing the path to OpenHands.
    _validate_paths(db, workspace_id, _path_values(db, version.id))
    return version.id, _agent_working_directory(workspace_id, version.working_path)


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
    return _agent_working_directory(workspace_id, version.working_path)


def _directory_predicates(
    workspace_id: str, *, include_archived: bool, include_managed: bool
) -> list[Any]:
    predicates: list[Any] = [AgentWorkDirectory.workspace_id == workspace_id]
    if not include_managed:
        predicates.extend(
            (
                ~AgentWorkDirectory.id.in_(
                    select(AgentConversationSchedule.work_directory_id).where(
                        AgentConversationSchedule.workspace_id == workspace_id,
                        AgentConversationSchedule.workspace_mode == "AUTOMATION",
                        AgentConversationSchedule.work_directory_id.is_not(None),
                    )
                ),
                ~AgentWorkDirectory.id.in_(
                    select(Case.control_work_directory_id).where(
                        Case.control_work_directory_id.is_not(None)
                    )
                ),
            )
        )
    if not include_archived:
        predicates.append(AgentWorkDirectory.archived_at.is_(None))
    return predicates


def _ordered_directories(db: Session, predicates: list[Any]) -> list[AgentWorkDirectory]:
    return list(
        db.scalars(
            select(AgentWorkDirectory)
            .where(*predicates)
            .order_by(
                AgentWorkDirectory.archived_at,
                AgentWorkDirectory.updated_at.desc(),
                AgentWorkDirectory.created_at.desc(),
            )
        )
    )


def list_work_directories(
    db: Session, workspace_id: str, *, include_archived: bool = False
) -> dict[str, Any]:
    _workspace(db, workspace_id)
    items = _ordered_directories(
        db,
        _directory_predicates(
            workspace_id, include_archived=include_archived, include_managed=False
        ),
    )
    return {
        "root": root_context(workspace_id=workspace_id),
        "items": [_dict(db, item) for item in items],
    }


def list_management_workspaces(
    db: Session, workspace_id: str, *, include_archived: bool = False
) -> dict[str, Any]:
    workspace = _workspace(db, workspace_id)
    items = _ordered_directories(
        db,
        _directory_predicates(
            workspace_id, include_archived=include_archived, include_managed=True
        ),
    )
    schedules = {
        item.work_directory_id: item
        for item in db.scalars(
            select(AgentConversationSchedule).where(
                AgentConversationSchedule.workspace_id == workspace_id,
                AgentConversationSchedule.workspace_mode == "AUTOMATION",
                AgentConversationSchedule.work_directory_id.is_not(None),
            )
        )
    }
    cases = {
        item.control_work_directory_id: item
        for item in db.scalars(
            select(Case).where(Case.control_work_directory_id.is_not(None))
        )
    }
    result: list[dict[str, Any]] = []
    for item in items:
        value = _dict(db, item)
        if schedule := schedules.get(item.id):
            value.update(
                {
                    "display_name": schedule.name,
                    "workspace_kind": "AGENT_SCHEDULE",
                    "source_id": schedule.id,
                }
            )
        elif case := cases.get(item.id):
            value.update(
                {
                    "display_name": case.name,
                    "workspace_kind": "CASE",
                    "source_id": case.id,
                }
            )
        else:
            value["workspace_kind"] = "AGENT"
        result.append(value)
    root = root_context(workspace_id=workspace_id)
    root.update({"display_name": workspace.display_name, "workspace_kind": "AGENT"})
    return {"root": root, "items": result}


def _directory_conversations(
    db: Session, workspace_id: str, work_directory_id: str
) -> list[AgentConversationBinding]:
    version_ids = select(AgentWorkDirectoryVersion.id).where(
        AgentWorkDirectoryVersion.work_directory_id == work_directory_id
    )
    return list(
        db.scalars(
            select(AgentConversationBinding)
            .where(
                AgentConversationBinding.workspace_id == workspace_id,
                AgentConversationBinding.work_directory_version_id.in_(version_ids),
                AgentConversationBinding.lifecycle == "ACTIVE",
                ~AgentConversationBinding.id.in_(
                    select(AgentSidebarConversation.sidebar_binding_id)
                ),
            )
            .order_by(AgentConversationBinding.id)
            .with_for_update()
        )
    )


def archive_work_directory(
    db: Session, workspace_id: str, work_directory_id: str
) -> dict[str, Any]:
    _workspace(db, workspace_id)
    item = _directory(db, workspace_id, work_directory_id, lock=True)
    bindings = _directory_conversations(db, workspace_id, item.id)
    from flowweave.modules.agent_sessions.application import conversations

    # Validate the complete batch before changing either directory or children.
    # Repeated archive also repairs legacy directories with unarchived children.
    for binding in bindings:
        if binding.archived_at is None:
            conversations.assert_conversation_stopped(db, workspace_id, binding.id, lock=False)
    timestamp = now()
    for binding in bindings:
        if binding.archived_at is None:
            binding.archived_at = timestamp
            binding.updated_at = timestamp
    if item.archived_at is None:
        item.archived_at = timestamp
        item.updated_at = timestamp
    db.flush()
    return _dict(db, item)


def restore_work_directory(
    db: Session, workspace_id: str, work_directory_id: str
) -> dict[str, Any]:
    _workspace(db, workspace_id)
    item = _directory(db, workspace_id, work_directory_id, lock=True)
    timestamp = now()
    for binding in _directory_conversations(db, workspace_id, item.id):
        if binding.archived_at is not None:
            binding.archived_at = None
            binding.updated_at = timestamp
    if item.archived_at is not None:
        item.archived_at = None
        item.updated_at = timestamp
    db.flush()
    return _dict(db, item)


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
    _lock_directory_paths(db, workspace_id)
    name = _display_name(display_name)
    _assert_name_available(db, workspace_id, name)
    paths = _validate_paths(db, workspace_id, selected_paths)
    item = AgentWorkDirectory(workspace_id=workspace_id, display_name=name)
    db.add(item)
    db.flush()
    _add_version(db, item, 1, paths)
    return _dict(db, item)


def create_automation_work_directory(
    db: Session, workspace_id: str, automation_id: str, display_name: str
) -> dict[str, Any]:
    root = _project_root(db, workspace_id)
    relative_path = f".flowweave/automations/{automation_id}"
    target = root / relative_path
    try:
        target.mkdir(mode=0o700, parents=True, exist_ok=False)
    except FileExistsError as exc:
        raise DomainError(
            "AGENT_AUTOMATION_WORKSPACE_CONFLICT", "定时任务工作区已存在", 409
        ) from exc
    except OSError as exc:
        raise DomainError(
            "AGENT_AUTOMATION_WORKSPACE_UNAVAILABLE", "无法创建定时任务工作区", 503
        ) from exc
    try:
        return create_work_directory(db, workspace_id, display_name, (relative_path,))
    except Exception:
        try:
            target.rmdir()
        except OSError:
            pass
        raise


def create_case_work_directory(
    db: Session, workspace_id: str, case_id: str, display_name: str
) -> dict[str, Any]:
    """Create a user-scoped Case directory within the shared Agent Runtime."""

    root = _project_root(db, workspace_id)
    relative_path = f".flowweave/cases/{case_id}"
    target = root / relative_path
    try:
        target.mkdir(mode=0o700, parents=True, exist_ok=False)
    except FileExistsError as exc:
        raise DomainError("CASE_WORKSPACE_CONFLICT", "事项工作目录已存在", 409) from exc
    except OSError as exc:
        raise DomainError("CASE_WORKSPACE_UNAVAILABLE", "无法创建事项工作目录", 503) from exc
    try:
        result = create_work_directory(db, workspace_id, display_name, (relative_path,))
        metadata = target.lstat()
        directory_id = str(result["id"])

        def rollback_uncommitted_directory() -> None:
            # A direct Session.commit() may leave this compensation registered.
            # ORM objects may have been evicted and reloaded, so the database
            # after rollback determines whether creation was ever committed.
            if db.get(AgentWorkDirectory, directory_id) is None:
                _remove_case_directory(root, PurePosixPath(relative_path), metadata)

        register_rollback_action(db, rollback_uncommitted_directory)
        return result
    except Exception:
        try:
            target.rmdir()
        except OSError:
            pass
        raise


def write_automation_files(
    db: Session,
    workspace_id: str,
    work_directory_id: str,
    *,
    automation_id: str,
    name: str,
    prompt: str,
    cron_expression: str,
    workspace_mode: str,
    conversation_mode: str,
    model_provider_id: str,
    model_name: str,
    reasoning_effort: str | None,
    capability_version_ids: tuple[str, ...],
    credential_ids: tuple[str, ...],
) -> None:
    item = _directory(db, workspace_id, work_directory_id)
    version = _version(db, item)
    root = _project_root(db, workspace_id) / version.working_path
    automation_root = (
        root
        if workspace_mode == "AUTOMATION"
        else root / ".flowweave" / "automations" / automation_id
    )
    try:
        automation_root.mkdir(mode=0o700, parents=True, exist_ok=True)
        config = "\n".join(
            [
                "version = 1",
                f'id = "{automation_id}"',
                f"name = {json.dumps(name, ensure_ascii=False)}",
                f"cron_expression = {json.dumps(cron_expression)}",
                f"workspace_mode = {json.dumps(workspace_mode)}",
                f"conversation_mode = {json.dumps(conversation_mode)}",
                f"model_provider_id = {json.dumps(model_provider_id)}",
                f"model_name = {json.dumps(model_name)}",
                f"reasoning_effort = {json.dumps(reasoning_effort or '')}",
                f"capability_version_ids = {json.dumps(list(capability_version_ids))}",
                f"credential_ids = {json.dumps(list(credential_ids))}",
                "",
                "[prompt]",
                f"business = {json.dumps(prompt, ensure_ascii=False)}",
                "",
            ]
        )
        (automation_root / "automation.toml").write_text(config, encoding="utf-8")
        memory = automation_root / "memory.md"
        if not memory.exists():
            memory.write_text(f"# {name} 自动化记忆\n\n", encoding="utf-8")
    except OSError as exc:
        raise DomainError(
            "AGENT_AUTOMATION_METADATA_UNAVAILABLE", "无法写入定时任务配置文件", 503
        ) from exc


def write_case_files(
    db: Session,
    workspace_id: str,
    work_directory_id: str,
    *,
    case_id: str,
    name: str,
    objective: str,
    state: str,
    stages: tuple[dict[str, Any], ...],
    control_plans: tuple[dict[str, Any], ...] = (),
    description: str = "",
    materials: tuple[dict[str, Any], ...] = (),
) -> None:
    """Maintain Case-local soft context without replacing platform ownership."""

    item = _directory(db, workspace_id, work_directory_id)
    version = _version(db, item)
    case_root = _project_root(db, workspace_id) / version.working_path
    try:
        case_root.mkdir(mode=0o700, parents=True, exist_ok=True)
        lines = [
            "version = 1",
            f"id = {json.dumps(case_id)}",
            f"name = {json.dumps(name, ensure_ascii=False)}",
            f"objective = {json.dumps(objective, ensure_ascii=False)}",
            f"description = {json.dumps(description, ensure_ascii=False)}",
            f"state = {json.dumps(state)}",
            "",
        ]
        for material in materials:
            lines.extend(
                [
                    "[[materials]]",
                    f"kind = {json.dumps(material['kind'])}",
                    f"name = {json.dumps(material['name'], ensure_ascii=False)}",
                    f"value = {json.dumps(material['value'], ensure_ascii=False)}",
                    "",
                ]
            )
        for stage in stages:
            lines.extend(
                [
                    "[[stages]]",
                    f"id = {json.dumps(str(stage['id']))}",
                    f"key = {json.dumps(str(stage['stage_key']))}",
                    f"name = {json.dumps(str(stage['name']), ensure_ascii=False)}",
                    f"objective = {json.dumps(str(stage['objective']), ensure_ascii=False)}",
                    f"position = {int(stage['position'])}",
                    f"state = {json.dumps(str(stage['state']))}",
                    f"execution_mode = {json.dumps(str(stage['execution_mode']))}",
                    f"flow_run_id = {json.dumps(stage.get('flow_run_id') or '')}",
                    "current_execution_flow_run_id = "
                    f"{json.dumps(stage.get('current_execution_flow_run_id') or '')}",
                    "",
                ]
            )
        for plan in control_plans:
            lines.extend(
                [
                    "[[control_plans]]",
                    f"id = {json.dumps(str(plan['id']))}",
                    f"stage_id = {json.dumps(str(plan['stage_id']))}",
                    "execution_flow_run_id = "
                    f"{json.dumps(plan.get('execution_flow_run_id') or '')}",
                    f"action = {json.dumps(str(plan['action']))}",
                    f"state = {json.dumps(str(plan['state']))}",
                    f"reason = {json.dumps(str(plan['reason']), ensure_ascii=False)}",
                    f"error_code = {json.dumps(plan.get('error_code') or '')}",
                    f"created_at = {json.dumps(str(plan['created_at']))}",
                    "",
                ]
            )
        (case_root / "case.toml").write_text("\n".join(lines), encoding="utf-8")
        memory = case_root / "memory.md"
        if not memory.exists():
            memory.write_text(f"# {name} Case 记忆\n\n", encoding="utf-8")
    except OSError as exc:
        raise DomainError("CASE_METADATA_UNAVAILABLE", "无法写入事项配置文件", 503) from exc


def update_work_directory(
    db: Session,
    workspace_id: str,
    work_directory_id: str,
    *,
    display_name: str | None,
    selected_paths: tuple[str, ...] | None,
) -> dict[str, Any]:
    _workspace(db, workspace_id)
    _lock_directory_paths(db, workspace_id)
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
    from flowweave.modules.agent_sessions.application import conversations
    from flowweave.modules.agent_sessions.application.draft_attachments import (
        ATTACHMENT_PATH,
    )
    from flowweave.modules.agent_workspaces.application import (
        directory_deletion,
        workspace,
    )

    _workspace(db, workspace_id)
    _lock_directory_paths(db, workspace_id)
    item = _directory(db, workspace_id, work_directory_id, lock=True)
    if db.scalar(select(Case.id).where(Case.control_work_directory_id == item.id)) or db.scalar(
        select(AgentConversationSchedule.id).where(
            AgentConversationSchedule.work_directory_id == item.id
        )
    ):
        raise DomainError(
            "AGENT_WORK_DIRECTORY_REFERENCED",
            "工作区仍被事项或定时任务使用，请先解除关联",
            409,
        )
    versions = list(
        db.scalars(
            select(AgentWorkDirectoryVersion).where(
                AgentWorkDirectoryVersion.work_directory_id == item.id
            )
        )
    )
    version_ids = [version.id for version in versions]
    paths: set[PurePosixPath] = set()
    if not versions:
        raise DomainError("AGENT_WORK_DIRECTORY_VERSION_MISSING", "工作目录版本数据不完整", 409)
    for version in versions:
        selected = _path_values(db, version.id)
        if not selected and version.working_path == ".":
            raise DomainError("AGENT_WORK_DIRECTORY_VERSION_MISSING", "工作目录版本数据不完整", 409)
        paths.update(_canonical_relative_path(path) for path in selected)
    # Older single-path versions can predate path rows. A multi-path version's
    # working_path='.' must never be interpreted as permission to delete root.
    paths.update(
        _canonical_relative_path(version.working_path)
        for version in versions
        if version.working_path != "."
    )
    if any(path.parts[0] in {"uploads", ".flowweave"} for path in paths):
        raise DomainError("AGENT_WORK_DIRECTORY_DELETE_UNSAFE", "不能删除平台保留目录", 409)
    bindings = list(
        db.scalars(
            select(AgentConversationBinding)
            .where(
                AgentConversationBinding.workspace_id == workspace_id,
                AgentConversationBinding.work_directory_version_id.in_(version_ids),
            )
            .with_for_update()
        )
    )
    by_id = {binding.id: binding for binding in bindings}
    # Sidebar conversations may have no independent directory version. Follow
    # their explicit binding relation, never infer ownership from title or cwd.
    while by_id:
        child_ids = list(
            db.scalars(
                select(AgentSidebarConversation.sidebar_binding_id).where(
                    AgentSidebarConversation.workspace_id == workspace_id,
                    AgentSidebarConversation.source_binding_id.in_(by_id),
                    ~AgentSidebarConversation.sidebar_binding_id.in_(by_id),
                )
            )
        )
        children = (
            list(
                db.scalars(
                    select(AgentConversationBinding)
                    .where(
                        AgentConversationBinding.id.in_(child_ids),
                        AgentConversationBinding.workspace_id == workspace_id,
                    )
                    .with_for_update()
                )
            )
            if child_ids
            else []
        )
        if not children:
            break
        by_id.update((child.id, child) for child in children)
    binding_ids = set(by_id)
    root = workspace.ensure_user_project_root(db, workspace_id)
    runtime_root = PurePosixPath(user_runtime_project_root(workspace_id))
    other_paths = list(
        db.scalars(
            select(AgentWorkDirectoryPath.relative_path)
            .join(
                AgentWorkDirectoryVersion,
                AgentWorkDirectoryVersion.id == AgentWorkDirectoryPath.version_id,
            )
            .join(
                AgentWorkDirectory,
                AgentWorkDirectory.id == AgentWorkDirectoryVersion.work_directory_id,
            )
            .where(
                AgentWorkDirectory.workspace_id == workspace_id,
                AgentWorkDirectory.id != item.id,
            )
        )
    )
    other_paths.extend(
        db.scalars(
            select(AgentWorkDirectoryVersion.working_path)
            .join(
                AgentWorkDirectory,
                AgentWorkDirectory.id == AgentWorkDirectoryVersion.work_directory_id,
            )
            .where(
                AgentWorkDirectory.workspace_id == workspace_id,
                AgentWorkDirectory.id != item.id,
                AgentWorkDirectoryVersion.working_path != ".",
            )
        )
    )

    def overlaps(path: PurePosixPath) -> bool:
        return any(path.is_relative_to(target) or target.is_relative_to(path) for target in paths)

    if any(overlaps(_canonical_relative_path(path)) for path in other_paths):
        raise DomainError(
            "AGENT_WORK_DIRECTORY_SHARED_PATH",
            "目录仍被其他工作区使用，无法连同文件删除",
            409,
        )
    for binding in db.scalars(
        select(AgentConversationBinding).where(
            AgentConversationBinding.workspace_id == workspace_id,
            ~AgentConversationBinding.id.in_(binding_ids),
        )
    ):
        cwd = PurePosixPath(binding.working_directory or "/")
        if (
            cwd != runtime_root
            and cwd.is_relative_to(runtime_root)
            and overlaps(cwd.relative_to(runtime_root))
        ):
            raise DomainError(
                "AGENT_WORK_DIRECTORY_SHARED_PATH",
                "目录仍被其他会话使用，无法删除",
                409,
            )
    for path in db.scalars(
        select(AgentConversationMessageAttachment.path)
        .join(
            AgentConversationBinding,
            AgentConversationBinding.id == AgentConversationMessageAttachment.binding_id,
        )
        .where(
            AgentConversationBinding.workspace_id == workspace_id,
            ~AgentConversationBinding.id.in_(binding_ids),
        )
    ):
        candidate = PurePosixPath(path)
        if candidate.is_relative_to(runtime_root) and any(
            candidate.relative_to(runtime_root).is_relative_to(target) for target in paths
        ):
            raise DomainError(
                "AGENT_WORK_DIRECTORY_SHARED_PATH",
                "目录中仍有其他会话的附件，无法删除",
                409,
            )
    for binding_id in binding_ids:
        conversations.assert_conversation_stopped(
            db, workspace_id, binding_id, lock=False, allow_missing=True
        )
    uploads = list(
        db.scalars(
            select(AgentAttachmentUpload)
            .where(
                AgentAttachmentUpload.host_kind == "AGENT_WORKSPACE",
                AgentAttachmentUpload.host_id == workspace_id,
                (AgentAttachmentUpload.work_directory_id == item.id)
                | AgentAttachmentUpload.binding_id.in_(binding_ids),
            )
            .with_for_update()
        )
    )
    upload_ids = [upload.id for upload in uploads]
    draft_owners = {upload.attachment_owner_id for upload in uploads if upload.attachment_owner_id}
    owners = binding_ids | draft_owners
    with directory_deletion.parent_fd(root, PurePosixPath("uploads/entry")) as uploads:
        if uploads is not None:
            for name in os.listdir(uploads):
                matched = ATTACHMENT_PATH.fullmatch(str(runtime_root / "uploads" / name))
                if matched and matched.group("owner") in owners:
                    shared = db.scalar(
                        select(AgentConversationMessageAttachment.id).where(
                            AgentConversationMessageAttachment.path
                            == str(runtime_root / "uploads" / name),
                            ~AgentConversationMessageAttachment.binding_id.in_(binding_ids),
                        )
                    )
                    if shared is not None:
                        raise DomainError(
                            "AGENT_WORK_DIRECTORY_SHARED_ATTACHMENT",
                            "附件仍被其他会话引用，无法删除",
                            409,
                        )
                    paths.add(PurePosixPath("uploads") / name)
    for path in paths:
        directory_deletion.metadata(root, path)
    directory_deletion.stage(db, workspace_id, root, tuple(paths))
    for binding_id in binding_ids:
        conversations.delete_conversation(
            db,
            workspace_id,
            binding_id,
            f"delete-work-directory:{work_directory_id}:{binding_id}",
        )
    db.execute(
        delete(AgentAttachmentUploadPart).where(AgentAttachmentUploadPart.upload_id.in_(upload_ids))
    )
    db.execute(delete(AgentAttachmentUpload).where(AgentAttachmentUpload.id.in_(upload_ids)))
    db.execute(
        delete(AgentSidebarConversation).where(
            AgentSidebarConversation.workspace_id == workspace_id,
            AgentSidebarConversation.sidebar_binding_id.in_(binding_ids),
        )
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


@contextmanager
def _case_directory_parent(root: Path, relative_path: PurePosixPath) -> Iterator[int | None]:
    """Open each parent without following links, including at commit time."""

    descriptor: int | None = None
    try:
        descriptor = os.open(root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        for part in relative_path.parts[:-1]:
            child = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=descriptor)
            os.close(descriptor)
            descriptor = child
    except FileNotFoundError:
        if descriptor is not None:
            os.close(descriptor)
        descriptor = None
    except OSError as exc:
        if descriptor is not None:
            os.close(descriptor)
        raise DomainError(
            "CASE_WORKSPACE_DELETE_UNSAFE", "事项工作目录不安全，拒绝删除", 409
        ) from exc
    try:
        yield descriptor
    finally:
        if descriptor is not None:
            os.close(descriptor)


def _case_directory_metadata(root: Path, relative_path: PurePosixPath) -> os.stat_result | None:
    with _case_directory_parent(root, relative_path) as descriptor:
        if descriptor is None:
            return None
        try:
            metadata = os.stat(relative_path.name, dir_fd=descriptor, follow_symlinks=False)
        except FileNotFoundError:
            return None
        if not stat.S_ISDIR(metadata.st_mode) or not shutil.rmtree.avoids_symlink_attacks:
            raise DomainError("CASE_WORKSPACE_DELETE_UNSAFE", "事项工作目录不安全，拒绝删除", 409)
        return metadata


def _remove_case_directory(
    root: Path, relative_path: PurePosixPath, expected: os.stat_result
) -> None:
    with _case_directory_parent(root, relative_path) as descriptor:
        if descriptor is None:
            return
        try:
            actual = os.stat(relative_path.name, dir_fd=descriptor, follow_symlinks=False)
        except FileNotFoundError:
            return
        if not stat.S_ISDIR(actual.st_mode) or (actual.st_dev, actual.st_ino) != (
            expected.st_dev,
            expected.st_ino,
        ):
            raise DomainError("CASE_WORKSPACE_DELETE_UNSAFE", "事项工作目录已变更，拒绝删除", 409)
        shutil.rmtree(relative_path.name, dir_fd=descriptor)


def delete_case_work_directory(
    db: Session, workspace_id: str, case_id: str, work_directory_id: str
) -> None:
    """Delete one Case-owned directory after its conversation has been removed."""

    item = _directory(db, workspace_id, work_directory_id, lock=True)
    root = _project_root(db, workspace_id)
    relative_path = _canonical_relative_path(f".flowweave/cases/{case_id}")
    version = _version(db, item)
    if _path_values(db, version.id) != (relative_path.as_posix(),):
        raise DomainError("CASE_WORKSPACE_DELETE_SCOPE_INVALID", "事项工作目录归属校验失败", 409)
    metadata = _case_directory_metadata(root, relative_path)
    version_ids = list(
        db.scalars(
            select(AgentWorkDirectoryVersion.id).where(
                AgentWorkDirectoryVersion.work_directory_id == item.id
            )
        )
    )
    binding_exists = bool(
        version_ids
        and db.scalar(
            select(AgentConversationBinding.id).where(
                AgentConversationBinding.workspace_id == workspace_id,
                AgentConversationBinding.work_directory_version_id.in_(version_ids),
            )
        )
    )
    if binding_exists:
        raise DomainError(
            "CASE_WORKSPACE_CONVERSATION_REMAINS", "事项会话仍存在，无法删除事项工作区", 409
        )
    if version_ids:
        db.execute(
            delete(AgentWorkDirectoryPath).where(AgentWorkDirectoryPath.version_id.in_(version_ids))
        )
        db.execute(
            delete(AgentWorkDirectoryVersion).where(AgentWorkDirectoryVersion.id.in_(version_ids))
        )
    db.delete(item)
    if metadata is not None:
        register_commit_action(db, lambda: _remove_case_directory(root, relative_path, metadata))
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
    # A selected logical directory controls the frozen file scope, not the
    # OpenHands workspace root. Every Attempt in this record shares its
    # record-level project root.
    return version.id, _flow_run_runtime_root(db, flow_run_id, node_attempt_id).as_posix()


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
    return _flow_run_runtime_root(db, flow_run_id, node_attempt_id).as_posix()


def write_case_material_file(db: Session, workspace_id: str, case_id: str, content: bytes) -> str:
    root = _project_root(db, workspace_id)
    relative = PurePosixPath(f".flowweave/cases/{case_id}/materials")
    with _case_directory_parent(root, relative) as parent:
        if parent is None:
            raise DomainError("CASE_WORKSPACE_UNAVAILABLE", "Case 工作目录不可用", 409)
        try:
            os.mkdir("materials", mode=0o700, dir_fd=parent)
        except FileExistsError:
            pass
        folder = os.open("materials", os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=parent)
        stored_name = str(uuid4())
        try:
            descriptor = os.open(
                stored_name,
                os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                0o600,
                dir_fd=folder,
            )
            with os.fdopen(descriptor, "wb") as output:
                output.write(content)
            stored_path = relative / stored_name

            def rollback_upload() -> None:
                with _case_directory_parent(root, stored_path) as directory:
                    if directory is not None:
                        try:
                            os.unlink(stored_path.name, dir_fd=directory)
                        except FileNotFoundError:
                            pass

            register_rollback_action(db, rollback_upload)
        finally:
            os.close(folder)
    return stored_path.as_posix()


def read_case_material_file(db: Session, workspace_id: str, case_id: str, value: str) -> bytes:
    relative = _canonical_relative_path(value)
    prefix = PurePosixPath(f".flowweave/cases/{case_id}/materials")
    if relative.parent != prefix:
        raise DomainError("CASE_MATERIAL_INVALID", "文件资料不属于当前 Case", 422)
    root = _project_root(db, workspace_id)
    try:
        with _case_directory_parent(root, relative) as parent:
            if parent is None:
                raise not_found("case_material", value)
            descriptor = os.open(
                relative.name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=parent
            )
            with os.fdopen(descriptor, "rb") as source:
                if not stat.S_ISREG(os.fstat(source.fileno()).st_mode):
                    raise DomainError("CASE_MATERIAL_INVALID", "资料不是普通文件", 422)
                content = source.read(64 * 1024 * 1024 + 1)
                if len(content) > 64 * 1024 * 1024:
                    raise DomainError("CASE_MATERIAL_TOO_LARGE", "文件超过 64 MB", 413)
    except OSError as exc:
        raise not_found("case_material", value) from exc
    return content


__all__ = (
    "write_case_material_file",
    "read_case_material_file",
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
    "list_management_workspaces",
    "list_work_directories",
    "list_flow_run_work_directories",
    "root_context",
    "update_work_directory",
)
