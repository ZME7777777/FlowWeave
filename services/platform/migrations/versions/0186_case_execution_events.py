"""Transactional Case execution event inbox with frozen routing references."""

import sqlalchemy as sa
from alembic import op

revision = "0186_case_execution_events"
down_revision = "0185_case_materials"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "case_execution_events",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("owner_user_id", sa.String(36), nullable=False),
        sa.Column(
            "case_id",
            sa.String(36),
            sa.ForeignKey("cases.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("stage_id", sa.String(36), nullable=False),
        sa.Column("stage_key", sa.String(100), nullable=False),
        sa.Column("execution_flow_run_id", sa.String(36), nullable=False),
        sa.Column("run_event_cursor", sa.BigInteger(), nullable=False),
        sa.Column("event_type", sa.String(80), nullable=False),
        sa.Column("fact_type", sa.String(40), nullable=False),
        sa.Column("node_run_id", sa.String(36)),
        sa.Column("node_key", sa.String(100)),
        sa.Column("node_attempt_id", sa.String(36)),
        sa.Column("candidate_output_set_id", sa.String(36)),
        sa.Column("artifact_version_id", sa.String(36)),
        sa.Column("native_completion_event_id", sa.String(200)),
        sa.Column("round_id", sa.String(36)),
        sa.Column("binding_id", sa.String(36)),
        sa.Column("case_row_version", sa.Integer(), nullable=False),
        sa.Column("case_state", sa.String(20), nullable=False),
        sa.Column("stage_state", sa.String(20), nullable=False),
        sa.Column("routing_state", sa.String(20), nullable=False),
        sa.Column("routing_reason", sa.String(80)),
        sa.Column("occurred_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("case_id", "run_event_cursor", name="uq_case_execution_event_source"),
        sa.CheckConstraint(
            "routing_state IN ('PENDING', 'HELD', 'AUDIT_ONLY')",
            name="ck_case_execution_event_routing",
        ),
        sa.CheckConstraint("case_row_version >= 1", name="ck_case_execution_event_version"),
    )
    op.create_index(
        "ix_case_execution_events_owner_user_id", "case_execution_events", ["owner_user_id"]
    )
    op.create_index("ix_case_execution_events_case_id", "case_execution_events", ["case_id"])
    op.create_index(
        "ix_case_execution_event_queue",
        "case_execution_events",
        ["round_id", "routing_state", "run_event_cursor"],
    )
    predicate = (
        "current_setting('flowweave.bypass', true) = 'on' OR "
        "owner_user_id = current_setting('flowweave.user_id', true)"
    )
    op.execute('ALTER TABLE "case_execution_events" ENABLE ROW LEVEL SECURITY')
    op.execute('ALTER TABLE "case_execution_events" FORCE ROW LEVEL SECURITY')
    op.execute(
        'CREATE POLICY "pl_tenant_case_execution_events" ON "case_execution_events" '
        f"USING ({predicate}) WITH CHECK ({predicate})"
    )
    # Historical RunEvents have no occurrence-time coordinator identity. Do not
    # invent one from today's Case round or replay old executions into it.


def downgrade() -> None:
    op.drop_table("case_execution_events")
