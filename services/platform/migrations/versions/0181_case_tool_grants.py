"""Freeze native Case tool authority per coordination round."""

import sqlalchemy as sa
from alembic import op

revision = "0181_case_tool_grants"
down_revision = "0180_merge_archives_case_rounds"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "case_tool_grants",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("owner_user_id", sa.String(36), nullable=False),
        sa.Column(
            "round_id",
            sa.String(36),
            sa.ForeignKey("case_control_rounds.id", ondelete="CASCADE"),
            nullable=False,
            unique=True,
        ),
        sa.Column(
            "binding_id",
            sa.String(36),
            sa.ForeignKey("agent_conversation_bindings.id", ondelete="CASCADE"),
            nullable=False,
            unique=True,
        ),
        sa.Column("work_directory_version_id", sa.String(36), nullable=False),
        sa.Column("runtime_session_id", sa.String(36), nullable=False),
        sa.Column("native_conversation_id", sa.String(36), nullable=False),
        sa.Column("token_digest", sa.String(64), nullable=False),
        sa.Column("encrypted_token", sa.LargeBinary(), nullable=False),
        sa.Column("capability_version_id", sa.String(36), nullable=False),
        sa.Column("capability_digest", sa.String(64), nullable=False),
        sa.Column("runtime_manifest_json", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_case_tool_grants_owner_user_id", "case_tool_grants", ["owner_user_id"])
    predicate = (
        "current_setting('flowweave.bypass', true) = 'on' OR "
        "owner_user_id = current_setting('flowweave.user_id', true)"
    )
    op.execute('ALTER TABLE "case_tool_grants" ENABLE ROW LEVEL SECURITY')
    op.execute('ALTER TABLE "case_tool_grants" FORCE ROW LEVEL SECURITY')
    op.execute(
        'CREATE POLICY "pl_tenant_case_tool_grants" ON "case_tool_grants" '
        f"USING ({predicate}) WITH CHECK ({predicate})"
    )


def downgrade() -> None:
    op.drop_table("case_tool_grants")
