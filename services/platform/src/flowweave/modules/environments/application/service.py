from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import func, select, update
from sqlalchemy.engine import Connection, Engine
from sqlalchemy.orm import Session

from flowweave.modules.environments.infrastructure import docker
from flowweave.modules.sandboxes import public as sandboxes
from flowweave.modules.tasks.public import Lease, enqueue, lease_is_current
from flowweave.shared.application.transactions import finish
from flowweave.shared.errors import DomainError, conflict, not_found
from flowweave.shared.models import (
    BackgroundTask,
    CapabilityValidation,
    EnvironmentSetupSession,
    EnvironmentVersion,
    FlowRun,
    MCPOAuthSecretReference,
    NodeAsset,
    TaskState,
    TerminalEnvironment,
)
from flowweave.shared.schemas import TerminalEnvironmentWrite
from flowweave.shared.settings import get_settings


def _time(value: datetime | None) -> str | None:
    return value.isoformat() if value else None


def _version_dict(
    item: EnvironmentVersion, *, node_reference_count: int = 0, run_reference_count: int = 0
) -> dict[str, Any]:
    return {
        "id": item.id,
        "environment_id": item.environment_id,
        "version_no": item.version_no,
        "parent_version_id": item.parent_version_id,
        "state": item.state,
        "image_reference": item.image_reference,
        "image_digest": item.image_digest,
        "manifest": item.manifest_json or {},
        "error_detail": item.error_detail,
        "node_reference_count": node_reference_count,
        "run_reference_count": run_reference_count,
        "reference_count": node_reference_count + run_reference_count,
        "created_at": _time(item.created_at),
    }


def _session_dict(item: EnvironmentSetupSession) -> dict[str, Any]:
    return {
        "id": item.id,
        "environment_id": item.environment_id,
        "base_version_id": item.base_version_id,
        "state": item.state,
        "base_image_reference": item.base_image_reference,
        "expires_at": _time(item.expires_at),
        "error_detail": item.error_detail,
        "created_at": _time(item.created_at),
        "updated_at": _time(item.updated_at),
    }


_CLEANUP_MAX_ATTEMPTS = 20


def _control_engine(db: Session) -> Engine:
    bind = db.get_bind()
    return bind.engine if isinstance(bind, Connection) else bind


def _session_lock(connection: Connection, key: str) -> int:
    lock_id = connection.scalar(select(func.hashtextextended(key, 0)))
    if lock_id is None:
        raise RuntimeError("Could not derive the environment control lock")
    connection.scalar(select(func.pg_advisory_lock(lock_id)))
    connection.commit()
    return int(lock_id)


def _session_unlock(connection: Connection, lock_id: int) -> None:
    if connection.in_transaction():
        connection.rollback()
    connection.scalar(select(func.pg_advisory_unlock(lock_id)))
    connection.commit()


def _enqueue_setup_cleanup(db: Session, item: EnvironmentSetupSession) -> None:
    if not item.container_id:
        return
    if item.sandbox_id:
        sandboxes.request_delete(db, item.sandbox_id)
    task = enqueue(
        db,
        task_type="CLEANUP_SETUP_CONTAINER",
        aggregate_type="SETUP_SESSION",
        aggregate_id=item.id,
        idempotency_key=f"cleanup-setup-container:{item.id}:{item.container_id}",
        payload={"container_id": item.container_id, "sandbox_id": item.sandbox_id},
    )
    task.max_attempts = max(task.max_attempts, _CLEANUP_MAX_ATTEMPTS)


def _enqueue_image_cleanup(
    db: Session,
    *,
    environment_id: str,
    version_id: str,
    version_no: int,
    image_reference: str,
    image_digest: str,
) -> None:
    if not image_reference or not image_digest:
        return
    task = enqueue(
        db,
        task_type="CLEANUP_ENVIRONMENT_IMAGE",
        aggregate_type="ENVIRONMENT_VERSION",
        aggregate_id=version_id,
        idempotency_key=f"cleanup-environment-image:{version_id}",
        payload={
            "environment_id": environment_id,
            "version_id": version_id,
            "version_no": version_no,
            "image_reference": image_reference,
            "image_digest": image_digest,
        },
    )
    task.max_attempts = max(task.max_attempts, _CLEANUP_MAX_ATTEMPTS)


