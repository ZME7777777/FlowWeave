"""add managed sandbox resource ledger"""

import sqlalchemy as sa
from alembic import op

from flowweave.modules.sandboxes.infrastructure import models as sandbox_models  # noqa: F401
from flowweave.shared.database import Base

revision = "0017_managed_sandboxes"
down_revision = "0016_env_version_delete"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    Base.metadata.tables["managed_sandboxes"].create(bind, checkfirst=True)
    columns = {
        column["name"] for column in sa.inspect(bind).get_columns("environment_setup_sessions")
    }
    if "sandbox_id" not in columns:
        op.add_column(
            "environment_setup_sessions",
            sa.Column("sandbox_id", sa.String(length=36), nullable=True),
        )
        op.create_index(
            "ix_environment_setup_sessions_sandbox_id",
            "environment_setup_sessions",
            ["sandbox_id"],
            unique=True,
        )
        op.create_foreign_key(
            "fk_environment_setup_sessions_sandbox",
            "environment_setup_sessions",
            "managed_sandboxes",
            ["sandbox_id"],
            ["id"],
            ondelete="SET NULL",
        )
    columns = {
        column["name"] for column in sa.inspect(bind).get_columns("environment_setup_sessions")
    }
    if "published_version_id" not in columns:
        op.add_column(
            "environment_setup_sessions",
            sa.Column("published_version_id", sa.String(length=36), nullable=True),
        )
        op.create_index(
            "ix_environment_setup_sessions_published_version_id",
            "environment_setup_sessions",
            ["published_version_id"],
            unique=True,
        )
        op.create_foreign_key(
            "fk_environment_setup_sessions_published_version",
            "environment_setup_sessions",
            "environment_versions",
            ["published_version_id"],
            ["id"],
            ondelete="SET NULL",
        )
    for table_name in ("node_attempts", "agent_conversations"):
        inspector = sa.inspect(bind)
        columns = {column["name"] for column in inspector.get_columns(table_name)}
        if "runtime_sandbox_id" not in columns:
            op.add_column(
                table_name,
                sa.Column("runtime_sandbox_id", sa.String(length=36), nullable=True),
            )
        indexes = {index["name"] for index in sa.inspect(bind).get_indexes(table_name)}
        index_name = f"ix_{table_name}_runtime_sandbox_id"
        if index_name not in indexes:
            op.create_index(
                index_name,
                table_name,
                ["runtime_sandbox_id"],
            )
        foreign_keys = {
            foreign_key["name"] for foreign_key in sa.inspect(bind).get_foreign_keys(table_name)
        }
        foreign_key_name = f"fk_{table_name}_runtime_sandbox"
        if foreign_key_name not in foreign_keys:
            op.create_foreign_key(
                foreign_key_name,
                table_name,
                "managed_sandboxes",
                ["runtime_sandbox_id"],
                ["id"],
                ondelete="SET NULL",
            )


def downgrade() -> None:
    bind = op.get_bind()
    for table_name in ("agent_conversations", "node_attempts"):
        columns = {column["name"] for column in sa.inspect(bind).get_columns(table_name)}
        if "runtime_sandbox_id" in columns:
            op.drop_constraint(f"fk_{table_name}_runtime_sandbox", table_name, type_="foreignkey")
            op.drop_index(f"ix_{table_name}_runtime_sandbox_id", table_name=table_name)
            op.drop_column(table_name, "runtime_sandbox_id")
    columns = {
        column["name"] for column in sa.inspect(bind).get_columns("environment_setup_sessions")
    }
    if "published_version_id" in columns:
        op.drop_constraint(
            "fk_environment_setup_sessions_published_version",
            "environment_setup_sessions",
            type_="foreignkey",
        )
        op.drop_index(
            "ix_environment_setup_sessions_published_version_id",
            table_name="environment_setup_sessions",
        )
        op.drop_column("environment_setup_sessions", "published_version_id")
    columns = {
        column["name"] for column in sa.inspect(bind).get_columns("environment_setup_sessions")
    }
    if "sandbox_id" in columns:
        op.drop_constraint(
            "fk_environment_setup_sessions_sandbox",
            "environment_setup_sessions",
            type_="foreignkey",
        )
        op.drop_index(
            "ix_environment_setup_sessions_sandbox_id",
            table_name="environment_setup_sessions",
        )
        op.drop_column("environment_setup_sessions", "sandbox_id")
    Base.metadata.tables["managed_sandboxes"].drop(bind, checkfirst=True)
