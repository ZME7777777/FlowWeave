"""add parent-owned subagent conversations

Revision ID: 0020_agent_subagents
Revises: 0019_hard_delete_legacy_flows
"""

import sqlalchemy as sa
from alembic import op

revision = "0020_agent_subagents"
down_revision = "0019_hard_delete_legacy_flows"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "agent_conversations",
        sa.Column("parent_conversation_id", sa.String(length=36), nullable=True),
    )
    op.add_column(
        "agent_conversations",
        sa.Column("delegation_batch_key", sa.String(length=100), nullable=True),
    )
    op.add_column(
        "agent_conversations",
        sa.Column("delegation_instruction", sa.Text(), nullable=True),
    )
    op.create_foreign_key(
        "fk_agent_conversation_parent",
        "agent_conversations",
        "agent_conversations",
        ["parent_conversation_id"],
        ["id"],
        ondelete="CASCADE",
    )
    op.create_index(
        "ix_agent_conversations_parent_conversation_id",
        "agent_conversations",
        ["parent_conversation_id"],
    )
    op.create_index(
        "ix_agent_conversations_delegation_batch_key",
        "agent_conversations",
        ["delegation_batch_key"],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_agent_conversations_delegation_batch_key", table_name="agent_conversations"
    )
    op.drop_index(
        "ix_agent_conversations_parent_conversation_id", table_name="agent_conversations"
    )
    op.drop_constraint(
        "fk_agent_conversation_parent", "agent_conversations", type_="foreignkey"
    )
    op.drop_column("agent_conversations", "delegation_instruction")
    op.drop_column("agent_conversations", "delegation_batch_key")
    op.drop_column("agent_conversations", "parent_conversation_id")
