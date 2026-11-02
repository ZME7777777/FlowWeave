"""Join independent credential metadata and history authority migration branches."""

revision = "0162_merge_history_credential"
down_revision = ("0159_credential_context_version", "0161_agent_history_capability")
branch_labels = None
depends_on = None


def upgrade() -> None:
    pass


def downgrade() -> None:
    pass
