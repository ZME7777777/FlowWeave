"""Allow Case coordinators to cancel a runtime-delivery-blocked stepwise Attempt."""

from alembic import op

revision = "0191_case_cancel_stepwise_attempt"
down_revision = "0190_merge_mcp_auth_case_review"
branch_labels = None
depends_on = None

_BASE = [
    "CREATE_EXECUTION_COPY",
    "START_EXECUTION",
    "START_STEPWISE_NODE",
    "ACCEPT_STEPWISE_ATTEMPT",
    "REJECT_STEPWISE_ATTEMPT",
    "STOP_EXECUTION",
    "RERUN_STEPWISE_NODE",
    "REVISE_UNSTARTED_RUNTIME",
    "CONFIGURE_EXECUTION",
    "COPY_CONTINUOUS_EXECUTION",
    "ADVANCE_CASE_STAGE",
    "COMPLETE_CASE",
]


def _actions(include_cancel: bool) -> str:
    values = list(_BASE)
    if include_cancel:
        values.insert(5, "CANCEL_STEPWISE_ATTEMPT")
    return "action IN (" + ", ".join(repr(value) for value in values) + ")"


def upgrade() -> None:
    op.drop_constraint("ck_case_control_plan_action", "case_control_plans", type_="check")
    op.create_check_constraint("ck_case_control_plan_action", "case_control_plans", _actions(True))


def downgrade() -> None:
    op.execute(
        """
        DO $$
        BEGIN
            IF EXISTS (
                SELECT 1 FROM case_control_plans WHERE action = 'CANCEL_STEPWISE_ATTEMPT'
            ) THEN
                RAISE EXCEPTION 'cannot downgrade while Case cancel-attempt plans exist';
            END IF;
        END
        $$
        """
    )
    op.drop_constraint("ck_case_control_plan_action", "case_control_plans", type_="check")
    op.create_check_constraint("ck_case_control_plan_action", "case_control_plans", _actions(False))
