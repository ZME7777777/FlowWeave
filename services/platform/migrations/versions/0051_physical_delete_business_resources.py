"""replace business-resource soft deletion with physical deletion

Revision ID: 0051_physical_delete
Revises: 0050_remove_node_environment
"""

import sqlalchemy as sa
from alembic import op

revision = "0051_physical_delete"
down_revision = "0050_remove_node_environment"
branch_labels = None
depends_on = None


_MEMORY_SOURCE_TRIGGER_SQL = """
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
           OR NEW.expired_at IS NOT NULL THEN
            RAISE EXCEPTION 'Invalid initial Memory Source governance state'
                USING ERRCODE = 'check_violation';
        END IF;
        RETURN NEW;
    END IF;

    IF TG_OP = 'DELETE' THEN
        IF OLD.lifecycle_state != 'EXPIRED'
           OR EXISTS (
               SELECT 1 FROM memory_source_version_references
               WHERE memory_source_version_id = OLD.id
           )
           OR EXISTS (
               SELECT 1 FROM memory_source_versions
               WHERE previous_version_id = OLD.id
           ) THEN
            RAISE EXCEPTION
                'Only unreferenced terminal Memory Source versions can be physically deleted'
                USING ERRCODE = 'check_violation';
        END IF;
        RETURN OLD;
    END IF;

    IF OLD.source_id IS DISTINCT FROM NEW.source_id
       OR OLD.previous_version_id IS DISTINCT FROM NEW.previous_version_id
       OR OLD.version_no IS DISTINCT FROM NEW.version_no
       OR OLD.content IS DISTINCT FROM NEW.content
       OR OLD.digest IS DISTINCT FROM NEW.digest
       OR OLD.byte_size IS DISTINCT FROM NEW.byte_size
       OR OLD.created_at IS DISTINCT FROM NEW.created_at THEN
        RAISE EXCEPTION 'Memory Source version identity and content are immutable'
            USING ERRCODE = 'check_violation';
    END IF;

    changed_steps :=
        (CASE WHEN OLD.review_status IS DISTINCT FROM NEW.review_status THEN 1 ELSE 0 END) +
        (CASE WHEN OLD.sensitive_data_status IS DISTINCT FROM NEW.sensitive_data_status
            THEN 1 ELSE 0 END) +
        (CASE WHEN OLD.lifecycle_state IS DISTINCT FROM NEW.lifecycle_state THEN 1 ELSE 0 END);
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
           OR OLD.retired_at IS DISTINCT FROM NEW.retired_at
           OR OLD.retention_days IS DISTINCT FROM NEW.retention_days
           OR OLD.expires_at IS DISTINCT FROM NEW.expires_at
           OR OLD.expired_at IS DISTINCT FROM NEW.expired_at THEN
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
           OR OLD.retired_at IS DISTINCT FROM NEW.retired_at
           OR OLD.retention_days IS DISTINCT FROM NEW.retention_days
           OR OLD.expires_at IS DISTINCT FROM NEW.expires_at
           OR OLD.expired_at IS DISTINCT FROM NEW.expired_at THEN
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
            RAISE EXCEPTION 'Memory Source lifecycle cannot alter governance evidence'
                USING ERRCODE = 'check_violation';
        END IF;
        IF OLD.lifecycle_state = 'DRAFT' AND NEW.lifecycle_state = 'ACTIVE' THEN
            IF OLD.review_status != 'APPROVED' OR OLD.sensitive_data_status != 'PASSED'
               OR OLD.activated_at IS NOT NULL OR NEW.activated_at IS NULL
               OR OLD.retention_days IS NOT NULL OR NEW.retention_days IS NULL
               OR OLD.retired_at IS DISTINCT FROM NEW.retired_at
               OR OLD.expires_at IS DISTINCT FROM NEW.expires_at
               OR OLD.expired_at IS DISTINCT FROM NEW.expired_at THEN
                RAISE EXCEPTION 'Invalid Memory Source activation transition'
                    USING ERRCODE = 'check_violation';
            END IF;
        ELSIF OLD.lifecycle_state = 'ACTIVE' AND NEW.lifecycle_state = 'RETIRED' THEN
            IF OLD.activated_at IS NULL
               OR OLD.activated_at IS DISTINCT FROM NEW.activated_at
               OR OLD.retention_days IS NULL
               OR OLD.retention_days IS DISTINCT FROM NEW.retention_days
               OR OLD.retired_at IS NOT NULL OR NEW.retired_at IS NULL
               OR NEW.retired_at < NEW.activated_at
               OR OLD.expires_at IS NOT NULL OR NEW.expires_at IS NULL
               OR NEW.expires_at != NEW.retired_at
                   + make_interval(days => NEW.retention_days)
               OR OLD.expired_at IS DISTINCT FROM NEW.expired_at THEN
                RAISE EXCEPTION 'Invalid Memory Source retirement transition'
                    USING ERRCODE = 'check_violation';
            END IF;
        ELSIF OLD.lifecycle_state = 'RETIRED' AND NEW.lifecycle_state = 'EXPIRED' THEN
            IF OLD.activated_at IS DISTINCT FROM NEW.activated_at
               OR OLD.retired_at IS DISTINCT FROM NEW.retired_at
               OR OLD.retention_days IS DISTINCT FROM NEW.retention_days
               OR OLD.expires_at IS NULL OR OLD.expires_at IS DISTINCT FROM NEW.expires_at
               OR CURRENT_TIMESTAMP < OLD.expires_at
               OR OLD.expired_at IS NOT NULL OR NEW.expired_at IS NULL
               OR NEW.expired_at < NEW.expires_at THEN
                RAISE EXCEPTION 'Memory Source retention period has not ended'
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
    # Rows hidden by the former soft-delete contract are not user-visible and
    # must not survive the switch to existence-based reads. Child rows use
    # database cascades; legacy flow cleanup was already normalized by 0019.
    inspector = sa.inspect(op.get_bind())
    columns_by_table = {
        table: {column["name"] for column in inspector.get_columns(table)}
        for table in (
            "node_assets",
            "flow_definitions",
            "terminal_environments",
            "managed_sandboxes",
            "memory_source_versions",
        )
    }
    for table in ("node_assets", "flow_definitions", "terminal_environments"):
        if "deleted_at" in columns_by_table[table]:
            op.execute(sa.text(f"DELETE FROM {table} WHERE deleted_at IS NOT NULL"))

    if "deleted_at" in columns_by_table["managed_sandboxes"]:
        op.execute(sa.text("DELETE FROM managed_sandboxes WHERE deleted_at IS NOT NULL"))

    op.execute(
        "DROP TRIGGER IF EXISTS trg_memory_source_version_immutable ON memory_source_versions"
    )
    op.execute("DROP FUNCTION IF EXISTS protect_memory_source_version()")
    op.execute(
        """
        DO $$
        BEGIN
            IF EXISTS (
                SELECT 1
                FROM memory_source_versions AS version
                WHERE version.lifecycle_state = 'DELETED'
                  AND (
                      EXISTS (
                          SELECT 1 FROM memory_source_version_references AS reference
                          WHERE reference.memory_source_version_id = version.id
                      )
                      OR EXISTS (
                          SELECT 1 FROM memory_source_versions AS successor
                          WHERE successor.previous_version_id = version.id
                      )
                  )
            ) THEN
                RAISE EXCEPTION
                    'Cannot physically migrate referenced Memory Source tombstones';
            END IF;
        END;
        $$;
        DELETE FROM memory_source_versions WHERE lifecycle_state = 'DELETED';
        """
    )
    op.drop_constraint(
        "ck_memory_source_version_retention_state",
        "memory_source_versions",
        type_="check",
    )
    op.create_check_constraint(
        "ck_memory_source_version_retention_state",
        "memory_source_versions",
        "(lifecycle_state = 'DRAFT' AND content <> '' AND retention_days IS NULL "
        "AND activated_at IS NULL AND retired_at IS NULL AND expires_at IS NULL "
        "AND expired_at IS NULL) OR "
        "(lifecycle_state = 'ACTIVE' AND content <> '' AND retention_days IS NOT NULL "
        "AND activated_at IS NOT NULL AND retired_at IS NULL AND expires_at IS NULL "
        "AND expired_at IS NULL) OR "
        "(lifecycle_state = 'RETIRED' AND content <> '' AND retention_days IS NOT NULL "
        "AND activated_at IS NOT NULL AND retired_at >= activated_at "
        "AND expires_at = retired_at + make_interval(days => retention_days) "
        "AND expired_at IS NULL) OR "
        "(lifecycle_state = 'EXPIRED' AND content <> '' AND retention_days IS NOT NULL "
        "AND activated_at IS NOT NULL AND retired_at >= activated_at "
        "AND expires_at = retired_at + make_interval(days => retention_days) "
        "AND expired_at >= expires_at)",
    )
    op.drop_constraint(
        "ck_memory_source_version_lifecycle", "memory_source_versions", type_="check"
    )
    op.create_check_constraint(
        "ck_memory_source_version_lifecycle",
        "memory_source_versions",
        "lifecycle_state IN ('DRAFT', 'ACTIVE', 'RETIRED', 'EXPIRED')",
    )
    op.execute(_MEMORY_SOURCE_TRIGGER_SQL)
    op.execute(
        "CREATE TRIGGER trg_memory_source_version_immutable "
        "BEFORE INSERT OR UPDATE OR DELETE ON memory_source_versions "
        "FOR EACH ROW EXECUTE FUNCTION protect_memory_source_version()"
    )

    node_indexes = {
        item["name"] for item in inspector.get_indexes("node_assets") if item.get("name")
    }
    if "uq_asset_active_directory_name" in node_indexes:
        op.drop_index("uq_asset_active_directory_name", table_name="node_assets")
    node_constraints = {
        item["name"] for item in inspector.get_unique_constraints("node_assets") if item.get("name")
    }
    if "uq_asset_directory_name" not in node_constraints:
        op.create_unique_constraint(
            "uq_asset_directory_name",
            "node_assets",
            ["directory_id", "name"],
            postgresql_nulls_not_distinct=True,
        )

    for table in (
        "node_assets",
        "flow_definitions",
        "terminal_environments",
        "managed_sandboxes",
        "memory_source_versions",
    ):
        if "deleted_at" in columns_by_table[table]:
            op.drop_column(table, "deleted_at")


def downgrade() -> None:
    # Physical deletion is intentionally not reversible. Restore only the old
    # schema shape for callers that must run the previous application version.
    inspector = sa.inspect(op.get_bind())
    for table in (
        "memory_source_versions",
        "managed_sandboxes",
        "terminal_environments",
        "flow_definitions",
        "node_assets",
    ):
        columns = {column["name"] for column in inspector.get_columns(table)}
        if "deleted_at" not in columns:
            op.add_column(table, sa.Column("deleted_at", sa.DateTime(timezone=True)))

    op.execute(
        "DROP TRIGGER IF EXISTS trg_memory_source_version_immutable ON memory_source_versions"
    )
    op.execute("DROP FUNCTION IF EXISTS protect_memory_source_version()")
    op.drop_constraint(
        "ck_memory_source_version_retention_state",
        "memory_source_versions",
        type_="check",
    )
    op.create_check_constraint(
        "ck_memory_source_version_retention_state",
        "memory_source_versions",
        "(lifecycle_state = 'DRAFT' AND content <> '' AND retention_days IS NULL "
        "AND activated_at IS NULL AND retired_at IS NULL AND expires_at IS NULL "
        "AND expired_at IS NULL AND deleted_at IS NULL) OR "
        "(lifecycle_state = 'ACTIVE' AND content <> '' AND retention_days IS NOT NULL "
        "AND activated_at IS NOT NULL AND retired_at IS NULL AND expires_at IS NULL "
        "AND expired_at IS NULL AND deleted_at IS NULL) OR "
        "(lifecycle_state = 'RETIRED' AND content <> '' AND retention_days IS NOT NULL "
        "AND activated_at IS NOT NULL AND retired_at >= activated_at "
        "AND expires_at = retired_at + make_interval(days => retention_days) "
        "AND expired_at IS NULL AND deleted_at IS NULL) OR "
        "(lifecycle_state = 'EXPIRED' AND content <> '' AND retention_days IS NOT NULL "
        "AND activated_at IS NOT NULL AND retired_at >= activated_at "
        "AND expires_at = retired_at + make_interval(days => retention_days) "
        "AND expired_at >= expires_at AND deleted_at IS NULL) OR "
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
    # Reuse the historical revision's full trigger contract so downgrade really
    # restores the former tombstone behavior instead of leaving no protection.
    from importlib import import_module

    retention = import_module("migrations.versions.0044_memory_source_retention")
    op.execute(retention._GOVERNANCE_TRIGGER_SQL)
    op.execute(
        "CREATE TRIGGER trg_memory_source_version_immutable "
        "BEFORE UPDATE OR DELETE ON memory_source_versions "
        "FOR EACH ROW EXECUTE FUNCTION protect_memory_source_version()"
    )
    constraints = {
        item["name"]
        for item in sa.inspect(op.get_bind()).get_unique_constraints("node_assets")
        if item.get("name")
    }
    if "uq_asset_directory_name" in constraints:
        op.drop_constraint("uq_asset_directory_name", "node_assets", type_="unique")
    indexes = {
        item["name"]
        for item in sa.inspect(op.get_bind()).get_indexes("node_assets")
        if item.get("name")
    }
    if "uq_asset_active_directory_name" in indexes:
        return
    op.create_index(
        "uq_asset_active_directory_name",
        "node_assets",
        ["directory_id", "name"],
        unique=True,
        postgresql_where=sa.text("deleted_at IS NULL"),
        postgresql_nulls_not_distinct=True,
    )
