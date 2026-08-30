"""Catalog baseline frozen at revision 0001.

Historical migrations must not import the live ORM: later revisions remove
node-owned Agent configuration, while a fresh database still needs those
legacy tables until revision 0075 migrates them away.
"""

import sqlalchemy as sa
from alembic import op

revision = "0001_catalog"
down_revision = None
branch_labels = None
depends_on = None

metadata = sa.MetaData()

node_directories = sa.Table(
    "node_directories",
    metadata,
    sa.Column("id", sa.String(36), primary_key=True),
    sa.Column(
        "parent_id",
        sa.String(36),
        sa.ForeignKey("node_directories.id", ondelete="RESTRICT"),
        nullable=True,
    ),
    sa.Column("name", sa.String(160), nullable=False),
    sa.Column("position", sa.Integer(), nullable=False),
    sa.Column("row_version", sa.Integer(), nullable=False),
    sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    sa.UniqueConstraint("parent_id", "name", name="uq_directory_parent_name"),
)
sa.Index("ix_node_directories_parent_id", node_directories.c.parent_id)

model_providers = sa.Table(
    "model_providers",
    metadata,
    sa.Column("id", sa.String(36), primary_key=True),
    sa.Column("name", sa.String(200), nullable=False, unique=True),
    sa.Column("base_url", sa.Text(), nullable=False),
    sa.Column("auth_type", sa.String(30), nullable=False),
    sa.Column("encrypted_api_key", sa.LargeBinary(), nullable=True),
    sa.Column("api_key_hint", sa.String(20), nullable=True),
    sa.Column("encrypted_oauth_access_token", sa.LargeBinary(), nullable=True),
    sa.Column("encrypted_oauth_refresh_token", sa.LargeBinary(), nullable=True),
    sa.Column("oauth_access_expires_at", sa.DateTime(timezone=True), nullable=True),
    sa.Column("oauth_account_id", sa.String(240), nullable=True),
    sa.Column("oauth_email", sa.String(320), nullable=True),
    sa.Column("encrypted_oauth_device_auth_id", sa.LargeBinary(), nullable=True),
    sa.Column("oauth_user_code", sa.String(80), nullable=True),
    sa.Column("oauth_device_expires_at", sa.DateTime(timezone=True), nullable=True),
    sa.Column("oauth_poll_interval", sa.Integer(), nullable=True),
    sa.Column("connection_state", sa.String(30), nullable=False),
    sa.Column("row_version", sa.Integer(), nullable=False),
    sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
)

provider_models = sa.Table(
    "provider_models",
    metadata,
    sa.Column("id", sa.String(36), primary_key=True),
    sa.Column(
        "provider_id",
        sa.String(36),
        sa.ForeignKey("model_providers.id", ondelete="CASCADE"),
        nullable=False,
    ),
    sa.Column("model_name", sa.String(240), nullable=False),
    sa.Column("enabled", sa.Boolean(), nullable=False),
    sa.Column("is_default", sa.Boolean(), nullable=False),
    sa.Column("default_reasoning_effort", sa.String(30), nullable=True),
    sa.Column("supported_reasoning_efforts", sa.JSON(), nullable=False),
    sa.Column("discovered_at", sa.DateTime(timezone=True), nullable=False),
    sa.UniqueConstraint("provider_id", "model_name", name="uq_provider_model_name"),
)
sa.Index("ix_provider_models_provider_id", provider_models.c.provider_id)
sa.Index(
    "uq_provider_default_model",
    provider_models.c.provider_id,
    unique=True,
    postgresql_where=provider_models.c.is_default.is_(True),
)

node_assets = sa.Table(
    "node_assets",
    metadata,
    sa.Column("id", sa.String(36), primary_key=True),
    sa.Column(
        "directory_id",
        sa.String(36),
        sa.ForeignKey("node_directories.id", ondelete="RESTRICT"),
        nullable=True,
    ),
    sa.Column("name", sa.String(200), nullable=False),
    sa.Column("description", sa.Text(), nullable=False),
    sa.Column("icon_kind", sa.String(30), nullable=False),
    sa.Column("icon_value", sa.String(80), nullable=False),
    sa.Column("row_version", sa.Integer(), nullable=False),
    sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    sa.UniqueConstraint(
        "directory_id",
        "name",
        name="uq_asset_directory_name",
        postgresql_nulls_not_distinct=True,
    ),
)
sa.Index("ix_node_assets_directory_id", node_assets.c.directory_id)

