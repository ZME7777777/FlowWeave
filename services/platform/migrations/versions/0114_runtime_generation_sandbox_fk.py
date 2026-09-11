"""restore Runtime generation Sandbox references as SET NULL foreign keys.

Revision ID: 0114_runtime_sandbox_fk
Revises: 0113_task_retention
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

# Production alembic_version.version_num is VARCHAR(32); keep this ID below
# that durable schema limit so the otherwise transactional migration can mark
# itself applied after the FK repair succeeds.
revision = "0114_runtime_sandbox_fk"
down_revision = "0113_task_retention"
branch_labels = None
depends_on = None


_REFERENCES = (
    ("runtime_generations", "fk_runtime_generations_managed_runtime"),
    (
        "agent_workspace_runtime_generations",
        "fk_agent_workspace_runtime_generations_managed_runtime",
    ),
)


def _managed_runtime_foreign_keys(table_name: str) -> list[str]:
    return [
        str(item["name"])
        for item in sa.inspect(op.get_bind()).get_foreign_keys(table_name)
        if item.get("constrained_columns") == ["managed_runtime_id"]
        and item.get("referred_table") == "managed_sandboxes"
        and item.get("name")
    ]


def _clear_orphaned_references(table_name: str) -> None:
    op.execute(
        sa.text(
            f"UPDATE {table_name} AS generation "
            "SET managed_runtime_id = NULL "
            "WHERE managed_runtime_id IS NOT NULL "
            "AND NOT EXISTS ("
            "SELECT 1 FROM managed_sandboxes AS sandbox "
            "WHERE sandbox.id = generation.managed_runtime_id"
            ")"
        )
    )


def upgrade() -> None:
    for table_name, constraint_name in _REFERENCES:
        _clear_orphaned_references(table_name)
        for existing_name in _managed_runtime_foreign_keys(table_name):
            op.drop_constraint(existing_name, table_name, type_="foreignkey")
        op.create_foreign_key(
            constraint_name,
            table_name,
            "managed_sandboxes",
            ["managed_runtime_id"],
            ["id"],
            ondelete="SET NULL",
        )


def downgrade() -> None:
    for table_name, constraint_name in reversed(_REFERENCES):
        for existing_name in _managed_runtime_foreign_keys(table_name):
            op.drop_constraint(existing_name, table_name, type_="foreignkey")
        # Both predecessor migrations already defined this relationship with
        # SET NULL semantics. Preserve that schema contract when moving back
        # to 0113 instead of silently reintroducing the dangling-reference
        # failure that this repair removes.
        op.create_foreign_key(
            constraint_name,
            table_name,
            "managed_sandboxes",
            ["managed_runtime_id"],
            ["id"],
            ondelete="SET NULL",
        )
