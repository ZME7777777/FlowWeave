"""add durable native Agent conversation search requests.

Revision ID: 0117_agent_conversation_search
Revises: 0116_agent_manual_order
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0117_agent_conversation_search"
down_revision = "0116_agent_manual_order"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "agent_conversation_searches",
        sa.Column("id", sa.String(length=36), primary_key=True),
        sa.Column("owner_user_id", sa.String(length=36), nullable=False),
        sa.Column("workspace_id", sa.String(length=36), nullable=False),
        sa.Column("query", sa.String(length=500), nullable=False),
        sa.Column("state", sa.String(length=20), nullable=False, server_default="PENDING"),
        sa.Column("failure_summary", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint(
            "state IN ('PENDING', 'RUNNING', 'SUCCEEDED', 'FAILED')",
            name="ck_agent_conversation_search_state",
        ),
    )
    op.create_index(
        "ix_agent_conversation_searches_owner_user_id",
        "agent_conversation_searches",
        ["owner_user_id"],
    )
    op.create_index(
        "ix_agent_conversation_searches_workspace_id",
        "agent_conversation_searches",
        ["workspace_id"],
    )
    op.create_index(
        "ix_agent_conversation_searches_state", "agent_conversation_searches", ["state"]
    )
    op.create_table(
        "agent_conversation_search_hits",
        sa.Column("id", sa.String(length=36), primary_key=True),
        sa.Column("owner_user_id", sa.String(length=36), nullable=False),
        sa.Column("search_id", sa.String(length=36), nullable=False),
        sa.Column("binding_id", sa.String(length=36), nullable=False),
        sa.Column("event_id", sa.String(length=200), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint(
            "search_id", "binding_id", "event_id", name="uq_agent_conversation_search_hit"
        ),
    )
    op.create_index(
        "ix_agent_conversation_search_hits_owner_user_id",
        "agent_conversation_search_hits",
        ["owner_user_id"],
    )
    op.create_index(
        "ix_agent_conversation_search_hits_search_id",
        "agent_conversation_search_hits",
        ["search_id"],
    )
    op.create_index(
        "ix_agent_conversation_search_hits_binding_id",
        "agent_conversation_search_hits",
        ["binding_id"],
    )


def downgrade() -> None:
    op.drop_table("agent_conversation_search_hits")
    op.drop_table("agent_conversation_searches")
