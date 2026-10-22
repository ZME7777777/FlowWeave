"""Real Git configuration in isolated homes; no Docker or database."""

from __future__ import annotations

import json
import os
import subprocess
import sys

import pytest

from flowweave.runtime.git_identity import GIT_IDENTITY_SCRIPT


def operation(root, home, **values):
    env = {"PATH": os.environ["PATH"], "HOME": str(home), "GIT_CONFIG_NOSYSTEM": "1"}
    return subprocess.run(
        [sys.executable, "-c", GIT_IDENTITY_SCRIPT, json.dumps({"root": str(root), **values})],
        env=env,
        capture_output=True,
        text=True,
    )


def identity(repository, home):
    return tuple(
        subprocess.run(
            ["git", "-C", str(repository), "config", "--get", key],
            env={"PATH": os.environ["PATH"], "HOME": str(home), "GIT_CONFIG_NOSYSTEM": "1"},
            capture_output=True,
            text=True,
            check=True,
        ).stdout.strip()
        for key in ("user.name", "user.email")
    )


def repository(root):
    root.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "init", "-q", str(root)], check=True)
    return root


def test_git_identity_is_confirmed_from_runtime_and_scoped_to_each_user(tmp_path):
    home = tmp_path / "home"
    home.mkdir()
    (home / ".gitconfig").write_text(
        "[user]\n name = Existing Default\n email = default@example.test\n"
    )
    a, b = tmp_path / "user-a", tmp_path / "user-b"
    a.mkdir()
    b.mkdir()
    assert json.loads(operation(a, home).stdout) == {"author": "", "email": "", "configured": False}
    for root, name in ((a, "张三 $(touch injected)"), (b, "User B")):
        result = operation(root, home, author=name, email=f"{root.name}@example.test")
        assert result.returncode == 0, result.stderr
        assert json.loads(result.stdout) == dict(
            author=name, email=f"{root.name}@example.test", configured=True
        )
        assert identity(repository(root / "new" / "nested-repository"), home) == (
            name,
            f"{root.name}@example.test",
        )
    assert identity(repository(tmp_path / "outside"), home) == (
        "Existing Default",
        "default@example.test",
    )
    # Reopening reads actual configuration, rather than a database preference.
    assert json.loads(operation(a, home).stdout)["author"] == "张三 $(touch injected)"
    assert operation(a, home, author="Updated", email="updated@example.test").returncode == 0
    assert identity(a / "new" / "nested-repository", home) == ("Updated", "updated@example.test")
    assert identity(b / "new" / "nested-repository", home) == ("User B", "user-b@example.test")
    assert not (a / "injected").exists()
    assert (home / ".gitconfig").read_text().count("[includeIf") == 2
    # A disconnected config must never be presented as applied.
    (home / ".gitconfig").write_text("")
    assert json.loads(operation(a, home).stdout) == {"author": "", "email": "", "configured": False}


def test_git_identity_respects_repository_overrides_and_survives_process_restart(tmp_path):
    home = tmp_path / "home"
    home.mkdir()
    root = repository(tmp_path / "user")
    assert operation(root, home, author="User", email="user@example.test").returncode == 0
    subprocess.run(["git", "-C", str(root), "config", "user.name", "Repository Author"], check=True)
    assert identity(root, home) == ("Repository Author", "user@example.test")
    assert json.loads(operation(root, home).stdout)["configured"] is True
    assert (root / ".flowweave-gitconfig").stat().st_mode & 0o777 == 0o600


@pytest.mark.parametrize("linked", [".flowweave-gitconfig", ".flowweave-git-settings.lock"])
def test_git_identity_rejects_symlink_config_and_lock(tmp_path, linked):
    home, root = tmp_path / "home", tmp_path / "user"
    home.mkdir()
    root.mkdir()
    victim = tmp_path / "other-user"
    victim.write_text("untouched")
    (root / linked).symlink_to(victim)
    assert operation(root, home, author="Wrong", email="wrong@example.test").returncode != 0
    assert victim.read_text() == "untouched"


def test_git_identity_failed_registration_restores_previous_configuration(tmp_path):
    home, root = tmp_path / "home", tmp_path / "user"
    home.mkdir()
    root.mkdir()
    (home / ".gitconfig.lock").write_text("locked")
    result = operation(root, home, author="User", email="user@example.test")
    assert result.returncode != 0
    assert not (root / ".flowweave-gitconfig").exists()


def test_git_settings_targets_only_authenticated_user_directory(monkeypatch):
    from flowweave.modules.agent_workspaces.application import git_settings
    from flowweave.modules.users.application.security import tenant_user

    calls = []
    monkeypatch.setattr(git_settings.workspace, "ensure_user_project_root", lambda *args: None)
    monkeypatch.setattr(
        git_settings.agent_sessions.conversations,
        "terminal_resource_details",
        lambda *args: ("owned", "resource-id"),
    )

    class Runtime:
        def workspace_git_identity(self, handle, *, identity):
            calls.append((handle.workspace_root, identity))
            return {}

    monkeypatch.setattr(git_settings, "get_runtime", Runtime)
    for user in ("user-a", "user-b"):
        with tenant_user(user):
            git_settings.git_identity(
                None, "shared-workspace", identity=(user, f"{user}@example.test")
            )
    assert [root for root, _ in calls] == [
        "/runtime/workspace/project/users/user-a",
        "/runtime/workspace/project/users/user-b",
    ]


@pytest.mark.parametrize(
    "values",
    [
        {"author": "a\nb", "email": "a@example.test"},
        {"author": "User", "email": "bad email"},
        {"author": "User", "email": "a@example.test", "root": "/another-user"},
    ],
)
def test_git_settings_rejects_identity_injection_and_client_paths(values):
    from pydantic import ValidationError

    from flowweave.modules.agent_workspaces.presentation.router import AgentGitIdentityWrite

    with pytest.raises(ValidationError):
        AgentGitIdentityWrite(**values)


def test_git_identity_hides_values_when_global_precedence_prevents_inheritance(tmp_path):
    home, root = tmp_path / "home", tmp_path / "user"
    home.mkdir()
    root.mkdir()
    assert operation(root, home, author="User", email="user@example.test").returncode == 0
    with (home / ".gitconfig").open("a") as config:
        config.write("\n[user]\n name = Overriding Global Author\n")
    assert json.loads(operation(root, home).stdout) == dict(author="", email="", configured=False)
    assert not list(root.glob(".flowweave-git-check-*"))


def test_simultaneous_users_register_independent_directory_defaults(tmp_path):
    from concurrent.futures import ThreadPoolExecutor

    home = tmp_path / "home"
    home.mkdir()
    roots = [tmp_path / f"user-{index}" for index in range(5)]
    for root in roots:
        root.mkdir()
    with ThreadPoolExecutor(max_workers=5) as executor:
        results = list(
            executor.map(
                lambda root: operation(
                    root, home, author=root.name, email=f"{root.name}@example.test"
                ),
                roots,
            )
        )
    assert all(result.returncode == 0 for result in results), [r.stderr for r in results]
    for root in roots:
        assert identity(repository(root / "repo"), home) == (root.name, f"{root.name}@example.test")
