"""add managed sandbox resource ledger"""

import sqlalchemy as sa
from alembic import op

revision = "0017_managed_sandboxes"
down_revision = "0016_env_version_delete"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    # Freeze the 0017 shape. FR-02 adds runtime_allocation_id only after the
    # referenced allocation table is created in 0053.
    op.create_table(
        "managed_sandboxes",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("kind", sa.String(40), nullable=False),
        sa.Column("owner_type", sa.String(40), nullable=False),
        sa.Column("owner_id", sa.String(100), nullable=False),
        sa.Column("backend", sa.String(30), nullable=False),
        sa.Column("backend_resource_id", sa.String(100), nullable=False),
        sa.Column("backend_resource_name", sa.String(100), nullable=False),
        sa.Column("desired_state", sa.String(20), nullable=False),
        sa.Column("observed_state", sa.String(20), nullable=False),
        sa.Column("generation", sa.Integer(), nullable=False),
        sa.Column("image_reference", sa.String(500), nullable=False),
        sa.Column("spec_json", sa.JSON(), nullable=False),
        sa.Column("last_activity_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("idle_expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("hard_expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("cleanup_attempts", sa.Integer(), nullable=False),
        sa.Column("next_reconcile_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("last_error_code", sa.String(100), nullable=True),
        sa.Column("last_error_detail", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint(
            "backend",
            "backend_resource_name",
            name="uq_sandbox_backend_name",
        ),
        sa.UniqueConstraint(
            "kind",
            "owner_type",
            "owner_id",
            "generation",
            name="uq_sandbox_owner_generation",
        ),
    )
    for column_name in (
        "kind",
        "owner_type",
        "owner_id",
        "desired_state",
        "observed_state",
        "idle_expires_at",
        "hard_expires_at",
        "next_reconcile_at",
    ):
        op.create_index(
            f"ix_managed_sandboxes_{column_name}",
            "managed_sandboxes",
            [column_name],
        )
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
    op.drop_table("managed_sandboxes")
