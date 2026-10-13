"""immutable artifacts and bindings"""

import sqlalchemy as sa
from alembic import op

revision = "0004_artifacts"
down_revision = "0003_runs"
branch_labels = None
depends_on = None


def upgrade():
    # Freeze the original tables. 0082, 0095 and 0103 own later node,
    # tenancy and Runtime completion fields; live ORM metadata is not history.
    op.create_table(
        "artifact_versions",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column(
            "flow_run_id",
            sa.String(36),
            sa.ForeignKey("flow_runs.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "producer_attempt_id",
            sa.String(36),
            sa.ForeignKey("node_attempts.id", ondelete="RESTRICT"),
            nullable=True,
        ),
        sa.Column("field_key", sa.String(100), nullable=False),
        sa.Column("version_no", sa.Integer(), nullable=False),
        sa.Column("artifact_type", sa.String(80), nullable=False),
        sa.Column("storage_key", sa.Text(), nullable=True),
        sa.Column("uri", sa.Text(), nullable=True),
        sa.Column("inline_content", sa.Text(), nullable=True),
        sa.Column("content_hash", sa.String(64), nullable=False),
        sa.Column("byte_size", sa.Integer(), nullable=False),
        sa.Column("mime_type", sa.String(100), nullable=False),
        sa.Column("source", sa.String(30), nullable=False),
        sa.Column("metadata_json", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint(
            "flow_run_id", "field_key", "version_no", name="uq_artifact_field_version"
        ),
        sa.CheckConstraint(
            "inline_content IS NOT NULL OR uri IS NOT NULL OR storage_key IS NOT NULL",
            name="ck_artifact_has_content",
        ),
        sa.CheckConstraint("byte_size >= 0", name="ck_artifact_size_nonnegative"),
    )
    op.create_index("ix_artifact_versions_flow_run_id", "artifact_versions", ["flow_run_id"])
    op.create_index(
        "ix_artifact_versions_producer_attempt_id", "artifact_versions", ["producer_attempt_id"]
    )
    op.create_table(
        "attempt_input_bindings",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column(
            "attempt_id",
            sa.String(36),
            sa.ForeignKey("node_attempts.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("input_field_key", sa.String(100), nullable=False),
        sa.Column(
            "artifact_version_id",
            sa.String(36),
            sa.ForeignKey("artifact_versions.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column("binding_source", sa.String(30), nullable=False),
        sa.UniqueConstraint("attempt_id", "input_field_key", name="uq_attempt_input_field"),
    )
    op.create_index(
        "ix_attempt_input_bindings_attempt_id", "attempt_input_bindings", ["attempt_id"]
    )


def downgrade():
    op.drop_table("attempt_input_bindings")
    op.drop_table("artifact_versions")
