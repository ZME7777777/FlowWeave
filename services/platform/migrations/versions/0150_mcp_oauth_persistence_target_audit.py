"""audit the governed target of MCP OAuth state persistence.

Revision ID: 0150_mcp_oauth_persistence_target_audit
Revises: 0149_add_oh_fw_v2_protocol_profile
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0150_mcp_oauth_persistence_target_audit"
down_revision = "0149_add_oh_fw_v2_protocol_profile"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "mcp_oauth_secret_references",
        sa.Column("mcp_server_url_digest", sa.String(length=64), nullable=True),
    )
    op.add_column(
        "mcp_oauth_secret_audits",
        sa.Column("capability_version_id", sa.String(length=36), nullable=True),
    )
    op.add_column(
        "mcp_oauth_secret_audits",
        sa.Column("environment_version_id", sa.String(length=36), nullable=True),
    )
    op.add_column(
        "mcp_oauth_secret_audits",
        sa.Column("runtime_resource_digest", sa.String(length=64), nullable=True),
    )
    op.add_column(
        "mcp_oauth_secret_audits",
        sa.Column("mcp_server_url_digest", sa.String(length=64), nullable=True),
    )
    op.create_index(
        "ix_mcp_oauth_secret_audits_capability_version_id",
        "mcp_oauth_secret_audits",
        ["capability_version_id"],
    )
    op.create_index(
        "ix_mcp_oauth_secret_audits_environment_version_id",
        "mcp_oauth_secret_audits",
        ["environment_version_id"],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_mcp_oauth_secret_audits_environment_version_id",
        table_name="mcp_oauth_secret_audits",
    )
    op.drop_index(
        "ix_mcp_oauth_secret_audits_capability_version_id",
        table_name="mcp_oauth_secret_audits",
    )
    op.drop_column("mcp_oauth_secret_audits", "mcp_server_url_digest")
    op.drop_column("mcp_oauth_secret_audits", "runtime_resource_digest")
    op.drop_column("mcp_oauth_secret_audits", "environment_version_id")
    op.drop_column("mcp_oauth_secret_audits", "capability_version_id")
    op.drop_column("mcp_oauth_secret_references", "mcp_server_url_digest")
