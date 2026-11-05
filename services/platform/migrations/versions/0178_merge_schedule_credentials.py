"""Merge deployed workspace defaults and schedule credential defaults."""

revision = "0178_merge_schedule_credentials"
down_revision = (
    "0177_merge_workspace_credentials",
    "0177_schedule_default_credentials_all",
)
branch_labels = None
depends_on = None


def upgrade() -> None:
    pass


def downgrade() -> None:
    pass
