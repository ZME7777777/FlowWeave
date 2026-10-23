"""Join independent case control and conversation history migration branches."""

revision = "0169_merge_case_history"
down_revision = ("0162_merge_history_credential", "0168_case_control_plan_retries")
branch_labels = None
depends_on = None


def upgrade() -> None:
    pass


def downgrade() -> None:
    pass
