"""Profile sources and native Git wiring without external services."""

from types import SimpleNamespace

import pytest
from sqlalchemy import create_engine, event
from sqlalchemy.orm import Session

from flowweave.modules.agent_workspaces.application import git_settings, workspace
from flowweave.modules.users.application.security import tenant_user
from flowweave.modules.users.infrastructure.models import User
from flowweave.runtime.base import RuntimeHandle
from flowweave.shared.errors import DomainError


@pytest.fixture
def profiles():
    engine = create_engine("sqlite://")

    @event.listens_for(engine, "connect")
    def sqlite_set_config(connection, _record):
        connection.create_function("set_config", 3, lambda _key, value, _local: value)

    User.__table__.create(engine)
    with Session(engine) as db:
        db.add_all(
            [
                User(
                    id="local",
                    username="login-account",
                    display_name="本地姓名",
                    email="local@example.test",
                    auth_source="LOCAL",
                    role="SUPER_ADMIN",
                ),
                User(
                    id="ldap",
                    username="ldap-login",
                    display_name="LDAP 姓名",
                    email="ldap@example.test",
                    auth_source="LDAP",
                    role="USER",
                ),
            ]
        )
        db.flush()
        yield db
    engine.dispose()


def test_profile_git_author_uses_name_column_and_is_scoped_to_current_user(profiles):
    for user, expected in [
        ("local", ("本地姓名", "local@example.test")),
        ("ldap", ("LDAP 姓名", "ldap@example.test")),
    ]:
        with tenant_user(user):
            assert git_settings.profile_identity(profiles) == expected


@pytest.mark.parametrize(
    "name,email",
    [
        (None, "valid@example.test"),
        ("Name", None),
        ("", "a@example.test"),
        ("Name", "invalid"),
        ("bad\nname", "a@example.test"),
    ],
)
def test_incomplete_profile_never_substitutes_login_username(profiles, name, email):
    user = profiles.get(User, "local")
    user.display_name, user.email = name, email
    with tenant_user("local"):
        assert git_settings.profile_identity(profiles) is None


def test_profile_is_written_through_native_adapter_before_conversation_work(profiles, monkeypatch):
    calls = []
    monkeypatch.setattr(workspace, "ensure_user_project_root", lambda *args: None)
    runtime = SimpleNamespace(
        workspace_git_identity=lambda handle, **kwargs: calls.append(
            (handle.workspace_root, kwargs)
        )
    )
    monkeypatch.setattr(git_settings, "get_runtime", lambda: runtime)
    with tenant_user("ldap"):
        git_settings.apply_profile_identity(
            profiles,
            "shared",
            RuntimeHandle("env-exec:runtime", "", workspace_root="ldap-user-root"),
        )
    assert calls == [("ldap-user-root", {"profile": ("LDAP 姓名", "ldap@example.test")})]


def test_profile_sync_failure_does_not_block_ordinary_agent_work(profiles, monkeypatch, caplog):
    monkeypatch.setattr(workspace, "ensure_user_project_root", lambda *args: None)

    def unavailable(*_args, **_kwargs):
        raise DomainError(
            "AGENT_GIT_CONFIG_UNAVAILABLE", "无法确认工作区 Git 配置，请稍后重试", 503
        )

    monkeypatch.setattr(
        git_settings,
        "get_runtime",
        lambda: SimpleNamespace(workspace_git_identity=unavailable),
    )
    with tenant_user("ldap"), caplog.at_level("WARNING"):
        git_settings.apply_profile_identity(
            profiles,
            "shared",
            RuntimeHandle("env-exec:runtime", "", workspace_root="ldap-user-root"),
        )

    assert "agent_workspace_git_profile_sync_unavailable" in caplog.text


def test_profile_sync_propagates_unrelated_runtime_failures(profiles, monkeypatch):
    monkeypatch.setattr(workspace, "ensure_user_project_root", lambda *args: None)

    def unavailable(*_args, **_kwargs):
        raise DomainError("EXECUTOR_UNAVAILABLE", "OpenHands 不可用", 503)

    monkeypatch.setattr(
        git_settings,
        "get_runtime",
        lambda: SimpleNamespace(workspace_git_identity=unavailable),
    )
    with tenant_user("ldap"), pytest.raises(DomainError, match="OpenHands 不可用"):
        git_settings.apply_profile_identity(
            profiles,
            "shared",
            RuntimeHandle("env-exec:runtime", "", workspace_root="ldap-user-root"),
        )


def test_settings_read_is_read_only_and_sync_uses_current_profile(profiles, monkeypatch):
    from flowweave.modules.agent_sessions.application import conversations

    calls = []
    monkeypatch.setattr(workspace, "ensure_user_project_root", lambda *args: None)
    monkeypatch.setattr(conversations, "terminal_resource_details", lambda *args: ("runtime", "id"))
    monkeypatch.setattr(
        git_settings,
        "get_runtime",
        lambda: SimpleNamespace(
            workspace_git_identity=lambda _handle, **kwargs: calls.append(kwargs)
            or {"author": "", "email": "", "configured": False}
        ),
    )
    with tenant_user("local"):
        assert git_settings.git_identity(profiles, "workspace")["configured"] is False
        git_settings.synchronize_profile_identity(profiles, "workspace")
    assert calls == [{}, {}, {"profile": ("本地姓名", "local@example.test")}]
