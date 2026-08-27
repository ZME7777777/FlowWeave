"""Persist the selected model for each Agent Conversation.

Revision ID: 0065_agent_model_selection
Revises: 0064_agent_streaming_callback
Create Date: 2026-08-27
"""

import sqlalchemy as sa
from alembic import op

revision = "0065_agent_model_selection"
down_revision = "0064_agent_streaming_callback"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Existing bindings may have switched models only inside a live OpenHands
    # Event Service. That value cannot be reconstructed safely during a schema
    # migration, so historical selections intentionally remain unknown.
    with op.batch_alter_table("agent_conversation_bindings") as batch:
        batch.add_column(sa.Column("model_name", sa.String(length=240), nullable=True))
        batch.add_column(sa.Column("reasoning_effort", sa.String(length=30), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table("agent_conversation_bindings") as batch:
        batch.drop_column("reasoning_effort")
        batch.drop_column("model_name")
