from __future__ import annotations

import hashlib
import re
import unicodedata
from dataclasses import dataclass
from datetime import timedelta
from typing import Any, cast

from sqlalchemy import select
from sqlalchemy.orm import Session

from flowweave.shared.application.transactions import finish
from flowweave.shared.database import now, uid
from flowweave.shared.errors import DomainError
from flowweave.shared.models import (
    MemorySource,
    MemorySourceVersion,
    MemorySourceVersionReference,
)
from flowweave.shared.schemas import (
    MemorySourceActivateWrite,
    MemorySourceCreateWrite,
    MemorySourceLifecycleWrite,
    MemorySourceReviewWrite,
    MemorySourceRevisionWrite,
    MemorySourceScanWrite,
)

_MAX_CONTENT_BYTES = 256 * 1024
_SCANNER_ID = "flowweave-sensitive-data-v1"
_SENSITIVE_PATTERNS = (
    ("PRIVATE_KEY", re.compile(r"-----BEGIN [A-Z0-9 ]*PRIVATE KEY-----")),
    ("AWS_ACCESS_KEY", re.compile(r"(?<![A-Z0-9])AKIA[0-9A-Z]{16}(?![A-Z0-9])")),
    (
        "SECRET_ASSIGNMENT",
        re.compile(
            r"(?im)\b(?:api[_-]?key|access[_-]?token|client[_-]?secret|password|passwd)"
            r"\s*[:=]\s*[^\s#]{8,}"
        ),
    ),
)


@dataclass(frozen=True, slots=True)
class GovernedMemoryMaterial:
    """Ephemeral bytes authorized by one immutable Run Snapshot."""

    scope: str
    version_id: str
    digest: str
    content: bytes


def register_policy_references(
    db: Session, *, policy_version_id: str, source_refs: list[dict[str, str]]
) -> None:
    """Create immutable retention holds for one published Memory Policy."""

    for source_ref in source_refs:
        version_id = str(source_ref.get("reference_id") or "")
        digest = str(source_ref.get("digest") or "")
        version = db.get(MemorySourceVersion, version_id)
        if (
            version is None
            or version.lifecycle_state != "ACTIVE"
            or version.review_status != "APPROVED"
            or version.sensitive_data_status != "PASSED"
            or version.digest != digest
        ):
            raise DomainError(
                "MEMORY_SOURCE_REFERENCE_INVALID",
                "Memory Policy must reference an active governed source with matching digest",
                422,
                {"memory_source_version_id": version_id},
            )
        existing = db.scalar(
            select(MemorySourceVersionReference.id).where(
                MemorySourceVersionReference.memory_source_version_id == version_id,
                MemorySourceVersionReference.reference_kind == "POLICY_VERSION",
                MemorySourceVersionReference.reference_id == policy_version_id,
            )
        )
        if existing is None:
            db.add(
                MemorySourceVersionReference(
                    memory_source_version_id=version_id,
                    reference_kind="POLICY_VERSION",
                    reference_id=policy_version_id,
                )
            )
    db.flush()


