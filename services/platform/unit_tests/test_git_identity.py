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


def test_profile_identity_auto_applies_and_tracks_profile_but_preserves_manual_choice(tmp_path):
    home, root = tmp_path / "home", tmp_path / "user"
    home.mkdir()
    root.mkdir()
    assert operation(root, home, profile=["用户姓名", "name@example.test"]).returncode == 0
    repo = repository(root / "project" / "nested")
    assert identity(repo, home) == ("用户姓名", "name@example.test")
    assert operation(root, home, profile=["新姓名", "new@example.test"]).returncode == 0
    assert identity(repo, home) == ("新姓名", "new@example.test")
    environment = {"PATH": os.environ["PATH"], "HOME": str(home), "GIT_CONFIG_NOSYSTEM": "1"}
    subprocess.run(
        ["git", "-C", str(repo), "commit", "--allow-empty", "-qm", "Verify profile author"],
        env=environment,
        check=True,
    )
    actual = subprocess.run(
        ["git", "-C", str(repo), "log", "-1", "--format=%an%n%ae"],
        env=environment,
        capture_output=True,
        text=True,
        check=True,
    ).stdout.splitlines()
    assert actual == ["新姓名", "new@example.test"]
    assert operation(root, home, author="Manual", email="manual@example.test").returncode == 0
    assert operation(root, home, profile=["再次更新", "latest@example.test"]).returncode == 0
    assert identity(repo, home) == ("Manual", "manual@example.test")


def test_existing_git_configuration_is_not_misclassified_as_profile_default(tmp_path):
    home, root = tmp_path / "home", tmp_path / "user"
    home.mkdir()
    root.mkdir()
    assert operation(root, home, author="Existing", email="existing@example.test").returncode == 0
    subprocess.run(
        [
            "git",
            "config",
            "--file",
            str(root / ".flowweave-gitconfig"),
            "--unset",
            "flowweave.identitySource",
        ],
        check=True,
    )
    result = operation(root, home, profile=["Profile", "profile@example.test"])
    assert result.returncode == 0
    assert json.loads(result.stdout)["author"] == "Existing"


def test_existing_openhands_defaults_are_reconciled_but_custom_child_identity_is_preserved(
    tmp_path,
):
    home, root = tmp_path / "home", repository(tmp_path / "user")
    home.mkdir()
    legacy = repository(root / "nested" / "old")
    custom = repository(root / "nested" / "custom")
    for repo, name, email in (
        (root, "openhands", "openhands@all-hands.dev"),
        (legacy, "openhands", "openhands@all-hands.dev"),
        (custom, "Custom Author", "custom@example.test"),
    ):
        subprocess.run(["git", "-C", str(repo), "config", "user.name", name], check=True)
        subprocess.run(["git", "-C", str(repo), "config", "user.email", email], check=True)
    subprocess.run(["git", "-C", str(legacy), "config", "core.filemode", "false"], check=True)
    result = operation(root, home, author="Chosen Author", email="chosen@example.test")
    assert result.returncode == 0, result.stderr
    for repo in (root, legacy):
        assert identity(repo, home) == ("Chosen Author", "chosen@example.test")
    assert identity(custom, home) == ("Custom Author", "custom@example.test")
    assert (
        subprocess.check_output(
            ["git", "-C", str(legacy), "config", "core.filemode"], text=True
        ).strip()
        == "false"
    )
    environment = {"PATH": os.environ["PATH"], "HOME": str(home), "GIT_CONFIG_NOSYSTEM": "1"}
    subprocess.run(
        ["git", "-C", str(legacy), "commit", "--allow-empty", "-qm", "Verify author"],
        check=True,
        env=environment,
    )
    assert subprocess.check_output(
        ["git", "-C", str(legacy), "log", "-1", "--format=%an%n%ae"], env=environment, text=True
    ).splitlines() == ["Chosen Author", "chosen@example.test"]


def test_reads_actual_root_repository_without_injecting_or_repairing_identity(tmp_path):
    home, root = tmp_path / "home", repository(tmp_path / "user")
    home.mkdir()
    assert operation(root, home, author="Chosen", email="chosen@example.test").returncode == 0
    subprocess.run(["git", "-C", str(root), "config", "user.name", "Terminal Name"], check=True)
    subprocess.run(
        ["git", "-C", str(root), "config", "user.email", "terminal@example.test"], check=True
    )
    before = (root / ".git" / "config").read_bytes()
    result = json.loads(operation(root, home).stdout)
    assert result == dict(author="Terminal Name", email="terminal@example.test", configured=True)
    assert (root / ".git" / "config").read_bytes() == before
    # Explicitly saving this directory replaces its own override.
    assert operation(root, home, author="Edited", email="edited@example.test").returncode == 0
    assert identity(root, home) == ("Edited", "edited@example.test")


def test_legacy_repair_does_not_follow_linked_repositories_outside_user_root(tmp_path):
    home, root = tmp_path / "home", tmp_path / "user"
    home.mkdir()
    root.mkdir()
    outside = repository(tmp_path / "outside")
    subprocess.run(["git", "-C", str(outside), "config", "user.name", "openhands"], check=True)
    subprocess.run(
        ["git", "-C", str(outside), "config", "user.email", "openhands@all-hands.dev"], check=True
    )
    before = (outside / ".git" / "config").read_bytes()
    (root / "linked").symlink_to(outside, target_is_directory=True)
    assert operation(root, home, author="Chosen", email="chosen@example.test").returncode == 0
    assert (outside / ".git" / "config").read_bytes() == before


def test_external_directory_file_edits_are_read_and_not_overwritten_by_next_profile_application(
    tmp_path,
):
    home, root = tmp_path / "home", tmp_path / "user"
    home.mkdir()
    root.mkdir()
    assert operation(root, home, profile=["Profile", "profile@example.test"]).returncode == 0
    config = root / ".flowweave-gitconfig"
    subprocess.run(
        ["git", "config", "--file", str(config), "user.name", "Manual Terminal"], check=True
    )
    assert json.loads(operation(root, home).stdout)["author"] == "Manual Terminal"
    assert operation(root, home, profile=["New Profile", "new@example.test"]).returncode == 0
    assert json.loads(operation(root, home).stdout)["author"] == "Manual Terminal"


def test_legacy_repair_respects_git_config_lock_and_keeps_the_pair_atomic(tmp_path):
    home, root = tmp_path / "home", tmp_path / "user"
    home.mkdir()
    root.mkdir()
    repo = repository(root / "repository")
    subprocess.run(["git", "-C", str(repo), "config", "user.name", "openhands"], check=True)
    subprocess.run(
        ["git", "-C", str(repo), "config", "user.email", "openhands@all-hands.dev"], check=True
    )
    original = (repo / ".git" / "config").read_bytes()
    lock = repo / ".git" / "config.lock"
    lock.write_text("other git writer")
    assert operation(root, home, author="Chosen", email="chosen@example.test").returncode != 0
    assert (repo / ".git" / "config").read_bytes() == original
    assert lock.read_text() == "other git writer"
    lock.unlink()
    assert operation(root, home, author="Chosen", email="chosen@example.test").returncode == 0
    assert identity(repo, home) == ("Chosen", "chosen@example.test")
