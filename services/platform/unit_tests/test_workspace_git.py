"""Real Git branch and remote-state checks in isolated repositories."""

from __future__ import annotations

import asyncio
import fcntl
import importlib
import json
import shlex
import subprocess
import sys
from types import SimpleNamespace

import pytest

from flowweave.modules.agent_workspaces.application import workspace
from flowweave.runtime.base import RuntimeHandle
from flowweave.runtime.dependencies import runtime_context
from flowweave.runtime.openhands import OpenHandsRuntime
from flowweave.runtime.workspace_git import WORKSPACE_GIT_SCRIPT
from flowweave.shared.errors import DomainError
from flowweave.shared.schemas import WorkspaceGitSwitchWrite


def git(repository, *args):
    return subprocess.run(
        ["git", "-C", str(repository), *args],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()


@pytest.fixture
def repository(tmp_path):
    repo = tmp_path / "project"
    repo.mkdir()
    git(repo, "init", "--initial-branch=main")
    git(repo, "config", "user.name", "FlowWeave Test")
    git(repo, "config", "user.email", "test@example.invalid")
    (repo / "file.txt").write_text("main\n")
    git(repo, "add", ".")
    git(repo, "commit", "-m", "base")
    git(repo, "switch", "-c", "feature/中文")
    (repo / "file.txt").write_text("feature\n")
    git(repo, "commit", "-am", "feature")
    git(repo, "switch", "main")
    return repo


def operation(repository, action="switch", **values):
    request = {
        "root": str(repository.parent),
        "repository": str(repository),
        "action": action,
        "ref": "refs/heads/feature/中文",
        "expected_head": git(repository, "rev-parse", "HEAD"),
        "expected_ref": git(repository, "symbolic-ref", "HEAD"),
        **values,
    }
    result = subprocess.run(
        [sys.executable, "-c", WORKSPACE_GIT_SCRIPT, json.dumps(request)],
        capture_output=True,
        text=True,
        check=True,
    )
    return json.loads(result.stdout)


def test_switch_local_branch_preserves_unrelated_edits_and_disables_hooks(repository):
    (repository / "notes.txt").write_text("keep\n")
    hooks = repository / ".git/hooks/post-checkout"
    hooks.write_text("#!/bin/sh\ntouch hook-ran\n")
    hooks.chmod(0o700)
    assert operation(repository) == {"ok": True}
    assert git(repository, "branch", "--show-current") == "feature/中文"
    assert (repository / "file.txt").read_text() == "feature\n"
    assert (repository / "notes.txt").read_text() == "keep\n"
    assert not (repository / "hook-ran").exists()


@pytest.mark.parametrize("kind", ["tracked", "untracked", "ignored"])
def test_switch_keeps_conflicting_files_and_branch(repository, kind):
    if kind == "tracked":
        (repository / "file.txt").write_text("local edit\n")
        path = repository / "file.txt"
    else:
        git(repository, "switch", "feature/中文")
        (repository / "new.txt").write_text("branch version\n")
        git(repository, "add", "new.txt")
        git(repository, "commit", "-m", "new file")
        git(repository, "switch", "main")
        path = repository / "new.txt"
        path.write_text("local edit\n")
        if kind == "ignored":
            (repository / ".git/info/exclude").write_text("new.txt\n")
    assert operation(repository) == {"error": "SWITCH_CONFLICT"}
    assert git(repository, "branch", "--show-current") == "main"
    assert path.read_text() == "local edit\n"


def test_switch_rejects_stale_identity_invalid_refs_and_outside_paths(repository):
    assert operation(repository, expected_head="0" * 40) == {"error": "STALE"}
    assert operation(repository, expected_ref="refs/heads/other") == {"error": "STALE"}
    for ref in ["--force", "HEAD", "refs/tags/v1", "refs/heads/missing"]:
        assert "error" in operation(repository, ref=ref)
    (repository / "subdirectory").mkdir()
    assert operation(repository, root=str(repository / "subdirectory")) == {"error": "PATH_INVALID"}
    assert git(repository, "branch", "--show-current") == "main"


def test_switch_remote_creates_tracking_branch_without_detaching(repository, tmp_path):
    remote = tmp_path / "remote.git"
    subprocess.run(["git", "init", "--bare", str(remote)], check=True, capture_output=True)
    git(repository, "remote", "add", "origin", str(remote))
    git(repository, "push", "origin", "feature/中文:feature/remote")
    assert operation(repository, ref="refs/remotes/origin/feature/remote") == {"ok": True}
    assert git(repository, "branch", "--show-current") == "feature/remote"
    assert git(repository, "rev-parse", "--abbrev-ref", "@{upstream}") == "origin/feature/remote"
    assert operation(repository, ref="refs/remotes/origin/feature/remote") == {"ok": True}


def test_stale_remote_refs_never_mark_published_commits_unpushed(repository, tmp_path):
    remote = tmp_path / "remote.git"
    subprocess.run(["git", "init", "--bare", str(remote)], check=True, capture_output=True)
    git(repository, "remote", "add", "origin", str(remote))
    git(repository, "push", "-u", "origin", "main")
    previous = git(repository, "rev-parse", "HEAD")
    (repository / "file.txt").write_text("published\n")
    git(repository, "commit", "-am", "published")
    git(repository, "push", "origin", "main")
    git(repository, "update-ref", "refs/remotes/origin/main", previous)
    scope = (repository, str(repository), (str(repository),), str(repository))
    handle = RuntimeHandle(job_id="test", conversation_id="", workspace_root=str(repository))

    def execute(_handle, _path, action):
        result = operation(repository, action)
        if "error" in result:
            raise DomainError("GIT_UNAVAILABLE", "remote unavailable", 502)

    with runtime_context(SimpleNamespace(workspace_git_operation=execute)):
        history = workspace.runtime_git_history(handle, *scope)
    assert history["repository"]["remote_status"] == "verified"
    assert history["repository"]["ahead"] == 0
    assert not any(commit["local_only"] for commit in history["commits"])
    git(repository, "update-ref", "refs/remotes/origin/main", previous)
    git(repository, "remote", "set-url", "origin", str(tmp_path / "missing.git"))
    with runtime_context(SimpleNamespace(workspace_git_operation=execute)):
        history = workspace.runtime_git_history(handle, *scope)
    assert history["repository"]["remote_status"] == "unverified"
    assert history["repository"]["ahead"] == 1
    assert not any(commit["local_only"] for commit in history["commits"])


def test_branch_list_excludes_symbolic_remote_head_and_switch_requires_whole_repo(repository):
    git(repository, "update-ref", "refs/remotes/origin/main", "HEAD")
    git(repository, "symbolic-ref", "refs/remotes/origin/HEAD", "refs/remotes/origin/main")
    scope = (repository, str(repository), (str(repository),), str(repository))
    branches = workspace.git_branches(*scope)["branches"]
    assert {branch["ref"] for branch in branches} == {
        "refs/heads/main",
        "refs/heads/feature/中文",
        "refs/remotes/origin/main",
    }
    # A scope limited to a subdirectory must never switch the enclosing repo.
    (repository / "subdirectory").mkdir()
    with pytest.raises(DomainError) as error:
        workspace.runtime_git_switch(
            RuntimeHandle(job_id="test", conversation_id="", workspace_root=str(repository)),
            repository,
            str(repository),
            (str(repository / "subdirectory"),),
            str(repository),
            "refs/heads/feature/中文",
            git(repository, "rev-parse", "HEAD"),
            "refs/heads/main",
        )
    assert error.value.status in {403, 404}


def test_native_operations_keep_runtime_configuration_and_fetch_other_branches(
    repository, tmp_path, monkeypatch
):
    remote = tmp_path / "remote.git"
    subprocess.run(["git", "init", "--bare", str(remote)], check=True, capture_output=True)
    home = tmp_path / "runtime-home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    git(
        repository,
        "config",
        "--global",
        f"url.{remote}.insteadOf",
        "https://git.example.invalid/repo.git",
    )
    git(repository, "remote", "add", "origin", "https://git.example.invalid/repo.git")
    git(repository, "push", "-u", "origin", "main", "feature/中文")
    git(repository, "update-ref", "-d", "refs/remotes/origin/feature/中文")
    assert operation(repository, "sync") == {"ok": True}
    assert git(repository, "rev-parse", "refs/remotes/origin/feature/中文")


