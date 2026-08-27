"""Freeze lazy Agent Conversation bootstrap context.

Revision ID: 0067_agent_bootstrap
Revises: 0066_agent_work_directories
Create Date: 2026-08-27
"""

import sqlalchemy as sa
from alembic import op

revision = "0067_agent_bootstrap"
down_revision = "0066_agent_work_directories"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "agent_conversation_bindings",
        sa.Column("work_directory_version_id", sa.String(36), nullable=True),
    )
    op.add_column(
        "agent_conversation_bindings",
        sa.Column("working_directory", sa.String(500), nullable=True),
    )
    op.add_column(
        "agent_conversation_bindings",
        sa.Column("bootstrap_parent_event_id", sa.String(200), nullable=True),
    )
    op.add_column(
        "agent_conversation_bindings",
        sa.Column("initial_user_event_id", sa.String(200), nullable=True),
    )
    op.create_foreign_key(
        "fk_agent_conversation_work_directory_version",
        "agent_conversation_bindings",
        "agent_work_directory_versions",
        ["work_directory_version_id"],
        ["id"],
        ondelete="RESTRICT",
    )
    op.create_index(
        "ix_agent_conversation_bindings_work_directory_version_id",
        "agent_conversation_bindings",
        ["work_directory_version_id"],
    )
    op.create_check_constraint(
        "ck_agent_conversation_working_directory",
        "agent_conversation_bindings",
        "working_directory IS NULL OR working_directory = '/runtime/workspace/project' "
        "OR working_directory LIKE '/runtime/workspace/project/%'",
    )


def downgrade() -> None:
    op.drop_constraint(
        "ck_agent_conversation_working_directory", "agent_conversation_bindings", type_="check"
    )
    op.drop_index(
        "ix_agent_conversation_bindings_work_directory_version_id",
        table_name="agent_conversation_bindings",
    )
    op.drop_constraint(
        "fk_agent_conversation_work_directory_version",
        "agent_conversation_bindings",
        type_="foreignkey",
    )
    op.drop_column("agent_conversation_bindings", "bootstrap_parent_event_id")
    op.drop_column("agent_conversation_bindings", "initial_user_event_id")
    op.drop_column("agent_conversation_bindings", "working_directory")
    op.drop_column("agent_conversation_bindings", "work_directory_version_id")
