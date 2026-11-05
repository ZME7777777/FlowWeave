"""Track explicit Agent schedule credential selections.

Revision ID: 0177_schedule_default_credentials_all
Revises: 0176_workspace_default_credentials_all
"""

import sqlalchemy as sa
from alembic import op

revision = "0177_schedule_default_credentials_all"
down_revision = "0176_workspace_default_credentials_all"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "agent_conversation_schedules",
        sa.Column(
            "credentials_configured",
            sa.Boolean(),
            nullable=False,
            server_default=sa.false(),
        ),
    )
    op.execute(
        "UPDATE agent_conversation_schedules SET credentials_configured = true "
        "WHERE json_array_length(credential_ids_json) > 0"
    )


def downgrade() -> None:
    op.drop_column("agent_conversation_schedules", "credentials_configured")
