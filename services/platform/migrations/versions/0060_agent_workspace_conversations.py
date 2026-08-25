"""Add Agent Workspace conversation locators and commands.

Revision ID: 0060_agent_conversations
Revises: 0059_agent_workspace_runtime
Create Date: 2026-08-25
"""

import sqlalchemy as sa
from alembic import op

revision = "0060_agent_conversations"
down_revision = "0059_agent_workspace_runtime"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "agent_workspaces",
        sa.Column("default_model_provider_id", sa.String(36), nullable=True),
    )
    op.create_foreign_key(
        "fk_agent_workspaces_default_model_provider",
        "agent_workspaces",
        "model_providers",
        ["default_model_provider_id"],
        ["id"],
        ondelete="RESTRICT",
    )
    op.create_index(
        "ix_agent_workspaces_default_model_provider_id",
        "agent_workspaces",
        ["default_model_provider_id"],
    )
    op.create_table(
        "agent_conversation_bindings",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column(
            "workspace_id",
            sa.String(36),
            sa.ForeignKey("agent_workspaces.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column(
            "runtime_session_id",
            sa.String(36),
            sa.ForeignKey("agent_workspace_runtimes.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column("openhands_conversation_id", sa.String(36), nullable=False),
        sa.Column("display_title", sa.String(240), nullable=True),
        sa.Column("lifecycle", sa.String(20), nullable=False),
        sa.Column("create_idempotency_key", sa.String(200), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("last_connected_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("deleted_at", sa.DateTime(timezone=True), nullable=True),
        sa.UniqueConstraint(
            "runtime_session_id",
            "openhands_conversation_id",
            name="uq_agent_conversation_runtime_id",
        ),
        sa.UniqueConstraint("create_idempotency_key", name="uq_agent_conversation_create_key"),
        sa.CheckConstraint(
            "lifecycle IN ('PROVISIONING', 'ACTIVE', 'DELETE_PENDING', 'DELETED', 'FAILED')",
            name="ck_agent_conversation_lifecycle",
        ),
    )
    op.create_index(
        "ix_agent_conversation_bindings_workspace_id",
        "agent_conversation_bindings",
        ["workspace_id"],
    )
    op.create_index(
        "ix_agent_conversation_bindings_runtime_session_id",
        "agent_conversation_bindings",
        ["runtime_session_id"],
    )
    op.create_index(
        "ix_agent_conversation_bindings_lifecycle", "agent_conversation_bindings", ["lifecycle"]
    )
    op.create_table(
        "agent_conversation_commands",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column(
            "workspace_id",
            sa.String(36),
            sa.ForeignKey("agent_workspaces.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column(
            "binding_id",
            sa.String(36),
            sa.ForeignKey("agent_conversation_bindings.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column("command_type", sa.String(20), nullable=False),
        sa.Column("idempotency_key", sa.String(200), nullable=False),
        sa.Column("state", sa.String(20), nullable=False),
        sa.Column("attempt_count", sa.Integer(), nullable=False),
        sa.Column("last_error_code", sa.String(100), nullable=True),
        sa.Column("failure_summary", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("idempotency_key", name="uq_agent_conversation_command_key"),
        sa.CheckConstraint(
            "command_type IN ('CREATE', 'DELETE', 'RENAME')", name="ck_agent_command_type"
        ),
        sa.CheckConstraint(
            "state IN ('PENDING', 'SUCCEEDED', 'AMBIGUOUS', 'FAILED')",
            name="ck_agent_command_state",
        ),
    )
    op.create_index(
        "ix_agent_conversation_commands_workspace_id",
        "agent_conversation_commands",
        ["workspace_id"],
    )
    op.create_index(
        "ix_agent_conversation_commands_binding_id", "agent_conversation_commands", ["binding_id"]
    )


def downgrade() -> None:
    op.drop_table("agent_conversation_commands")
    op.drop_table("agent_conversation_bindings")
    op.drop_index("ix_agent_workspaces_default_model_provider_id", table_name="agent_workspaces")
    op.drop_constraint(
        "fk_agent_workspaces_default_model_provider", "agent_workspaces", type_="foreignkey"
    )
    op.drop_column("agent_workspaces", "default_model_provider_id")
