"""Scheduled retention safety; no Docker, SSH or database fixtures."""

import datetime
import fcntl
import importlib.util
import json
import os
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[3]
SPEC = importlib.util.spec_from_file_location(
    "maintenance", REPO / "scripts/storage-maintenance.py"
)
maintenance = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(maintenance)


def image(number, tags=None):
    return {
        "Id": str(number),
        "Created": f"2026-01-{number:02d}",
        "RepoTags": tags or [f"flowweave-platform:rollback-{number}"],
    }


def test_images_keep_current_containers_and_one_rollback_each_repo():
    images = [image(i) for i in range(1, 9)]
    images += [image(i, [f"flowweave-web:rollback-{i}"]) for i in range(9, 14)]
    selected = maintenance.select_images(images, {"1", "9"}, {"2", "10"})
    assert set(selected) == {"3", "4", "5", "6", "7", "11", "12"}


def test_images_with_foreign_or_runtime_tags_are_never_candidates():
    images = [image(i) for i in range(1, 8)]
    images[0]["RepoTags"].append("another-app:rollback-old")
    images[1]["RepoTags"].append("flowweave-environment:version-1")
    images[2]["RepoTags"] = None
    assert set(maintenance.select_images(images, set(), set())) == {"4", "5", "6"}


def test_active_build_install_detection_and_daemon_exclusion(tmp_path):
    pid = tmp_path / "123"
    pid.mkdir()
    for command in [b"/usr/bin/docker\0buildx\0build\0.\0", b"uv\0sync\0", b"npm\0ci\0"]:
        (pid / "cmdline").write_bytes(command)
        assert maintenance.active_mutation(tmp_path)
    (pid / "cmdline").write_bytes(b"buildkitd\0--addr\0test\0")
    assert not maintenance.active_mutation(tmp_path)


def test_busy_release_lock_skips_without_inventory(tmp_path, monkeypatch):
    build = tmp_path / "build"
    build.mkdir()
    monkeypatch.setattr(maintenance, "active_mutation", lambda: pytest.fail("busy job must skip"))
    with (build / ".flowweave-release.lock").open("w") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        assert maintenance.maintain({"root": str(tmp_path), "build": "build"}, True) == {
            "status": "skipped-lock"
        }


def test_late_image_reference_stops_untag(tmp_path, monkeypatch):
    (tmp_path / "build").mkdir()
    monkeypatch.setattr(maintenance, "active_mutation", lambda: False)
    monkeypatch.setattr(maintenance.release, "docker_protection", lambda *_args: (set(), []))
    plans = iter([{"old": ["flowweave-platform:rollback-old"]}, {}])
    monkeypatch.setattr(maintenance, "image_plan", lambda: next(plans))
    monkeypatch.setattr(
        maintenance.release, "run", lambda _: pytest.fail("must not untag referenced image")
    )
    with pytest.raises(ValueError, match="protection"):
        maintenance.maintain({"root": str(tmp_path), "build": "build"}, True)


def test_oversize_plan_refuses_before_deleting_context(tmp_path, monkeypatch):
    build = tmp_path / "build"
    build.mkdir()
    monkeypatch.setattr(maintenance, "active_mutation", lambda: False)
    monkeypatch.setattr(maintenance.release, "docker_protection", lambda *_args: (set(), []))
    monkeypatch.setattr(maintenance, "image_plan", lambda: {str(i): [] for i in range(101)})
    old = build / "old"
    old.mkdir()
    monkeypatch.setattr(maintenance.release, "select_cleanup", lambda *_args, **_kw: [old])
    with pytest.raises(ValueError, match="Bounded"):
        maintenance.maintain({"root": str(tmp_path), "build": "build"}, True)
    assert old.exists()


def test_dry_run_does_not_remove_governed_old_context(tmp_path, monkeypatch):
    build = tmp_path / "build"
    build.mkdir()
    for i in range(1, 5):
        directory = build / maintenance.release.release_id(f"{i:040x}", "platform")
        directory.mkdir()
        maintenance.release.write_marker(
            directory,
            {
                "schema": 1,
                "commit": f"{i:040x}",
                "scope": "platform",
                "state": "succeeded",
                "created_at": 1,
                "finished_at": i,
            },
        )
    import os

    for p in build.glob("release-*"):
        os.utime(p / maintenance.release.MARKER, (1, 1))
        os.utime(p, (1, 1))
    monkeypatch.setattr(maintenance, "active_mutation", lambda: False)
    monkeypatch.setattr(maintenance.release, "docker_protection", lambda *_args: (set(), []))
    monkeypatch.setattr(maintenance, "image_plan", lambda: {})
    result = maintenance.maintain({"root": str(tmp_path), "build": "build"})
    assert result["candidate_contexts"] == 4
    assert result["removed_contexts"] == 0
    assert len(list(build.glob("release-*"))) == 4


