"""record explicit credential synchronization for Agent Conversations.

Revision ID: 0120_agent_conversation_credential_sync
Revises: 0119_website_credential_token
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0120_agent_conversation_credential_sync"
down_revision = "0119_website_credential_token"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "agent_conversation_bindings",
        sa.Column("credential_sync_initialized_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_table(
        "agent_conversation_credential_syncs",
        sa.Column("id", sa.String(length=36), primary_key=True),
        sa.Column("binding_id", sa.String(length=36), nullable=False),
        sa.Column("credential_id", sa.String(length=36), nullable=False),
        sa.Column("credential_row_version", sa.Integer(), nullable=False),
        sa.Column("synced_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "credential_row_version >= 1", name="ck_agent_conversation_credential_sync_version"
        ),
        sa.ForeignKeyConstraint(
            ["binding_id"], ["agent_conversation_bindings.id"], ondelete="RESTRICT"
        ),
        sa.UniqueConstraint(
            "binding_id", "credential_id", name="uq_agent_conversation_credential_sync"
        ),
    )
    op.create_index(
        "ix_agent_conversation_credential_syncs_binding_id",
        "agent_conversation_credential_syncs",
        ["binding_id"],
    )
    op.create_index(
        "ix_agent_conversation_credential_syncs_credential_id",
        "agent_conversation_credential_syncs",
        ["credential_id"],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_agent_conversation_credential_syncs_credential_id",
        table_name="agent_conversation_credential_syncs",
    )
    op.drop_index(
        "ix_agent_conversation_credential_syncs_binding_id",
        table_name="agent_conversation_credential_syncs",
    )
    op.drop_table("agent_conversation_credential_syncs")
    op.drop_column("agent_conversation_bindings", "credential_sync_initialized_at")
