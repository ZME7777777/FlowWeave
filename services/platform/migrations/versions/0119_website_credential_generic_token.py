"""rename website credential bearer tokens to generic tokens.

Revision ID: 0119_website_credential_generic_token
Revises: 0118_credential_target_path
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0119_website_credential_generic_token"
down_revision = "0118_credential_target_path"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.drop_constraint(
        "ck_website_credential_auth_type",
        "website_credentials",
        type_="check",
    )
    op.execute(
        sa.text(
            "UPDATE website_credentials SET auth_type = 'TOKEN' WHERE auth_type = 'BEARER_TOKEN'"
        )
    )
    op.create_check_constraint(
        "ck_website_credential_auth_type",
        "website_credentials",
        "auth_type IN ('USERNAME_PASSWORD', 'TOKEN')",
    )


def downgrade() -> None:
    op.drop_constraint(
        "ck_website_credential_auth_type",
        "website_credentials",
        type_="check",
    )
    op.execute(
        sa.text(
            "UPDATE website_credentials SET auth_type = 'BEARER_TOKEN' WHERE auth_type = 'TOKEN'"
        )
    )
    op.create_check_constraint(
        "ck_website_credential_auth_type",
        "website_credentials",
        "auth_type IN ('USERNAME_PASSWORD', 'BEARER_TOKEN')",
    )
