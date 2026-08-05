from __future__ import annotations

import base64
import hashlib
import io
import json
import zipfile
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path, PurePosixPath
from secrets import token_urlsafe
from typing import Any, cast

import yaml
from sqlalchemy import select, update
from sqlalchemy.orm import Session

from flowweave.modules.tasks.public import enqueue
from flowweave.shared.application.transactions import (
    finish,
    register_commit_action,
    register_rollback_action,
)
from flowweave.shared.artifact_store import get_artifact_store
from flowweave.shared.errors import DomainError
from flowweave.shared.models import CapabilityImport
from flowweave.shared.schemas import CapabilityValidateWrite
from flowweave.shared.settings import get_settings

SENSITIVE = {"api_key", "apikey", "token", "secret", "password", "authorization"}
ZIP_MAX_COMPRESSED_BYTES = 5 * 1024 * 1024
ZIP_MAX_EXPANDED_BYTES = 20 * 1024 * 1024
ZIP_MAX_FILE_BYTES = 2 * 1024 * 1024
ZIP_MAX_FILES = 200
ZIP_MAX_DEPTH = 8
CONFIG_MAX_BYTES = 1024 * 1024
CONFIG_MAX_ALIASES = 20
CONFIG_MAX_DEPTH = 20
CONFIG_MAX_NODES = 10_000
NESTED_ARCHIVE_SUFFIXES = {
    ".zip",
    ".tar",
    ".gz",
    ".tgz",
    ".bz2",
    ".xz",
    ".7z",
    ".rar",
    ".jar",
    ".whl",
}
SKILL_ALLOWED_SUFFIXES = {
    ".md",
    ".txt",
    ".json",
    ".yaml",
    ".yml",
    ".toml",
    ".py",
    ".js",
    ".mjs",
    ".cjs",
    ".ts",
    ".sh",
    ".svg",
    ".png",
    ".jpg",
    ".jpeg",
    ".gif",
}
SKILL_ALLOWED_EXTENSIONLESS = {"license", "notice"}


def _reject(message: str, **details: Any) -> DomainError:
    return DomainError("IMPORT_REJECTED", message, 422, details)


def _reject_sensitive(value: object, path: str = "$") -> None:
    if isinstance(value, dict):
        mapping = cast(dict[object, object], value)
        for key, child in mapping.items():
            if str(key).lower() in SENSITIVE:
                raise _reject("Capability config contains a secret field", path=f"{path}.{key}")
            _reject_sensitive(child, f"{path}.{key}")
    elif isinstance(value, list):
        sequence = cast(list[object], value)
        for index, child in enumerate(sequence):
            _reject_sensitive(child, f"{path}[{index}]")


def _validate_config_structure(value: object) -> None:
    count = 0

    def walk(current: object, depth: int, ancestors: frozenset[int]) -> None:
        nonlocal count
        count += 1
        if count > CONFIG_MAX_NODES:
            raise _reject("Config expands to too many values")
        if depth > CONFIG_MAX_DEPTH:
            raise _reject("Config nesting exceeds limit")
        if isinstance(current, dict):
            mapping = cast(dict[object, object], current)
            identity = id(mapping)
            if identity in ancestors:
                raise _reject("Recursive YAML aliases are not allowed")
            nested_ancestors = ancestors | {identity}
            for key, child in mapping.items():
                walk(key, depth + 1, nested_ancestors)
                walk(child, depth + 1, nested_ancestors)
        elif isinstance(current, list):
            sequence = cast(list[object], current)
            identity = id(sequence)
            if identity in ancestors:
                raise _reject("Recursive YAML aliases are not allowed")
            nested_ancestors = ancestors | {identity}
            for child in sequence:
                walk(child, depth + 1, nested_ancestors)

    walk(value, 0, frozenset())


def _safe_yaml_load(text: str) -> object:
    try:
        aliases = sum(isinstance(token, yaml.tokens.AliasToken) for token in yaml.scan(text))
    except yaml.YAMLError as exc:
        raise _reject("Invalid JSON or YAML") from exc
    if aliases > CONFIG_MAX_ALIASES:
        raise _reject("YAML alias count exceeds limit")
    try:
        value = yaml.safe_load(text)
    except yaml.YAMLError as exc:
        raise _reject("Invalid JSON or YAML") from exc
    _validate_config_structure(value)
    return value


