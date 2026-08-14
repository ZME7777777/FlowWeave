"""remove obsolete node-level terminal environment binding

Revision ID: 0050_remove_node_environment
Revises: 0049_context_policy_identity
"""

import sqlalchemy as sa
from alembic import op

revision = "0050_remove_node_environment"
down_revision = "0049_context_policy_identity"
branch_labels = None
depends_on = None

_TABLE = "node_assets"
_COLUMN = "environment_version_id"
_FOREIGN_KEY = "fk_node_assets_environment_version"
_INDEX = "ix_node_assets_environment_version_id"


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    columns = {column["name"] for column in inspector.get_columns(_TABLE)}
    if _COLUMN not in columns:
        return

    foreign_keys = {
        foreign_key["name"]
        for foreign_key in inspector.get_foreign_keys(_TABLE)
        if foreign_key.get("name")
    }
    if _FOREIGN_KEY in foreign_keys:
        op.drop_constraint(_FOREIGN_KEY, _TABLE, type_="foreignkey")

    indexes = {index["name"] for index in sa.inspect(bind).get_indexes(_TABLE) if index.get("name")}
    if _INDEX in indexes:
        op.drop_index(_INDEX, table_name=_TABLE)

    op.drop_column(_TABLE, _COLUMN)


def downgrade() -> None:
    bind = op.get_bind()
    columns = {column["name"] for column in sa.inspect(bind).get_columns(_TABLE)}
    if _COLUMN not in columns:
        op.add_column(_TABLE, sa.Column(_COLUMN, sa.String(length=36), nullable=True))
    indexes = {index["name"] for index in sa.inspect(bind).get_indexes(_TABLE) if index.get("name")}
    if _INDEX not in indexes:
        op.create_index(_INDEX, _TABLE, [_COLUMN])
    foreign_keys = {
        foreign_key["name"]
        for foreign_key in sa.inspect(bind).get_foreign_keys(_TABLE)
        if foreign_key.get("name")
    }
    if _FOREIGN_KEY not in foreign_keys:
        op.create_foreign_key(
            _FOREIGN_KEY,
            _TABLE,
            "environment_versions",
            [_COLUMN],
            ["id"],
            ondelete="RESTRICT",
        )
