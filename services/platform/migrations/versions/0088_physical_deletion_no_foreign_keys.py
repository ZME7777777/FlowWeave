"""Use physical deletion and application-owned reference integrity.

Revision ID: 0088_physical_delete_no_fks
Revises: 0087_nested_automatic_runs
Create Date: 2026-09-02
"""

from alembic import op

revision = "0088_physical_delete_no_fks"
down_revision = "0087_nested_automatic_runs"
branch_labels = None
depends_on = None


_FOREIGN_KEY_ARCHIVE = "migration_foreign_keys_0088"


def upgrade() -> None:
    # Remove old Conversation tombstones as complete application-owned graphs.
    # Background tasks have no FK and therefore need an explicit cleanup too.
    op.execute(
        """
        DELETE FROM runtime_confirmation_approvals
        WHERE flow_run_conversation_binding_id IN (
            SELECT id FROM agent_conversation_bindings WHERE lifecycle = 'DELETED'
        );
        DELETE FROM background_tasks
        WHERE aggregate_id IN (
            SELECT id FROM agent_conversation_bindings WHERE lifecycle = 'DELETED'
        );
        DELETE FROM agent_conversation_message_attachments
        WHERE binding_id IN (
            SELECT id FROM agent_conversation_bindings WHERE lifecycle = 'DELETED'
        );
        DELETE FROM agent_conversation_capabilities
        WHERE binding_id IN (
            SELECT id FROM agent_conversation_bindings WHERE lifecycle = 'DELETED'
        );
        DELETE FROM agent_conversation_commands
        WHERE binding_id IN (
            SELECT id FROM agent_conversation_bindings WHERE lifecycle = 'DELETED'
        );
        DELETE FROM agent_conversation_bindings WHERE lifecycle = 'DELETED';
        """
    )

    # An archived directory used to remain as a grouping tombstone. Conversations
    # retain their frozen concrete working_directory, so detach only the obsolete
    # grouping reference before removing the directory graph.
    op.execute(
        """
        UPDATE agent_conversation_bindings AS binding
        SET work_directory_version_id = NULL
        WHERE binding.work_directory_version_id IN (
            SELECT version.id
            FROM agent_work_directory_versions AS version
            JOIN agent_work_directories AS directory
              ON directory.id = version.work_directory_id
            WHERE directory.state = 'ARCHIVED'
        );
        DELETE FROM agent_work_directory_paths
        WHERE version_id IN (
            SELECT version.id
            FROM agent_work_directory_versions AS version
            JOIN agent_work_directories AS directory
              ON directory.id = version.work_directory_id
            WHERE directory.state = 'ARCHIVED'
        );
        DELETE FROM agent_work_directory_versions
        WHERE work_directory_id IN (
            SELECT id FROM agent_work_directories WHERE state = 'ARCHIVED'
        );
        DELETE FROM agent_work_directories WHERE state = 'ARCHIVED';
        """
    )

    op.drop_constraint(
        "ck_agent_conversation_lifecycle",
        "agent_conversation_bindings",
        type_="check",
    )
    op.create_check_constraint(
        "ck_agent_conversation_lifecycle",
        "agent_conversation_bindings",
        "lifecycle IN ('PROVISIONING', 'ACTIVE', 'DELETE_PENDING', 'FAILED')",
    )
    op.drop_column("agent_conversation_bindings", "deleted_at")

    op.drop_constraint(
        "ck_agent_work_directory_state",
        "agent_work_directories",
        type_="check",
    )
    op.drop_index("ix_agent_work_directories_state", table_name="agent_work_directories")
    op.drop_column("agent_work_directories", "archived_at")
    op.drop_column("agent_work_directories", "state")

    # Keep exact PostgreSQL definitions solely so an immediate Alembic downgrade
    # can restore the previous schema. This table is not a business reference
    # registry and intentionally contains no foreign keys itself.
    op.execute(
        f"""
        CREATE TABLE {_FOREIGN_KEY_ARCHIVE} (
            schema_name TEXT NOT NULL,
            table_name TEXT NOT NULL,
            constraint_name TEXT NOT NULL,
            definition TEXT NOT NULL,
            PRIMARY KEY (schema_name, table_name, constraint_name)
        );
        INSERT INTO {_FOREIGN_KEY_ARCHIVE} (
            schema_name, table_name, constraint_name, definition
        )
        SELECT namespace.nspname, relation.relname, fk.conname,
               pg_get_constraintdef(fk.oid, true)
        FROM pg_constraint AS fk
        JOIN pg_class AS relation ON relation.oid = fk.conrelid
        JOIN pg_namespace AS namespace ON namespace.oid = relation.relnamespace
        WHERE fk.contype = 'f' AND namespace.nspname = 'public';

        DO $$
        DECLARE item RECORD;
        BEGIN
            FOR item IN
                SELECT schema_name, table_name, constraint_name
                FROM {_FOREIGN_KEY_ARCHIVE}
                ORDER BY schema_name, table_name, constraint_name
            LOOP
                EXECUTE format(
                    'ALTER TABLE %I.%I DROP CONSTRAINT %I',
                    item.schema_name, item.table_name, item.constraint_name
                );
            END LOOP;
        END $$;
        """
    )


def downgrade() -> None:
    op.execute(
        f"""
        DO $$
        DECLARE item RECORD;
        BEGIN
            FOR item IN
                SELECT schema_name, table_name, constraint_name, definition
                FROM {_FOREIGN_KEY_ARCHIVE}
                ORDER BY schema_name, table_name, constraint_name
            LOOP
                EXECUTE format(
                    'ALTER TABLE %I.%I ADD CONSTRAINT %I %s',
                    item.schema_name, item.table_name, item.constraint_name, item.definition
                );
            END LOOP;
        END $$;
        DROP TABLE {_FOREIGN_KEY_ARCHIVE};
        """
    )

    op.add_column(
        "agent_work_directories",
        __import__("sqlalchemy").Column(
            "state", __import__("sqlalchemy").String(20), nullable=False, server_default="ACTIVE"
        ),
    )
    op.add_column(
        "agent_work_directories",
        __import__("sqlalchemy").Column(
            "archived_at", __import__("sqlalchemy").DateTime(timezone=True), nullable=True
        ),
    )
    op.create_index("ix_agent_work_directories_state", "agent_work_directories", ["state"])
    op.create_check_constraint(
        "ck_agent_work_directory_state",
        "agent_work_directories",
        "state IN ('ACTIVE', 'ARCHIVED')",
    )

    op.add_column(
        "agent_conversation_bindings",
        __import__("sqlalchemy").Column(
            "deleted_at", __import__("sqlalchemy").DateTime(timezone=True), nullable=True
        ),
    )
    op.drop_constraint(
        "ck_agent_conversation_lifecycle",
        "agent_conversation_bindings",
        type_="check",
    )
    op.create_check_constraint(
        "ck_agent_conversation_lifecycle",
        "agent_conversation_bindings",
        "lifecycle IN ('PROVISIONING', 'ACTIVE', 'DELETE_PENDING', 'DELETED', 'FAILED')",
    )
