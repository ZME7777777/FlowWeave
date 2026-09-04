"""make new FlowRun Runtime allocations Attempt-owned

Revision ID: 0094_attempt_owned_runtimes
Revises: 0093_flow_run_schedules
"""

import sqlalchemy as sa
from alembic import op

revision = "0094_attempt_owned_runtimes"
down_revision = "0093_flow_run_schedules"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Existing FlowRun rows deliberately keep a NULL attempt owner: their
    # workspace and conversation state cannot safely be attributed to one
    # Attempt. New records are always keyed by node_attempt_id.
    op.add_column(
        "flow_run_runtime_allocations",
        sa.Column("node_attempt_id", sa.String(length=36), nullable=True),
    )
    op.add_column(
        "flow_run_runtimes",
        sa.Column("node_attempt_id", sa.String(length=36), nullable=True),
    )
    op.drop_constraint(
        "uq_flow_run_runtime_allocations_flow_run_id",
        "flow_run_runtime_allocations",
        type_="unique",
    )
    op.drop_constraint("uq_flow_run_runtimes_flow_run_id", "flow_run_runtimes", type_="unique")
    op.create_unique_constraint(
        "uq_flow_run_runtime_allocations_node_attempt_id",
        "flow_run_runtime_allocations",
        ["node_attempt_id"],
    )
    op.create_unique_constraint(
        "uq_flow_run_runtimes_node_attempt_id", "flow_run_runtimes", ["node_attempt_id"]
    )
    op.create_index(
        "ix_flow_run_runtime_allocations_node_attempt_id",
        "flow_run_runtime_allocations",
        ["node_attempt_id"],
    )
    op.create_index(
        "ix_flow_run_runtimes_node_attempt_id", "flow_run_runtimes", ["node_attempt_id"]
    )


def downgrade() -> None:
    bind = op.get_bind()
    if bind.execute(
        sa.text(
            "SELECT 1 FROM flow_run_runtime_allocations WHERE node_attempt_id IS NOT NULL LIMIT 1"
        )
    ).first():
        raise RuntimeError("Cannot downgrade Attempt-owned Runtime allocations; archive them first")
    if bind.execute(
        sa.text("SELECT 1 FROM flow_run_runtimes WHERE node_attempt_id IS NOT NULL LIMIT 1")
    ).first():
        raise RuntimeError("Cannot downgrade Attempt-owned Runtime sessions; archive them first")
    op.drop_index("ix_flow_run_runtimes_node_attempt_id", table_name="flow_run_runtimes")
    op.drop_index(
        "ix_flow_run_runtime_allocations_node_attempt_id", table_name="flow_run_runtime_allocations"
    )
    op.drop_constraint("uq_flow_run_runtimes_node_attempt_id", "flow_run_runtimes", type_="unique")
    op.drop_constraint(
        "uq_flow_run_runtime_allocations_node_attempt_id",
        "flow_run_runtime_allocations",
        type_="unique",
    )
    op.create_unique_constraint(
        "uq_flow_run_runtimes_flow_run_id", "flow_run_runtimes", ["flow_run_id"]
    )
    op.create_unique_constraint(
        "uq_flow_run_runtime_allocations_flow_run_id",
        "flow_run_runtime_allocations",
        ["flow_run_id"],
    )
    op.drop_column("flow_run_runtimes", "node_attempt_id")
    op.drop_column("flow_run_runtime_allocations", "node_attempt_id")
