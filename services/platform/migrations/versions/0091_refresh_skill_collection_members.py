"""Refresh Skill collection members to their latest published version.

Revision ID: 0091_refresh_skill_collection_members
Revises: 0090_env_version_desc
"""

import sqlalchemy as sa
from alembic import op

revision = "0091_refresh_skill_collection_members"
down_revision = "0090_env_version_desc"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Collections are mutable selection shortcuts.  They intentionally track
    # the latest published Skill Version, unlike frozen node/session/run
    # consumers.  Remove a legacy duplicate first so the following update
    # remains valid under uq_capability_collection_version.
    op.execute(
        sa.text(
            """
            DELETE FROM capability_collection_items AS stale
            USING capability_versions AS source
            JOIN capability_packages AS package ON package.id = source.package_id
            JOIN capability_versions AS latest
              ON latest.package_id = source.package_id
             AND latest.state = 'PUBLISHED'
             AND latest.version_no = (
                 SELECT max(candidate.version_no)
                 FROM capability_versions AS candidate
                 WHERE candidate.package_id = source.package_id
                   AND candidate.state = 'PUBLISHED'
             )
            JOIN capability_collection_items AS retained
              ON retained.collection_id = stale.collection_id
             AND retained.capability_version_id = latest.id
            WHERE stale.capability_version_id = source.id
              AND source.id <> latest.id
              AND package.capability_type = 'SKILL'
            """
        )
    )
    op.execute(
        sa.text(
            """
            UPDATE capability_collections AS collection
            SET row_version = collection.row_version + 1,
                updated_at = CURRENT_TIMESTAMP
            WHERE EXISTS (
                SELECT 1
                FROM capability_collection_items AS item
                JOIN capability_versions AS source ON source.id = item.capability_version_id
                JOIN capability_packages AS package ON package.id = source.package_id
                WHERE item.collection_id = collection.id
                  AND package.capability_type = 'SKILL'
                  AND source.state = 'PUBLISHED'
                  AND source.version_no < (
                      SELECT max(candidate.version_no)
                      FROM capability_versions AS candidate
                      WHERE candidate.package_id = source.package_id
                        AND candidate.state = 'PUBLISHED'
                  )
            )
            """
        )
    )
    op.execute(
        sa.text(
            """
            UPDATE capability_collection_items AS item
            SET capability_version_id = latest.id
            FROM capability_versions AS source
            JOIN capability_packages AS package ON package.id = source.package_id
            JOIN capability_versions AS latest
              ON latest.package_id = source.package_id
             AND latest.state = 'PUBLISHED'
             AND latest.version_no = (
                 SELECT max(candidate.version_no)
                 FROM capability_versions AS candidate
                 WHERE candidate.package_id = source.package_id
                   AND candidate.state = 'PUBLISHED'
             )
            WHERE item.capability_version_id = source.id
              AND source.id <> latest.id
              AND package.capability_type = 'SKILL'
            """
        )
    )


def downgrade() -> None:
    # Prior version identities cannot be inferred safely after a collection
    # has followed its latest Skill version.
    pass
