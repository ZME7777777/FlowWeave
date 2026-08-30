"""Remove retired Agent Tool Policy capability records.

Revision ID: 0076_remove_tool_policy
Revises: 0075_shared_agent_runtime_config
Create Date: 2026-08-30
"""

import sqlalchemy as sa
from alembic import op

revision = "0076_remove_tool_policy"
down_revision = "0075_shared_agent_runtime_config"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    version_ids = sa.text(
        "SELECT v.id FROM capability_versions v "
        "JOIN capability_packages p ON p.id = v.package_id "
        "WHERE p.capability_type = 'TOOL_POLICY'"
    )
    tables = set(sa.inspect(bind).get_table_names())
    if "node_capability_refs" in tables:
        bind.execute(
            sa.text("DELETE FROM node_capability_refs WHERE capability_type = 'TOOL_POLICY'")
        )
    if "agent_workspace_capabilities" in tables:
        bind.execute(
            sa.text(
                "DELETE FROM agent_workspace_capabilities WHERE capability_type = 'TOOL_POLICY'"
            )
        )
    if "capability_collection_members" in tables:
        bind.execute(
            sa.text(
                "DELETE FROM capability_collection_members WHERE capability_version_id IN ("
                + version_ids.text
                + ")"
            )
        )
    bind.execute(
        sa.text(
            "DELETE FROM capability_validations WHERE capability_version_id IN ("
            + version_ids.text
            + ")"
        )
    )
    if "capability_dependencies" in tables:
        bind.execute(
            sa.text(
                "DELETE FROM capability_dependencies WHERE capability_version_id IN ("
                + version_ids.text
                + ")"
            )
        )
    bind.execute(sa.text("DELETE FROM capability_versions WHERE id IN (" + version_ids.text + ")"))
    bind.execute(sa.text("DELETE FROM capability_packages WHERE capability_type = 'TOOL_POLICY'"))
    bind.execute(sa.text("DELETE FROM capability_imports WHERE capability_type = 'TOOL_POLICY'"))


def downgrade() -> None:
    # Deleted policies cannot be recreated without guessing their frozen data.
    # Historical Snapshots remain intentionally read-only and require rerun.
    pass
