"""project native OpenHands Task cumulative usage and budget facts

Revision ID: 0037_subagent_task_usage
Revises: 0036_native_subagent_tasks
"""

import sqlalchemy as sa
from alembic import op

revision = "0037_subagent_task_usage"
down_revision = "0036_native_subagent_tasks"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "runtime_subagent_task_usage",
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
        sa.Column(
            "runtime_subagent_task_id",
            sa.String(36),
            sa.ForeignKey("runtime_subagent_tasks.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("runtime_task_id", sa.String(100), nullable=False),
        sa.Column("source_cursor", sa.String(200), nullable=True),
        sa.Column("snapshot_digest", sa.String(64), nullable=False),
        sa.Column("usage_version", sa.Integer(), nullable=False),
        sa.Column("model_name", sa.String(200), nullable=False),
        sa.Column("accumulated_cost_usd", sa.Numeric(20, 8), nullable=False),
        sa.Column("prompt_tokens", sa.BigInteger(), nullable=False),
        sa.Column("completion_tokens", sa.BigInteger(), nullable=False),
        sa.Column("cache_read_tokens", sa.BigInteger(), nullable=False),
        sa.Column("cache_write_tokens", sa.BigInteger(), nullable=False),
        sa.Column("reasoning_tokens", sa.BigInteger(), nullable=False),
        sa.Column("context_window", sa.BigInteger(), nullable=False),
        sa.Column("per_turn_tokens", sa.BigInteger(), nullable=False),
        sa.Column("budget_limit_usd", sa.Numeric(20, 8), nullable=True),
        sa.Column("budget_state", sa.String(20), nullable=False),
        sa.Column("budget_exceeded_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint("usage_version > 0", name="ck_runtime_subagent_usage_version"),
        sa.CheckConstraint(
            "accumulated_cost_usd >= 0 AND prompt_tokens >= 0 "
            "AND completion_tokens >= 0 AND cache_read_tokens >= 0 "
            "AND cache_write_tokens >= 0 AND reasoning_tokens >= 0 "
            "AND context_window >= 0 AND per_turn_tokens >= 0",
            name="ck_runtime_subagent_usage_nonnegative",
        ),
        sa.CheckConstraint(
            "budget_state IN ('UNBOUNDED', 'WITHIN', 'EXCEEDED')",
            name="ck_runtime_subagent_usage_budget_state",
        ),
    )
    op.create_index(
        "ix_runtime_subagent_task_usage_attempt_id",
        "runtime_subagent_task_usage",
        ["attempt_id"],
    )
    op.create_index(
        "ix_runtime_subagent_task_usage_conversation_id",
        "runtime_subagent_task_usage",
        ["conversation_id"],
    )
    op.create_index(
        "uq_runtime_subagent_task_usage_task",
        "runtime_subagent_task_usage",
        ["runtime_subagent_task_id"],
        unique=True,
    )
    op.create_index(
        "uq_runtime_subagent_task_usage_identity",
        "runtime_subagent_task_usage",
        ["conversation_id", "runtime_task_id"],
        unique=True,
    )


def downgrade() -> None:
    op.drop_table("runtime_subagent_task_usage")
