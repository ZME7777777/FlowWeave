"""Stage exact user-owned paths, compensate rollback, and durably retry physical cleanup."""

import json
import logging
import os
import shutil
import stat
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path, PurePosixPath
from uuid import UUID, uuid4

from sqlalchemy.orm import Session

from flowweave.modules.tasks.public import enqueue
from flowweave.shared.application.transactions import (
    register_commit_action,
    register_rollback_action,
)
from flowweave.shared.errors import DomainError

_logger = logging.getLogger(__name__)
_TRASH = PurePosixPath(".flowweave/deleted-workspaces")


def unsafe() -> DomainError:
    return DomainError(
        "AGENT_WORK_DIRECTORY_DELETE_UNSAFE", "工作区目录不安全或已变更，拒绝删除", 409
    )


@contextmanager
def parent_fd(root: Path, path: PurePosixPath, *, create: bool = False) -> Iterator[int | None]:
    descriptor = None
    try:
        descriptor = os.open(root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        for part in path.parts[:-1]:
            if create:
                try:
                    os.mkdir(part, mode=0o700, dir_fd=descriptor)
                except FileExistsError:
                    pass
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
        raise unsafe() from exc
    try:
        yield descriptor
    finally:
        if descriptor is not None:
            os.close(descriptor)


def metadata(root: Path, path: PurePosixPath) -> os.stat_result | None:
    with parent_fd(root, path) as descriptor:
        if descriptor is None:
            return None
        try:
            value = os.stat(path.name, dir_fd=descriptor, follow_symlinks=False)
        except FileNotFoundError:
            return None
        if not (stat.S_ISDIR(value.st_mode) or stat.S_ISREG(value.st_mode)):
            raise unsafe()
        return value


def purge(root: Path, cleanup_id: str, device: int, inode: int) -> None:
    path = _TRASH / str(UUID(cleanup_id))
    with parent_fd(root, path) as descriptor:
        if descriptor is None:
            return
        try:
            value = os.stat(path.name, dir_fd=descriptor, follow_symlinks=False)
        except FileNotFoundError:
            return
        if not stat.S_ISDIR(value.st_mode) or (value.st_dev, value.st_ino) != (device, inode):
            raise unsafe()
        if not shutil.rmtree.avoids_symlink_attacks:
            raise unsafe()
        target = os.open(path.name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=descriptor)
        try:
            actual = os.fstat(target)
            if (actual.st_dev, actual.st_ino) != (device, inode):
                raise unsafe()
            for name in os.listdir(target):
                child = os.stat(name, dir_fd=target, follow_symlinks=False)
                if stat.S_ISDIR(child.st_mode):
                    shutil.rmtree(name, dir_fd=target)
                else:
                    os.unlink(name, dir_fd=target)
            current = os.stat(path.name, dir_fd=descriptor, follow_symlinks=False)
            if (current.st_dev, current.st_ino) != (device, inode):
                raise unsafe()
            os.rmdir(path.name, dir_fd=descriptor)
        finally:
            os.close(target)


def stage(db: Session, workspace_id: str, root: Path, paths: tuple[PurePosixPath, ...]) -> None:
    # All candidates are preflighted before the first rename; missing historical
    # directories are already clean. Ancestors subsume descendants.
    ordered = sorted(set(paths), key=lambda path: (len(path.parts), path.parts))
    candidates: list[tuple[PurePosixPath, os.stat_result]] = []
    for path in ordered:
        if path.is_absolute() or not path.parts or any(p in {".", ".."} for p in path.parts):
            raise unsafe()
        if path == _TRASH or _TRASH.is_relative_to(path) or path.is_relative_to(_TRASH):
            raise unsafe()
        if any(path.is_relative_to(parent) for parent, _ in candidates):
            continue
        value = metadata(root, path)
        if value is not None:
            candidates.append((path, value))
    if not candidates:
        return
    cleanup_id = str(uuid4())
    trash = _TRASH / cleanup_id
    with parent_fd(root, trash, create=True) as descriptor:
        if descriptor is None:
            raise unsafe()
        os.mkdir(trash.name, mode=0o700, dir_fd=descriptor)
    expected = metadata(root, trash)
    assert expected is not None
    # Preserve the original mapping before renames, including for an operator
    # recovering an API process killed before its database transaction settles.
    with parent_fd(root, trash / "manifest.json") as descriptor:
        if descriptor is None:
            raise unsafe()
        actual = os.fstat(descriptor)
        if (actual.st_dev, actual.st_ino) != (expected.st_dev, expected.st_ino):
            raise unsafe()
        output = os.open(
            "manifest.json",
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
            0o600,
            dir_fd=descriptor,
        )
        with os.fdopen(output, "w") as stream:
            json.dump(
                {"workspace_id": workspace_id, "entries": [str(path) for path, _ in candidates]},
                stream,
            )
            stream.flush()
            os.fsync(stream.fileno())
    moved: list[tuple[PurePosixPath, str]] = []

    def rollback() -> None:
        with parent_fd(root, trash / "entry") as source:
            if source is None:
                return
            for path, name in reversed(moved):
                with parent_fd(root, path) as target:
                    if target is None or metadata(root, path) is not None:
                        raise unsafe()
                    os.rename(name, path.name, src_dir_fd=source, dst_dir_fd=target)
        purge(root, cleanup_id, expected.st_dev, expected.st_ino)

    register_rollback_action(db, rollback)
    enqueue(
        db,
        task_type="CLEANUP_AGENT_WORK_DIRECTORY",
        aggregate_type="AGENT_WORK_DIRECTORY_CLEANUP",
        aggregate_id=cleanup_id,
        idempotency_key=f"cleanup-agent-work-directory:{cleanup_id}",
        payload={"workspace_id": workspace_id, "device": expected.st_dev, "inode": expected.st_ino},
    )
    with parent_fd(root, trash / "entry") as target:
        if target is None:
            raise unsafe()
        for index, (path, value) in enumerate(candidates):
            with parent_fd(root, path) as source:
                actual = (
                    os.stat(path.name, dir_fd=source, follow_symlinks=False)
                    if source is not None
                    else None
                )
                if (
                    source is None
                    or actual is None
                    or (actual.st_dev, actual.st_ino) != (value.st_dev, value.st_ino)
                ):
                    raise unsafe()
                name = str(index)
                os.rename(path.name, name, src_dir_fd=source, dst_dir_fd=target)
                moved.append((path, name))

    def committed() -> None:
        try:
            purge(root, cleanup_id, expected.st_dev, expected.st_ino)
        except (OSError, DomainError):
            # The exact private quarantine is no longer user-visible. A durable
            # maintenance task retries it without ever resolving original paths.
            _logger.warning("Work-directory file cleanup pending retry")

    register_commit_action(db, committed)


def cleanup(db: Session, cleanup_id: str, payload: dict[str, object]) -> None:
    from flowweave.modules.agent_workspaces.application import workspace

    workspace_id = payload.get("workspace_id")
    device, inode = payload.get("device"), payload.get("inode")
    if not isinstance(workspace_id, str) or type(device) is not int or type(inode) is not int:
        raise unsafe()
    root = workspace.ensure_user_project_root(db, workspace_id)
    purge(root, cleanup_id, device, inode)
