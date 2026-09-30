"""Persist pending Agent conversation terminal reconciliation.

Revision ID: 0140_agent_terminal_reconciliation
Revises: 0139_user_credentials_runs
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0140_agent_terminal_reconciliation"
down_revision = "0139_user_credentials_runs"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "agent_conversation_bindings",
        sa.Column(
            "terminal_reconciliation_pending",
            sa.Boolean(),
            nullable=False,
            server_default=sa.false(),
        ),
    )
    op.add_column(
        "agent_conversation_bindings",
        sa.Column("last_notified_completion_event_id", sa.String(length=200), nullable=True),
    )
    op.alter_column(
        "agent_conversation_bindings",
        "terminal_reconciliation_pending",
        server_default=None,
    )


def downgrade() -> None:
    op.drop_column("agent_conversation_bindings", "last_notified_completion_event_id")
    op.drop_column("agent_conversation_bindings", "terminal_reconciliation_pending")
