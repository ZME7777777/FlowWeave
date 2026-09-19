"""add path-scoped website credentials.

Revision ID: 0118_credential_target_path
Revises: 0117_agent_conversation_search
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0118_credential_target_path"
down_revision = "0117_agent_conversation_search"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "website_credentials",
        sa.Column("target_path", sa.String(length=2048), nullable=False, server_default="/"),
    )
    op.drop_constraint(
        "uq_website_credential_owner_host_name",
        "website_credentials",
        type_="unique",
    )
    op.create_unique_constraint(
        "uq_website_credential_owner_host_path_name",
        "website_credentials",
        ["owner_user_id", "target_host", "target_path", "name"],
    )
    op.alter_column("website_credentials", "target_path", server_default=None)


def downgrade() -> None:
    op.drop_constraint(
        "uq_website_credential_owner_host_path_name",
        "website_credentials",
        type_="unique",
    )
    op.create_unique_constraint(
        "uq_website_credential_owner_host_name",
        "website_credentials",
        ["owner_user_id", "target_host", "name"],
    )
    op.drop_column("website_credentials", "target_path")
