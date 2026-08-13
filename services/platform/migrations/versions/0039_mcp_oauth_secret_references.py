"""govern MCP OAuth state as encrypted Secret References

Revision ID: 0039_mcp_oauth_secret_references
Revises: 0038_mcp_target_validations
"""

import sqlalchemy as sa
from alembic import op

revision = "0039_mcp_oauth_secret_references"
down_revision = "0038_mcp_target_validations"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "mcp_oauth_secret_references",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column(
            "capability_version_id",
            sa.String(36),
            sa.ForeignKey("capability_versions.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column(
            "environment_version_id",
            sa.String(36),
            sa.ForeignKey("environment_versions.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column("state", sa.String(20), nullable=False),
        sa.Column("state_version", sa.Integer(), nullable=False),
        sa.Column("encrypted_oauth_state", sa.LargeBinary(), nullable=True),
        sa.Column("oauth_state_digest", sa.String(64), nullable=True),
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "state IN ('ACTIVE', 'REVOKED')", name="ck_mcp_oauth_secret_reference_state"
        ),
        sa.CheckConstraint(
            "state_version > 0", name="ck_mcp_oauth_secret_reference_version_positive"
        ),
        sa.UniqueConstraint(
            "capability_version_id",
            "environment_version_id",
            name="uq_mcp_oauth_secret_reference_target",
        ),
    )
    for column in ("capability_version_id", "environment_version_id", "state"):
        op.create_index(
            f"ix_mcp_oauth_secret_references_{column}",
            "mcp_oauth_secret_references",
            [column],
        )
    op.create_table(
        "mcp_oauth_secret_audits",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column(
            "secret_reference_id",
            sa.String(36),
            sa.ForeignKey("mcp_oauth_secret_references.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column(
            "validation_id",
            sa.String(36),
            sa.ForeignKey("capability_validations.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("action", sa.String(20), nullable=False),
        sa.Column("state_version", sa.Integer(), nullable=False),
        sa.Column("oauth_state_digest", sa.String(64), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "action IN ('CREATED', 'REFRESHED', 'REVOKED')",
            name="ck_mcp_oauth_secret_audit_action",
        ),
        sa.CheckConstraint("state_version > 0", name="ck_mcp_oauth_secret_audit_version_positive"),
    )
    op.create_index(
        "ix_mcp_oauth_secret_audits_secret_reference_id",
        "mcp_oauth_secret_audits",
        ["secret_reference_id"],
    )
    op.create_index(
        "ix_mcp_oauth_secret_audits_validation_id",
        "mcp_oauth_secret_audits",
        ["validation_id"],
    )


def downgrade() -> None:
    op.drop_table("mcp_oauth_secret_audits")
    op.drop_table("mcp_oauth_secret_references")
