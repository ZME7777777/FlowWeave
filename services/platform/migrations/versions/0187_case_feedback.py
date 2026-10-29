"""Durable native Case feedback delivery and acknowledgement audit."""

import sqlalchemy as sa
from alembic import op

revision = "0187_case_feedback"
down_revision = "0186_case_execution_events"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "case_feedback_deliveries",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("owner_user_id", sa.String(36), nullable=False),
        sa.Column(
            "case_id", sa.String(36), sa.ForeignKey("cases.id", ondelete="CASCADE"), nullable=False
        ),
        sa.Column("round_id", sa.String(36), nullable=False),
        sa.Column("binding_id", sa.String(36), nullable=False),
        sa.Column("source_key", sa.String(140), nullable=False),
        sa.Column("source_kind", sa.String(40), nullable=False),
        sa.Column("execution_event_id", sa.String(36)),
        sa.Column("case_row_version", sa.Integer(), nullable=False),
        sa.Column("batch_id", sa.String(36)),
        sa.Column("state", sa.String(20), nullable=False),
        sa.Column("message_event_id", sa.String(200)),
        sa.Column("error_code", sa.String(100)),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("delivered_at", sa.DateTime(timezone=True)),
        sa.UniqueConstraint("case_id", "source_key", name="uq_case_feedback_source"),
        sa.UniqueConstraint("execution_event_id", name="uq_case_feedback_execution_event"),
        sa.CheckConstraint(
            "state IN ('PENDING', 'SUBMITTING', 'ACKNOWLEDGED', 'DELIVERED', "
            "'AMBIGUOUS', 'AUDIT_ONLY', 'FAILED')",
            name="ck_case_feedback_state",
        ),
    )
    for column in ("owner_user_id", "case_id", "batch_id"):
        op.create_index(
            f"ix_case_feedback_deliveries_{column}", "case_feedback_deliveries", [column]
        )
    op.create_index(
        "ix_case_feedback_queue", "case_feedback_deliveries", ["round_id", "state", "created_at"]
    )
    predicate = (
        "current_setting('flowweave.bypass', true) = 'on' OR "
        "owner_user_id = current_setting('flowweave.user_id', true)"
    )
    op.execute('ALTER TABLE "case_feedback_deliveries" ENABLE ROW LEVEL SECURITY')
    op.execute('ALTER TABLE "case_feedback_deliveries" FORCE ROW LEVEL SECURITY')
    op.execute(
        'CREATE POLICY "pl_tenant_case_feedback_deliveries" ON "case_feedback_deliveries" '
        f"USING ({predicate}) WITH CHECK ({predicate})"
    )
    # CASE-05A pending/held inbox rows already have occurrence-time recipients.
    # Worker recovery schedules these; no historical RunEvents are reconstructed.


def downgrade() -> None:
    op.drop_table("case_feedback_deliveries")
