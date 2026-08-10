"""remove platform-owned OAuth credentials and runtime leases

Revision ID: 0018_remove_platform_credentials
Revises: 0017_managed_sandboxes

The upgrade intentionally destroys encrypted tokens, OAuth verifier material,
and lease digests.  A downgrade can only restore empty compatibility tables;
destroyed credential data is never reconstructed.
"""

import sqlalchemy as sa
from alembic import op

revision = "0018_remove_platform_credentials"
down_revision = "0017_managed_sandboxes"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.drop_table("credential_leases")
    op.drop_table("credential_connections")
    op.drop_table("oauth_sessions")


def downgrade() -> None:
    # Compatibility only: credentials removed by upgrade cannot be recovered.
    op.create_table(
        "oauth_sessions",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("provider", sa.String(length=30), nullable=False),
        sa.Column("subject_key", sa.String(length=160), nullable=False),
        sa.Column("state_digest", sa.String(length=64), nullable=False),
        sa.Column("encrypted_code_verifier", sa.LargeBinary(), nullable=False),
        sa.Column("scopes_json", sa.JSON(), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("consumed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("state_digest"),
    )
    for column in ("provider", "subject_key", "state_digest", "expires_at"):
        op.create_index(f"ix_oauth_sessions_{column}", "oauth_sessions", [column])

    op.create_table(
        "credential_connections",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("provider", sa.String(length=30), nullable=False),
        sa.Column("subject_key", sa.String(length=160), nullable=False),
        sa.Column("provider_subject", sa.String(length=240), nullable=True),
        sa.Column("encrypted_access_token", sa.LargeBinary(), nullable=False),
        sa.Column("encrypted_refresh_token", sa.LargeBinary(), nullable=True),
        sa.Column("scopes_json", sa.JSON(), nullable=False),
        sa.Column("access_expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("state", sa.String(length=30), nullable=False),
        sa.Column("row_version", sa.Integer(), nullable=False),
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("provider", "subject_key", name="uq_credential_provider_subject"),
    )
    for column in ("provider", "subject_key", "state"):
        op.create_index(f"ix_credential_connections_{column}", "credential_connections", [column])

    op.create_table(
        "credential_leases",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("connection_id", sa.String(length=36), nullable=False),
        sa.Column("token_digest", sa.String(length=64), nullable=False),
        sa.Column("audience", sa.String(length=240), nullable=False),
        sa.Column("scopes_json", sa.JSON(), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("max_uses", sa.Integer(), nullable=False),
        sa.Column("use_count", sa.Integer(), nullable=False),
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_used_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["connection_id"], ["credential_connections.id"], ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("token_digest"),
    )
    for column in ("connection_id", "token_digest", "audience", "expires_at"):
        op.create_index(f"ix_credential_leases_{column}", "credential_leases", [column])
