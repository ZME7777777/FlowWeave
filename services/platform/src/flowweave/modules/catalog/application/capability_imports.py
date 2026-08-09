from __future__ import annotations

import base64
import hashlib
import io
import json
import re
import zipfile
from copy import deepcopy
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from pathlib import Path, PurePosixPath
from secrets import token_urlsafe
from typing import Any, cast

import yaml
from sqlalchemy import select, update
from sqlalchemy.orm import Session

from flowweave.modules.tasks.public import enqueue
from flowweave.runtime.workspace import materialize_node_workspace
from flowweave.shared.application.transactions import (
    finish,
    register_commit_action,
    register_rollback_action,
)
from flowweave.shared.artifact_store import get_artifact_store
from flowweave.shared.dependency_builder import get_dependency_builder
from flowweave.shared.errors import DomainError
from flowweave.shared.models import CapabilityImport, NodeAsset, NodeCapabilityRef
from flowweave.shared.schemas import CapabilityValidateWrite
from flowweave.shared.settings import get_settings

SENSITIVE = {"api_key", "apikey", "token", "secret", "password", "authorization"}
ZIP_MAX_COMPRESSED_BYTES = 25 * 1024 * 1024
ZIP_MAX_EXPANDED_BYTES = 100 * 1024 * 1024
ZIP_MAX_FILE_BYTES = 25 * 1024 * 1024
ZIP_MAX_FILES = 1000
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
DEPENDENCY_ECOSYSTEMS = {"python", "node", "cli"}
PLATFORM_CLI_VERSIONS = {
    "lark-cli": "1.0.84",
    "uv": "0.7.8",
}
DEPENDENCY_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._@/-]{0,127}$")
EXACT_VERSION = re.compile(r"^[0-9]+(?:\.[0-9]+){1,3}(?:[-+][A-Za-z0-9.-]+)?$")


def _ignored_archive_entry(path: PurePosixPath) -> bool:
    return "__MACOSX" in path.parts or path.name == ".DS_Store" or path.name.startswith("._")


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


def _ensure_unique_capability_keys(capabilities: list[dict[str, Any]]) -> None:
    seen: set[str] = set()
    duplicates: set[str] = set()
    for capability in capabilities:
        key = str(capability.get("capability_key") or "")
        if key in seen:
            duplicates.add(key)
        seen.add(key)
    if duplicates:
        raise _reject(
            "Capability names must be unique within one import",
            capability_keys=sorted(duplicates),
        )


def _dependencies(value: object) -> dict[str, dict[str, str]]:
    """Validate declarative dependencies without accepting executable text."""

    if value in (None, {}):
        return {}
    if not isinstance(value, dict):
        raise _reject("Skill dependencies must be an object")
    raw = cast(dict[object, object], value)
    unknown = sorted(str(key) for key in raw if str(key) not in DEPENDENCY_ECOSYSTEMS)
    if unknown:
        raise _reject("Unsupported dependency ecosystem", ecosystems=unknown)
    result: dict[str, dict[str, str]] = {}
    total = 0
    for ecosystem in ("python", "node", "cli"):
        values = raw.get(ecosystem)
        if values is None:
            continue
        if not isinstance(values, dict):
            raise _reject("Dependency group must be an object", ecosystem=ecosystem)
        entries: dict[str, str] = {}
        for raw_name, raw_version in cast(dict[object, object], values).items():
            name = str(raw_name).strip()
            version = str(raw_version).strip()
            if not DEPENDENCY_NAME.fullmatch(name) or ".." in name or name.startswith(("/", ".")):
                raise _reject("Dependency name is invalid", ecosystem=ecosystem, name=name)
            if not EXACT_VERSION.fullmatch(version):
                raise _reject(
                    "Dependency version must be exactly pinned",
                    ecosystem=ecosystem,
                    name=name,
                )
            if ecosystem == "cli" and PLATFORM_CLI_VERSIONS.get(name) != version:
                raise _reject(
                    "CLI is not in the platform allowlist",
                    name=name,
                    allowed=PLATFORM_CLI_VERSIONS,
                )
            entries[name] = version
            total += 1
        if entries:
            result[ecosystem] = entries
    if total > 50:
        raise _reject("Skill declares too many dependencies", max_dependencies=50)
    return result


