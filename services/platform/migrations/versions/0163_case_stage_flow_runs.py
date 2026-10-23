"""bind one FlowRun to each Case stage.

Revision ID: 0163_case_stage_flow_runs
Revises: 0162_case_decision_proposals
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0163_case_stage_flow_runs"
down_revision = "0162_case_decision_proposals"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("case_stages", sa.Column("flow_run_id", sa.String(length=36), nullable=True))
    op.add_column(
        "case_stages",
        sa.Column(
            "execution_mode", sa.String(length=20), nullable=False, server_default="STEPWISE"
        ),
    )
    op.create_index("ix_case_stages_flow_run_id", "case_stages", ["flow_run_id"], unique=True)
    op.create_check_constraint(
        "ck_case_stage_execution_mode",
        "case_stages",
        "execution_mode IN ('STEPWISE', 'CONTINUOUS')",
    )
    op.alter_column("case_stages", "execution_mode", server_default=None)


def downgrade() -> None:
    op.drop_constraint("ck_case_stage_execution_mode", "case_stages", type_="check")
    op.drop_index("ix_case_stages_flow_run_id", table_name="case_stages")
    op.drop_column("case_stages", "execution_mode")
    op.drop_column("case_stages", "flow_run_id")
