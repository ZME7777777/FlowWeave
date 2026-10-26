"""Workspace hosts are shared; session credential defaults belong to the user."""

from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from alembic.script import ScriptDirectory
from sqlalchemy import text

from flowweave.modules.agent_sessions.application import conversations
from flowweave.modules.agent_workspaces.infrastructure.models import (
    AgentWorkDirectory,
    AgentWorkspace,
)
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
            ScriptDirectory.from_config(config).get_current_head()
        )


def test_named_workspace_defaults_are_separate_and_reject_other_users(
    settings, db_session_factory
):
    with settings_context(settings), tenant_user(FLOWWEAVE_USER_ID), db_session_factory() as db:
        workspace = AgentWorkspace(scope_key="directory-session-defaults")
        db.add(workspace)
        db.flush()
        first = AgentWorkDirectory(workspace_id=workspace.id, display_name="first")
        second = AgentWorkDirectory(workspace_id=workspace.id, display_name="second")
        db.add_all([first, second])
        own = save_credential(
            db,
            WebsiteCredentialWrite(
                name="own", target_host="example.test", auth_type="TOKEN", secret="test-token"
            ),
        )["id"]
        conversations.replace_workspace_session_configuration(db, workspace.id, (), ())
        assert conversations.workspace_session_configuration(db, workspace.id, first.id)[
            "credential_ids"
        ] == [own]
        conversations.replace_workspace_session_configuration(db, workspace.id, (), (), first.id)
        conversations.replace_workspace_session_configuration(
            db, workspace.id, (), (own, own), second.id
        )
        assert (
            conversations.workspace_session_configuration(db, workspace.id)["credential_ids"] == []
        )
        assert conversations.workspace_session_configuration(db, workspace.id, first.id)[
            "credential_ids"
        ] == []
        assert conversations.workspace_session_configuration(db, workspace.id, second.id)[
            "credential_ids"
        ] == [own]
        db.commit()

    with settings_context(settings), tenant_user(USER_USER_ID), db_session_factory() as db:
        for directory_id in (first.id, second.id, "missing"):
            for write in (False, True):
                with pytest.raises(DomainError, match="工作区不存在"):
                    with db.begin_nested():
                        if write:
                            conversations.replace_workspace_session_configuration(
                                db, workspace.id, (), (), directory_id
                            )
                        else:
                            conversations.workspace_session_configuration(
                                db, workspace.id, directory_id
                            )


def test_merge_defaults_upgrade_preserves_deployed_user_configuration(settings, db_session_factory):
    config = Config(str(Path(__file__).parents[1] / "alembic.ini"))
    config.attributes["database_url"] = settings.database_url
    engine = db_session_factory.kw["bind"]
    command.downgrade(config, "0173_agent_workspace_session_configuration")
    command.upgrade(config, "0174_agent_workspace_user_defaults")
    with engine.begin() as connection:
        connection.execute(
            text(
                "INSERT INTO agent_workspace_session_configurations "
                "(id, owner_user_id, workspace_id, credential_ids_json, updated_at) "
                "VALUES ('existing-default', :owner, 'shared', '[\"credential\"]', now())"
            ),
            {"owner": FLOWWEAVE_USER_ID},
        )
        connection.execute(
            text(
                "INSERT INTO agent_work_directories "
                "(id, owner_user_id, workspace_id, display_name, current_version, row_version, "
                "created_at, updated_at) VALUES "
                "('existing-directory', :owner, 'shared', 'existing', 1, 1, now(), now())"
            ),
            {"owner": FLOWWEAVE_USER_ID},
        )
    command.upgrade(config, "head")
    with engine.connect() as connection:
        assert connection.execute(
            text(
                "SELECT credential_ids_json FROM agent_workspace_session_configurations "
                "WHERE id='existing-default'"
            )
        ).scalar_one() == ["credential"]
        assert connection.execute(
            text(
                "SELECT session_capability_ids_json, session_credential_ids_json "
                "FROM agent_work_directories WHERE id='existing-directory'"
            )
        ).one() == ([], None)
        assert connection.execute(text("SELECT version_num FROM alembic_version")).scalar_one() == (
            ScriptDirectory.from_config(config).get_current_head()
        )
