"""Make the shared Agent-session locator host-neutral.

Revision ID: 0071_shared_agent_session_hosts
Revises: 0070_agent_caps
Create Date: 2026-08-29

The direct Agent Workspace keeps its historical table and values.  FlowRun
node sessions will use the same table in the following service cutover; this
revision deliberately makes the locator capable of expressing both hosts
without inventing a second session schema.
"""

import sqlalchemy as sa
from alembic import op

revision = "0071_shared_agent_session_hosts"
down_revision = "0070_agent_caps"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.drop_constraint(
        "agent_conversation_bindings_runtime_session_id_fkey",
        "agent_conversation_bindings",
        type_="foreignkey",
    )
    op.alter_column(
        "agent_conversation_bindings",
        "workspace_id",
        existing_type=sa.String(length=36),
        nullable=True,
    )
    op.add_column(
        "agent_conversation_bindings",
        sa.Column("host_kind", sa.String(length=30), nullable=True),
    )
    op.add_column(
        "agent_conversation_bindings",
        sa.Column("host_id", sa.String(length=36), nullable=True),
    )
    op.add_column(
        "agent_conversation_bindings",
        sa.Column("conversation_scope_id", sa.String(length=36), nullable=True),
    )
    op.add_column(
        "agent_conversation_bindings",
        sa.Column("flow_run_id", sa.String(length=36), nullable=True),
    )
    op.add_column(
        "agent_conversation_bindings",
        sa.Column("node_run_id", sa.String(length=36), nullable=True),
    )
    op.add_column(
        "agent_conversation_bindings",
        sa.Column("node_attempt_id", sa.String(length=36), nullable=True),
    )
    op.execute(
        sa.text(
            "UPDATE agent_conversation_bindings "
            "SET host_kind = 'AGENT_WORKSPACE', "
            "host_id = workspace_id, conversation_scope_id = workspace_id"
        )
    )
    for column in ("host_kind", "host_id", "conversation_scope_id"):
        op.alter_column(
            "agent_conversation_bindings",
            column,
            existing_type=sa.String(length=30 if column == "host_kind" else 36),
            nullable=False,
        )
    op.create_check_constraint(
        "ck_agent_conversation_host_kind",
        "agent_conversation_bindings",
        "host_kind IN ('AGENT_WORKSPACE', 'FLOW_NODE')",
    )
    op.create_foreign_key(
        "fk_agent_conversation_flow_run",
        "agent_conversation_bindings",
        "flow_runs",
        ["flow_run_id"],
        ["id"],
        ondelete="CASCADE",
    )
    op.create_foreign_key(
        "fk_agent_conversation_node_run",
        "agent_conversation_bindings",
        "node_runs",
        ["node_run_id"],
        ["id"],
        ondelete="CASCADE",
    )
    op.create_foreign_key(
        "fk_agent_conversation_node_attempt",
        "agent_conversation_bindings",
        "node_attempts",
        ["node_attempt_id"],
        ["id"],
        ondelete="CASCADE",
    )
    op.create_index(
        "ix_agent_conversation_bindings_host_scope",
        "agent_conversation_bindings",
        ["host_kind", "host_id", "conversation_scope_id", "lifecycle"],
    )

    op.alter_column(
        "agent_conversation_commands",
        "workspace_id",
        existing_type=sa.String(length=36),
        nullable=True,
    )
    op.add_column(
        "agent_conversation_commands",
        sa.Column("host_kind", sa.String(length=30), nullable=True),
    )
    op.add_column(
        "agent_conversation_commands",
        sa.Column("host_id", sa.String(length=36), nullable=True),
    )
    op.execute(
        sa.text(
            "UPDATE agent_conversation_commands "
            "SET host_kind = 'AGENT_WORKSPACE', host_id = workspace_id"
        )
    )
    op.alter_column(
        "agent_conversation_commands",
        "host_kind",
        existing_type=sa.String(length=30),
        nullable=False,
    )
    op.alter_column(
        "agent_conversation_commands",
        "host_id",
        existing_type=sa.String(length=36),
        nullable=False,
    )
    op.create_index(
        "ix_agent_conversation_commands_host",
        "agent_conversation_commands",
        ["host_kind", "host_id"],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_agent_conversation_commands_host",
        table_name="agent_conversation_commands",
    )
    op.drop_column("agent_conversation_commands", "host_id")
    op.drop_column("agent_conversation_commands", "host_kind")
    op.alter_column(
        "agent_conversation_commands",
        "workspace_id",
        existing_type=sa.String(length=36),
        nullable=False,
    )

    op.drop_index(
        "ix_agent_conversation_bindings_host_scope",
        table_name="agent_conversation_bindings",
    )
    op.drop_constraint(
        "fk_agent_conversation_node_attempt",
        "agent_conversation_bindings",
        type_="foreignkey",
    )
    op.drop_constraint(
        "fk_agent_conversation_node_run",
        "agent_conversation_bindings",
        type_="foreignkey",
    )
    op.drop_constraint(
        "fk_agent_conversation_flow_run",
        "agent_conversation_bindings",
        type_="foreignkey",
    )
    op.drop_constraint(
        "ck_agent_conversation_host_kind",
        "agent_conversation_bindings",
        type_="check",
    )
    for column in (
        "node_attempt_id",
        "node_run_id",
        "flow_run_id",
        "conversation_scope_id",
        "host_id",
        "host_kind",
    ):
        op.drop_column("agent_conversation_bindings", column)
    op.alter_column(
        "agent_conversation_bindings",
        "workspace_id",
        existing_type=sa.String(length=36),
        nullable=False,
    )
    op.create_foreign_key(
        "agent_conversation_bindings_runtime_session_id_fkey",
        "agent_conversation_bindings",
        "agent_workspace_runtimes",
        ["runtime_session_id"],
        ["id"],
        ondelete="RESTRICT",
    )
