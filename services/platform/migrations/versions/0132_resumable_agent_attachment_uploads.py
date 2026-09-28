"""add resumable agent attachment uploads.

Revision ID: 0132_resumable_attach
Revises: 0131_website_pwd_auth
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0132_resumable_attach"
down_revision = "0131_website_pwd_auth"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "agent_attachment_uploads",
        sa.Column("id", sa.String(length=36), primary_key=True),
        sa.Column("owner_user_id", sa.String(length=36), nullable=False),
        sa.Column("host_kind", sa.String(length=30), nullable=False),
        sa.Column("host_id", sa.String(length=36), nullable=False),
        sa.Column("host_scope_id", sa.String(length=36)),
        sa.Column("binding_id", sa.String(length=36)),
        sa.Column("work_directory_id", sa.String(length=36)),
        sa.Column("attachment_owner_id", sa.String(length=36), nullable=False),
        sa.Column("filename", sa.String(length=240), nullable=False),
        sa.Column("mime_type", sa.String(length=200), nullable=False),
        sa.Column("total_size", sa.Integer(), nullable=False),
        sa.Column("chunk_size", sa.Integer(), nullable=False, server_default="262144"),
        sa.Column("status", sa.String(length=20), nullable=False, server_default="ACTIVE"),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "host_kind IN ('AGENT_WORKSPACE', 'FLOW_NODE')",
            name="ck_agent_attachment_upload_host_kind",
        ),
        sa.CheckConstraint(
            "total_size > 0 AND total_size <= 26214400",
            name="ck_agent_attachment_upload_total_size",
        ),
        sa.CheckConstraint("chunk_size = 262144", name="ck_agent_attachment_upload_chunk_size"),
        sa.CheckConstraint(
            "status IN ('ACTIVE', 'COMPLETED', 'CANCELLED')",
            name="ck_agent_attachment_upload_status",
        ),
    )
    for column in (
        "owner_user_id",
        "host_kind",
        "host_id",
        "host_scope_id",
        "binding_id",
        "attachment_owner_id",
        "status",
        "expires_at",
    ):
        op.create_index(
            f"ix_agent_attachment_uploads_{column}", "agent_attachment_uploads", [column]
        )
    op.create_table(
        "agent_attachment_upload_parts",
        sa.Column("id", sa.String(length=36), primary_key=True),
        sa.Column("owner_user_id", sa.String(length=36), nullable=False),
        sa.Column("upload_id", sa.String(length=36), nullable=False),
        sa.Column("part_number", sa.Integer(), nullable=False),
        sa.Column("content", sa.LargeBinary(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("upload_id", "part_number", name="uq_agent_attachment_upload_part"),
        sa.CheckConstraint("part_number >= 0", name="ck_agent_attachment_upload_part_number"),
    )
    op.create_index(
        "ix_agent_attachment_upload_parts_owner_user_id",
        "agent_attachment_upload_parts",
        ["owner_user_id"],
    )
    op.create_index(
        "ix_agent_attachment_upload_parts_upload_id", "agent_attachment_upload_parts", ["upload_id"]
    )
    for table in ("agent_attachment_uploads", "agent_attachment_upload_parts"):
        policy = f"pl_tenant_{table}"
        op.execute(sa.text(f'ALTER TABLE "{table}" ENABLE ROW LEVEL SECURITY'))
        op.execute(sa.text(f'ALTER TABLE "{table}" FORCE ROW LEVEL SECURITY'))
        op.execute(
            sa.text(
                f'CREATE POLICY "{policy}" ON "{table}" '
                "USING (current_setting('flowweave.bypass', true) = 'on' OR "
                "owner_user_id = current_setting('flowweave.user_id', true)) "
                "WITH CHECK (current_setting('flowweave.bypass', true) = 'on' OR "
                "owner_user_id = current_setting('flowweave.user_id', true))"
            )
        )


def downgrade() -> None:
    for table in ("agent_attachment_upload_parts", "agent_attachment_uploads"):
        policy = f"pl_tenant_{table}"
        op.execute(sa.text(f'DROP POLICY IF EXISTS "{policy}" ON "{table}"'))
        op.execute(sa.text(f'ALTER TABLE "{table}" NO FORCE ROW LEVEL SECURITY'))
        op.execute(sa.text(f'ALTER TABLE "{table}" DISABLE ROW LEVEL SECURITY'))
    op.drop_table("agent_attachment_upload_parts")
    op.drop_table("agent_attachment_uploads")
