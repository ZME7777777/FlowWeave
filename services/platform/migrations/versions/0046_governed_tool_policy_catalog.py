"""govern the target OpenHands Tool catalog and concurrency policy

Revision ID: 0046_tool_policy_catalog
Revises: 0045_runtime_conversation_forks
"""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime
from uuid import NAMESPACE_URL, uuid5

import sqlalchemy as sa
from alembic import op

from flowweave.shared.domain.capability_digest import capability_version_digest
from flowweave.shared.domain.tool_policy import (
    DEFAULT_TOOL_POLICY_CONFIG,
    DEFAULT_TOOL_POLICY_KEY,
    OPENHANDS_VERSION,
)

revision = "0046_tool_policy_catalog"
down_revision = "0045_runtime_conversation_forks"
branch_labels = None
depends_on = None

LEGACY_VERSION_ID = str(
    uuid5(NAMESPACE_URL, f"flowweave:version:builtin:{DEFAULT_TOOL_POLICY_KEY}:1")
)
POLICY_BYTES = json.dumps(
    DEFAULT_TOOL_POLICY_CONFIG,
    ensure_ascii=False,
    sort_keys=True,
    separators=(",", ":"),
).encode()
POLICY_CONTENT_HASH = hashlib.sha256(POLICY_BYTES).hexdigest()
POLICY_BLOB_ID = str(uuid5(NAMESPACE_URL, f"flowweave:blob:{POLICY_CONTENT_HASH}"))
POLICY_PACKAGE_ID = str(
    uuid5(NAMESPACE_URL, f"flowweave:package:TOOL_POLICY:{DEFAULT_TOOL_POLICY_KEY}")
)
POLICY_VERSION_ID = str(
    uuid5(NAMESPACE_URL, f"flowweave:version:builtin:{DEFAULT_TOOL_POLICY_KEY}:2")
)
POLICY_VALIDATION_ID = str(uuid5(NAMESPACE_URL, f"flowweave:validation:{POLICY_VERSION_ID}"))
POLICY_DIGEST = capability_version_digest(
    "TOOL_POLICY",
    DEFAULT_TOOL_POLICY_KEY,
    POLICY_CONTENT_HASH,
    DEFAULT_TOOL_POLICY_CONFIG,
)


def _runtime_config() -> dict[str, object]:
    return {
        **DEFAULT_TOOL_POLICY_CONFIG,
        "capability_id": POLICY_VERSION_ID,
        "capability_version_id": POLICY_VERSION_ID,
        "package_id": POLICY_PACKAGE_ID,
        "version_no": 2,
        "digest": POLICY_DIGEST,
        "filename": "flowweave-default-tools-v2.json",
        "content_hash": POLICY_CONTENT_HASH,
        "storage_key": f"builtin://tool-policies/{POLICY_CONTENT_HASH}.json",
    }


def _hash(value: object) -> str:
    encoded = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(encoded).hexdigest()


def _seed_policy(bind: sa.Connection) -> None:
    now = datetime.now(UTC)
    bind.execute(
        sa.text(
            "INSERT INTO capability_blobs "
            "(id, content_hash, storage_key, byte_size, media_type, created_at) "
            "VALUES (:id, :content_hash, :storage_key, :byte_size, "
            "'application/json', :created_at) ON CONFLICT (id) DO NOTHING"
        ),
        {
            "id": POLICY_BLOB_ID,
            "content_hash": POLICY_CONTENT_HASH,
            "storage_key": f"builtin://tool-policies/{POLICY_CONTENT_HASH}.json",
            "byte_size": len(POLICY_BYTES),
            "created_at": now,
        },
    )
    bind.execute(
        sa.text(
            "INSERT INTO capability_versions "
            "(id, package_id, blob_id, version_no, digest, normalized_config_json, "
            "source_filename, source_import_id, source_position, state, created_at) "
            "VALUES (:id, :package_id, :blob_id, 2, :digest, CAST(:config AS JSON), "
            "'flowweave-default-tools-v2.json', NULL, NULL, 'PUBLISHED', :created_at) "
            "ON CONFLICT (id) DO NOTHING"
        ),
        {
            "id": POLICY_VERSION_ID,
            "package_id": POLICY_PACKAGE_ID,
            "blob_id": POLICY_BLOB_ID,
            "digest": POLICY_DIGEST,
            "config": json.dumps(DEFAULT_TOOL_POLICY_CONFIG, ensure_ascii=False),
            "created_at": now,
        },
    )
    bind.execute(
        sa.text(
            "INSERT INTO capability_validations "
            "(id, capability_version_id, validator, status, report_json, created_at) "
            "VALUES (:id, :version_id, 'flowweave-builtin-v2', 'PASSED', "
            "CAST(:report AS JSON), :created_at) ON CONFLICT (id) DO NOTHING"
        ),
        {
            "id": POLICY_VALIDATION_ID,
            "version_id": POLICY_VERSION_ID,
            "report": json.dumps(
                {
                    "builtin": True,
                    "openhands_version": OPENHANDS_VERSION,
                    "source_commit": DEFAULT_TOOL_POLICY_CONFIG["source_commit"],
                    "catalog_digest": DEFAULT_TOOL_POLICY_CONFIG["catalog_digest"],
                },
                ensure_ascii=False,
            ),
            "created_at": now,
        },
    )


