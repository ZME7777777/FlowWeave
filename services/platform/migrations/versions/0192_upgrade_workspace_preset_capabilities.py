"""Advance mutable workspace presets to available capability versions.

Revision ID: 0192_upgrade_workspace_preset_capabilities
Revises: 0191_case_cancel_stepwise_attempt
"""

from alembic import op

revision = "0192_upgrade_workspace_preset_capabilities"
down_revision = "0191_case_cancel_stepwise_attempt"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        UPDATE agent_work_directories AS directory
        SET session_capability_ids_json = COALESCE(
            (
                SELECT jsonb_agg(candidate.capability_version_id ORDER BY candidate.position)
                FROM (
                    SELECT resolved.capability_version_id, MIN(resolved.position) AS position
                    FROM (
                        SELECT
                            COALESCE(replacement.id, stale.id) AS capability_version_id,
                            configured.ordinality AS position
                        FROM jsonb_array_elements_text(
                            COALESCE(directory.session_capability_ids_json::jsonb, '[]'::jsonb)
                        ) WITH ORDINALITY AS configured(id, ordinality)
                        JOIN capability_versions AS stale ON stale.id = configured.id
                        LEFT JOIN LATERAL (
                            SELECT current.id
                            FROM capability_versions AS current
                            WHERE current.package_id = stale.package_id
                              AND current.state = 'PUBLISHED'
                              AND stale.state <> 'PUBLISHED'
                            ORDER BY current.version_no DESC
                            LIMIT 1
                        ) AS replacement ON true
                    ) AS resolved
                    GROUP BY resolved.capability_version_id
                ) AS candidate
            ),
            '[]'::jsonb
        )
        WHERE EXISTS (
            SELECT 1
            FROM jsonb_array_elements_text(
                COALESCE(directory.session_capability_ids_json::jsonb, '[]'::jsonb)
            ) AS configured(id)
            LEFT JOIN capability_versions AS version ON version.id = configured.id
            WHERE version.id IS NULL OR version.state <> 'PUBLISHED'
        )
        """
    )


def downgrade() -> None:
    pass
