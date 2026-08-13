"""govern Memory Source review, scanning, and activation

Revision ID: 0043_memory_source_governance
Revises: 0042_memory_sources
"""

import sqlalchemy as sa
from alembic import op

revision = "0043_memory_source_governance"
down_revision = "0042_memory_sources"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "memory_source_versions",
        sa.Column("governance_version", sa.Integer(), nullable=False, server_default="1"),
    )
    op.add_column("memory_source_versions", sa.Column("reviewed_by", sa.String(200), nullable=True))
    op.add_column("memory_source_versions", sa.Column("reviewed_at", sa.DateTime(timezone=True)))
    op.add_column("memory_source_versions", sa.Column("review_note", sa.Text(), nullable=True))
    op.add_column(
        "memory_source_versions",
        sa.Column("sensitive_data_scanner", sa.String(100), nullable=True),
    )
    op.add_column(
        "memory_source_versions",
        sa.Column("sensitive_data_report_json", sa.JSON(), nullable=False, server_default="{}"),
    )
    op.add_column(
        "memory_source_versions",
        sa.Column("sensitive_data_scanned_at", sa.DateTime(timezone=True)),
    )
    op.add_column("memory_source_versions", sa.Column("activated_at", sa.DateTime(timezone=True)))
    op.add_column("memory_source_versions", sa.Column("retired_at", sa.DateTime(timezone=True)))
    op.create_check_constraint(
        "ck_memory_source_version_governance_positive",
        "memory_source_versions",
        "governance_version > 0",
    )
    op.create_check_constraint(
        "ck_memory_source_version_active_governed",
        "memory_source_versions",
        "lifecycle_state != 'ACTIVE' OR "
        "(review_status = 'APPROVED' AND sensitive_data_status = 'PASSED')",
    )
    op.create_index(
        "uq_memory_source_one_active_version",
        "memory_source_versions",
        ["source_id"],
        unique=True,
        postgresql_where=sa.text("lifecycle_state = 'ACTIVE'"),
    )
    op.alter_column("memory_source_versions", "governance_version", server_default=None)
    op.alter_column("memory_source_versions", "sensitive_data_report_json", server_default=None)

    op.execute(
        """
        CREATE OR REPLACE FUNCTION protect_memory_source_version() RETURNS trigger AS $$
        DECLARE
            changed_steps integer;
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

            changed_steps :=
                (CASE WHEN OLD.review_status IS DISTINCT FROM NEW.review_status THEN 1 ELSE 0 END) +
                (CASE WHEN OLD.sensitive_data_status IS DISTINCT FROM NEW.sensitive_data_status
                    THEN 1 ELSE 0 END) +
                (CASE WHEN OLD.lifecycle_state IS DISTINCT FROM NEW.lifecycle_state
                    THEN 1 ELSE 0 END);

            IF changed_steps = 0 THEN
                IF OLD.governance_version IS DISTINCT FROM NEW.governance_version
                   OR OLD.reviewed_by IS DISTINCT FROM NEW.reviewed_by
                   OR OLD.reviewed_at IS DISTINCT FROM NEW.reviewed_at
                   OR OLD.review_note IS DISTINCT FROM NEW.review_note
                   OR OLD.sensitive_data_scanner IS DISTINCT FROM NEW.sensitive_data_scanner
                   OR OLD.sensitive_data_report_json::jsonb
                      IS DISTINCT FROM NEW.sensitive_data_report_json::jsonb
                   OR OLD.sensitive_data_scanned_at IS DISTINCT FROM NEW.sensitive_data_scanned_at
                   OR OLD.activated_at IS DISTINCT FROM NEW.activated_at
                   OR OLD.retired_at IS DISTINCT FROM NEW.retired_at THEN
                    RAISE EXCEPTION
                        'Memory Source governance evidence cannot change without a state transition'
                        USING ERRCODE = 'check_violation';
                END IF;
                RETURN NEW;
            END IF;

            IF changed_steps != 1 OR NEW.governance_version != OLD.governance_version + 1 THEN
                RAISE EXCEPTION 'Memory Source governance must advance one fenced step at a time'
                    USING ERRCODE = 'check_violation';
            END IF;

            IF OLD.review_status IS DISTINCT FROM NEW.review_status THEN
                IF OLD.review_status != 'PENDING'
                   OR NEW.review_status NOT IN ('APPROVED', 'REJECTED')
                   OR NEW.reviewed_by IS NULL OR NEW.reviewed_at IS NULL
                   OR OLD.sensitive_data_status IS DISTINCT FROM NEW.sensitive_data_status
                   OR OLD.lifecycle_state IS DISTINCT FROM NEW.lifecycle_state
                   OR OLD.sensitive_data_scanner IS DISTINCT FROM NEW.sensitive_data_scanner
                   OR OLD.sensitive_data_report_json::jsonb
                      IS DISTINCT FROM NEW.sensitive_data_report_json::jsonb
                   OR OLD.sensitive_data_scanned_at IS DISTINCT FROM NEW.sensitive_data_scanned_at
                   OR OLD.activated_at IS DISTINCT FROM NEW.activated_at
                   OR OLD.retired_at IS DISTINCT FROM NEW.retired_at THEN
                    RAISE EXCEPTION 'Invalid Memory Source review transition'
                        USING ERRCODE = 'check_violation';
                END IF;
            ELSIF OLD.sensitive_data_status IS DISTINCT FROM NEW.sensitive_data_status THEN
                IF OLD.sensitive_data_status != 'NOT_SCANNED'
                   OR NEW.sensitive_data_status NOT IN ('PASSED', 'BLOCKED')
                   OR NEW.sensitive_data_scanner IS NULL OR NEW.sensitive_data_scanned_at IS NULL
                   OR NEW.sensitive_data_report_json::jsonb = '{}'::jsonb
                   OR OLD.review_status IS DISTINCT FROM NEW.review_status
                   OR OLD.lifecycle_state IS DISTINCT FROM NEW.lifecycle_state
                   OR OLD.reviewed_by IS DISTINCT FROM NEW.reviewed_by
                   OR OLD.reviewed_at IS DISTINCT FROM NEW.reviewed_at
                   OR OLD.review_note IS DISTINCT FROM NEW.review_note
                   OR OLD.activated_at IS DISTINCT FROM NEW.activated_at
                   OR OLD.retired_at IS DISTINCT FROM NEW.retired_at THEN
                    RAISE EXCEPTION 'Invalid Memory Source sensitive-data transition'
                        USING ERRCODE = 'check_violation';
                END IF;
            ELSE
                IF OLD.reviewed_by IS DISTINCT FROM NEW.reviewed_by
                   OR OLD.reviewed_at IS DISTINCT FROM NEW.reviewed_at
                   OR OLD.review_note IS DISTINCT FROM NEW.review_note
                   OR OLD.sensitive_data_scanner IS DISTINCT FROM NEW.sensitive_data_scanner
                   OR OLD.sensitive_data_report_json::jsonb
                      IS DISTINCT FROM NEW.sensitive_data_report_json::jsonb
                   OR OLD.sensitive_data_scanned_at
                        IS DISTINCT FROM NEW.sensitive_data_scanned_at THEN
                    RAISE EXCEPTION
                        'Memory Source lifecycle transition cannot alter governance evidence'
                        USING ERRCODE = 'check_violation';
                END IF;
                IF OLD.lifecycle_state = 'DRAFT' AND NEW.lifecycle_state = 'ACTIVE' THEN
                    IF OLD.review_status != 'APPROVED' OR OLD.sensitive_data_status != 'PASSED'
                       OR OLD.activated_at IS NOT NULL OR NEW.activated_at IS NULL
                       OR OLD.retired_at IS DISTINCT FROM NEW.retired_at
                       OR NEW.retired_at IS NOT NULL THEN
                        RAISE EXCEPTION
                            'Memory Source activation requires approved review and passed scan'
                            USING ERRCODE = 'check_violation';
                    END IF;
                ELSIF OLD.lifecycle_state = 'ACTIVE' AND NEW.lifecycle_state = 'RETIRED' THEN
                    IF OLD.activated_at IS NULL
                       OR OLD.activated_at IS DISTINCT FROM NEW.activated_at
                       OR OLD.retired_at IS NOT NULL OR NEW.retired_at IS NULL THEN
                        RAISE EXCEPTION 'Invalid Memory Source retirement transition'
                            USING ERRCODE = 'check_violation';
                    END IF;
                ELSE
                    RAISE EXCEPTION 'Invalid Memory Source lifecycle transition'
                        USING ERRCODE = 'check_violation';
                END IF;
            END IF;
            RETURN NEW;
        END;
        $$ LANGUAGE plpgsql;
        """
    )


def downgrade() -> None:
    op.execute(
        """
        CREATE OR REPLACE FUNCTION protect_memory_source_version() RETURNS trigger AS $$
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
        """
    )
    op.drop_index("uq_memory_source_one_active_version", table_name="memory_source_versions")
    op.drop_constraint(
        "ck_memory_source_version_active_governed",
        "memory_source_versions",
        type_="check",
    )
    op.drop_constraint(
        "ck_memory_source_version_governance_positive",
        "memory_source_versions",
        type_="check",
    )
    for column in (
        "retired_at",
        "activated_at",
        "sensitive_data_scanned_at",
        "sensitive_data_report_json",
        "sensitive_data_scanner",
        "review_note",
        "reviewed_at",
        "reviewed_by",
        "governance_version",
    ):
        op.drop_column("memory_source_versions", column)
