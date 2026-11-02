"""link Cases to FlowRuns for observation projection.

Revision ID: 0161_case_flow_run_observations
Revises: 0160_case_control_conversation_directory
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0161_case_flow_run_observations"
down_revision = "0160_case_control_conversation_directory"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "case_flow_runs",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("owner_user_id", sa.String(length=36), nullable=False),
        sa.Column("case_id", sa.String(length=36), nullable=False),
        sa.Column("flow_run_id", sa.String(length=36), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("case_id", "flow_run_id", name="uq_case_flow_run"),
    )
    op.create_index("ix_case_flow_runs_owner_user_id", "case_flow_runs", ["owner_user_id"])
    op.create_index("ix_case_flow_runs_case_id", "case_flow_runs", ["case_id"])
    op.create_index("ix_case_flow_runs_flow_run_id", "case_flow_runs", ["flow_run_id"])
    predicate = (
        "current_setting('flowweave.bypass', true) = 'on' OR "
        "owner_user_id = current_setting('flowweave.user_id', true)"
    )
    op.execute(sa.text('ALTER TABLE "case_flow_runs" ENABLE ROW LEVEL SECURITY'))
    op.execute(sa.text('ALTER TABLE "case_flow_runs" FORCE ROW LEVEL SECURITY'))
    op.execute(
        sa.text(
            'CREATE POLICY "pl_tenant_case_flow_runs" ON "case_flow_runs" '
            f"USING ({predicate}) WITH CHECK ({predicate})"
        )
    )


def downgrade() -> None:
    op.execute(sa.text('DROP POLICY IF EXISTS "pl_tenant_case_flow_runs" ON "case_flow_runs"'))
    op.drop_table("case_flow_runs")