def _enqueue_credential_cleanup(db: Session, environment_id: str) -> None:
    task = enqueue(
        db,
        task_type="CLEANUP_ENVIRONMENT_CREDENTIALS",
        aggregate_type="ENVIRONMENT",
        aggregate_id=environment_id,
        idempotency_key=f"cleanup-environment-credentials:{environment_id}",
        payload={"environment_id": environment_id},
    )
    task.max_attempts = max(task.max_attempts, _CLEANUP_MAX_ATTEMPTS)


def _expire_setup_sessions(db: Session, environment_id: str | None = None) -> int:
    query = select(EnvironmentSetupSession).where(
        EnvironmentSetupSession.state.in_(["STARTING", "RUNNING"]),
        EnvironmentSetupSession.expires_at <= datetime.now(UTC),
    )
    if environment_id is not None:
        query = query.where(EnvironmentSetupSession.environment_id == environment_id)
    expired = list(db.scalars(query.with_for_update()))
    for item in expired:
        item.state = "EXPIRED"
        item.updated_at = datetime.now(UTC)
        _enqueue_setup_cleanup(db, item)
    return len(expired)


def expire_setup_sessions(db: Session, *, commit: bool = True) -> int:
    """Actively reclaim expired setup containers from a worker maintenance pass."""

    expired = _expire_setup_sessions(db)
    if commit:
        finish(db)
    else:
        db.flush()
    return expired


def recover_environment_cleanup_tasks(db: Session, *, commit: bool = True) -> int:
    """Requeue terminal resource cleanups that exhausted normal delivery retries."""

    recovered = 0
    terminal_sessions = list(
        db.scalars(
            select(EnvironmentSetupSession).where(
                EnvironmentSetupSession.container_id != "",
                EnvironmentSetupSession.state.in_(["PUBLISHED", "CANCELLED", "EXPIRED"]),
            )
        )
    )
    for item in terminal_sessions:
        key = f"cleanup-setup-container:{item.id}:{item.container_id}"
        task = db.scalar(select(BackgroundTask).where(BackgroundTask.idempotency_key == key))
        if task is None:
            _enqueue_setup_cleanup(db, item)
            recovered += 1
        elif task.state == TaskState.DEAD:
            task.state = TaskState.RETRY
            task.available_at = datetime.now(UTC)
            task.lease_owner = None
            task.lease_until = None
            task.last_error = "RESOURCE_CLEANUP_RECOVERY"
            task.max_attempts = max(task.max_attempts, _CLEANUP_MAX_ATTEMPTS)
            recovered += 1
    dead_image_tasks = list(
        db.scalars(
            select(BackgroundTask).where(
                BackgroundTask.task_type == "CLEANUP_ENVIRONMENT_IMAGE",
                BackgroundTask.state == TaskState.DEAD,
            )
        )
    )
    for task in dead_image_tasks:
        if any(
            code in (task.last_error or "")
            for code in (
                "ENVIRONMENT_IMAGE_OWNERSHIP_MISMATCH",
                "ENVIRONMENT_IMAGE_TAG_CONFLICT",
            )
        ):
            continue
        task.state = TaskState.RETRY
        task.available_at = datetime.now(UTC)
        task.lease_owner = None
        task.lease_until = None
        task.last_error = "RESOURCE_CLEANUP_RECOVERY"
        task.max_attempts = max(task.max_attempts, _CLEANUP_MAX_ATTEMPTS)
        recovered += 1
    deleted_environments = list(
        db.scalars(select(TerminalEnvironment).where(TerminalEnvironment.deleted_at.is_not(None)))
    )
    for environment in deleted_environments:
        key = f"cleanup-environment-credentials:{environment.id}"
        task = db.scalar(select(BackgroundTask).where(BackgroundTask.idempotency_key == key))
        if task is None:
            _enqueue_credential_cleanup(db, environment.id)
            recovered += 1
        elif task.state == TaskState.DEAD and "SANDBOX_RESOURCE_CONFLICT" not in (
            task.last_error or ""
        ):
            task.state = TaskState.RETRY
            task.available_at = datetime.now(UTC)
            task.lease_owner = None
            task.lease_until = None
            task.last_error = "RESOURCE_CLEANUP_RECOVERY"
            task.max_attempts = max(task.max_attempts, _CLEANUP_MAX_ATTEMPTS)
            recovered += 1
    if commit:
        finish(db)
    else:
        db.flush()
    return recovered


