from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import func, select, update
from sqlalchemy.orm import Session

from flowweave.modules.environments.infrastructure import docker
from flowweave.shared.application.transactions import (
    finish,
    register_commit_action,
    register_rollback_action,
)
from flowweave.shared.errors import DomainError, conflict, not_found
from flowweave.shared.models import (
    EnvironmentSetupSession,
    EnvironmentVersion,
    FlowRun,
    NodeAsset,
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


def _expire_setup_sessions(db: Session, environment_id: str | None = None) -> int:
    query = select(EnvironmentSetupSession).where(
        EnvironmentSetupSession.state.in_(["STARTING", "RUNNING"]),
        EnvironmentSetupSession.expires_at <= datetime.now(UTC),
    )
    if environment_id is not None:
        query = query.where(EnvironmentSetupSession.environment_id == environment_id)
    expired = list(db.scalars(query.with_for_update()))
    for item in expired:
        if item.container_id:
            register_commit_action(
                db,
                lambda container_id=item.container_id: docker.remove_runtime_container(
                    container_id
                ),
            )
        item.container_id = ""
        item.state = "EXPIRED"
        item.updated_at = datetime.now(UTC)
    return len(expired)


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


def read_environment(db: Session, environment_id: str) -> dict[str, Any]:
    item = _environment(db, environment_id)
    if _expire_setup_sessions(db, environment_id):
        finish(db)
    return environment_dict(db, item)


def save_environment(
    db: Session, payload: TerminalEnvironmentWrite, environment_id: str | None = None
) -> dict[str, Any]:
    base_image = docker.validate_image(payload.base_image)
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
    environment = _environment(db, environment_id)
    if _expire_setup_sessions(db, environment.id):
        db.flush()
    active = db.scalar(
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
    image = environment.base_image
    if base_version_id:
        version = db.scalar(
            select(EnvironmentVersion)
            .where(EnvironmentVersion.id == base_version_id)
            .with_for_update()
        )
        if version is None or version.environment_id != environment.id or version.state != "READY":
            raise DomainError(
                "ENVIRONMENT_VERSION_INVALID",
                "The selected base environment version is unavailable",
                422,
            )
        image = version.image_digest or version.image_reference
    item = EnvironmentSetupSession(
        environment_id=environment.id,
        base_version_id=base_version_id,
        state="STARTING",
        base_image_reference=image,
        expires_at=datetime.now(UTC)
        + timedelta(seconds=get_settings().terminal_environment_session_ttl_seconds),
    )
    db.add(item)
    db.flush()
    try:
        item.container_id = docker.start_setup_container(image, environment.id)
        register_rollback_action(
            db,
            lambda container_id=item.container_id: docker.remove_runtime_container(container_id),
        )
        item.state = "RUNNING"
    except DomainError as exc:
        item.state = "FAILED"
        item.error_detail = exc.message
        finish(db)
        raise
    finish(db)
    return _session_dict(item)


def get_setup_session(db: Session, session_id: str) -> EnvironmentSetupSession:
    item = db.get(EnvironmentSetupSession, session_id)
    if item is None:
        raise not_found("environment_setup_session", session_id)
    return item


def terminal_session_details(db: Session, session_id: str) -> tuple[str, str, datetime]:
    """Expire stale setup containers before allowing a terminal attachment."""

    _expire_setup_sessions(db)
    item = get_setup_session(db, session_id)
    return item.state, item.container_id, item.expires_at


def publish_setup_session(db: Session, session_id: str) -> dict[str, Any]:
    if _expire_setup_sessions(db):
        finish(db)
    item = get_setup_session(db, session_id)
    if item.state == "PUBLISHED":
        # Publishing is idempotent. A browser may retry after the first request
        # succeeded but before its response reached the UI (for example when the
        # API container or WebSocket reconnects during the commit). Return the
        # version created by this setup session instead of turning that harmless
        # retry into ENVIRONMENT_SETUP_NOT_RUNNING.
        published_version = db.scalar(
            select(EnvironmentVersion)
            .where(
                EnvironmentVersion.environment_id == item.environment_id,
                EnvironmentVersion.state == "READY",
                EnvironmentVersion.created_at >= item.created_at,
            )
            .order_by(EnvironmentVersion.created_at.asc())
            .limit(1)
        )
        if published_version is not None:
            return _version_dict(published_version)
    if item.state != "RUNNING" or not item.container_id:
        raise DomainError(
            "ENVIRONMENT_SETUP_NOT_RUNNING",
            "Only a running setup session can be published",
            409,
        )
    environment = db.scalar(
        select(TerminalEnvironment)
        .where(TerminalEnvironment.id == item.environment_id)
        .with_for_update()
    )
    if environment is None or environment.deleted_at is not None:
        raise not_found("terminal_environment", item.environment_id)
    persisted_max = int(
        db.scalar(
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
    db.add(version)
    db.flush()
    try:
        published = docker.publish_container(
            item.container_id, environment_id=item.environment_id, version_no=version_no
        )
        version.image_reference = published.reference
        version.image_digest = published.digest
        version.manifest_json = published.manifest
        version.state = "READY"
        item.state = "PUBLISHED"
        register_rollback_action(
            db,
            lambda reference=published.reference: docker.remove_image(reference),
        )
        register_commit_action(
            db,
            lambda container_id=item.container_id: docker.remove_runtime_container(container_id),
        )
        item.container_id = ""
    except DomainError as exc:
        version.state = "FAILED"
        version.error_detail = exc.message
        finish(db)
        raise
    finish(db)
    return _version_dict(version)


def stop_setup_session(db: Session, session_id: str) -> None:
    item = get_setup_session(db, session_id)
    if item.container_id:
        register_commit_action(
            db,
            lambda container_id=item.container_id: docker.remove_runtime_container(container_id),
        )
        item.container_id = ""
    if item.state in {"STARTING", "RUNNING"}:
        item.state = "CANCELLED"
    finish(db)


def delete_environment_version(db: Session, environment_id: str, version_id: str) -> None:
    _environment(db, environment_id)
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
    if node_reference_count or run_reference_count:
        raise DomainError(
            "ENVIRONMENT_VERSION_IN_USE",
            "The terminal environment version is still referenced",
            409,
            {
                "environment_id": environment_id,
                "version_id": version.id,
                "node_reference_count": node_reference_count,
                "run_reference_count": run_reference_count,
            },
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
    db.delete(version)
    if image_reference:
        register_commit_action(db, lambda reference=image_reference: docker.remove_image(reference))
    finish(db)


def delete_environment(db: Session, environment_id: str) -> None:
    item = _environment(db, environment_id)
    referenced = db.scalar(
        select(NodeAsset.id).where(
            NodeAsset.environment_version_id.in_(
                select(EnvironmentVersion.id).where(
                    EnvironmentVersion.environment_id == environment_id
                )
            ),
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
        select(FlowRun.id).where(
            FlowRun.environment_version_id.in_(
                select(EnvironmentVersion.id).where(
                    EnvironmentVersion.environment_id == environment_id
                )
            )
        )
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
    for session in active_sessions:
        if session.container_id:
            register_commit_action(
                db,
                lambda container_id=session.container_id: docker.remove_runtime_container(
                    container_id
                ),
            )
        session.container_id = ""
        session.state = "CANCELLED"
    item.deleted_at = datetime.now(UTC)
    item.row_version += 1
    finish(db)
