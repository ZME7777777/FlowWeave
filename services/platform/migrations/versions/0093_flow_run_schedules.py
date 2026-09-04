"""add independent FlowRun schedule directories

Revision ID: 0093_flow_run_schedules
Revises: 0092_node_run_names
"""

import sqlalchemy as sa
from alembic import op

revision = "0093_flow_run_schedules"
down_revision = "0092_node_run_names"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("flow_runs", sa.Column("schedule_id", sa.String(length=36), nullable=True))
    op.add_column("flow_runs", sa.Column("schedule_occurrence_id", sa.String(length=36), nullable=True))
    op.create_index("ix_flow_runs_schedule_id", "flow_runs", ["schedule_id"])
    op.create_index("ix_flow_runs_schedule_occurrence_id", "flow_runs", ["schedule_occurrence_id"])
    op.create_table(
        "flow_run_schedules",
        sa.Column("id", sa.String(length=36), primary_key=True),
        sa.Column("flow_definition_id", sa.String(length=36), nullable=False),
        sa.Column("environment_version_id", sa.String(length=36), nullable=False),
        sa.Column("name", sa.String(length=220), nullable=False),
        sa.Column("run_mode", sa.String(length=20), nullable=False),
        sa.Column("start_node_key", sa.String(length=100), nullable=False),
        sa.Column("plan_json", sa.JSON(), nullable=False),
        sa.Column("interval_minutes", sa.Integer(), nullable=False),
        sa.Column("status", sa.String(length=20), nullable=False),
        sa.Column("next_run_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("config_version", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("last_run_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("has_execution", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("row_version", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint("run_mode IN ('MANUAL', 'AUTOMATIC')", name="ck_schedule_run_mode"),
        sa.CheckConstraint("interval_minutes >= 1", name="ck_schedule_interval_positive"),
    )
    op.create_index("ix_flow_run_schedules_flow_definition_id", "flow_run_schedules", ["flow_definition_id"])
    op.create_index("ix_flow_run_schedules_next_run_at", "flow_run_schedules", ["next_run_at"])
    op.create_table(
        "flow_run_schedule_occurrences",
        sa.Column("id", sa.String(length=36), primary_key=True),
        sa.Column("schedule_id", sa.String(length=36), nullable=False),
        sa.Column("config_version", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("scheduled_for", sa.DateTime(timezone=True), nullable=True),
        sa.Column("trigger_kind", sa.String(length=20), nullable=False),
        sa.Column("state", sa.String(length=20), nullable=False),
        sa.Column("flow_run_id", sa.String(length=36), nullable=True),
        sa.Column("error_detail", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("schedule_id", "scheduled_for", name="uq_schedule_occurrence_slot"),
    )
    op.create_index("ix_schedule_occurrences_schedule_id", "flow_run_schedule_occurrences", ["schedule_id"])
    op.create_index("ix_schedule_occurrences_flow_run_id", "flow_run_schedule_occurrences", ["flow_run_id"])


def downgrade() -> None:
    op.drop_table("flow_run_schedule_occurrences")
    op.drop_table("flow_run_schedules")
    op.drop_index("ix_flow_runs_schedule_occurrence_id", table_name="flow_runs")
    op.drop_index("ix_flow_runs_schedule_id", table_name="flow_runs")
    op.drop_column("flow_runs", "schedule_occurrence_id")
    op.drop_column("flow_runs", "schedule_id")
