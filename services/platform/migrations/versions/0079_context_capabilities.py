"""Add frozen node Context references and remove deprecated I/O template URLs.

Revision ID: 0079_context_capabilities
Revises: 0078_node_attempt_work_dirs
Create Date: 2026-08-31
"""

import sqlalchemy as sa
from alembic import op

revision = "0079_context_capabilities"
down_revision = "0078_node_attempt_work_dirs"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.drop_column("node_io_fields", "template_url")
    op.create_table(
        "node_context_capabilities",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("node_asset_id", sa.String(length=36), nullable=False),
        sa.Column("capability_version_id", sa.String(length=36), nullable=False),
        sa.Column("position", sa.Integer(), nullable=False),
        sa.CheckConstraint("position >= 0", name="ck_node_context_capability_position"),
        sa.ForeignKeyConstraint(
            ["node_asset_id"], ["node_assets.id"], ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(
            ["capability_version_id"], ["capability_versions.id"], ondelete="RESTRICT"
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "node_asset_id", "capability_version_id", name="uq_node_context_capability"
        ),
    )
    op.create_index(
        "ix_node_context_capabilities_node_asset_id",
        "node_context_capabilities",
        ["node_asset_id"],
    )
    op.create_index(
        "ix_node_context_capabilities_capability_version_id",
        "node_context_capabilities",
        ["capability_version_id"],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_node_context_capabilities_capability_version_id",
        table_name="node_context_capabilities",
    )
    op.drop_index(
        "ix_node_context_capabilities_node_asset_id",
        table_name="node_context_capabilities",
    )
    op.drop_table("node_context_capabilities")
    op.add_column(
        "node_io_fields",
        sa.Column("template_url", sa.Text(), nullable=False, server_default=""),
    )
    op.alter_column("node_io_fields", "template_url", server_default=None)
