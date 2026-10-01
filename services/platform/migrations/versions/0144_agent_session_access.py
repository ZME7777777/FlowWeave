"""Gate direct Agent sessions by LDAP user or organization.

Revision ID: 0144_agent_session_access
Revises: 0143_user_model_providers
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0144_agent_session_access"
down_revision = "0143_user_model_providers"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "users",
        sa.Column(
            "agent_sessions_enabled",
            sa.Boolean(),
            nullable=False,
            server_default=sa.false(),
        ),
    )
    op.add_column(
        "users",
        sa.Column(
            "ldap_organization_ids",
            sa.JSON(),
            nullable=False,
            server_default=sa.text("'[]'"),
        ),
    )
    op.create_table(
        "ldap_agent_session_organization_grants",
        sa.Column("organization_id", sa.String(length=64), nullable=False),
        sa.Column("created_by_user_id", sa.String(length=36), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("organization_id"),
    )
    op.create_index(
        "ix_ldap_agent_session_organization_grants_created_by_user_id",
        "ldap_agent_session_organization_grants",
        ["created_by_user_id"],
    )
    op.alter_column("users", "agent_sessions_enabled", server_default=None)
    op.alter_column("users", "ldap_organization_ids", server_default=None)


def downgrade() -> None:
    op.drop_index(
        "ix_ldap_agent_session_organization_grants_created_by_user_id",
        table_name="ldap_agent_session_organization_grants",
    )
    op.drop_table("ldap_agent_session_organization_grants")
    op.drop_column("users", "ldap_organization_ids")
    op.drop_column("users", "agent_sessions_enabled")