def register_snapshot_references(
    db: Session, *, snapshot_id: str, runtime_manifest: dict[str, Any]
) -> None:
    """Create immutable holds for all Memory versions frozen by a Snapshot."""

    raw_nodes = runtime_manifest.get("nodes")
    if not isinstance(raw_nodes, dict):
        raise DomainError("SNAPSHOT_INVALID", "Snapshot Runtime nodes are invalid", 409)
    nodes = cast(dict[object, object], raw_nodes)
    references: dict[str, str] = {}
    for raw_node in nodes.values():
        if not isinstance(raw_node, dict):
            raise DomainError("SNAPSHOT_INVALID", "Snapshot Runtime node is invalid", 409)
        node = cast(dict[object, object], raw_node)
        raw_agent = node.get("agent_spec")
        # Agent configuration is now frozen on the shared Conversation
        # Binding, not on a Flow node. Current manifests therefore have no
        # node-level Agent Spec and hold no Snapshot-owned Memory references.
        if raw_agent is None:
            continue
        if not isinstance(raw_agent, dict):
            raise DomainError("SNAPSHOT_INVALID", "Snapshot Agent Spec is invalid", 409)
        agent = cast(dict[object, object], raw_agent)
        raw_policy = agent.get("memory_policy")
        policy = cast(dict[object, object], raw_policy) if isinstance(raw_policy, dict) else {}
        raw_config = policy.get("runtime_config")
        config = cast(dict[object, object], raw_config) if isinstance(raw_config, dict) else {}
        raw_refs = config.get("source_refs")
        if not isinstance(raw_refs, list):
            raise DomainError("SNAPSHOT_INVALID", "Snapshot Memory references are invalid", 409)
        for raw_ref in cast(list[object], raw_refs):
            if not isinstance(raw_ref, dict):
                raise DomainError("SNAPSHOT_INVALID", "Snapshot Memory reference is invalid", 409)
            source_ref = cast(dict[object, object], raw_ref)
            version_id = str(source_ref.get("reference_id") or "")
            digest = str(source_ref.get("digest") or "")
            previous = references.setdefault(version_id, digest)
            if previous != digest:
                raise DomainError(
                    "SNAPSHOT_INVALID",
                    "Snapshot freezes conflicting digests for one Memory Source",
                    409,
                    {"memory_source_version_id": version_id},
                )
    for version_id in sorted(references):
        db.add(
            MemorySourceVersionReference(
                memory_source_version_id=version_id,
                reference_kind="RUN_SNAPSHOT",
                reference_id=snapshot_id,
            )
        )
    db.flush()


def resolve_snapshot_material(
    db: Session,
    *,
    snapshot_id: str,
    source_refs: list[dict[str, str]],
    allowed_scopes: set[str],
) -> tuple[GovernedMemoryMaterial, ...]:
    """Resolve only governed versions held by the exact frozen Snapshot.

    The returned bytes are deliberately ephemeral. Callers may materialize a
    digest-scoped read-only bundle for OpenHands' native project-memory loader,
    but must not persist the bytes in Runtime DTOs, manifests, events, or
    ordinary audit records.
    """

    materials: list[GovernedMemoryMaterial] = []
    seen: set[str] = set()
    for source_ref in source_refs:
        version_id = str(source_ref.get("reference_id") or "")
        expected_digest = str(source_ref.get("digest") or "")
        if version_id in seen:
            raise DomainError(
                "MEMORY_SOURCE_INVALID",
                "Frozen Memory Policy contains a duplicate source version",
                409,
                {"memory_source_version_id": version_id},
            )
        seen.add(version_id)
        row = db.execute(
            select(MemorySourceVersion, MemorySource)
            .join(MemorySource, MemorySource.id == MemorySourceVersion.source_id)
            .join(
                MemorySourceVersionReference,
                MemorySourceVersionReference.memory_source_version_id == MemorySourceVersion.id,
            )
            .where(
                MemorySourceVersion.id == version_id,
                MemorySourceVersionReference.reference_kind == "RUN_SNAPSHOT",
                MemorySourceVersionReference.reference_id == snapshot_id,
            )
        ).one_or_none()
        if row is None:
            raise DomainError(
                "MEMORY_SOURCE_REFERENCE_INVALID",
                "Frozen Memory Source is not held by this Run Snapshot",
                409,
                {"memory_source_version_id": version_id},
            )
        version, source = row
        if (
            version.lifecycle_state != "ACTIVE"
            or version.review_status != "APPROVED"
            or version.sensitive_data_status != "PASSED"
        ):
            raise DomainError(
                "MEMORY_SOURCE_UNAVAILABLE",
                "Frozen Memory Source is not active and governed",
                409,
                {"memory_source_version_id": version_id},
            )
        if source.scope not in {"USER", "PROJECT"} or source.scope not in allowed_scopes:
            raise DomainError(
                "MEMORY_SOURCE_SCOPE_INVALID",
                "Frozen Memory Source scope is not authorized by the Memory Policy",
                409,
                {"memory_source_version_id": version_id, "scope": source.scope},
            )
        try:
            content = version.content.encode("utf-8")
        except UnicodeEncodeError as exc:
            raise DomainError(
                "MEMORY_SOURCE_UNAVAILABLE",
                "Frozen Memory Source cannot be encoded as UTF-8",
                409,
                {"memory_source_version_id": version_id},
            ) from exc
        actual_digest = hashlib.sha256(content).hexdigest()
        if actual_digest != expected_digest or version.digest != expected_digest:
            raise DomainError(
                "MEMORY_SOURCE_DIGEST_MISMATCH",
                "Frozen Memory Source digest does not match its immutable content",
                409,
                {"memory_source_version_id": version_id},
            )
        materials.append(
            GovernedMemoryMaterial(
                scope=source.scope,
                version_id=version.id,
                digest=version.digest,
                content=content,
            )
        )
    return tuple(materials)


