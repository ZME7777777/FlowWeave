"""freeze immutable capability runtime manifests on run snapshots

Revision ID: 0030_snapshot_runtime_manifest
Revises: 0029_capability_repository
"""

from __future__ import annotations

import hashlib
import json

import sqlalchemy as sa
from alembic import op

revision = "0030_snapshot_runtime_manifest"
down_revision = "0029_capability_repository"
branch_labels = None
depends_on = None


def _hash(value: object) -> str:
    encoded = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(encoded).hexdigest()


def _version_digest(
    capability_type: str,
    capability_key: str,
    content_hash: str,
    config: object,
) -> str:
    return _hash(
        {
            "capability_type": capability_type,
            "capability_key": capability_key,
            "content_hash": content_hash,
            "normalized_config": config,
        }
    )


def _version_config(
    bind: sa.Connection,
    raw_capability: dict[object, object],
    normalized: dict[object, object],
    snapshot_id: str,
) -> tuple[str, str, str, dict[str, object]]:
    raw_id = str(
        raw_capability.get("capability_id") or normalized.get("capability_version_id") or ""
    )
    params: dict[str, object]
    if len(raw_id) == 36:
        where = "v.id = :version_id"
        params = {"version_id": raw_id}
    else:
        import_id = str(normalized.get("import_id") or "")
        position: int | None = None
        if ":" in raw_id and raw_id.rsplit(":", 1)[1].isdigit():
            legacy_import, raw_position = raw_id.rsplit(":", 1)
            import_id = import_id or legacy_import
            position = int(raw_position)
        if not import_id or position is None:
            raise RuntimeError(f"Snapshot {snapshot_id} lacks an immutable capability version")
        where = "v.source_import_id = :import_id AND v.source_position = :position"
        params = {"import_id": import_id, "position": position}
    row = (
        bind.execute(
            sa.text(
                "SELECT v.id, v.package_id, v.version_no, v.digest, "
                "v.normalized_config_json, v.source_filename, "
                "b.content_hash, b.storage_key "
                "FROM capability_versions v "
                "JOIN capability_blobs b ON b.id = v.blob_id "
                f"WHERE {where}"
            ),
            params,
        )
        .mappings()
        .one_or_none()
    )
    if row is None and len(raw_id) != 36:
        # Migration 0029 intentionally folds historical imports with the same
        # immutable digest into one canonical Version. The canonical row keeps
        # the first import as provenance, so a Snapshot that names a later
        # duplicate source tuple must resolve through that import's frozen
        # preview rather than through source_import_id.
        imported = (
            bind.execute(
                sa.text(
                    "SELECT capability_type, content_hash, preview_json "
                    "FROM capability_imports WHERE id = :import_id"
                ),
                {"import_id": import_id},
            )
            .mappings()
            .one_or_none()
        )
        preview = imported["preview_json"] if imported is not None else None
        entries = preview.get("capabilities") if isinstance(preview, dict) else None
        entry = (
            entries[position]
            if isinstance(entries, list) and position is not None and position < len(entries)
            else None
        )
        capability_type = str(raw_capability.get("capability_type") or "")
        capability_key = str(raw_capability.get("capability_key") or "")
        if (
            imported is not None
            and isinstance(entry, dict)
            and str(imported["capability_type"]) == capability_type
            and str(entry.get("capability_key") or "") == capability_key
        ):
            digest = _version_digest(
                capability_type,
                capability_key,
                str(imported["content_hash"]),
                entry.get("normalized_config") or {},
            )
            row = (
                bind.execute(
                    sa.text(
                        "SELECT v.id, v.package_id, v.version_no, v.digest, "
                        "v.normalized_config_json, v.source_filename, "
                        "b.content_hash, b.storage_key "
                        "FROM capability_versions v "
                        "JOIN capability_blobs b ON b.id = v.blob_id "
                        "WHERE v.digest = :digest"
                    ),
                    {"digest": digest},
                )
                .mappings()
                .one_or_none()
            )
    if row is None or not isinstance(row["normalized_config_json"], dict):
        raise RuntimeError(f"Snapshot {snapshot_id} lacks an immutable capability version")
    runtime_config: dict[str, object] = {
        **row["normalized_config_json"],
        "capability_id": str(row["id"]),
        "capability_version_id": str(row["id"]),
        "package_id": str(row["package_id"]),
        "version_no": int(row["version_no"]),
        "digest": str(row["digest"]),
        "filename": str(row["source_filename"]),
        "content_hash": str(row["content_hash"]),
        "storage_key": str(row["storage_key"]),
    }
    return (
        str(row["id"]),
        str(row["digest"]),
        str(row["content_hash"]),
        runtime_config,
    )


