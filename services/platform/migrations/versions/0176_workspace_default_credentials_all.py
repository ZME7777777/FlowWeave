"""Track whether workspace credential defaults were explicitly configured.

Revision ID: 0176_workspace_default_credentials_all
Revises: 0175_merge_workspace_session_defaults
"""

import sqlalchemy as sa
from alembic import op

revision = "0176_workspace_default_credentials_all"
down_revision = "0175_merge_workspace_session_defaults"
branch_labels = None
depends_on = None


def upgrade() -> None:
    table = "agent_workspace_session_configurations"
    op.add_column(
        table,
        sa.Column(
            "credentials_configured",
            sa.Boolean(),
            nullable=False,
            server_default=sa.false(),
        ),
    )
    op.execute(
        f"UPDATE {table} SET credentials_configured = true "
        "WHERE json_array_length(credential_ids_json) > 0"
    )
    op.execute(
        "UPDATE agent_work_directories SET session_credential_ids_json = NULL "
        "WHERE json_array_length(session_credential_ids_json) = 0"
    )


def downgrade() -> None:
    op.drop_column("agent_workspace_session_configurations", "credentials_configured")
