"""Add one-shot Agent Conversation title metadata.

Revision ID: 0068_agent_title_metadata
Revises: 0067_agent_bootstrap
Create Date: 2026-08-27
"""

import sqlalchemy as sa
from alembic import op

revision = "0068_agent_title_metadata"
down_revision = "0067_agent_bootstrap"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "agent_conversation_bindings",
        sa.Column("title_state", sa.String(20), nullable=False, server_default="FALLBACK"),
    )
    op.add_column(
        "agent_conversation_bindings",
        sa.Column("title_generation", sa.Integer(), nullable=False, server_default="1"),
    )
    op.create_check_constraint(
        "ck_agent_conversation_title_state",
        "agent_conversation_bindings",
        "title_state IN ('PENDING', 'GENERATED', 'MANUAL', 'FALLBACK')",
    )
    op.create_check_constraint(
        "ck_agent_conversation_title_generation",
        "agent_conversation_bindings",
        "title_generation >= 1",
    )
    op.alter_column("agent_conversation_bindings", "title_state", server_default=None)
    op.alter_column("agent_conversation_bindings", "title_generation", server_default=None)


def downgrade() -> None:
    op.drop_constraint(
        "ck_agent_conversation_title_generation", "agent_conversation_bindings", type_="check"
    )
    op.drop_constraint(
        "ck_agent_conversation_title_state", "agent_conversation_bindings", type_="check"
    )
    op.drop_column("agent_conversation_bindings", "title_generation")
    op.drop_column("agent_conversation_bindings", "title_state")
