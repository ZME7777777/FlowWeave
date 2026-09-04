"""Node-Attempt workspace projection for sessions entered from a FlowRun node.

The project root remains shared by a FlowRun. Logical work directories are
owned by one node Attempt and never appear through another node entry.
"""

from __future__ import annotations

import mimetypes
import os
import shutil
import stat
from pathlib import Path, PurePosixPath
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from flowweave.modules.agent_sessions.application import flow_node_conversations
from flowweave.modules.agent_sessions.application.flow_node_host import (
    resolve_flow_node_session_host,
)
from flowweave.modules.agent_sessions.application.ide import ssh_remote_descriptor
from flowweave.modules.agent_sessions.public import AgentConversationMessageAttachment
from flowweave.modules.agent_workspaces import public as agent_workspace_host
from flowweave.modules.sandboxes import public as sandboxes
from flowweave.shared.errors import DomainError
from flowweave.shared.models import NodeAttempt

_RUNTIME_PROJECT = PurePosixPath("/runtime/workspace/project")
_MAX_INDEX_ENTRIES = 20_000
_MAX_FILE_BYTES = 25 * 1024 * 1024

AgentWorkDirectory = agent_workspace_host.AgentWorkDirectory
AgentWorkDirectoryPath = agent_workspace_host.AgentWorkDirectoryPath
AgentWorkDirectoryVersion = agent_workspace_host.AgentWorkDirectoryVersion


