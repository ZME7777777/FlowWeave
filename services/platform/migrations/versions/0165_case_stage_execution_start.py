"""audit Case stage execution starts.

Revision ID: 0165_case_stage_execution_start
Revises: 0164_case_stage_execution_records
"""

from __future__ import annotations

from alembic import op

revision = "0165_case_stage_execution_start"
down_revision = "0164_case_stage_execution_records"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.drop_constraint("ck_case_decision_proposal_action", "case_decision_proposals", type_="check")
    op.create_check_constraint(
        "ck_case_decision_proposal_action",
        "case_decision_proposals",
        "action IN ('START_STAGE_EXECUTION', 'CONTINUE_STEPWISE', 'STOP_FLOW_RUN', "
        "'RERUN_STEPWISE_NODE', 'REVISE_UNSTARTED_RUNTIME', 'COPY_AUTOMATIC_RUN')",
    )


def downgrade() -> None:
    op.drop_constraint("ck_case_decision_proposal_action", "case_decision_proposals", type_="check")
    op.create_check_constraint(
        "ck_case_decision_proposal_action",
        "case_decision_proposals",
        "action IN ('CONTINUE_STEPWISE', 'STOP_FLOW_RUN', 'RERUN_STEPWISE_NODE', "
        "'REVISE_UNSTARTED_RUNTIME', 'COPY_AUTOMATIC_RUN')",
    )
