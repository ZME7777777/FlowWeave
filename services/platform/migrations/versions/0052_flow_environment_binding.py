"""freeze custom environment versions on flows and run snapshots

Revision ID: 0052_flow_environment
Revises: 0051_physical_delete

Historical rows intentionally remain nullable. FR-01 rejects them at every
new publication/runtime boundary instead of guessing a default Environment.
"""

import sqlalchemy as sa
from alembic import op

revision = "0052_flow_environment"
down_revision = "0051_physical_delete"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "terminal_environments",
        sa.Column("base_image_digest", sa.String(length=100), nullable=True),
    )
    op.add_column(
        "environment_versions",
        sa.Column("base_image_reference", sa.String(length=500), nullable=False, server_default=""),
    )
    op.add_column(
        "environment_versions",
        sa.Column("base_image_digest", sa.String(length=100), nullable=False, server_default=""),
    )
    op.add_column(
        "environment_setup_sessions",
        sa.Column("base_image_digest", sa.String(length=100), nullable=False, server_default=""),
    )

    for table_name in ("flow_definitions", "run_snapshots"):
        op.add_column(
            table_name,
            sa.Column("environment_version_id", sa.String(length=36), nullable=True),
        )
        op.create_index(
            f"ix_{table_name}_environment_version_id",
            table_name,
            ["environment_version_id"],
        )
        op.create_foreign_key(
            f"fk_{table_name}_environment_version",
            table_name,
            "environment_versions",
            ["environment_version_id"],
            ["id"],
            ondelete="RESTRICT",
        )


def downgrade() -> None:
    for table_name in ("run_snapshots", "flow_definitions"):
        op.drop_constraint(
            f"fk_{table_name}_environment_version", table_name, type_="foreignkey"
        )
        op.drop_index(
            f"ix_{table_name}_environment_version_id", table_name=table_name
        )
        op.drop_column(table_name, "environment_version_id")

    op.drop_column("environment_setup_sessions", "base_image_digest")
    op.drop_column("environment_versions", "base_image_digest")
    op.drop_column("environment_versions", "base_image_reference")
    op.drop_column("terminal_environments", "base_image_digest")
