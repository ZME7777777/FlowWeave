"""add stable FlowRun Runtime Sessions and fenced generations

Revision ID: 0054_runtime_sessions
Revises: 0053_runtime_allocation

Historical FlowRuns and managed containers are intentionally not backfilled:
their prior state cannot prove a stable logical session or active generation.
The new Runtime Provider path adopts a compatible live physical record under
its owner lock, otherwise historical Runs fail closed at later boundaries.
"""

import sqlalchemy as sa
from alembic import op

revision = "0054_runtime_sessions"
down_revision = "0053_runtime_allocation"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "flow_run_runtimes",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("flow_run_id", sa.String(length=36), nullable=False),
        sa.Column("environment_version_id", sa.String(length=36), nullable=False),
        sa.Column("runtime_image_digest", sa.String(length=500), nullable=False),
        sa.Column("workspace_allocation_id", sa.String(length=36), nullable=False),
        sa.Column("active_generation", sa.Integer(), nullable=True),
        sa.Column("status", sa.String(length=30), nullable=False),
        sa.Column("row_version", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("stopped_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint(
            "active_generation IS NULL OR active_generation >= 1",
            name="ck_flow_run_runtime_active_generation",
        ),
        sa.CheckConstraint(
            "runtime_image_digest <> ''", name="ck_flow_run_runtime_image_digest"
        ),
        sa.CheckConstraint("row_version >= 1", name="ck_flow_run_runtime_row_version"),
        sa.CheckConstraint(
            "status IN ('STARTING', 'ACTIVE', 'REPLACING', 'RECONNECTING', "
            "'DEGRADED', 'STOPPED', 'DELETING')",
            name="ck_flow_run_runtime_status",
        ),
        sa.ForeignKeyConstraint(
            ["environment_version_id"],
            ["environment_versions.id"],
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(["flow_run_id"], ["flow_runs.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(
            ["workspace_allocation_id"],
            ["flow_run_runtime_allocations.id"],
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("flow_run_id", name="uq_flow_run_runtimes_flow_run_id"),
        sa.UniqueConstraint(
            "workspace_allocation_id",
            name="uq_flow_run_runtimes_workspace_allocation_id",
        ),
    )
    op.create_index(
        "ix_flow_run_runtimes_environment_version_id",
        "flow_run_runtimes",
        ["environment_version_id"],
    )
    op.create_index(
        "ix_flow_run_runtimes_flow_run_id",
        "flow_run_runtimes",
        ["flow_run_id"],
    )
    op.create_index(
        "ix_flow_run_runtimes_status", "flow_run_runtimes", ["status"]
    )
    op.create_index(
        "ix_flow_run_runtimes_workspace_allocation_id",
        "flow_run_runtimes",
        ["workspace_allocation_id"],
    )

    op.create_table(
        "runtime_generations",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("runtime_session_id", sa.String(length=36), nullable=False),
        sa.Column("generation", sa.Integer(), nullable=False),
        sa.Column("managed_runtime_id", sa.String(length=36), nullable=True),
        sa.Column("instance_id", sa.String(length=100), nullable=True),
        sa.Column("runtime_image_digest", sa.String(length=500), nullable=False),
        sa.Column("state", sa.String(length=30), nullable=False),
        sa.Column("fence_token", sa.String(length=36), nullable=False),
        sa.Column("row_version", sa.Integer(), nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("ready_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("draining_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("stopped_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("deleted_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("failure_code", sa.String(length=100), nullable=True),
        sa.Column("failure_summary", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint("generation >= 1", name="ck_runtime_generation_number"),
        sa.CheckConstraint(
            "runtime_image_digest <> ''", name="ck_runtime_generation_image_digest"
        ),
        sa.CheckConstraint("row_version >= 1", name="ck_runtime_generation_row_version"),
        sa.CheckConstraint(
            "state IN ('PROVISIONING', 'READY', 'DRAINING', 'STOPPED', 'DELETED', 'FAILED')",
            name="ck_runtime_generation_state",
        ),
        sa.ForeignKeyConstraint(
            ["managed_runtime_id"],
            ["managed_sandboxes.id"],
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["runtime_session_id"], ["flow_run_runtimes.id"], ondelete="RESTRICT"
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("fence_token", name="uq_runtime_generations_fence_token"),
        sa.UniqueConstraint(
            "managed_runtime_id", name="uq_runtime_generations_managed_runtime_id"
        ),
        sa.UniqueConstraint(
            "runtime_session_id",
            "generation",
            name="uq_runtime_generation_session_number",
        ),
    )
    op.create_index(
        "ix_runtime_generations_managed_runtime_id",
        "runtime_generations",
        ["managed_runtime_id"],
    )
    op.create_index(
        "ix_runtime_generations_runtime_session_id",
        "runtime_generations",
        ["runtime_session_id"],
    )
    op.create_index(
        "ix_runtime_generations_state", "runtime_generations", ["state"]
    )
    op.create_foreign_key(
        "fk_flow_run_runtime_active_generation",
        "flow_run_runtimes",
        "runtime_generations",
        ["id", "active_generation"],
        ["runtime_session_id", "generation"],
        ondelete="RESTRICT",
    )


def downgrade() -> None:
    op.drop_constraint(
        "fk_flow_run_runtime_active_generation",
        "flow_run_runtimes",
        type_="foreignkey",
    )
    op.drop_index(
        "ix_runtime_generations_state", table_name="runtime_generations"
    )
    op.drop_index(
        "ix_runtime_generations_runtime_session_id",
        table_name="runtime_generations",
    )
    op.drop_index(
        "ix_runtime_generations_managed_runtime_id",
        table_name="runtime_generations",
    )
    op.drop_table("runtime_generations")
    op.drop_index(
        "ix_flow_run_runtimes_workspace_allocation_id",
        table_name="flow_run_runtimes",
    )
    op.drop_index("ix_flow_run_runtimes_status", table_name="flow_run_runtimes")
    op.drop_index(
        "ix_flow_run_runtimes_flow_run_id", table_name="flow_run_runtimes"
    )
    op.drop_index(
        "ix_flow_run_runtimes_environment_version_id",
        table_name="flow_run_runtimes",
    )
    op.drop_table("flow_run_runtimes")
