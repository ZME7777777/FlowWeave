"""Merge workspace archives and deployed Case coordination rounds."""

revision = "0180_merge_archives_case_rounds"
down_revision = (
    "0179_agent_workspace_and_conversation_archives",
    "0179_case_control_rounds",
)
branch_labels = None
depends_on = None


def upgrade() -> None:
    pass


def downgrade() -> None:
    pass
