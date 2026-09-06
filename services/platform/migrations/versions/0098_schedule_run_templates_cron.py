"""freeze schedule masters from ready FlowRun records and use cron

Revision ID: 0098_schedule_templates_cron
Revises: 0097_record_ws_path
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0098_schedule_templates_cron"
down_revision = "0097_record_ws_path"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "flow_run_schedules", sa.Column("source_flow_run_id", sa.String(length=36), nullable=True)
    )
    op.add_column(
        "flow_run_schedules", sa.Column("cron_expression", sa.String(length=120), nullable=True)
    )
    op.create_index(
        "ix_flow_run_schedules_source_flow_run_id", "flow_run_schedules", ["source_flow_run_id"]
    )
    op.create_check_constraint(
        "ck_schedule_cron_present",
        "flow_run_schedules",
        "cron_expression IS NULL OR length(cron_expression) > 0",
    )
    # Existing interval schedules have no auditable ready-record master.  Do
    # not guess one or let them run with their old partial configuration.
    op.execute("UPDATE flow_run_schedules SET status = 'PAUSED', next_run_at = NULL")


def downgrade() -> None:
    op.drop_constraint("ck_schedule_cron_present", "flow_run_schedules", type_="check")
    op.drop_index("ix_flow_run_schedules_source_flow_run_id", table_name="flow_run_schedules")
    op.drop_column("flow_run_schedules", "cron_expression")
    op.drop_column("flow_run_schedules", "source_flow_run_id")
