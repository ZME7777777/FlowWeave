"""Freeze a model provider for each new Agent Workspace conversation.

Revision ID: 0062_agent_conversation_provider
Revises: 0061_agent_workspace_fork
Create Date: 2026-08-26
"""

import sqlalchemy as sa
from alembic import op

revision = "0062_agent_conversation_provider"
down_revision = "0061_agent_workspace_fork"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Existing bindings lack an auditable provider identity.  Keep the column
    # nullable rather than silently binding them to the workspace's current
    # default, which may differ from the provider OpenHands originally used.
    with op.batch_alter_table("agent_conversation_bindings") as batch:
        batch.add_column(sa.Column("model_provider_id", sa.String(length=36), nullable=True))
        batch.create_foreign_key(
            "fk_agent_conversation_model_provider",
            "model_providers",
            ["model_provider_id"],
            ["id"],
            ondelete="RESTRICT",
        )
        batch.create_index(
            "ix_agent_conversation_bindings_model_provider_id", ["model_provider_id"]
        )


def downgrade() -> None:
    with op.batch_alter_table("agent_conversation_bindings") as batch:
        batch.drop_index("ix_agent_conversation_bindings_model_provider_id")
        batch.drop_constraint("fk_agent_conversation_model_provider", type_="foreignkey")
        batch.drop_column("model_provider_id")
