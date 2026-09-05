"""add platform users and tenant ownership

Revision ID: 0095_user_tenancy
Revises: 0094_attempt_owned_runtimes
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0095_user_tenancy"
down_revision = "0094_attempt_owned_runtimes"
branch_labels = None
depends_on = None

_FLOWWEAVE_USER_ID = "00000000-0000-0000-0000-000000000001"

_OWNER_UNIQUES = (
    ("flow_definitions", ("name",), "uq_flow_definition_owner_name", ("owner_user_id", "name")),
    ("model_providers", ("name",), "uq_model_provider_owner_name", ("owner_user_id", "name")),
    (
        "terminal_environments",
        ("name",),
        "uq_terminal_environment_owner_name",
        ("owner_user_id", "name"),
    ),
    (
        "capability_collections",
        ("name",),
        "uq_capability_collection_owner_name",
        ("owner_user_id", "name"),
    ),
    (
        "capability_packages",
        ("capability_type", "capability_key"),
        "uq_capability_package_owner_identity",
        ("owner_user_id", "capability_type", "capability_key"),
    ),
    (
        "memory_sources",
        ("source_key",),
        "uq_memory_source_owner_key",
        ("owner_user_id", "source_key"),
    ),
    (
        "memory_sources",
        ("scope", "scope_key", "source_key"),
        "uq_memory_source_owner_scope_identity",
        ("owner_user_id", "scope", "scope_key", "source_key"),
    ),
    (
        "agent_work_directories",
        ("workspace_id", "display_name"),
        "uq_agent_work_directory_owner_workspace_name",
        ("owner_user_id", "workspace_id", "display_name"),
    ),
    (
        "agent_workspace_capabilities",
        ("workspace_id", "capability_version_id"),
        "uq_agent_workspace_capability_owner",
        ("owner_user_id", "workspace_id", "capability_version_id"),
    ),
    (
        "agent_workspace_capabilities",
        ("workspace_id", "position"),
        "uq_agent_workspace_capability_owner_position",
        ("owner_user_id", "workspace_id", "position"),
    ),
    (
        "agent_conversation_bindings",
        ("runtime_session_id", "openhands_conversation_id"),
        "uq_agent_conversation_owner_runtime_id",
        ("owner_user_id", "runtime_session_id", "openhands_conversation_id"),
    ),
    (
        "agent_conversation_bindings",
        ("create_idempotency_key",),
        "uq_agent_conversation_owner_create_key",
        ("owner_user_id", "create_idempotency_key"),
    ),
    (
        "agent_conversation_commands",
        ("idempotency_key",),
        "uq_agent_conversation_command_owner_key",
        ("owner_user_id", "idempotency_key"),
    ),
    (
        "website_credentials",
        ("target_host", "name"),
        "uq_website_credential_owner_host_name",
        ("owner_user_id", "target_host", "name"),
    ),
    (
        "node_assets",
        ("directory_id", "name"),
        "uq_asset_owner_directory_name",
        ("owner_user_id", "directory_id", "name"),
    ),
    (
        "capability_versions",
        ("digest",),
        "uq_capability_version_owner_digest",
        ("owner_user_id", "digest"),
    ),
    (
        "plugin_source_resolutions",
        ("source_kind", "source_url", "requested_commit", "repo_path", "marketplace_plugin_name"),
        "uq_plugin_source_resolution_owner_identity",
        (
            "owner_user_id",
            "source_kind",
            "source_url",
            "requested_commit",
            "repo_path",
            "marketplace_plugin_name",
        ),
    ),
    (
        "human_actions",
        ("idempotency_key",),
        "uq_human_action_owner_key",
        ("owner_user_id", "idempotency_key"),
    ),
    (
        "runtime_confirmation_approvals",
        ("decision_idempotency_key",),
        "uq_runtime_confirmation_owner_decision_key",
        ("owner_user_id", "decision_idempotency_key"),
    ),
)

_LEGACY_UNIQUE_NAMES = {
    ("flow_definitions", ("name",)): "flow_definitions_name_key",
    ("model_providers", ("name",)): "model_providers_name_key",
    ("terminal_environments", ("name",)): "terminal_environments_name_key",
    ("capability_collections", ("name",)): "uq_capability_collections_name",
    (
        "capability_packages",
        ("capability_type", "capability_key"),
    ): "uq_capability_package_identity",
    ("memory_sources", ("source_key",)): "memory_sources_source_key_key",
    ("memory_sources", ("scope", "scope_key", "source_key")): "uq_memory_source_scope_identity",
    (
        "agent_work_directories",
        ("workspace_id", "display_name"),
    ): "uq_agent_work_directory_workspace_name",
    (
        "agent_workspace_capabilities",
        ("workspace_id", "capability_version_id"),
    ): "uq_agent_workspace_capability",
    (
        "agent_workspace_capabilities",
        ("workspace_id", "position"),
    ): "uq_agent_workspace_capability_position",
    (
        "agent_conversation_bindings",
        ("runtime_session_id", "openhands_conversation_id"),
    ): "uq_agent_conversation_runtime_id",
    (
        "agent_conversation_bindings",
        ("create_idempotency_key",),
    ): "uq_agent_conversation_create_key",
    ("agent_conversation_commands", ("idempotency_key",)): "uq_agent_conversation_command_key",
    ("website_credentials", ("target_host", "name")): "uq_website_credential_host_name",
    ("node_assets", ("directory_id", "name")): "uq_asset_directory_name",
    ("capability_versions", ("digest",)): "capability_versions_digest_key",
    (
        "plugin_source_resolutions",
        ("source_kind", "source_url", "requested_commit", "repo_path", "marketplace_plugin_name"),
    ): "uq_plugin_source_resolution_identity",
    ("human_actions", ("idempotency_key",)): "human_actions_idempotency_key_key",
    (
        "runtime_confirmation_approvals",
        ("decision_idempotency_key",),
    ): "runtime_confirmation_approvals_decision_idempotency_key_key",
}

# Existing user-owned tables. Shared control-plane and authentication tables
# are deliberately absent from this list.
_TENANT_TABLES = (
    "agent_conversation_bindings",
    "agent_conversation_capabilities",
    "agent_conversation_commands",
    "agent_conversation_message_attachments",
    "agent_work_directories",
    "agent_work_directory_paths",
    "agent_work_directory_versions",
    "agent_workspace_capabilities",
    "artifact_versions",
    "attempt_input_bindings",
    "capability_collection_items",
    "capability_collections",
    "capability_dependencies",
    "capability_imports",
    "capability_packages",
    "capability_validations",
    "capability_versions",
    "environment_setup_sessions",
    "environment_versions",
    "flow_definitions",
    "flow_edges",
    "flow_nodes",
    "flow_port_mappings",
    "flow_run_runtime_allocations",
    "flow_run_runtime_secret_references",
    "flow_run_runtimes",
    "flow_run_schedule_occurrences",
    "flow_run_schedules",
    "flow_runs",
    "gate_evaluations",
    "gate_policies",
    "human_actions",
    "mcp_oauth_authorizations",
    "mcp_oauth_secret_audits",
    "mcp_oauth_secret_references",
    "memory_source_version_references",
    "memory_source_versions",
    "memory_sources",
    "model_providers",
    "node_assets",
    "node_attempts",
    "node_context_capabilities",
    "node_directories",
    "node_executor_configs",
    "node_io_fields",
    "node_runs",
    "plugin_source_resolutions",
    "provider_models",
    "run_events",
    "run_snapshots",
    "runtime_confirmation_approvals",
    "runtime_generations",
    "terminal_environments",
    "website_credentials",
)


def _enable_tenant_policy(table: str) -> None:
    policy = f"pl_tenant_{table}"
    op.execute(sa.text(f'ALTER TABLE "{table}" ENABLE ROW LEVEL SECURITY'))
    op.execute(sa.text(f'ALTER TABLE "{table}" FORCE ROW LEVEL SECURITY'))
    op.execute(
        sa.text(
            f'CREATE POLICY "{policy}" ON "{table}" '
            "USING (current_setting('flowweave.bypass', true) = 'on' OR "
            "owner_user_id = current_setting('flowweave.user_id', true)) "
            "WITH CHECK (current_setting('flowweave.bypass', true) = 'on' OR "
            "owner_user_id = current_setting('flowweave.user_id', true))"
        )
    )


def _disable_tenant_policy(table: str) -> None:
    policy = f"pl_tenant_{table}"
    op.execute(sa.text(f'DROP POLICY IF EXISTS "{policy}" ON "{table}"'))
    op.execute(sa.text(f'ALTER TABLE "{table}" NO FORCE ROW LEVEL SECURITY'))
    op.execute(sa.text(f'ALTER TABLE "{table}" DISABLE ROW LEVEL SECURITY'))


def _add_owner(table: str) -> None:
    op.add_column(table, sa.Column("owner_user_id", sa.String(length=36), nullable=True))
    op.execute(
        sa.text(
            f'UPDATE "{table}" SET owner_user_id = :owner WHERE owner_user_id IS NULL'
        ).bindparams(owner=_FLOWWEAVE_USER_ID)
    )
    op.alter_column(table, "owner_user_id", nullable=False)
    op.create_index(f"ix_{table}_owner_user_id", table, ["owner_user_id"])
    _enable_tenant_policy(table)


def _scope_business_uniques() -> None:
    for table, old_columns, new_name, new_columns in _OWNER_UNIQUES:
        op.drop_constraint(
            _LEGACY_UNIQUE_NAMES[(table, old_columns)], table, type_="unique"
        )
        options = {"postgresql_nulls_not_distinct": True} if table == "node_assets" else {}
        op.create_unique_constraint(new_name, table, list(new_columns), **options)


def _restore_business_uniques() -> None:
    for table, old_columns, new_name, _new_columns in reversed(_OWNER_UNIQUES):
        op.drop_constraint(new_name, table, type_="unique")
        options = {"postgresql_nulls_not_distinct": True} if table == "node_assets" else {}
        op.create_unique_constraint(
            _LEGACY_UNIQUE_NAMES[(table, old_columns)], table, list(old_columns), **options
        )


def upgrade() -> None:
    op.create_table(
        "users",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("username", sa.String(length=80), nullable=False),
        sa.Column("password_hash", sa.String(length=300), nullable=False),
        sa.Column("role", sa.String(length=20), nullable=False),
        sa.Column("is_active", sa.Boolean(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint("role IN ('SUPER_ADMIN', 'USER')", name="ck_user_role"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("username"),
    )
    op.create_index("ix_users_username", "users", ["username"])
    op.create_index("ix_users_role", "users", ["role"])
    op.create_table(
        "user_sessions",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("user_id", sa.String(length=36), nullable=False),
        sa.Column("token_digest", sa.String(length=64), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("last_seen_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("token_digest"),
    )
    op.create_index("ix_user_sessions_user_id", "user_sessions", ["user_id"])
    op.create_index("ix_user_sessions_token_digest", "user_sessions", ["token_digest"])
    op.create_index("ix_user_sessions_expires_at", "user_sessions", ["expires_at"])
    op.create_table(
        "user_operation_logs",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("user_id", sa.String(length=36), nullable=False),
        sa.Column("username", sa.String(length=80), nullable=False),
        sa.Column("request_id", sa.String(length=80), nullable=False),
        sa.Column("method", sa.String(length=10), nullable=False),
        sa.Column("route", sa.String(length=500), nullable=False),
        sa.Column("status_code", sa.Integer(), nullable=False),
        sa.Column("duration_ms", sa.Integer(), nullable=False),
        sa.Column("client_ip", sa.String(length=80), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )
    for column in ("user_id", "username", "request_id", "route", "created_at"):
        op.create_index(f"ix_user_operation_logs_{column}", "user_operation_logs", [column])

    for table in _TENANT_TABLES:
        _add_owner(table)
    _scope_business_uniques()

    # The delivery ledger is globally scanned by workers, but each claimed task
    # restores this owner before its aggregate handler executes.
    op.add_column(
        "background_tasks", sa.Column("owner_user_id", sa.String(length=36), nullable=True)
    )
    op.execute(
        sa.text(
            "UPDATE background_tasks SET owner_user_id = :owner WHERE owner_user_id IS NULL"
        ).bindparams(owner=_FLOWWEAVE_USER_ID)
    )
    op.alter_column("background_tasks", "owner_user_id", nullable=False)
    op.create_index("ix_background_tasks_owner_user_id", "background_tasks", ["owner_user_id"])
    op.drop_constraint("background_tasks_idempotency_key_key", "background_tasks", type_="unique")
    op.create_unique_constraint(
        "uq_background_task_owner_key",
        "background_tasks",
        ["owner_user_id", "idempotency_key"],
    )

    op.create_table(
        "agent_workspace_preferences",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("workspace_id", sa.String(length=36), nullable=False),
        sa.Column("default_model_provider_id", sa.String(length=36), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("owner_user_id", sa.String(length=36), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "owner_user_id", "workspace_id", name="uq_agent_workspace_preference_owner"
        ),
    )
    op.create_index(
        "ix_agent_workspace_preferences_workspace_id",
        "agent_workspace_preferences",
        ["workspace_id"],
    )
    op.create_index(
        "ix_agent_workspace_preferences_default_model_provider_id",
        "agent_workspace_preferences",
        ["default_model_provider_id"],
    )
    op.create_index(
        "ix_agent_workspace_preferences_owner_user_id",
        "agent_workspace_preferences",
        ["owner_user_id"],
    )
    _enable_tenant_policy("agent_workspace_preferences")


def downgrade() -> None:
    _disable_tenant_policy("agent_workspace_preferences")
    op.drop_table("agent_workspace_preferences")

    op.drop_constraint("uq_background_task_owner_key", "background_tasks", type_="unique")
    op.create_unique_constraint(
        "background_tasks_idempotency_key_key", "background_tasks", ["idempotency_key"]
    )
    op.drop_index("ix_background_tasks_owner_user_id", table_name="background_tasks")
    op.drop_column("background_tasks", "owner_user_id")

    _restore_business_uniques()
    for table in reversed(_TENANT_TABLES):
        _disable_tenant_policy(table)
        op.drop_index(f"ix_{table}_owner_user_id", table_name=table)
        op.drop_column(table, "owner_user_id")

    op.drop_table("user_operation_logs")
    op.drop_table("user_sessions")
    op.drop_table("users")
