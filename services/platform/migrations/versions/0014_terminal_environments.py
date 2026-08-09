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
    for table_name in (
        "terminal_environments",
        "environment_versions",
        "environment_setup_sessions",
    ):
        Base.metadata.tables[table_name].create(bind, checkfirst=True)
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
