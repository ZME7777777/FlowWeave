"""Publish the current 1.44.0 default Tool Policy.

Revision ID: 0073_default_tool_policy_v4
Revises: 0072_flow_node_locator
Create Date: 2026-08-30

The previous built-in v3 preserved 1.42.0 source provenance.  The Runtime now
uses the fixed 1.44.0 catalog, so selecting that stale version for a new Node
Asset fails closed while normalizing its frozen tool entries.  Publish v4 as a
new immutable version; do not mutate v1--v3.  Only editable Node Asset
references advance to the new platform default; historical Run Snapshots stay
frozen with their originally selected policy identity.
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
)

revision = "0073_default_tool_policy_v4"
down_revision = "0072_flow_node_locator"
branch_labels = None
depends_on = None

PACKAGE_ID = str(uuid5(NAMESPACE_URL, f"flowweave:package:TOOL_POLICY:{DEFAULT_TOOL_POLICY_KEY}"))
V3_ID = str(uuid5(NAMESPACE_URL, f"flowweave:version:builtin:{DEFAULT_TOOL_POLICY_KEY}:3"))
V4_ID = str(uuid5(NAMESPACE_URL, f"flowweave:version:builtin:{DEFAULT_TOOL_POLICY_KEY}:4"))
CONFIG_BYTES = json.dumps(
    DEFAULT_TOOL_POLICY_CONFIG, ensure_ascii=False, sort_keys=True, separators=(",", ":")
).encode()
CONTENT_HASH = hashlib.sha256(CONFIG_BYTES).hexdigest()
BLOB_ID = str(uuid5(NAMESPACE_URL, f"flowweave:blob:{CONTENT_HASH}"))
VALIDATION_ID = str(uuid5(NAMESPACE_URL, f"flowweave:validation:{V4_ID}"))
DIGEST = capability_version_digest(
    "TOOL_POLICY", DEFAULT_TOOL_POLICY_KEY, CONTENT_HASH, DEFAULT_TOOL_POLICY_CONFIG
)


def _compatible_version_id(bind: sa.Connection) -> str | None:
    return bind.scalar(
        sa.text(
            "SELECT id FROM capability_versions WHERE package_id = :package AND digest = :digest"
        ),
        {"package": PACKAGE_ID, "digest": DIGEST},
    )


def _seed(bind: sa.Connection) -> str:
    existing = _compatible_version_id(bind)
    if existing is not None:
        # An empty database runs migration 0046 with this source tree and has
        # already created the content-identical default version.  Do not
        # manufacture a duplicate immutable digest merely to renumber it.
        return existing
    now = datetime.now(UTC)
    bind.execute(
        sa.text(
            "INSERT INTO capability_blobs "
            "(id, content_hash, storage_key, byte_size, media_type, created_at) "
            "VALUES (:id, :hash, :key, :size, 'application/json', :now) "
            "ON CONFLICT (id) DO NOTHING"
        ),
        {
            "id": BLOB_ID,
            "hash": CONTENT_HASH,
            "key": f"builtin://tool-policies/{CONTENT_HASH}.json",
            "size": len(CONFIG_BYTES),
            "now": now,
        },
    )
    bind.execute(
        sa.text(
            "INSERT INTO capability_versions "
            "(id, package_id, blob_id, version_no, digest, normalized_config_json, "
            "source_filename, source_import_id, source_position, state, created_at) "
            "VALUES (:id, :package, :blob, 4, :digest, CAST(:config AS JSON), "
            "'flowweave-default-tools-v4.json', NULL, NULL, 'PUBLISHED', :now) "
            "ON CONFLICT (id) DO NOTHING"
        ),
        {
            "id": V4_ID,
            "package": PACKAGE_ID,
            "blob": BLOB_ID,
            "digest": DIGEST,
            "config": json.dumps(DEFAULT_TOOL_POLICY_CONFIG, ensure_ascii=False),
            "now": now,
        },
    )
    bind.execute(
        sa.text(
            "INSERT INTO capability_validations "
            "(id, capability_version_id, validator, status, report_json, created_at) "
            "VALUES (:id, :version, 'flowweave-builtin-v4', 'PASSED', "
            "CAST(:report AS JSON), :now) ON CONFLICT (id) DO NOTHING"
        ),
        {
            "id": VALIDATION_ID,
            "version": V4_ID,
            "report": json.dumps(
                {
                    "builtin": True,
                    "openhands_version": DEFAULT_TOOL_POLICY_CONFIG["openhands_version"],
                    "source_commit": DEFAULT_TOOL_POLICY_CONFIG["source_commit"],
                    "catalog_digest": DEFAULT_TOOL_POLICY_CONFIG["catalog_digest"],
                }
            ),
            "now": now,
        },
    )
    return V4_ID


def _runtime_config() -> dict[str, object]:
    return {
        **DEFAULT_TOOL_POLICY_CONFIG,
        "capability_id": V4_ID,
        "capability_version_id": V4_ID,
        "package_id": PACKAGE_ID,
        "version_no": 4,
        "digest": DIGEST,
        "filename": "flowweave-default-tools-v4.json",
        "content_hash": CONTENT_HASH,
        "storage_key": f"builtin://tool-policies/{CONTENT_HASH}.json",
    }


def _rewrite(
    bind: sa.Connection, *, source_id: str, target_id: str, target_config: dict[str, object]
) -> None:
    bind.execute(
        sa.text(
            "UPDATE node_capability_refs SET capability_version_id = :target, "
            "normalized_config = CAST(:config AS JSON) "
            "WHERE capability_type = 'TOOL_POLICY' AND capability_version_id = :source"
        ),
        {
            "source": source_id,
            "target": target_id,
            "config": json.dumps(target_config, ensure_ascii=False),
        },
    )


def upgrade() -> None:
    bind = op.get_bind()
    target_id = _seed(bind)
    if target_id == V4_ID:
        _rewrite(bind, source_id=V3_ID, target_id=V4_ID, target_config=_runtime_config())


def downgrade() -> None:
    bind = op.get_bind()
    if _compatible_version_id(bind) != V4_ID:
        return
    previous = (
        bind.execute(
            sa.text(
                "SELECT v.digest, v.normalized_config_json, v.source_filename, "
                "b.content_hash, b.storage_key FROM capability_versions v "
                "JOIN capability_blobs b ON b.id = v.blob_id WHERE v.id = :id"
            ),
            {"id": V3_ID},
        )
        .mappings()
        .one()
    )
    _rewrite(
        bind,
        source_id=V4_ID,
        target_id=V3_ID,
        target_config={
            **dict(previous["normalized_config_json"]),
            "capability_id": V3_ID,
            "capability_version_id": V3_ID,
            "package_id": PACKAGE_ID,
            "version_no": 3,
            "digest": previous["digest"],
            "filename": previous["source_filename"],
            "content_hash": previous["content_hash"],
            "storage_key": previous["storage_key"],
        },
    )
    bind.execute(
        sa.text("DELETE FROM capability_validations WHERE capability_version_id = :id"),
        {"id": V4_ID},
    )
    bind.execute(sa.text("DELETE FROM capability_versions WHERE id = :id"), {"id": V4_ID})
