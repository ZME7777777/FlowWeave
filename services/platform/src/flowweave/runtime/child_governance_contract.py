"""Immutable V4 child governance; V3 remains tied to its original source."""

from __future__ import annotations

from typing import Any

from flowweave.runtime.child_contract import (
    CHILD_HTTP_OPERATIONS,
    V3_ARCHIVE_SHA256,
    V3_SOURCE_COMMIT,
    child_capability_manifest,
)
from flowweave.runtime.manifest import runtime_manifest_hash
from flowweave.shared.errors import DomainError

V4_SOURCE_COMMIT = "b9f976b2ca05b947ee28eb34ebe590ff0409307f"
V4_ARCHIVE_SHA256 = "4f53a19445c7c5cfabe022414dd33942fc3c086b0be5b60fd87e9fa0ccc5899f"
V4_PACKAGE_VERSION = "1.58.2"
V5_SOURCE_COMMIT = "805bb3fe0a57cdc3e383e26019fa216a6892e608"
V5_ARCHIVE_SHA256 = "eaf4322c182d2ea80202840b43834cb72697371e479eca91aaf03dc458adb208"
V5_PACKAGE_VERSION = "1.58.2"
GOVERNED_CHILD_SOURCES = {
    "OH_FW_V4": (V4_SOURCE_COMMIT, V4_ARCHIVE_SHA256, V4_PACKAGE_VERSION),
    "OH_FW_V5": (V5_SOURCE_COMMIT, V5_ARCHIVE_SHA256, V5_PACKAGE_VERSION),
}
V4_CHILD_HTTP_OPERATIONS = (
    *CHILD_HTTP_OPERATIONS,
    ("POST", "/api/conversations/{conversation_id}/children/launcher/resolve"),
    (
        "POST",
        "/api/conversations/{conversation_id}/children/preparations/{creation_id}/governed",
    ),
)


def governed_child_capability_manifest(profile: str = "OH_FW_V4") -> dict[str, Any]:
    source, archive, package = GOVERNED_CHILD_SOURCES[profile]
    document = child_capability_manifest()
    document.pop("digest")
    document.update(
        version="oh-fw-child-v2" if profile == "OH_FW_V4" else "oh-fw-child-v3",
        source_commit=source,
        source_archive_sha256=archive,
        package_version=package,
        launcher_resolution_schema_version=1,
        native_creation_identity_required=True,
        preparation_governance={
            "request_field": "overrides",
            "identity_field": "governance_id",
            "fields": [
                "governance_id",
                "remove_secrets",
                "replace_secrets",
                "remove_mcp_servers",
            ],
            "before_initialization": True,
            "encrypted_immutable_plan": True,
            "identical_retry_required": True,
        },
    )
    return {**document, "digest": runtime_manifest_hash(document)}


def require_governed_child_capability_manifest(
    value: object, profile: str = "OH_FW_V4"
) -> dict[str, Any]:
    expected = governed_child_capability_manifest(profile)
    try:
        matches = runtime_manifest_hash(value) == runtime_manifest_hash(expected)
    except (TypeError, ValueError):
        matches = False
    if not matches:
        raise DomainError(
            "RUNTIME_CHILD_CAPABILITY_INCOMPATIBLE",
            "The V4 child capability differs from its frozen governance contract",
            409,
        )
    return expected


def child_capability_build_evidence(provenance: dict[str, Any]) -> dict[str, Any]:
    """Only a probe of the corresponding immutable image may declare capability."""
    if (
        provenance.get("source_commit") == V5_SOURCE_COMMIT
        and provenance.get("source_ref") == V5_SOURCE_COMMIT
        and provenance.get("source_archive_digest") == V5_ARCHIVE_SHA256
        and provenance.get("package_versions")
        == {
            name: V5_PACKAGE_VERSION
            for name in (
                "openhands-agent-server",
                "openhands-sdk",
                "openhands-tools",
                "openhands-workspace",
            )
        }
    ):
        return {"child_conversations": governed_child_capability_manifest("OH_FW_V5")}
    if (
        provenance.get("source_commit") == V4_SOURCE_COMMIT
        and provenance.get("source_ref") == V4_SOURCE_COMMIT
        and provenance.get("source_archive_digest") == V4_ARCHIVE_SHA256
        and provenance.get("package_versions")
        == {
            name: V4_PACKAGE_VERSION
            for name in (
                "openhands-agent-server",
                "openhands-sdk",
                "openhands-tools",
                "openhands-workspace",
            )
        }
    ):
        return {"child_conversations": governed_child_capability_manifest()}
    if (
        provenance.get("source_commit") == V3_SOURCE_COMMIT
        and provenance.get("source_archive_digest") == V3_ARCHIVE_SHA256
    ):
        return {"child_conversations": child_capability_manifest()}
    return {}
