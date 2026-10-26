"""Workspace hosts are shared; session credential defaults belong to the user."""

from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import text

from flowweave.modules.agent_sessions.application import conversations
from flowweave.modules.agent_workspaces.infrastructure.models import AgentWorkspace
from flowweave.modules.credentials.application.service import save_credential
from flowweave.modules.users.application.security import (
    FLOWWEAVE_USER_ID,
    USER_USER_ID,
    tenant_user,
)
from flowweave.shared.errors import DomainError
from flowweave.shared.schemas import WebsiteCredentialWrite
from flowweave.shared.settings import settings_context


def test_shared_workspace_defaults_are_user_isolated(settings, db_session_factory):
    with settings_context(settings), tenant_user(FLOWWEAVE_USER_ID), db_session_factory() as db:
        workspace = AgentWorkspace(scope_key="session-defaults")
        db.add(workspace)
        db.flush()
        own = save_credential(
            db,
            WebsiteCredentialWrite(
                name="own", target_host="example.test", auth_type="TOKEN", secret="test-token"
            ),
        )["id"]
        assert conversations.workspace_session_configuration(db, workspace.id) == {
            "capability_version_ids": [],
            "credential_ids": [own],
        }
        assert conversations._workspace_credential_defaults(db, workspace.id) is None
        conversations.replace_workspace_session_configuration(db, workspace.id, (), ())
        db.commit()

    with settings_context(settings), tenant_user(USER_USER_ID), db_session_factory() as db:
        other = save_credential(
            db,
            WebsiteCredentialWrite(
                name="other", target_host="example.test", auth_type="TOKEN", secret="other-token"
            ),
        )["id"]
        assert conversations.workspace_session_configuration(db, workspace.id)[
            "credential_ids"
        ] == [other]
        with pytest.raises(DomainError):
            with db.begin_nested():
                conversations.replace_workspace_session_configuration(db, workspace.id, (), (own,))
        saved = conversations.replace_workspace_session_configuration(
            db, workspace.id, (), (other, other)
        )
        assert saved["credential_ids"] == [other]
        assert conversations._workspace_credential_defaults(db, workspace.id) == (other,)
        db.commit()

    with settings_context(settings), tenant_user(FLOWWEAVE_USER_ID), db_session_factory() as db:
        assert (
            conversations.workspace_session_configuration(db, workspace.id)["credential_ids"] == []
        )
        assert conversations._workspace_credential_defaults(db, workspace.id) == ()
        conversations.replace_workspace_session_configuration(db, workspace.id, (), (own,))
        db.commit()
    with settings_context(settings), tenant_user(USER_USER_ID), db_session_factory() as db:
        assert conversations.workspace_session_configuration(db, workspace.id)[
            "credential_ids"
        ] == [other]


def test_workspace_defaults_postgres_rls_enforces_reads_and_writes(db_session_factory):
    engine = db_session_factory.kw["bind"]
    table = "agent_workspace_session_configurations"
    with engine.connect() as connection, connection.begin():
        assert all(
            connection.execute(
                text(
                    "SELECT relrowsecurity, relforcerowsecurity FROM pg_class WHERE relname=:table"
                ),
                {"table": table},
            ).one()
        )
        for index, user in enumerate((FLOWWEAVE_USER_ID, USER_USER_ID)):
            connection.execute(
                text(
                    f"INSERT INTO {table} "
                    "(id, owner_user_id, workspace_id, credential_ids_json, updated_at) "
                    "VALUES (:id, :owner, 'shared', '[]', now())"
                ),
                {"id": f"rls-{index}", "owner": user},
            )
        connection.execute(text("CREATE ROLE fr700_defaults_probe NOLOGIN"))
        connection.execute(text("GRANT USAGE ON SCHEMA public TO fr700_defaults_probe"))
        connection.execute(text(f"GRANT SELECT, INSERT, UPDATE ON {table} TO fr700_defaults_probe"))
        connection.execute(text("SET LOCAL ROLE fr700_defaults_probe"))
        connection.execute(text("SELECT set_config('flowweave.bypass', 'off', true)"))
        connection.execute(
            text("SELECT set_config('flowweave.user_id', :user, true)"), {"user": USER_USER_ID}
        )
        assert connection.execute(text(f"SELECT id FROM {table}")).scalars().all() == ["rls-1"]
        assert (
            connection.execute(
                text(f"UPDATE {table} SET credential_ids_json='[]' WHERE id='rls-0'")
            ).rowcount
            == 0
        )
        with pytest.raises(Exception, match="row-level security"):
            with connection.begin_nested():
                connection.execute(
                    text(
                        f"INSERT INTO {table} "
                        "(id, owner_user_id, workspace_id, credential_ids_json, updated_at) "
                        "VALUES ('forged', :owner, 'other', '[]', now())"
                    ),
                    {"owner": FLOWWEAVE_USER_ID},
                )
        # Roll back this test's role, grants and rows together.
        connection.rollback()


def test_workspace_defaults_migration_refuses_to_discard_saved_configuration(
    settings, db_session_factory
):
    config = Config(str(Path(__file__).parents[1] / "alembic.ini"))
    config.attributes["database_url"] = settings.database_url
    engine = db_session_factory.kw["bind"]
    table = "agent_workspace_session_configurations"
    command.downgrade(config, "0173_agent_workspace_session_configuration")
    command.upgrade(config, "head")
    with engine.begin() as connection:
        connection.execute(
            text(
                f"INSERT INTO {table} "
                "(id, owner_user_id, workspace_id, credential_ids_json, updated_at) "
                "VALUES ('keep', :owner, 'shared', '[]', now())"
            ),
            {"owner": FLOWWEAVE_USER_ID},
        )
    with pytest.raises(RuntimeError, match="Cannot discard persisted"):
        command.downgrade(config, "0173_agent_workspace_session_configuration")
    with engine.connect() as connection:
        assert connection.execute(text(f"SELECT id FROM {table}")).scalar_one() == "keep"
        assert connection.execute(text("SELECT version_num FROM alembic_version")).scalar_one() == (
            "0174_agent_workspace_user_defaults"
        )
