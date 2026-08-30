"""Allow logical work directories to belong to a FlowRun.

Revision ID: 0074_flow_run_work_directories
Revises: 0073_default_tool_policy_v4
Create Date: 2026-08-30
"""

import sqlalchemy as sa
from alembic import op

revision = "0074_flow_run_work_directories"
down_revision = "0073_default_tool_policy_v4"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.alter_column(
        "agent_work_directories",
        "workspace_id",
        existing_type=sa.String(length=36),
        nullable=True,
    )
    op.add_column(
        "agent_work_directories",
        sa.Column("flow_run_id", sa.String(length=36), nullable=True),
    )
    op.create_foreign_key(
        "fk_agent_work_directory_flow_run",
        "agent_work_directories",
        "flow_runs",
        ["flow_run_id"],
        ["id"],
        ondelete="CASCADE",
    )
    op.create_index(
        "ix_agent_work_directories_flow_run_id",
        "agent_work_directories",
        ["flow_run_id"],
    )
    op.create_unique_constraint(
        "uq_agent_work_directory_flow_run_name",
        "agent_work_directories",
        ["flow_run_id", "display_name"],
    )
    op.create_check_constraint(
        "ck_agent_work_directory_owner",
        "agent_work_directories",
        "(workspace_id IS NOT NULL) <> (flow_run_id IS NOT NULL)",
    )
    op.execute(
        sa.text(
            "UPDATE agent_conversation_bindings "
            "SET conversation_scope_id = flow_run_id "
            "WHERE host_kind = 'FLOW_NODE' AND flow_run_id IS NOT NULL"
        )
    )


def downgrade() -> None:
    # FlowRun-owned rows cannot be represented by the pre-0074 schema. Remove
    # their dependent immutable versions explicitly before restoring the
    # non-null Agent Workspace owner column.
    op.execute(
        sa.text(
            "UPDATE agent_conversation_bindings SET work_directory_version_id = NULL "
            "WHERE work_directory_version_id IN ("
            "SELECT v.id FROM agent_work_directory_versions v "
            "JOIN agent_work_directories d ON d.id = v.work_directory_id "
            "WHERE d.flow_run_id IS NOT NULL)"
        )
    )
    op.execute(
        sa.text(
            "DELETE FROM agent_work_directory_paths WHERE version_id IN ("
            "SELECT v.id FROM agent_work_directory_versions v "
            "JOIN agent_work_directories d ON d.id = v.work_directory_id "
            "WHERE d.flow_run_id IS NOT NULL)"
        )
    )
    op.execute(
        sa.text(
            "DELETE FROM agent_work_directory_versions WHERE work_directory_id IN ("
            "SELECT id FROM agent_work_directories WHERE flow_run_id IS NOT NULL)"
        )
    )
    op.execute(sa.text("DELETE FROM agent_work_directories WHERE flow_run_id IS NOT NULL"))
    op.execute(
        sa.text(
            "UPDATE agent_conversation_bindings "
            "SET conversation_scope_id = node_attempt_id "
            "WHERE host_kind = 'FLOW_NODE' AND node_attempt_id IS NOT NULL"
        )
    )
    op.drop_constraint("ck_agent_work_directory_owner", "agent_work_directories", type_="check")
    op.drop_constraint(
        "uq_agent_work_directory_flow_run_name",
        "agent_work_directories",
        type_="unique",
    )
    op.drop_index("ix_agent_work_directories_flow_run_id", table_name="agent_work_directories")
    op.drop_constraint(
        "fk_agent_work_directory_flow_run",
        "agent_work_directories",
        type_="foreignkey",
    )
    op.drop_column("agent_work_directories", "flow_run_id")
    op.alter_column(
        "agent_work_directories",
        "workspace_id",
        existing_type=sa.String(length=36),
        nullable=False,
    )
