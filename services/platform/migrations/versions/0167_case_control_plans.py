"""add platform-owned Case control plans.

Revision ID: 0167_case_control_plans
Revises: 0166_migrate_legacy_case_stage_runs
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0167_case_control_plans"
down_revision = "0166_migrate_legacy_case_stage_runs"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "case_control_plans",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("owner_user_id", sa.String(length=36), nullable=False),
        sa.Column("case_id", sa.String(length=36), nullable=False),
        sa.Column("stage_id", sa.String(length=36), nullable=False),
        sa.Column("source_binding_id", sa.String(length=36), nullable=True),
        sa.Column("execution_flow_run_id", sa.String(length=36), nullable=True),
        sa.Column("node_run_id", sa.String(length=36), nullable=True),
        sa.Column("node_attempt_id", sa.String(length=36), nullable=True),
        sa.Column("action", sa.String(length=40), nullable=False),
        sa.Column("state", sa.String(length=20), nullable=False, server_default="PENDING"),
        sa.Column("idempotency_key", sa.String(length=140), nullable=False),
        sa.Column("reason", sa.Text(), nullable=False, server_default=""),
        sa.Column("input_snapshot_json", sa.JSON(), nullable=False, server_default="{}"),
        sa.Column("result_json", sa.JSON(), nullable=False, server_default="{}"),
        sa.Column("error_code", sa.String(length=120), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("validated_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("executed_at", sa.DateTime(timezone=True), nullable=True),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("case_id", "idempotency_key", name="uq_case_control_plan_key"),
        sa.CheckConstraint(
            "action IN ('CREATE_EXECUTION_COPY', 'START_EXECUTION', 'START_STEPWISE_NODE', "
            "'STOP_EXECUTION', 'RERUN_STEPWISE_NODE', 'REVISE_UNSTARTED_RUNTIME')",
            name="ck_case_control_plan_action",
        ),
        sa.CheckConstraint(
            "state IN ('PENDING', 'VALIDATED', 'EXECUTING', 'SUCCEEDED', 'FAILED', 'CANCELLED')",
            name="ck_case_control_plan_state",
        ),
    )
    for column in (
        "owner_user_id",
        "case_id",
        "stage_id",
        "source_binding_id",
        "execution_flow_run_id",
        "node_run_id",
        "node_attempt_id",
        "state",
    ):
        op.create_index(f"ix_case_control_plans_{column}", "case_control_plans", [column])
    predicate = (
        "current_setting('flowweave.bypass', true) = 'on' OR "
        "owner_user_id = current_setting('flowweave.user_id', true)"
    )
    op.execute(sa.text('ALTER TABLE "case_control_plans" ENABLE ROW LEVEL SECURITY'))
    op.execute(sa.text('ALTER TABLE "case_control_plans" FORCE ROW LEVEL SECURITY'))
    op.execute(
        sa.text(
            'CREATE POLICY "pl_tenant_case_control_plans" ON "case_control_plans" '
            f"USING ({predicate}) WITH CHECK ({predicate})"
        )
    )
    for column in ("state", "reason", "input_snapshot_json", "result_json"):
        op.alter_column("case_control_plans", column, server_default=None)


def downgrade() -> None:
    op.execute(
        sa.text('DROP POLICY IF EXISTS "pl_tenant_case_control_plans" ON "case_control_plans"')
    )
    op.drop_table("case_control_plans")