node_io_fields = sa.Table(
    "node_io_fields",
    metadata,
    sa.Column("id", sa.String(36), primary_key=True),
    sa.Column(
        "node_asset_id",
        sa.String(36),
        sa.ForeignKey("node_assets.id", ondelete="CASCADE"),
        nullable=False,
    ),
    sa.Column("direction", sa.String(10), nullable=False),
    sa.Column("field_key", sa.String(100), nullable=False),
    sa.Column("display_name", sa.String(160), nullable=False),
    sa.Column("data_type", sa.String(80), nullable=False),
    sa.Column("description", sa.Text(), nullable=False),
    sa.Column("template_url", sa.Text(), nullable=False),
    sa.Column("position", sa.Integer(), nullable=False),
    sa.UniqueConstraint("node_asset_id", "direction", "field_key", name="uq_asset_direction_field"),
    sa.CheckConstraint("position >= 0", name="ck_io_position_nonnegative"),
)
sa.Index("ix_node_io_fields_node_asset_id", node_io_fields.c.node_asset_id)

node_executor_configs = sa.Table(
    "node_executor_configs",
    metadata,
    sa.Column(
        "node_asset_id",
        sa.String(36),
        sa.ForeignKey("node_assets.id", ondelete="CASCADE"),
        primary_key=True,
    ),
    sa.Column(
        "model_provider_id",
        sa.String(36),
        sa.ForeignKey("model_providers.id", ondelete="RESTRICT"),
        nullable=True,
    ),
    sa.Column("model_name", sa.String(200), nullable=True),
    sa.Column("startup_prompt", sa.Text(), nullable=False),
    sa.Column("context_prompt", sa.Text(), nullable=False),
    sa.Column("timeout_seconds", sa.Integer(), nullable=False),
    sa.Column("max_iterations", sa.Integer(), nullable=False),
    sa.Column("confirmation_policy", sa.String(20), nullable=False),
    sa.Column("condenser_config_json", sa.JSON(), nullable=False),
    sa.CheckConstraint("timeout_seconds > 0", name="ck_executor_timeout_positive"),
    sa.CheckConstraint("max_iterations > 0", name="ck_executor_iterations_positive"),
    sa.CheckConstraint(
        "confirmation_policy IN ('ALWAYS', 'NEVER')",
        name="ck_executor_confirmation_policy",
    ),
)

node_capability_refs = sa.Table(
    "node_capability_refs",
    metadata,
    sa.Column("id", sa.String(36), primary_key=True),
    sa.Column(
        "node_asset_id",
        sa.String(36),
        sa.ForeignKey("node_assets.id", ondelete="CASCADE"),
        nullable=False,
    ),
    sa.Column("capability_type", sa.String(16), nullable=False),
    sa.Column("capability_key", sa.String(200), nullable=False),
    # The capability repository and FK are introduced by revision 0029.
    sa.Column("capability_version_id", sa.String(36), nullable=False),
    sa.Column("normalized_config", sa.JSON(), nullable=False),
    sa.Column("position", sa.Integer(), nullable=False),
    sa.UniqueConstraint(
        "node_asset_id", "capability_type", "capability_key", name="uq_asset_capability"
    ),
)
sa.Index("ix_node_capability_refs_node_asset_id", node_capability_refs.c.node_asset_id)
sa.Index(
    "ix_node_capability_refs_capability_version_id",
    node_capability_refs.c.capability_version_id,
)

TABLES = (
    node_directories,
    model_providers,
    provider_models,
    node_assets,
    node_io_fields,
    node_executor_configs,
    node_capability_refs,
)


def upgrade() -> None:
    bind = op.get_bind()
    for table in TABLES:
        table.create(bind, checkfirst=True)


def downgrade() -> None:
    bind = op.get_bind()
    for table in reversed(TABLES):
        table.drop(bind, checkfirst=True)
