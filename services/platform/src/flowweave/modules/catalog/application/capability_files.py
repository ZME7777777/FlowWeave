"""Read governed package members without extracting or executing their contents."""

from __future__ import annotations

import base64
import difflib
import hashlib
import io
import re
import zipfile
from pathlib import PurePosixPath
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from flowweave.modules.catalog.application import capability_imports as imports
from flowweave.modules.catalog.application.capability_repository import resolve_version
from flowweave.shared.artifact_store import get_artifact_store
from flowweave.shared.errors import DomainError
from flowweave.shared.models import CapabilityVersion

PREVIEW_MAX_BYTES = 1024 * 1024


def package_files(db: Session, capability_id: str) -> tuple[Any, dict[str, bytes]]:
    published = resolve_version(db, capability_id)
    kind = published.package.capability_type
    if kind not in {"SKILL", "CONTEXT"}:
        raise DomainError(
            "CAPABILITY_NOT_INSPECTABLE", "Only Skill and Context packages are supported", 422
        )
    normalized = published.version.normalized_config_json or {}
    try:
        content = get_artifact_store().read(published.blob.storage_key)
        if kind == "CONTEXT" and normalized.get("content_format") != "BUNDLE":
            return published, {PurePosixPath(published.version.source_filename).name: content}
        with zipfile.ZipFile(io.BytesIO(content)) as archive:
            entries = archive.infolist()
            if len(entries) > imports.ZIP_MAX_RAW_ENTRIES:
                raise ValueError("Too many package entries")
            paths = [
                PurePosixPath(imports._context_bundle_entry_name(info).replace("\\", "/"))
                if kind == "CONTEXT"
                else PurePosixPath(info.filename.replace("\\", "/"))
                for info in entries
            ]
            root = PurePosixPath(str(normalized.get("entry") or "SKILL.md")).parent
            if kind == "CONTEXT":
                bundle_root = imports._bundle_root(
                    [
                        path
                        for info, path in zip(entries, paths, strict=True)
                        if not info.is_dir()
                        and not imports._ignored_archive_entry(path)
                        and path.name.casefold() not in imports.CONTEXT_BUNDLE_MANIFEST_NAMES
                    ]
                )
                root = PurePosixPath(bundle_root) if bundle_root else PurePosixPath()
            files: dict[str, bytes] = {}
            total = 0
            for info, path in zip(entries, paths, strict=True):
                if (
                    path.is_absolute()
                    or ".." in path.parts
                    or "\x00" in str(path)
                    or re.match(r"^[A-Za-z]:", str(path))
                ):
                    raise ValueError("Unsafe package path")
                if info.is_dir() or imports._ignored_archive_entry(path):
                    continue
                if (info.external_attr >> 16) & 0o170000 not in {0, 0o100000}:
                    raise ValueError("Unsupported package member")
                if info.file_size > imports.ZIP_MAX_FILE_BYTES:
                    raise ValueError("Package member too large")
                total += info.file_size
                if total > imports.ZIP_MAX_EXPANDED_BYTES:
                    raise ValueError("Expanded package too large")
                if root.parts and path.parts[: len(root.parts)] != root.parts:
                    continue
                relative = PurePosixPath(*path.parts[len(root.parts) :]).as_posix()
                if relative in files:
                    raise ValueError("Duplicate package path")
                files[relative] = archive.read(info)
            return published, files
    except (FileNotFoundError, KeyError, OSError, ValueError, zipfile.BadZipFile) as exc:
        raise DomainError("CAPABILITY_SOURCE_UNAVAILABLE", "能力包暂时无法读取。", 422) from exc


def _text(content: bytes) -> str | None:
    if len(content) > PREVIEW_MAX_BYTES or b"\x00" in content:
        return None
    try:
        return content.decode("utf-8")
    except UnicodeDecodeError:
        return None


def _metadata(published: Any) -> dict[str, Any]:
    return {
        "id": published.version.id,
        "capability_key": published.package.capability_key,
        "capability_type": published.package.capability_type,
        "filename": published.version.source_filename,
        "digest": published.version.digest,
        "version_no": published.version.version_no,
    }


