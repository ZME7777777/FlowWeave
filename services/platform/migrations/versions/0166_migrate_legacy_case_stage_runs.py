"""migrate direct Case stage runs into historical executions.

Revision ID: 0166_migrate_legacy_case_stage_runs
Revises: 0165_case_stage_execution_start
"""

from __future__ import annotations

from alembic import op

revision = "0166_migrate_legacy_case_stage_runs"
down_revision = "0165_case_stage_execution_start"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Before 0164, flow_run_id named the Case's directly executed FlowRun.
    # It cannot safely become a reusable template: it may carry Attempts,
    # conversations, runtime output, or incomplete stepwise configuration.
    # Preserve it as the current historical execution and require an explicit
    # complete template to be bound before a future Case execution is created.
    op.execute(
        "UPDATE case_stages "
        "SET current_execution_flow_run_id = flow_run_id, flow_run_id = NULL "
        "WHERE flow_run_id IS NOT NULL AND current_execution_flow_run_id IS NULL"
    )


def downgrade() -> None:
    # Restore only rows that are unambiguously the legacy direct binding.
    # A stage with a newly bound template must retain that template.
    op.execute(
        "UPDATE case_stages "
        "SET flow_run_id = current_execution_flow_run_id, current_execution_flow_run_id = NULL "
        "WHERE flow_run_id IS NULL AND current_execution_flow_run_id IS NOT NULL"
    )
