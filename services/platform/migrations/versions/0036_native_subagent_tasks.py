"""project native OpenHands Task tool invocations

Revision ID: 0036_native_subagent_tasks
Revises: 0035_capability_collections
"""

import sqlalchemy as sa
from alembic import op

revision = "0036_native_subagent_tasks"
down_revision = "0035_capability_collections"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "runtime_subagent_tasks",
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
        sa.Column("action_event_id", sa.String(200), nullable=False),
        sa.Column("action_cursor", sa.String(200), nullable=True),
        sa.Column("tool_call_id", sa.String(200), nullable=True),
        sa.Column("llm_response_id", sa.String(200), nullable=True),
        sa.Column("observation_event_id", sa.String(200), nullable=True),
        sa.Column("observation_cursor", sa.String(200), nullable=True),
        sa.Column("runtime_task_id", sa.String(100), nullable=True),
        sa.Column("subagent_type", sa.String(200), nullable=False),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("resume_task_id", sa.String(100), nullable=True),
        sa.Column("state", sa.String(20), nullable=False),
        sa.Column("native_status", sa.String(40), nullable=True),
        sa.Column("result", sa.Text(), nullable=True),
        sa.Column("error_detail", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "state IN ('REQUESTED', 'COMPLETED', 'ERROR')",
            name="ck_runtime_subagent_task_state",
        ),
    )
    op.create_index(
        "ix_runtime_subagent_tasks_attempt_id",
        "runtime_subagent_tasks",
        ["attempt_id"],
    )
    op.create_index(
        "ix_runtime_subagent_tasks_conversation_id",
        "runtime_subagent_tasks",
        ["conversation_id"],
    )
    op.create_index(
        "ix_runtime_subagent_tasks_tool_call_id",
        "runtime_subagent_tasks",
        ["tool_call_id"],
    )
    op.create_index(
        "ix_runtime_subagent_tasks_llm_response_id",
        "runtime_subagent_tasks",
        ["llm_response_id"],
    )
    op.create_index(
        "ix_runtime_subagent_tasks_runtime_task_id",
        "runtime_subagent_tasks",
        ["runtime_task_id"],
    )
    op.create_index(
        "ix_runtime_subagent_tasks_state",
        "runtime_subagent_tasks",
        ["state"],
    )
    op.create_index(
        "ix_runtime_subagent_task_attempt_created",
        "runtime_subagent_tasks",
        ["attempt_id", "created_at"],
    )
    op.create_index(
        "uq_runtime_subagent_task_action",
        "runtime_subagent_tasks",
        ["conversation_id", "action_event_id"],
        unique=True,
    )
    op.create_index(
        "uq_runtime_subagent_task_observation",
        "runtime_subagent_tasks",
        ["conversation_id", "observation_event_id"],
        unique=True,
        postgresql_where=sa.text("observation_event_id IS NOT NULL"),
    )

    # The old platform-owned sub-agent executor is no longer part of the
    # runtime protocol. Freeze any rows left by an interrupted rolling upgrade
    # and prevent their durable work from being replayed. They remain readable
    # as historical conversations, but are never presented as native Tasks.
    bind = op.get_bind()
    bind.execute(
        sa.text(
            "UPDATE agent_conversations SET state = 'IDLE', "
            "state_version = state_version + 1 "
            "WHERE state = 'WAITING_SUBAGENTS'"
        )
    )
    bind.execute(
        sa.text(
            "UPDATE agent_conversations SET state = 'READ_ONLY', "
            "state_version = state_version + 1 "
            "WHERE kind = 'SUBAGENT' AND state <> 'READ_ONLY'"
        )
    )
    bind.execute(
        sa.text(
            "UPDATE background_tasks SET state = 'DEAD', lease_owner = NULL, "
            "lease_until = NULL, last_error = 'LEGACY_SUBAGENT_EXECUTOR_REMOVED' "
            "WHERE state IN ('PENDING', 'RETRY', 'RUNNING') AND ("
            "aggregate_id IN (SELECT id FROM agent_conversations WHERE kind = 'SUBAGENT') "
            "OR aggregate_id IN (SELECT id FROM agent_messages WHERE conversation_id IN "
            "(SELECT id FROM agent_conversations WHERE kind = 'SUBAGENT')))"
        )
    )
    bind.execute(
        sa.text(
            "UPDATE managed_sandboxes SET desired_state = 'DELETED', "
            "next_reconcile_at = CURRENT_TIMESTAMP "
            "WHERE owner_type = 'CONVERSATION' AND owner_id IN "
            "(SELECT id FROM agent_conversations WHERE kind = 'SUBAGENT') "
            "AND desired_state <> 'DELETED'"
        )
    )

    # Remove the schema surface that made the retired executor appear to be a
    # supported protocol. Historical child conversations keep their title and
    # message log, but no new code can create or resume a platform-owned task.
    inspector = sa.inspect(bind)
    indexes = {item["name"] for item in inspector.get_indexes("agent_conversations")}
    if "ix_agent_conversations_delegation_batch_key" in indexes:
        op.drop_index(
            "ix_agent_conversations_delegation_batch_key",
            table_name="agent_conversations",
        )
    if "ix_agent_conversations_parent_conversation_id" in indexes:
        op.drop_index(
            "ix_agent_conversations_parent_conversation_id",
            table_name="agent_conversations",
        )
    parent_fk = next(
        (
            item.get("name")
            for item in inspector.get_foreign_keys("agent_conversations")
            if item.get("constrained_columns") == ["parent_conversation_id"]
        ),
        None,
    )
    if parent_fk:
        op.drop_constraint(parent_fk, "agent_conversations", type_="foreignkey")
    columns = {item["name"] for item in inspector.get_columns("agent_conversations")}
    for column in (
        "delegation_instruction",
        "delegation_batch_key",
        "parent_conversation_id",
    ):
        if column in columns:
            op.drop_column("agent_conversations", column)


def downgrade() -> None:
    op.drop_table("runtime_subagent_tasks")
    op.add_column(
        "agent_conversations",
        sa.Column("parent_conversation_id", sa.String(length=36), nullable=True),
    )
    op.add_column(
        "agent_conversations",
        sa.Column("delegation_batch_key", sa.String(length=100), nullable=True),
    )
    op.add_column(
        "agent_conversations",
        sa.Column("delegation_instruction", sa.Text(), nullable=True),
    )
    op.create_foreign_key(
        "fk_agent_conversation_parent",
        "agent_conversations",
        "agent_conversations",
        ["parent_conversation_id"],
        ["id"],
        ondelete="CASCADE",
    )
    op.create_index(
        "ix_agent_conversations_parent_conversation_id",
        "agent_conversations",
        ["parent_conversation_id"],
    )
    op.create_index(
        "ix_agent_conversations_delegation_batch_key",
        "agent_conversations",
        ["delegation_batch_key"],
    )
