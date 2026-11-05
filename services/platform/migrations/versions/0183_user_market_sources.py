"""Persist each user's shared Plugin/Skill market sources."""

import sqlalchemy as sa
from alembic import op

revision = "0183_user_market_sources"
down_revision = "0182_case_flow_tools"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "users",
        sa.Column("market_sources_json", sa.JSON(), nullable=False, server_default=sa.text("'[]'")),
    )
    op.alter_column("users", "market_sources_json", server_default=None)


def downgrade() -> None:
    op.drop_column("users", "market_sources_json")
