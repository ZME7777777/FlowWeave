"""Persist workspace and conversation archive states."""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0179_agent_workspace_and_conversation_archives"
down_revision = "0178_merge_schedule_credentials"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "agent_work_directories",
        sa.Column("archived_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_index(
        "ix_agent_work_directories_archived_at", "agent_work_directories", ["archived_at"]
    )
    op.add_column(
        "agent_conversation_bindings",
        sa.Column("archived_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_index(
        "ix_agent_conversation_bindings_archived_at",
        "agent_conversation_bindings",
        ["archived_at"],
    )


def downgrade() -> None:
    op.drop_index("ix_agent_conversation_bindings_archived_at", "agent_conversation_bindings")
    op.drop_column("agent_conversation_bindings", "archived_at")
    op.drop_index("ix_agent_work_directories_archived_at", "agent_work_directories")
    op.drop_column("agent_work_directories", "archived_at")
