"""Require explicit Case review before cross-stage progression."""

from alembic import op

revision = "0189_case_stage_review"
down_revision = "0188_case_stepwise_review"
branch_labels = None
depends_on = None


def _actions(cross_stage: bool) -> str:
    values = [
        "CREATE_EXECUTION_COPY",
        "START_EXECUTION",
        "START_STEPWISE_NODE",
        "ACCEPT_STEPWISE_ATTEMPT",
        "REJECT_STEPWISE_ATTEMPT",
        "STOP_EXECUTION",
        "RERUN_STEPWISE_NODE",
        "REVISE_UNSTARTED_RUNTIME",
        "CONFIGURE_EXECUTION",
    ]
    if cross_stage:
        values.extend(["COPY_CONTINUOUS_EXECUTION", "ADVANCE_CASE_STAGE", "COMPLETE_CASE"])
    return "action IN (" + ", ".join(repr(value) for value in values) + ")"


def _stage_states(review_pending: bool) -> str:
    values = ["PENDING", "ACTIVE", "COMPLETED", "SKIPPED"]
    if review_pending:
        values.insert(2, "REVIEW_PENDING")
    return "state IN (" + ", ".join(repr(value) for value in values) + ")"


def upgrade() -> None:
    op.drop_constraint("ck_case_control_plan_action", "case_control_plans", type_="check")
    op.create_check_constraint("ck_case_control_plan_action", "case_control_plans", _actions(True))
    op.drop_constraint("ck_case_stage_state", "case_stages", type_="check")
    op.create_check_constraint("ck_case_stage_state", "case_stages", _stage_states(True))


def downgrade() -> None:
    op.execute(
        """
        DO $$
        BEGIN
            IF EXISTS (SELECT 1 FROM case_stages WHERE state = 'REVIEW_PENDING') OR EXISTS (
                SELECT 1 FROM case_control_plans
                WHERE action IN ('COPY_CONTINUOUS_EXECUTION', 'ADVANCE_CASE_STAGE', 'COMPLETE_CASE')
            ) THEN
                RAISE EXCEPTION 'cannot downgrade while Case cross-stage review records exist';
            END IF;
        END
        $$
        """
    )
    op.drop_constraint("ck_case_stage_state", "case_stages", type_="check")
    op.create_check_constraint("ck_case_stage_state", "case_stages", _stage_states(False))
    op.drop_constraint("ck_case_control_plan_action", "case_control_plans", type_="check")
    op.create_check_constraint("ck_case_control_plan_action", "case_control_plans", _actions(False))
