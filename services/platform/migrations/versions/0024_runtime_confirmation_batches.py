"""add native OpenHands confirmation batches

Revision ID: 0024_confirmation_batches
Revises: 0023_skill_collections
"""

import sqlalchemy as sa
from alembic import op

revision = "0024_confirmation_batches"
down_revision = "0023_skill_collections"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "runtime_confirmation_batches",
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
        sa.Column("runtime_conversation_id", sa.String(100), nullable=False),
        sa.Column("runtime_cursor", sa.String(200), nullable=True),
        sa.Column("pending_actions_digest", sa.String(64), nullable=False),
        sa.Column("pending_actions_json", sa.JSON(), nullable=False),
        sa.Column("risk_summary_json", sa.JSON(), nullable=False),
        sa.Column("policy_version_id", sa.String(36), nullable=True),
        sa.Column("action_count", sa.Integer(), nullable=False),
        sa.Column("state", sa.String(20), nullable=False, server_default="PENDING"),
        sa.Column("decision_accept", sa.Boolean(), nullable=True),
        sa.Column("decision_reason", sa.Text(), nullable=True),
        sa.Column("decision_idempotency_key", sa.String(180), nullable=True),
        sa.Column("decided_by", sa.String(160), nullable=True),
        sa.Column("decided_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("runtime_response_cursor", sa.String(200), nullable=True),
        sa.Column("state_version", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint("action_count > 0", name="ck_runtime_confirmation_actions_positive"),
        sa.CheckConstraint("state_version > 0", name="ck_runtime_confirmation_version_positive"),
        sa.CheckConstraint(
            "state IN ('PENDING', 'DECIDING', 'APPROVED', 'REJECTED', 'EXPIRED', 'CANCELLED')",
            name="ck_runtime_confirmation_state",
        ),
        sa.UniqueConstraint(
            "decision_idempotency_key", name="uq_runtime_confirmation_decision_key"
        ),
    )
    op.create_index(
        "ix_runtime_confirmation_batches_attempt_id",
        "runtime_confirmation_batches",
        ["attempt_id"],
    )
    op.create_index(
        "ix_runtime_confirmation_batches_conversation_id",
        "runtime_confirmation_batches",
        ["conversation_id"],
    )
    op.create_index(
        "ix_runtime_confirmation_attempt_created",
        "runtime_confirmation_batches",
        ["attempt_id", "created_at"],
    )
    op.create_index(
        "uq_runtime_confirmation_pending",
        "runtime_confirmation_batches",
        ["conversation_id"],
        unique=True,
        postgresql_where=sa.text("state IN ('PENDING', 'DECIDING')"),
    )


def downgrade() -> None:
    op.drop_table("runtime_confirmation_batches")