def _manifest(bind: sa.Connection, definition: object, snapshot_id: str) -> dict[str, object]:
    if not isinstance(definition, dict) or not isinstance(definition.get("nodes"), list):
        raise RuntimeError(f"Cannot compile Runtime Manifest for Snapshot {snapshot_id}")
    nodes: dict[str, object] = {}
    for raw_node in definition["nodes"]:
        if not isinstance(raw_node, dict):
            raise RuntimeError(f"Snapshot {snapshot_id} contains an invalid node")
        instance_key = str(raw_node.get("instance_key") or "")
        asset = raw_node.get("asset")
        if not instance_key or not isinstance(asset, dict):
            raise RuntimeError(f"Snapshot {snapshot_id} contains an invalid node asset")
        capabilities = asset.get("capabilities", [])
        if not isinstance(capabilities, list):
            raise RuntimeError(f"Snapshot {snapshot_id} contains invalid capabilities")
        frozen: list[dict[str, object]] = []
        for raw_capability in capabilities:
            if not isinstance(raw_capability, dict):
                raise RuntimeError(f"Snapshot {snapshot_id} contains an invalid capability")
            normalized = raw_capability.get("normalized_config")
            if not isinstance(normalized, dict):
                raise RuntimeError(f"Snapshot {snapshot_id} lacks capability configuration")
            version_id, digest, content_hash, runtime_config = _version_config(
                bind, raw_capability, normalized, snapshot_id
            )
            frozen.append(
                {
                    "capability_version_id": version_id,
                    "capability_type": str(raw_capability.get("capability_type") or ""),
                    "capability_key": str(raw_capability.get("capability_key") or ""),
                    "digest": digest,
                    "content_hash": content_hash,
                    "runtime_config": runtime_config,
                }
            )
        nodes[instance_key] = {
            "node_asset_id": str(raw_node.get("node_asset_id") or asset.get("id") or ""),
            "capabilities": frozen,
        }
    return {"schema_version": 1, "nodes": nodes}


def upgrade() -> None:
    bind = op.get_bind()
    columns = {column["name"] for column in sa.inspect(bind).get_columns("run_snapshots")}
    if "runtime_manifest_json" not in columns:
        op.add_column("run_snapshots", sa.Column("runtime_manifest_json", sa.JSON(), nullable=True))
    if "runtime_manifest_hash" not in columns:
        op.add_column(
            "run_snapshots", sa.Column("runtime_manifest_hash", sa.String(64), nullable=True)
        )
    rows = bind.execute(
        sa.text("SELECT id, definition_json FROM run_snapshots ORDER BY created_at, id")
    ).mappings()
    for row in rows:
        manifest = _manifest(bind, row["definition_json"], str(row["id"]))
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
    op.alter_column(
        "run_snapshots", "runtime_manifest_json", existing_type=sa.JSON(), nullable=False
    )
    op.alter_column(
        "run_snapshots",
        "runtime_manifest_hash",
        existing_type=sa.String(64),
        nullable=False,
    )


def downgrade() -> None:
    bind = op.get_bind()
    columns = {column["name"] for column in sa.inspect(bind).get_columns("run_snapshots")}
    if "runtime_manifest_hash" in columns:
        op.drop_column("run_snapshots", "runtime_manifest_hash")
    if "runtime_manifest_json" in columns:
        op.drop_column("run_snapshots", "runtime_manifest_json")