def test_competing_switches_are_rejected_and_existing_local_names_are_preserved(repository):
    with (repository / ".git/flowweave-branch.lock").open("w") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        assert operation(repository) == {"error": "BUSY"}
    git(repository, "update-ref", "refs/remotes/origin/feature/中文", "feature/中文")
    assert operation(repository, ref="refs/remotes/origin/feature/中文") == {
        "error": "LOCAL_EXISTS"
    }
    assert git(repository, "branch", "--show-current") == "main"


def test_branch_change_during_remote_refresh_invalidates_the_verification(repository):
    scope = (repository, str(repository), (str(repository),), str(repository))
    handle = RuntimeHandle(job_id="test", conversation_id="", workspace_root=str(repository))

    def execute(*args):
        git(repository, "switch", "feature/中文")

    with runtime_context(SimpleNamespace(workspace_git_operation=execute)):
        history = workspace.runtime_git_history(handle, *scope)
    assert history["repository"]["remote_status"] == "unverified"
    assert history["repository"]["branch"] == "feature/中文"


def test_adapter_uses_native_bash_and_keeps_untrusted_values_as_json(monkeypatch):
    runtime = object.__new__(OpenHandsRuntime)
    calls = []
    monkeypatch.setattr(runtime, "_base_url_for_handle", lambda _handle: "http://runtime.test")
    monkeypatch.setattr(runtime, "_session_key_for_handle", lambda _handle: "test")

    def request(method, path, **values):
        calls.append((method, path, values))
        return {"exit_code": 0, "stdout": '{"ok":true}'}

    monkeypatch.setattr(runtime, "_request", request)
    handle = RuntimeHandle(job_id="test", conversation_id="", workspace_root="/workspace")
    branch = "refs/heads/$(touch injected)"
    runtime.workspace_git_operation(handle, "/workspace/repo", "switch", ref=branch)
    method, path, values = calls[0]
    assert (method, path) == ("POST", "/api/bash/execute_bash_command")
    command = shlex.split(values["json"]["command"])
    assert command[:3] == ["python3", "-c", WORKSPACE_GIT_SCRIPT]
    assert json.loads(command[3])["ref"] == branch
    assert json.loads(command[3])["repository"] == "/workspace/repo"
    assert json.loads(command[3])["root"] == "/workspace"


