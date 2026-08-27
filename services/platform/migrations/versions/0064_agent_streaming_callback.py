"""Track Agent Conversation streaming callback compatibility.

Revision ID: 0064_agent_streaming_callback
Revises: 0063_autonomous_defaults
Create Date: 2026-08-27
"""

import sqlalchemy as sa
from alembic import op

revision = "0064_agent_streaming_callback"
down_revision = "0063_autonomous_defaults"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Existing Event Services may have been created before FlowWeave forced
    # stream=True. OpenHands cannot report or retrofit their token callback, so
    # keep them fail-closed until they are migrated through a native fork.
    with op.batch_alter_table("agent_conversation_bindings") as batch:
        batch.add_column(
            sa.Column(
                "streaming_callback_ready",
                sa.Boolean(),
                nullable=False,
                server_default=sa.false(),
            )
        )
    with op.batch_alter_table("agent_conversation_bindings") as batch:
        batch.alter_column("streaming_callback_ready", server_default=sa.true())


def downgrade() -> None:
    with op.batch_alter_table("agent_conversation_bindings") as batch:
        batch.drop_column("streaming_callback_ready")
