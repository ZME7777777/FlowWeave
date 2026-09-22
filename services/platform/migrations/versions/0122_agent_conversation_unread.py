"""persist per-user Agent conversation unread state.

Revision ID: 0122_agent_conversation_unread
Revises: 0121_credential_sync_owner
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0122_agent_conversation_unread"
down_revision = "0121_credential_sync_owner"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "agent_conversation_bindings",
        sa.Column("unread", sa.Boolean(), nullable=False, server_default=sa.false()),
    )


def downgrade() -> None:
    op.drop_column("agent_conversation_bindings", "unread")