@pytest.mark.parametrize("error", ["REMOTE_UNAVAILABLE", "SWITCH_CONFLICT", "STALE"])
def test_adapter_reports_errors_without_exposing_git_output(monkeypatch, error):
    runtime = object.__new__(OpenHandsRuntime)
    monkeypatch.setattr(runtime, "_base_url_for_handle", lambda _handle: "http://runtime.test")
    monkeypatch.setattr(runtime, "_session_key_for_handle", lambda _handle: "test")
    monkeypatch.setattr(
        runtime,
        "_request",
        lambda *args, **kwargs: {
            "exit_code": 0,
            "stdout": json.dumps({"error": error}),
            "stderr": "private diagnostic",
        },
    )
    with pytest.raises(DomainError) as caught:
        runtime.workspace_git_operation(
            RuntimeHandle(job_id="test", conversation_id=""), "/workspace", "sync"
        )
    assert caught.value.code == "AGENT_WORKSPACE_GIT_" + error
    assert "private diagnostic" not in str(caught.value)


@pytest.mark.parametrize("host", ["agent_workspaces", "agent_sessions"])
def test_branch_routes_forward_the_exact_authorized_host_and_scope(monkeypatch, host):
    router = importlib.import_module(f"flowweave.modules.{host}.presentation.router")
    application = (
        router.workspace
        if host == "agent_workspaces"
        else router.agent_sessions.flow_node_workspace
    )
    calls = []
    database = object()

    async def execute(_container, operation):
        return operation(database)

    def read(db, **values):
        calls.append((db, values))
        return {"repository": {}, "branches": []}

    monkeypatch.setattr(router, "run_blocking_auxiliary", execute)
    monkeypatch.setattr(router, "run_blocking_mutation", execute)
    monkeypatch.setattr(application, "list_git_branches", read)
    monkeypatch.setattr(application, "switch_git_branch", read)
    identity = (
        {"workspace_id": "workspace"}
        if host == "agent_workspaces"
        else {"flow_run_id": "run", "attempt_id": "attempt"}
    )
    scope = {
        **identity,
        "repository_path": "/workspace/repo",
        "binding_id": "binding",
        "work_directory_id": None,
    }
    asyncio.run(router.workspace_git_branches(container=None, **scope))
    body = WorkspaceGitSwitchWrite(
        ref="refs/heads/feature", expected_head="a" * 40, expected_ref="refs/heads/main"
    )
    asyncio.run(router.switch_workspace_git_branch(container=None, body=body, **scope))
    assert calls == [(database, scope), (database, {**scope, **body.model_dump()})]
