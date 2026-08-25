from __future__ import annotations

import hashlib
import os
import re
import secrets
import stat
from datetime import UTC, datetime, timedelta
from pathlib import Path, PurePosixPath

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from flowweave.modules.agent_workspaces.infrastructure.models import (
    AgentWorkspace,
    AgentWorkspaceRuntime,
    AgentWorkspaceRuntimeAllocation,
    AgentWorkspaceRuntimeGeneration,
    AgentWorkspaceRuntimeSecretReference,
)
from flowweave.modules.environments.infrastructure.docker import resolve_setup_image
from flowweave.modules.sandboxes.infrastructure.docker import DockerSandboxProvider, backend_name
from flowweave.modules.sandboxes.infrastructure.models import ManagedSandbox
from flowweave.modules.tasks.application.service import enqueue
from flowweave.shared.credentials_crypto import decrypt_secret, encrypt_secret
from flowweave.shared.database import uid
from flowweave.shared.errors import DomainError
from flowweave.shared.settings import get_settings

_SCOPE_KEY = "platform-default"
_ROOT = PurePosixPath(".agent-workspaces")
_MARKER = ".flowweave-allocation"
_DIRECTORIES = (
    PurePosixPath("workspace"),
    PurePosixPath("workspace/project"),
    PurePosixPath("state"),
    PurePosixPath("state/conversations"),
    PurePosixPath("state/bash-events"),
    PurePosixPath("state/persistence"),
    PurePosixPath("capabilities"),
)


def _workspace_root() -> Path:
    root = Path(get_settings().workspace_root).absolute()
    if root.is_symlink() or not root.is_dir():
        raise DomainError(
            "AGENT_WORKSPACE_STORAGE_UNAVAILABLE",
            "The managed workspace root must be an existing plain directory",
            503,
        )
    return root.resolve()


def _relative_root() -> PurePosixPath:
    return _ROOT / _SCOPE_KEY


def _host_root(relative_root: str) -> Path:
    relative = PurePosixPath(relative_root)
    if (
        relative != _relative_root()
        or relative.is_absolute()
        or any(part in {"", ".", ".."} for part in relative.parts)
    ):
        raise DomainError(
            "AGENT_WORKSPACE_ALLOCATION_INVALID",
            "The Agent Workspace allocation path is invalid",
            409,
        )
    root = _workspace_root()
    target = root.joinpath(*relative.parts)
    if not target.is_relative_to(root):
        raise DomainError(
            "AGENT_WORKSPACE_ALLOCATION_INVALID",
            "The Agent Workspace allocation escaped its managed root",
            409,
        )
    return target


def _ensure_directory(path: Path) -> None:
    path.mkdir(mode=0o700, parents=True, exist_ok=True)
    metadata = path.lstat()
    if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISDIR(metadata.st_mode):
        raise DomainError(
            "AGENT_WORKSPACE_ALLOCATION_CONFLICT",
            "An Agent Workspace allocation path is not a plain directory",
            409,
        )
    path.chmod(0o700)


def _verify_allocation(allocation: AgentWorkspaceRuntimeAllocation) -> Path:
    if allocation.relative_root != _relative_root().as_posix():
        raise DomainError(
            "AGENT_WORKSPACE_ALLOCATION_CONFLICT",
            "The Agent Workspace allocation does not match the default scope",
            409,
        )
    root = _host_root(allocation.relative_root)
    marker = root / _MARKER
    try:
        root_metadata = root.lstat()
        marker_metadata = marker.lstat()
        marker_value = marker.read_text(encoding="ascii")
    except (FileNotFoundError, OSError, UnicodeDecodeError) as exc:
        raise DomainError(
            "AGENT_WORKSPACE_ALLOCATION_MISSING",
            "The Agent Workspace external storage is incomplete",
            409,
        ) from exc
    if (
        stat.S_ISLNK(root_metadata.st_mode)
        or not stat.S_ISDIR(root_metadata.st_mode)
        or stat.S_IMODE(root_metadata.st_mode) != 0o700
        or stat.S_ISLNK(marker_metadata.st_mode)
        or not stat.S_ISREG(marker_metadata.st_mode)
        or stat.S_IMODE(marker_metadata.st_mode) != 0o400
        or marker_value != allocation.id
    ):
        raise DomainError(
            "AGENT_WORKSPACE_ALLOCATION_CONFLICT",
            "The Agent Workspace external storage marker is invalid",
            409,
        )
    for relative in _DIRECTORIES:
        metadata = (root / relative).lstat()
        if (
            stat.S_ISLNK(metadata.st_mode)
            or not stat.S_ISDIR(metadata.st_mode)
            or stat.S_IMODE(metadata.st_mode) != 0o700
        ):
            raise DomainError(
                "AGENT_WORKSPACE_ALLOCATION_CONFLICT",
                "The Agent Workspace external storage permissions are invalid",
                409,
                {"path": relative.as_posix()},
            )
    return root


