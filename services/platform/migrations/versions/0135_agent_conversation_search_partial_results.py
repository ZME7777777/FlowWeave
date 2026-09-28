"""Persist partial newest-first Agent conversation search results.

Revision ID: 0135_agent_search_partial
Revises: 0131_background_task_claim_index
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0135_agent_search_partial"
down_revision = "0131_background_task_claim_index"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "agent_conversation_searches",
        sa.Column("is_partial", sa.Boolean(), nullable=False, server_default=sa.false()),
    )
    op.add_column("agent_conversation_searches", sa.Column("partial_summary", sa.Text()))
    op.alter_column("agent_conversation_searches", "is_partial", server_default=None)
    op.add_column(
        "agent_conversation_search_hits",
        sa.Column("occurred_at", sa.DateTime(timezone=True)),
    )
    op.create_index(
        "ix_agent_conversation_search_hits_occurred_at",
        "agent_conversation_search_hits",
        ["occurred_at"],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_agent_conversation_search_hits_occurred_at", table_name="agent_conversation_search_hits"
    )
    op.drop_column("agent_conversation_search_hits", "occurred_at")
    op.drop_column("agent_conversation_searches", "partial_summary")
    op.drop_column("agent_conversation_searches", "is_partial")
