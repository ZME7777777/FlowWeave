"""Scope FlowRun logical work directories to one node Attempt.

Revision ID: 0078_node_attempt_work_dirs
Revises: 0077_generalized_node_io
Create Date: 2026-08-31
"""

import sqlalchemy as sa
from alembic import op

revision = "0078_node_attempt_work_dirs"
down_revision = "0077_generalized_node_io"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "agent_work_directories",
        sa.Column("node_attempt_id", sa.String(length=36), nullable=True),
    )
    op.create_foreign_key(
        "fk_agent_work_directory_node_attempt",
        "agent_work_directories",
        "node_attempts",
        ["node_attempt_id"],
        ["id"],
        ondelete="CASCADE",
    )
    op.create_index(
        "ix_agent_work_directories_node_attempt_id",
        "agent_work_directories",
        ["node_attempt_id"],
    )
    op.drop_constraint(
        "uq_agent_work_directory_flow_run_name",
        "agent_work_directories",
        type_="unique",
    )
    op.create_unique_constraint(
        "uq_agent_work_directory_flow_run_attempt_name",
        "agent_work_directories",
        ["flow_run_id", "node_attempt_id", "display_name"],
    )
    op.drop_constraint("ck_agent_work_directory_owner", "agent_work_directories", type_="check")
    op.create_check_constraint(
        "ck_agent_work_directory_owner",
        "agent_work_directories",
        "(workspace_id IS NOT NULL AND flow_run_id IS NULL AND node_attempt_id IS NULL) "
        "OR (workspace_id IS NULL AND flow_run_id IS NOT NULL)",
    )
    # Existing FlowRun-level directory rows have no trustworthy creator
    # Attempt. Keep them for migration/audit only; new node entries never list
    # or select them rather than guessing an owner and leaking them cross-node.


def downgrade() -> None:
    # Attempt-scoped rows cannot be represented in the prior FlowRun-wide
    # schema. Remove their dependent frozen references before restoring it.
    op.execute(
        sa.text(
            "UPDATE agent_conversation_bindings SET work_directory_version_id = NULL "
            "WHERE work_directory_version_id IN ("
            "SELECT v.id FROM agent_work_directory_versions v "
            "JOIN agent_work_directories d ON d.id = v.work_directory_id "
            "WHERE d.node_attempt_id IS NOT NULL)"
        )
    )
    op.execute(
        sa.text(
            "DELETE FROM agent_work_directory_paths WHERE version_id IN ("
            "SELECT v.id FROM agent_work_directory_versions v "
            "JOIN agent_work_directories d ON d.id = v.work_directory_id "
            "WHERE d.node_attempt_id IS NOT NULL)"
        )
    )
    op.execute(
        sa.text(
            "DELETE FROM agent_work_directory_versions WHERE work_directory_id IN ("
            "SELECT id FROM agent_work_directories WHERE node_attempt_id IS NOT NULL)"
        )
    )
    op.execute(sa.text("DELETE FROM agent_work_directories WHERE node_attempt_id IS NOT NULL"))
    op.drop_constraint("ck_agent_work_directory_owner", "agent_work_directories", type_="check")
    op.create_check_constraint(
        "ck_agent_work_directory_owner",
        "agent_work_directories",
        "(workspace_id IS NOT NULL) <> (flow_run_id IS NOT NULL)",
    )
    op.drop_constraint(
        "uq_agent_work_directory_flow_run_attempt_name",
        "agent_work_directories",
        type_="unique",
    )
    op.create_unique_constraint(
        "uq_agent_work_directory_flow_run_name",
        "agent_work_directories",
        ["flow_run_id", "display_name"],
    )
    op.drop_index("ix_agent_work_directories_node_attempt_id", table_name="agent_work_directories")
    op.drop_constraint(
        "fk_agent_work_directory_node_attempt",
        "agent_work_directories",
        type_="foreignkey",
    )
    op.drop_column("agent_work_directories", "node_attempt_id")