def process_cleanup_setup_container(
    db: Session, session_id: str, payload: dict[str, Any], lease: Lease, *, commit: bool = True
) -> None:
    item = get_setup_session(db, session_id)
    expected_container_id = str(payload.get("container_id") or "")
    if not expected_container_id or item.container_id != expected_container_id:
        return
    expected_sandbox_id = str(payload.get("sandbox_id") or "")
    if expected_sandbox_id and item.sandbox_id != expected_sandbox_id:
        return
    db.rollback()
    if expected_sandbox_id:
        sandboxes.delete_sandbox_now(db, expected_sandbox_id)
    else:
        # Compatibility for setup sessions created before the sandbox ledger.
        docker.remove_legacy_setup_container(
            expected_container_id, environment_id=item.environment_id
        )
    if not lease_is_current(db, lease):
        raise RuntimeError("task lease was lost during setup container cleanup")
    current = db.scalar(
        select(EnvironmentSetupSession)
        .where(EnvironmentSetupSession.id == session_id)
        .with_for_update()
    )
    if current is not None and current.container_id == expected_container_id:
        current.container_id = ""
        current.error_detail = None
    db.commit() if commit else db.flush()


def process_cleanup_environment_image(
    db: Session, payload: dict[str, Any], lease: Lease, *, commit: bool = True
) -> None:
    reference = str(payload.get("image_reference") or "")
    digest = str(payload.get("image_digest") or "")
    environment_id = str(payload.get("environment_id") or "") or None
    version_id = str(payload.get("version_id") or "") or None
    raw_version_no = payload.get("version_no")
    version_no = int(raw_version_no) if raw_version_no is not None else None
    if not reference or not digest:
        return
    if environment_id and version_id:
        # Use the same parent-row lock as reference creation and deletion. The
        # task payload is only intent; current database references remain the
        # authority immediately before destructive Docker I/O.
        db.scalar(
            select(TerminalEnvironment.id)
            .where(TerminalEnvironment.id == environment_id)
            .with_for_update()
        )
        current_version = db.get(EnvironmentVersion, version_id)
        node_reference = db.scalar(
            select(NodeAsset.id).where(NodeAsset.environment_version_id == version_id).limit(1)
        )
        run_reference = db.scalar(
            select(FlowRun.id).where(FlowRun.environment_version_id == version_id).limit(1)
        )
        setup_reference = db.scalar(
            select(EnvironmentSetupSession.id)
            .where(
                (EnvironmentSetupSession.base_version_id == version_id)
                | (EnvironmentSetupSession.published_version_id == version_id),
                (EnvironmentSetupSession.state.in_(["STARTING", "RUNNING"]))
                | (EnvironmentSetupSession.container_id != ""),
            )
            .limit(1)
        )
        sandbox_reference = sandboxes.image_has_live_sandbox(db, reference=reference, digest=digest)
        if (
            (
                current_version is not None
                and (current_version.state == "READY" or bool(current_version.image_reference))
            )
            or node_reference is not None
            or run_reference is not None
            or setup_reference is not None
            or sandbox_reference
        ):
            raise DomainError(
                "ENVIRONMENT_IMAGE_STILL_REFERENCED",
                "The environment image is still referenced by durable state",
                409,
                {"environment_id": environment_id, "version_id": version_id},
            )
    db.rollback()
    docker.remove_image(
        reference,
        expected_digest=digest,
        environment_id=environment_id,
        version_id=version_id,
        version_no=version_no,
    )
    if not lease_is_current(db, lease):
        raise RuntimeError("task lease was lost during environment image cleanup")
    if commit:
        db.commit()
    else:
        db.flush()


def process_cleanup_environment_credentials(
    db: Session, environment_id: str, lease: Lease, *, commit: bool = True
) -> None:
    item = db.scalar(
        select(TerminalEnvironment)
        .where(TerminalEnvironment.id == environment_id)
        .with_for_update()
    )
    if item is None or item.deleted_at is None:
        raise DomainError(
            "ENVIRONMENT_CREDENTIAL_CLEANUP_STALE",
            "Credential cleanup requires a deleted environment",
            409,
            {"environment_id": environment_id},
        )
    if sandboxes.environment_has_live_sandbox(db, environment_id=environment_id):
        raise DomainError(
            "ENVIRONMENT_CREDENTIALS_STILL_REFERENCED",
            "The environment credential volume is still mounted by a sandbox",
            409,
            {"environment_id": environment_id},
        )
    db.rollback()
    sandboxes.delete_environment_credentials(environment_id)
    if not lease_is_current(db, lease):
        raise RuntimeError("task lease was lost during environment credential cleanup")
    db.commit() if commit else db.flush()


