"""add immutable remote Plugin source resolution workflows

Revision ID: 0033_plugin_source_resolutions
Revises: 0032_context_policy_runtime_spec
"""

import sqlalchemy as sa
from alembic import op

revision = "0033_plugin_source_resolutions"
down_revision = "0032_context_policy_runtime_spec"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "plugin_source_resolutions",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("source_url", sa.Text(), nullable=False),
        sa.Column("requested_commit", sa.String(40), nullable=False),
        sa.Column("repo_path", sa.Text(), nullable=False),
        sa.Column("state", sa.String(20), nullable=False),
        sa.Column("state_version", sa.Integer(), nullable=False),
        sa.Column("content_hash", sa.String(64), nullable=True),
        sa.Column("storage_key", sa.Text(), nullable=True),
        sa.Column("byte_size", sa.Integer(), nullable=True),
        sa.Column("preview_json", sa.JSON(), nullable=False),
        sa.Column("resolver_report_json", sa.JSON(), nullable=False),
        sa.Column("error_detail", sa.Text(), nullable=True),
        sa.Column(
            "capability_version_id",
            sa.String(36),
            sa.ForeignKey("capability_versions.id", ondelete="RESTRICT"),
            nullable=True,
        ),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("resolved_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("published_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "state IN ('PENDING', 'READY', 'PUBLISHED', 'FAILED', 'EXPIRED')",
            name="ck_plugin_source_resolution_state",
        ),
        sa.CheckConstraint(
            "state_version > 0",
            name="ck_plugin_source_resolution_version_positive",
        ),
        sa.UniqueConstraint(
            "source_url",
            "requested_commit",
            "repo_path",
            name="uq_plugin_source_resolution_identity",
        ),
    )
    op.create_index("ix_plugin_source_resolutions_state", "plugin_source_resolutions", ["state"])
    op.create_index(
        "ix_plugin_source_resolutions_content_hash",
        "plugin_source_resolutions",
        ["content_hash"],
    )
    op.create_index(
        "ix_plugin_source_resolutions_capability_version_id",
        "plugin_source_resolutions",
        ["capability_version_id"],
    )
    op.create_index(
        "ix_plugin_source_resolutions_expires_at",
        "plugin_source_resolutions",
        ["expires_at"],
    )


def downgrade() -> None:
    op.drop_table("plugin_source_resolutions")