def _authorize_entry(db: Session, *, flow_run_id: str, attempt_id: str) -> Path:
    host = resolve_flow_node_session_host(
        db,
        flow_run_id=flow_run_id,
        attempt_id=attempt_id,
        require_start_permission=False,
    )
    # The stable in-container project path belongs to this Attempt Runtime
    # allocation. Never resolve it through the FlowRun allocation: that would
    # expose files produced by another Attempt.
    del host
    root = sandboxes.node_attempt_workspace_project_path(
        db, flow_run_id=flow_run_id, node_attempt_id=attempt_id
    )
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
        if (
            version is None
            or directory is None
            or directory.flow_run_id != flow_run_id
            or directory.node_attempt_id != attempt_id
        ):
            raise DomainError("AGENT_WORK_DIRECTORY_VERSION_MISSING", "工作目录版本数据不完整", 409)
        paths = _version_paths(db, version.id)
        details = {
            "id": directory.id,
            "display_name": directory.display_name,
            "state": "ACTIVE",
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
        directory = agent_workspace_host.get_flow_run_work_directory(
            db, flow_run_id, attempt_id, work_directory_id
        )
        paths = tuple(directory["current_version"]["selected_paths"])
        roots = tuple(str(_RUNTIME_PROJECT / path) for path in paths)
        return directory["current_version"]["working_directory"], directory, roots
    return str(_RUNTIME_PROJECT), None, (str(_RUNTIME_PROJECT),)


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
        # This entry was authorized against an active Attempt Runtime.
        "runtime": {"state": "ACTIVE", "write_available": True},
        "ide": {
            "workspace_path": working_directory,
            "gateway": ssh_remote_descriptor(project_root, working_directory),
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
    attachment = (
        db.scalar(
            select(AgentConversationMessageAttachment)
            .where(
                AgentConversationMessageAttachment.binding_id == binding_id,
                AgentConversationMessageAttachment.path == path,
            )
            .order_by(AgentConversationMessageAttachment.created_at.desc())
        )
        if binding_id
        else None
    )
    content_type = (
        attachment.mime_type
        if attachment
        else mimetypes.guess_type(candidate.name)[0] or "application/octet-stream"
    )
    filename = attachment.filename if attachment else candidate.name
    return content, content_type, filename


def delete_entries(
    db: Session,
    *,
    flow_run_id: str,
    attempt_id: str,
    binding_id: str | None,
    work_directory_id: str | None,
    paths: tuple[str, ...],
) -> list[str]:
    if not paths:
        raise DomainError("FLOW_RUN_WORKSPACE_DELETE_EMPTY", "请选择要删除的文件或目录", 422)
    if len(set(paths)) > 100:
        raise DomainError("FLOW_RUN_WORKSPACE_DELETE_TOO_MANY", "一次最多删除 100 项", 422)
    project_root = _authorize_entry(db, flow_run_id=flow_run_id, attempt_id=attempt_id)
    _, _, roots = _scope(
        db,
        flow_run_id=flow_run_id,
        attempt_id=attempt_id,
        binding_id=binding_id,
        work_directory_id=work_directory_id,
    )
    _validate_scope_roots(project_root, roots)
    candidates: list[tuple[str, Path]] = []
    for path in sorted(set(paths)):
        parsed = PurePosixPath(path)
        if (
            path in roots
            or not parsed.is_absolute()
            or not parsed.is_relative_to(_RUNTIME_PROJECT)
            or parsed.as_posix() != path
            or any(part in {"", ".", ".."} or part.startswith(".") for part in parsed.parts)
            or not any(path.startswith(root.rstrip("/") + "/") for root in roots)
        ):
            raise DomainError(
                "FLOW_RUN_WORKSPACE_PATH_INVALID",
                "不能删除当前工作区根目录或范围外路径",
                422,
            )
        candidate = project_root.joinpath(*parsed.relative_to(_RUNTIME_PROJECT).parts)
        try:
            metadata = candidate.lstat()
            resolved = candidate.resolve(strict=True)
        except OSError as exc:
            raise DomainError(
                "FLOW_RUN_WORKSPACE_FILE_NOT_FOUND", "文件不存在或不可读取", 404
            ) from exc
        if (
            stat.S_ISLNK(metadata.st_mode)
            or not (stat.S_ISREG(metadata.st_mode) or stat.S_ISDIR(metadata.st_mode))
            or not resolved.is_relative_to(project_root)
        ):
            raise DomainError(
                "FLOW_RUN_WORKSPACE_PATH_INVALID",
                "只能删除当前工作区中的普通文件或目录",
                422,
            )
        candidates.append((path, resolved))
    selected = [
        item
        for item in candidates
        if not any(item[0].startswith(parent[0] + "/") for parent in candidates if parent != item)
    ]
    for _, candidate in selected:
        try:
            if candidate.is_dir():
                shutil.rmtree(candidate)
            else:
                candidate.unlink()
        except OSError as exc:
            raise DomainError(
                "FLOW_RUN_WORKSPACE_DELETE_FAILED", "删除工作区文件失败", 503
            ) from exc
    return [path for path, _ in selected]


def read_candidate_output_file(
    db: Session,
    *,
    flow_run_id: str,
    attempt_id: str,
    field_key: str,
    path: str,
) -> tuple[bytes, str, str]:
    """Read one unaccepted FILE candidate without exposing a workspace path.

    The caller supplies a declared output slot and its relative submission.
    Both are revalidated against the Attempt before the server resolves the
    actual persistent location.  This endpoint deliberately does not create
    an ArtifactVersion or alter any gate/transition state.
    """

    attempt = db.get(NodeAttempt, attempt_id)
    target = (attempt.output_targets_json or {}).get(field_key) if attempt else None
    if not isinstance(target, dict) or target.get("artifact_type") != "FILE":
        raise DomainError("RUNTIME_OUTPUT_INVALID", "输出字段不是文件候选", 422)
    raw_path = path.strip()
    relative = PurePosixPath(raw_path)
    if (
        relative.is_absolute()
        or not relative.parts
        or relative.as_posix() != raw_path
        or any(part in {"", ".", ".."} for part in relative.parts)
    ):
        raise DomainError("RUNTIME_OUTPUT_INVALID", "候选文件路径无效", 422)
    # The Attempt-local directory is server-side provenance only. FlowRun
    # Agents write their candidate files in the shared project mount, so use
    # the same fully authorized root as the node workspace drawer.
    project_root = _authorize_entry(db, flow_run_id=flow_run_id, attempt_id=attempt_id)
    try:
        root_metadata = project_root.lstat()
        candidate = project_root.joinpath(*relative.parts)
        metadata = candidate.lstat()
        resolved = candidate.resolve(strict=True)
    except OSError as exc:
        raise DomainError("RUNTIME_OUTPUT_FILE_NOT_FOUND", "候选文件不存在或不可读取", 404) from exc
    if (
        stat.S_ISLNK(root_metadata.st_mode)
        or not stat.S_ISDIR(root_metadata.st_mode)
        or stat.S_ISLNK(metadata.st_mode)
        or not stat.S_ISREG(metadata.st_mode)
        or not resolved.is_relative_to(project_root)
    ):
        raise DomainError("RUNTIME_OUTPUT_INVALID", "候选文件不在受管节点工作区", 422)
    try:
        content = resolved.read_bytes()
    except OSError as exc:
        raise DomainError("RUNTIME_OUTPUT_FILE_NOT_FOUND", "候选文件不存在或不可读取", 404) from exc
    if len(content) > _MAX_FILE_BYTES:
        raise DomainError("RUNTIME_OUTPUT_FILE_TOO_LARGE", "候选文件超过预览大小限制", 422)
    content_type = mimetypes.guess_type(resolved.name)[0] or "application/octet-stream"
    return content, content_type, resolved.name


def conversation_working_directory(
    db: Session, *, flow_run_id: str, attempt_id: str, binding_id: str
) -> str:
    project_root = _authorize_entry(db, flow_run_id=flow_run_id, attempt_id=attempt_id)
    # A node Attempt grants access to a FlowRun session, but must never move
    # the interactive terminal out of the shared Agent project. Existing
    # bindings can contain the old nested Attempt path; ignore it here.
    del binding_id
    _validate_scope_roots(project_root, (str(_RUNTIME_PROJECT),))
    return str(_RUNTIME_PROJECT)


__all__ = (
    "conversation_working_directory",
    "details",
    "read_candidate_output_file",
    "read_file",
)