def environment_dict(db: Session, item: TerminalEnvironment) -> dict[str, Any]:
    versions = list(
        db.scalars(
            select(EnvironmentVersion)
            .where(EnvironmentVersion.environment_id == item.id)
            .order_by(EnvironmentVersion.version_no.desc())
        )
    )
    sessions = list(
        db.scalars(
            select(EnvironmentSetupSession)
            .where(
                EnvironmentSetupSession.environment_id == item.id,
                EnvironmentSetupSession.state.in_(["STARTING", "RUNNING"]),
            )
            .order_by(EnvironmentSetupSession.created_at.desc())
        )
    )
    version_ids = [version.id for version in versions]
    node_references: dict[str, int] = {}
    run_references: dict[str, int] = {}
    if version_ids:
        node_references = {
            version_id: int(count)
            for version_id, count in db.execute(
                select(NodeAsset.environment_version_id, func.count(NodeAsset.id))
                .where(
                    NodeAsset.environment_version_id.in_(version_ids),
                    NodeAsset.deleted_at.is_(None),
                )
                .group_by(NodeAsset.environment_version_id)
            )
            if version_id is not None
        }
        run_references = {
            version_id: int(count)
            for version_id, count in db.execute(
                select(FlowRun.environment_version_id, func.count(FlowRun.id))
                .where(FlowRun.environment_version_id.in_(version_ids))
                .group_by(FlowRun.environment_version_id)
            )
            if version_id is not None
        }
    return {
        "id": item.id,
        "name": item.name,
        "description": item.description,
        "base_image": item.base_image,
        "row_version": item.row_version,
        "versions": [
            _version_dict(
                version,
                node_reference_count=node_references.get(version.id, 0),
                run_reference_count=run_references.get(version.id, 0),
            )
            for version in versions
        ],
        "active_sessions": [_session_dict(session) for session in sessions],
        "created_at": _time(item.created_at),
        "updated_at": _time(item.updated_at),
    }


def list_environments(db: Session) -> list[dict[str, Any]]:
    if _expire_setup_sessions(db):
        finish(db)
    return [
        environment_dict(db, item)
        for item in db.scalars(
            select(TerminalEnvironment)
            .where(TerminalEnvironment.deleted_at.is_(None))
            .order_by(TerminalEnvironment.updated_at.desc())
        )
    ]


def _environment(db: Session, environment_id: str) -> TerminalEnvironment:
    item = db.get(TerminalEnvironment, environment_id)
    if item is None or item.deleted_at is not None:
        raise not_found("terminal_environment", environment_id)
    return item


def lock_referenceable_version(db: Session, version_id: str) -> EnvironmentVersion | None:
    """Lock and return a READY version whose parent environment is active.

    Reference creation and environment deletion deliberately acquire the parent
    environment row first. This common lock order closes the check/use race in
    which a node or run could bind a version while its environment was being
    soft-deleted.
    """

    environment_id = db.scalar(
        select(EnvironmentVersion.environment_id).where(EnvironmentVersion.id == version_id)
    )
    if environment_id is None:
        return None
    parent = db.scalar(
        select(TerminalEnvironment)
        .where(TerminalEnvironment.id == environment_id)
        .with_for_update()
    )
    if parent is None or parent.deleted_at is not None:
        return None
    version = db.scalar(
        select(EnvironmentVersion).where(EnvironmentVersion.id == version_id).with_for_update()
    )
    if version is None or version.state != "READY" or not version.image_digest:
        return None
    return version


def read_environment(db: Session, environment_id: str) -> dict[str, Any]:
    item = _environment(db, environment_id)
    if _expire_setup_sessions(db, environment_id):
        finish(db)
    return environment_dict(db, item)


def save_environment(
    db: Session, payload: TerminalEnvironmentWrite, environment_id: str | None = None
) -> dict[str, Any]:
    base_image = docker.validate_image(payload.base_image)
    configured_base = get_settings().terminal_environment_base_image
    if base_image != configured_base:
        raise DomainError(
            "ENVIRONMENT_BASE_IMAGE_NOT_ALLOWED",
            "Terminal environments must use the administrator-approved base image",
            422,
            {"allowed_base_image": configured_base},
        )
    if environment_id:
        item = _environment(db, environment_id)
        if payload.row_version != item.row_version:
            raise conflict(
                "terminal environment was modified",
                expected=payload.row_version,
                actual=item.row_version,
            )
        item.row_version += 1
    else:
        item = TerminalEnvironment(
            name=payload.name, description=payload.description, base_image=base_image
        )
        db.add(item)
    item.name = payload.name
    item.description = payload.description
    item.base_image = base_image
    item.updated_at = datetime.now(UTC)
    finish(db)
    return environment_dict(db, item)


