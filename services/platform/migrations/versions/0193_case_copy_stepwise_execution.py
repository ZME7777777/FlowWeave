"""Allow Case coordinators to copy a stepwise execution record into a new draft."""

from alembic import op

revision = "0193_case_copy_stepwise_execution"
down_revision = "0192_upgrade_workspace_preset_capabilities"
branch_labels = None
depends_on = None

_BASE = [
    "CREATE_EXECUTION_COPY",
    "START_EXECUTION",
    "START_STEPWISE_NODE",
    "ACCEPT_STEPWISE_ATTEMPT",
    "REJECT_STEPWISE_ATTEMPT",
    "CANCEL_STEPWISE_ATTEMPT",
    "STOP_EXECUTION",
    "RERUN_STEPWISE_NODE",
    "REVISE_UNSTARTED_RUNTIME",
    "CONFIGURE_EXECUTION",
    "COPY_CONTINUOUS_EXECUTION",
    "ADVANCE_CASE_STAGE",
    "COMPLETE_CASE",
]


def _actions(include_copy: bool) -> str:
    values = list(_BASE)
    if include_copy:
        values.insert(values.index("COPY_CONTINUOUS_EXECUTION") + 1, "COPY_STEPWISE_EXECUTION")
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
                SELECT 1 FROM case_control_plans WHERE action = 'COPY_STEPWISE_EXECUTION'
            ) THEN
                RAISE EXCEPTION 'cannot downgrade while Case stepwise-copy plans exist';
            END IF;
        END
        $$
        """
    )
    op.drop_constraint("ck_case_control_plan_action", "case_control_plans", type_="check")
    op.create_check_constraint("ck_case_control_plan_action", "case_control_plans", _actions(False))
