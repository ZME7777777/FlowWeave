"""project formal asynchronous MCP OAuth authorization jobs

Revision ID: 0040_mcp_oauth_authorizations
Revises: 0039_mcp_oauth_secret_references
"""

import sqlalchemy as sa
from alembic import op

revision = "0040_mcp_oauth_authorizations"
down_revision = "0039_mcp_oauth_secret_references"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "mcp_oauth_authorizations",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column(
            "secret_reference_id",
            sa.String(36),
            sa.ForeignKey("mcp_oauth_secret_references.id", ondelete="RESTRICT"),
            nullable=False,
        ),
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
        sa.Column(
            "sandbox_id",
            sa.String(36),
            sa.ForeignKey("managed_sandboxes.id", ondelete="RESTRICT"),
            nullable=True,
        ),
        sa.Column("state", sa.String(20), nullable=False),
        sa.Column("state_version", sa.Integer(), nullable=False),
        sa.Column("expected_secret_version", sa.Integer(), nullable=False),
        sa.Column("persisted_secret_version", sa.Integer(), nullable=True),
        sa.Column("runtime_job_id", sa.String(100), nullable=True),
        sa.Column("runtime_resource_name", sa.String(100), nullable=True),
        sa.Column("runtime_base_url", sa.Text(), nullable=True),
        sa.Column("encrypted_authorization_url", sa.LargeBinary(), nullable=True),
        sa.Column("callback_ready", sa.Boolean(), nullable=False),
        sa.Column("tool_catalog_json", sa.JSON(), nullable=False),
        sa.Column("error_code", sa.String(100), nullable=True),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "state IN ('PENDING', 'AUTHORIZING', 'SUCCEEDED', 'FAILED', 'EXPIRED')",
            name="ck_mcp_oauth_authorization_state",
        ),
        sa.CheckConstraint("state_version > 0", name="ck_mcp_oauth_authorization_version_positive"),
        sa.CheckConstraint(
            "expected_secret_version > 0",
            name="ck_mcp_oauth_authorization_secret_version_positive",
        ),
    )
    for column in (
        "secret_reference_id",
        "capability_version_id",
        "environment_version_id",
        "sandbox_id",
        "state",
        "expires_at",
    ):
        op.create_index(
            f"ix_mcp_oauth_authorizations_{column}",
            "mcp_oauth_authorizations",
            [column],
        )
    op.drop_constraint(
        "ck_mcp_oauth_secret_audit_action",
        "mcp_oauth_secret_audits",
        type_="check",
    )
    op.create_check_constraint(
        "ck_mcp_oauth_secret_audit_action",
        "mcp_oauth_secret_audits",
        "action IN ('CREATED', 'AUTHORIZED', 'REFRESHED', 'REVOKED')",
    )
    op.add_column(
        "mcp_oauth_secret_audits",
        sa.Column("authorization_id", sa.String(36), nullable=True),
    )
    op.create_foreign_key(
        "fk_mcp_oauth_audit_authorization",
        "mcp_oauth_secret_audits",
        "mcp_oauth_authorizations",
        ["authorization_id"],
        ["id"],
        ondelete="SET NULL",
    )
    op.create_index(
        "ix_mcp_oauth_secret_audits_authorization_id",
        "mcp_oauth_secret_audits",
        ["authorization_id"],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_mcp_oauth_secret_audits_authorization_id",
        table_name="mcp_oauth_secret_audits",
    )
    op.drop_constraint(
        "fk_mcp_oauth_audit_authorization",
        "mcp_oauth_secret_audits",
        type_="foreignkey",
    )
    op.drop_column("mcp_oauth_secret_audits", "authorization_id")
    op.drop_constraint(
        "ck_mcp_oauth_secret_audit_action",
        "mcp_oauth_secret_audits",
        type_="check",
    )
    op.create_check_constraint(
        "ck_mcp_oauth_secret_audit_action",
        "mcp_oauth_secret_audits",
        "action IN ('CREATED', 'REFRESHED', 'REVOKED')",
    )
    op.drop_table("mcp_oauth_authorizations")