def create_setup_session(
    db: Session, environment_id: str, base_version_id: str | None
) -> dict[str, Any]:
    # This command owns an independent, crash-recoverable state machine. End
    # the API read transaction before waiting on either the allocation lock or
    # Docker. No unrelated writes are allowed before this command is entered.
    db.rollback()
    engine = _control_engine(db)
    with engine.connect() as connection:
        lock_id = _session_lock(connection, f"ENVIRONMENT_SETUP:{environment_id}")
        try:
            capacity_lock_id = _session_lock(connection, "ENVIRONMENT_SETUP:CAPACITY")
            try:
                with Session(bind=connection, expire_on_commit=False) as control_db:
                    environment = control_db.scalar(
                        select(TerminalEnvironment)
                        .where(TerminalEnvironment.id == environment_id)
                        .with_for_update()
                    )
                    if environment is None or environment.deleted_at is not None:
                        raise not_found("terminal_environment", environment_id)
                    if _expire_setup_sessions(control_db):
                        control_db.flush()
                    active = control_db.scalar(
                        select(EnvironmentSetupSession).where(
                            EnvironmentSetupSession.environment_id == environment.id,
                            EnvironmentSetupSession.state.in_(["STARTING", "RUNNING"]),
                        )
                    )
                    if active is not None:
                        raise DomainError(
                            "ENVIRONMENT_SETUP_ALREADY_RUNNING",
                            "This environment already has an active setup session",
                            409,
                            {"session_id": active.id},
                        )
                    active_count = int(
                        control_db.scalar(
                            select(func.count(EnvironmentSetupSession.id)).where(
                                EnvironmentSetupSession.state.in_(["STARTING", "RUNNING"])
                            )
                        )
                        or 0
                    )
                    max_active = get_settings().terminal_environment_max_active_sessions
                    if active_count >= max_active:
                        raise DomainError(
                            "ENVIRONMENT_SETUP_CAPACITY_EXCEEDED",
                            "The global setup session capacity is exhausted",
                            429,
                            {"active_sessions": active_count, "max_active_sessions": max_active},
                        )
                    image = environment.base_image
                    base_version_no: int | None = None
                    if base_version_id:
                        version = control_db.get(EnvironmentVersion, base_version_id)
                        if (
                            version is None
                            or version.environment_id != environment.id
                            or version.state != "READY"
                        ):
                            raise DomainError(
                                "ENVIRONMENT_VERSION_INVALID",
                                "The selected base environment version is unavailable",
                                422,
                            )
                        image = version.image_digest or version.image_reference
                        base_version_no = version.version_no
                    item = EnvironmentSetupSession(
                        environment_id=environment.id,
                        base_version_id=base_version_id,
                        state="STARTING",
                        base_image_reference=image,
                        expires_at=datetime.now(UTC)
                        + timedelta(
                            seconds=get_settings().terminal_environment_session_ttl_seconds
                        ),
                    )
                    control_db.add(item)
                    control_db.commit()
                    session_id = item.id
                    expires_at = item.expires_at
            finally:
                _session_unlock(connection, capacity_lock_id)

            try:
                # create_setup_sandbox commits its ledger before Docker. The
                # connection holding our advisory lock is idle during this I/O.
                resource = sandboxes.create_setup_sandbox(
                    db,
                    owner_id=session_id,
                    environment_id=environment_id,
                    image=image,
                    base_version_id=base_version_id,
                    base_version_no=base_version_no,
                    hard_expires_at=expires_at,
                )
            except DomainError as exc:
                with Session(bind=connection) as control_db:
                    current = control_db.get(EnvironmentSetupSession, session_id)
                    if current is not None and current.state == "STARTING":
                        current.state = "FAILED"
                        current.error_detail = exc.message
                    control_db.commit()
                raise

            with Session(bind=connection, expire_on_commit=False) as control_db:
                current = control_db.scalar(
                    select(EnvironmentSetupSession)
                    .where(EnvironmentSetupSession.id == session_id)
                    .with_for_update()
                )
                if current is None or current.state != "STARTING":
                    sandboxes.request_delete_durable(db, resource.id)
                    raise DomainError(
                        "ENVIRONMENT_SETUP_STALE",
                        "The setup session changed while its sandbox was starting",
                        409,
                    )
                current.sandbox_id = resource.id
                current.container_id = resource.backend_resource_name
                current.state = "RUNNING"
                current.error_detail = None
                control_db.commit()
                result = _session_dict(current)
            return result
        finally:
            _session_unlock(connection, lock_id)


