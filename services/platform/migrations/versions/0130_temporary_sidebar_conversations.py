"""Add temporary linked sidebar conversations.

Revision ID: 0130_temporary_sidebar_conversations
Revises: 0129_runtime_business_obs
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0130_temporary_sidebar_conversations"
down_revision = "0129_runtime_business_obs"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "agent_sidebar_conversations",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("workspace_id", sa.String(length=36), nullable=False),
        sa.Column("owner_user_id", sa.String(length=36), nullable=False),
        sa.Column("source_binding_id", sa.String(length=36), nullable=False),
        sa.Column("sidebar_binding_id", sa.String(length=36), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("expired_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("sidebar_binding_id", name="uq_agent_sidebar_binding"),
    )
    op.create_index("ix_agent_sidebar_workspace", "agent_sidebar_conversations", ["workspace_id"])
    op.create_index("ix_agent_sidebar_source", "agent_sidebar_conversations", ["source_binding_id"])
    op.create_index("ix_agent_sidebar_expiry", "agent_sidebar_conversations", ["expires_at"])


def downgrade() -> None:
    op.drop_table("agent_sidebar_conversations")
