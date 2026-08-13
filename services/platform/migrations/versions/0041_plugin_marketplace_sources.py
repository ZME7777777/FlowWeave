"""govern immutable OpenHands Marketplace Plugin sources

Revision ID: 0041_plugin_marketplace_sources
Revises: 0040_mcp_oauth_authorizations
"""

import sqlalchemy as sa
from alembic import op

revision = "0041_plugin_marketplace_sources"
down_revision = "0040_mcp_oauth_authorizations"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.drop_constraint(
        "uq_plugin_source_resolution_identity",
        "plugin_source_resolutions",
        type_="unique",
    )
    op.add_column(
        "plugin_source_resolutions",
        sa.Column("source_kind", sa.String(20), nullable=False, server_default="GIT"),
    )
    op.add_column(
        "plugin_source_resolutions",
        sa.Column("marketplace_plugin_name", sa.String(128), nullable=False, server_default=""),
    )
    op.add_column(
        "plugin_source_resolutions",
        sa.Column("resolved_source_url", sa.Text(), nullable=True),
    )
    op.add_column(
        "plugin_source_resolutions",
        sa.Column("resolved_commit", sa.String(40), nullable=True),
    )
    op.add_column(
        "plugin_source_resolutions",
        sa.Column("resolved_repo_path", sa.Text(), nullable=True),
    )
    op.execute(
        sa.text(
            "UPDATE plugin_source_resolutions "
            "SET resolved_source_url = source_url, resolved_commit = requested_commit, "
            "resolved_repo_path = NULLIF(repo_path, '') "
            "WHERE state IN ('READY', 'PUBLISHED')"
        )
    )
    op.create_check_constraint(
        "ck_plugin_source_resolution_kind",
        "plugin_source_resolutions",
        "source_kind IN ('GIT', 'MARKETPLACE')",
    )
    op.create_unique_constraint(
        "uq_plugin_source_resolution_identity",
        "plugin_source_resolutions",
        [
            "source_kind",
            "source_url",
            "requested_commit",
            "repo_path",
            "marketplace_plugin_name",
        ],
    )
    op.alter_column("plugin_source_resolutions", "source_kind", server_default=None)
    op.alter_column("plugin_source_resolutions", "marketplace_plugin_name", server_default=None)


def downgrade() -> None:
    op.execute(sa.text("DELETE FROM plugin_source_resolutions WHERE source_kind = 'MARKETPLACE'"))
    op.drop_constraint(
        "uq_plugin_source_resolution_identity",
        "plugin_source_resolutions",
        type_="unique",
    )
    op.drop_constraint(
        "ck_plugin_source_resolution_kind",
        "plugin_source_resolutions",
        type_="check",
    )
    op.drop_column("plugin_source_resolutions", "resolved_repo_path")
    op.drop_column("plugin_source_resolutions", "resolved_commit")
    op.drop_column("plugin_source_resolutions", "resolved_source_url")
    op.drop_column("plugin_source_resolutions", "marketplace_plugin_name")
    op.drop_column("plugin_source_resolutions", "source_kind")
    op.create_unique_constraint(
        "uq_plugin_source_resolution_identity",
        "plugin_source_resolutions",
        ["source_url", "requested_commit", "repo_path"],
    )
