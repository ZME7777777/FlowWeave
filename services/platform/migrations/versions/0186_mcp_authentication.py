"""User-isolated optional MCP authentication references and native use audit."""

import sqlalchemy as sa
from alembic import op

revision = "0186_mcp_authentication"
down_revision = "0185_message_submission_cancel"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "mcp_authentication_bindings",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("owner_user_id", sa.String(36), nullable=False),
        sa.Column(
            "capability_version_id",
            sa.String(36),
            sa.ForeignKey("capability_versions.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("strategy", sa.String(20), nullable=False),
        sa.Column("credential_id", sa.String(36)),
        sa.Column("row_version", sa.Integer(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint(
            "owner_user_id", "capability_version_id", name="uq_mcp_authentication_owner_version"
        ),
        sa.CheckConstraint("strategy IN ('none', 'bearer')", name="ck_mcp_authentication_strategy"),
        sa.CheckConstraint("row_version > 0", name="ck_mcp_authentication_version"),
    )
    op.create_table(
        "mcp_authentication_uses",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("owner_user_id", sa.String(36), nullable=False),
        sa.Column("consumer_id", sa.String(100), nullable=False),
        sa.Column(
            "capability_version_id",
            sa.String(36),
            sa.ForeignKey("capability_versions.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("reference_json", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint(
            "owner_user_id",
            "consumer_id",
            "capability_version_id",
            name="uq_mcp_authentication_use",
        ),
    )
    for table in ("mcp_authentication_bindings", "mcp_authentication_uses"):
        for column in ("owner_user_id", "capability_version_id"):
            op.create_index(f"ix_{table}_{column}", table, [column])
        predicate = (
            "current_setting('flowweave.bypass', true) = 'on' OR "
            "owner_user_id = current_setting('flowweave.user_id', true)"
        )
        op.execute(f'ALTER TABLE "{table}" ENABLE ROW LEVEL SECURITY')
        op.execute(f'ALTER TABLE "{table}" FORCE ROW LEVEL SECURITY')
        op.execute(
            f'CREATE POLICY "pl_tenant_{table}" ON "{table}" '
            f"USING ({predicate}) WITH CHECK ({predicate})"
        )
    op.create_index(
        "ix_mcp_authentication_uses_consumer_id", "mcp_authentication_uses", ["consumer_id"]
    )


def downgrade() -> None:
    op.drop_table("mcp_authentication_uses")
    op.drop_table("mcp_authentication_bindings")
