"""add Case control-plane foundation.

Revision ID: 0159_case_control_plane
Revises: 0158_flow_run_revision_drafts
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0159_case_control_plane"
down_revision = "0158_flow_run_revision_drafts"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "cases",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("owner_user_id", sa.String(length=36), nullable=False),
        sa.Column("name", sa.String(length=220), nullable=False),
        sa.Column("objective", sa.Text(), nullable=False, server_default=""),
        sa.Column("state", sa.String(length=20), nullable=False, server_default="DRAFT"),
        sa.Column("control_conversation_binding_id", sa.String(length=36), nullable=True),
        sa.Column("row_version", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("control_conversation_binding_id"),
        sa.CheckConstraint(
            "state IN ('DRAFT', 'ACTIVE', 'PAUSED', 'COMPLETED', 'ARCHIVED')",
            name="ck_case_state",
        ),
        sa.CheckConstraint("row_version >= 1", name="ck_case_row_version"),
    )
    op.create_index("ix_cases_owner_user_id", "cases", ["owner_user_id"])
    op.create_index("ix_cases_state", "cases", ["state"])
    op.create_table(
        "case_stages",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("owner_user_id", sa.String(length=36), nullable=False),
        sa.Column("case_id", sa.String(length=36), nullable=False),
        sa.Column("stage_key", sa.String(length=100), nullable=False),
        sa.Column("name", sa.String(length=220), nullable=False),
        sa.Column("objective", sa.Text(), nullable=False, server_default=""),
        sa.Column("position", sa.Integer(), nullable=False),
        sa.Column("state", sa.String(length=20), nullable=False, server_default="PENDING"),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("case_id", "stage_key", name="uq_case_stage_key"),
        sa.UniqueConstraint("case_id", "position", name="uq_case_stage_position"),
        sa.CheckConstraint(
            "state IN ('PENDING', 'ACTIVE', 'COMPLETED', 'SKIPPED')",
            name="ck_case_stage_state",
        ),
        sa.CheckConstraint("position >= 0", name="ck_case_stage_position"),
    )
    op.create_index("ix_case_stages_owner_user_id", "case_stages", ["owner_user_id"])
    op.create_index("ix_case_stages_case_id", "case_stages", ["case_id"])
    op.create_table(
        "case_agent_tasks",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("owner_user_id", sa.String(length=36), nullable=False),
        sa.Column("case_id", sa.String(length=36), nullable=False),
        sa.Column("stage_id", sa.String(length=36), nullable=True),
        sa.Column("task_key", sa.String(length=120), nullable=False),
        sa.Column("task_type", sa.String(length=30), nullable=False),
        sa.Column("state", sa.String(length=20), nullable=False, server_default="PENDING"),
        sa.Column("flow_run_id", sa.String(length=36), nullable=True),
        sa.Column("node_attempt_id", sa.String(length=36), nullable=True),
        sa.Column("input_context_json", sa.JSON(), nullable=False, server_default="{}"),
        sa.Column("result_json", sa.JSON(), nullable=False, server_default="{}"),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("case_id", "task_key", name="uq_case_agent_task_key"),
        sa.CheckConstraint(
            "task_type IN ('OBSERVE', 'PROPOSE_REVISION', 'REVIEW')",
            name="ck_case_agent_task_type",
        ),
        sa.CheckConstraint(
            "state IN ('PENDING', 'RUNNING', 'SUCCEEDED', 'FAILED', 'CANCELLED')",
            name="ck_case_agent_task_state",
        ),
    )
    op.create_index("ix_case_agent_tasks_owner_user_id", "case_agent_tasks", ["owner_user_id"])
    op.create_index("ix_case_agent_tasks_case_id", "case_agent_tasks", ["case_id"])
    op.create_index("ix_case_agent_tasks_stage_id", "case_agent_tasks", ["stage_id"])
    op.create_index("ix_case_agent_tasks_state", "case_agent_tasks", ["state"])
    op.create_index("ix_case_agent_tasks_flow_run_id", "case_agent_tasks", ["flow_run_id"])
    op.create_index("ix_case_agent_tasks_node_attempt_id", "case_agent_tasks", ["node_attempt_id"])
    for table in ("cases", "case_stages", "case_agent_tasks"):
        policy = f"pl_tenant_{table}"
        predicate = (
            "current_setting('flowweave.bypass', true) = 'on' OR "
            "owner_user_id = current_setting('flowweave.user_id', true)"
        )
        op.execute(sa.text(f'ALTER TABLE "{table}" ENABLE ROW LEVEL SECURITY'))
        op.execute(sa.text(f'ALTER TABLE "{table}" FORCE ROW LEVEL SECURITY'))
        statement = (
            f'CREATE POLICY "{policy}" ON "{table}" USING ({predicate}) WITH CHECK ({predicate})'
        )
        op.execute(sa.text(statement))
    for table, column in (
        ("cases", "objective"),
        ("cases", "state"),
        ("cases", "row_version"),
        ("case_stages", "objective"),
        ("case_stages", "state"),
        ("case_agent_tasks", "state"),
        ("case_agent_tasks", "input_context_json"),
        ("case_agent_tasks", "result_json"),
    ):
        op.alter_column(table, column, server_default=None)


def downgrade() -> None:
    for table in ("case_agent_tasks", "case_stages", "cases"):
        op.execute(sa.text(f'DROP POLICY IF EXISTS "pl_tenant_{table}" ON "{table}"'))
    op.drop_table("case_agent_tasks")
    op.drop_table("case_stages")
    op.drop_table("cases")
