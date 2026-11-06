"""Configure per-user model-provider budget query templates."""

import sqlalchemy as sa
from alembic import op

revision = "0195_model_provider_usage_query"
down_revision = "0194_conversation_event_count"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("model_providers", sa.Column("usage_query_json", sa.JSON(), nullable=True))


def downgrade() -> None:
    op.drop_column("model_providers", "usage_query_json")
