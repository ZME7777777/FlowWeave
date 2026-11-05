"""Project model-change dividers above the user message that first used a new model."""

import sqlalchemy as sa
from alembic import op

revision = "0200_message_model_changes"
down_revision = "0199_provider_primary"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("agent_conversation_bindings", sa.Column("last_sent_provider_id", sa.String(36)))
    op.add_column("agent_conversation_bindings", sa.Column("last_sent_model_name", sa.String(240)))
    op.create_table(
        "agent_message_model_changes",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("owner_user_id", sa.String(36), nullable=False),
        sa.Column(
            "binding_id",
            sa.String(36),
            sa.ForeignKey("agent_conversation_bindings.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("event_id", sa.String(200), nullable=False),
        sa.Column("from_provider_id", sa.String(36)),
        sa.Column("from_model_name", sa.String(240)),
        sa.Column("from_label", sa.String(300), nullable=False),
        sa.Column("to_provider_id", sa.String(36)),
        sa.Column("to_model_name", sa.String(240)),
        sa.Column("to_label", sa.String(300), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("binding_id", "event_id", name="uq_agent_message_model_change"),
    )
    op.create_index(
        "ix_agent_message_model_changes_binding_id", "agent_message_model_changes", ["binding_id"]
    )
    op.create_index(
        "ix_agent_message_model_changes_owner_user_id",
        "agent_message_model_changes",
        ["owner_user_id"],
    )
    predicate = (
        "current_setting('flowweave.bypass', true) = 'on' OR "
        "owner_user_id = current_setting('flowweave.user_id', true)"
    )
    op.execute('ALTER TABLE "agent_message_model_changes" ENABLE ROW LEVEL SECURITY')
    op.execute('ALTER TABLE "agent_message_model_changes" FORCE ROW LEVEL SECURITY')
    op.execute(
        'CREATE POLICY "pl_tenant_agent_message_model_changes" ON "agent_message_model_changes" '
        f"USING ({predicate}) WITH CHECK ({predicate})"
    )


def downgrade() -> None:
    op.drop_table("agent_message_model_changes")
    op.drop_column("agent_conversation_bindings", "last_sent_model_name")
    op.drop_column("agent_conversation_bindings", "last_sent_provider_id")
