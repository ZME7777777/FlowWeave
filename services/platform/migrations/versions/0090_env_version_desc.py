"""add environment version description

Revision ID: 0090_env_version_desc
Revises: 0089_website_credentials
"""

import sqlalchemy as sa
from alembic import op

revision = "0090_env_version_desc"
down_revision = "0089_website_credentials"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "environment_versions",
        sa.Column("description", sa.Text(), nullable=False, server_default=""),
    )
    op.alter_column("environment_versions", "description", server_default=None)


def downgrade() -> None:
    op.drop_column("environment_versions", "description")
