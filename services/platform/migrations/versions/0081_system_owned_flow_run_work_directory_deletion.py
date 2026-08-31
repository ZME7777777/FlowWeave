"""Move FlowRun work-directory deletion policy into the application service.

Revision ID: 0081_system_owned_delete
Revises: 0080_agent_conversation_contexts
Create Date: 2026-08-31
"""

import sqlalchemy as sa
from alembic import op

revision = "0081_system_owned_delete"
down_revision = "0080_agent_conversation_contexts"
branch_labels = None
depends_on = None


def upgrade() -> None:
    """Remove database-owned deletion behavior from the FlowRun directory graph."""

    op.drop_constraint(
        "fk_agent_conversation_work_directory_version",
        "agent_conversation_bindings",
        type_="foreignkey",
    )
    op.drop_constraint(
        "fk_agent_work_directory_flow_run",
        "agent_work_directories",
        type_="foreignkey",
    )
    op.drop_constraint(
        "fk_agent_work_directory_node_attempt",
        "agent_work_directories",
        type_="foreignkey",
    )
    op.drop_constraint(
        "agent_work_directory_versions_work_directory_id_fkey",
        "agent_work_directory_versions",
        type_="foreignkey",
    )
    op.drop_constraint(
        "agent_work_directory_paths_version_id_fkey",
        "agent_work_directory_paths",
        type_="foreignkey",
    )


def downgrade() -> None:
    """Restore the former constraints after discarding rows they cannot represent."""

    op.execute(
        sa.text(
            "UPDATE agent_conversation_bindings SET work_directory_version_id = NULL "
            "WHERE work_directory_version_id IS NOT NULL AND NOT EXISTS ("
            "SELECT 1 FROM agent_work_directory_versions v "
            "WHERE v.id = agent_conversation_bindings.work_directory_version_id)"
        )
    )
    op.execute(
        sa.text(
            "DELETE FROM agent_work_directory_paths p WHERE NOT EXISTS ("
            "SELECT 1 FROM agent_work_directory_versions v WHERE v.id = p.version_id)"
        )
    )
    op.execute(
        sa.text(
            "DELETE FROM agent_work_directory_versions v WHERE NOT EXISTS ("
            "SELECT 1 FROM agent_work_directories d WHERE d.id = v.work_directory_id)"
        )
    )
    op.execute(
        sa.text(
            "DELETE FROM agent_work_directories d WHERE (d.flow_run_id IS NOT NULL AND NOT EXISTS ("
            "SELECT 1 FROM flow_runs r WHERE r.id = d.flow_run_id)) OR "
            "(d.node_attempt_id IS NOT NULL AND NOT EXISTS ("
            "SELECT 1 FROM node_attempts a WHERE a.id = d.node_attempt_id))"
        )
    )
    op.create_foreign_key(
        "fk_agent_conversation_work_directory_version",
        "agent_conversation_bindings",
        "agent_work_directory_versions",
        ["work_directory_version_id"],
        ["id"],
        ondelete="RESTRICT",
    )
    op.create_foreign_key(
        "fk_agent_work_directory_flow_run",
        "agent_work_directories",
        "flow_runs",
        ["flow_run_id"],
        ["id"],
        ondelete="CASCADE",
    )
    op.create_foreign_key(
        "fk_agent_work_directory_node_attempt",
        "agent_work_directories",
        "node_attempts",
        ["node_attempt_id"],
        ["id"],
        ondelete="CASCADE",
    )
    op.create_foreign_key(
        "agent_work_directory_versions_work_directory_id_fkey",
        "agent_work_directory_versions",
        "agent_work_directories",
        ["work_directory_id"],
        ["id"],
        ondelete="RESTRICT",
    )
    op.create_foreign_key(
        "agent_work_directory_paths_version_id_fkey",
        "agent_work_directory_paths",
        "agent_work_directory_versions",
        ["version_id"],
        ["id"],
        ondelete="RESTRICT",
    )