def get_setup_session(db: Session, session_id: str) -> EnvironmentSetupSession:
    item = db.get(EnvironmentSetupSession, session_id)
    if item is None:
        raise not_found("environment_setup_session", session_id)
    return item


def setup_sandbox_owner_is_active(
    db: Session,
    owner_id: str,
    sandbox_id: str,
    *,
    created_at: datetime,
    now: datetime,
    binding_grace_seconds: int,
) -> bool:
    """Report whether a setup session still authorizes its sandbox.

    The setup-session row and sandbox ledger are committed in separate phases.
    During that bounded interval the owner row may still be invisible to the
    control-plane transaction, so a newly-created sandbox remains provisionally
    active until the binding grace period ends.
    """

    item = db.get(EnvironmentSetupSession, owner_id)
    provisioning = bool(
        (item is None or (item.sandbox_id is None and item.state == "STARTING"))
        and created_at + timedelta(seconds=binding_grace_seconds) > now
    )
    return bool(
        provisioning
        or (
            item is not None
            and item.sandbox_id == sandbox_id
            and item.state in {"STARTING", "RUNNING"}
        )
    )


def terminal_session_details(
    db: Session, session_id: str
) -> tuple[str, str, str | None, str, datetime]:
    """Expire stale setup containers before allowing a terminal attachment."""

    _expire_setup_sessions(db)
    item = get_setup_session(db, session_id)
    return (
        item.state,
        item.container_id,
        item.sandbox_id,
        item.environment_id,
        item.expires_at,
    )


def publish_setup_session(db: Session, session_id: str) -> dict[str, Any]:
    db.rollback()
    engine = _control_engine(db)
    with engine.connect() as connection:
        lock_id = _session_lock(connection, f"ENVIRONMENT_PUBLISH:{session_id}")
        try:
            with Session(bind=connection, expire_on_commit=False) as control_db:
                item = control_db.scalar(
                    select(EnvironmentSetupSession)
                    .where(EnvironmentSetupSession.id == session_id)
                    .with_for_update()
                )
                if item is None:
                    raise not_found("environment_setup_session", session_id)
                if item.state == "PUBLISHED" and item.published_version_id:
                    published_version = control_db.get(
                        EnvironmentVersion, item.published_version_id
                    )
                    if published_version is not None and published_version.state == "READY":
                        return _version_dict(published_version)
                if item.expires_at <= datetime.now(UTC):
                    item.state = "EXPIRED"
                    _enqueue_setup_cleanup(control_db, item)
                    control_db.commit()
                    raise DomainError(
                        "ENVIRONMENT_SETUP_NOT_RUNNING",
                        "The setup session expired before it could be published",
                        409,
                    )
                if item.state != "RUNNING" or not item.container_id:
                    raise DomainError(
                        "ENVIRONMENT_SETUP_NOT_RUNNING",
                        "Only a running setup session can be published",
                        409,
                    )
                if not item.sandbox_id:
                    raise DomainError(
                        "ENVIRONMENT_SANDBOX_MISSING",
                        "The setup session has no managed sandbox ledger entry",
                        409,
                    )
                environment = control_db.scalar(
                    select(TerminalEnvironment)
                    .where(TerminalEnvironment.id == item.environment_id)
                    .with_for_update()
                )
                if environment is None or environment.deleted_at is not None:
                    raise not_found("terminal_environment", item.environment_id)
                version = (
                    control_db.get(EnvironmentVersion, item.published_version_id)
                    if item.published_version_id
                    else None
                )
                if version is None:
                    persisted_max = int(
                        control_db.scalar(
                            select(func.coalesce(func.max(EnvironmentVersion.version_no), 0)).where(
                                EnvironmentVersion.environment_id == item.environment_id
                            )
                        )
                        or 0
                    )
                    version_no = max(environment.last_version_no, persisted_max) + 1
                    environment.last_version_no = version_no
                    version = EnvironmentVersion(
                        environment_id=item.environment_id,
                        version_no=version_no,
                        parent_version_id=item.base_version_id,
                        state="PUBLISHING",
                    )
                    control_db.add(version)
                    control_db.flush()
                    item.published_version_id = version.id
                else:
                    version.state = "PUBLISHING"
                    version.error_detail = None
                resource_name = item.container_id
                sandbox_id = item.sandbox_id
                environment_id = item.environment_id
                version_id = version.id
                version_no = version.version_no
                control_db.commit()

            try:
                # The advisory-lock connection is idle while the controller or
                # local Docker performs scans and image commit.
                published = docker.publish_setup_container(
                    resource_name,
                    sandbox_id=sandbox_id,
                    environment_id=environment_id,
                    version_id=version_id,
                    version_no=version_no,
                )
            except DomainError as exc:
                with Session(bind=connection) as control_db:
                    failed = control_db.get(EnvironmentVersion, version_id)
                    if failed is not None and failed.state == "PUBLISHING":
                        failed.state = "FAILED"
                        failed.error_detail = exc.message
                    control_db.commit()
                raise

            with Session(bind=connection, expire_on_commit=False) as control_db:
                current = control_db.scalar(
                    select(EnvironmentSetupSession)
                    .where(EnvironmentSetupSession.id == session_id)
                    .with_for_update()
                )
                current_version = control_db.get(EnvironmentVersion, version_id)
                if (
                    current is None
                    or current.state != "RUNNING"
                    or current.published_version_id != version_id
                    or current_version is None
                    or current_version.state != "PUBLISHING"
                ):
                    if current_version is not None:
                        current_version.state = "FAILED"
                        current_version.error_detail = "Publishing result became stale"
                    _enqueue_image_cleanup(
                        control_db,
                        environment_id=environment_id,
                        version_id=version_id,
                        version_no=version_no,
                        image_reference=published.reference,
                        image_digest=published.digest,
                    )
                    control_db.commit()
                    raise DomainError(
                        "ENVIRONMENT_PUBLISH_STALE",
                        "The setup session changed while its image was publishing",
                        409,
                    )
                current_version.image_reference = published.reference
                current_version.image_digest = published.digest
                current_version.manifest_json = published.manifest
                current_version.state = "READY"
                current_version.error_detail = None
                current.state = "PUBLISHED"
                _enqueue_setup_cleanup(control_db, current)
                control_db.commit()
                return _version_dict(current_version)
        finally:
            _session_unlock(connection, lock_id)


