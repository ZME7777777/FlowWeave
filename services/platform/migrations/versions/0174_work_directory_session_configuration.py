"""Persist session defaults per named Agent workspace.

Revision ID: 0174_work_directory_session_configuration
Revises: 0173_agent_workspace_session_configuration
"""

import sqlalchemy as sa
from alembic import op

revision = "0174_work_directory_session_configuration"
down_revision = "0173_agent_workspace_session_configuration"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "agent_work_directories",
        sa.Column(
            "session_capability_ids_json",
            sa.JSON(),
            nullable=False,
            server_default=sa.text("'[]'"),
        ),
    )
    op.alter_column(
        "agent_work_directories",
        "session_capability_ids_json",
        server_default=None,
    )
    op.add_column(
        "agent_work_directories",
        sa.Column("session_credential_ids_json", sa.JSON(), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("agent_work_directories", "session_credential_ids_json")
    op.drop_column("agent_work_directories", "session_capability_ids_json")
