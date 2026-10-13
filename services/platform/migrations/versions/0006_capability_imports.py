"""persistent capability import sessions"""

import sqlalchemy as sa
from alembic import op

revision = "0006_capability_imports"
down_revision = "0005_execution"
branch_labels = None
depends_on = None


def upgrade():
    # 0095 owns tenant ownership. Live ORM metadata would add its column early.
    op.create_table(
        "capability_imports",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("token_digest", sa.String(64), nullable=False),
        sa.Column("capability_type", sa.String(16), nullable=False),
        sa.Column("filename", sa.String(255), nullable=False),
        sa.Column("content_hash", sa.String(64), nullable=False),
        sa.Column("storage_key", sa.Text(), nullable=False),
        sa.Column("byte_size", sa.Integer(), nullable=False),
        sa.Column("preview_json", sa.JSON(), nullable=False),
        sa.Column("state", sa.String(20), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("consumed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "state IN ('VALIDATED', 'COMMITTED', 'EXPIRED')", name="ck_capability_import_state"
        ),
    )
    op.create_index(
        "ix_capability_imports_token_digest", "capability_imports", ["token_digest"], unique=True
    )
    for column in ("content_hash", "state", "expires_at"):
        op.create_index(f"ix_capability_imports_{column}", "capability_imports", [column])


def downgrade():
    op.drop_table("capability_imports")