def stop_setup_session(db: Session, session_id: str) -> None:
    item = get_setup_session(db, session_id)
    if item.state in {"STARTING", "RUNNING"}:
        item.state = "CANCELLED"
    _enqueue_setup_cleanup(db, item)
    finish(db)


def delete_environment_version(db: Session, environment_id: str, version_id: str) -> None:
    parent = db.scalar(
        select(TerminalEnvironment)
        .where(TerminalEnvironment.id == environment_id)
        .with_for_update()
    )
    if parent is None or parent.deleted_at is not None:
        raise not_found("terminal_environment", environment_id)
    version = db.scalar(
        select(EnvironmentVersion)
        .where(
            EnvironmentVersion.id == version_id,
            EnvironmentVersion.environment_id == environment_id,
        )
        .with_for_update()
    )
    if version is None:
        raise not_found("environment_version", version_id)

    node_reference_count = int(
        db.scalar(
            select(func.count(NodeAsset.id)).where(
                NodeAsset.environment_version_id == version.id,
                NodeAsset.deleted_at.is_(None),
            )
        )
        or 0
    )
    run_reference_count = int(
        db.scalar(
            select(func.count(FlowRun.id)).where(FlowRun.environment_version_id == version.id)
        )
        or 0
    )
    setup_reference = db.scalar(
        select(EnvironmentSetupSession.id).where(
            EnvironmentSetupSession.base_version_id == version.id,
            (EnvironmentSetupSession.state.in_(["STARTING", "RUNNING"]))
            | (EnvironmentSetupSession.container_id != ""),
        )
    )
    validation_reference_count = int(
        db.scalar(
            select(func.count(CapabilityValidation.id)).where(
                CapabilityValidation.environment_version_id == version.id
            )
        )
        or 0
    )
    oauth_reference_count = int(
        db.scalar(
            select(func.count(MCPOAuthSecretReference.id)).where(
                MCPOAuthSecretReference.environment_version_id == version.id
            )
        )
        or 0
    )
    if (
        node_reference_count
        or run_reference_count
        or setup_reference is not None
        or validation_reference_count
        or oauth_reference_count
    ):
        details: dict[str, Any] = {
            "environment_id": environment_id,
            "version_id": version.id,
            "node_reference_count": node_reference_count,
            "run_reference_count": run_reference_count,
        }
        if setup_reference is not None:
            details["setup_reference_count"] = 1
        if validation_reference_count:
            details["capability_validation_reference_count"] = validation_reference_count
        if oauth_reference_count:
            details["mcp_oauth_secret_reference_count"] = oauth_reference_count
        raise DomainError(
            "ENVIRONMENT_VERSION_IN_USE",
            "The terminal environment version is still referenced",
            409,
            details,
        )

    # These links preserve provenance only. They do not make an otherwise
    # unused image part of a live runtime contract.
    db.execute(
        update(EnvironmentVersion)
        .where(EnvironmentVersion.parent_version_id == version.id)
        .values(parent_version_id=None)
    )
    db.execute(
        update(EnvironmentSetupSession)
        .where(EnvironmentSetupSession.base_version_id == version.id)
        .values(base_version_id=None)
    )
    # Soft-deleted nodes are not user-visible consumers, but their nullable
    # metadata link must be cleared before the RESTRICT foreign key is checked.
    db.execute(
        update(NodeAsset)
        .where(
            NodeAsset.environment_version_id == version.id,
            NodeAsset.deleted_at.is_not(None),
        )
        .values(environment_version_id=None)
    )
    image_reference = version.image_reference
    image_digest = version.image_digest
    environment_id = version.environment_id
    version_no = version.version_no
    version_id = version.id
    db.delete(version)
    if image_reference and image_digest:
        _enqueue_image_cleanup(
            db,
            environment_id=environment_id,
            version_id=version_id,
            version_no=version_no,
            image_reference=image_reference,
            image_digest=image_digest,
        )
    finish(db)


