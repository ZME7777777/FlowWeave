"""add Codex OAuth model-provider credentials

Revision ID: 0021_codex_oauth_model_providers
Revises: 0020_agent_subagents
"""

import sqlalchemy as sa
from alembic import op

revision = "0021_codex_oauth_model_providers"
down_revision = "0020_agent_subagents"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Migration 0001 creates this table from live ORM metadata. Fresh installs
    # therefore already contain these fields, while existing installations do
    # not. Inspect first so both upgrade paths remain valid.
    existing = {item["name"] for item in sa.inspect(op.get_bind()).get_columns("model_providers")}
    columns = (
        sa.Column(
            "auth_type",
            sa.String(30),
            nullable=False,
            server_default="API_KEY",
        ),
        sa.Column("encrypted_oauth_access_token", sa.LargeBinary(), nullable=True),
        sa.Column("encrypted_oauth_refresh_token", sa.LargeBinary(), nullable=True),
        sa.Column(
            "oauth_access_expires_at",
            sa.DateTime(timezone=True),
            nullable=True,
        ),
        sa.Column("oauth_account_id", sa.String(240), nullable=True),
        sa.Column("oauth_email", sa.String(320), nullable=True),
        sa.Column("encrypted_oauth_device_auth_id", sa.LargeBinary(), nullable=True),
        sa.Column("oauth_user_code", sa.String(80), nullable=True),
        sa.Column(
            "oauth_device_expires_at",
            sa.DateTime(timezone=True),
            nullable=True,
        ),
        sa.Column("oauth_poll_interval", sa.Integer(), nullable=True),
    )
    for column in columns:
        if column.name not in existing:
            op.add_column("model_providers", column)


def downgrade() -> None:
    for column in (
        "oauth_poll_interval",
        "oauth_device_expires_at",
        "oauth_user_code",
        "encrypted_oauth_device_auth_id",
        "oauth_email",
        "oauth_account_id",
        "oauth_access_expires_at",
        "encrypted_oauth_refresh_token",
        "encrypted_oauth_access_token",
        "auth_type",
    ):
        op.drop_column("model_providers", column)
