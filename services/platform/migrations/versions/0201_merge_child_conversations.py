"""Join the independently deployed child and main platform migration graphs."""

revision = "0201_merge_child_conversations"
down_revision = ("0180_child_completion_read_watermark", "0200_message_model_changes")
branch_labels = None
depends_on = None


def upgrade() -> None:
    pass


def downgrade() -> None:
    pass