def _parse_capability_id(capability_id: str) -> tuple[str, int]:
    import_id, separator, raw_position = capability_id.rpartition(":")
    if not separator or not raw_position.isdigit():
        raise DomainError("CAPABILITY_REFERENCE_INVALID", "Capability reference is invalid", 422)
    return import_id, int(raw_position)


def _capability_entry(
    db: Session, capability_id: str, *, include_deleted: bool = False
) -> tuple[CapabilityImport, int, dict[str, Any]]:
    import_id, position = _parse_capability_id(capability_id)
    item = db.get(CapabilityImport, import_id)
    if item is None or item.state != "COMMITTED":
        raise DomainError("CAPABILITY_REFERENCE_INVALID", "Capability version is unavailable", 404)
    entries = item.preview_json.get("capabilities", [])
    if position >= len(entries) or not isinstance(entries[position], dict):
        raise DomainError("CAPABILITY_REFERENCE_INVALID", "Capability version is unavailable", 404)
    entry = cast(dict[str, Any], entries[position])
    if entry.get("deleted_at") and not include_deleted:
        raise DomainError("CAPABILITY_REFERENCE_INVALID", "Capability version is unavailable", 404)
    return item, position, entry


def _validate_skill(content: bytes, fallback_name: str) -> dict[str, Any]:
    if len(content) > ZIP_MAX_COMPRESSED_BYTES:
        raise _reject("ZIP exceeds 25 MiB", max_bytes=ZIP_MAX_COMPRESSED_BYTES)
    try:
        with zipfile.ZipFile(io.BytesIO(content)) as archive:
            files = archive.infolist()
            if len(files) > ZIP_MAX_FILES:
                raise _reject(
                    f"ZIP contains {len(files)} entries; maximum is {ZIP_MAX_FILES}",
                    actual_entries=len(files),
                    max_entries=ZIP_MAX_FILES,
                )
            names: list[str] = []
            total = 0
            for item in files:
                name = item.filename.replace("\\", "/")
                path = PurePosixPath(name)
                if path.is_absolute() or ".." in path.parts:
                    raise _reject("Unsafe ZIP path")
                if _ignored_archive_entry(path):
                    continue
                if len(path.parts) > ZIP_MAX_DEPTH:
                    raise _reject("ZIP path nesting exceeds limit", filename=name)
                if (item.external_attr >> 16) & 0o170000 == 0o120000:
                    raise _reject("Symbolic links are not allowed")
                if item.file_size > ZIP_MAX_FILE_BYTES:
                    raise _reject(
                        "ZIP file exceeds 25 MiB per-file limit",
                        filename=name,
                        max_bytes=ZIP_MAX_FILE_BYTES,
                    )
                total += item.file_size
                if total > ZIP_MAX_EXPANDED_BYTES:
                    raise _reject("Expanded ZIP exceeds 100 MiB", max_bytes=ZIP_MAX_EXPANDED_BYTES)
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
                key = str(metadata.get("name") or parent or fallback_name or "skill").strip()
                if not key or len(key) > 200:
                    raise _reject("Skill name is invalid", skill_file=skill_file)
                capabilities.append(
                    {
                        "capability_key": key,
                        "normalized_config": {
                            "entry": skill_file,
                            "description": str(metadata.get("description") or "")[:2000],
                            "version": str(metadata.get("version") or ""),
                            "dependencies": _dependencies(metadata.get("dependencies")),
                            "dependency_build_state": (
                                "PENDING" if metadata.get("dependencies") else "NOT_REQUIRED"
                            ),
                        },
                    }
                )
            _ensure_unique_capability_keys(capabilities)
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
        return content, _validate_skill(content, Path(filename).stem)
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
    capabilities = _config_capabilities(payload.capability_type, parsed)
    _ensure_unique_capability_keys(capabilities)
    return content, {
        "capabilities": capabilities,
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


@dataclass(frozen=True, slots=True)
class PreparedSkillUpdate:
    capability_id: str
    import_id: str
    position: int
    expected_content_hash: str
    expected_storage_key: str
    prepared: PreparedCapabilityImport


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

    try:
        key = get_artifact_store().put_temporary("capability-imports", prepared.content)
    except OSError as exc:
        raise DomainError(
            "CAPABILITY_STORAGE_UNAVAILABLE",
            "Capability storage is temporarily unavailable",
            503,
        ) from exc
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
    except OSError as exc:
        raise DomainError(
            "CAPABILITY_STORAGE_UNAVAILABLE",
            "Capability storage is temporarily unavailable",
            503,
        ) from exc


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
    for position, entry in enumerate(item.preview_json.get("capabilities", [])):
        normalized = cast(dict[str, Any], entry.get("normalized_config", {}))
        dependencies = normalized.get("dependencies")
        if isinstance(dependencies, dict) and dependencies:
            enqueue(
                db,
                task_type="BUILD_CAPABILITY_DEPENDENCIES",
                aggregate_type="CAPABILITY_IMPORT",
                aggregate_id=item.id,
                idempotency_key=f"build-capability-dependencies:{item.id}:{position}",
                payload={"position": position},
            )
    finish(db)
    capabilities = [
        {
            "capability_id": f"{item.id}:{position}",
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
        for position, entry in enumerate(item.preview_json.get("capabilities", []))
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


def list_capabilities(db: Session) -> list[dict[str, Any]]:
    """Expand committed imports into immutable, reusable capability versions."""

    imports = db.scalars(
        select(CapabilityImport)
        .where(CapabilityImport.state == "COMMITTED")
        .order_by(CapabilityImport.created_at, CapabilityImport.id)
    ).all()
    reference_counts: dict[tuple[str, str, str], int] = {}
    refs = db.scalars(
        select(NodeCapabilityRef)
        .join(NodeAsset, NodeAsset.id == NodeCapabilityRef.node_asset_id)
        .where(NodeAsset.deleted_at.is_(None))
    ).all()
    for ref in refs:
        import_id = ref.normalized_config.get("import_id")
        if isinstance(import_id, str):
            identity = (import_id, ref.capability_type, ref.capability_key)
            reference_counts[identity] = reference_counts.get(identity, 0) + 1
    result: list[dict[str, Any]] = []
    revision_numbers: dict[tuple[str, str], int] = {}
    latest_by_lineage: dict[str, str] = {}
    for item in imports:
        for position, entry in enumerate(item.preview_json.get("capabilities", [])):
            capability_key = str(entry.get("capability_key") or "")
            identity = (item.capability_type, capability_key)
            revision_numbers[identity] = revision_numbers.get(identity, 0) + 1
            if entry.get("deleted_at"):
                continue
            normalized = cast(dict[str, Any], entry.get("normalized_config", {}))
            capability_id = f"{item.id}:{position}"
            lineage_digest = hashlib.sha256(
                f"{item.capability_type}\0{capability_key}".encode()
            ).hexdigest()[:24]
            lineage_id = f"{item.capability_type.lower()}-{lineage_digest}"
            result.append(
                {
                    "id": capability_id,
                    "lineage_id": lineage_id,
                    "revision_number": revision_numbers[identity],
                    "is_latest": False,
                    "capability_type": item.capability_type,
                    "capability_key": capability_key,
                    "description": str(normalized.get("description") or ""),
                    "version": str(normalized.get("version") or ""),
                    "filename": item.filename,
                    "content_hash": item.content_hash,
                    "byte_size": item.byte_size,
                    "import_id": item.id,
                    "created_at": (item.consumed_at or item.created_at).isoformat(),
                    "reference_count": reference_counts.get(
                        (item.id, item.capability_type, capability_key),
                        0,
                    ),
                    "dependencies": normalized.get("dependencies", {}),
                    "dependency_build_state": normalized.get(
                        "dependency_build_state", "NOT_REQUIRED"
                    ),
                    "dependency_build_error": normalized.get("dependency_build_error"),
                }
            )
            latest_by_lineage[lineage_id] = capability_id
    for capability in result:
        capability["is_latest"] = (
            latest_by_lineage[cast(str, capability["lineage_id"])] == capability["id"]
        )
    result.sort(key=lambda capability: (capability["created_at"], capability["id"]), reverse=True)
    return result


def read_skill_source(db: Session, capability_id: str) -> dict[str, Any]:
    item, _position, entry = _capability_entry(db, capability_id)
    if item.capability_type != "SKILL":
        raise DomainError("CAPABILITY_NOT_EDITABLE", "Only Skill source can be edited", 422)
    normalized = cast(dict[str, Any], entry.get("normalized_config", {}))
    skill_file = str(normalized.get("entry") or "")
    try:
        package = get_artifact_store().read(item.storage_key)
        with zipfile.ZipFile(io.BytesIO(package)) as archive:
            content = archive.read(skill_file).decode("utf-8")
    except (FileNotFoundError, KeyError, UnicodeDecodeError, zipfile.BadZipFile) as exc:
        raise DomainError(
            "CAPABILITY_SOURCE_UNAVAILABLE", "Skill source is unavailable", 422
        ) from exc
    return {
        "id": capability_id,
        "capability_key": str(entry.get("capability_key") or ""),
        "filename": item.filename,
        "entry": skill_file,
        "content": content,
    }


def prepare_skill_update(db: Session, capability_id: str, content: str) -> PreparedSkillUpdate:
    item, position, entry = _capability_entry(db, capability_id)
    if item.capability_type != "SKILL":
        raise DomainError("CAPABILITY_NOT_EDITABLE", "Only Skill source can be edited", 422)
    normalized = cast(dict[str, Any], entry.get("normalized_config", {}))
    skill_file = str(normalized.get("entry") or "")
    try:
        source = get_artifact_store().read(item.storage_key)
        output = io.BytesIO()
        replaced = False
        with (
            zipfile.ZipFile(io.BytesIO(source)) as archive,
            zipfile.ZipFile(output, "w") as revised,
        ):
            for info in archive.infolist():
                data = archive.read(info.filename) if not info.is_dir() else b""
                if info.filename.replace("\\", "/") == skill_file:
                    if data.decode("utf-8") == content:
                        raise DomainError(
                            "CAPABILITY_SOURCE_UNCHANGED",
                            "Capability source has not changed",
                            422,
                        )
                    data = content.encode("utf-8")
                    replaced = True
                revised.writestr(info, data)
        if not replaced:
            raise KeyError(skill_file)
    except (FileNotFoundError, KeyError, OSError, zipfile.BadZipFile) as exc:
        raise DomainError(
            "CAPABILITY_SOURCE_UNAVAILABLE", "Skill source is unavailable", 422
        ) from exc
    payload = CapabilityValidateWrite(
        capability_type="SKILL",
        filename=item.filename,
        content_base64=base64.b64encode(output.getvalue()).decode("ascii"),
    )
    prepared = prepare_validation(payload)
    revised_entries = [
        candidate
        for candidate in cast(list[dict[str, Any]], prepared.preview.get("capabilities", []))
        if cast(dict[str, Any], candidate.get("normalized_config", {})).get("entry") == skill_file
    ]
    if len(revised_entries) != 1:
        raise DomainError(
            "CAPABILITY_SOURCE_INVALID",
            "Edited capability is not present in the validated package",
            422,
        )
    revised_key = str(revised_entries[0].get("capability_key") or "")
    original_key = str(entry.get("capability_key") or "")
    if revised_key != original_key:
        raise DomainError(
            "CAPABILITY_IDENTITY_CHANGED",
            "Editing a capability cannot change its name",
            422,
            {"expected": original_key, "actual": revised_key},
        )
    preview = deepcopy(prepared.preview)
    preview["capabilities"] = revised_entries
    return PreparedSkillUpdate(
        capability_id=capability_id,
        import_id=item.id,
        position=position,
        expected_content_hash=item.content_hash,
        expected_storage_key=item.storage_key,
        prepared=replace(prepared, preview=preview),
    )


def store_skill_update_source(update_plan: PreparedSkillUpdate) -> PreparedSkillUpdate:
    return replace(update_plan, prepared=store_validation_source(update_plan.prepared))


def finalize_skill_update_source(update_plan: PreparedSkillUpdate) -> str:
    temporary_key = update_plan.prepared.storage_key
    if temporary_key is None:
        raise RuntimeError("Capability update source was not stored")
    final_key = (
        f"capability-imports/{update_plan.prepared.content_hash[:2]}/"
        f"{update_plan.prepared.content_hash}-{update_plan.import_id}-"
        f"{update_plan.prepared.token_digest[:12]}"
    )
    try:
        return get_artifact_store().finalize(temporary_key, final_key)
    except FileNotFoundError as exc:
        raise DomainError(
            "CAPABILITY_SOURCE_UNAVAILABLE", "Capability source is unavailable", 422
        ) from exc
    except OSError as exc:
        raise DomainError(
            "CAPABILITY_STORAGE_UNAVAILABLE",
            "Capability storage is temporarily unavailable",
            503,
        ) from exc


def _updated_capability_config(
    item: CapabilityImport, capability_id: str, entry: dict[str, Any]
) -> dict[str, Any]:
    return {
        **cast(dict[str, Any], entry.get("normalized_config", {})),
        "capability_id": capability_id,
        "import_id": item.id,
        "filename": item.filename,
        "content_hash": item.content_hash,
        "storage_key": item.storage_key,
    }


def _refresh_node_workspace(asset: dict[str, Any]) -> None:
    materialize_node_workspace(asset)


def confirm_skill_update(
    db: Session, update_plan: PreparedSkillUpdate, final_key: str
) -> dict[str, Any]:
    """Replace one editable Skill in place while preserving historical snapshot objects."""

    register_rollback_action(db, lambda: get_artifact_store().delete(final_key))
    item = db.get(CapabilityImport, update_plan.import_id)
    if (
        item is None
        or item.state != "COMMITTED"
        or item.content_hash != update_plan.expected_content_hash
        or item.storage_key != update_plan.expected_storage_key
    ):
        raise DomainError(
            "CAPABILITY_SOURCE_CONFLICT",
            "Capability source changed while it was being edited",
            409,
        )

    entries = cast(list[dict[str, Any]], item.preview_json.get("capabilities", []))
    if update_plan.position >= len(entries) or entries[update_plan.position].get("deleted_at"):
        raise DomainError("CAPABILITY_REFERENCE_INVALID", "Capability is unavailable", 404)
    updated_entries = cast(
        list[dict[str, Any]], update_plan.prepared.preview.get("capabilities", [])
    )
    if len(updated_entries) != 1:
        raise DomainError("CAPABILITY_SOURCE_INVALID", "Capability source is invalid", 422)

    capability_key = str(entries[update_plan.position].get("capability_key") or "")
    preview = deepcopy(item.preview_json)
    preview_entries = cast(list[dict[str, Any]], preview.get("capabilities", []))
    preview_entries[update_plan.position] = deepcopy(updated_entries[0])
    item.preview_json = preview
    item.content_hash = update_plan.prepared.content_hash
    item.storage_key = final_key
    item.byte_size = len(update_plan.prepared.content)
    item.consumed_at = datetime.now(UTC)

    # Older records created by the former publish-new-version behavior are
    # collapsed into this editable capability. Their objects remain available
    # to immutable run snapshots, but they no longer appear in the live pool.
    imports = db.scalars(
        select(CapabilityImport).where(
            CapabilityImport.state == "COMMITTED",
            CapabilityImport.capability_type == item.capability_type,
        )
    ).all()
    for candidate in imports:
        candidate_preview = deepcopy(candidate.preview_json)
        candidate_entries = cast(list[dict[str, Any]], candidate_preview.get("capabilities", []))
        changed = False
        for position, candidate_entry in enumerate(candidate_entries):
            if (
                candidate_entry.get("capability_key") == capability_key
                and not (candidate.id == item.id and position == update_plan.position)
                and not candidate_entry.get("deleted_at")
            ):
                candidate_entry["deleted_at"] = datetime.now(UTC).isoformat()
                changed = True
        if changed:
            candidate.preview_json = candidate_preview

    normalized_by_key = {
        str(candidate.get("capability_key") or ""): _updated_capability_config(
            item, f"{item.id}:{position}", candidate
        )
        for position, candidate in enumerate(preview_entries)
        if not candidate.get("deleted_at")
    }
    normalized = normalized_by_key[capability_key]
    active_nodes = db.scalars(
        select(NodeAsset).where(NodeAsset.deleted_at.is_(None)).order_by(NodeAsset.id)
    ).all()
    assets_to_refresh: list[dict[str, Any]] = []
    has_dependencies = bool(normalized.get("dependencies"))
    for node in active_nodes:
        refs = db.scalars(
            select(NodeCapabilityRef).where(
                NodeCapabilityRef.node_asset_id == node.id,
                NodeCapabilityRef.capability_type == item.capability_type,
            )
        ).all()
        changed = False
        node_waits_for_dependencies = False
        for ref in refs:
            ref_config = ref.normalized_config or {}
            replacement = normalized_by_key.get(ref.capability_key)
            if ref.capability_key == capability_key:
                replacement = normalized
            elif ref_config.get("import_id") != item.id:
                replacement = None
            if replacement is None:
                continue
            ref.normalized_config = dict(replacement)
            changed = True
            if ref.capability_key == capability_key and has_dependencies:
                node_waits_for_dependencies = True
        if not changed:
            continue
        db.flush()
        if not node_waits_for_dependencies:
            from flowweave.modules.catalog.application.service import asset_dict

            assets_to_refresh.append(asset_dict(db, node))

    if has_dependencies:
        enqueue(
            db,
            task_type="BUILD_CAPABILITY_DEPENDENCIES",
            aggregate_type="CAPABILITY_IMPORT",
            aggregate_id=item.id,
            idempotency_key=(
                f"build-capability-dependencies:{item.id}:"
                f"{update_plan.position}:{item.content_hash}"
            ),
            payload={"position": update_plan.position},
        )
    else:
        for asset in assets_to_refresh:
            register_commit_action(db, lambda asset=asset: _refresh_node_workspace(asset))

    finish(db)
    return next(
        capability
        for capability in list_capabilities(db)
        if capability["id"] == update_plan.capability_id
    )


def process_dependency_build(db: Session, import_id: str, position: int) -> None:
    """Build a pinned dependency bundle without holding a database transaction."""

    item = db.get(CapabilityImport, import_id)
    raw_entries: object = item.preview_json.get("capabilities", []) if item else []
    entries = cast(list[object], raw_entries) if isinstance(raw_entries, list) else []
    if (
        item is None
        or item.state != "COMMITTED"
        or position < 0
        or position >= len(entries)
        or not isinstance(entries[position], dict)
    ):
        raise RuntimeError("Capability version is unavailable")
    entry = cast(dict[str, Any], entries[position])
    if entry.get("deleted_at"):
        return
    normalized = cast(dict[str, Any], entry.get("normalized_config", {}))
    raw_dependencies = normalized.get("dependencies")
    dependencies = (
        cast(dict[str, dict[str, str]], raw_dependencies)
        if isinstance(raw_dependencies, dict)
        else {}
    )
    if not dependencies:
        return
    if normalized.get("dependency_build_state") == "READY":
        return

    # The builder can access package registries, so close the database transaction first.
    # Its container receives only this validated manifest and never application credentials.
    db.rollback()
    bundle = get_dependency_builder().build(dependencies)
    digest = hashlib.sha256(bundle.content).hexdigest()
    storage_key = f"capability-dependencies/{digest[:2]}/{digest}.zip"
    try:
        get_artifact_store().put(storage_key, bundle.content)
        current = db.get(CapabilityImport, import_id)
        raw_current_entries: object = (
            current.preview_json.get("capabilities", []) if current else []
        )
        current_entries = (
            cast(list[object], raw_current_entries) if isinstance(raw_current_entries, list) else []
        )
        if (
            current is None
            or current.state != "COMMITTED"
            or position >= len(current_entries)
            or not isinstance(current_entries[position], dict)
        ):
            raise RuntimeError("Capability version disappeared during dependency build")
        preview = deepcopy(current.preview_json)
        current_entry = cast(dict[str, Any], preview["capabilities"][position])
        if current_entry.get("deleted_at"):
            get_artifact_store().delete(storage_key)
            return
        current_normalized = cast(dict[str, Any], current_entry.setdefault("normalized_config", {}))
        if current_normalized.get("dependencies") != dependencies:
            raise RuntimeError("Capability dependencies changed during build")
        current_normalized.update(
            {
                "dependency_build_state": "READY",
                "dependency_storage_key": storage_key,
                "dependency_content_hash": digest,
                "dependency_manifest": bundle.manifest,
            }
        )
        current_normalized.pop("dependency_build_error", None)
        current.preview_json = preview
        capability_id = f"{current.id}:{position}"
        capability_key = str(current_entry.get("capability_key") or "")
        normalized = _updated_capability_config(current, capability_id, current_entry)
        nodes = db.scalars(
            select(NodeAsset).where(NodeAsset.deleted_at.is_(None)).order_by(NodeAsset.id)
        ).all()
        assets_to_refresh: list[dict[str, Any]] = []
        for node in nodes:
            refs = db.scalars(
                select(NodeCapabilityRef).where(
                    NodeCapabilityRef.node_asset_id == node.id,
                    NodeCapabilityRef.capability_type == current.capability_type,
                    NodeCapabilityRef.capability_key == capability_key,
                )
            ).all()
            if not refs:
                continue
            for ref in refs:
                ref.normalized_config = dict(normalized)
            db.flush()
            from flowweave.modules.catalog.application.service import asset_dict

            assets_to_refresh.append(asset_dict(db, node))
        for asset in assets_to_refresh:
            register_commit_action(db, lambda asset=asset: _refresh_node_workspace(asset))
        register_rollback_action(db, lambda: get_artifact_store().delete(storage_key))
        finish(db)
    except BaseException:
        if not db.in_transaction():
            get_artifact_store().delete(storage_key)
        raise


def _references(
    db: Session, capability_id: str, item: CapabilityImport, entry: dict[str, Any]
) -> list[dict[str, str]]:
    key = str(entry.get("capability_key") or "")
    result: list[dict[str, str]] = []
    rows = db.execute(
        select(NodeCapabilityRef, NodeAsset.name)
        .join(NodeAsset, NodeAsset.id == NodeCapabilityRef.node_asset_id)
        .where(NodeAsset.deleted_at.is_(None))
        .order_by(NodeAsset.name, NodeAsset.id)
    ).all()
    for ref, node_name in rows:
        config = cast(dict[str, Any], ref.normalized_config or {})
        if config.get("capability_id") == capability_id or (
            config.get("import_id") == item.id
            and ref.capability_type == item.capability_type
            and ref.capability_key == key
        ):
            result.append({"id": ref.node_asset_id, "name": node_name})
    return result


def delete_capabilities(db: Session, capability_ids: list[str]) -> dict[str, Any]:
    unique_ids = list(dict.fromkeys(capability_ids))
    resolved = [
        (*_capability_entry(db, capability_id), capability_id) for capability_id in unique_ids
    ]
    blocked: list[dict[str, Any]] = []
    deletable: list[tuple[CapabilityImport, int, dict[str, Any], str]] = []
    for item, position, entry, capability_id in resolved:
        nodes = _references(db, capability_id, item, entry)
        if nodes:
            blocked.append(
                {
                    "id": capability_id,
                    "name": str(entry.get("capability_key") or ""),
                    "relation": "NODE_CAPABILITY",
                    "nodes": nodes,
                }
            )
        else:
            deletable.append((item, position, entry, capability_id))

    by_import: dict[str, list[int]] = {}
    for item, position, _entry, _capability_id in deletable:
        by_import.setdefault(item.id, []).append(position)
    deleted_at = datetime.now(UTC).isoformat()
    for import_id, positions in by_import.items():
        item = db.get(CapabilityImport, import_id)
        if item is None:
            continue
        preview = deepcopy(item.preview_json)
        entries = cast(list[dict[str, Any]], preview.get("capabilities", []))
        for position in positions:
            entries[position]["deleted_at"] = deleted_at
        # Pool deletion is deliberately logical. Historical run snapshots may
        # still contain this immutable storage key even when no live node does.
        item.preview_json = preview
    finish(db)
    return {
        "deleted_ids": [capability_id for *_rest, capability_id in deletable],
        "blocked": blocked,
    }
