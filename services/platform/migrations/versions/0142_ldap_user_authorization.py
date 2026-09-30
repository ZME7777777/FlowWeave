"""Add LDAP identities to the local user authorization projection.

Revision ID: 0142_ldap_user_authorization
Revises: 0141_agent_conversation_pinned
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0142_ldap_user_authorization"
down_revision = "0141_agent_conversation_pinned"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "users",
        sa.Column("auth_source", sa.String(length=20), nullable=False, server_default="LOCAL"),
    )
    op.add_column("users", sa.Column("external_subject", sa.String(length=200), nullable=True))
    op.add_column("users", sa.Column("display_name", sa.String(length=200), nullable=True))
    op.add_column("users", sa.Column("email", sa.String(length=320), nullable=True))
    op.create_check_constraint("ck_user_auth_source", "users", "auth_source IN ('LOCAL', 'LDAP')")
    op.create_index("ix_users_auth_source", "users", ["auth_source"])
    op.create_unique_constraint("uq_users_external_subject", "users", ["external_subject"])
    op.alter_column("users", "auth_source", server_default=None)


def downgrade() -> None:
    op.drop_constraint("uq_users_external_subject", "users", type_="unique")
    op.drop_index("ix_users_auth_source", table_name="users")
    op.drop_constraint("ck_user_auth_source", "users", type_="check")
    op.drop_column("users", "email")
    op.drop_column("users", "display_name")
    op.drop_column("users", "external_subject")
    op.drop_column("users", "auth_source")
