"""Merge MCP authentication and Case review migration branches."""

revision = "0190_merge_mcp_auth_case_review"
down_revision = (
    "0189_case_stage_review",
    "0186_mcp_authentication",
)
branch_labels = None
depends_on = None


def upgrade() -> None:
    pass


def downgrade() -> None:
    pass
