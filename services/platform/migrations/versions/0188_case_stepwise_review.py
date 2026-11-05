"""Authorize audited Case stepwise review and rerun actions."""

from alembic import op

revision = "0188_case_stepwise_review"
down_revision = "0187_case_feedback"
branch_labels = None
depends_on = None


def _actions(review: bool) -> str:
    values = [
        "CREATE_EXECUTION_COPY",
        "START_EXECUTION",
        "START_STEPWISE_NODE",
        "STOP_EXECUTION",
        "RERUN_STEPWISE_NODE",
        "REVISE_UNSTARTED_RUNTIME",
        "CONFIGURE_EXECUTION",
    ]
    if review:
        values.extend(["ACCEPT_STEPWISE_ATTEMPT", "REJECT_STEPWISE_ATTEMPT"])
    return "action IN (" + ", ".join(repr(value) for value in values) + ")"


def upgrade() -> None:
    op.drop_constraint("ck_case_control_plan_action", "case_control_plans", type_="check")
    op.create_check_constraint("ck_case_control_plan_action", "case_control_plans", _actions(True))


def downgrade() -> None:
    # Audited review rows are durable execution evidence. Operators must export or
    # migrate them before removing the actions from the database contract.
    op.execute(
        """
        DO $$
        BEGIN
            IF EXISTS (
                SELECT 1 FROM case_control_plans
                WHERE action IN ('ACCEPT_STEPWISE_ATTEMPT', 'REJECT_STEPWISE_ATTEMPT')
            ) THEN
                RAISE EXCEPTION 'cannot downgrade while Case stepwise review audit rows exist';
            END IF;
        END
        $$
        """
    )
    op.drop_constraint("ck_case_control_plan_action", "case_control_plans", type_="check")
    op.create_check_constraint("ck_case_control_plan_action", "case_control_plans", _actions(False))
