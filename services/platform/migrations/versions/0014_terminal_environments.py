"""add terminal environments and node environment bindings"""

import sqlalchemy as sa
from alembic import op

from flowweave.modules.environments.infrastructure import models as environment_models  # noqa: F401
from flowweave.shared.database import Base

revision = "0014_terminal_environments"
down_revision = "0013_lazy_lark_run_resources"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    for table_name in ("terminal_environments", "environment_versions"):
        Base.metadata.tables[table_name].create(bind, checkfirst=True)
    # Keep historical migrations independent from future ORM columns. In
    # particular, ``sandbox_id`` is introduced by 0017 after the referenced
    # managed_sandboxes table exists. Using today's full ORM table here would
    # make a clean upgrade try to create that future foreign key in 0014.
    if "environment_setup_sessions" not in sa.inspect(bind).get_table_names():
        op.create_table(
            "environment_setup_sessions",
            sa.Column("id", sa.String(length=36), nullable=False),
            sa.Column("environment_id", sa.String(length=36), nullable=False),
            sa.Column("base_version_id", sa.String(length=36), nullable=True),
            sa.Column("state", sa.String(length=30), nullable=False),
            sa.Column("container_id", sa.String(length=100), nullable=False),
            sa.Column("base_image_reference", sa.String(length=500), nullable=False),
            sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("error_detail", sa.Text(), nullable=True),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
            sa.ForeignKeyConstraint(
                ["environment_id"], ["terminal_environments.id"], ondelete="CASCADE"
            ),
            sa.ForeignKeyConstraint(
                ["base_version_id"], ["environment_versions.id"], ondelete="RESTRICT"
            ),
            sa.PrimaryKeyConstraint("id"),
        )
        op.create_index(
            "ix_environment_setup_sessions_environment_id",
            "environment_setup_sessions",
            ["environment_id"],
        )
        op.create_index(
            "ix_environment_setup_sessions_state",
            "environment_setup_sessions",
            ["state"],
        )
        op.create_index(
            "ix_environment_setup_sessions_expires_at",
            "environment_setup_sessions",
            ["expires_at"],
        )
    inspector = sa.inspect(bind)
    columns = {column["name"] for column in inspector.get_columns("node_assets")}
    if "environment_version_id" not in columns:
        op.add_column(
            "node_assets",
            sa.Column("environment_version_id", sa.String(length=36), nullable=True),
        )
    indexes = {index["name"] for index in sa.inspect(bind).get_indexes("node_assets")}
    if "ix_node_assets_environment_version_id" not in indexes:
        op.create_index(
            "ix_node_assets_environment_version_id",
            "node_assets",
            ["environment_version_id"],
        )
    foreign_keys = {
        foreign_key["name"] for foreign_key in sa.inspect(bind).get_foreign_keys("node_assets")
    }
    if "fk_node_assets_environment_version" not in foreign_keys:
        op.create_foreign_key(
            "fk_node_assets_environment_version",
            "node_assets",
            "environment_versions",
            ["environment_version_id"],
            ["id"],
            ondelete="RESTRICT",
        )


def downgrade() -> None:
    bind = op.get_bind()
    foreign_keys = {
        foreign_key["name"] for foreign_key in sa.inspect(bind).get_foreign_keys("node_assets")
    }
    if "fk_node_assets_environment_version" in foreign_keys:
        op.drop_constraint("fk_node_assets_environment_version", "node_assets", type_="foreignkey")
    indexes = {index["name"] for index in sa.inspect(bind).get_indexes("node_assets")}
    if "ix_node_assets_environment_version_id" in indexes:
        op.drop_index("ix_node_assets_environment_version_id", table_name="node_assets")
    columns = {column["name"] for column in sa.inspect(bind).get_columns("node_assets")}
    if "environment_version_id" in columns:
        op.drop_column("node_assets", "environment_version_id")
    op.drop_table("environment_setup_sessions")
    op.drop_table("environment_versions")
    op.drop_table("terminal_environments")
