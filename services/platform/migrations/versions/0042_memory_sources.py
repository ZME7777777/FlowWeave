"""govern immutable Memory Source content versions

Revision ID: 0042_memory_sources
Revises: 0041_plugin_marketplace_sources
"""

import sqlalchemy as sa
from alembic import op

revision = "0042_memory_sources"
down_revision = "0041_plugin_marketplace_sources"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "memory_sources",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("source_key", sa.String(200), nullable=False, unique=True),
        sa.Column("display_name", sa.String(200), nullable=False),
        sa.Column("owner_id", sa.String(200), nullable=False),
        sa.Column("scope", sa.String(20), nullable=False),
        sa.Column("scope_key", sa.String(200), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint("scope IN ('USER', 'PROJECT')", name="ck_memory_source_scope"),
        sa.UniqueConstraint(
            "scope", "scope_key", "source_key", name="uq_memory_source_scope_identity"
        ),
    )
    for column in ("owner_id", "scope", "scope_key"):
        op.create_index(f"ix_memory_sources_{column}", "memory_sources", [column])

    op.create_table(
        "memory_source_versions",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column(
            "source_id",
            sa.String(36),
            sa.ForeignKey("memory_sources.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column(
            "previous_version_id",
            sa.String(36),
            sa.ForeignKey("memory_source_versions.id", ondelete="RESTRICT"),
            nullable=True,
        ),
        sa.Column("version_no", sa.Integer(), nullable=False),
        sa.Column("content", sa.Text(), nullable=False),
        sa.Column("digest", sa.String(64), nullable=False),
        sa.Column("byte_size", sa.Integer(), nullable=False),
        sa.Column("review_status", sa.String(20), nullable=False),
        sa.Column("sensitive_data_status", sa.String(20), nullable=False),
        sa.Column("lifecycle_state", sa.String(20), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint("version_no > 0", name="ck_memory_source_version_positive"),
        sa.CheckConstraint("byte_size > 0", name="ck_memory_source_version_size_positive"),
        sa.CheckConstraint(
            "review_status IN ('PENDING', 'APPROVED', 'REJECTED')",
            name="ck_memory_source_version_review",
        ),
        sa.CheckConstraint(
            "sensitive_data_status IN ('NOT_SCANNED', 'PASSED', 'BLOCKED')",
            name="ck_memory_source_version_sensitive_data",
        ),
        sa.CheckConstraint(
            "lifecycle_state IN ('DRAFT', 'ACTIVE', 'RETIRED')",
            name="ck_memory_source_version_lifecycle",
        ),
        sa.UniqueConstraint("source_id", "version_no", name="uq_memory_source_version_number"),
        sa.UniqueConstraint("source_id", "digest", name="uq_memory_source_version_digest"),
        sa.UniqueConstraint("previous_version_id", name="uq_memory_source_previous_version"),
    )
    for column in (
        "source_id",
        "previous_version_id",
        "digest",
        "review_status",
        "sensitive_data_status",
        "lifecycle_state",
    ):
        op.create_index(f"ix_memory_source_versions_{column}", "memory_source_versions", [column])

    op.execute(
        """
        CREATE FUNCTION protect_memory_source_identity() RETURNS trigger AS $$
        BEGIN
            IF OLD.source_key IS DISTINCT FROM NEW.source_key
               OR OLD.owner_id IS DISTINCT FROM NEW.owner_id
               OR OLD.scope IS DISTINCT FROM NEW.scope
               OR OLD.scope_key IS DISTINCT FROM NEW.scope_key THEN
                RAISE EXCEPTION 'Memory Source identity and ownership are immutable'
                    USING ERRCODE = 'check_violation';
            END IF;
            RETURN NEW;
        END;
        $$ LANGUAGE plpgsql;
        CREATE TRIGGER trg_memory_source_identity_immutable
        BEFORE UPDATE ON memory_sources
        FOR EACH ROW EXECUTE FUNCTION protect_memory_source_identity();
        """
    )
    op.execute(
        """
        CREATE FUNCTION protect_memory_source_version() RETURNS trigger AS $$
        BEGIN
            IF TG_OP = 'DELETE' THEN
                RAISE EXCEPTION 'Memory Source versions are immutable'
                    USING ERRCODE = 'check_violation';
            END IF;
            IF OLD.source_id IS DISTINCT FROM NEW.source_id
               OR OLD.previous_version_id IS DISTINCT FROM NEW.previous_version_id
               OR OLD.version_no IS DISTINCT FROM NEW.version_no
               OR OLD.content IS DISTINCT FROM NEW.content
               OR OLD.digest IS DISTINCT FROM NEW.digest
               OR OLD.byte_size IS DISTINCT FROM NEW.byte_size
               OR OLD.created_at IS DISTINCT FROM NEW.created_at THEN
                RAISE EXCEPTION 'Memory Source version content is immutable'
                    USING ERRCODE = 'check_violation';
            END IF;
            RETURN NEW;
        END;
        $$ LANGUAGE plpgsql;
        CREATE TRIGGER trg_memory_source_version_immutable
        BEFORE UPDATE OR DELETE ON memory_source_versions
        FOR EACH ROW EXECUTE FUNCTION protect_memory_source_version();
        """
    )


def downgrade() -> None:
    op.execute("DROP TRIGGER trg_memory_source_version_immutable ON memory_source_versions")
    op.execute("DROP FUNCTION protect_memory_source_version()")
    op.execute("DROP TRIGGER trg_memory_source_identity_immutable ON memory_sources")
    op.execute("DROP FUNCTION protect_memory_source_identity()")
    op.drop_table("memory_source_versions")
    op.drop_table("memory_sources")
