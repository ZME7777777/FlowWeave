"""Persist Agent conversation sidebar pinning.

Revision ID: 0141_agent_conversation_pinned
Revises: 0140_agent_terminal_reconciliation
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0141_agent_conversation_pinned"
down_revision = "0140_agent_terminal_reconciliation"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "agent_conversation_bindings",
        sa.Column("pinned", sa.Boolean(), nullable=False, server_default=sa.false()),
    )
    op.alter_column("agent_conversation_bindings", "pinned", server_default=None)


def downgrade() -> None:
    op.drop_column("agent_conversation_bindings", "pinned")
