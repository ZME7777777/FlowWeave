"""allocate external FlowRun Runtime storage and stable secret references

Revision ID: 0053_runtime_allocation
Revises: 0052_flow_environment
"""

import sqlalchemy as sa
from alembic import op

revision = "0053_runtime_allocation"
down_revision = "0052_flow_environment"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "flow_run_runtime_secret_references",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("encrypted_secret_key", sa.LargeBinary(), nullable=False),
        sa.Column("secret_digest", sa.String(length=64), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "secret_digest", name="uq_flow_run_runtime_secret_references_secret_digest"
        ),
    )
    op.create_table(
        "flow_run_runtime_allocations",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("flow_run_id", sa.String(length=36), nullable=False),
        sa.Column("secret_reference_id", sa.String(length=36), nullable=False),
        sa.Column("relative_root", sa.String(length=500), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "relative_root LIKE '.flow-run-runtimes/%'",
            name="ck_flow_run_runtime_allocation_root",
        ),
        sa.ForeignKeyConstraint(["flow_run_id"], ["flow_runs.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(
            ["secret_reference_id"],
            ["flow_run_runtime_secret_references.id"],
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("flow_run_id", name="uq_flow_run_runtime_allocations_flow_run_id"),
        sa.UniqueConstraint(
            "secret_reference_id",
            name="uq_flow_run_runtime_allocations_secret_reference_id",
        ),
        sa.UniqueConstraint("relative_root", name="uq_flow_run_runtime_allocations_relative_root"),
    )
    op.create_index(
        "ix_flow_run_runtime_allocations_flow_run_id",
        "flow_run_runtime_allocations",
        ["flow_run_id"],
    )
    op.create_index(
        "ix_flow_run_runtime_allocations_secret_reference_id",
        "flow_run_runtime_allocations",
        ["secret_reference_id"],
    )
    op.add_column(
        "managed_sandboxes",
        sa.Column("runtime_allocation_id", sa.String(length=36), nullable=True),
    )
    op.create_index(
        "ix_managed_sandboxes_runtime_allocation_id",
        "managed_sandboxes",
        ["runtime_allocation_id"],
    )
    op.create_foreign_key(
        "fk_managed_sandboxes_runtime_allocation",
        "managed_sandboxes",
        "flow_run_runtime_allocations",
        ["runtime_allocation_id"],
        ["id"],
        ondelete="RESTRICT",
    )


def downgrade() -> None:
    op.drop_constraint(
        "fk_managed_sandboxes_runtime_allocation",
        "managed_sandboxes",
        type_="foreignkey",
    )
    op.drop_index("ix_managed_sandboxes_runtime_allocation_id", table_name="managed_sandboxes")
    op.drop_column("managed_sandboxes", "runtime_allocation_id")
    op.drop_index(
        "ix_flow_run_runtime_allocations_secret_reference_id",
        table_name="flow_run_runtime_allocations",
    )
    op.drop_index(
        "ix_flow_run_runtime_allocations_flow_run_id",
        table_name="flow_run_runtime_allocations",
    )
    op.drop_table("flow_run_runtime_allocations")
    op.drop_table("flow_run_runtime_secret_references")