def test_running_install_skips_before_inventory(tmp_path, monkeypatch):
    (tmp_path / "build").mkdir()
    monkeypatch.setattr(maintenance, "active_mutation", lambda: True)
    monkeypatch.setattr(maintenance, "image_plan", lambda: pytest.fail("must skip inventory"))
    result = maintenance.maintain({"root": str(tmp_path), "build": "build"}, True)
    assert result == {"status": "skipped-active-build-or-install"}


def test_late_context_reference_stops_directory_removal(tmp_path, monkeypatch):
    build = tmp_path / "build"
    build.mkdir()
    old = build / "old"
    old.mkdir()
    monkeypatch.setattr(maintenance, "active_mutation", lambda: False)
    monkeypatch.setattr(maintenance.release, "docker_protection", lambda *_args: (set(), []))
    plans = iter([[old], []])
    monkeypatch.setattr(maintenance.release, "select_cleanup", lambda *_a, **_k: next(plans))
    monkeypatch.setattr(maintenance, "image_plan", lambda: {})
    with pytest.raises(ValueError, match="reference changed"):
        maintenance.maintain({"root": str(tmp_path), "build": "build"}, True)
    assert old.exists()


def test_exact_multi_tag_removal_rechecks_remaining_tags(tmp_path, monkeypatch):
    (tmp_path / "build").mkdir()
    monkeypatch.setattr(maintenance, "active_mutation", lambda: False)
    monkeypatch.setattr(maintenance.release, "docker_protection", lambda *_args: (set(), []))
    tags = ["flowweave-platform:rollback-old", "flowweave-web:rollback-old"]
    plans = iter([{"old": tags}, {"old": tags}, {"old": tags[1:]}])
    monkeypatch.setattr(maintenance, "image_plan", lambda: next(plans))
    commands = []

    def run(command):
        commands.append(command)
        return '[{"Id":"old"}]' if "inspect" in command else ""

    monkeypatch.setattr(maintenance.release, "run", run)
    result = maintenance.maintain({"root": str(tmp_path), "build": "build"}, True)
    assert result["removed_images"] == 1
    assert [c for c in commands if "rm" in c] == [["docker", "image", "rm", tag] for tag in tags]


def test_remote_installer_program_handles_its_own_entrypoint_literal():
    spec = importlib.util.spec_from_file_location(
        "installer", REPO / "scripts/install-storage-maintenance.py"
    )
    installer = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(installer)
    source = (REPO / "scripts/install-storage-maintenance.py").read_text()
    program = installer.server_program(source)
    compile(program, "<test-remote-installer>", "exec")
    assert program.rstrip().endswith("sys.exit(1)")
    assert "def install_server(payload):" in program


def test_current_image_does_not_consume_the_one_rollback_slot():
    images = [image(i) for i in range(1, 5)]
    assert set(maintenance.select_images(images, set(), {"4"})) == {"1", "2"}


def test_old_platform_build_tags_are_cleaned_but_environment_images_are_not():
    images = [image(1, ["flowweave-platform:build-old"]), image(2)]
    images += [image(3, ["flowweave-environment:published"])]
    assert set(maintenance.select_images(images, set(), set())) == {"1"}


def test_cache_candidates_exclude_recent_active_shared_and_unknown_dates():
    now = datetime.datetime(2026, 10, 26, tzinfo=datetime.UTC).timestamp()
    old = {"ID": "old", "Size": 123, "LastUsedAt": "2026-10-24T23:59:59.123456789Z"}
    rows = [old, dict(old, ID="active", InUse=True), dict(old, ID="shared", Shared=True)]
    rows += [dict(old, ID="fresh", LastUsedAt="2026-10-25T00:00:01Z"), {"ID": "unknown"}]
    assert maintenance.select_build_cache(rows, now) == [old]


