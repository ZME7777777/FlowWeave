"""Fence cancellation before native message dispatch."""

import sqlalchemy as sa
from alembic import op

revision = "0185_message_submission_cancel"
down_revision = "0184_cascade_directory_archives"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "agent_message_submissions",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("owner_user_id", sa.String(36), nullable=False),
        sa.Column(
            "binding_id",
            sa.String(36),
            sa.ForeignKey("agent_conversation_bindings.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("submission_id", sa.String(200), nullable=False),
        sa.Column("phase", sa.String(20), nullable=False),
        sa.Column("pause_requested", sa.Boolean(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint(
            "owner_user_id", "binding_id", "submission_id", name="uq_agent_submission"
        ),
        sa.CheckConstraint(
            "phase IN ('PREPARING', 'DISPATCHING', 'CANCELLED')", name="ck_agent_submission_phase"
        ),
    )
    op.create_index(
        "ix_agent_message_submissions_binding_id", "agent_message_submissions", ["binding_id"]
    )
    op.create_index(
        "ix_agent_message_submissions_owner_user_id", "agent_message_submissions", ["owner_user_id"]
    )
    predicate = (
        "current_setting('flowweave.bypass', true) = 'on' OR "
        "owner_user_id = current_setting('flowweave.user_id', true)"
    )
    op.execute('ALTER TABLE "agent_message_submissions" ENABLE ROW LEVEL SECURITY')
    op.execute('ALTER TABLE "agent_message_submissions" FORCE ROW LEVEL SECURITY')
    op.execute(
        'CREATE POLICY "pl_tenant_agent_message_submissions" ON "agent_message_submissions" '
        f"USING ({predicate}) WITH CHECK ({predicate})"
    )


def downgrade() -> None:
    op.drop_table("agent_message_submissions")
