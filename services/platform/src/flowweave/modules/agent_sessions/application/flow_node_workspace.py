"""Read-only node-Attempt workspace projection for shared Agent sessions.

FlowRun Runtime allocations are host paths, whereas OpenHands and the browser
address the mounted project as ``/runtime/workspace/project``.  This adapter
proves that mapping from the server-owned Attempt record and never accepts a
host path from the caller.
"""

from __future__ import annotations

import mimetypes
import os
import stat
from pathlib import Path, PurePosixPath
from typing import Any

from sqlalchemy.orm import Session

from flowweave.modules.agent_sessions.application import flow_node_conversations
from flowweave.modules.agent_sessions.application.flow_node_host import (
    resolve_flow_node_session_host,
)
from flowweave.modules.sandboxes import public as sandboxes
from flowweave.shared.errors import DomainError
from flowweave.shared.models import NodeAttempt

_RUNTIME_PROJECT = PurePosixPath("/runtime/workspace/project")
_MAX_INDEX_ENTRIES = 20_000
_MAX_FILE_BYTES = 25 * 1024 * 1024


def _attempt_workspace(
    db: Session, *, flow_run_id: str, attempt_id: str, binding_id: str | None
) -> tuple[NodeAttempt, Path, Path, PurePosixPath]:
    """Resolve the immutable Attempt directory and its Runtime-relative path."""

    host = resolve_flow_node_session_host(
        db,
        flow_run_id=flow_run_id,
        attempt_id=attempt_id,
        require_start_permission=False,
    )
    if binding_id is not None:
        flow_node_conversations.node_conversation_binding(
            db,
            flow_run_id=flow_run_id,
            attempt_id=attempt_id,
            binding_id=binding_id,
        )
    attempt = db.get(NodeAttempt, host.attempt_id)
    if attempt is None or not attempt.workspace_ref:
        raise DomainError(
            "NODE_WORKSPACE_REQUIRED", "The node Attempt has no isolated workspace", 409
        )
    project_root = sandboxes.flow_run_workspace_project_path(flow_run_id)
    try:
        root_metadata = project_root.lstat()
        attempt_root = Path(attempt.workspace_ref)
        attempt_metadata = attempt_root.lstat()
        resolved_project = project_root.resolve(strict=True)
        resolved_attempt = attempt_root.resolve(strict=True)
    except OSError as exc:
        raise DomainError(
            "NODE_WORKSPACE_UNAVAILABLE", "The node workspace is unavailable", 503
        ) from exc
    if (
        stat.S_ISLNK(root_metadata.st_mode)
        or not stat.S_ISDIR(root_metadata.st_mode)
        or stat.S_ISLNK(attempt_metadata.st_mode)
        or not stat.S_ISDIR(attempt_metadata.st_mode)
        or not resolved_attempt.is_relative_to(resolved_project)
    ):
        raise DomainError(
            "NODE_WORKSPACE_INVALID", "The node workspace is outside its FlowRun allocation", 409
        )
    relative = resolved_attempt.relative_to(resolved_project)
    if not relative.parts or any(part in {"", ".", ".."} for part in relative.parts):
        raise DomainError("NODE_WORKSPACE_INVALID", "The node workspace layout is invalid", 409)
    return attempt, resolved_project, resolved_attempt, PurePosixPath(*relative.parts)


def _runtime_path(relative: PurePosixPath) -> str:
    return str(_RUNTIME_PROJECT.joinpath(relative))


def _host_file(*, project_root: Path, attempt_root: Path, path: str, require_file: bool) -> Path:
    candidate_path = PurePosixPath(path)
    if (
        not candidate_path.is_absolute()
        or not candidate_path.is_relative_to(_RUNTIME_PROJECT)
        or candidate_path.as_posix() != path
        or any(part in {"", ".", ".."} or part.startswith(".") for part in candidate_path.parts)
    ):
        raise DomainError("NODE_WORKSPACE_PATH_INVALID", "文件路径不在节点工作目录范围内", 422)
    relative = candidate_path.relative_to(_RUNTIME_PROJECT)
    candidate = project_root.joinpath(*relative.parts)
    try:
        metadata = candidate.lstat()
        resolved = candidate.resolve(strict=True)
    except OSError as exc:
        raise DomainError("NODE_WORKSPACE_FILE_NOT_FOUND", "文件不存在或不可读取", 404) from exc
    if (
        stat.S_ISLNK(metadata.st_mode)
        or not resolved.is_relative_to(attempt_root)
        or (require_file and not stat.S_ISREG(metadata.st_mode))
    ):
        raise DomainError("NODE_WORKSPACE_PATH_INVALID", "文件路径不在节点工作目录范围内", 422)
    return resolved


def _entries(
    project_root: Path, attempt_root: Path, relative_root: PurePosixPath
) -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = []
    for current, directories, files in os.walk(attempt_root, followlinks=False):
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
            relative = candidate.relative_to(project_root)
            items.append(
                {
                    "path": _runtime_path(PurePosixPath(*relative.parts)),
                    "kind": kind,
                    "size": metadata.st_size if kind == "file" else 0,
                }
            )
            if len(items) >= _MAX_INDEX_ENTRIES:
                return items
    return items


def details(
    db: Session, *, flow_run_id: str, attempt_id: str, binding_id: str | None = None
) -> dict[str, Any]:
    """Project exactly one frozen node Attempt directory for browser tools."""

    attempt, project_root, attempt_root, relative = _attempt_workspace(
        db, flow_run_id=flow_run_id, attempt_id=attempt_id, binding_id=binding_id
    )
    working_directory = _runtime_path(relative)
    return {
        "root": str(_RUNTIME_PROJECT),
        "scope": {
            "kind": "WORK_DIRECTORY",
            "id": attempt.id,
            "display_name": "节点工作目录",
        },
        "working_directory": working_directory,
        "work_directory": None,
        "files": _entries(project_root, attempt_root, relative),
        "repositories": [],
        "runtime": {"container_id": None},
        "ide": {
            "workspace_path": working_directory,
            "gateway": {
                "supported": False,
                "status": "不可用",
                "note": "节点会话仅提供已冻结目录内的文件与终端。",
            },
        },
    }


def read_file(
    db: Session, *, flow_run_id: str, attempt_id: str, binding_id: str | None, path: str
) -> tuple[bytes, str, str]:
    """Return one bounded regular file from the selected Attempt workspace."""

    _, project_root, attempt_root, _ = _attempt_workspace(
        db, flow_run_id=flow_run_id, attempt_id=attempt_id, binding_id=binding_id
    )
    candidate = _host_file(
        project_root=project_root, attempt_root=attempt_root, path=path, require_file=True
    )
    try:
        content = candidate.read_bytes()
    except OSError as exc:
        raise DomainError("NODE_WORKSPACE_FILE_NOT_FOUND", "文件不存在或不可读取", 404) from exc
    if len(content) > _MAX_FILE_BYTES:
        raise DomainError("NODE_WORKSPACE_FILE_TOO_LARGE", "文件超过预览大小限制", 422)
    return (
        content,
        mimetypes.guess_type(candidate.name)[0] or "application/octet-stream",
        candidate.name,
    )


__all__ = ("details", "read_file")