def test_cache_prune_is_only_engine_build_cache_with_one_day_filter(monkeypatch):
    calls = []
    monkeypatch.setattr(maintenance, "docker_request", lambda *a: calls.append(a) or {})
    maintenance.prune_build_cache()
    from urllib.parse import parse_qs, urlsplit

    method, url = calls[0]
    assert method == "POST" and urlsplit(url).path == "/build/prune"
    assert json.loads(parse_qs(urlsplit(url).query)["filters"][0]) == {"until": ["24h"]}
    assert "all" not in parse_qs(urlsplit(url).query)


def test_cache_disabled_unless_explicitly_configured(tmp_path, monkeypatch):
    (tmp_path / "build").mkdir()
    monkeypatch.setattr(maintenance, "active_mutation", lambda: False)
    monkeypatch.setattr(maintenance.release, "docker_protection", lambda *_: (set(), []))
    monkeypatch.setattr(maintenance, "image_plan", lambda: {})
    monkeypatch.setattr(maintenance, "cache_plan", lambda: pytest.fail("not authorized"))
    assert (
        maintenance.maintain({"root": str(tmp_path), "build": "build"})["candidate_build_cache"]
        == 0
    )


def test_late_build_stops_cache_prune(tmp_path, monkeypatch):
    (tmp_path / "build").mkdir()
    active = iter([False, True])
    monkeypatch.setattr(maintenance, "active_mutation", lambda: next(active))
    monkeypatch.setattr(maintenance.release, "docker_protection", lambda *_: (set(), []))
    monkeypatch.setattr(maintenance, "image_plan", lambda: {})
    monkeypatch.setattr(maintenance, "cache_plan", lambda: [{"Size": 1}])
    monkeypatch.setattr(maintenance, "prune_build_cache", lambda: pytest.fail("build started"))
    with pytest.raises(ValueError, match="before cache cleanup"):
        maintenance.maintain(
            {"root": str(tmp_path), "build": "build", "prune_build_cache": True}, True
        )


def test_legacy_cleanup_checks_content_age_references_symlinks_and_backups(tmp_path):
    build = tmp_path / "build"
    build.mkdir()
    old = build / ("release-" + "a" * 40 + "-runtime")
    old.mkdir()
    (old / "Dockerfile").write_text("FROM scratch")
    unknown = build / ("release-" + "b" * 40 + "-unknown")
    unknown.mkdir()
    backup = build / "database-backups"
    backup.mkdir()
    link = build / ("runtime-" + "c" * 40)
    link.symlink_to(old, target_is_directory=True)
    for p in [old / "Dockerfile", old, unknown, backup]:
        os.utime(p, (1, 1))
    select = maintenance.release.select_legacy_build_assets
    assert select(build, [], now=100000) == [old]
    assert select(build, [old / "Dockerfile"], now=100000) == []
    (old / "recent").write_text("active")
    assert select(build, [], now=100000) == []


def test_legacy_invalid_marker_and_sensitive_tree_cannot_fall_back(tmp_path):
    for i, name in enumerate([maintenance.release.MARKER, "data", ".git"]):
        directory = tmp_path / ("release-" + f"{i:040x}" + "-runtime")
        directory.mkdir()
        (directory / "Dockerfile").write_text("FROM scratch")
        (directory / name).write_text("{}")
        for p in directory.iterdir():
            os.utime(p, (1, 1))
        os.utime(directory, (1, 1))
    assert maintenance.release.select_legacy_build_assets(tmp_path, [], now=100000) == []


def test_legacy_wrapped_source_is_recognized_but_nested_backup_blocks_deletion(tmp_path):
    directory = tmp_path / ("release-" + "a" * 40 + "-old-build")
    source = directory / "source" / "extracted"
    dockerfile = source / "services/platform/Dockerfile"
    dockerfile.parent.mkdir(parents=True)
    dockerfile.write_text("FROM scratch")
    for p in [*directory.rglob("*"), directory]:
        os.utime(p, (1, 1))
    select = maintenance.release.select_legacy_build_assets
    assert select(tmp_path, [], now=100000) == [directory]
    (directory / "source/backups").mkdir()
    assert not maintenance.release.disposable_build_layout(directory)


