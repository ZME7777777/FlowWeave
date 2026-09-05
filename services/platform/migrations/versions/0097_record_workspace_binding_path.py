"""Allow record-scoped Runtime workspace roots in conversation bindings.

Revision ID: 0097_record_workspace_binding_path
Revises: 0096_shared_business_resources
"""

from __future__ import annotations

from alembic import op

revision = "0097_record_workspace_binding_path"
down_revision = "0096_shared_business_resources"
branch_labels = None
depends_on = None

_WORKING_DIRECTORY_CHECK = (
    "working_directory IS NULL "
    "OR working_directory = '/runtime/workspace/project' "
    "OR working_directory LIKE '/runtime/workspace/project/%' "
    "OR working_directory ~ '^/runtime/workspace/"
    "[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}"
    "(/.*)?$'"
)


def upgrade() -> None:
    op.drop_constraint(
        "ck_agent_conversation_working_directory",
        "agent_conversation_bindings",
        type_="check",
    )
    op.create_check_constraint(
        "ck_agent_conversation_working_directory",
        "agent_conversation_bindings",
        _WORKING_DIRECTORY_CHECK,
    )


def downgrade() -> None:
    op.drop_constraint(
        "ck_agent_conversation_working_directory",
        "agent_conversation_bindings",
        type_="check",
    )
    op.create_check_constraint(
        "ck_agent_conversation_working_directory",
        "agent_conversation_bindings",
        "working_directory IS NULL OR working_directory = '/runtime/workspace/project' "
        "OR working_directory LIKE '/runtime/workspace/project/%'",
    )
