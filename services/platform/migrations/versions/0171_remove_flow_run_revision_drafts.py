"""remove superseded FlowRun runtime revision drafts.

Revision ID: 0171_remove_flow_run_revision_drafts
Revises: 0170_revision_draft_owner
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0171_remove_flow_run_revision_drafts"
down_revision = "0170_revision_draft_owner"
branch_labels = None
depends_on = None


def upgrade() -> None:
    connection = op.get_bind()
    if connection.dialect.name == "postgresql":
        # Serialize the emptiness check with writers from the previous release.
        op.execute("SET LOCAL lock_timeout = '5s'")
        op.execute("LOCK TABLE flow_run_revision_drafts IN ACCESS EXCLUSIVE MODE")
        # Refuse the migration if RLS would hide records from this role.
        op.execute("SET LOCAL row_security = off")
    if connection.execute(sa.text("SELECT count(*) FROM flow_run_revision_drafts")).scalar_one():
        raise RuntimeError("Cannot retire nonempty FlowRun revision drafts; preserve existing data")
    op.drop_table("flow_run_revision_drafts")


def downgrade() -> None:
    op.create_table(
        "flow_run_revision_drafts",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("owner_user_id", sa.String(length=36), nullable=False),
        sa.Column("flow_run_id", sa.String(length=36), nullable=False),
        sa.Column("base_snapshot_id", sa.String(length=36), nullable=False),
        sa.Column("base_snapshot_version", sa.Integer(), nullable=False),
        sa.Column("definition_json", sa.JSON(), nullable=False),
        sa.Column("definition_hash", sa.String(length=64), nullable=False),
        sa.Column("row_version", sa.Integer(), nullable=False),
        sa.Column("state", sa.String(length=20), nullable=False),
        sa.Column("applied_snapshot_id", sa.String(length=36), nullable=True),
        sa.Column("reason", sa.Text(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.CheckConstraint(
            "state IN ('DRAFT', 'APPLIED', 'REJECTED')",
            name="ck_flow_run_revision_draft_state",
        ),
    )
    op.create_index(
        "ix_flow_run_revision_drafts_flow_run_id",
        "flow_run_revision_drafts",
        ["flow_run_id"],
    )
    op.create_index(
        "ix_flow_run_revision_drafts_owner_user_id",
        "flow_run_revision_drafts",
        ["owner_user_id"],
    )
    op.create_index(
        "uq_flow_run_revision_draft_active_base",
        "flow_run_revision_drafts",
        ["owner_user_id", "flow_run_id", "base_snapshot_id"],
        unique=True,
        postgresql_where=sa.text("state = 'DRAFT'"),
        sqlite_where=sa.text("state = 'DRAFT'"),
    )
    predicate = (
        "current_setting('flowweave.bypass', true) = 'on' OR "
        "owner_user_id = current_setting('flowweave.user_id', true)"
    )
    op.execute(sa.text("ALTER TABLE flow_run_revision_drafts ENABLE ROW LEVEL SECURITY"))
    op.execute(sa.text("ALTER TABLE flow_run_revision_drafts FORCE ROW LEVEL SECURITY"))
    op.execute(
        sa.text(
            "CREATE POLICY pl_tenant_flow_run_revision_drafts ON flow_run_revision_drafts "
            f"USING ({predicate}) WITH CHECK ({predicate})"
        )
    )
