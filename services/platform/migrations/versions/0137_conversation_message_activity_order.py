"""Order conversations by last accepted message activity.

Revision ID: 0137_conversation_message_order
Revises: 0136_merge_activity_search
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0137_conversation_message_order"
down_revision = "0136_merge_activity_search"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "agent_conversation_bindings",
        sa.Column("last_message_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_index(
        "ix_agent_conversation_bindings_last_message_at",
        "agent_conversation_bindings",
        ["last_message_at"],
    )
    # Historical records do not retain a separate message timestamp. The last
    # native connection is the closest durable signal; creation is the safe
    # fallback for conversations that never sent a message. Existing manual
    # ranks were calculated against the old creation-time ordering and cannot
    # be reconciled safely with a recent-message time axis, so reset them.
    op.execute(
        "UPDATE agent_conversation_bindings "
        "SET last_message_at = COALESCE(last_connected_at, created_at), "
        "manual_sort_rank = NULL"
    )


def downgrade() -> None:
    op.drop_index(
        "ix_agent_conversation_bindings_last_message_at",
        table_name="agent_conversation_bindings",
    )
    op.drop_column("agent_conversation_bindings", "last_message_at")
