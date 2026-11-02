"""add autonomous Case decision proposals.

Revision ID: 0162_case_decision_proposals
Revises: 0161_case_flow_run_observations
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0162_case_decision_proposals"
down_revision = "0161_case_flow_run_observations"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "case_decision_proposals",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("owner_user_id", sa.String(length=36), nullable=False),
        sa.Column("case_id", sa.String(length=36), nullable=False),
        sa.Column("flow_run_id", sa.String(length=36), nullable=False),
        sa.Column("node_run_id", sa.String(length=36), nullable=True),
        sa.Column("node_attempt_id", sa.String(length=36), nullable=True),
        sa.Column("proposal_key", sa.String(length=120), nullable=False),
        sa.Column("source_task_id", sa.String(length=36), nullable=True),
        sa.Column("source_binding_id", sa.String(length=36), nullable=True),
        sa.Column("action", sa.String(length=40), nullable=False),
        sa.Column("state", sa.String(length=20), nullable=False, server_default="PROPOSED"),
        sa.Column("reason", sa.Text(), nullable=False, server_default=""),
        sa.Column("payload_json", sa.JSON(), nullable=False, server_default="{}"),
        sa.Column("result_json", sa.JSON(), nullable=False, server_default="{}"),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("executed_at", sa.DateTime(timezone=True), nullable=True),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("case_id", "proposal_key", name="uq_case_decision_proposal_key"),
        sa.CheckConstraint(
            "action IN ('CONTINUE_STEPWISE', 'STOP_FLOW_RUN', 'RERUN_STEPWISE_NODE', "
            "'REVISE_UNSTARTED_RUNTIME', 'COPY_AUTOMATIC_RUN')",
            name="ck_case_decision_proposal_action",
        ),
        sa.CheckConstraint(
            "state IN ('PROPOSED', 'EXECUTED', 'FAILED', 'SUPERSEDED')",
            name="ck_case_decision_proposal_state",
        ),
    )
    for column in (
        "owner_user_id",
        "case_id",
        "flow_run_id",
        "node_run_id",
        "node_attempt_id",
        "source_task_id",
        "source_binding_id",
        "state",
    ):
        op.create_index(f"ix_case_decision_proposals_{column}", "case_decision_proposals", [column])
    predicate = (
        "current_setting('flowweave.bypass', true) = 'on' OR "
        "owner_user_id = current_setting('flowweave.user_id', true)"
    )
    op.execute(sa.text('ALTER TABLE "case_decision_proposals" ENABLE ROW LEVEL SECURITY'))
    op.execute(sa.text('ALTER TABLE "case_decision_proposals" FORCE ROW LEVEL SECURITY'))
    op.execute(
        sa.text(
            'CREATE POLICY "pl_tenant_case_decision_proposals" ON "case_decision_proposals" '
            f"USING ({predicate}) WITH CHECK ({predicate})"
        )
    )
    for column in ("state", "reason", "payload_json", "result_json"):
        op.alter_column("case_decision_proposals", column, server_default=None)


def downgrade() -> None:
    op.execute(
        sa.text(
            'DROP POLICY IF EXISTS "pl_tenant_case_decision_proposals" ON "case_decision_proposals"'
        )
    )
    op.drop_table("case_decision_proposals")
