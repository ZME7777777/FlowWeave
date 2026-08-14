"""govern hooks, critic, goal, and stateless diagnostics

Revision ID: 0047_runtime_agent_governance
Revises: 0046_tool_policy_catalog
"""

import sqlalchemy as sa
from alembic import op

revision = "0047_runtime_agent_governance"
down_revision = "0046_tool_policy_catalog"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "runtime_critic_evaluations",
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
        sa.Column("source_type", sa.String(40), nullable=False),
        sa.Column("score", sa.Numeric(8, 7), nullable=False),
        sa.Column("message", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint("score >= 0 AND score <= 1", name="ck_runtime_critic_score"),
        sa.UniqueConstraint("conversation_id", "runtime_event_id", name="uq_runtime_critic_event"),
    )
    op.create_index(
        "ix_runtime_critic_evaluations_attempt_id", "runtime_critic_evaluations", ["attempt_id"]
    )
    op.create_index(
        "ix_runtime_critic_evaluations_conversation_id",
        "runtime_critic_evaluations",
        ["conversation_id"],
    )

    op.create_table(
        "runtime_goal_commands",
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
        sa.Column("action", sa.String(20), nullable=False),
        sa.Column("objective", sa.Text(), nullable=True),
        sa.Column("max_iterations", sa.Integer(), nullable=False),
        sa.Column("max_tokens", sa.BigInteger(), nullable=True),
        sa.Column("max_cost_usd", sa.Numeric(20, 8), nullable=True),
        sa.Column("baseline_cost_usd", sa.Numeric(20, 8), nullable=False),
        sa.Column("baseline_tokens", sa.BigInteger(), nullable=False),
        sa.Column("state", sa.String(20), nullable=False),
        sa.Column("state_version", sa.Integer(), nullable=False),
        sa.Column("idempotency_key", sa.String(180), nullable=False, unique=True),
        sa.Column("requested_by", sa.String(160), nullable=True),
        sa.Column("terminal_status", sa.String(20), nullable=True),
        sa.Column("error_code", sa.String(80), nullable=True),
        sa.Column("error_detail", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "action IN ('START', 'STOP', 'RESUME') AND "
            "state IN ('PENDING', 'RUNNING', 'SUCCEEDED', 'FAILED')",
            name="ck_runtime_goal_command_state",
        ),
        sa.CheckConstraint(
            "max_iterations >= 1 AND max_iterations <= 20 AND state_version > 0",
            name="ck_runtime_goal_command_limits",
        ),
    )
    op.create_index("ix_runtime_goal_commands_attempt_id", "runtime_goal_commands", ["attempt_id"])
    op.create_index(
        "ix_runtime_goal_commands_conversation_id", "runtime_goal_commands", ["conversation_id"]
    )
    op.create_index(
        "uq_runtime_goal_command_active",
        "runtime_goal_commands",
        ["conversation_id"],
        unique=True,
        postgresql_where=sa.text("state IN ('PENDING', 'RUNNING')"),
    )

    op.create_table(
        "runtime_goal_statuses",
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
        sa.Column("active", sa.Boolean(), nullable=False),
        sa.Column("status", sa.String(20), nullable=False),
        sa.Column("iteration", sa.Integer(), nullable=False),
        sa.Column("max_iterations", sa.Integer(), nullable=False),
        sa.Column("objective", sa.Text(), nullable=False),
        sa.Column("verdict_json", sa.JSON(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "status IN ('running', 'complete', 'capped', 'interrupted')",
            name="ck_runtime_goal_status",
        ),
        sa.UniqueConstraint(
            "conversation_id", "runtime_event_id", name="uq_runtime_goal_status_event"
        ),
    )
    op.create_index("ix_runtime_goal_statuses_attempt_id", "runtime_goal_statuses", ["attempt_id"])
    op.create_index(
        "ix_runtime_goal_statuses_conversation_id", "runtime_goal_statuses", ["conversation_id"]
    )

    op.create_table(
        "runtime_diagnostic_queries",
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
        sa.Column("question_text", sa.Text(), nullable=False),
        sa.Column("question_digest", sa.String(64), nullable=False),
        sa.Column("question_length", sa.Integer(), nullable=False),
        sa.Column("output_classification", sa.String(40), nullable=False),
        sa.Column("timeout_seconds", sa.Integer(), nullable=False),
        sa.Column("state", sa.String(20), nullable=False),
        sa.Column("idempotency_key", sa.String(180), nullable=False, unique=True),
        sa.Column("requested_by", sa.String(160), nullable=True),
        sa.Column("response_text", sa.Text(), nullable=True),
        sa.Column("cost_usd", sa.Numeric(20, 8), nullable=True),
        sa.Column("prompt_tokens", sa.BigInteger(), nullable=True),
        sa.Column("completion_tokens", sa.BigInteger(), nullable=True),
        sa.Column("error_code", sa.String(80), nullable=True),
        sa.Column("error_detail", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint(
            "state IN ('PENDING', 'RUNNING', 'SUCCEEDED', 'FAILED') "
            "AND timeout_seconds BETWEEN 1 AND 120",
            name="ck_runtime_diagnostic_query_state",
        ),
    )
    op.create_index(
        "ix_runtime_diagnostic_queries_attempt_id", "runtime_diagnostic_queries", ["attempt_id"]
    )
    op.create_index(
        "ix_runtime_diagnostic_queries_conversation_id",
        "runtime_diagnostic_queries",
        ["conversation_id"],
    )


def downgrade() -> None:
    op.drop_table("runtime_diagnostic_queries")
    op.drop_table("runtime_goal_statuses")
    op.drop_table("runtime_goal_commands")
    op.drop_table("runtime_critic_evaluations")
