from __future__ import annotations

import fcntl
import hashlib
import os
import re
import secrets
import shutil
import stat
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session

from flowweave.modules.sandboxes.application.runtime_owner import runtime_owner_flow_run_id
from flowweave.modules.sandboxes.infrastructure.models import (
    FlowRunRuntimeAllocation,
    FlowRunRuntimeSecretReference,
    ManagedSandbox,
)
from flowweave.shared.application.transactions import (
    register_commit_action,
    register_rollback_action,
)
from flowweave.shared.credentials_crypto import decrypt_secret, encrypt_secret
from flowweave.shared.database import uid
from flowweave.shared.errors import DomainError
from flowweave.shared.settings import get_settings

_ROOT_NAME = ".flow-run-runtimes"
_MARKER_NAME = ".flowweave-allocation"
_LOCK_NAME = ".capability-materialization.lock"
_DIRECTORY_MODES = {
    PurePosixPath("workspace"): 0o700,
    PurePosixPath("workspace/project"): 0o700,
    PurePosixPath("state"): 0o700,
    PurePosixPath("state/conversations"): 0o700,
    PurePosixPath("state/bash-events"): 0o700,
    PurePosixPath("state/persistence"): 0o700,
    # The control plane must be able to create and remove immutable digest
    # bundles on rootless/bind-mounted filesystems.  Runtime read-only access
    # is enforced by the Docker mount contract; each completed bundle is still
    # frozen independently before it is exposed.
    PurePosixPath("capabilities"): 0o700,
}


@dataclass(frozen=True, slots=True)
class RuntimeStorageAllocation:
    id: str
    flow_run_id: str
    secret_reference_id: str
    relative_root: str


def _canonical_uuid(value: str, *, field: str) -> str:
    try:
        canonical = str(UUID(value))
    except ValueError as exc:
        raise DomainError(
            "RUNTIME_ALLOCATION_INVALID",
            f"The {field} identity is invalid",
            422,
        ) from exc
    if canonical != value.lower():
        raise DomainError(
            "RUNTIME_ALLOCATION_INVALID",
            f"The {field} identity is not canonical",
            422,
        )
    return canonical


def _relative_root(flow_run_id: str) -> PurePosixPath:
    run_id = _canonical_uuid(flow_run_id, field="FlowRun")
    scope = get_settings().sandbox_manager_scope
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,127}", scope):
        raise DomainError(
            "RUNTIME_ALLOCATION_UNAVAILABLE",
            "A stable Runtime manager scope is required for FlowRun storage",
            503,
        )
    scope_digest = hashlib.sha256(scope.encode("utf-8")).hexdigest()[:32]
    return PurePosixPath(_ROOT_NAME, scope_digest, run_id)


def _workspace_root() -> Path:
    configured = Path(get_settings().workspace_root).absolute()
    if configured.is_symlink() or not configured.is_dir():
        raise DomainError(
            "RUNTIME_ALLOCATION_UNAVAILABLE",
            "The managed workspace root must be an existing plain directory",
            503,
        )
    return configured.resolve()


def _host_root(relative_root: str | PurePosixPath) -> Path:
    relative = PurePosixPath(relative_root)
    expected_prefix = PurePosixPath(_ROOT_NAME)
    if (
        relative.is_absolute()
        or not relative.is_relative_to(expected_prefix)
        or len(relative.parts) != 3
        or any(part in {"", ".", ".."} for part in relative.parts)
    ):
        raise DomainError(
            "RUNTIME_ALLOCATION_INVALID",
            "The Runtime allocation root is invalid",
            409,
        )
    workspace_root = _workspace_root()
    target = workspace_root.joinpath(*relative.parts)
    if target == workspace_root or not target.is_relative_to(workspace_root):
        raise DomainError(
            "RUNTIME_ALLOCATION_INVALID",
            "The Runtime allocation escaped its managed root",
            409,
        )
    return target


def flow_run_workspace_project_path(flow_run_id: str) -> Path:
    """Return the server-derived project directory for one FlowRun."""

    return _host_root(_relative_root(flow_run_id)) / "workspace" / "project"


def flow_run_workspace_nodes_path(flow_run_id: str) -> Path:
    """Return the sibling data tree for one FlowRun's node Attempts."""

    return _host_root(_relative_root(flow_run_id)) / "workspace" / "nodes"


