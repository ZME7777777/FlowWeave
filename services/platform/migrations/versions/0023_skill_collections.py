"""add virtual Skill collections

Revision ID: 0023_skill_collections
Revises: 0022_attempt_model_reasoning
"""

import sqlalchemy as sa
from alembic import op

revision = "0023_skill_collections"
down_revision = "0022_attempt_model_reasoning"
branch_labels = None
depends_on = None


def upgrade() -> None:
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
            nullable=False,
        ),
        sa.Column("capability_position", sa.Integer(), nullable=False),
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


def downgrade() -> None:
    op.drop_table("skill_collection_items")
    op.drop_table("skill_collections")
