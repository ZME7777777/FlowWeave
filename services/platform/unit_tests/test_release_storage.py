"""Filesystem and subprocess tests; no database, Docker daemon, or SSH needed."""

from __future__ import annotations

import importlib.util
import io
import json
import subprocess
import sys
import tarfile
from pathlib import Path
from types import SimpleNamespace

import pytest

REPO = Path(__file__).resolve().parents[3]
SPEC = importlib.util.spec_from_file_location(
    "flowweave_release", REPO / "scripts/remote-release.py"
)
assert SPEC is not None and SPEC.loader is not None
release = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(release)


def context(build, number, *, scope="platform", state="succeeded", age=0):
    sha = f"{number:040x}"
    directory = build / release.release_id(sha, scope)
    directory.mkdir()
    release.write_marker(
        directory,
        {
            "schema": 1,
            "commit": sha,
            "scope": scope,
            "state": state,
            "created_at": 1_000_000 - age,
            "finished_at": number,
        },
    )
    return directory


def test_retention_preserves_each_scope_and_all_references(tmp_path):
    build = tmp_path
    old = context(build, 1)
    protected = context(build, 2)
    mounted = context(build, 3)
    opened = context(build, 4)
    newest = [context(build, n) for n in (5, 6, 7)]
    web = [context(build, n, scope="web") for n in (8, 9, 10, 11)]
    result = release.select_cleanup(
        build,
        {f"{2:040x}"},
        [mounted / "src", opened / "build.log"],
        keep=3,
        now=1_000_000,
    )
    assert result == sorted([old, web[0]])
    assert all(p not in result for p in [protected, mounted, opened, *newest, *web[1:]])
    assert release.select_cleanup(build, set(), [build], keep=3, now=1_000_000) == []


def test_unknown_symlink_and_incomplete_contexts_are_protected(tmp_path):
    context(tmp_path, 1)
    fresh = context(tmp_path, 2, state="building", age=3600)
    stale = context(tmp_path, 3, state="failed", age=8 * 86400)
    unknown = tmp_path / "legacy-release"
    unknown.mkdir()
    malformed = tmp_path / release.release_id(f"{4:040x}", "platform")
    malformed.mkdir()
    (malformed / release.MARKER).write_text("{}")
    link = tmp_path / release.release_id(f"{5:040x}", "platform")
    link.symlink_to(unknown, target_is_directory=True)
    result = release.select_cleanup(tmp_path, set(), [], keep=3, now=1_000_000)
    assert result == [stale]
    assert fresh.exists() and unknown.exists() and link.is_symlink()
    assert (
        release.select_cleanup(tmp_path, {release.UNKNOWN_PROVENANCE}, [], keep=3, now=1_000_000)
        == []
    )
    with pytest.raises(ValueError):
        release.select_cleanup(tmp_path, set(), [], keep=0, now=1_000_000)


def test_context_streaming_and_traversal_rejection(tmp_path, monkeypatch, capsys):
    build = tmp_path / "build"
    build.mkdir()
    config = {"FLOWWEAVE_REMOTE_ROOT": str(tmp_path), "FLOWWEAVE_REMOTE_BUILD_ROOT": "build"}
    payload = {
        "config": config,
        "commit": "a" * 40,
        "scope": "web",
        "action": "prepare",
        "apply": False,
    }

    def archive(name):
        data = io.BytesIO()
        with tarfile.open(fileobj=data, mode="w:gz") as stream:
            item = tarfile.TarInfo(name)
            item.size = 6
            stream.addfile(item, io.BytesIO(b"source"))
        data.seek(0)
        return data

    monkeypatch.setattr(sys, "stdin", SimpleNamespace(buffer=archive("apps/web/src/main.tsx")))
    release.remote_main(payload)
    directory = build / release.release_id("a" * 40, "web")
    assert (directory / "apps/web/src/main.tsx").read_text() == "source"
    assert release.read_marker(directory)["state"] == "building"
    assert not list(build.glob("*.tar*"))
    assert json.loads(capsys.readouterr().out)["archive_bytes"] > 0
    with pytest.raises(ValueError, match="already exists"):
        release.remote_main(payload)
    payload["commit"] = "b" * 40
    monkeypatch.setattr(sys, "stdin", SimpleNamespace(buffer=archive("../escaped")))
    with pytest.raises(ValueError, match="non-regular"):
        release.remote_main(payload)
    assert not (tmp_path / "escaped").exists()
    failed = build / release.release_id("b" * 40, "web")
    assert release.read_marker(failed)["state"] == "failed"


