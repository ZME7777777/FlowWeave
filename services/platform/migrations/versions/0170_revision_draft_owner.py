"""Align runtime revision draft ownership with the tenant-aware ORM contract."""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0170_revision_draft_owner"
down_revision = "0169_merge_case_history"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("flow_run_revision_drafts", sa.Column("owner_user_id", sa.String(36)))
    op.execute(
        sa.text(
            "UPDATE flow_run_revision_drafts AS draft SET owner_user_id = run.owner_user_id "
            "FROM flow_runs AS run WHERE run.id = draft.flow_run_id"
        )
    )
    op.alter_column("flow_run_revision_drafts", "owner_user_id", nullable=False)
    op.create_index(
        "ix_flow_run_revision_drafts_owner_user_id",
        "flow_run_revision_drafts",
        ["owner_user_id"],
    )
    op.drop_index("uq_flow_run_revision_draft_active_base", "flow_run_revision_drafts")
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


def downgrade() -> None:
    op.execute(
        sa.text("DROP POLICY pl_tenant_flow_run_revision_drafts ON flow_run_revision_drafts")
    )
    op.execute(sa.text("ALTER TABLE flow_run_revision_drafts NO FORCE ROW LEVEL SECURITY"))
    op.execute(sa.text("ALTER TABLE flow_run_revision_drafts DISABLE ROW LEVEL SECURITY"))
    op.drop_index("ix_flow_run_revision_drafts_owner_user_id", "flow_run_revision_drafts")
    op.drop_index("uq_flow_run_revision_draft_active_base", "flow_run_revision_drafts")
    op.create_index(
        "uq_flow_run_revision_draft_active_base",
        "flow_run_revision_drafts",
        ["flow_run_id", "base_snapshot_id"],
        unique=True,
        postgresql_where=sa.text("state = 'DRAFT'"),
        sqlite_where=sa.text("state = 'DRAFT'"),
    )
    op.drop_column("flow_run_revision_drafts", "owner_user_id")