def _canonical_content(value: str) -> tuple[str, bytes]:
    content = unicodedata.normalize("NFC", value.replace("\r\n", "\n").replace("\r", "\n"))
    if "\x00" in content or any(
        ord(character) < 32 and character not in {"\n", "\t"} for character in content
    ):
        raise DomainError(
            "MEMORY_SOURCE_CONTENT_INVALID",
            "Memory Source content contains unsupported control characters",
            422,
        )
    if not content.strip():
        raise DomainError(
            "MEMORY_SOURCE_CONTENT_INVALID", "Memory Source content cannot be blank", 422
        )
    content = content.rstrip("\n") + "\n"
    encoded = content.encode("utf-8")
    if len(encoded) > _MAX_CONTENT_BYTES:
        raise DomainError(
            "MEMORY_SOURCE_CONTENT_TOO_LARGE",
            "Memory Source content exceeds the 256 KiB limit",
            422,
        )
    return content, encoded


def _version_dict(item: MemorySourceVersion) -> dict[str, Any]:
    return {
        "id": item.id,
        "source_id": item.source_id,
        "previous_version_id": item.previous_version_id,
        "version_no": item.version_no,
        "digest": item.digest,
        "byte_size": item.byte_size,
        "review_status": item.review_status,
        "sensitive_data_status": item.sensitive_data_status,
        "lifecycle_state": item.lifecycle_state,
        "governance_version": item.governance_version,
        "reviewed_by": item.reviewed_by,
        "reviewed_at": item.reviewed_at.isoformat() if item.reviewed_at else None,
        "review_note": item.review_note,
        "sensitive_data_scanner": item.sensitive_data_scanner,
        "sensitive_data_report": item.sensitive_data_report_json,
        "sensitive_data_scanned_at": (
            item.sensitive_data_scanned_at.isoformat() if item.sensitive_data_scanned_at else None
        ),
        "activated_at": item.activated_at.isoformat() if item.activated_at else None,
        "retired_at": item.retired_at.isoformat() if item.retired_at else None,
        "retention_days": item.retention_days,
        "expires_at": item.expires_at.isoformat() if item.expires_at else None,
        "expired_at": item.expired_at.isoformat() if item.expired_at else None,
        "created_at": item.created_at.isoformat(),
    }


def _source_dict(db: Session, item: MemorySource, *, include_versions: bool) -> dict[str, Any]:
    versions = list(
        db.scalars(
            select(MemorySourceVersion)
            .where(MemorySourceVersion.source_id == item.id)
            .order_by(MemorySourceVersion.version_no.desc())
        )
    )
    result: dict[str, Any] = {
        "id": item.id,
        "source_key": item.source_key,
        "display_name": item.display_name,
        "owner_id": item.owner_id,
        "scope": item.scope,
        "scope_key": item.scope_key,
        "latest_version": _version_dict(versions[0]) if versions else None,
        "created_at": item.created_at.isoformat(),
    }
    if include_versions:
        result["versions"] = [_version_dict(version) for version in versions]
    return result


