"""Let a user mark one provider as primary so conversations default to it."""

import sqlalchemy as sa
from alembic import op

revision = "0199_provider_primary"
down_revision = "0198_provider_model_protocol"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "model_providers",
        sa.Column("is_primary", sa.Boolean(), nullable=False, server_default=sa.text("false")),
    )
    # One primary provider per owner; enforced by the database, not by callers.
    op.create_index(
        "uq_model_provider_owner_primary",
        "model_providers",
        ["owner_user_id"],
        unique=True,
        postgresql_where=sa.text("is_primary = true"),
    )


def downgrade() -> None:
    op.drop_index("uq_model_provider_owner_primary", table_name="model_providers")
    op.drop_column("model_providers", "is_primary")