def test_source_workspace_placeholder_is_distinct_from_real_workspace(tmp_path):
    source = tmp_path / "source"
    placeholder = source / "var/workspaces"
    placeholder.mkdir(parents=True)
    (placeholder / ".gitkeep").write_text("\n")
    (source / "Dockerfile").write_text("FROM scratch")
    assert maintenance.release.disposable_build_layout(tmp_path)
    (placeholder / "session.json").write_text("data")
    assert not maintenance.release.disposable_build_layout(tmp_path)


def test_bare_commit_builds_are_only_selected_in_historical_releases_directory(tmp_path):
    build = tmp_path / "build"
    historical = build / "releases"
    historical.mkdir(parents=True)
    source = historical / ("a" * 40)
    source.mkdir()
    (source / "Dockerfile").write_text("FROM scratch")
    unrelated = build / ("b" * 40)
    unrelated.mkdir()
    (unrelated / "Dockerfile").write_text("FROM scratch")
    for p in [*build.rglob("*"), build]:
        os.utime(p, (1, 1))
    assert maintenance.legacy_plan(build, None, []) == [source]
    assert maintenance.legacy_plan(build, None, [source / "Dockerfile"]) == []


def test_install_audit_program_is_executable_without_live_config_changes():
    # Execute the exact audit-code expression from the installer with a fake runner.
    import ast

    source = (REPO / "scripts/install-storage-maintenance.py").read_text()
    tree = ast.parse(source)
    install = next(
        n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == "install_locked"
    )
    assignment = next(
        n
        for n in ast.walk(install)
        if isinstance(n, ast.Assign)
        and any(isinstance(t, ast.Name) and t.id == "audit_code" for t in n.targets)
    )
    code = ast.Expression(assignment.value)
    audit = eval(
        compile(code, "<audit-program-test>", "eval"),
        {"version": Path("/example"), "config_data": b"{}"},
    )
    compile(audit, "<generated-audit>", "exec")


def test_installer_restores_settings_units_and_version_on_first_run_failure(tmp_path, monkeypatch):
    import base64

    spec = importlib.util.spec_from_file_location(
        "installer", REPO / "scripts/install-storage-maintenance.py"
    )
    installer = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(installer)
    base = tmp_path / "usr/local/lib/flowweave-storage-maintenance"
    units = tmp_path / "etc/systemd/system"
    settings = tmp_path / "etc/flowweave-storage-maintenance.json"
    base.mkdir(parents=True)
    units.mkdir(parents=True)
    (base / ".managed-schema-1").touch()
    (base / "prior").mkdir()
    (base / "current").symlink_to("prior")
    old_settings = b'{"schema":1,"root":"/deployment","build":"build"}'
    settings.write_bytes(old_settings)
    settings.chmod(0o600)
    for name in ("flowweave-storage-maintenance.service", "flowweave-storage-maintenance.timer"):
        (units / name).write_text(installer.OWNED + "\nold unit")
    actual_path = Path

    def path(value):
        p = actual_path(value)
        return tmp_path / str(p).lstrip("/") if p.is_absolute() else p

    monkeypatch.setattr(installer, "Path", path)
    monkeypatch.setattr(installer.os, "geteuid", lambda: 0)
    monkeypatch.setattr(
        installer.subprocess, "run", lambda *_a, **_k: type("R", (), {"returncode": 0})()
    )

    def run(args):
        if "-c" in args:
            compile(args[-1], "<remote-audit>", "exec")
            return '{"status":"dry-run"}\n'
        if args[:2] == ["systemctl", "start"] and args[-1].endswith(".service"):
            raise ValueError("first run failed")
        return ""

    monkeypatch.setattr(installer, "run", run)
    payload = {
        "root": "/deployment",
        "build": "build",
        "images": "images",
        "prune_build_cache": True,
        "commit": "a" * 40,
        "files": {name: base64.b64encode(b"new version").decode() for name in installer.FILES},
    }
    with pytest.raises(ValueError, match="first run failed"):
        installer.install_locked(payload)
    assert settings.read_bytes() == old_settings
    assert settings.stat().st_mode & 0o777 == 0o600
    assert os.readlink(base / "current") == "prior"
    assert all(
        (units / name).read_text().endswith("old unit")
        for name in ("flowweave-storage-maintenance.service", "flowweave-storage-maintenance.timer")
    )
