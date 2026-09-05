from __future__ import annotations

import mimetypes
import os
import re
import shutil
import stat
import subprocess
from hashlib import sha256
from pathlib import Path, PurePosixPath
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from flowweave.modules.agent_sessions import public as agent_sessions
from flowweave.modules.agent_workspaces.application import service, work_directories
from flowweave.modules.agent_workspaces.infrastructure.models import (
    AgentConversationBinding,
    AgentConversationMessageAttachment,
    AgentWorkDirectory,
    AgentWorkDirectoryPath,
    AgentWorkDirectoryVersion,
    AgentWorkspace,
)
from flowweave.modules.users.application.security import (
    agent_workspace_runtime_root,
    current_user_id,
    user_runtime_project_root,
)
from flowweave.runtime.base import RuntimeWorkspaceFile
from flowweave.shared.errors import DomainError, not_found

_MAX_INDEX_ENTRIES = 20_000
_MAX_FILE_BYTES = 25 * 1024 * 1024
_GIT_TIMEOUT_SECONDS = 2


def terminal_session_name(workspace_id: str, container_id: str, terminal_instance_id: str) -> str:
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


def _project_root(db: Session, workspace_id: str) -> Path:
    """Resolve the platform-owned persistent project root without using Runtime."""
    project_root = service.agent_workspace_record_path(db, workspace_id)
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
        or resolved != project_root.resolve()
    ):
        raise DomainError(
            "AGENT_WORKSPACE_STORAGE_INVALID",
            "工作区持久化目录无效",
            409,
        )
    users_root = resolved / "users"
    user_root = users_root / current_user_id()
    try:
        users_root.mkdir(mode=0o700, exist_ok=True)
        user_root.mkdir(mode=0o700, exist_ok=True)
        users_metadata = users_root.lstat()
        user_metadata = user_root.lstat()
        resolved_user_root = user_root.resolve(strict=True)
    except OSError as exc:
        raise DomainError(
            "AGENT_WORKSPACE_STORAGE_UNAVAILABLE",
            "用户工作区持久化目录不可用",
            503,
        ) from exc
    if (
        stat.S_ISLNK(users_metadata.st_mode)
        or stat.S_ISLNK(user_metadata.st_mode)
        or not stat.S_ISDIR(users_metadata.st_mode)
        or not stat.S_ISDIR(user_metadata.st_mode)
        or not resolved_user_root.is_relative_to(resolved)
    ):
        raise DomainError(
            "AGENT_WORKSPACE_STORAGE_INVALID",
            "用户工作区持久化目录无效",
            409,
        )
    return resolved


def _runtime_root(workspace_id: str) -> str:
    return agent_workspace_runtime_root(workspace_id)


def _host_path(
    project_root: Path, runtime_root: str, runtime_path: str, *, require_file: bool
) -> Path:
    parsed = PurePosixPath(runtime_path)
    if (
        not runtime_path.startswith(runtime_root + "/")
        or parsed.as_posix() != runtime_path
        or ".." in parsed.parts
        or any(part.startswith(".") for part in parsed.parts)
    ):
        raise DomainError("AGENT_WORKSPACE_PATH_INVALID", "文件路径不在工作区范围内", 422)
    relative = parsed.relative_to(PurePosixPath(runtime_root))
    candidate = project_root.joinpath(*relative.parts)
    try:
        metadata = candidate.lstat()
        resolved = candidate.resolve(strict=True)
    except (FileNotFoundError, OSError) as exc:
        raise DomainError("AGENT_WORKSPACE_FILE_NOT_FOUND", "文件不存在或不可读取", 404) from exc
    if (
        stat.S_ISLNK(metadata.st_mode)
        or not resolved.is_relative_to(project_root)
        or (require_file and not stat.S_ISREG(metadata.st_mode))
    ):
        raise DomainError("AGENT_WORKSPACE_PATH_INVALID", "文件路径不在工作区范围内", 422)
    return resolved


def _workspace_entries(
    project_root: Path, runtime_root: str, working_directory: str
) -> list[dict[str, Any]]:
    host_root = (
        project_root
        if working_directory == runtime_root
        else _host_path(project_root, runtime_root, working_directory, require_file=False)
    )
    if not host_root.is_dir():
        raise DomainError("AGENT_WORKSPACE_PATH_INVALID", "当前工作目录不存在", 409)
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
                    "path": f"{runtime_root}/{relative}",
                    "kind": kind,
                    "size": metadata.st_size if kind == "file" else 0,
                }
            )
            if len(entries) >= _MAX_INDEX_ENTRIES:
                return entries
    return entries


