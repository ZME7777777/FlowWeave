"""add default session configuration to Agent schedules.

Revision ID: 0156_agent_schedule_session_configuration
Revises: 0155_agent_final_reply_preview
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0156_agent_schedule_session_configuration"
down_revision = "0155_agent_final_reply_preview"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "agent_conversation_schedules",
        sa.Column("capability_version_ids_json", sa.JSON(), nullable=False, server_default="[]"),
    )
    op.add_column(
        "agent_conversation_schedules",
        sa.Column("credential_ids_json", sa.JSON(), nullable=False, server_default="[]"),
    )
    op.alter_column(
        "agent_conversation_schedules", "capability_version_ids_json", server_default=None
    )
    op.alter_column("agent_conversation_schedules", "credential_ids_json", server_default=None)


def downgrade() -> None:
    op.drop_column("agent_conversation_schedules", "credential_ids_json")
    op.drop_column("agent_conversation_schedules", "capability_version_ids_json")