def _new_version(
    source: MemorySource,
    content: str,
    *,
    previous: MemorySourceVersion | None,
) -> MemorySourceVersion:
    canonical, encoded = _canonical_content(content)
    digest = hashlib.sha256(encoded).hexdigest()
    if previous is not None and previous.digest == digest:
        raise DomainError(
            "MEMORY_SOURCE_VERSION_UNCHANGED",
            "Memory Source revision must change the canonical content",
            409,
            {"version_id": previous.id, "digest": digest},
        )
    return MemorySourceVersion(
        id=uid(),
        source_id=source.id,
        previous_version_id=previous.id if previous else None,
        version_no=(previous.version_no + 1) if previous else 1,
        content=canonical,
        digest=digest,
        byte_size=len(encoded),
        review_status="PENDING",
        sensitive_data_status="NOT_SCANNED",
        lifecycle_state="DRAFT",
        governance_version=1,
        sensitive_data_report_json={},
    )


def create_source(db: Session, payload: MemorySourceCreateWrite) -> dict[str, Any]:
    existing = db.scalar(select(MemorySource).where(MemorySource.source_key == payload.source_key))
    if existing is not None:
        raise DomainError(
            "MEMORY_SOURCE_EXISTS",
            "Memory Source key already exists",
            409,
            {"source_id": existing.id},
        )
    source = MemorySource(
        id=uid(),
        source_key=payload.source_key,
        display_name=payload.display_name.strip(),
        owner_id=payload.owner_id.strip(),
        scope=payload.scope,
        scope_key=payload.scope_key.strip(),
    )
    db.add(source)
    db.flush()
    db.add(_new_version(source, payload.content, previous=None))
    db.flush()
    result = _source_dict(db, source, include_versions=True)
    finish(db)
    return result


def list_sources(db: Session) -> list[dict[str, Any]]:
    return [
        _source_dict(db, item, include_versions=False)
        for item in db.scalars(
            select(MemorySource).order_by(
                MemorySource.scope, MemorySource.scope_key, MemorySource.source_key
            )
        )
    ]


def read_source(db: Session, source_id: str) -> dict[str, Any]:
    item = db.get(MemorySource, source_id)
    if item is None:
        raise DomainError("MEMORY_SOURCE_NOT_FOUND", "Memory Source was not found", 404)
    return _source_dict(db, item, include_versions=True)


def create_revision(
    db: Session, source_id: str, payload: MemorySourceRevisionWrite
) -> dict[str, Any]:
    source = db.scalar(select(MemorySource).where(MemorySource.id == source_id).with_for_update())
    if source is None:
        raise DomainError("MEMORY_SOURCE_NOT_FOUND", "Memory Source was not found", 404)
    previous = db.scalar(
        select(MemorySourceVersion)
        .where(MemorySourceVersion.source_id == source.id)
        .order_by(MemorySourceVersion.version_no.desc())
        .limit(1)
    )
    if previous is None:
        raise DomainError(
            "MEMORY_SOURCE_INVALID", "Memory Source has no immutable content version", 409
        )
    version = _new_version(source, payload.content, previous=previous)
    db.add(version)
    db.flush()
    result = _version_dict(version)
    finish(db)
    return result


def _locked_version(
    db: Session, source_id: str, version_id: str
) -> tuple[MemorySource, MemorySourceVersion]:
    source = db.scalar(select(MemorySource).where(MemorySource.id == source_id).with_for_update())
    if source is None:
        raise DomainError("MEMORY_SOURCE_NOT_FOUND", "Memory Source was not found", 404)
    version = db.scalar(
        select(MemorySourceVersion)
        .where(MemorySourceVersion.id == version_id, MemorySourceVersion.source_id == source.id)
        .with_for_update()
    )
    if version is None:
        raise DomainError(
            "MEMORY_SOURCE_VERSION_NOT_FOUND", "Memory Source version was not found", 404
        )
    return source, version


