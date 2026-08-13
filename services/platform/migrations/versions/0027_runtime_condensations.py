"""persist native OpenHands condensation events

Revision ID: 0027_runtime_condensations
Revises: 0026_condenser_policy
"""

import sqlalchemy as sa
from alembic import op

revision = "0027_runtime_condensations"
down_revision = "0026_condenser_policy"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if "runtime_condensations" in inspector.get_table_names():
        return
    op.create_table(
        "runtime_condensations",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column(
            "attempt_id",
            sa.String(36),
            sa.ForeignKey("node_attempts.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "conversation_id",
            sa.String(36),
            sa.ForeignKey("agent_conversations.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("runtime_event_id", sa.String(200), nullable=False),
        sa.Column("runtime_cursor", sa.String(200), nullable=False),
        sa.Column("event_type", sa.String(20), nullable=False),
        sa.Column("forgotten_event_ids_json", sa.JSON(), nullable=False),
        sa.Column("summary", sa.Text(), nullable=True),
        sa.Column("summary_offset", sa.Integer(), nullable=True),
        sa.Column("llm_response_id", sa.String(200), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "summary_offset IS NULL OR summary_offset >= 0",
            name="ck_runtime_condensation_summary_offset",
        ),
        sa.CheckConstraint(
            "event_type IN ('REQUESTED', 'COMPLETED')",
            name="ck_runtime_condensation_event_type",
        ),
    )
    op.create_index(
        "ix_runtime_condensations_attempt_id",
        "runtime_condensations",
        ["attempt_id"],
    )
    op.create_index(
        "ix_runtime_condensations_conversation_id",
        "runtime_condensations",
        ["conversation_id"],
    )
    op.create_index(
        "ix_runtime_condensations_llm_response_id",
        "runtime_condensations",
        ["llm_response_id"],
    )
    op.create_index(
        "ix_runtime_condensation_attempt_created",
        "runtime_condensations",
        ["attempt_id", "created_at"],
    )
    op.create_index(
        "uq_runtime_condensation_event",
        "runtime_condensations",
        ["conversation_id", "runtime_event_id"],
        unique=True,
    )


def downgrade() -> None:
    bind = op.get_bind()
    if "runtime_condensations" in sa.inspect(bind).get_table_names():
        op.drop_table("runtime_condensations")