def _ensure_allocation(db: Session, workspace: AgentWorkspace) -> AgentWorkspaceRuntimeAllocation:
    allocation = db.scalar(
        select(AgentWorkspaceRuntimeAllocation)
        .where(AgentWorkspaceRuntimeAllocation.workspace_id == workspace.id)
        .with_for_update()
    )
    if allocation is not None:
        _verify_allocation(allocation)
        return allocation
    root = _host_root(_relative_root().as_posix())
    if root.exists() or root.is_symlink():
        raise DomainError(
            "AGENT_WORKSPACE_ALLOCATION_CONFLICT",
            "Untracked data already exists at the Agent Workspace allocation path",
            409,
        )
    allocation_id = uid()
    secret_id = uid()
    secret = secrets.token_hex(32)
    root.mkdir(mode=0o700, parents=True)
    try:
        marker = root / _MARKER
        descriptor = os.open(marker, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o400)
        try:
            os.write(descriptor, allocation_id.encode("ascii"))
        finally:
            os.close(descriptor)
        for directory in _DIRECTORIES:
            _ensure_directory(root / directory)
        reference = AgentWorkspaceRuntimeSecretReference(
            id=secret_id,
            workspace_id=workspace.id,
            encrypted_secret_key=encrypt_secret(secret),
            secret_digest=hashlib.sha256(secret.encode("ascii")).hexdigest(),
        )
        allocation = AgentWorkspaceRuntimeAllocation(
            id=allocation_id,
            workspace_id=workspace.id,
            secret_reference_id=secret_id,
            relative_root=_relative_root().as_posix(),
        )
        # The allocation has an explicit FK but no ORM relationship to its
        # Secret Reference, so flush the reference first rather than relying
        # on SQLAlchemy's unit-of-work ordering.
        db.add(reference)
        db.flush()
        db.add(allocation)
        db.flush()
        _verify_allocation(allocation)
        return allocation
    except BaseException:
        # The exclusive root belongs to this attempt; retain no partially valid
        # allocation if the database transaction cannot be established.
        if root.exists() and not root.is_symlink():
            for current, _dirs, _files in os.walk(root, topdown=False):
                Path(current).chmod(0o700)
            import shutil

            shutil.rmtree(root)
        raise


def runtime_allocation_for_agent_workspace(
    db: Session, workspace_id: str
) -> AgentWorkspaceRuntimeAllocation:
    allocation = db.scalar(
        select(AgentWorkspaceRuntimeAllocation).where(
            AgentWorkspaceRuntimeAllocation.workspace_id == workspace_id
        )
    )
    if allocation is None:
        raise DomainError(
            "AGENT_WORKSPACE_ALLOCATION_REQUIRED",
            "The Agent Workspace external storage has not been allocated",
            409,
        )
    _verify_allocation(allocation)
    return allocation


def resolve_agent_workspace_runtime_secret(db: Session, allocation_id: str) -> str:
    allocation = db.get(AgentWorkspaceRuntimeAllocation, allocation_id)
    reference = (
        db.get(AgentWorkspaceRuntimeSecretReference, allocation.secret_reference_id)
        if allocation is not None
        else None
    )
    if reference is None:
        raise DomainError(
            "AGENT_WORKSPACE_SECRET_REFERENCE_MISSING",
            "The Agent Workspace Runtime Secret Reference is unavailable",
            409,
        )
    try:
        value = decrypt_secret(reference.encrypted_secret_key)
    except Exception as exc:
        raise DomainError(
            "AGENT_WORKSPACE_SECRET_REFERENCE_INVALID",
            "The Agent Workspace Runtime Secret Reference cannot be decrypted",
            409,
        ) from exc
    if (
        len(value) < 32
        or hashlib.sha256(value.encode("utf-8")).hexdigest() != reference.secret_digest
    ):
        raise DomainError(
            "AGENT_WORKSPACE_SECRET_REFERENCE_INVALID",
            "The Agent Workspace Runtime Secret Reference failed integrity validation",
            409,
        )
    return value


