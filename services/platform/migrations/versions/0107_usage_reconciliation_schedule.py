"""schedule periodic OpenHands usage reconciliation

Revision ID: 0107_usage_reconciliation_schedule
Revises: 0105_conversation_token_usage
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0107_usage_reconciliation_schedule"
down_revision = "0105_conversation_token_usage"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "agent_conversation_bindings",
        sa.Column("usage_reconciled_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column(
        "agent_conversation_bindings",
        sa.Column("usage_reconcile_after", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_index(
        "ix_agent_conversation_bindings_usage_reconcile_after",
        "agent_conversation_bindings",
        ["usage_reconcile_after"],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_agent_conversation_bindings_usage_reconcile_after",
        table_name="agent_conversation_bindings",
    )
    op.drop_column("agent_conversation_bindings", "usage_reconcile_after")
    op.drop_column("agent_conversation_bindings", "usage_reconciled_at")
