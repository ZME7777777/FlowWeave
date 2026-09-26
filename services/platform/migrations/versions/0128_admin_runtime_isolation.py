"""Allow audited Runtime isolation controls.

Revision ID: 0128_admin_runtime_isolation
Revises: 0127_admin_runtime_scope
"""

from __future__ import annotations

from alembic import op

revision = "0128_admin_runtime_isolation"
down_revision = "0127_admin_runtime_scope"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.drop_constraint(
        "ck_admin_runtime_operation_action", "admin_runtime_operations", type_="check"
    )
    op.create_check_constraint(
        "ck_admin_runtime_operation_action",
        "admin_runtime_operations",
        "action IN ('REPLACE_RUNTIME', 'ISOLATE_RUNTIME', 'RESUME_RUNTIME')",
    )
    op.drop_constraint("ck_flow_run_runtime_status", "flow_run_runtimes", type_="check")
    op.create_check_constraint(
        "ck_flow_run_runtime_status",
        "flow_run_runtimes",
        (
            "status IN ('STARTING', 'ACTIVE', 'REPLACING', 'RECONNECTING', "
            "'DEGRADED', 'MAINTENANCE', 'STOPPED', 'DELETING')"
        ),
    )


def downgrade() -> None:
    op.drop_constraint("ck_flow_run_runtime_status", "flow_run_runtimes", type_="check")
    op.create_check_constraint(
        "ck_flow_run_runtime_status",
        "flow_run_runtimes",
        (
            "status IN ('STARTING', 'ACTIVE', 'REPLACING', 'RECONNECTING', "
            "'DEGRADED', 'STOPPED', 'DELETING')"
        ),
    )
    op.drop_constraint(
        "ck_admin_runtime_operation_action", "admin_runtime_operations", type_="check"
    )
    op.create_check_constraint(
        "ck_admin_runtime_operation_action",
        "admin_runtime_operations",
        "action = 'REPLACE_RUNTIME'",
    )