def _rewrite_default_references(bind: sa.Connection, *, upgrading: bool) -> None:
    source_id = LEGACY_VERSION_ID if upgrading else POLICY_VERSION_ID
    target_id = POLICY_VERSION_ID if upgrading else LEGACY_VERSION_ID
    if not upgrading:
        legacy_exists = bind.scalar(
            sa.text("SELECT 1 FROM capability_versions WHERE id = :id"), {"id": LEGACY_VERSION_ID}
        )
        if not legacy_exists:
            return
    target = (
        bind.execute(
            sa.text(
                "SELECT v.id, v.version_no, v.digest, v.normalized_config_json, "
                "v.source_filename, b.content_hash, b.storage_key "
                "FROM capability_versions v JOIN capability_blobs b ON b.id = v.blob_id "
                "WHERE v.id = :id"
            ),
            {"id": target_id},
        )
        .mappings()
        .one()
    )
    runtime_config = {
        **dict(target["normalized_config_json"]),
        "capability_id": target["id"],
        "capability_version_id": target["id"],
        "package_id": POLICY_PACKAGE_ID,
        "version_no": target["version_no"],
        "digest": target["digest"],
        "filename": target["source_filename"],
        "content_hash": target["content_hash"],
        "storage_key": target["storage_key"],
    }
    bind.execute(
        sa.text(
            "UPDATE node_capability_refs SET capability_version_id = :target_id, "
            "normalized_config = CAST(:config AS JSON) "
            "WHERE capability_type = 'TOOL_POLICY' AND capability_version_id = :source_id"
        ),
        {
            "source_id": source_id,
            "target_id": target_id,
            "config": json.dumps(runtime_config, ensure_ascii=False),
        },
    )
    rows = bind.execute(
        sa.text(
            "SELECT id, runtime_manifest_json FROM run_snapshots "
            "WHERE runtime_manifest_json IS NOT NULL ORDER BY created_at, id"
        )
    ).mappings()
    for row in rows:
        manifest = dict(row["runtime_manifest_json"])
        changed = False
        for raw_node in dict(manifest.get("nodes") or {}).values():
            if not isinstance(raw_node, dict):
                continue
            spec = raw_node.get("agent_spec")
            policy = spec.get("tool_policy") if isinstance(spec, dict) else None
            if isinstance(policy, dict) and policy.get("capability_version_id") == source_id:
                policy.clear()
                policy.update(
                    {
                        "capability_version_id": target_id,
                        "capability_type": "TOOL_POLICY",
                        "capability_key": DEFAULT_TOOL_POLICY_KEY,
                        "digest": target["digest"],
                        "content_hash": target["content_hash"],
                        "runtime_config": runtime_config,
                    }
                )
                spec["openhands_version"] = (
                    OPENHANDS_VERSION if upgrading else spec.get("openhands_version")
                )
                changed = True
        if changed:
            if upgrading:
                manifest["openhands_version"] = OPENHANDS_VERSION
            bind.execute(
                sa.text(
                    "UPDATE run_snapshots SET runtime_manifest_json = CAST(:manifest AS JSON), "
                    "runtime_manifest_hash = :digest WHERE id = :id"
                ),
                {
                    "id": row["id"],
                    "manifest": json.dumps(manifest, ensure_ascii=False),
                    "digest": _hash(manifest),
                },
            )


def upgrade() -> None:
    bind = op.get_bind()
    _seed_policy(bind)
    _rewrite_default_references(bind, upgrading=True)


def downgrade() -> None:
    bind = op.get_bind()
    _rewrite_default_references(bind, upgrading=False)
    bind.execute(
        sa.text("DELETE FROM capability_validations WHERE capability_version_id = :id"),
        {"id": POLICY_VERSION_ID},
    )
    bind.execute(
        sa.text("DELETE FROM capability_versions WHERE id = :id"),
        {"id": POLICY_VERSION_ID},
    )
    bind.execute(
        sa.text(
            "DELETE FROM capability_blobs WHERE id = :id AND NOT EXISTS "
            "(SELECT 1 FROM capability_versions WHERE blob_id = :id)"
        ),
        {"id": POLICY_BLOB_ID},
    )
