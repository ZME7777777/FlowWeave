"""Track native credential metadata delivery separately from secret values.

Revision ID: 0159_credential_context_version
Revises: 0158_schedule_china_timezone
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0159_credential_context_version"
down_revision = "0158_schedule_china_timezone"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "agent_conversation_credential_syncs",
        sa.Column("context_version", sa.Integer(), nullable=False, server_default="0"),
    )


def downgrade() -> None:
    op.drop_column("agent_conversation_credential_syncs", "context_version")