def _frontmatter(text: str) -> dict[str, Any]:
    if not text.startswith("---"):
        return {}
    parts = text.split("---", 2)
    if len(parts) < 3:
        return {}
    try:
        value = _safe_yaml_load(parts[1])
    except DomainError as exc:
        raise _reject("SKILL.md frontmatter is invalid") from exc
    return cast(dict[str, Any], value) if isinstance(value, dict) else {}


def _validate_skill(content: bytes) -> dict[str, Any]:
    if len(content) > ZIP_MAX_COMPRESSED_BYTES:
        raise _reject("ZIP exceeds 5 MiB")
    try:
        with zipfile.ZipFile(io.BytesIO(content)) as archive:
            files = archive.infolist()
            if len(files) > ZIP_MAX_FILES:
                raise _reject("ZIP has too many files")
            names: list[str] = []
            total = 0
            for item in files:
                name = item.filename.replace("\\", "/")
                path = PurePosixPath(name)
                if path.is_absolute() or ".." in path.parts:
                    raise _reject("Unsafe ZIP path")
                if len(path.parts) > ZIP_MAX_DEPTH:
                    raise _reject("ZIP path nesting exceeds limit", filename=name)
                if (item.external_attr >> 16) & 0o170000 == 0o120000:
                    raise _reject("Symbolic links are not allowed")
                if item.file_size > ZIP_MAX_FILE_BYTES:
                    raise _reject("ZIP file exceeds per-file limit", filename=name)
                total += item.file_size
                if total > ZIP_MAX_EXPANDED_BYTES:
                    raise _reject("Expanded ZIP exceeds limit")
                if not item.is_dir():
                    suffix = path.suffix.lower()
                    if suffix in NESTED_ARCHIVE_SUFFIXES:
                        raise _reject("Nested archives are not allowed", filename=name)
                    if (
                        suffix not in SKILL_ALLOWED_SUFFIXES
                        and path.name.lower() not in SKILL_ALLOWED_EXTENSIONLESS
                    ):
                        raise _reject("ZIP file extension is not allowed", filename=name)
                    names.append(name)
            skill_files = sorted(x for x in names if x == "SKILL.md" or x.endswith("/SKILL.md"))
            if not skill_files:
                raise _reject("SKILL.md is required")
            capabilities: list[dict[str, Any]] = []
            for skill_file in skill_files:
                try:
                    text = archive.read(skill_file).decode("utf-8")
                except (KeyError, UnicodeDecodeError) as exc:
                    raise _reject("SKILL.md must be UTF-8") from exc
                metadata = _frontmatter(text)
                parent = PurePosixPath(skill_file).parent.name
                key = str(metadata.get("name") or parent or "skill").strip()
                if not key or len(key) > 200:
                    raise _reject("Skill name is invalid", skill_file=skill_file)
                capabilities.append(
                    {
                        "capability_key": key,
                        "normalized_config": {
                            "entry": skill_file,
                            "description": str(metadata.get("description") or "")[:2000],
                            "version": str(metadata.get("version") or ""),
                        },
                    }
                )
            return {
                "capabilities": capabilities,
                "skill_files": skill_files,
                "files": names[:50],
                "file_count": len(names),
            }
    except zipfile.BadZipFile as exc:
        raise _reject("Invalid ZIP file") from exc


def _named_mapping(value: object, label: str) -> list[dict[str, Any]]:
    if not isinstance(value, dict) or not value:
        raise _reject(f"{label} config must contain at least one named item")
    mapping = cast(dict[object, object], value)
    return [
        {
            "capability_key": str(key),
            "normalized_config": (
                cast(dict[str, Any], config) if isinstance(config, dict) else {"value": config}
            ),
        }
        for key, config in mapping.items()
        if str(key).strip()
    ]


def _config_capabilities(capability_type: str, parsed: dict[str, Any]) -> list[dict[str, Any]]:
    if capability_type == "MCP":
        servers = cast(object, parsed.get("mcpServers", parsed.get("servers")))
        return _named_mapping(servers, "MCP")
    hooks = cast(object, parsed.get("hooks"))
    if isinstance(hooks, list):
        result: list[dict[str, Any]] = []
        for index, raw_hook in enumerate(cast(list[object], hooks)):
            if not isinstance(raw_hook, dict):
                raise _reject("Hook entries must be objects")
            hook = cast(dict[str, Any], raw_hook)
            key = str(
                hook.get("name") or hook.get("id") or hook.get("event") or f"hook-{index + 1}"
            )
            result.append({"capability_key": key, "normalized_config": hook})
        if result:
            return result
        raise _reject("Hook config must contain at least one item")
    return _named_mapping(hooks, "Hook")