def ensure_default_agent_workspace(db: Session) -> AgentWorkspace:
    workspace = db.scalar(
        select(AgentWorkspace).where(AgentWorkspace.scope_key == _SCOPE_KEY).with_for_update()
    )
    if workspace is None:
        workspace = AgentWorkspace(scope_key=_SCOPE_KEY, display_name="Agent 工作区")
        db.add(workspace)
        db.flush()
    allocation = _ensure_allocation(db, workspace)
    runtime = db.scalar(
        select(AgentWorkspaceRuntime)
        .where(AgentWorkspaceRuntime.workspace_id == workspace.id)
        .with_for_update()
    )
    if runtime is None:
        if get_settings().runtime_adapter == "mock":
            digest = "sha256:" + "0" * 64
        else:
            _reference, digest = resolve_setup_image(get_settings().agent_workspace_runtime_image)
        if re.fullmatch(r"sha256:[0-9a-f]{64}", digest) is None:
            raise DomainError(
                "AGENT_WORKSPACE_RUNTIME_IMAGE_INVALID",
                "The default Agent Runtime image did not resolve to an immutable digest",
                409,
            )
        runtime = AgentWorkspaceRuntime(
            workspace_id=workspace.id,
            runtime_image_digest=digest,
            workspace_allocation_id=allocation.id,
            status="STARTING",
        )
        db.add(runtime)
        db.flush()
    elif runtime.workspace_allocation_id != allocation.id:
        raise DomainError(
            "AGENT_WORKSPACE_RUNTIME_SPEC_CONFLICT",
            "The Agent Workspace Runtime is bound to a different storage allocation",
            409,
        )
    if get_settings().runtime_adapter != "mock" and workspace.desired_state == "RUNNING":
        task = enqueue(
            db,
            task_type="PROVISION_AGENT_WORKSPACE_RUNTIME",
            aggregate_type="AGENT_WORKSPACE",
            aggregate_id=workspace.id,
            idempotency_key=f"provision-agent-workspace-runtime:{workspace.id}",
        )
        task.max_attempts = max(task.max_attempts, 20)
    return workspace


def agent_workspace_owner_is_active(db: Session, workspace_id: str) -> bool:
    return (
        db.scalar(
            select(AgentWorkspace.id).where(
                AgentWorkspace.id == workspace_id,
                AgentWorkspace.desired_state == "RUNNING",
            )
        )
        is not None
    )


