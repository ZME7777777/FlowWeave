"""allow deleted node asset names to be reused

Revision ID: 0048_node_asset_name
Revises: 0047_runtime_agent_governance
"""

import sqlalchemy as sa
from alembic import op

revision = "0048_node_asset_name"
down_revision = "0047_runtime_agent_governance"
branch_labels = None
depends_on = None

_OLD_CONSTRAINT = "uq_asset_directory_name"
_ACTIVE_INDEX = "uq_asset_active_directory_name"


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    columns = {column["name"] for column in inspector.get_columns("node_assets")}
    if "deleted_at" not in columns:
        return
    constraints = {
        item["name"] for item in inspector.get_unique_constraints("node_assets") if item.get("name")
    }
    if _OLD_CONSTRAINT in constraints:
        op.drop_constraint(_OLD_CONSTRAINT, "node_assets", type_="unique")

    indexes = {
        item["name"] for item in sa.inspect(bind).get_indexes("node_assets") if item.get("name")
    }
    if _ACTIVE_INDEX not in indexes:
        op.create_index(
            _ACTIVE_INDEX,
            "node_assets",
            ["directory_id", "name"],
            unique=True,
            postgresql_where=sa.text("deleted_at IS NULL"),
            postgresql_nulls_not_distinct=True,
        )


def downgrade() -> None:
    bind = op.get_bind()
    columns = {column["name"] for column in sa.inspect(bind).get_columns("node_assets")}
    if "deleted_at" not in columns:
        return
    indexes = {
        item["name"] for item in sa.inspect(bind).get_indexes("node_assets") if item.get("name")
    }
    if _ACTIVE_INDEX in indexes:
        op.drop_index(_ACTIVE_INDEX, table_name="node_assets")
    op.create_unique_constraint(
        _OLD_CONSTRAINT,
        "node_assets",
        ["directory_id", "name"],
    )
