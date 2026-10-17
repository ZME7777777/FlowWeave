"""correct unstarted stepwise records to the draft lifecycle state.

Revision ID: 0151_stepwise_draft_state
Revises: 0150_mcp_oauth_persistence_target_audit
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0151_stepwise_draft_state"
down_revision = "0150_mcp_oauth_persistence_target_audit"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Earlier child stepwise records inherited FlowRun's ACTIVE ORM default
    # despite having no NodeRun. Only correct that impossible pre-start shape;
    # an executed record keeps its authoritative lifecycle state.
    op.execute(
        sa.text(
            """
            UPDATE flow_runs
            SET state = 'DRAFT'
            WHERE run_mode = 'MANUAL'
              AND parent_flow_run_id IS NOT NULL
              AND state = 'ACTIVE'
              AND NOT EXISTS (
                  SELECT 1
                  FROM node_runs
                  WHERE node_runs.flow_run_id = flow_runs.id
              )
            """
        )
    )


def downgrade() -> None:
    # This is a one-way data correction. Reverting schema history must not
    # relabel real unstarted records as active executions.
    pass
