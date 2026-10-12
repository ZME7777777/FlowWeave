"""Execute HOME seeding against isolated directories, preserving real files."""

import shlex
import subprocess
import sys
from datetime import UTC, datetime, timedelta

import pytest

from flowweave.bootstrap.settings import Settings
from flowweave.modules.sandboxes.infrastructure.docker import DockerSandboxProvider
from flowweave.shared.models import ManagedSandbox


def provider():
    return DockerSandboxProvider(
        Settings(
            _env_file=None,
            terminal_environment_backend="docker",
            sandbox_manager_scope="cache-test",
        )
    )


def test_home_seed_skips_downloads_and_preserves_installed_tools(tmp_path, monkeypatch):
    root = tmp_path / "image-home"
    target = tmp_path / "volume-home"
    root.mkdir()
    target.mkdir()
    for name in (
        ".cache/uv/archive-v0/wheel",
        ".cache/pip/wheel",
        ".cache/pnpm/package",
        ".cache/node/headers",
        ".npm/package",
        ".cache/ms-playwright/chromium/browser",
        ".cache/.application/state",
        ".local/bin/tool",
        ".config/tool/config.json",
    ):
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("image")
    existing = target / ".config/tool/config.json"
    existing.parent.mkdir(parents=True)
    existing.write_text("existing-credential")
    previous = target / ".cache/uv/previous"
    previous.parent.mkdir(parents=True)
    previous.write_text("existing cache is not deleted by seeding")
    instance = provider()
    scripts = []
    monkeypatch.setattr(
        instance, "_run", lambda command, **_kwargs: scripts.append(command[-1]) or ""
    )
    instance._prepare_environment_home("volume", verified_image_reference="sha256:" + "a" * 64)
    script = (
        scripts[0]
        .replace("/flowweave-home", shlex.quote(str(target)))
        .replace("/root", shlex.quote(str(root)))
    )
    script = script.replace('chown -R 10001:10001 "$target"', ":")
    if sys.platform == "darwin":
        # BSD cp reports 1 (with no error) for a skipped -n target; GNU cp in
        # the Runtime reports 0. Normalize only that host-specific skip result.
        script = (
            """cp() {
          local output code
          output=$(command cp "$@" 2>&1) && return 0
          code=$?
          [ "$code" -eq 1 ] && [ -z "$output" ] && return 0
          printf '%s\\n' "$output" >&2
          return "$code"
        }
"""
            + script
        )
    subprocess.run(["sh", "-c", script], check=True, capture_output=True)
    assert not (target / ".cache/uv/archive-v0").exists()
    assert not any(
        (target / name).exists() for name in (".cache/pip", ".cache/pnpm", ".cache/node", ".npm")
    )
    assert (target / ".cache/ms-playwright/chromium/browser").read_text() == "image"
    assert (target / ".cache/.application/state").read_text() == "image"
    assert (target / ".local/bin/tool").read_text() == "image"
    assert existing.read_text() == "existing-credential"
    assert previous.exists()


@pytest.mark.parametrize("kind", ["ENVIRONMENT_SETUP", "AGENT_RUNTIME"])
def test_setup_and_agent_downloads_do_not_use_persistent_home(kind, monkeypatch):
    instance = provider()
    now = datetime.now(UTC)
    resource = ManagedSandbox(
        id="12345678-1234-4234-9234-123456789abc",
        kind=kind,
        owner_type="CAPABILITY_VALIDATION",
        owner_id="cache-test",
        backend="docker",
        backend_resource_name="cache-test",
        image_reference="sha256:" + "a" * 64,
        created_at=now,
        hard_expires_at=now + timedelta(hours=1),
        spec_json={"environment_id": "test-environment", "workspace_relative": "test"},
    )
    monkeypatch.setattr(
        instance, "_ensure_environment_credential_volume", lambda _id: "test-volume"
    )
    monkeypatch.setattr(instance, "_runtime_workspace_mount", lambda _resource: [])
    command = instance._create_command(resource, verified_image_reference=resource.image_reference)
    assert "UV_NO_CACHE=1" in command
    assert "PIP_NO_CACHE_DIR=1" in command
    assert "NPM_CONFIG_CACHE=/tmp/flowweave-package-cache/npm" in command
    assert any(p.startswith("/tmp:rw,nosuid,nodev,size=1g") for p in command)
