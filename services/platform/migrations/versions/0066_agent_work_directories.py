"""Add versioned Agent Workspace work directories.

Revision ID: 0066_agent_work_directories
Revises: 0065_agent_model_selection
Create Date: 2026-08-27
"""

import sqlalchemy as sa
from alembic import op

revision = "0066_agent_work_directories"
down_revision = "0065_agent_model_selection"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "agent_work_directories",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column(
            "workspace_id",
            sa.String(36),
            sa.ForeignKey("agent_workspaces.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column("display_name", sa.String(160), nullable=False),
        sa.Column("state", sa.String(20), nullable=False),
        sa.Column("current_version", sa.Integer(), nullable=False),
        sa.Column("row_version", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("archived_at", sa.DateTime(timezone=True), nullable=True),
        sa.UniqueConstraint(
            "workspace_id", "display_name", name="uq_agent_work_directory_workspace_name"
        ),
        sa.CheckConstraint("state IN ('ACTIVE', 'ARCHIVED')", name="ck_agent_work_directory_state"),
        sa.CheckConstraint("current_version >= 1", name="ck_agent_work_directory_current_version"),
        sa.CheckConstraint("row_version >= 1", name="ck_agent_work_directory_row_version"),
    )
    op.create_index(
        "ix_agent_work_directories_workspace_id", "agent_work_directories", ["workspace_id"]
    )
    op.create_index("ix_agent_work_directories_state", "agent_work_directories", ["state"])

    op.create_table(
        "agent_work_directory_versions",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column(
            "work_directory_id",
            sa.String(36),
            sa.ForeignKey("agent_work_directories.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("working_path", sa.String(500), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("work_directory_id", "version", name="uq_agent_work_directory_version"),
        sa.CheckConstraint("version >= 1", name="ck_agent_work_directory_version_number"),
        sa.CheckConstraint(
            "working_path = '.' OR (working_path <> '' AND working_path NOT LIKE '/%')",
            name="ck_agent_work_directory_working_path",
        ),
    )
    op.create_index(
        "ix_agent_work_directory_versions_work_directory_id",
        "agent_work_directory_versions",
        ["work_directory_id"],
    )

    op.create_table(
        "agent_work_directory_paths",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column(
            "version_id",
            sa.String(36),
            sa.ForeignKey("agent_work_directory_versions.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column("relative_path", sa.String(500), nullable=False),
        sa.Column("position", sa.Integer(), nullable=False),
        sa.UniqueConstraint(
            "version_id", "relative_path", name="uq_agent_work_directory_version_path"
        ),
        sa.UniqueConstraint(
            "version_id", "position", name="uq_agent_work_directory_version_position"
        ),
        sa.CheckConstraint("position >= 0", name="ck_agent_work_directory_path_position"),
        sa.CheckConstraint(
            "relative_path <> '' AND relative_path NOT LIKE '/%'",
            name="ck_agent_work_directory_relative_path",
        ),
    )
    op.create_index(
        "ix_agent_work_directory_paths_version_id",
        "agent_work_directory_paths",
        ["version_id"],
    )


def downgrade() -> None:
    op.drop_table("agent_work_directory_paths")
    op.drop_table("agent_work_directory_versions")
    op.drop_table("agent_work_directories")
