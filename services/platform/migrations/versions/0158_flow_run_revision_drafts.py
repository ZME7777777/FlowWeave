"""add FlowRun runtime revision drafts.

Revision ID: 0158_flow_run_revision_drafts
Revises: 0157_background_task_aggregate_type_length
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0158_flow_run_revision_drafts"
down_revision = "0157_background_task_aggregate_type_length"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "flow_run_revision_drafts",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("flow_run_id", sa.String(length=36), nullable=False),
        sa.Column("base_snapshot_id", sa.String(length=36), nullable=False),
        sa.Column("base_snapshot_version", sa.Integer(), nullable=False),
        sa.Column("definition_json", sa.JSON(), nullable=False),
        sa.Column("definition_hash", sa.String(length=64), nullable=False),
        sa.Column("row_version", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("state", sa.String(length=20), nullable=False, server_default="DRAFT"),
        sa.Column("applied_snapshot_id", sa.String(length=36), nullable=True),
        sa.Column("reason", sa.Text(), nullable=False, server_default=""),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.CheckConstraint(
            "state IN ('DRAFT', 'APPLIED', 'REJECTED')",
            name="ck_flow_run_revision_draft_state",
        ),
    )
    op.create_index(
        "uq_flow_run_revision_draft_active_base",
        "flow_run_revision_drafts",
        ["flow_run_id", "base_snapshot_id"],
        unique=True,
        postgresql_where=sa.text("state = 'DRAFT'"),
        sqlite_where=sa.text("state = 'DRAFT'"),
    )
    op.create_index(
        "ix_flow_run_revision_drafts_flow_run_id",
        "flow_run_revision_drafts",
        ["flow_run_id"],
    )
    op.alter_column("flow_run_revision_drafts", "row_version", server_default=None)
    op.alter_column("flow_run_revision_drafts", "state", server_default=None)
    op.alter_column("flow_run_revision_drafts", "reason", server_default=None)


def downgrade() -> None:
    op.drop_index("ix_flow_run_revision_drafts_flow_run_id", table_name="flow_run_revision_drafts")
    op.drop_table("flow_run_revision_drafts")
