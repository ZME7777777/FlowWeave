from logging.config import fileConfig

from alembic import context
from sqlalchemy import engine_from_config, inspect, pool, text

from flowweave.bootstrap.settings import Settings
from flowweave.modules.catalog.infrastructure import models as catalog_models  # noqa: F401
from flowweave.modules.conversations.infrastructure import (
    models as conversation_models,  # noqa: F401
)
from flowweave.modules.environments.infrastructure import models as environment_models  # noqa: F401
from flowweave.modules.flows.infrastructure import models as flow_models  # noqa: F401
from flowweave.modules.model_providers.infrastructure import models as provider_models  # noqa: F401
from flowweave.modules.runs.infrastructure import models as run_models  # noqa: F401
from flowweave.modules.sandboxes.infrastructure import models as sandbox_models  # noqa: F401
from flowweave.modules.tasks.infrastructure import models as task_models  # noqa: F401
from flowweave.shared.database import Base

config = context.config
settings = Settings()
database_url = config.attributes.get("database_url", settings.database_url)
if not str(database_url).startswith("postgresql+psycopg://"):
    raise RuntimeError("FlowWeave migrations support PostgreSQL through psycopg only")
config.set_main_option("sqlalchemy.url", str(database_url))
if config.config_file_name:
    fileConfig(config.config_file_name)
target_metadata = Base.metadata

_LEGACY_RUNTIME_LINEAGE = "0054_binding_schema"
_RUNTIME_REFACTOR_BASELINE = "0051_physical_delete"


def _bridge_legacy_runtime_lineage(connection) -> None:
    """Map the superseded Runtime draft onto the FR migration baseline.

    The pre-FR Runtime draft ended at ``0054_binding_schema``.  FR-01 starts
    from the same durable business-resource schema but replaces that draft's
    Conversation Runtime projection wholesale.  Alembic cannot infer this
    relationship after the old revisions leave the tree, so only this exact,
    schema-verified historical head is mapped.  Its projections are retained
    as read-only archives before the new FlowRun Runtime migrations run.
    """

    # SQLAlchemy 2 inspection starts an implicit transaction.  Finish that
    # transaction here even when no bridge is needed; otherwise Alembic's
    # migration transaction is nested in it and the connection close rolls an
    # otherwise successful empty/current database upgrade back wholesale.
    with connection.begin():
        tables = set(inspect(connection).get_table_names())
        if "alembic_version" not in tables:
            return
        revisions = {
            str(row[0])
            for row in connection.execute(text("SELECT version_num FROM alembic_version"))
        }
        if revisions != {_LEGACY_RUNTIME_LINEAGE}:
            return
        required_tables = {
            "conversation_runtime_bindings",
            "runtime_provisioning_attempts",
            "agent_conversations",
            "flow_runs",
        }
        forbidden_tables = {
            "flow_run_runtime_allocations",
            "flow_run_runtimes",
            "flow_run_conversation_bindings",
        }
        if not required_tables <= tables or forbidden_tables & tables:
            raise RuntimeError(
                "legacy Runtime revision 0054_binding_schema does not match the supported "
                "FlowWeave schema; restore a supported backup before upgrading"
            )

        connection.execute(
            text(
                "ALTER TABLE conversation_runtime_bindings "
                "RENAME TO archived_conversation_runtime_bindings"
            )
        )
        connection.execute(
            text(
                "ALTER TABLE runtime_provisioning_attempts "
                "RENAME TO archived_runtime_provisioning_attempts"
            )
        )
        connection.execute(
            text("UPDATE alembic_version SET version_num = :revision"),
            {"revision": _RUNTIME_REFACTOR_BASELINE},
        )


def run_migrations_offline() -> None:
    context.configure(
        url=config.get_main_option("sqlalchemy.url"),
        target_metadata=target_metadata,
        literal_binds=True,
        compare_type=True,
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    connectable = engine_from_config(
        config.get_section(config.config_ini_section), prefix="sqlalchemy.", poolclass=pool.NullPool
    )
    with connectable.connect() as connection:
        _bridge_legacy_runtime_lineage(connection)
        context.configure(connection=connection, target_metadata=target_metadata, compare_type=True)
        with context.begin_transaction():
            context.run_migrations()


run_migrations_offline() if context.is_offline_mode() else run_migrations_online()
