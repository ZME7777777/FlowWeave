"""Idempotent installation of platform-managed MCP capability versions.

The platform owns these capabilities, so operators neither upload them nor copy
version identifiers into server configuration. A version is addressed by the
content of its frozen config: the same URL yields the same immutable version,
and a changed URL publishes a new version without touching the old one.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from urllib.parse import urlsplit
from uuid import NAMESPACE_URL, uuid5

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from flowweave.modules.catalog.application.capability_repository import PublishedCapability
from flowweave.modules.catalog.application.platform_capabilities import (
    PLATFORM_MANAGED_CAPABILITY_KEYS,
)
from flowweave.shared.artifact_store import get_artifact_store
from flowweave.shared.domain.capability_digest import capability_version_digest
from flowweave.shared.errors import DomainError
from flowweave.shared.models import (
    CapabilityBlob,
    CapabilityPackage,
    CapabilityValidation,
    CapabilityVersion,
)


@dataclass(frozen=True, slots=True)
class PlatformMcp:
    """What the platform needs from one of its own MCP servers."""

    server_name: str
    path: str
    auth_header: str
    display_name: str


def _stable_id(kind: str, value: str) -> str:
    return str(uuid5(NAMESPACE_URL, f"flowweave:platform-mcp:{kind}:{value}"))


def validated_url(base_url: str, path: str) -> str:
    """Join a trusted base URL and a fixed server path; refuse anything unsafe."""

    parsed = urlsplit(base_url.strip())
    if (
        parsed.scheme not in {"http", "https"}
        or not parsed.hostname
        or parsed.username
        or parsed.password
        or parsed.query
        or parsed.fragment
    ):
        raise DomainError(
            "PLATFORM_MCP_BASE_URL_INVALID",
            "平台内置 MCP 的访问前缀必须是不含凭据、查询和片段的 http(s) 地址",
            500,
        )
    return base_url.strip().rstrip("/") + path


def ensure_platform_mcp(db: Session, mcp: PlatformMcp, url: str) -> PublishedCapability:
    """Publish (or find) the immutable version of one platform MCP for ``url``."""

    if mcp.server_name not in PLATFORM_MANAGED_CAPABILITY_KEYS:
        raise DomainError(
            "PLATFORM_MCP_NOT_MANAGED", "只能自动安装平台托管的 MCP", 500, {"key": mcp.server_name}
        )
    config = {
        "url": url,
        "transport": "streamable-http",
        "headers": {"Authorization": mcp.auth_header},
    }
    content = json.dumps(
        {"mcpServers": {mcp.server_name: config}}, ensure_ascii=False, sort_keys=True
    ).encode()
    content_hash = hashlib.sha256(content).hexdigest()
    digest = capability_version_digest("MCP", mcp.server_name, content_hash, config)

    # Adopt a package that already carries this identity (an earlier manual
    # import, or a prior install) instead of creating a competing one.
    package = db.scalar(
        select(CapabilityPackage).where(
            CapabilityPackage.capability_type == "MCP",
            CapabilityPackage.capability_key == mcp.server_name,
        )
    )
    if package is None:
        package = CapabilityPackage(
            id=_stable_id("package", mcp.server_name),
            capability_type="MCP",
            capability_key=mcp.server_name,
            display_name=mcp.display_name,
            description="",
        )
        db.add(package)
        db.flush()

    # The digest is unique per owner, so an identical version imported earlier
    # (same URL and header) is the version to use, whichever package id it has.
    version = db.scalar(
        select(CapabilityVersion).where(
            CapabilityVersion.package_id == package.id, CapabilityVersion.digest == digest
        )
    )
    if version is None:
        storage_key = f"platform-mcp/{mcp.server_name}/{content_hash}.json"
        blob = db.scalar(select(CapabilityBlob).where(CapabilityBlob.content_hash == content_hash))
        if blob is None:
            get_artifact_store().put(storage_key, content)
            blob = CapabilityBlob(
                id=_stable_id("blob", content_hash),
                content_hash=content_hash,
                storage_key=storage_key,
                byte_size=len(content),
                media_type="application/json",
            )
            db.add(blob)
            db.flush()
        version_no = (
            db.scalar(
                select(func.max(CapabilityVersion.version_no)).where(
                    CapabilityVersion.package_id == package.id
                )
            )
            or 0
        ) + 1
        version = CapabilityVersion(
            id=_stable_id("version", f"{mcp.server_name}:{digest}"),
            package_id=package.id,
            blob_id=blob.id,
            version_no=version_no,
            digest=digest,
            normalized_config_json=config,
            source_filename=f"{mcp.server_name}.json",
            state="PUBLISHED",
        )
        db.add(version)
        db.flush()
        db.add(
            CapabilityValidation(
                id=_stable_id("validation", version.id),
                capability_version_id=version.id,
                validator="flowweave-platform-mcp-v1",
                status="PASSED",
                report_json={"platform_managed": True},
            )
        )
        db.flush()
    blob = db.get(CapabilityBlob, version.blob_id)
    if blob is None:
        raise DomainError("CAPABILITY_REPOSITORY_CORRUPT", "平台 MCP 缺少不可变内容", 500)
    return PublishedCapability(package, version, blob)
