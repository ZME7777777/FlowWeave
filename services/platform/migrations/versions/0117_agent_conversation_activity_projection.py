"""Persist Agent conversation activity transitions.

Revision ID: 0117_agent_activity_projection
Revises: 0116_agent_manual_order
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0117_agent_activity_projection"
down_revision = "0116_agent_manual_order"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "agent_conversation_bindings",
        sa.Column("activity_was_running", sa.Boolean(), nullable=False, server_default=sa.false()),
    )
    op.alter_column("agent_conversation_bindings", "activity_was_running", server_default=None)


def downgrade() -> None:
    op.drop_column("agent_conversation_bindings", "activity_was_running")