def read_package_files(db: Session, capability_id: str) -> dict[str, Any]:
    published, files = package_files(db, capability_id)
    normalized = published.version.normalized_config_json or {}
    manifest = normalized.get("manifest") or {}
    titles = {item["path"]: item["title"] for item in manifest.get("documents", [])}
    return {
        **_metadata(published),
        "entry": "SKILL.md" if published.package.capability_type == "SKILL" else "",
        "compiled_content": imports.read_context_source(db, capability_id)["content"]
        if published.package.capability_type == "CONTEXT"
        else None,
        "manifest": manifest or None,
        "files": [
            {
                "path": path,
                "title": titles.get(path, path),
                "byte_size": len(data),
                "sha256": hashlib.sha256(data).hexdigest(),
                "editable": published.package.capability_type == "SKILL"
                and _text(data) is not None,
            }
            for path, data in sorted(files.items())
        ],
    }


def read_package_file(db: Session, capability_id: str, path: str) -> dict[str, Any]:
    published, files = package_files(db, capability_id)
    if path not in files:
        raise DomainError("CAPABILITY_FILE_NOT_FOUND", "能力包中没有这个文件。", 404)
    data = files[path]
    text = _text(data)
    image_types = {
        ".png": "image/png",
        ".jpg": "image/jpeg",
        ".jpeg": "image/jpeg",
        ".webp": "image/webp",
        ".gif": "image/gif",
    }
    image_type = image_types.get(PurePosixPath(path).suffix.lower())
    return {
        "path": path,
        "content": text,
        "byte_size": len(data),
        "editable": published.package.capability_type == "SKILL" and text is not None,
        "preview_unavailable": text is None,
        "image": f"data:{image_type};base64,{base64.b64encode(data).decode('ascii')}"
        if image_type and len(data) <= PREVIEW_MAX_BYTES
        else None,
    }


def _comparison(
    db: Session, capability_id: str
) -> tuple[Any, Any, dict[str, bytes], dict[str, bytes]]:
    current, after = package_files(db, capability_id)
    previous = db.scalar(
        select(CapabilityVersion)
        .where(
            CapabilityVersion.package_id == current.package.id,
            CapabilityVersion.version_no < current.version.version_no,
            CapabilityVersion.state == "PUBLISHED",
        )
        .order_by(CapabilityVersion.version_no.desc())
        .limit(1)
    )
    if previous is None:
        raise DomainError("CAPABILITY_PREVIOUS_VERSION_UNAVAILABLE", "暂无上一版本可对比。", 409)
    before_version, before = package_files(db, previous.id)
    if current.package.capability_type == "CONTEXT":
        before[""] = imports.read_context_source(db, previous.id)["content"].encode("utf-8")
        after[""] = imports.read_context_source(db, capability_id)["content"].encode("utf-8")
    return before_version, current, before, after


def compare_package_versions(db: Session, capability_id: str) -> dict[str, Any]:
    previous, current, before, after = _comparison(db, capability_id)
    return {
        "previous": _metadata(previous),
        "current": _metadata(current),
        "files": [
            {
                "path": path,
                "status": "added"
                if path not in before
                else "deleted"
                if path not in after
                else "unchanged"
                if before[path] == after[path]
                else "modified",
                "before_size": len(before.get(path, b"")),
                "after_size": len(after.get(path, b"")),
            }
            for path in sorted(before.keys() | after.keys())
        ],
    }


def compare_package_file(db: Session, capability_id: str, path: str) -> dict[str, Any]:
    previous, current, before, after = _comparison(db, capability_id)
    if path not in before and path not in after:
        raise DomainError("CAPABILITY_FILE_NOT_FOUND", "能力包中没有这个文件。", 404)
    left, right = _text(before.get(path, b"")), _text(after.get(path, b""))
    patch = ""
    if left is not None and right is not None:
        # Preserve missing-final-newline changes without joining +/- rows together.
        patch = "".join(
            line if line.endswith("\n") else line + "\n\\ No newline at end of file\n"
            for line in difflib.unified_diff(
                left.splitlines(keepends=True),
                right.splitlines(keepends=True),
                fromfile=f"previous/{path or 'compiled.txt'}",
                tofile=f"current/{path or 'compiled.txt'}",
            )
        )
    return {
        "path": path,
        "diff": patch,
        "preview_unavailable": left is None or right is None,
        "previous_id": previous.version.id,
        "current_id": current.version.id,
    }
