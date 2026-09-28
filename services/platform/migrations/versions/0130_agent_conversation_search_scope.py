"""Persist Agent conversation search work-directory scopes.

Revision ID: 0130_agent_conv_search_scope
Revises: 0129_runtime_business_obs
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0130_agent_conv_search_scope"
down_revision = "0129_runtime_business_obs"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "agent_conversation_searches",
        sa.Column("work_directory_ids", sa.JSON(), nullable=True),
    )
    op.add_column(
        "agent_conversation_searches",
        sa.Column("include_root", sa.Boolean(), nullable=False, server_default=sa.true()),
    )
    op.alter_column("agent_conversation_searches", "include_root", server_default=None)


def downgrade() -> None:
    op.drop_column("agent_conversation_searches", "include_root")
    op.drop_column("agent_conversation_searches", "work_directory_ids")