def delete_environment(db: Session, environment_id: str) -> None:
    item = db.scalar(
        select(TerminalEnvironment)
        .where(TerminalEnvironment.id == environment_id)
        .with_for_update()
    )
    if item is None or item.deleted_at is not None:
        raise not_found("terminal_environment", environment_id)
    versions = list(
        db.scalars(
            select(EnvironmentVersion)
            .where(EnvironmentVersion.environment_id == environment_id)
            .order_by(EnvironmentVersion.id)
            .with_for_update()
        )
    )
    version_ids = [version.id for version in versions]
    oauth_reference_count = int(
        db.scalar(
            select(func.count(MCPOAuthSecretReference.id)).where(
                MCPOAuthSecretReference.environment_version_id.in_(version_ids)
            )
        )
        or 0
    )
    if oauth_reference_count:
        raise DomainError(
            "ENVIRONMENT_IN_USE",
            "The terminal environment is referenced by MCP OAuth audit history",
            409,
            {"mcp_oauth_secret_reference_count": oauth_reference_count},
        )
    referenced = db.scalar(
        select(NodeAsset.id).where(
            NodeAsset.environment_version_id.in_(version_ids),
            NodeAsset.deleted_at.is_(None),
        )
    )
    if referenced:
        raise DomainError(
            "ENVIRONMENT_IN_USE",
            "The terminal environment is referenced by an active node",
            409,
        )
    run_reference = db.scalar(
        select(FlowRun.id).where(FlowRun.environment_version_id.in_(version_ids))
    )
    if run_reference:
        raise DomainError(
            "ENVIRONMENT_IN_USE",
            "The terminal environment is referenced by a flow run",
            409,
        )
    active_sessions = list(
        db.scalars(
            select(EnvironmentSetupSession).where(
                EnvironmentSetupSession.environment_id == environment_id,
                EnvironmentSetupSession.state.in_(["STARTING", "RUNNING"]),
            )
        )
    )
    if active_sessions:
        raise DomainError(
            "ENVIRONMENT_SETUP_ACTIVE",
            "Stop the active setup session before deleting the environment",
            409,
            {"session_ids": [session.id for session in active_sessions]},
        )
    db.execute(
        update(EnvironmentSetupSession)
        .where(EnvironmentSetupSession.environment_id == environment_id)
        .values(base_version_id=None, published_version_id=None)
    )
    db.execute(
        update(EnvironmentVersion)
        .where(EnvironmentVersion.environment_id == environment_id)
        .values(parent_version_id=None)
    )
    db.execute(
        update(NodeAsset)
        .where(
            NodeAsset.environment_version_id.in_(version_ids),
            NodeAsset.deleted_at.is_not(None),
        )
        .values(environment_version_id=None)
    )
    for version in versions:
        if version.image_reference and version.image_digest:
            _enqueue_image_cleanup(
                db,
                environment_id=environment_id,
                version_id=version.id,
                version_no=version.version_no,
                image_reference=version.image_reference,
                image_digest=version.image_digest,
            )
        db.delete(version)
    item.deleted_at = datetime.now(UTC)
    item.row_version += 1
    _enqueue_credential_cleanup(db, environment_id)
    finish(db)
