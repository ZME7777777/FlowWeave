"""Merge user-isolated root defaults and named workspace configuration."""

revision = "0175_merge_workspace_session_defaults"
down_revision = (
    "0174_agent_workspace_user_defaults",
    "0174_work_directory_session_configuration",
)
branch_labels = None
depends_on = None


def upgrade() -> None:
    pass


def downgrade() -> None:
    pass
