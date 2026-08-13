"""govern Memory Source retention and irreversible content deletion

Revision ID: 0044_memory_source_retention
Revises: 0043_memory_source_governance
"""

import sqlalchemy as sa
from alembic import op

revision = "0044_memory_source_retention"
down_revision = "0043_memory_source_governance"
branch_labels = None
depends_on = None

_GOVERNANCE_TRIGGER_SQL = """
CREATE FUNCTION protect_memory_source_version() RETURNS trigger AS $$
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
        (CASE WHEN OLD.lifecycle_state IS DISTINCT FROM NEW.lifecycle_state THEN 1 ELSE 0 END);

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
           OR OLD.sensitive_data_scanned_at IS DISTINCT FROM NEW.sensitive_data_scanned_at THEN
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


def upgrade() -> None:
    op.add_column("memory_source_versions", sa.Column("retention_days", sa.Integer()))
    op.add_column("memory_source_versions", sa.Column("expires_at", sa.DateTime(timezone=True)))
    op.add_column("memory_source_versions", sa.Column("expired_at", sa.DateTime(timezone=True)))
    op.add_column("memory_source_versions", sa.Column("deleted_at", sa.DateTime(timezone=True)))
    op.create_index(
        "ix_memory_source_versions_expires_at", "memory_source_versions", ["expires_at"]
    )
    op.execute(
        "UPDATE memory_source_versions SET retention_days = 30 WHERE lifecycle_state = 'ACTIVE'"
    )
    op.execute(
        "UPDATE memory_source_versions "
        "SET retention_days = 30, "
        "expires_at = retired_at + make_interval(days => 30) "
        "WHERE lifecycle_state = 'RETIRED'"
    )
    op.create_check_constraint(
        "ck_memory_source_version_retention_days",
        "memory_source_versions",
        "retention_days IS NULL OR retention_days BETWEEN 1 AND 3650",
    )
    op.create_check_constraint(
        "ck_memory_source_version_retention_state",
        "memory_source_versions",
        "(lifecycle_state = 'DRAFT' AND content <> '' AND retention_days IS NULL "
        "AND activated_at IS NULL "
        "AND retired_at IS NULL AND expires_at IS NULL AND expired_at IS NULL "
        "AND deleted_at IS NULL) OR "
        "(lifecycle_state = 'ACTIVE' AND content <> '' AND retention_days IS NOT NULL "
        "AND activated_at IS NOT NULL "
        "AND retired_at IS NULL AND expires_at IS NULL AND expired_at IS NULL "
        "AND deleted_at IS NULL) OR "
        "(lifecycle_state = 'RETIRED' AND content <> '' AND retention_days IS NOT NULL "
        "AND activated_at IS NOT NULL AND retired_at >= activated_at "
        "AND expires_at = retired_at + make_interval(days => retention_days) "
        "AND expired_at IS NULL "
        "AND deleted_at IS NULL) OR "
        "(lifecycle_state = 'EXPIRED' AND content <> '' AND retention_days IS NOT NULL "
        "AND activated_at IS NOT NULL AND retired_at >= activated_at "
        "AND expires_at = retired_at + make_interval(days => retention_days) "
        "AND expired_at >= expires_at "
        "AND deleted_at IS NULL) OR "
        "(lifecycle_state = 'DELETED' AND retention_days IS NOT NULL "
        "AND activated_at IS NOT NULL AND retired_at >= activated_at "
        "AND expires_at = retired_at + make_interval(days => retention_days) "
        "AND expired_at >= expires_at AND deleted_at >= expired_at AND content = '')",
    )
    op.drop_constraint(
        "ck_memory_source_version_lifecycle", "memory_source_versions", type_="check"
    )
    op.create_check_constraint(
        "ck_memory_source_version_lifecycle",
        "memory_source_versions",
        "lifecycle_state IN ('DRAFT', 'ACTIVE', 'RETIRED', 'EXPIRED', 'DELETED')",
    )

    op.create_table(
        "memory_source_version_references",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column(
            "memory_source_version_id",
            sa.String(36),
            sa.ForeignKey("memory_source_versions.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column("reference_kind", sa.String(30), nullable=False),
        sa.Column("reference_id", sa.String(36), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "reference_kind IN ('POLICY_VERSION', 'RUN_SNAPSHOT')",
            name="ck_memory_source_version_reference_kind",
        ),
        sa.UniqueConstraint(
            "memory_source_version_id",
            "reference_kind",
            "reference_id",
            name="uq_memory_source_version_reference",
        ),
    )
    for column in ("memory_source_version_id", "reference_kind", "reference_id"):
        op.create_index(
            f"ix_memory_source_version_references_{column}",
            "memory_source_version_references",
            [column],
        )

    op.execute(
        """
        CREATE OR REPLACE FUNCTION protect_memory_source_version() RETURNS trigger AS $$
        DECLARE
            changed_steps integer;
        BEGIN
            IF TG_OP = 'INSERT' THEN
                IF NEW.review_status != 'PENDING'
                   OR NEW.sensitive_data_status != 'NOT_SCANNED'
                   OR NEW.lifecycle_state != 'DRAFT'
                   OR NEW.governance_version != 1
                   OR NEW.content = ''
                   OR NEW.reviewed_by IS NOT NULL OR NEW.reviewed_at IS NOT NULL
                   OR NEW.review_note IS NOT NULL
                   OR NEW.sensitive_data_scanner IS NOT NULL
                   OR NEW.sensitive_data_report_json::jsonb != '{}'::jsonb
                   OR NEW.sensitive_data_scanned_at IS NOT NULL
                   OR NEW.activated_at IS NOT NULL OR NEW.retired_at IS NOT NULL
                   OR NEW.retention_days IS NOT NULL OR NEW.expires_at IS NOT NULL
                   OR NEW.expired_at IS NOT NULL OR NEW.deleted_at IS NOT NULL THEN
                    RAISE EXCEPTION 'Invalid initial Memory Source governance state'
                        USING ERRCODE = 'check_violation';
                END IF;
                RETURN NEW;
            END IF;
            IF TG_OP = 'DELETE' THEN
                RAISE EXCEPTION 'Memory Source versions cannot be physically deleted'
                    USING ERRCODE = 'check_violation';
            END IF;
            IF OLD.source_id IS DISTINCT FROM NEW.source_id
               OR OLD.previous_version_id IS DISTINCT FROM NEW.previous_version_id
               OR OLD.version_no IS DISTINCT FROM NEW.version_no
               OR OLD.digest IS DISTINCT FROM NEW.digest
               OR OLD.byte_size IS DISTINCT FROM NEW.byte_size
               OR OLD.created_at IS DISTINCT FROM NEW.created_at THEN
                RAISE EXCEPTION 'Memory Source version identity is immutable'
                    USING ERRCODE = 'check_violation';
            END IF;

            changed_steps :=
                (CASE WHEN OLD.review_status IS DISTINCT FROM NEW.review_status THEN 1 ELSE 0 END) +
                (CASE WHEN OLD.sensitive_data_status IS DISTINCT FROM NEW.sensitive_data_status
                    THEN 1 ELSE 0 END) +
                (CASE WHEN OLD.lifecycle_state IS DISTINCT FROM NEW.lifecycle_state
                    THEN 1 ELSE 0 END);

            IF changed_steps = 0 THEN
                IF OLD.content IS DISTINCT FROM NEW.content
                   OR OLD.governance_version IS DISTINCT FROM NEW.governance_version
                   OR OLD.reviewed_by IS DISTINCT FROM NEW.reviewed_by
                   OR OLD.reviewed_at IS DISTINCT FROM NEW.reviewed_at
                   OR OLD.review_note IS DISTINCT FROM NEW.review_note
                   OR OLD.sensitive_data_scanner IS DISTINCT FROM NEW.sensitive_data_scanner
                   OR OLD.sensitive_data_report_json::jsonb
                      IS DISTINCT FROM NEW.sensitive_data_report_json::jsonb
                   OR OLD.sensitive_data_scanned_at IS DISTINCT FROM NEW.sensitive_data_scanned_at
                   OR OLD.activated_at IS DISTINCT FROM NEW.activated_at
                   OR OLD.retired_at IS DISTINCT FROM NEW.retired_at
                   OR OLD.retention_days IS DISTINCT FROM NEW.retention_days
                   OR OLD.expires_at IS DISTINCT FROM NEW.expires_at
                   OR OLD.expired_at IS DISTINCT FROM NEW.expired_at
                   OR OLD.deleted_at IS DISTINCT FROM NEW.deleted_at THEN
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
                   OR OLD.content IS DISTINCT FROM NEW.content
                   OR OLD.sensitive_data_status IS DISTINCT FROM NEW.sensitive_data_status
                   OR OLD.lifecycle_state IS DISTINCT FROM NEW.lifecycle_state
                   OR OLD.sensitive_data_scanner IS DISTINCT FROM NEW.sensitive_data_scanner
                   OR OLD.sensitive_data_report_json::jsonb
                      IS DISTINCT FROM NEW.sensitive_data_report_json::jsonb
                   OR OLD.sensitive_data_scanned_at IS DISTINCT FROM NEW.sensitive_data_scanned_at
                   OR OLD.activated_at IS DISTINCT FROM NEW.activated_at
                   OR OLD.retired_at IS DISTINCT FROM NEW.retired_at
                   OR OLD.retention_days IS DISTINCT FROM NEW.retention_days
                   OR OLD.expires_at IS DISTINCT FROM NEW.expires_at
                   OR OLD.expired_at IS DISTINCT FROM NEW.expired_at
                   OR OLD.deleted_at IS DISTINCT FROM NEW.deleted_at THEN
                    RAISE EXCEPTION 'Invalid Memory Source review transition'
                        USING ERRCODE = 'check_violation';
                END IF;
            ELSIF OLD.sensitive_data_status IS DISTINCT FROM NEW.sensitive_data_status THEN
                IF OLD.sensitive_data_status != 'NOT_SCANNED'
                   OR NEW.sensitive_data_status NOT IN ('PASSED', 'BLOCKED')
                   OR NEW.sensitive_data_scanner IS NULL OR NEW.sensitive_data_scanned_at IS NULL
                   OR NEW.sensitive_data_report_json::jsonb = '{}'::jsonb
                   OR OLD.content IS DISTINCT FROM NEW.content
                   OR OLD.review_status IS DISTINCT FROM NEW.review_status
                   OR OLD.lifecycle_state IS DISTINCT FROM NEW.lifecycle_state
                   OR OLD.reviewed_by IS DISTINCT FROM NEW.reviewed_by
                   OR OLD.reviewed_at IS DISTINCT FROM NEW.reviewed_at
                   OR OLD.review_note IS DISTINCT FROM NEW.review_note
                   OR OLD.activated_at IS DISTINCT FROM NEW.activated_at
                   OR OLD.retired_at IS DISTINCT FROM NEW.retired_at
                   OR OLD.retention_days IS DISTINCT FROM NEW.retention_days
                   OR OLD.expires_at IS DISTINCT FROM NEW.expires_at
                   OR OLD.expired_at IS DISTINCT FROM NEW.expired_at
                   OR OLD.deleted_at IS DISTINCT FROM NEW.deleted_at THEN
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
                       OR OLD.content IS DISTINCT FROM NEW.content
                       OR OLD.activated_at IS NOT NULL OR NEW.activated_at IS NULL
                       OR OLD.retention_days IS NOT NULL OR NEW.retention_days IS NULL
                       OR OLD.retired_at IS DISTINCT FROM NEW.retired_at
                       OR OLD.expires_at IS DISTINCT FROM NEW.expires_at
                       OR OLD.expired_at IS DISTINCT FROM NEW.expired_at
                       OR OLD.deleted_at IS DISTINCT FROM NEW.deleted_at THEN
                        RAISE EXCEPTION
                            'Memory Source activation requires governed content and retention'
                            USING ERRCODE = 'check_violation';
                    END IF;
                ELSIF OLD.lifecycle_state = 'ACTIVE' AND NEW.lifecycle_state = 'RETIRED' THEN
                    IF OLD.content IS DISTINCT FROM NEW.content
                       OR OLD.activated_at IS NULL
                       OR OLD.activated_at IS DISTINCT FROM NEW.activated_at
                       OR OLD.retention_days IS NULL
                       OR OLD.retention_days IS DISTINCT FROM NEW.retention_days
                       OR OLD.retired_at IS NOT NULL OR NEW.retired_at IS NULL
                       OR NEW.retired_at < NEW.activated_at
                       OR OLD.expires_at IS NOT NULL OR NEW.expires_at IS NULL
                       OR NEW.expires_at != NEW.retired_at
                           + make_interval(days => NEW.retention_days)
                       OR OLD.expired_at IS DISTINCT FROM NEW.expired_at
                       OR OLD.deleted_at IS DISTINCT FROM NEW.deleted_at THEN
                        RAISE EXCEPTION 'Invalid Memory Source retirement transition'
                            USING ERRCODE = 'check_violation';
                    END IF;
                ELSIF OLD.lifecycle_state = 'RETIRED' AND NEW.lifecycle_state = 'EXPIRED' THEN
                    IF OLD.content IS DISTINCT FROM NEW.content
                       OR OLD.activated_at IS DISTINCT FROM NEW.activated_at
                       OR OLD.retired_at IS DISTINCT FROM NEW.retired_at
                       OR OLD.retention_days IS DISTINCT FROM NEW.retention_days
                       OR OLD.expires_at IS NULL OR OLD.expires_at IS DISTINCT FROM NEW.expires_at
                       OR CURRENT_TIMESTAMP < OLD.expires_at
                       OR OLD.expired_at IS NOT NULL OR NEW.expired_at IS NULL
                       OR NEW.expired_at < NEW.expires_at
                       OR OLD.deleted_at IS DISTINCT FROM NEW.deleted_at THEN
                        RAISE EXCEPTION 'Memory Source retention period has not ended'
                            USING ERRCODE = 'check_violation';
                    END IF;
                ELSIF OLD.lifecycle_state = 'EXPIRED' AND NEW.lifecycle_state = 'DELETED' THEN
                    IF EXISTS (
                        SELECT 1 FROM memory_source_version_references
                        WHERE memory_source_version_id = OLD.id
                    ) OR OLD.content = '' OR NEW.content != ''
                       OR OLD.activated_at IS DISTINCT FROM NEW.activated_at
                       OR OLD.retired_at IS DISTINCT FROM NEW.retired_at
                       OR OLD.retention_days IS DISTINCT FROM NEW.retention_days
                       OR OLD.expires_at IS DISTINCT FROM NEW.expires_at
                       OR OLD.expired_at IS DISTINCT FROM NEW.expired_at
                       OR OLD.deleted_at IS NOT NULL OR NEW.deleted_at IS NULL
                       OR NEW.deleted_at < NEW.expired_at THEN
                        RAISE EXCEPTION 'Memory Source content deletion is not allowed'
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
    op.execute("DROP TRIGGER trg_memory_source_version_immutable ON memory_source_versions")
    op.execute(
        "CREATE TRIGGER trg_memory_source_version_immutable "
        "BEFORE INSERT OR UPDATE OR DELETE ON memory_source_versions "
        "FOR EACH ROW EXECUTE FUNCTION protect_memory_source_version()"
    )

    op.execute(
        """
        CREATE FUNCTION protect_memory_source_version_reference() RETURNS trigger AS $$
        DECLARE
            target_state text;
            target_digest text;
            frozen_reference jsonb;
            reference_is_valid boolean;
        BEGIN
            IF TG_OP = 'INSERT' THEN
                SELECT lifecycle_state, digest INTO target_state, target_digest
                FROM memory_source_versions
                WHERE id = NEW.memory_source_version_id;
                IF target_state != 'ACTIVE' THEN
                    RAISE EXCEPTION
                        'Memory Source references require an active governed version'
                        USING ERRCODE = 'check_violation';
                END IF;
                frozen_reference := jsonb_build_array(
                    jsonb_build_object(
                        'reference_id', NEW.memory_source_version_id,
                        'digest', target_digest
                    )
                );
                IF NEW.reference_kind = 'POLICY_VERSION' THEN
                    SELECT EXISTS (
                        SELECT 1
                        FROM capability_versions AS version
                        JOIN capability_packages AS package ON package.id = version.package_id
                        WHERE version.id = NEW.reference_id
                          AND package.capability_type = 'MEMORY_POLICY'
                          AND (version.normalized_config_json::jsonb -> 'source_refs')
                              @> frozen_reference
                    ) INTO reference_is_valid;
                ELSIF NEW.reference_kind = 'RUN_SNAPSHOT' THEN
                    SELECT EXISTS (
                        SELECT 1
                        FROM run_snapshots AS snapshot
                        CROSS JOIN LATERAL jsonb_each(
                            snapshot.runtime_manifest_json::jsonb -> 'nodes'
                        ) AS node
                        WHERE snapshot.id = NEW.reference_id
                          AND (
                              node.value -> 'agent_spec' -> 'memory_policy'
                              -> 'runtime_config' -> 'source_refs'
                          ) @> frozen_reference
                    ) INTO reference_is_valid;
                ELSE
                    reference_is_valid := false;
                END IF;
                IF NOT coalesce(reference_is_valid, false) THEN
                    RAISE EXCEPTION
                        'Memory Source reference does not match an immutable frozen object'
                        USING ERRCODE = 'check_violation';
                END IF;
                RETURN NEW;
            ELSE
                RAISE EXCEPTION 'Memory Source retention references are immutable'
                    USING ERRCODE = 'check_violation';
            END IF;
        END;
        $$ LANGUAGE plpgsql;
        CREATE TRIGGER trg_memory_source_version_reference_immutable
        BEFORE INSERT OR UPDATE OR DELETE ON memory_source_version_references
        FOR EACH ROW EXECUTE FUNCTION protect_memory_source_version_reference();
        """
    )