def flow_run_capability_path(flow_run_id: str, manifest_digest: str, *relative_parts: str) -> Path:
    """Return a digest-scoped capability path after validating every segment."""

    if not re.fullmatch(r"[0-9a-f]{64}", manifest_digest):
        raise DomainError(
            "RUNTIME_CAPABILITY_UNAVAILABLE",
            "The Snapshot Runtime Manifest digest is invalid",
            409,
        )
    relative = PurePosixPath(*relative_parts)
    if relative.is_absolute() or any(part in {"", ".", ".."} for part in relative.parts):
        raise DomainError(
            "RUNTIME_CAPABILITY_UNAVAILABLE",
            "The FlowRun capability path is invalid",
            409,
        )
    return (
        _host_root(_relative_root(flow_run_id))
        / "capabilities"
        / manifest_digest
        / Path(*relative.parts)
    )


def openhands_flow_run_project_path() -> PurePosixPath:
    return PurePosixPath("/runtime/workspace/project")


def openhands_flow_run_nodes_path() -> PurePosixPath:
    return PurePosixPath("/runtime/workspace/nodes")


def openhands_flow_run_capability_path(manifest_digest: str, *relative_parts: str) -> PurePosixPath:
    if not re.fullmatch(r"[0-9a-f]{64}", manifest_digest):
        raise DomainError(
            "RUNTIME_CAPABILITY_UNAVAILABLE",
            "The Snapshot Runtime Manifest digest is invalid",
            409,
        )
    relative = PurePosixPath(*relative_parts)
    if relative.is_absolute() or any(part in {"", ".", ".."} for part in relative.parts):
        raise DomainError(
            "RUNTIME_CAPABILITY_UNAVAILABLE",
            "The FlowRun capability path is invalid",
            409,
        )
    return PurePosixPath("/runtime/capabilities", manifest_digest, *relative.parts)


def _ensure_parent(path: Path, *, managed_root: Path) -> None:
    if path == managed_root:
        return
    _ensure_parent(path.parent, managed_root=managed_root)
    try:
        metadata = path.lstat()
    except FileNotFoundError:
        path.mkdir(mode=0o700)
        metadata = path.lstat()
    if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISDIR(metadata.st_mode):
        raise DomainError(
            "RUNTIME_ALLOCATION_CONFLICT",
            "A Runtime allocation parent is not a plain directory",
            409,
        )
    if stat.S_IMODE(metadata.st_mode) & 0o022:
        raise DomainError(
            "RUNTIME_ALLOCATION_PERMISSIONS_INVALID",
            "A Runtime allocation parent is writable by another identity",
            409,
        )


def _write_marker(root: Path, allocation_id: str) -> None:
    marker = root / _MARKER_NAME
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    descriptor = os.open(marker, flags, 0o400)
    try:
        os.write(descriptor, allocation_id.encode("ascii"))
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _create_lock_file(root: Path) -> None:
    path = root / _LOCK_NAME
    flags = os.O_RDWR | os.O_CREAT | os.O_EXCL
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    descriptor = os.open(path, flags, 0o600)
    os.close(descriptor)


def _verify_layout(allocation: FlowRunRuntimeAllocation) -> Path:
    expected = _relative_root(allocation.flow_run_id).as_posix()
    if allocation.relative_root != expected:
        raise DomainError(
            "RUNTIME_ALLOCATION_CONFLICT",
            "The Runtime allocation path does not match its FlowRun",
            409,
        )
    root = _host_root(allocation.relative_root)
    try:
        root_metadata = root.lstat()
        marker_metadata = (root / _MARKER_NAME).lstat()
        lock_metadata = (root / _LOCK_NAME).lstat()
        marker_value = (root / _MARKER_NAME).read_text(encoding="ascii")
    except (FileNotFoundError, OSError, UnicodeDecodeError) as exc:
        raise DomainError(
            "RUNTIME_ALLOCATION_MISSING",
            "The FlowRun Runtime allocation is incomplete",
            409,
        ) from exc
    if (
        stat.S_ISLNK(root_metadata.st_mode)
        or not stat.S_ISDIR(root_metadata.st_mode)
        or stat.S_IMODE(root_metadata.st_mode) != 0o700
        or not stat.S_ISREG(marker_metadata.st_mode)
        or stat.S_IMODE(marker_metadata.st_mode) != 0o400
        or marker_metadata.st_uid != root_metadata.st_uid
        or marker_metadata.st_gid != root_metadata.st_gid
        or not stat.S_ISREG(lock_metadata.st_mode)
        or stat.S_IMODE(lock_metadata.st_mode) != 0o600
        or lock_metadata.st_uid != root_metadata.st_uid
        or lock_metadata.st_gid != root_metadata.st_gid
        or marker_value != allocation.id
    ):
        raise DomainError(
            "RUNTIME_ALLOCATION_CONFLICT",
            "The FlowRun Runtime allocation ownership marker is invalid",
            409,
        )
    for relative, expected_mode in _DIRECTORY_MODES.items():
        path = root.joinpath(*relative.parts)
        try:
            metadata = path.lstat()
        except FileNotFoundError as exc:
            # ``workspace/nodes`` was introduced after the original FlowRun
            # allocation layout.  It is safe to add because it is an empty
            # sibling of the existing project mount and will immediately be
            # created and ownership-checked by ``_ensure_nodes_store``.
            if relative == PurePosixPath("workspace/nodes"):
                continue
            raise DomainError(
                "RUNTIME_ALLOCATION_MISSING",
                "The FlowRun Runtime allocation is incomplete",
                409,
                {"path": relative.as_posix()},
            ) from exc
        if (
            stat.S_ISLNK(metadata.st_mode)
            or not stat.S_ISDIR(metadata.st_mode)
            or stat.S_IMODE(metadata.st_mode) != expected_mode
            or metadata.st_uid != root_metadata.st_uid
            or metadata.st_gid != root_metadata.st_gid
        ):
            raise DomainError(
                "RUNTIME_ALLOCATION_PERMISSIONS_INVALID",
                "The FlowRun Runtime allocation permissions are invalid",
                409,
                {"path": relative.as_posix()},
            )
    return root


