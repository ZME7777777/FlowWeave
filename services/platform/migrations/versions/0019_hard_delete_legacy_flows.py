"""remove legacy soft-deleted flow definitions without run history

Revision ID: 0019_hard_delete_legacy_flows
Revises: 0018_remove_platform_credentials
"""

from alembic import op

revision = "0019_hard_delete_legacy_flows"
down_revision = "0018_remove_platform_credentials"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Old releases only hid flows. Rows without run history can now be removed
    # safely, which also releases their globally unique names.
    op.execute(
        """
        DELETE FROM flow_definitions AS flow
        WHERE flow.deleted_at IS NOT NULL
          AND NOT EXISTS (
              SELECT 1 FROM flow_runs AS run
              WHERE run.flow_definition_id = flow.id
          )
        """
    )


def downgrade() -> None:
    # Permanently deleted definitions cannot be reconstructed.
    pass
