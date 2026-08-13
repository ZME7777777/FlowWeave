"""add immutable capability repository and version references

Revision ID: 0029_capability_repository
Revises: 0028_condensation_commands
"""

from __future__ import annotations

import hashlib
import json
from collections import defaultdict
from uuid import NAMESPACE_URL, uuid5

import sqlalchemy as sa
from alembic import op

revision = "0029_capability_repository"
down_revision = "0028_condensation_commands"
branch_labels = None
depends_on = None


def _id(kind: str, value: str) -> str:
    return str(uuid5(NAMESPACE_URL, f"flowweave:{kind}:{value}"))


def _digest(capability_type: str, capability_key: str, content_hash: str, config: object) -> str:
    encoded = json.dumps(
        {
            "capability_type": capability_type,
            "capability_key": capability_key,
            "content_hash": content_hash,
            "normalized_config": config,
        },
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode()
    return hashlib.sha256(encoded).hexdigest()


def _columns(bind: sa.Connection, table: str) -> set[str]:
    return {str(column["name"]) for column in sa.inspect(bind).get_columns(table)}


def _foreign_keys(bind: sa.Connection, table: str) -> set[str]:
    return {str(item.get("name") or "") for item in sa.inspect(bind).get_foreign_keys(table)}


def upgrade() -> None:
    bind = op.get_bind()
    tables = set(sa.inspect(bind).get_table_names())
    if "capability_blobs" not in tables:
        op.create_table(
            "capability_blobs",
            sa.Column("id", sa.String(36), primary_key=True),
            sa.Column("content_hash", sa.String(64), nullable=False),
            sa.Column("storage_key", sa.Text(), nullable=False),
            sa.Column("byte_size", sa.Integer(), nullable=False),
            sa.Column("media_type", sa.String(120), nullable=False),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
            sa.UniqueConstraint("content_hash", name="uq_capability_blob_content_hash"),
            sa.UniqueConstraint("storage_key", name="uq_capability_blob_storage_key"),
        )
        op.create_index("ix_capability_blobs_content_hash", "capability_blobs", ["content_hash"])
    if "capability_packages" not in tables:
        op.create_table(
            "capability_packages",
            sa.Column("id", sa.String(36), primary_key=True),
            sa.Column("capability_type", sa.String(32), nullable=False),
            sa.Column("capability_key", sa.String(200), nullable=False),
            sa.Column("display_name", sa.String(200), nullable=False),
            sa.Column("description", sa.Text(), nullable=False),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
            sa.UniqueConstraint(
                "capability_type", "capability_key", name="uq_capability_package_identity"
            ),
        )
        op.create_index(
            "ix_capability_packages_capability_type",
            "capability_packages",
            ["capability_type"],
        )
    if "capability_versions" not in tables:
        op.create_table(
            "capability_versions",
            sa.Column("id", sa.String(36), primary_key=True),
            sa.Column(
                "package_id",
                sa.String(36),
                sa.ForeignKey("capability_packages.id", ondelete="RESTRICT"),
                nullable=False,
            ),
            sa.Column(
                "blob_id",
                sa.String(36),
                sa.ForeignKey("capability_blobs.id", ondelete="RESTRICT"),
                nullable=False,
            ),
            sa.Column("version_no", sa.Integer(), nullable=False),
            sa.Column("digest", sa.String(64), nullable=False),
            sa.Column("normalized_config_json", sa.JSON(), nullable=False),
            sa.Column("source_filename", sa.String(255), nullable=False),
            sa.Column(
                "source_import_id",
                sa.String(36),
                sa.ForeignKey("capability_imports.id", ondelete="SET NULL"),
                nullable=True,
            ),
            sa.Column("source_position", sa.Integer(), nullable=True),
            sa.Column("state", sa.String(20), nullable=False),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
            sa.CheckConstraint("version_no > 0", name="ck_capability_version_number_positive"),
            sa.CheckConstraint(
                "state IN ('PUBLISHED', 'RETIRED')", name="ck_capability_version_state"
            ),
            sa.UniqueConstraint("package_id", "version_no", name="uq_capability_version_number"),
            sa.UniqueConstraint(
                "source_import_id", "source_position", name="uq_capability_version_import_position"
            ),
            sa.UniqueConstraint("digest", name="uq_capability_version_digest"),
        )
        for column in ("package_id", "blob_id", "digest", "source_import_id", "state"):
            op.create_index(f"ix_capability_versions_{column}", "capability_versions", [column])
    if "capability_dependencies" not in tables:
        op.create_table(
            "capability_dependencies",
            sa.Column("id", sa.String(36), primary_key=True),
            sa.Column(
                "capability_version_id",
                sa.String(36),
                sa.ForeignKey("capability_versions.id", ondelete="CASCADE"),
                nullable=False,
            ),
            sa.Column("ecosystem", sa.String(40), nullable=False),
            sa.Column("name", sa.String(160), nullable=False),
            sa.Column("version", sa.String(100), nullable=False),
            sa.UniqueConstraint(
                "capability_version_id", "ecosystem", "name", name="uq_capability_dependency"
            ),
        )
        op.create_index(
            "ix_capability_dependencies_capability_version_id",
            "capability_dependencies",
            ["capability_version_id"],
        )
    if "capability_validations" not in tables:
        op.create_table(
            "capability_validations",
            sa.Column("id", sa.String(36), primary_key=True),
            sa.Column(
                "capability_version_id",
                sa.String(36),
                sa.ForeignKey("capability_versions.id", ondelete="CASCADE"),
                nullable=False,
            ),
            sa.Column("validator", sa.String(100), nullable=False),
            sa.Column("status", sa.String(20), nullable=False),
            sa.Column("report_json", sa.JSON(), nullable=False),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
            sa.CheckConstraint(
                "status IN ('PASSED', 'FAILED')", name="ck_capability_validation_status"
            ),
        )
        op.create_index(
            "ix_capability_validations_capability_version_id",
            "capability_validations",
            ["capability_version_id"],
        )

    if "capability_version_id" not in _columns(bind, "node_capability_refs"):
        op.add_column(
            "node_capability_refs", sa.Column("capability_version_id", sa.String(36), nullable=True)
        )
        op.create_index(
            "ix_node_capability_refs_capability_version_id",
            "node_capability_refs",
            ["capability_version_id"],
        )
    if "capability_version_id" not in _columns(bind, "skill_collection_items"):
        op.add_column(
            "skill_collection_items",
            sa.Column("capability_version_id", sa.String(36), nullable=True),
        )
        op.create_index(
            "ix_skill_collection_items_capability_version_id",
            "skill_collection_items",
            ["capability_version_id"],
        )

    imports = bind.execute(
        sa.text(
            "SELECT id, capability_type, filename, content_hash, storage_key, byte_size, "
            "preview_json, consumed_at, created_at FROM capability_imports "
            "WHERE state = 'COMMITTED' ORDER BY COALESCE(consumed_at, created_at), id"
        )
    ).mappings()
    version_by_source: dict[tuple[str, int], str] = {}
    version_numbers: defaultdict[tuple[str, str], int] = defaultdict(int)
    seen_blobs: set[str] = set()
    seen_packages: set[tuple[str, str]] = set()
    for imported in imports:
        content_hash = str(imported["content_hash"])
        blob_id = _id("blob", content_hash)
        if content_hash not in seen_blobs:
            bind.execute(
                sa.text(
                    "INSERT INTO capability_blobs "
                    "(id, content_hash, storage_key, byte_size, media_type, created_at) "
                    "VALUES (:id, :hash, :storage, :size, :media, :created) "
                    "ON CONFLICT (content_hash) DO NOTHING"
                ),
                {
                    "id": blob_id,
                    "hash": content_hash,
                    "storage": imported["storage_key"],
                    "size": imported["byte_size"],
                    "media": "application/zip"
                    if imported["capability_type"] == "SKILL"
                    else "application/json",
                    "created": imported["created_at"],
                },
            )
            actual_blob = bind.execute(
                sa.text("SELECT id FROM capability_blobs WHERE content_hash = :hash"),
                {"hash": content_hash},
            ).scalar_one()
            blob_id = str(actual_blob)
            seen_blobs.add(content_hash)
        else:
            blob_id = str(
                bind.execute(
                    sa.text("SELECT id FROM capability_blobs WHERE content_hash = :hash"),
                    {"hash": content_hash},
                ).scalar_one()
            )
        preview = imported["preview_json"] or {}
        entries = preview.get("capabilities", []) if isinstance(preview, dict) else []
        for position, raw_entry in enumerate(entries if isinstance(entries, list) else []):
            if not isinstance(raw_entry, dict):
                continue
            capability_type = str(imported["capability_type"])
            capability_key = str(raw_entry.get("capability_key") or "")
            if not capability_key:
                continue
            identity = (capability_type, capability_key)
            package_id = _id("package", f"{capability_type}\0{capability_key}")
            normalized = raw_entry.get("normalized_config") or {}
            if identity not in seen_packages:
                bind.execute(
                    sa.text(
                        "INSERT INTO capability_packages "
                        "(id, capability_type, capability_key, display_name, description, "
                        "created_at, updated_at) "
                        "VALUES (:id, :type, :key, :name, :description, :created, :created) "
                        "ON CONFLICT (capability_type, capability_key) DO NOTHING"
                    ),
                    {
                        "id": package_id,
                        "type": capability_type,
                        "key": capability_key,
                        "name": capability_key,
                        "description": str(normalized.get("description") or "")
                        if isinstance(normalized, dict)
                        else "",
                        "created": imported["consumed_at"] or imported["created_at"],
                    },
                )
                package_id = str(
                    bind.execute(
                        sa.text(
                            "SELECT id FROM capability_packages "
                            "WHERE capability_type = :type AND capability_key = :key"
                        ),
                        {"type": capability_type, "key": capability_key},
                    ).scalar_one()
                )
                seen_packages.add(identity)
            else:
                package_id = str(
                    bind.execute(
                        sa.text(
                            "SELECT id FROM capability_packages "
                            "WHERE capability_type = :type AND capability_key = :key"
                        ),
                        {"type": capability_type, "key": capability_key},
                    ).scalar_one()
                )
            version_numbers[identity] += 1
            version_id = _id("version", f"{imported['id']}:{position}")
            digest = _digest(capability_type, capability_key, content_hash, normalized)
            state = "RETIRED" if raw_entry.get("deleted_at") else "PUBLISHED"
            bind.execute(
                sa.text(
                    "INSERT INTO capability_versions "
                    "(id, package_id, blob_id, version_no, digest, "
                    "normalized_config_json, source_filename, "
                    "source_import_id, source_position, state, created_at) "
                    "VALUES (:id, :package, :blob, :version, :digest, "
                    "CAST(:config AS JSON), :filename, :import_id, :position, :state, "
                    ":created) ON CONFLICT (source_import_id, source_position) DO NOTHING"
                ),
                {
                    "id": version_id,
                    "package": package_id,
                    "blob": blob_id,
                    "version": version_numbers[identity],
                    "digest": digest,
                    "config": json.dumps(normalized),
                    "filename": imported["filename"],
                    "import_id": imported["id"],
                    "position": position,
                    "state": state,
                    "created": imported["consumed_at"] or imported["created_at"],
                },
            )
            version_id = str(
                bind.execute(
                    sa.text(
                        "SELECT id FROM capability_versions "
                        "WHERE source_import_id = :import_id AND source_position = :position"
                    ),
                    {"import_id": imported["id"], "position": position},
                ).scalar_one()
            )
            version_by_source[(str(imported["id"]), position)] = version_id
            dependencies = (
                normalized.get("dependencies", {}) if isinstance(normalized, dict) else {}
            )
            if isinstance(dependencies, dict):
                for ecosystem, values in dependencies.items():
                    if not isinstance(values, dict):
                        continue
                    for name, pinned in values.items():
                        bind.execute(
                            sa.text(
                                "INSERT INTO capability_dependencies "
                                "(id, capability_version_id, ecosystem, name, version) "
                                "VALUES (:id, :version_id, :ecosystem, :name, :version) "
                                "ON CONFLICT DO NOTHING"
                            ),
                            {
                                "id": _id("dependency", f"{version_id}:{ecosystem}:{name}"),
                                "version_id": version_id,
                                "ecosystem": str(ecosystem),
                                "name": str(name),
                                "version": str(pinned),
                            },
                        )
            bind.execute(
                sa.text(
                    "INSERT INTO capability_validations "
                    "(id, capability_version_id, validator, status, report_json, created_at) "
                    "VALUES (:id, :version_id, 'flowweave-import-v1', 'PASSED', "
                    "CAST(:report AS JSON), :created)"
                ),
                {
                    "id": _id("validation", version_id),
                    "version_id": version_id,
                    "report": json.dumps({"migrated_from_import": str(imported["id"])}),
                    "created": imported["consumed_at"] or imported["created_at"],
                },
            )

    refs = bind.execute(
        sa.text(
            "SELECT id, capability_type, capability_key, normalized_config "
            "FROM node_capability_refs"
        )
    ).mappings()
    for ref in refs:
        config = ref["normalized_config"] or {}
        import_id = config.get("import_id") if isinstance(config, dict) else None
        legacy_id = config.get("capability_id") if isinstance(config, dict) else None
        position: int | None = None
        if (
            isinstance(legacy_id, str)
            and ":" in legacy_id
            and legacy_id.rsplit(":", 1)[1].isdigit()
        ):
            legacy_import, raw_position = legacy_id.rsplit(":", 1)
            import_id = import_id or legacy_import
            position = int(raw_position)
        if isinstance(import_id, str) and position is None:
            row = bind.execute(
                sa.text(
                    "SELECT source_position FROM capability_versions v "
                    "JOIN capability_packages p ON p.id = v.package_id "
                    "WHERE v.source_import_id = :import_id "
                    "AND p.capability_type = :type AND p.capability_key = :key "
                    "ORDER BY v.version_no DESC LIMIT 1"
                ),
                {
                    "import_id": import_id,
                    "type": ref["capability_type"],
                    "key": ref["capability_key"],
                },
            ).first()
            position = int(row[0]) if row and row[0] is not None else None
        version_id = (
            version_by_source.get((str(import_id), position))
            if isinstance(import_id, str) and position is not None
            else None
        )
        if version_id is None:
            raise RuntimeError(f"Cannot migrate node capability reference {ref['id']}")
        version = (
            bind.execute(
                sa.text(
                    "SELECT v.package_id, v.version_no, v.digest, v.normalized_config_json, "
                    "v.source_filename, b.content_hash, b.storage_key "
                    "FROM capability_versions v "
                    "JOIN capability_blobs b ON b.id = v.blob_id "
                    "WHERE v.id = :version_id"
                ),
                {"version_id": version_id},
            )
            .mappings()
            .one()
        )
        canonical = version["normalized_config_json"] or {}
        if not isinstance(canonical, dict):
            raise RuntimeError(f"Cannot migrate node capability config {ref['id']}")
        config = {
            **canonical,
            "capability_id": version_id,
            "capability_version_id": version_id,
            "package_id": str(version["package_id"]),
            "version_no": int(version["version_no"]),
            "digest": str(version["digest"]),
            "filename": str(version["source_filename"]),
            "content_hash": str(version["content_hash"]),
            "storage_key": str(version["storage_key"]),
        }
        bind.execute(
            sa.text(
                "UPDATE node_capability_refs SET capability_version_id = :version_id, "
                "normalized_config = CAST(:config AS JSON) WHERE id = :id"
            ),
            {"version_id": version_id, "config": json.dumps(config), "id": ref["id"]},
        )

    for row in bind.execute(
        sa.text("SELECT id, capability_import_id, capability_position FROM skill_collection_items")
    ).mappings():
        version_id = version_by_source.get(
            (str(row["capability_import_id"]), int(row["capability_position"]))
        )
        if version_id is None:
            raise RuntimeError(f"Cannot migrate Skill collection item {row['id']}")
        bind.execute(
            sa.text(
                "UPDATE skill_collection_items SET capability_version_id = :version_id "
                "WHERE id = :id"
            ),
            {"version_id": version_id, "id": row["id"]},
        )

    op.alter_column(
        "node_capability_refs", "capability_version_id", existing_type=sa.String(36), nullable=False
    )
    op.alter_column(
        "skill_collection_items", "capability_import_id", existing_type=sa.String(36), nullable=True
    )
    op.alter_column(
        "skill_collection_items", "capability_position", existing_type=sa.Integer(), nullable=True
    )
    op.alter_column(
        "skill_collection_items",
        "capability_version_id",
        existing_type=sa.String(36),
        nullable=False,
    )
    if "fk_node_capability_ref_version" not in _foreign_keys(bind, "node_capability_refs"):
        op.create_foreign_key(
            "fk_node_capability_ref_version",
            "node_capability_refs",
            "capability_versions",
            ["capability_version_id"],
            ["id"],
            ondelete="RESTRICT",
        )
    if "fk_skill_collection_item_version" not in _foreign_keys(bind, "skill_collection_items"):
        op.create_foreign_key(
            "fk_skill_collection_item_version",
            "skill_collection_items",
            "capability_versions",
            ["capability_version_id"],
            ["id"],
            ondelete="RESTRICT",
        )
    op.create_unique_constraint(
        "uq_skill_collection_capability_version_id",
        "skill_collection_items",
        ["collection_id", "capability_version_id"],
    )


def downgrade() -> None:
    bind = op.get_bind()
    constraints = {
        str(item.get("name") or "")
        for item in sa.inspect(bind).get_unique_constraints("skill_collection_items")
    }
    if "uq_skill_collection_capability_version_id" in constraints:
        op.drop_constraint(
            "uq_skill_collection_capability_version_id", "skill_collection_items", type_="unique"
        )
    for name, table in (
        ("fk_skill_collection_item_version", "skill_collection_items"),
        ("fk_node_capability_ref_version", "node_capability_refs"),
    ):
        if name in _foreign_keys(bind, table):
            op.drop_constraint(name, table, type_="foreignkey")
    op.alter_column(
        "skill_collection_items",
        "capability_import_id",
        existing_type=sa.String(36),
        nullable=False,
    )
    op.alter_column(
        "skill_collection_items", "capability_position", existing_type=sa.Integer(), nullable=False
    )
    if "capability_version_id" in _columns(bind, "skill_collection_items"):
        op.drop_column("skill_collection_items", "capability_version_id")
    if "capability_version_id" in _columns(bind, "node_capability_refs"):
        op.drop_column("node_capability_refs", "capability_version_id")
    for table in (
        "capability_validations",
        "capability_dependencies",
        "capability_versions",
        "capability_packages",
        "capability_blobs",
    ):
        if table in set(sa.inspect(bind).get_table_names()):
            op.drop_table(table)