def _ensure_profile_store(root: Path) -> None:
    """Safely add the FR-77 profile child to old Runtime allocations."""

    parent = root / "state" / "persistence"
    parent_metadata = parent.lstat()
    target = parent / "profiles"
    try:
        target.mkdir(mode=0o700)
    except FileExistsError:
        pass
    metadata = target.lstat()
    if (
        stat.S_ISLNK(metadata.st_mode)
        or not stat.S_ISDIR(metadata.st_mode)
        or stat.S_IMODE(metadata.st_mode) != 0o700
        or metadata.st_uid != parent_metadata.st_uid
        or metadata.st_gid != parent_metadata.st_gid
    ):
        raise DomainError(
            "RUNTIME_ALLOCATION_PERMISSIONS_INVALID",
            "The Runtime profile store permissions are invalid",
            409,
        )


def _ensure_nodes_store(root: Path) -> None:
    """Backfill the sibling node store for allocations created before it."""

    workspace = root / "workspace"
    nodes = workspace / "nodes"
    try:
        nodes.mkdir(mode=0o700)
    except FileExistsError:
        pass
    metadata = nodes.lstat()
    parent_metadata = workspace.lstat()
    if (
        stat.S_ISLNK(metadata.st_mode)
        or not stat.S_ISDIR(metadata.st_mode)
        or stat.S_IMODE(metadata.st_mode) != 0o700
        or metadata.st_uid != parent_metadata.st_uid
        or metadata.st_gid != parent_metadata.st_gid
    ):
        raise DomainError(
            "RUNTIME_ALLOCATION_PERMISSIONS_INVALID",
            "The Runtime node store permissions are invalid",
            409,
        )


def allocate_flow_run_runtime(db: Session, flow_run_id: str) -> RuntimeStorageAllocation:
    """Create one rollback-safe, tenant-scoped external Runtime allocation."""

    run_id = _canonical_uuid(flow_run_id, field="FlowRun")
    existing = db.scalar(
        select(FlowRunRuntimeAllocation).where(FlowRunRuntimeAllocation.flow_run_id == run_id)
    )
    if existing is not None:
        root = _verify_layout(existing)
        _ensure_profile_store(root)
        _ensure_nodes_store(root)
        return RuntimeStorageAllocation(
            existing.id,
            existing.flow_run_id,
            existing.secret_reference_id,
            existing.relative_root,
        )

    relative = _relative_root(run_id)
    root = _host_root(relative)
    workspace_root = _workspace_root()
    _ensure_parent(root.parent, managed_root=workspace_root)
    if root.exists() or root.is_symlink():
        raise DomainError(
            "RUNTIME_ALLOCATION_CONFLICT",
            "Untracked data already exists at the FlowRun Runtime allocation path",
            409,
        )

    allocation_id = uid()
    secret_reference_id = uid()
    secret_key = secrets.token_hex(32)
    try:
        root.mkdir(mode=0o700)
        _write_marker(root, allocation_id)
        _create_lock_file(root)
        for relative_path, mode in _DIRECTORY_MODES.items():
            root.joinpath(*relative_path.parts).mkdir(mode=mode, parents=True, exist_ok=True)
            root.joinpath(*relative_path.parts).chmod(mode)
        _ensure_profile_store(root)
        root.chmod(0o700)
        secret_reference = FlowRunRuntimeSecretReference(
            id=secret_reference_id,
            encrypted_secret_key=encrypt_secret(secret_key),
            secret_digest=hashlib.sha256(secret_key.encode("ascii")).hexdigest(),
        )
        db.add(secret_reference)
        db.flush()
        allocation = FlowRunRuntimeAllocation(
            id=allocation_id,
            flow_run_id=run_id,
            secret_reference_id=secret_reference_id,
            relative_root=relative.as_posix(),
        )
        db.add(allocation)
        db.flush()
        _verify_layout(allocation)
    except BaseException:
        _remove_new_allocation_root(root, allocation_id)
        raise
    register_rollback_action(
        db,
        lambda root=root, allocation_id=allocation_id: _remove_allocation_root(root, allocation_id),
    )
    return RuntimeStorageAllocation(
        allocation_id,
        run_id,
        secret_reference_id,
        relative.as_posix(),
    )


