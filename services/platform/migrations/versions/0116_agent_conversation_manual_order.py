"""Add workspace-local manual ordering for Agent conversations.

Revision ID: 0116_agent_manual_order
Revises: 0115_agent_annotations
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0116_agent_manual_order"
down_revision = "0115_agent_annotations"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "agent_conversation_bindings",
        sa.Column("manual_sort_rank", sa.Numeric(precision=30, scale=12), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("agent_conversation_bindings", "manual_sort_rank")
