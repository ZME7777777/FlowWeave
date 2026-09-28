"""Index the ready BackgroundTask claim queue.

Revision ID: 0131_background_task_claim_index
Revises: 0134_merge_search_upload
"""

from __future__ import annotations

from alembic import op

revision = "0131_background_task_claim_index"
down_revision = "0134_merge_search_upload"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_index(
        "ix_background_tasks_claim_ready",
        "background_tasks",
        ["available_at", "created_at"],
        postgresql_where="state IN ('PENDING', 'RETRY')",
    )


def downgrade() -> None:
    op.drop_index("ix_background_tasks_claim_ready", table_name="background_tasks")