def ensure_capability_manifest_directory(
    allocation: FlowRunRuntimeAllocation, manifest_digest: str
) -> Path:
    """Create one digest directory without making the Runtime mount writable."""

    if not re.fullmatch(r"[0-9a-f]{64}", manifest_digest):
        raise DomainError(
            "RUNTIME_CAPABILITY_UNAVAILABLE",
            "The Snapshot Runtime Manifest digest is invalid",
            409,
        )
    root = _verify_layout(allocation)
    capabilities = root / "capabilities"
    target = capabilities / manifest_digest
    if target.is_symlink() or (target.exists() and not target.is_dir()):
        raise DomainError(
            "RUNTIME_CAPABILITY_UNAVAILABLE",
            "The Snapshot capability directory is not a plain directory",
            409,
        )
    target.mkdir(mode=0o700, exist_ok=True)
    target.chmod(0o700)
    return target


def runtime_allocation_for_flow_run(
    db: Session, flow_run_id: str, *, manifest_digest: str | None = None
) -> FlowRunRuntimeAllocation:
    flow_run_id = runtime_owner_flow_run_id(db, flow_run_id)
    allocation = db.scalar(
        select(FlowRunRuntimeAllocation).where(FlowRunRuntimeAllocation.flow_run_id == flow_run_id)
    )
    if allocation is None:
        raise DomainError(
            "FLOW_RUN_RUNTIME_ALLOCATION_REQUIRED",
            "This historical FlowRun has no external Runtime allocation and must be rerun",
            409,
            {"flow_run_id": flow_run_id},
        )
    # Allocations created before the node/project split do not contain this
    # sibling directory. Validate the existing allocation first, then create
    # and validate the backward-compatible node store.
    root = _verify_layout(allocation)
    _ensure_nodes_store(root)
    if manifest_digest is not None:
        with capability_materialization_lock(allocation):
            manifest_path = _host_root(allocation.relative_root) / "capabilities" / manifest_digest
            existed = manifest_path.exists() or manifest_path.is_symlink()
            ensure_capability_manifest_directory(allocation, manifest_digest)
            if not existed:
                allocation_root = _host_root(allocation.relative_root)
                allocation_id = allocation.id
                register_rollback_action(
                    db,
                    lambda allocation_root=allocation_root,
                    allocation_id=allocation_id,
                    manifest_digest=manifest_digest: (
                        _remove_capability_manifest(allocation_root, allocation_id, manifest_digest)
                    ),
                )
    return allocation


@contextmanager
def capability_materialization_lock(
    allocation: FlowRunRuntimeAllocation,
) -> Iterator[None]:
    """Serialize immutable capability materialization for one FlowRun."""

    root = _verify_layout(allocation)
    lock_path = root / _LOCK_NAME
    flags = os.O_RDWR
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    descriptor = os.open(lock_path, flags)
    try:
        fcntl.flock(descriptor, fcntl.LOCK_EX)
        yield
    finally:
        fcntl.flock(descriptor, fcntl.LOCK_UN)
        os.close(descriptor)


def resolve_runtime_secret(db: Session, allocation_id: str) -> str:
    allocation = db.get(FlowRunRuntimeAllocation, allocation_id)
    if allocation is None:
        raise DomainError(
            "RUNTIME_SECRET_REFERENCE_MISSING",
            "The Runtime allocation Secret Reference is unavailable",
            409,
        )
    reference = db.get(FlowRunRuntimeSecretReference, allocation.secret_reference_id)
    if reference is None:
        raise DomainError(
            "RUNTIME_SECRET_REFERENCE_MISSING",
            "The Runtime allocation Secret Reference is unavailable",
            409,
        )
    try:
        secret_key = decrypt_secret(reference.encrypted_secret_key)
    except Exception as exc:
        raise DomainError(
            "RUNTIME_SECRET_REFERENCE_INVALID",
            "The Runtime allocation Secret Reference cannot be decrypted",
            409,
        ) from exc
    if (
        len(secret_key) < 32
        or hashlib.sha256(secret_key.encode("utf-8")).hexdigest() != reference.secret_digest
    ):
        raise DomainError(
            "RUNTIME_SECRET_REFERENCE_INVALID",
            "The Runtime allocation Secret Reference failed integrity validation",
            409,
        )
    return secret_key