def _check_governance_version(item: MemorySourceVersion, expected: int) -> None:
    if item.governance_version != expected:
        raise DomainError(
            "MEMORY_SOURCE_GOVERNANCE_VERSION_CONFLICT",
            "Memory Source governance changed; refresh before retrying",
            409,
            {"expected": expected, "actual": item.governance_version},
        )


def review_version(
    db: Session,
    source_id: str,
    version_id: str,
    payload: MemorySourceReviewWrite,
    actor: str,
) -> dict[str, Any]:
    source, version = _locked_version(db, source_id, version_id)
    _check_governance_version(version, payload.expected_governance_version)
    if version.review_status != "PENDING":
        raise DomainError(
            "MEMORY_SOURCE_REVIEW_FINAL", "Memory Source review is already final", 409
        )
    reviewer = actor.strip()
    if not reviewer:
        raise DomainError(
            "MEMORY_SOURCE_REVIEW_ACTOR_REQUIRED",
            "Memory Source review requires a non-blank actor identity",
            422,
        )
    if reviewer == source.owner_id:
        raise DomainError(
            "MEMORY_SOURCE_SELF_REVIEW_FORBIDDEN",
            "Memory Source owners cannot review their own content",
            403,
        )
    version.review_status = "APPROVED" if payload.decision == "APPROVE" else "REJECTED"
    version.reviewed_by = reviewer
    version.reviewed_at = now()
    version.review_note = payload.note.strip() or None
    version.governance_version += 1
    db.flush()
    result = _version_dict(version)
    finish(db)
    return result


def scan_version(
    db: Session, source_id: str, version_id: str, payload: MemorySourceScanWrite
) -> dict[str, Any]:
    _, version = _locked_version(db, source_id, version_id)
    _check_governance_version(version, payload.expected_governance_version)
    if version.sensitive_data_status != "NOT_SCANNED":
        raise DomainError(
            "MEMORY_SOURCE_SCAN_FINAL", "Memory Source sensitive-data scan is already final", 409
        )
    findings = [
        {"category": category, "count": len(pattern.findall(version.content))}
        for category, pattern in _SENSITIVE_PATTERNS
        if pattern.search(version.content)
    ]
    finding_count = sum(int(finding["count"]) for finding in findings)
    version.sensitive_data_status = "BLOCKED" if findings else "PASSED"
    version.sensitive_data_scanner = _SCANNER_ID
    version.sensitive_data_report_json = {
        "scanner": _SCANNER_ID,
        "finding_count": finding_count,
        "findings": findings,
    }
    version.sensitive_data_scanned_at = now()
    version.governance_version += 1
    db.flush()
    result = _version_dict(version)
    finish(db)
    return result


def activate_version(
    db: Session, source_id: str, version_id: str, payload: MemorySourceActivateWrite
) -> dict[str, Any]:
    _, version = _locked_version(db, source_id, version_id)
    _check_governance_version(version, payload.expected_governance_version)
    if version.lifecycle_state != "DRAFT":
        raise DomainError(
            "MEMORY_SOURCE_LIFECYCLE_FINAL",
            "Only a draft Memory Source version can be activated",
            409,
        )
    if version.review_status != "APPROVED" or version.sensitive_data_status != "PASSED":
        raise DomainError(
            "MEMORY_SOURCE_ACTIVATION_BLOCKED",
            "Memory Source activation requires approved review and a passed sensitive-data scan",
            409,
            {
                "review_status": version.review_status,
                "sensitive_data_status": version.sensitive_data_status,
            },
        )
    activated_at = now()
    current = db.scalar(
        select(MemorySourceVersion)
        .where(
            MemorySourceVersion.source_id == source_id,
            MemorySourceVersion.lifecycle_state == "ACTIVE",
            MemorySourceVersion.id != version.id,
        )
        .with_for_update()
    )
    if current is not None:
        if current.retention_days is None:
            raise DomainError(
                "MEMORY_SOURCE_RETENTION_INVALID",
                "Active Memory Source version has no frozen retention period",
                409,
            )
        current.lifecycle_state = "RETIRED"
        current.retired_at = activated_at
        current.expires_at = activated_at + timedelta(days=current.retention_days)
        current.governance_version += 1
        db.flush()
    version.lifecycle_state = "ACTIVE"
    version.activated_at = activated_at
    version.retention_days = payload.retention_days
    version.governance_version += 1
    db.flush()
    result = _version_dict(version)
    finish(db)
    return result


