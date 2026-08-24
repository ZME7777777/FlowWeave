"""Archive platform Conversation truth and keep FlowRun-native locators.

Revision ID: 0057_flow_run_conversations
Revises: 0056_runtime_replacement
Create Date: 2026-08-22
"""

import sqlalchemy as sa
from alembic import op

revision = "0057_flow_run_conversations"
down_revision = "0056_runtime_replacement"
branch_labels = None
depends_on = None


_LEGACY_TABLES = (
    "message_artifact_refs",
    "runtime_subagent_task_usage",
    "runtime_subagent_tasks",
    "runtime_confirmation_batches",
    "runtime_condensation_commands",
    "runtime_diagnostic_queries",
    "runtime_goal_statuses",
    "runtime_goal_commands",
    "runtime_critic_evaluations",
    "runtime_conversation_forks",
    "runtime_condensations",
    "agent_messages",
    "agent_conversations",
)
_LEGACY_ATTEMPT_COLUMNS = (
    "runtime_adapter",
    "runtime_job_id",
    "runtime_cursor",
    "runtime_sandbox_id",
)
_LEGACY_TASK_TYPES = (
    "CREATE_CONVERSATION",
    "FORK_CONVERSATION",
    "CLEANUP_CONVERSATION_RUNTIME",
    "CONDENSE_CONVERSATION",
    "CONTROL_CONVERSATION_GOAL",
    "ASK_CONVERSATION_AGENT",
    "DELIVER_CONVERSATION_MESSAGE",
    "POLL_CONVERSATION",
    "WAIT_CONVERSATION_WAKEUP",
    "STOP_CONVERSATION_RUNTIME",
)


def upgrade() -> None:
    # The pre-FR-09 projection cannot be proven equivalent to OpenHands' native
    # persistence. Preserve it for the FR-11 read-only archive instead of
    # guessing a migration into the active locator model.
    for table_name in _LEGACY_TABLES:
        op.rename_table(table_name, f"archived_{table_name}")
    for column_name in _LEGACY_ATTEMPT_COLUMNS:
        op.alter_column(
            "node_attempts",
            column_name,
            new_column_name=f"archived_{column_name}",
        )
    quoted_task_types = ", ".join(f"'{item}'" for item in _LEGACY_TASK_TYPES)
    op.execute(
        sa.text(
            "UPDATE background_tasks SET state = 'DEAD', lease_owner = NULL, "
            "lease_until = NULL, last_error = "
            "'Archived by FR-09: OpenHands owns Conversation lifecycle' "
            f"WHERE task_type IN ({quoted_task_types}) "
            "AND state IN ('PENDING', 'RUNNING', 'RETRY')"
        )
    )

    op.create_table(
        "runtime_confirmation_approvals",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column(
            "flow_run_conversation_binding_id",
            sa.String(36),
            sa.ForeignKey("flow_run_conversation_bindings.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "attempt_id",
            sa.String(36),
            sa.ForeignKey("node_attempts.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("pending_actions_digest", sa.String(64), nullable=False),
        sa.Column("pending_actions_json", sa.JSON(), nullable=False),
        sa.Column("risk_summary_json", sa.JSON(), nullable=False),
        sa.Column("action_count", sa.Integer(), nullable=False),
        sa.Column("state", sa.String(20), nullable=False),
        sa.Column("decision_accept", sa.Boolean(), nullable=True),
        sa.Column("decision_reason", sa.Text(), nullable=True),
        sa.Column("decision_idempotency_key", sa.String(180), nullable=True),
        sa.Column("decided_by", sa.String(160), nullable=True),
        sa.Column("decided_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("state_version", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint("action_count > 0", name="ck_runtime_confirmation_action_count"),
        sa.CheckConstraint("state_version > 0", name="ck_runtime_confirmation_version"),
        sa.CheckConstraint(
            "state IN ('PENDING', 'DECIDING', 'APPROVED', 'REJECTED', 'EXPIRED', 'CANCELLED')",
            name="ck_runtime_confirmation_approval_state",
        ),
        sa.UniqueConstraint(
            "decision_idempotency_key",
            name="uq_runtime_confirmation_decision_idempotency",
        ),
    )
    op.create_index(
        "ix_runtime_confirmation_approvals_attempt_id",
        "runtime_confirmation_approvals",
        ["attempt_id"],
    )
    op.create_index(
        "ix_runtime_confirmation_approvals_binding_id",
        "runtime_confirmation_approvals",
        ["flow_run_conversation_binding_id"],
    )
    op.create_index(
        "uq_runtime_confirmation_approval_active",
        "runtime_confirmation_approvals",
        ["flow_run_conversation_binding_id"],
        unique=True,
        postgresql_where=sa.text("state IN ('PENDING', 'DECIDING')"),
    )


def downgrade() -> None:
    op.drop_table("runtime_confirmation_approvals")
    for column_name in reversed(_LEGACY_ATTEMPT_COLUMNS):
        op.alter_column(
            "node_attempts",
            f"archived_{column_name}",
            new_column_name=column_name,
        )
    for table_name in reversed(_LEGACY_TABLES):
        op.rename_table(f"archived_{table_name}", table_name)
