"""persist final Agent reply previews for unread conversations.

Revision ID: 0155_agent_final_reply_preview
Revises: 0154_agent_schedule_templates
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0155_agent_final_reply_preview"
down_revision = "0154_agent_schedule_templates"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "agent_conversation_bindings",
        sa.Column("final_reply_preview_event_id", sa.String(length=200), nullable=True),
    )
    op.add_column(
        "agent_conversation_bindings",
        sa.Column("final_reply_preview_content", sa.Text(), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("agent_conversation_bindings", "final_reply_preview_content")
    op.drop_column("agent_conversation_bindings", "final_reply_preview_event_id")