def process_agent_workspace_runtime(db: Session, workspace_id: str) -> None:
    workspace = db.scalar(
        select(AgentWorkspace).where(AgentWorkspace.id == workspace_id).with_for_update()
    )
    if workspace is None or workspace.desired_state != "RUNNING":
        return
    allocation = runtime_allocation_for_agent_workspace(db, workspace.id)
    runtime = db.scalar(
        select(AgentWorkspaceRuntime)
        .where(AgentWorkspaceRuntime.workspace_id == workspace.id)
        .with_for_update()
    )
    if runtime is None:
        raise DomainError(
            "AGENT_WORKSPACE_RUNTIME_MISSING", "The Agent Workspace Runtime is missing", 409
        )
    resources = list(
        db.scalars(
            select(ManagedSandbox)
            .where(
                ManagedSandbox.kind == "AGENT_RUNTIME",
                ManagedSandbox.owner_type == "AGENT_WORKSPACE",
                ManagedSandbox.owner_id == workspace.id,
            )
            .order_by(ManagedSandbox.generation.desc())
            .with_for_update()
        )
    )
    running = [item for item in resources if item.desired_state == "RUNNING"]
    if len(running) > 1:
        raise DomainError(
            "AGENT_WORKSPACE_RUNTIME_GENERATION_CONFLICT",
            "The Agent Workspace has multiple writable Runtime generations",
            409,
        )
    if resources and resources[0].desired_state == "DELETED":
        deleted_resource = resources[0]
        historical_generation = db.scalar(
            select(AgentWorkspaceRuntimeGeneration)
            .where(
                AgentWorkspaceRuntimeGeneration.runtime_session_id == runtime.id,
                AgentWorkspaceRuntimeGeneration.managed_runtime_id == deleted_resource.id,
            )
            .with_for_update()
        )
        if historical_generation is not None:
            historical_generation.managed_runtime_id = None
            historical_generation.state = "DELETED"
            historical_generation.stopped_at = datetime.now(UTC)
            historical_generation.row_version += 1
        DockerSandboxProvider(get_settings()).delete(deleted_resource)
        db.delete(deleted_resource)
        db.flush()
        resources = []
        running = []
    resource = running[0] if running else None
    if resource is None:
        floor = int(
            db.scalar(
                select(
                    func.coalesce(func.max(AgentWorkspaceRuntimeGeneration.generation), 0)
                ).where(AgentWorkspaceRuntimeGeneration.runtime_session_id == runtime.id)
            )
            or 0
        )
        generation = floor + 1
        now = datetime.now(UTC)
        resource_id = uid()
        resource = ManagedSandbox(
            id=resource_id,
            kind="AGENT_RUNTIME",
            owner_type="AGENT_WORKSPACE",
            owner_id=workspace.id,
            backend="docker",
            backend_resource_name=backend_name(
                resource_id, owner_type="AGENT_WORKSPACE", owner_id=workspace.id
            ),
            generation=generation,
            image_reference=runtime.runtime_image_digest,
            agent_workspace_allocation_id=allocation.id,
            spec_json={
                "port": 8000,
                "agent_workspace_id": workspace.id,
                "runtime_allocation_id": allocation.id,
                "agent_workspace_allocation_id": allocation.id,
                "runtime_allocation_relative": allocation.relative_root,
                "runtime_secret_reference_id": allocation.secret_reference_id,
            },
            hard_expires_at=now + timedelta(days=3650),
            observed_state="CREATING",
        )
        db.add(resource)
        db.flush()
    secret = resolve_agent_workspace_runtime_secret(db, allocation.id)
    try:
        observation = DockerSandboxProvider(get_settings()).ensure_running(
            resource, runtime_secret_key=secret
        )
    except DomainError as exc:
        runtime.status = "DEGRADED"
        runtime.failure_code = exc.code
        runtime.failure_summary = "Runtime provisioning failed; inspect protected logs"
        runtime.row_version += 1
        raise
    resource.backend_resource_id = observation.resource_identifier
    resource.observed_state = observation.state
    resource.last_activity_at = datetime.now(UTC)
    resource.idle_expires_at = None
    resource.last_error_code = None
    resource.last_error_detail = None
    generation = db.scalar(
        select(AgentWorkspaceRuntimeGeneration).where(
            AgentWorkspaceRuntimeGeneration.managed_runtime_id == resource.id
        )
    )
    if generation is None:
        generation = AgentWorkspaceRuntimeGeneration(
            runtime_session_id=runtime.id,
            generation=resource.generation,
            managed_runtime_id=resource.id,
            runtime_image_digest=runtime.runtime_image_digest,
            state="READY",
            fence_token=uid(),
            started_at=datetime.now(UTC),
            ready_at=datetime.now(UTC),
        )
        db.add(generation)
    else:
        generation.state = "READY"
        generation.ready_at = datetime.now(UTC)
        generation.row_version += 1
    runtime.active_generation = resource.generation
    runtime.status = "ACTIVE"
    runtime.failure_code = None
    runtime.failure_summary = None
    runtime.row_version += 1
    db.flush()


def mark_agent_workspace_runtime_lost(db: Session, workspace_id: str, sandbox_id: str) -> None:
    runtime = db.scalar(
        select(AgentWorkspaceRuntime)
        .join(
            AgentWorkspaceRuntimeGeneration,
            AgentWorkspaceRuntimeGeneration.runtime_session_id == AgentWorkspaceRuntime.id,
        )
        .where(
            AgentWorkspaceRuntime.workspace_id == workspace_id,
            AgentWorkspaceRuntimeGeneration.managed_runtime_id == sandbox_id,
        )
        .with_for_update()
    )
    if runtime is None:
        return
    runtime.status = "RECONNECTING"
    runtime.failure_code = "SANDBOX_RUNTIME_LOST"
    runtime.failure_summary = "The Agent Runtime is being replaced"
    runtime.row_version += 1
    resource = db.get(ManagedSandbox, sandbox_id)
    if resource is not None:
        resource.desired_state = "DELETED"
        resource.next_reconcile_at = datetime.now(UTC)
    task = enqueue(
        db,
        task_type="PROVISION_AGENT_WORKSPACE_RUNTIME",
        aggregate_type="AGENT_WORKSPACE",
        aggregate_id=workspace_id,
        idempotency_key=f"recover-agent-workspace-runtime:{workspace_id}:{sandbox_id}",
    )
    task.max_attempts = max(task.max_attempts, 20)


__all__ = (
    "agent_workspace_owner_is_active",
    "ensure_default_agent_workspace",
    "mark_agent_workspace_runtime_lost",
    "process_agent_workspace_runtime",
    "resolve_agent_workspace_runtime_secret",
    "runtime_allocation_for_agent_workspace",
)
