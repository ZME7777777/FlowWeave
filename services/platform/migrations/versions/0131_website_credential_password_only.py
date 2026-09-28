"""add password-only website credentials.

Revision ID: 0131_website_pwd_auth
Revises: 0130_tmp_sidebar_convos
"""

from __future__ import annotations

from alembic import op

revision = "0131_website_pwd_auth"
down_revision = "0130_tmp_sidebar_convos"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.drop_constraint(
        "ck_website_credential_auth_type",
        "website_credentials",
        type_="check",
    )
    op.create_check_constraint(
        "ck_website_credential_auth_type",
        "website_credentials",
        "auth_type IN ('USERNAME_PASSWORD', 'PASSWORD', 'TOKEN')",
    )


def downgrade() -> None:
    op.drop_constraint(
        "ck_website_credential_auth_type",
        "website_credentials",
        type_="check",
    )
    op.create_check_constraint(
        "ck_website_credential_auth_type",
        "website_credentials",
        "auth_type IN ('USERNAME_PASSWORD', 'TOKEN')",
    )
