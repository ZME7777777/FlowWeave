"""Persist direct Agent workspace session defaults.

Revision ID: 0173_agent_workspace_session_configuration
Revises: 0172_case_configuration
"""

import sqlalchemy as sa
from alembic import op

revision = "0173_agent_workspace_session_configuration"
down_revision = "0172_case_configuration"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "agent_workspaces",
        sa.Column("session_credential_ids_json", sa.JSON(), nullable=True),
    )
    op.drop_constraint(
        "ck_agent_workspace_capability_type",
        "agent_workspace_capabilities",
        type_="check",
    )
    op.create_check_constraint(
        "ck_agent_workspace_capability_type",
        "agent_workspace_capabilities",
        "capability_type IN ('SKILL', 'MCP', 'PLUGIN', 'CONTEXT', 'AGENT_DEFINITION', 'HOOK')",
    )


def downgrade() -> None:
    op.drop_constraint(
        "ck_agent_workspace_capability_type",
        "agent_workspace_capabilities",
        type_="check",
    )
    op.create_check_constraint(
        "ck_agent_workspace_capability_type",
        "agent_workspace_capabilities",
        "capability_type IN ('SKILL', 'MCP', 'PLUGIN')",
    )
    op.drop_column("agent_workspaces", "session_credential_ids_json")