def retire_version(
    db: Session, source_id: str, version_id: str, payload: MemorySourceLifecycleWrite
) -> dict[str, Any]:
    _, version = _locked_version(db, source_id, version_id)
    _check_governance_version(version, payload.expected_governance_version)
    if version.lifecycle_state != "ACTIVE":
        raise DomainError(
            "MEMORY_SOURCE_RETIREMENT_BLOCKED",
            "Only an active Memory Source version can be retired",
            409,
        )
    if version.retention_days is None:
        raise DomainError(
            "MEMORY_SOURCE_RETENTION_INVALID",
            "Active Memory Source version has no frozen retention period",
            409,
        )
    retired_at = now()
    version.lifecycle_state = "RETIRED"
    version.retired_at = retired_at
    version.expires_at = retired_at + timedelta(days=version.retention_days)
    version.governance_version += 1
    db.flush()
    result = _version_dict(version)
    finish(db)
    return result


def expire_version(
    db: Session, source_id: str, version_id: str, payload: MemorySourceLifecycleWrite
) -> dict[str, Any]:
    _, version = _locked_version(db, source_id, version_id)
    _check_governance_version(version, payload.expected_governance_version)
    current_time = now()
    if (
        version.lifecycle_state != "RETIRED"
        or version.expires_at is None
        or version.expires_at > current_time
    ):
        raise DomainError(
            "MEMORY_SOURCE_NOT_EXPIRED",
            "Memory Source content cannot expire before its frozen retention period ends",
            409,
            {"expires_at": version.expires_at.isoformat() if version.expires_at else None},
        )
    version.lifecycle_state = "EXPIRED"
    version.expired_at = current_time
    version.governance_version += 1
    db.flush()
    result = _version_dict(version)
    finish(db)
    return result


def delete_version_content(
    db: Session, source_id: str, version_id: str, payload: MemorySourceLifecycleWrite
) -> dict[str, Any]:
    source, version = _locked_version(db, source_id, version_id)
    _check_governance_version(version, payload.expected_governance_version)
    if version.lifecycle_state != "EXPIRED":
        raise DomainError(
            "MEMORY_SOURCE_DELETION_BLOCKED",
            "Only expired Memory Source content can be irreversibly deleted",
            409,
        )
    reference = db.scalar(
        select(MemorySourceVersionReference.id)
        .where(MemorySourceVersionReference.memory_source_version_id == version.id)
        .limit(1)
    )
    if reference is not None:
        raise DomainError(
            "MEMORY_SOURCE_REFERENCED",
            "Memory Source content is retained by an immutable policy or Run Snapshot",
            409,
        )
    successor = db.scalar(
        select(MemorySourceVersion.id)
        .where(MemorySourceVersion.previous_version_id == version.id)
        .limit(1)
    )
    if successor is not None:
        raise DomainError(
            "MEMORY_SOURCE_HAS_SUCCESSOR",
            "Memory Source version is retained by a later immutable version",
            409,
            {"successor_version_id": successor},
        )
    result = {"id": version.id, "source_id": source.id, "deleted": True}
    db.delete(version)
    db.flush()
    remaining = db.scalar(
        select(MemorySourceVersion.id).where(MemorySourceVersion.source_id == source.id).limit(1)
    )
    if remaining is None:
        db.delete(source)
    finish(db)
    return result
