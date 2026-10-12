"""Scheduled retention safety; no Docker, SSH or database fixtures."""

import fcntl
import importlib.util
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


def test_images_keep_current_containers_and_three_rollbacks_each_repo():
    images = [image(i) for i in range(1, 9)]
    images += [image(i, [f"flowweave-web:rollback-{i}"]) for i in range(9, 14)]
    selected = maintenance.select_images(images, {"1", "9"}, {"2", "10"})
    assert set(selected) == {"3", "4", "5"}


def test_images_with_foreign_or_nonrollback_tags_are_never_candidates():
    images = [image(i) for i in range(1, 8)]
    images[0]["RepoTags"].append("another-app:rollback-old")
    images[1]["RepoTags"].append("flowweave-web:build-candidate")
    images[2]["RepoTags"] = None
    assert set(maintenance.select_images(images, set(), set())) == {"4"}


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
    monkeypatch.setattr(maintenance, "active_mutation", lambda: False)
    monkeypatch.setattr(maintenance.release, "docker_protection", lambda *_args: (set(), []))
    monkeypatch.setattr(maintenance, "image_plan", lambda: {})
    result = maintenance.maintain({"root": str(tmp_path), "build": "build"})
    assert result["candidate_contexts"] == 1
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
