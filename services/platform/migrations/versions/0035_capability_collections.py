"""replace Skill-only collections with generic immutable Capability collections

Revision ID: 0035_capability_collections
Revises: 0034_runtime_policies
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0035_capability_collections"
down_revision = "0034_runtime_policies"
branch_labels = None
depends_on = None


def _create_target_tables() -> None:
    op.create_table(
        "capability_collections",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("name", sa.String(200), nullable=False),
        sa.Column("category", sa.String(120), nullable=False, server_default=""),
        sa.Column("description", sa.Text(), nullable=False, server_default=""),
        sa.Column("row_version", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("name", name="uq_capability_collections_name"),
    )
    op.create_table(
        "capability_collection_items",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column(
            "collection_id",
            sa.String(36),
            sa.ForeignKey("capability_collections.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "capability_version_id",
            sa.String(36),
            sa.ForeignKey("capability_versions.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column("position", sa.Integer(), nullable=False),
        sa.CheckConstraint("position >= 0", name="ck_capability_collection_item_position"),
        sa.UniqueConstraint(
            "collection_id",
            "capability_version_id",
            name="uq_capability_collection_version",
        ),
    )
    op.create_index(
        "ix_capability_collection_items_collection_id",
        "capability_collection_items",
        ["collection_id"],
    )
    op.create_index(
        "ix_capability_collection_items_capability_version_id",
        "capability_collection_items",
        ["capability_version_id"],
    )


def upgrade() -> None:
    bind = op.get_bind()
    _create_target_tables()
    bind.execute(
        sa.text(
            "INSERT INTO capability_collections "
            "(id, name, category, description, row_version, created_at, updated_at) "
            "SELECT id, name, category, description, row_version, created_at, updated_at "
            "FROM skill_collections"
        )
    )
    bind.execute(
        sa.text(
            "INSERT INTO capability_collection_items "
            "(id, collection_id, capability_version_id, position) "
            "SELECT id, collection_id, capability_version_id, position "
            "FROM skill_collection_items"
        )
    )
    op.drop_table("skill_collection_items")
    op.drop_table("skill_collections")


def downgrade() -> None:
    bind = op.get_bind()
    op.create_table(
        "skill_collections",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("name", sa.String(200), nullable=False),
        sa.Column("category", sa.String(120), nullable=False, server_default=""),
        sa.Column("description", sa.Text(), nullable=False, server_default=""),
        sa.Column("row_version", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("name", name="uq_skill_collections_name"),
    )
    op.create_table(
        "skill_collection_items",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column(
            "collection_id",
            sa.String(36),
            sa.ForeignKey("skill_collections.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "capability_import_id",
            sa.String(36),
            sa.ForeignKey("capability_imports.id", ondelete="RESTRICT"),
            nullable=True,
        ),
        sa.Column("capability_position", sa.Integer(), nullable=True),
        sa.Column("capability_version_id", sa.String(36), nullable=False),
        sa.Column("position", sa.Integer(), nullable=False),
        sa.CheckConstraint(
            "capability_position >= 0", name="ck_skill_collection_capability_position"
        ),
        sa.CheckConstraint("position >= 0", name="ck_skill_collection_item_position"),
        sa.UniqueConstraint(
            "collection_id",
            "capability_import_id",
            "capability_position",
            name="uq_skill_collection_capability_version",
        ),
        sa.UniqueConstraint(
            "collection_id",
            "capability_version_id",
            name="uq_skill_collection_capability_version_id",
        ),
        sa.ForeignKeyConstraint(
            ["capability_version_id"],
            ["capability_versions.id"],
            name="fk_skill_collection_item_version",
            ondelete="RESTRICT",
        ),
    )
    op.create_index(
        "ix_skill_collection_items_collection_id",
        "skill_collection_items",
        ["collection_id"],
    )
    op.create_index(
        "ix_skill_collection_items_capability_import_id",
        "skill_collection_items",
        ["capability_import_id"],
    )
    op.create_index(
        "ix_skill_collection_items_capability_version_id",
        "skill_collection_items",
        ["capability_version_id"],
    )
    bind.execute(
        sa.text(
            "INSERT INTO skill_collections "
            "(id, name, category, description, row_version, created_at, updated_at) "
            "SELECT id, name, category, description, row_version, created_at, updated_at "
            "FROM capability_collections"
        )
    )
    bind.execute(
        sa.text(
            "INSERT INTO skill_collection_items "
            "(id, collection_id, capability_import_id, capability_position, "
            "capability_version_id, position) "
            "SELECT i.id, i.collection_id, v.source_import_id, v.source_position, "
            "i.capability_version_id, i.position "
            "FROM capability_collection_items i "
            "JOIN capability_versions v ON v.id = i.capability_version_id"
        )
    )
    op.drop_table("capability_collection_items")
    op.drop_table("capability_collections")
