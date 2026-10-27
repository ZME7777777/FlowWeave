"""Merge deployed temporary services and workspace credential defaults."""

revision = "0177_merge_workspace_credentials"
down_revision = (
    "0176_agent_temporary_services",
    "0176_workspace_default_credentials_all",
)
branch_labels = None
depends_on = None


def upgrade() -> None:
    pass


def downgrade() -> None:
    pass
