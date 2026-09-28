"""add resumable workspace file uploads.

Revision ID: 0133_workspace_file_resumable_uploads
Revises: 0132_resumable_agent_attachment_uploads
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa

revision = "0133_workspace_file_resumable_uploads"
down_revision = "0132_resumable_agent_attachment_uploads"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("agent_attachment_uploads", sa.Column("upload_kind", sa.String(length=20), nullable=False, server_default="ATTACHMENT"))
    op.add_column("agent_attachment_uploads", sa.Column("target_path", sa.String(length=500)))
    op.alter_column("agent_attachment_uploads", "attachment_owner_id", existing_type=sa.String(length=36), nullable=True)
    op.drop_constraint("ck_agent_attachment_upload_total_size", "agent_attachment_uploads", type_="check")
    op.create_check_constraint("ck_agent_attachment_upload_total_size", "agent_attachment_uploads", "total_size > 0 AND total_size <= 104857600")
    op.create_check_constraint("ck_agent_attachment_upload_kind", "agent_attachment_uploads", "upload_kind IN ('ATTACHMENT', 'WORKSPACE_FILE')")


def downgrade() -> None:
    op.drop_constraint("ck_agent_attachment_upload_kind", "agent_attachment_uploads", type_="check")
    op.drop_constraint("ck_agent_attachment_upload_total_size", "agent_attachment_uploads", type_="check")
    op.create_check_constraint("ck_agent_attachment_upload_total_size", "agent_attachment_uploads", "total_size > 0 AND total_size <= 26214400")
    op.alter_column("agent_attachment_uploads", "attachment_owner_id", existing_type=sa.String(length=36), nullable=False)
    op.drop_column("agent_attachment_uploads", "target_path")
    op.drop_column("agent_attachment_uploads", "upload_kind")