def _decode_and_validate(payload: CapabilityValidateWrite) -> tuple[bytes, dict[str, Any]]:
    try:
        content = base64.b64decode(payload.content_base64, validate=True)
    except (ValueError, TypeError) as exc:
        raise _reject("Invalid base64 content") from exc
    filename = Path(payload.filename).name
    if filename != payload.filename or not filename:
        raise _reject("Invalid filename")
    if payload.capability_type == "SKILL":
        if not filename.lower().endswith(".zip"):
            raise _reject("Skill must be a ZIP")
        return content, _validate_skill(content)
    suffix = Path(filename).suffix.lower()
    if suffix not in {".json", ".yaml", ".yml"}:
        raise _reject("Config must be JSON or YAML")
    if len(content) > CONFIG_MAX_BYTES:
        raise _reject("Config exceeds 1 MiB")
    try:
        text = content.decode("utf-8")
        parsed_object = (
            cast(object, json.loads(text)) if suffix == ".json" else _safe_yaml_load(text)
        )
        if suffix == ".json":
            _validate_config_structure(parsed_object)
    except (UnicodeDecodeError, json.JSONDecodeError, RecursionError) as exc:
        raise _reject("Invalid JSON or YAML") from exc
    if not isinstance(parsed_object, dict):
        raise _reject("Config root must be an object")
    parsed = cast(dict[str, Any], parsed_object)
    _reject_sensitive(parsed)
    return content, {
        "capabilities": _config_capabilities(payload.capability_type, parsed),
        "config": parsed,
    }


@dataclass(frozen=True, slots=True)
class PreparedCapabilityImport:
    token: str
    token_digest: str
    capability_type: str
    filename: str
    content: bytes
    content_hash: str
    preview: dict[str, Any]
    expires_at: datetime
    storage_key: str | None = None


@dataclass(frozen=True, slots=True)
class CapabilityCommitPlan:
    import_id: str
    source_key: str
    final_key: str
    expired: bool


def prepare_validation(payload: CapabilityValidateWrite) -> PreparedCapabilityImport:
    """Validate and freeze an import before object-store or database work."""

    content, preview = _decode_and_validate(payload)
    token = token_urlsafe(32)
    return PreparedCapabilityImport(
        token=token,
        token_digest=hashlib.sha256(token.encode()).hexdigest(),
        capability_type=payload.capability_type,
        filename=payload.filename,
        content=content,
        content_hash=hashlib.sha256(content).hexdigest(),
        preview=preview,
        expires_at=datetime.now(UTC)
        + timedelta(seconds=get_settings().capability_import_ttl_seconds),
    )


def store_validation_source(prepared: PreparedCapabilityImport) -> PreparedCapabilityImport:
    """Write the validated source while no database transaction is active."""

    from dataclasses import replace

    key = get_artifact_store().put_temporary("capability-imports", prepared.content)
    return replace(prepared, storage_key=key)


def discard_validation_source(prepared: PreparedCapabilityImport) -> None:
    if prepared.storage_key is not None:
        get_artifact_store().delete(prepared.storage_key)


def register_validation(db: Session, prepared: PreparedCapabilityImport) -> dict[str, Any]:
    """Persist a pre-written source reference in one short transaction."""

    if prepared.storage_key is None:
        raise RuntimeError("Capability import source was not stored")
    source_key = prepared.storage_key
    register_rollback_action(db, lambda: get_artifact_store().delete(source_key))
    item = CapabilityImport(
        token_digest=prepared.token_digest,
        capability_type=prepared.capability_type,
        filename=prepared.filename,
        content_hash=prepared.content_hash,
        storage_key=source_key,
        byte_size=len(prepared.content),
        preview_json=prepared.preview,
        expires_at=prepared.expires_at,
    )
    db.add(item)
    db.flush()
    enqueue(
        db,
        task_type="CLEANUP_CAPABILITY_IMPORT",
        aggregate_type="CAPABILITY_IMPORT",
        aggregate_id=item.id,
        idempotency_key=f"cleanup-capability-import:{item.id}",
        available_at=item.expires_at,
    )
    finish(db)
    return {
        "import_token": prepared.token,
        "content_hash": prepared.content_hash,
        "preview": prepared.preview,
        "warnings": [],
        "errors": [],
        "expires_at": prepared.expires_at.isoformat(),
    }


