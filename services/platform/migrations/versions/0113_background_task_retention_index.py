"""Index bounded terminal background-task retention cleanup.

Revision ID: 0113_task_retention
Revises: 0112_agent_fallback
"""

from __future__ import annotations

from alembic import op

revision = "0113_task_retention"
down_revision = "0112_agent_fallback"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_index(
        "ix_background_tasks_terminal_updated_at",
        "background_tasks",
        ["state", "updated_at"],
    )


def downgrade() -> None:
    op.drop_index("ix_background_tasks_terminal_updated_at", table_name="background_tasks")