def delete_flow_run_runtime_allocation(db: Session, flow_run_id: str) -> None:
    """Delete allocation metadata only after every physical Runtime is gone."""

    allocation = db.scalar(
        select(FlowRunRuntimeAllocation)
        .where(FlowRunRuntimeAllocation.flow_run_id == flow_run_id)
        .with_for_update()
    )
    if allocation is None:
        return
    sandbox_id = db.scalar(
        select(ManagedSandbox.id)
        .where(ManagedSandbox.runtime_allocation_id == allocation.id)
        .limit(1)
    )
    if sandbox_id is not None:
        raise DomainError(
            "FLOW_RUN_RUNTIME_DELETE_PROTECTED",
            "The FlowRun Runtime allocation is still referenced by managed compute",
            409,
            {"flow_run_id": flow_run_id, "sandbox_id": sandbox_id},
        )
    root = _verify_layout(allocation)
    allocation_id = allocation.id
    secret_reference = db.get(FlowRunRuntimeSecretReference, allocation.secret_reference_id)
    db.delete(allocation)
    db.flush()
    if secret_reference is not None:
        db.delete(secret_reference)
    register_commit_action(
        db,
        lambda root=root, allocation_id=allocation_id: _remove_allocation_root(root, allocation_id),
    )


def _remove_allocation_root(root: Path, allocation_id: str) -> None:
    if root.is_symlink() or root.is_file():
        raise OSError("Refusing to delete a non-directory Runtime allocation root")
    if not root.exists():
        return
    marker = root / _MARKER_NAME
    if marker.is_symlink() or not marker.is_file():
        raise OSError("Refusing to delete a Runtime allocation without its ownership marker")
    if marker.read_text(encoding="ascii") != allocation_id:
        raise OSError("Refusing to delete a Runtime allocation owned by another record")
    for current_root, directories, _files in os.walk(root, topdown=False, followlinks=False):
        current = Path(current_root)
        for name in directories:
            child = current / name
            if not child.is_symlink():
                child.chmod(0o700)
        if not current.is_symlink():
            current.chmod(0o700)
    shutil.rmtree(root)


def _remove_new_allocation_root(root: Path, allocation_id: str) -> None:
    """Compensate the exclusive create path even if marker creation failed."""

    if not root.exists() and not root.is_symlink():
        return
    marker = root / _MARKER_NAME
    if marker.exists() or marker.is_symlink():
        _remove_allocation_root(root, allocation_id)
        return
    if root.is_symlink() or not root.is_dir():
        raise OSError("Refusing to compensate a replaced Runtime allocation root")
    shutil.rmtree(root)


def _remove_capability_manifest(root: Path, allocation_id: str, manifest_digest: str) -> None:
    marker = root / _MARKER_NAME
    if (
        root.is_symlink()
        or not root.is_dir()
        or marker.is_symlink()
        or not marker.is_file()
        or marker.read_text(encoding="ascii") != allocation_id
    ):
        raise OSError("Refusing to alter an unowned Runtime capability allocation")
    capabilities = root / "capabilities"
    target = capabilities / manifest_digest
    if target.is_symlink() or target.is_file():
        raise OSError("Refusing to remove a non-directory capability manifest")
    if not target.exists():
        return
    for current_root, directories, _files in os.walk(target, topdown=False, followlinks=False):
        current = Path(current_root)
        for name in directories:
            child = current / name
            if not child.is_symlink():
                child.chmod(0o700)
        if not current.is_symlink():
            current.chmod(0o700)
    shutil.rmtree(target)


__all__ = (
    "RuntimeStorageAllocation",
    "allocate_flow_run_runtime",
    "capability_materialization_lock",
    "delete_flow_run_runtime_allocation",
    "ensure_capability_manifest_directory",
    "flow_run_capability_path",
    "flow_run_workspace_nodes_path",
    "flow_run_workspace_project_path",
    "openhands_flow_run_capability_path",
    "openhands_flow_run_nodes_path",
    "openhands_flow_run_project_path",
    "resolve_runtime_secret",
    "runtime_allocation_for_flow_run",
)
