"""expand background task aggregate type.

Revision ID: 0157_background_task_aggregate_type_length
Revises: 0156_agent_schedule_session_configuration
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0157_background_task_aggregate_type_length"
down_revision = "0156_agent_schedule_session_configuration"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.alter_column(
        "background_tasks",
        "aggregate_type",
        existing_type=sa.String(length=30),
        type_=sa.String(length=64),
        existing_nullable=False,
    )


def downgrade() -> None:
    op.alter_column(
        "background_tasks",
        "aggregate_type",
        existing_type=sa.String(length=64),
        type_=sa.String(length=30),
        existing_nullable=False,
    )
