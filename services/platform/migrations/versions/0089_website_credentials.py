"""Add encrypted host-scoped website credentials.

Revision ID: 0089_website_credentials
Revises: 0088_physical_delete_no_fks
Create Date: 2026-09-03
"""

import sqlalchemy as sa
from alembic import op

revision = "0089_website_credentials"
down_revision = "0088_physical_delete_no_fks"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "website_credentials",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("name", sa.String(200), nullable=False),
        sa.Column("target_host", sa.String(253), nullable=False),
        sa.Column(
            "include_subdomains", sa.Boolean(), nullable=False, server_default=sa.false()
        ),
        sa.Column("auth_type", sa.String(30), nullable=False),
        sa.Column("encrypted_username", sa.LargeBinary(), nullable=True),
        sa.Column("encrypted_secret", sa.LargeBinary(), nullable=False),
        sa.Column("secret_hint", sa.String(20), nullable=True),
        sa.Column("row_version", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint(
            "target_host", "name", name="uq_website_credential_host_name"
        ),
        sa.CheckConstraint(
            "auth_type IN ('USERNAME_PASSWORD', 'BEARER_TOKEN')",
            name="ck_website_credential_auth_type",
        ),
        sa.CheckConstraint("row_version >= 1", name="ck_website_credential_row_version"),
    )
    op.create_index("ix_website_credentials_target_host", "website_credentials", ["target_host"])


def downgrade() -> None:
    op.drop_index("ix_website_credentials_target_host", table_name="website_credentials")
    op.drop_table("website_credentials")
