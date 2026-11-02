"""Renewable target-scoped history credentials and explicit source allowlists."""

import sqlalchemy as sa
from alembic import op

revision = "0160_agent_history_sessions"
down_revision = "0159_agent_history_grants"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "agent_history_sessions",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("owner_user_id", sa.String(36), nullable=False),
        sa.Column("workspace_id", sa.String(36), nullable=False),
        sa.Column("target_binding_id", sa.String(36), nullable=False),
        sa.Column("target_conversation_id", sa.String(100), nullable=False),
        sa.Column("target_runtime_session_id", sa.String(36), nullable=False),
        sa.Column("token_digest", sa.String(64), nullable=False, unique=True),
        sa.Column("encrypted_token", sa.LargeBinary(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("revoked_at", sa.DateTime(timezone=True)),
        sa.UniqueConstraint("owner_user_id", "target_binding_id", name="uq_agent_history_target"),
    )
    op.create_table(
        "agent_history_sources",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("owner_user_id", sa.String(36), nullable=False),
        sa.Column("session_id", sa.String(36), nullable=False),
        sa.Column("source_binding_id", sa.String(36), nullable=False),
        sa.Column("source_conversation_id", sa.String(100), nullable=False),
        sa.Column("source_runtime_session_id", sa.String(36), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("revoked_at", sa.DateTime(timezone=True)),
        sa.UniqueConstraint("session_id", "source_binding_id", name="uq_agent_history_source"),
    )
    for table, columns in (
        (
            "agent_history_sessions",
            ("owner_user_id", "workspace_id", "target_binding_id", "expires_at"),
        ),
        (
            "agent_history_sources",
            ("owner_user_id", "session_id", "source_binding_id", "expires_at"),
        ),
    ):
        for column in columns:
            op.create_index(f"ix_{table}_{column}", table, [column])
        op.execute(f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY")
        op.execute(f"ALTER TABLE {table} FORCE ROW LEVEL SECURITY")
        policy = (
            "current_setting('flowweave.bypass', true) = 'on' OR "
            "owner_user_id = current_setting('flowweave.user_id', true)"
        )
        op.execute(
            f"CREATE POLICY pl_tenant_{table} ON {table} USING ({policy}) WITH CHECK ({policy})"
        )


def downgrade() -> None:
    op.drop_table("agent_history_sources")
    op.drop_table("agent_history_sessions")