def cleanup_expired_import(db: Session, import_id: str) -> None:
    stmt = select(CapabilityImport).where(CapabilityImport.id == import_id)
    if db.bind and db.bind.dialect.name == "postgresql":
        stmt = stmt.with_for_update()
    item = db.scalar(stmt)
    if not item or item.state == "COMMITTED":
        return
    current_time = datetime.now(UTC)
    if item.state == "VALIDATED" and _utc(item.expires_at) > current_time:
        return
    item.state = "EXPIRED"
    source_key = item.storage_key
    register_commit_action(db, lambda: get_artifact_store().delete(source_key))
    finish(db)


def _utc(value: datetime) -> datetime:
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


def prepare_commit(db: Session, token: str) -> CapabilityCommitPlan:
    """Freeze an import source in a short transaction before finalize I/O."""

    token_digest = hashlib.sha256(token.encode()).hexdigest()
    stmt = select(CapabilityImport).where(CapabilityImport.token_digest == token_digest)
    if db.bind and db.bind.dialect.name == "postgresql":
        stmt = stmt.with_for_update()
    item = db.scalar(stmt)
    if not item or item.state != "VALIDATED":
        raise _reject("Import token is invalid, expired, or already consumed")
    expired = _utc(item.expires_at) <= datetime.now(UTC)
    if expired:
        item.state = "EXPIRED"
        source_key = item.storage_key
        register_commit_action(db, lambda: get_artifact_store().delete(source_key))
    final_key = f"capability-imports/{item.content_hash[:2]}/{item.content_hash}-{item.id}"
    plan = CapabilityCommitPlan(item.id, item.storage_key, final_key, expired)
    finish(db)
    return plan


def finalize_commit_source(plan: CapabilityCommitPlan) -> str:
    """Finalize an import object while no database transaction is active."""

    if plan.expired:
        raise _reject("Import token is invalid, expired, or already consumed")
    try:
        return get_artifact_store().finalize(plan.source_key, plan.final_key)
    except FileNotFoundError as exc:
        raise _reject("Imported content is unavailable") from exc


def confirm_commit(db: Session, plan: CapabilityCommitPlan, final_key: str) -> dict[str, Any]:
    """CAS-confirm the finalized object reference and consume the one-time token."""

    consumed_at = datetime.now(UTC)
    claimed_id = db.scalar(
        update(CapabilityImport)
        .where(
            CapabilityImport.id == plan.import_id,
            CapabilityImport.state == "VALIDATED",
            CapabilityImport.storage_key == plan.source_key,
            CapabilityImport.expires_at > consumed_at,
        )
        .values(state="COMMITTED", storage_key=final_key, consumed_at=consumed_at)
        .returning(CapabilityImport.id)
        .execution_options(synchronize_session=False)
    )
    if claimed_id is None:
        db.expire_all()
        current = db.get(CapabilityImport, plan.import_id)
        winner_owns_object = (
            current is not None
            and current.state == "COMMITTED"
            and current.storage_key == final_key
        )
        if not winner_owns_object:
            register_rollback_action(db, lambda: get_artifact_store().delete(final_key))
        raise _reject("Import token is invalid, expired, or already consumed")
    # If the DB commit fails, the finalized object is not referenced and can be reclaimed.
    register_rollback_action(db, lambda: get_artifact_store().delete(final_key))
    db.expire_all()
    item = db.get(CapabilityImport, plan.import_id)
    if item is None:
        raise _reject("Import token is invalid, expired, or already consumed")
    finish(db)
    capabilities = [
        {
            "capability_type": item.capability_type,
            "capability_key": entry["capability_key"],
            "normalized_config": {
                **entry.get("normalized_config", {}),
                "import_id": item.id,
                "filename": item.filename,
                "content_hash": item.content_hash,
                "storage_key": final_key,
            },
        }
        for entry in item.preview_json.get("capabilities", [])
    ]
    return {
        "id": item.id,
        "capability_type": item.capability_type,
        "filename": item.filename,
        "content_hash": item.content_hash,
        "storage_key": final_key,
        "byte_size": item.byte_size,
        "preview": item.preview_json,
        "capabilities": capabilities,
        "committed_at": consumed_at.isoformat(),
    }
