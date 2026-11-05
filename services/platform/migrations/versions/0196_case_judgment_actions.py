"""Let Case coordinators conclude early and ask the human."""

from alembic import op

revision = "0196_case_judgment_actions"
down_revision = "0195_model_provider_usage_query"
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
    "COPY_STEPWISE_EXECUTION",
    "ADVANCE_CASE_STAGE",
    "COMPLETE_CASE",
]
_NEW = ["CONCLUDE_CASE", "ASK_HUMAN"]


def _check(values: list[str]) -> str:
    return "action IN (" + ", ".join(repr(v) for v in values) + ")"


def upgrade() -> None:
    op.drop_constraint("ck_case_control_plan_action", "case_control_plans", type_="check")
    op.create_check_constraint(
        "ck_case_control_plan_action", "case_control_plans", _check(_BASE + _NEW)
    )


def downgrade() -> None:
    op.execute("DELETE FROM case_control_plans WHERE action IN ('CONCLUDE_CASE', 'ASK_HUMAN')")
    op.drop_constraint("ck_case_control_plan_action", "case_control_plans", type_="check")
    op.create_check_constraint("ck_case_control_plan_action", "case_control_plans", _check(_BASE))