def _git_value(repository: Path, *arguments: str) -> str | None:
    """Read bounded, local Git metadata without invoking a shell."""

    try:
        result = subprocess.run(
            ["git", "-C", str(repository), *arguments],
            capture_output=True,
            check=False,
            env={"PATH": os.defpath},
            text=True,
            timeout=_GIT_TIMEOUT_SECONDS,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    value = result.stdout.strip()
    return value if result.returncode == 0 and value else None


def _repository_details(repository: Path, runtime_path: str) -> dict[str, str]:
    details = {"path": runtime_path}
    branch = _git_value(repository, "branch", "--show-current")
    head = _git_value(repository, "rev-parse", "HEAD")
    remote = _git_value(repository, "remote", "get-url", "origin")
    if branch:
        details["branch"] = branch
    if head:
        details["head"] = head
    if remote:
        details["remote"] = remote
    return details


def _scope_repositories(
    project_root: Path, runtime_workspace_root: str, file_roots: tuple[str, ...]
) -> list[tuple[Path, str]]:
    """Find repositories intersecting the visible scope, including an owning parent repo."""

    found: dict[Path, str] = {}
    for runtime_root in file_roots:
        host_root = (
            project_root
            if runtime_root == runtime_workspace_root
            else _host_path(project_root, runtime_workspace_root, runtime_root, require_file=False)
        )
        current = host_root
        while current.is_relative_to(project_root):
            if (current / ".git").is_dir():
                relative = current.relative_to(project_root).as_posix()
                found[current] = (
                    runtime_workspace_root
                    if relative == "."
                    else f"{runtime_workspace_root}/{relative}"
                )
                break
            if current == project_root:
                break
            current = current.parent
        for directory, directory_names, _ in os.walk(host_root, followlinks=False):
            current_path = Path(directory)
            if ".git" in directory_names:
                relative = current_path.relative_to(project_root).as_posix()
                found[current_path] = (
                    runtime_workspace_root
                    if relative == "."
                    else f"{runtime_workspace_root}/{relative}"
                )
            directory_names[:] = [
                name
                for name in directory_names
                if not name.startswith(".") and not (current_path / name).is_symlink()
            ]
            if len(found) >= 100:
                break
    return sorted(found.items(), key=lambda item: item[1])


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
        return binding.working_directory or user_runtime_project_root(workspace_id), None
    if not work_directory_id:
        return user_runtime_project_root(workspace_id), None
    directory = work_directories.get_work_directory(db, workspace_id, work_directory_id)
    # A browser draft contains only an ID. Revalidate the active directory and
    # its real project-tree path before exposing it to file or terminal APIs.
    _, working_directory = work_directories.conversation_context(
        db, workspace_id, work_directory_id
    )
    return working_directory, directory


def _scope_details(
    db: Session,
    workspace_id: str,
    work_directory_id: str | None,
    binding_id: str | None,
    directory: dict[str, Any] | None,
) -> dict[str, str]:
    """Return the server-owned product scope without inferring it from a path."""

    if directory is not None:
        return {
            "kind": "WORK_DIRECTORY",
            "id": str(directory["id"]),
            "display_name": str(directory["display_name"]),
        }
    if binding_id:
        selected = db.execute(
            select(AgentWorkDirectory.id, AgentWorkDirectory.display_name)
            .join(
                AgentWorkDirectoryVersion,
                AgentWorkDirectoryVersion.work_directory_id == AgentWorkDirectory.id,
            )
            .join(
                AgentConversationBinding,
                AgentConversationBinding.work_directory_version_id == AgentWorkDirectoryVersion.id,
            )
            .where(
                AgentConversationBinding.id == binding_id,
                AgentConversationBinding.workspace_id == workspace_id,
            )
        ).one_or_none()
        if selected is not None:
            return {
                "kind": "WORK_DIRECTORY",
                "id": selected.id,
                "display_name": selected.display_name,
            }
    if work_directory_id:
        # `_working_directory` already rejects missing or archived IDs. This is
        # defensive only and must not invent a name from the filesystem path.
        raise DomainError("AGENT_WORK_DIRECTORY_NOT_FOUND", "工作目录不存在", 404)
    return {"kind": "ROOT", "display_name": "根工作区"}


def _file_scope_roots(
    db: Session,
    workspace_id: str,
    work_directory_id: str | None,
    binding_id: str | None,
    directory: dict[str, Any] | None,
) -> tuple[str, ...]:
    """Resolve file roots independently from OpenHands' single working directory."""

    version_id: str | None = None
    if directory is not None:
        version_id = str(directory["current_version"]["id"])
    elif binding_id:
        version_id = db.scalar(
            select(AgentConversationBinding.work_directory_version_id).where(
                AgentConversationBinding.id == binding_id,
                AgentConversationBinding.workspace_id == workspace_id,
                AgentConversationBinding.lifecycle == "ACTIVE",
            )
        )
    elif work_directory_id:
        raise DomainError("AGENT_WORK_DIRECTORY_NOT_FOUND", "工作目录不存在", 404)
    user_root = user_runtime_project_root(workspace_id)
    if version_id is None:
        return (user_root,)
    relative_paths = tuple(
        db.scalars(
            select(AgentWorkDirectoryPath.relative_path)
            .where(AgentWorkDirectoryPath.version_id == version_id)
            .order_by(AgentWorkDirectoryPath.position)
        )
    )
    if not relative_paths:
        raise DomainError("AGENT_WORK_DIRECTORY_VERSION_MISSING", "工作目录版本数据不完整", 409)
    return tuple(f"{user_root}/{path}" for path in relative_paths)


def _scoped_workspace_entries(
    project_root: Path,
    runtime_root: str,
    working_directory: str,
    file_roots: tuple[str, ...],
) -> list[dict[str, Any]]:
    if len(file_roots) == 1 and file_roots[0] == working_directory:
        return _workspace_entries(project_root, runtime_root, working_directory)
    entries: list[dict[str, Any]] = []
    for root in file_roots:
        entries.append({"path": root, "kind": "directory", "size": 0})
        entries.extend(_workspace_entries(project_root, runtime_root, root))
        if len(entries) >= _MAX_INDEX_ENTRIES:
            break
    return entries[:_MAX_INDEX_ENTRIES]


def _bound_attachment_entries(
    db: Session, binding_id: str | None, project_root: Path, runtime_root: str
) -> list[dict[str, Any]]:
    """Return only files explicitly attached to the requested conversation.

    Attachments live under the shared upload directory, which may be outside a
    frozen work-directory scope.  Their database binding is the additional
    authorization boundary; this must never become a general uploads allowlist.
    """

    if binding_id is None:
        return []
    attachments = db.scalars(
        select(AgentConversationMessageAttachment).where(
            AgentConversationMessageAttachment.binding_id == binding_id
        )
    ).all()
    entries: list[dict[str, Any]] = []
    for attachment in attachments:
        try:
            host_path = _host_path(project_root, runtime_root, attachment.path, require_file=True)
            size = host_path.stat().st_size
        except (DomainError, OSError):
            continue
        entries.append({"path": attachment.path, "kind": "file", "size": size})
    return entries


def _is_bound_attachment(db: Session, binding_id: str | None, path: str) -> bool:
    if binding_id is None:
        return False
    return (
        db.scalar(
            select(AgentConversationMessageAttachment.id).where(
                AgentConversationMessageAttachment.binding_id == binding_id,
                AgentConversationMessageAttachment.path == path,
            )
        )
        is not None
    )


def _subtree_has_bound_attachment(db: Session, workspace_id: str, path: str) -> bool:
    """Keep every conversation attachment out of recursive removal.

    Workspace files are shared between conversations.  Checking only the
    currently displayed binding would allow one conversation to delete a file
    still referenced by another, so the protection deliberately spans all
    bindings in this workspace.
    """

    return (
        db.scalar(
            select(AgentConversationMessageAttachment.id)
            .join(
                AgentConversationBinding,
                AgentConversationBinding.id == AgentConversationMessageAttachment.binding_id,
            )
            .where(
                AgentConversationBinding.workspace_id == workspace_id,
                (AgentConversationMessageAttachment.path == path)
                | AgentConversationMessageAttachment.path.startswith(path.rstrip("/") + "/"),
            )
        )
        is not None
    )


_PRIVATE_ATTACHMENT_PATH = re.compile(
    r"^/runtime/workspace/(?:project|[0-9a-f-]{36})/uploads/"
    r"(?P<owner>[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})-"
    r"[0-9a-f]{32}(?:--[A-Za-z0-9][A-Za-z0-9._-]{0,180})?$"
)


def _attachment_media_metadata(
    db: Session, binding_id: str | None, path: str
) -> tuple[str, str] | None:
    """Return trusted display metadata for a conversation-private upload.

    Historical uploads used opaque paths with no suffix.  Their persisted
    attachment projection is therefore the authoritative MIME type and
    download filename; filesystem guessing is only appropriate for normal
    workspace files.
    """

    if binding_id is None:
        return None
    attachment = db.scalar(
        select(AgentConversationMessageAttachment)
        .where(
            AgentConversationMessageAttachment.binding_id == binding_id,
            AgentConversationMessageAttachment.path == path,
        )
        .order_by(AgentConversationMessageAttachment.created_at.desc())
    )
    if attachment is None:
        return None
    return attachment.filename, attachment.mime_type


def delete_bound_attachment_files(db: Session, workspace_id: str, binding_id: str) -> None:
    """Best-effort cleanup of a deleted conversation's private attachments."""

    paths = db.scalars(
        select(AgentConversationMessageAttachment.path)
        .where(AgentConversationMessageAttachment.binding_id == binding_id)
        .distinct()
    ).all()
    project_root = _project_root(db, workspace_id)
    runtime_root = _runtime_root(workspace_id)
    private_paths = set(paths)
    uploads = project_root / "uploads"
    try:
        private_paths.update(
            str(PurePosixPath(runtime_root) / "uploads" / candidate.name)
            for candidate in uploads.glob(f"{binding_id}-*")
            if candidate.is_file()
        )
    except OSError:
        pass
    for path in private_paths:
        matched = _PRIVATE_ATTACHMENT_PATH.fullmatch(path)
        if matched is None or matched.group("owner") != binding_id:
            continue
        # Do not remove an object if an unexpected historical projection shares it.
        referenced_elsewhere = db.scalar(
            select(AgentConversationMessageAttachment.id).where(
                AgentConversationMessageAttachment.binding_id != binding_id,
                AgentConversationMessageAttachment.path == path,
            )
        )
        if referenced_elsewhere is not None:
            continue
        try:
            _host_path(project_root, runtime_root, path, require_file=True).unlink()
        except (DomainError, OSError):
            # The conversation deletion has already succeeded upstream. Missing
            # files and transient storage cleanup failures must not resurrect it.
            continue
    db.flush()


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
    session_service = agent_sessions.conversations
    resource_name, resource_id, container_id = session_service.terminal_container_details(
        db, workspace_id
    )
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
    scope = _scope_details(db, workspace_id, work_directory_id, binding_id, directory)
    file_roots = _file_scope_roots(db, workspace_id, work_directory_id, binding_id, directory)
    project_root = _project_root(db, workspace_id)
    runtime_root = _runtime_root(workspace_id)
    repositories = [
        _repository_details(host_repository, runtime_path)
        for host_repository, runtime_path in _scope_repositories(
            project_root, runtime_root, file_roots
        )
    ]
    try:
        session_service = agent_sessions.conversations
        _, _, container_id = session_service.terminal_container_details(db, workspace_id)
        container_short_id = container_id.removeprefix("sha256:")[:12]
    except DomainError:
        container_short_id = None
    return {
        "root": runtime_root,
        "scope": scope,
        "working_directory": working_directory,
        "work_directory": directory,
        "files": list(
            {
                entry["path"]: entry
                for entry in (
                    _scoped_workspace_entries(
                        project_root, runtime_root, working_directory, file_roots
                    )
                    + _bound_attachment_entries(db, binding_id, project_root, runtime_root)
                )
            }.values()
        ),
        "repositories": repositories,
        "runtime": {"container_id": container_short_id},
        "ide": {
            "workspace_path": working_directory,
            "gateway": agent_sessions.ssh_remote_descriptor(project_root, working_directory),
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
    runtime_root = _runtime_root(workspace_id)
    parsed = PurePosixPath(path)
    if (
        not path.startswith(runtime_root + "/")
        or parsed.as_posix() != path
        or ".." in parsed.parts
        or any(part in {".git", ".openhands"} for part in parsed.parts)
    ):
        raise DomainError("AGENT_WORKSPACE_PATH_INVALID", "文件路径不在工作区范围内", 422)
    _, directory = _working_directory(db, workspace_id, work_directory_id, binding_id)
    file_roots = _file_scope_roots(db, workspace_id, work_directory_id, binding_id, directory)
    in_workspace_scope = any(
        path == root or path.startswith(root.rstrip("/") + "/") for root in file_roots
    )
    if not in_workspace_scope and not _is_bound_attachment(db, binding_id, path):
        raise DomainError("AGENT_WORKSPACE_PATH_INVALID", "文件路径不在当前工作目录范围内", 422)
    host_path = _host_path(_project_root(db, workspace_id), runtime_root, path, require_file=True)
    try:
        size = host_path.stat().st_size
    except OSError as exc:
        raise DomainError("AGENT_WORKSPACE_FILE_UNAVAILABLE", "文件暂时无法读取", 503) from exc
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
        raise DomainError("AGENT_WORKSPACE_FILE_UNAVAILABLE", "文件暂时无法读取", 503) from exc
    attachment_metadata = _attachment_media_metadata(db, binding_id, path)
    filename, content_type = attachment_metadata or (
        host_path.name,
        mimetypes.guess_type(host_path.name)[0] or "application/octet-stream",
    )
    return RuntimeWorkspaceFile(
        filename=filename,
        content_type=content_type,
        content=content,
    )


def delete_entries(
    db: Session,
    workspace_id: str,
    paths: tuple[str, ...],
    binding_id: str | None = None,
    work_directory_id: str | None = None,
) -> list[str]:
    """Remove selected file/directory trees from the authorized scope."""
    if not paths:
        raise DomainError("AGENT_WORKSPACE_DELETE_EMPTY", "请选择要删除的文件或目录", 422)
    if len(set(paths)) > 100:
        raise DomainError("AGENT_WORKSPACE_DELETE_TOO_MANY", "一次最多删除 100 项", 422)
    _workspace(db, workspace_id)
    runtime_root = _runtime_root(workspace_id)
    _, directory = _working_directory(db, workspace_id, work_directory_id, binding_id)
    roots = _file_scope_roots(db, workspace_id, work_directory_id, binding_id, directory)
    project_root = _project_root(db, workspace_id)
    candidates: list[tuple[str, Path]] = []
    for path in sorted(set(paths), key=lambda value: (value.count("/"), value)):
        parsed = PurePosixPath(path)
        if (
            not path.startswith(runtime_root + "/")
            or parsed.as_posix() != path
            or ".." in parsed.parts
            or any(part in {".git", ".openhands"} or part.startswith(".") for part in parsed.parts)
            or path in roots
            or not any(path.startswith(root.rstrip("/") + "/") for root in roots)
            or _is_bound_attachment(db, binding_id, path)
        ):
            raise DomainError("AGENT_WORKSPACE_PATH_INVALID", "文件路径不在当前工作目录范围内", 422)
        candidate = _host_path(project_root, runtime_root, path, require_file=False)
        metadata = candidate.lstat()
        if stat.S_ISLNK(metadata.st_mode) or not (
            stat.S_ISREG(metadata.st_mode) or stat.S_ISDIR(metadata.st_mode)
        ):
            raise DomainError("AGENT_WORKSPACE_PATH_INVALID", "只能删除普通文件或目录", 422)
        candidates.append((path, candidate))
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
            raise DomainError("AGENT_WORKSPACE_DELETE_FAILED", "删除工作区文件失败", 503) from exc
    return [path for path, _ in selected]


def delete_entry(
    db: Session,
    workspace_id: str,
    path: str,
    binding_id: str | None = None,
    work_directory_id: str | None = None,
    *,
    recursive: bool = False,
) -> None:
    """Delete a user-visible ordinary file or a verified directory subtree."""
    _workspace(db, workspace_id)
    runtime_root = _runtime_root(workspace_id)
    parsed = PurePosixPath(path)
    if (
        path == runtime_root
        or not path.startswith(runtime_root + "/")
        or parsed.as_posix() != path
        or ".." in parsed.parts
        or any(part.startswith(".") for part in parsed.parts)
    ):
        raise DomainError("AGENT_WORKSPACE_PATH_INVALID", "文件路径不在工作区范围内", 422)
    _, directory = _working_directory(db, workspace_id, work_directory_id, binding_id)
    file_roots = _file_scope_roots(db, workspace_id, work_directory_id, binding_id, directory)
    if path in file_roots or not any(
        path == root or path.startswith(root.rstrip("/") + "/") for root in file_roots
    ):
        raise DomainError("AGENT_WORKSPACE_PATH_INVALID", "文件路径不在当前工作目录范围内", 422)
    if _subtree_has_bound_attachment(db, workspace_id, path):
        raise DomainError("AGENT_WORKSPACE_ATTACHMENT_PROTECTED", "会话附件不能从文件栏删除", 409)
    host_path = _host_path(_project_root(db, workspace_id), runtime_root, path, require_file=False)

    def validate_tree(target: Path) -> None:
        try:
            children = list(os.scandir(target))
        except OSError as exc:
            raise DomainError("AGENT_WORKSPACE_DELETE_FAILED", "目录暂时无法删除", 409) from exc
        for child in children:
            try:
                mode = child.stat(follow_symlinks=False).st_mode
            except OSError as exc:
                raise DomainError("AGENT_WORKSPACE_DELETE_FAILED", "目录暂时无法删除", 409) from exc
            if stat.S_ISLNK(mode) or not (stat.S_ISREG(mode) or stat.S_ISDIR(mode)):
                raise DomainError("AGENT_WORKSPACE_PATH_INVALID", "目录包含不能删除的特殊文件", 422)
            if stat.S_ISDIR(mode):
                validate_tree(Path(child.path))

    def remove_tree(target: Path) -> None:
        for child in os.scandir(target):
            child_path = Path(child.path)
            if child.is_dir(follow_symlinks=False):
                remove_tree(child_path)
            else:
                child_path.unlink()
        target.rmdir()

    try:
        mode = host_path.lstat().st_mode
        if stat.S_ISLNK(mode):
            raise DomainError("AGENT_WORKSPACE_PATH_INVALID", "不能删除符号链接", 422)
        if stat.S_ISREG(mode):
            host_path.unlink()
        elif stat.S_ISDIR(mode):
            if recursive:
                # Reject unsafe descendants before any entry is removed.  This
                # keeps a malformed tree fail-closed instead of deleting a
                # prefix and only then discovering a protected node.
                validate_tree(host_path)
                remove_tree(host_path)
            else:
                host_path.rmdir()
        else:
            raise DomainError("AGENT_WORKSPACE_PATH_INVALID", "只能删除普通文件或目录", 422)
    except OSError as exc:
        raise DomainError(
            "AGENT_WORKSPACE_DELETE_FAILED", "文件删除失败；目录必须为空或选择递归删除。", 409
        ) from exc


def create_entry(
    db: Session,
    workspace_id: str,
    parent_path: str,
    name: str,
    kind: str,
    binding_id: str | None = None,
    work_directory_id: str | None = None,
) -> None:
    """Create an empty ordinary file or directory in the authorized scope."""

    _workspace(db, workspace_id)
    runtime_root = _runtime_root(workspace_id)
    name = name.strip()
    if not name or name in {".", ".."} or name.startswith(".") or "/" in name or "\\" in name:
        raise DomainError("AGENT_WORKSPACE_ENTRY_NAME_INVALID", "名称必须是非隐藏的单级文件名", 422)
    if kind not in {"FILE", "DIRECTORY"}:
        raise DomainError("AGENT_WORKSPACE_ENTRY_KIND_INVALID", "只支持创建文件或目录", 422)
    parsed = PurePosixPath(parent_path)
    if parent_path != runtime_root and (
        not parent_path.startswith(runtime_root + "/")
        or parsed.as_posix() != parent_path
        or ".." in parsed.parts
        or any(part.startswith(".") for part in parsed.parts)
    ):
        raise DomainError("AGENT_WORKSPACE_PATH_INVALID", "父目录不在工作区范围内", 422)
    _, directory = _working_directory(db, workspace_id, work_directory_id, binding_id)
    file_roots = _file_scope_roots(db, workspace_id, work_directory_id, binding_id, directory)
    if not any(
        parent_path == root or parent_path.startswith(root.rstrip("/") + "/") for root in file_roots
    ):
        raise DomainError("AGENT_WORKSPACE_PATH_INVALID", "父目录不在当前工作目录范围内", 422)
    project_root = _project_root(db, workspace_id)
    parent = _host_path(project_root, runtime_root, parent_path, require_file=False)
    try:
        parent_mode = parent.lstat().st_mode
    except OSError as exc:
        raise DomainError("AGENT_WORKSPACE_FILE_NOT_FOUND", "父目录不存在", 404) from exc
    if stat.S_ISLNK(parent_mode) or not stat.S_ISDIR(parent_mode):
        raise DomainError("AGENT_WORKSPACE_PATH_INVALID", "父路径不是可用目录", 422)
    target = parent / name
    try:
        if kind == "DIRECTORY":
            target.mkdir(mode=0o700)
        else:
            descriptor = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            os.close(descriptor)
    except FileExistsError as exc:
        raise DomainError("AGENT_WORKSPACE_ENTRY_EXISTS", "同名文件或目录已存在", 409) from exc
    except OSError as exc:
        raise DomainError("AGENT_WORKSPACE_CREATE_FAILED", "文件或目录创建失败", 409) from exc
