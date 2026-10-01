"""scope FlowRun numbers to one user.

Revision ID: 0145_tenant_flow_run_numbers
Revises: 0144_agent_session_access
"""

from __future__ import annotations

from alembic import op

revision = "0145_tenant_flow_run_numbers"
down_revision = "0144_agent_session_access"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.drop_constraint("uq_flow_run_number", "flow_runs", type_="unique")
    op.create_unique_constraint(
        "uq_flow_run_number",
        "flow_runs",
        ["owner_user_id", "flow_definition_id", "run_no"],
    )


def downgrade() -> None:
    op.drop_constraint("uq_flow_run_number", "flow_runs", type_="unique")
    op.create_unique_constraint(
        "uq_flow_run_number",
        "flow_runs",
        ["flow_definition_id", "run_no"],
    )
