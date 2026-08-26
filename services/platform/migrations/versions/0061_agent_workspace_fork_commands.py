"""Allow auditable native forks from Agent Workspace conversations.

Revision ID: 0061_agent_workspace_fork
Revises: 0060_agent_conversations
Create Date: 2026-08-26
"""

from alembic import op

revision = "0061_agent_workspace_fork"
down_revision = "0060_agent_conversations"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("agent_conversation_commands") as batch:
        batch.drop_constraint("ck_agent_command_type", type_="check")
        batch.create_check_constraint(
            "ck_agent_command_type",
            "command_type IN ('CREATE', 'DELETE', 'RENAME', 'FORK')",
        )


def downgrade() -> None:
    with op.batch_alter_table("agent_conversation_commands") as batch:
        batch.drop_constraint("ck_agent_command_type", type_="check")
        batch.create_check_constraint(
            "ck_agent_command_type",
            "command_type IN ('CREATE', 'DELETE', 'RENAME')",
        )