def downgrade() -> None:
    op.execute(
        """
        DO $$
        BEGIN
            IF EXISTS (
                SELECT 1 FROM memory_source_versions WHERE lifecycle_state = 'DELETED'
            ) THEN
                RAISE EXCEPTION
                    'Cannot downgrade after irreversible Memory Source content deletion';
            END IF;
        END;
        $$;
        """
    )
    op.execute(
        "DROP TRIGGER trg_memory_source_version_reference_immutable "
        "ON memory_source_version_references"
    )
    op.execute("DROP FUNCTION protect_memory_source_version_reference()")
    op.drop_table("memory_source_version_references")
    op.execute("DROP TRIGGER trg_memory_source_version_immutable ON memory_source_versions")
    op.execute("DROP FUNCTION protect_memory_source_version()")
    op.execute(
        "UPDATE memory_source_versions SET lifecycle_state = 'RETIRED' "
        "WHERE lifecycle_state = 'EXPIRED'"
    )
    op.drop_constraint(
        "ck_memory_source_version_retention_state",
        "memory_source_versions",
        type_="check",
    )
    op.drop_constraint(
        "ck_memory_source_version_retention_days",
        "memory_source_versions",
        type_="check",
    )
    op.drop_constraint(
        "ck_memory_source_version_lifecycle", "memory_source_versions", type_="check"
    )
    op.create_check_constraint(
        "ck_memory_source_version_lifecycle",
        "memory_source_versions",
        "lifecycle_state IN ('DRAFT', 'ACTIVE', 'RETIRED')",
    )
    op.drop_index("ix_memory_source_versions_expires_at", table_name="memory_source_versions")
    for column in ("deleted_at", "expired_at", "expires_at", "retention_days"):
        op.drop_column("memory_source_versions", column)

    op.execute(_GOVERNANCE_TRIGGER_SQL)
    op.execute(
        "CREATE TRIGGER trg_memory_source_version_immutable "
        "BEFORE UPDATE OR DELETE ON memory_source_versions "
        "FOR EACH ROW EXECUTE FUNCTION protect_memory_source_version()"
    )