def test_finish_checks_health_before_marking_or_removing(tmp_path, monkeypatch):
    build = tmp_path / "build"
    build.mkdir()
    directory = context(build, 1, state="building")

    def unhealthy(*_args):
        raise ValueError("unhealthy")

    monkeypatch.setattr(release, "check_release_services", unhealthy)
    monkeypatch.setattr(
        release, "docker_protection", lambda *_args: pytest.fail("must not plan removal")
    )
    with pytest.raises(ValueError, match="unhealthy"):
        release.remote_main(
            {
                "config": {
                    "FLOWWEAVE_REMOTE_ROOT": str(tmp_path),
                    "FLOWWEAVE_REMOTE_BUILD_ROOT": "build",
                },
                "commit": f"{1:040x}",
                "scope": "platform",
                "action": "finish",
                "apply": True,
            }
        )
    assert release.read_marker(directory)["state"] == "building"


def test_late_reference_prevents_directory_removal(tmp_path, monkeypatch):
    build = tmp_path / "build"
    build.mkdir()
    old = context(build, 1)
    for number in (2, 3, 4):
        context(build, number)
    calls = 0

    def references(*_args):
        nonlocal calls
        calls += 1
        return ({f"{1:040x}"} if calls > 1 else set()), []

    monkeypatch.setattr(release, "docker_protection", references)
    with pytest.raises(ValueError, match="changed after dry-run"):
        release.remote_main(
            {
                "config": {
                    "FLOWWEAVE_REMOTE_ROOT": str(tmp_path),
                    "FLOWWEAVE_REMOTE_BUILD_ROOT": "build",
                },
                "commit": f"{4:040x}",
                "scope": "platform",
                "action": "cleanup",
                "apply": True,
            }
        )
    assert old.exists()


def test_commit_archive_omits_unrelated_tracked_assets(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    subprocess.run(["git", "init", "-q"], check=True)
    for scope, paths in (("platform", release.PLATFORM_FILES), ("web", release.WEB_FILES)):
        for path in paths:
            destination = tmp_path / path
            if path.endswith(("/src", "/migrations")):
                destination /= "fixture.txt"
            destination.parent.mkdir(parents=True, exist_ok=True)
            destination.write_text(scope)
    (tmp_path / ".dockerignore").write_text(".local\n.tmp\n")
    for path in ("docs/history.md", "infra/openhands/vendor/old.tar.gz", "apps/web/e2e/test.ts"):
        destination = tmp_path / path
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_text("not a build input")
    subprocess.run(["git", "add", "."], check=True)
    subprocess.run(
        [
            "git",
            "-c",
            "user.name=Release Test",
            "-c",
            "user.email=release@example.test",
            "commit",
            "-qm",
            "fixture",
        ],
        check=True,
    )
    for scope in ("platform", "web"):
        paths = release.source_paths("HEAD", scope)
        result = subprocess.run(
            ["git", "archive", "HEAD", "--", *paths], capture_output=True, check=True
        )
        with tarfile.open(fileobj=io.BytesIO(result.stdout)) as stream:
            names = stream.getnames()
        assert not any("vendor/" in p or "/e2e/" in p or p.startswith("docs/") for p in names)
        if scope == "platform":
            assert not any(p.startswith("apps/") for p in names)
        else:
            assert not any(p.startswith("services/") for p in names)


def test_process_ancestor_cwd_does_not_protect_all_contexts(tmp_path):
    build = tmp_path / "build"
    build.mkdir()
    old = context(build, 1)
    for number in (2, 3, 4):
        context(build, number)
    proc = tmp_path / "proc"
    pid = proc / "123"
    (pid / "fd").mkdir(parents=True)
    (pid / "cwd").symlink_to("/")
    (pid / "fd/1").symlink_to(build)
    assert release.process_references(proc, build) == []
    assert release.select_cleanup(
        build, set(), release.process_references(proc, build), keep=3, now=1_000_000
    ) == [old]
    (pid / "fd/2").symlink_to(old / "source-file")
    assert (
        release.select_cleanup(
            build, set(), release.process_references(proc, build), keep=3, now=1_000_000
        )
        == []
    )
    # A container mounting an ancestor still references its descendant tree.
    assert release.select_cleanup(build, set(), [tmp_path], keep=3, now=1_000_000) == []
