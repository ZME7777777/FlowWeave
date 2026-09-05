"""share non-Agent business resources between authenticated users

Revision ID: 0096_shared_business_resources
Revises: 0095_user_tenancy
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0096_shared_business_resources"
down_revision = "0095_user_tenancy"
branch_labels = None
depends_on = None

# Ownership columns remain on every table for future tenancy support. Current
# product behavior isolates only the independent Agent product. Tables shared
# by independent Agent and Flow-node sessions stay protected: Flow-node routes
# explicitly enter the shared bypass context, while independent Agent routes do
# not.
_SHARED_TABLES = (
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


def _disable_tenant_policy(table: str) -> None:
    op.execute(sa.text(f'DROP POLICY IF EXISTS "pl_tenant_{table}" ON "{table}"'))
    op.execute(sa.text(f'ALTER TABLE "{table}" NO FORCE ROW LEVEL SECURITY'))
    op.execute(sa.text(f'ALTER TABLE "{table}" DISABLE ROW LEVEL SECURITY'))


def _enable_tenant_policy(table: str) -> None:
    op.execute(sa.text(f'ALTER TABLE "{table}" ENABLE ROW LEVEL SECURITY'))
    op.execute(sa.text(f'ALTER TABLE "{table}" FORCE ROW LEVEL SECURITY'))
    op.execute(
        sa.text(
            f'CREATE POLICY "pl_tenant_{table}" ON "{table}" '
            "USING (current_setting('flowweave.bypass', true) = 'on' OR "
            "owner_user_id = current_setting('flowweave.user_id', true)) "
            "WITH CHECK (current_setting('flowweave.bypass', true) = 'on' OR "
            "owner_user_id = current_setting('flowweave.user_id', true))"
        )
    )


def upgrade() -> None:
    for table in _SHARED_TABLES:
        _disable_tenant_policy(table)


def downgrade() -> None:
    for table in reversed(_SHARED_TABLES):
        _enable_tenant_policy(table)
