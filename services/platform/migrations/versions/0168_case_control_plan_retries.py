"""link Case control plan retries.

Revision ID: 0168_case_control_plan_retries
Revises: 0167_case_control_plans
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0168_case_control_plan_retries"
down_revision = "0167_case_control_plans"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "case_control_plans",
        sa.Column("retry_of_plan_id", sa.String(length=36), nullable=True),
    )
    op.create_index(
        "ix_case_control_plans_retry_of_plan_id",
        "case_control_plans",
        ["retry_of_plan_id"],
    )


def downgrade() -> None:
    op.drop_index("ix_case_control_plans_retry_of_plan_id", table_name="case_control_plans")
    op.drop_column("case_control_plans", "retry_of_plan_id")
