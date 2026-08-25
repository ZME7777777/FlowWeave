"""Create the persistent default Agent Workspace Runtime.

Revision ID: 0059_agent_workspace_runtime
Revises: 0058_run_environment
Create Date: 2026-08-25
"""

import sqlalchemy as sa
from alembic import op

revision = "0059_agent_workspace_runtime"
down_revision = "0058_run_environment"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "agent_workspaces",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("scope_key", sa.String(100), nullable=False, unique=True),
        sa.Column("display_name", sa.String(160), nullable=False),
        sa.Column("desired_state", sa.String(20), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "desired_state IN ('RUNNING', 'MAINTENANCE')",
            name="ck_agent_workspace_desired_state",
        ),
    )
    op.create_index("ix_agent_workspaces_scope_key", "agent_workspaces", ["scope_key"])
    op.create_table(
        "agent_workspace_runtime_secret_references",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column(
            "workspace_id",
            sa.String(36),
            sa.ForeignKey("agent_workspaces.id", ondelete="RESTRICT"),
            nullable=False,
            unique=True,
        ),
        sa.Column("encrypted_secret_key", sa.LargeBinary(), nullable=False),
        sa.Column("secret_digest", sa.String(64), nullable=False, unique=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index(
        "ix_agent_workspace_runtime_secret_references_workspace_id",
        "agent_workspace_runtime_secret_references",
        ["workspace_id"],
    )
    op.create_table(
        "agent_workspace_runtime_allocations",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column(
            "workspace_id",
            sa.String(36),
            sa.ForeignKey("agent_workspaces.id", ondelete="RESTRICT"),
            nullable=False,
            unique=True,
        ),
        sa.Column(
            "secret_reference_id",
            sa.String(36),
            sa.ForeignKey("agent_workspace_runtime_secret_references.id", ondelete="RESTRICT"),
            nullable=False,
            unique=True,
        ),
        sa.Column("relative_root", sa.String(500), nullable=False, unique=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "relative_root LIKE '.agent-workspaces/%'",
            name="ck_agent_workspace_runtime_allocation_root",
        ),
    )
    op.create_index(
        "ix_agent_workspace_runtime_allocations_workspace_id",
        "agent_workspace_runtime_allocations",
        ["workspace_id"],
    )
    op.create_table(
        "agent_workspace_runtimes",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column(
            "workspace_id",
            sa.String(36),
            sa.ForeignKey("agent_workspaces.id", ondelete="RESTRICT"),
            nullable=False,
            unique=True,
        ),
        sa.Column("runtime_image_digest", sa.String(500), nullable=False),
        sa.Column(
            "workspace_allocation_id",
            sa.String(36),
            sa.ForeignKey("agent_workspace_runtime_allocations.id", ondelete="RESTRICT"),
            nullable=False,
            unique=True,
        ),
        sa.Column("active_generation", sa.Integer(), nullable=True),
        sa.Column("status", sa.String(20), nullable=False),
        sa.Column("row_version", sa.Integer(), nullable=False),
        sa.Column("failure_code", sa.String(100), nullable=True),
        sa.Column("failure_summary", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "active_generation IS NULL OR active_generation >= 1",
            name="ck_agent_workspace_runtime_generation",
        ),
        sa.CheckConstraint("row_version >= 1", name="ck_agent_workspace_runtime_row_version"),
        sa.CheckConstraint(
            "runtime_image_digest <> ''", name="ck_agent_workspace_runtime_image_digest"
        ),
        sa.CheckConstraint(
            "status IN ('STARTING', 'ACTIVE', 'RECONNECTING', 'DEGRADED', 'MAINTENANCE')",
            name="ck_agent_workspace_runtime_status",
        ),
    )
    op.create_index(
        "ix_agent_workspace_runtimes_workspace_id", "agent_workspace_runtimes", ["workspace_id"]
    )
    op.add_column(
        "managed_sandboxes",
        sa.Column("agent_workspace_allocation_id", sa.String(36), nullable=True),
    )
    op.create_foreign_key(
        "fk_managed_sandboxes_agent_workspace_allocation",
        "managed_sandboxes",
        "agent_workspace_runtime_allocations",
        ["agent_workspace_allocation_id"],
        ["id"],
        ondelete="RESTRICT",
    )
    op.create_index(
        "ix_managed_sandboxes_agent_workspace_allocation_id",
        "managed_sandboxes",
        ["agent_workspace_allocation_id"],
    )
    op.create_table(
        "agent_workspace_runtime_generations",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column(
            "runtime_session_id",
            sa.String(36),
            sa.ForeignKey("agent_workspace_runtimes.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column("generation", sa.Integer(), nullable=False),
        sa.Column(
            "managed_runtime_id",
            sa.String(36),
            sa.ForeignKey("managed_sandboxes.id", ondelete="SET NULL"),
            nullable=True,
            unique=True,
        ),
        sa.Column("runtime_image_digest", sa.String(500), nullable=False),
        sa.Column("state", sa.String(20), nullable=False),
        sa.Column("fence_token", sa.String(36), nullable=False, unique=True),
        sa.Column("row_version", sa.Integer(), nullable=False),
        sa.Column("failure_code", sa.String(100), nullable=True),
        sa.Column("failure_summary", sa.Text(), nullable=True),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("ready_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("stopped_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint(
            "runtime_session_id", "generation", name="uq_agent_workspace_runtime_generation"
        ),
        sa.CheckConstraint("generation >= 1", name="ck_agent_workspace_runtime_generation_number"),
        sa.CheckConstraint(
            "row_version >= 1", name="ck_agent_workspace_runtime_generation_row_version"
        ),
        sa.CheckConstraint(
            "state IN ('PROVISIONING', 'READY', 'STOPPED', 'DELETED', 'FAILED')",
            name="ck_agent_workspace_runtime_generation_state",
        ),
    )
    op.create_index(
        "ix_agent_workspace_runtime_generations_runtime_session_id",
        "agent_workspace_runtime_generations",
        ["runtime_session_id"],
    )
    op.create_index(
        "ix_agent_workspace_runtime_generations_managed_runtime_id",
        "agent_workspace_runtime_generations",
        ["managed_runtime_id"],
    )


def downgrade() -> None:
    op.drop_table("agent_workspace_runtime_generations")
    op.drop_index(
        "ix_managed_sandboxes_agent_workspace_allocation_id", table_name="managed_sandboxes"
    )
    op.drop_constraint(
        "fk_managed_sandboxes_agent_workspace_allocation",
        "managed_sandboxes",
        type_="foreignkey",
    )
    op.drop_column("managed_sandboxes", "agent_workspace_allocation_id")
    op.drop_table("agent_workspace_runtimes")
    op.drop_table("agent_workspace_runtime_allocations")
    op.drop_table("agent_workspace_runtime_secret_references")
    op.drop_index("ix_agent_workspaces_scope_key", table_name="agent_workspaces")
    op.drop_table("agent_workspaces")
