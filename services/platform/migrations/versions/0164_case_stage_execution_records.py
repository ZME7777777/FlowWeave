"""separate Case stage templates from execution copies.

Revision ID: 0164_case_stage_execution_records
Revises: 0163_case_stage_flow_runs
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0164_case_stage_execution_records"
down_revision = "0163_case_stage_flow_runs"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "case_stages",
        sa.Column("current_execution_flow_run_id", sa.String(length=36), nullable=True),
    )
    op.create_index(
        "ix_case_stages_current_execution_flow_run_id",
        "case_stages",
        ["current_execution_flow_run_id"],
        unique=True,
    )


def downgrade() -> None:
    op.drop_index("ix_case_stages_current_execution_flow_run_id", table_name="case_stages")
    op.drop_column("case_stages", "current_execution_flow_run_id")
