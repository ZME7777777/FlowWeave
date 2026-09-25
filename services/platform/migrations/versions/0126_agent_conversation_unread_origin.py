"""Record whether a conversation unread state was set manually or by the system.

Revision ID: 0126_unread_origin
Revises: 0125_admin_alert_lifecycle
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0126_unread_origin"
down_revision = "0125_admin_alert_lifecycle"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "agent_conversation_bindings",
        sa.Column("unread_origin", sa.String(length=20), nullable=True),
    )
    op.create_check_constraint(
        "ck_agent_conversation_unread_origin",
        "agent_conversation_bindings",
        "unread_origin IS NULL OR unread_origin IN ('MANUAL', 'SYSTEM')",
    )


def downgrade() -> None:
    op.drop_constraint(
        "ck_agent_conversation_unread_origin",
        "agent_conversation_bindings",
        type_="check",
    )
    op.drop_column("agent_conversation_bindings", "unread_origin")
