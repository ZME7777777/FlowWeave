"""Add user-isolated, revocable history-only MCP grants (no plaintext tokens)."""

import sqlalchemy as sa
from alembic import op

revision = "0159_agent_history_grants"
down_revision = "0158_schedule_china_timezone"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "agent_conversation_history_grants",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("owner_user_id", sa.String(36), nullable=False),
        sa.Column("workspace_id", sa.String(36), nullable=False),
        sa.Column("target_binding_id", sa.String(36), nullable=False),
        sa.Column("source_binding_id", sa.String(36), nullable=False),
        sa.Column("target_conversation_id", sa.String(100), nullable=False),
        sa.Column("source_conversation_id", sa.String(100), nullable=False),
        sa.Column("target_runtime_session_id", sa.String(36), nullable=False),
        sa.Column("source_runtime_session_id", sa.String(36), nullable=False),
        sa.Column("token_digest", sa.String(64), nullable=False, unique=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
    )
    for column in (
        "owner_user_id",
        "workspace_id",
        "target_binding_id",
        "source_binding_id",
        "expires_at",
    ):
        op.create_index(
            f"ix_agent_conversation_history_grants_{column}",
            "agent_conversation_history_grants",
            [column],
        )
    op.execute("ALTER TABLE agent_conversation_history_grants ENABLE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE agent_conversation_history_grants FORCE ROW LEVEL SECURITY")
    policy = (
        "current_setting('flowweave.bypass', true) = 'on' OR "
        "owner_user_id = current_setting('flowweave.user_id', true)"
    )
    op.execute(
        "CREATE POLICY pl_tenant_agent_conversation_history_grants "
        f"ON agent_conversation_history_grants USING ({policy}) WITH CHECK ({policy})"
    )


def downgrade() -> None:
    op.drop_table("agent_conversation_history_grants")
