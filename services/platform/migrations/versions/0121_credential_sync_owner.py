"""restore tenant ownership for credential synchronization records.

Revision ID: 0121_credential_sync_owner
Revises: 0120_agent_credential_sync
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0121_credential_sync_owner"
down_revision = "0120_agent_credential_sync"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "agent_conversation_credential_syncs",
        sa.Column("owner_user_id", sa.String(length=36), nullable=True),
    )
    op.execute(
        "UPDATE agent_conversation_credential_syncs AS credential_sync "
        "SET owner_user_id = binding.owner_user_id "
        "FROM agent_conversation_bindings AS binding "
        "WHERE credential_sync.binding_id = binding.id"
    )
    op.alter_column("agent_conversation_credential_syncs", "owner_user_id", nullable=False)
    op.create_index(
        "ix_agent_conversation_credential_syncs_owner_user_id",
        "agent_conversation_credential_syncs",
        ["owner_user_id"],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_agent_conversation_credential_syncs_owner_user_id",
        table_name="agent_conversation_credential_syncs",
    )
    op.drop_column("agent_conversation_credential_syncs", "owner_user_id")
