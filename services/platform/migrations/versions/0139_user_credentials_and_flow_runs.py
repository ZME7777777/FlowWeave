"""isolate credentials and FlowRun records by user.

Revision ID: 0139_user_credentials_runs
Revises: 0138_admin_resource_cleanup_operations
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0139_user_credentials_runs"
down_revision = "0138_admin_resource_cleanup_operations"
branch_labels = None
depends_on = None

_FLOWWEAVE_USER_ID = "6311561c-06e4-41ad-8afe-aac35cfa83ec"
_USER_SCOPED_TABLES = (
    "website_credentials",
    "flow_runs",
    "flow_run_schedules",
    "flow_run_schedule_occurrences",
    "run_snapshots",
    "node_runs",
    "node_attempts",
    "artifact_versions",
    "candidate_output_sets",
    "attempt_input_bindings",
    "gate_evaluations",
    "human_actions",
    "run_events",
    "runtime_confirmation_approvals",
    "flow_run_runtime_secret_references",
    "flow_run_runtime_allocations",
    "flow_run_runtimes",
    "runtime_generations",
)


def _enable_tenant_policy(table: str) -> None:
    policy = f"pl_tenant_{table}"
    op.execute(sa.text(f'DROP POLICY IF EXISTS "{policy}" ON "{table}"'))
    op.execute(sa.text(f'ALTER TABLE "{table}" ENABLE ROW LEVEL SECURITY'))
    op.execute(sa.text(f'ALTER TABLE "{table}" FORCE ROW LEVEL SECURITY'))
    op.execute(
        sa.text(
            f'CREATE POLICY "{policy}" ON "{table}" '
            "USING (current_setting('flowweave.bypass', true) = 'on' OR "
            "owner_user_id = current_setting('flowweave.user_id', true)) "
            "WITH CHECK (current_setting('flowweave.bypass', true) = 'on' OR "
            "owner_user_id = current_setting('flowweave.user_id', true))"
        )
    )


def _disable_tenant_policy(table: str) -> None:
    policy = f"pl_tenant_{table}"
    op.execute(sa.text(f'DROP POLICY IF EXISTS "{policy}" ON "{table}"'))
    op.execute(sa.text(f'ALTER TABLE "{table}" NO FORCE ROW LEVEL SECURITY'))
    op.execute(sa.text(f'ALTER TABLE "{table}" DISABLE ROW LEVEL SECURITY'))


def upgrade() -> None:
    for table in _USER_SCOPED_TABLES:
        op.execute(
            sa.text(f'UPDATE "{table}" SET owner_user_id = :owner').bindparams(
                owner=_FLOWWEAVE_USER_ID
            )
        )
        _enable_tenant_policy(table)


def downgrade() -> None:
    for table in reversed(_USER_SCOPED_TABLES):
        _disable_tenant_policy(table)
