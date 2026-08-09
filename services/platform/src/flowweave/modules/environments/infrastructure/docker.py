from __future__ import annotations

import hashlib
import json
import os
import pty
import re
import socket
import struct
import subprocess
import termios
import time
from dataclasses import dataclass
from typing import Any, cast
from uuid import uuid4

from flowweave.shared.errors import DomainError
from flowweave.shared.settings import get_settings

_IMAGE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._/@:+-]{0,511}$")
_SAFE_NAME = re.compile(r"[^a-z0-9_.-]+")
_SENSITIVE_PATH_MARKERS = (
    "/.ssh",
    "/.aws",
    "/.kube",
    "/.gnupg",
    "/.lark",
    "/.local/share/lark-cli",
    "/.config/lark",
    "/.config/feishu",
    "/.docker/config.json",
    "/.npmrc",
    "/.pypirc",
    "/.netrc",
    "/.bash_history",
    "/credentials",
    "/token.json",
    "/cookies",
)


def validate_image(value: str) -> str:
    image = value.strip()
    if not _IMAGE.fullmatch(image) or ".." in image:
        raise DomainError("ENVIRONMENT_IMAGE_INVALID", "Runtime image reference is invalid", 422)
    return image


def _run(command: list[str], *, timeout: int = 60, input_text: str | None = None) -> str:
    try:
        completed = subprocess.run(
            command,
            input=input_text,
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
            env={"PATH": os.defpath},
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise DomainError(
            "ENVIRONMENT_BACKEND_UNAVAILABLE",
            "The terminal environment Docker backend is unavailable",
            503,
        ) from exc
    if completed.returncode:
        detail = (completed.stderr or completed.stdout)[-4000:]
        raise DomainError(
            "ENVIRONMENT_DOCKER_FAILED",
            "The terminal environment operation failed",
            502,
            {"detail": detail},
        )
    return completed.stdout.strip()


def require_backend() -> None:
    if get_settings().terminal_environment_backend != "docker":
        raise DomainError(
            "ENVIRONMENT_BACKEND_DISABLED",
            "Terminal environment management is not enabled on this server",
            503,
        )


@dataclass(frozen=True, slots=True)
class PublishedImage:
    reference: str
    digest: str
    manifest: dict[str, Any]


def start_setup_container(image: str, environment_id: str) -> str:
    require_backend()
    image = validate_image(image)
    name = f"fw-setup-{environment_id[:8]}-{uuid4().hex[:10]}"
    settings = get_settings()
    command = [
        settings.docker_binary,
        "run",
        "--detach",
        "--interactive",
        "--tty",
        "--name",
        name,
        "--network",
        settings.terminal_environment_setup_network,
        "--security-opt",
        "no-new-privileges",
        "--cap-drop",
        "ALL",
        "--cap-add",
        "CHOWN",
        "--cap-add",
        "DAC_OVERRIDE",
        "--cap-add",
        "FOWNER",
        "--cap-add",
        "SETGID",
        "--cap-add",
        "SETUID",
        "--pids-limit",
        str(settings.terminal_environment_pids_limit),
        "--memory",
        settings.terminal_environment_memory,
        "--cpus",
        str(settings.terminal_environment_cpus),
        "--label",
        "flowweave.managed=terminal-environment",
        "--label",
        f"flowweave.environment={environment_id}",
        image,
        "sh",
        "-c",
        "trap : TERM INT; while :; do sleep 3600; done",
    ]
    return _run(command, timeout=settings.terminal_environment_start_timeout_seconds) or name


def remove_container(container_id: str) -> None:
    require_backend()
    _run([get_settings().docker_binary, "rm", "--force", container_id], timeout=30)


def remove_runtime_container(container_id: str) -> None:
    if not container_id:
        return
    try:
        remove_container(container_id)
    except DomainError:
        # Runtime cleanup is idempotent. A Docker --rm/reaper policy may have
        # already removed the container after an executor failure.
        return


def remove_image(reference: str) -> None:
    """Best-effort removal used to compensate a publish transaction rollback."""

    if not reference:
        return
    try:
        require_backend()
        _run(
            [get_settings().docker_binary, "image", "rm", "--force", reference],
            timeout=30,
        )
    except DomainError:
        # Compensation is idempotent: the image may already have been removed
        # by an operator or a Docker image-pruning policy.
        return


def open_terminal(
    container_id: str, *, session_name: str | None = None
) -> tuple[int, subprocess.Popen[bytes]]:
    """Open an interactive shell, optionally backed by a persistent tmux session.

    The docker exec process is only an attachment. When its PTY disappears, tmux
    keeps the shell and its child processes alive inside the runtime container so a
    browser can reconnect without interrupting work.
    """

    require_backend()
    shell_command = ["bash"]
    if session_name:
        safe_session = _SAFE_NAME.sub("-", session_name.lower()).strip("-.")[:64]
        if not safe_session:
            raise DomainError(
                "ENVIRONMENT_TERMINAL_SESSION_INVALID",
                "The terminal session name is invalid",
                422,
            )
        shell_command = [
            "tmux",
            "new-session",
            "-A",
            "-s",
            safe_session,
            "bash",
        ]
    master, slave = pty.openpty()
    try:
        process = subprocess.Popen(
            [
                get_settings().docker_binary,
                "exec",
                "-it",
                "-e",
                "TERM=xterm-256color",
                container_id,
                *shell_command,
            ],
            stdin=slave,
            stdout=slave,
            stderr=slave,
            close_fds=True,
            env={"PATH": os.defpath},
        )
    finally:
        os.close(slave)
    return master, process


def resize_terminal(master: int, rows: int, columns: int) -> None:
    import fcntl

    size = struct.pack("HHHH", rows, columns, 0, 0)
    fcntl.ioctl(master, termios.TIOCSWINSZ, size)


def container_diff(container_id: str) -> list[str]:
    require_backend()
    output = _run([get_settings().docker_binary, "diff", container_id], timeout=30)
    return [line for line in output.splitlines() if line.strip()]


def _sensitive_paths(diff: list[str]) -> list[str]:
    """Return credential-like paths that still exist in the container.

    ``docker diff`` prefixes every path with ``A``, ``C``, or ``D``. Deleted
    paths are safe to publish and are especially common after the automatic
    cleanup performed immediately before this check.
    """

    detected: set[str] = set()
    for entry in diff:
        change, separator, path = entry.partition(" ")
        path = path.strip()
        if not separator or change not in {"A", "C"} or not path:
            continue
        if any(marker in path.lower() for marker in _SENSITIVE_PATH_MARKERS):
            detected.add(path)
    return sorted(detected)


def _clean_ephemeral_files(container_id: str) -> None:
    script = (
        "rm -rf /tmp/* /var/tmp/* /root/.cache /home/*/.cache; "
        # Authentication state must never become part of a published image.
        # Removing these paths keeps globally installed CLIs and skill files,
        # while clearing their user-specific tokens, keys, and app secrets.
        "rm -rf /root/.ssh /root/.aws /root/.kube /root/.gnupg "
        "/root/.lark /root/.lark-cli /root/.config/lark /root/.config/feishu "
        "/root/.local/share/lark-cli; "
        "rm -f /root/.docker/config.json /root/.npmrc /root/.pypirc "
        "/root/.netrc /root/.bash_history; "
        'for home in /home/*; do [ -d "$home" ] || continue; '
        'rm -rf "$home/.ssh" "$home/.aws" "$home/.kube" '
        '"$home/.gnupg" "$home/.lark" "$home/.lark-cli" '
        '"$home/.config/lark" "$home/.config/feishu" '
        '"$home/.local/share/lark-cli"; '
        'rm -f "$home/.docker/config.json" "$home/.npmrc" '
        '"$home/.pypirc" "$home/.netrc" "$home/.bash_history"; done; '
        "find /var/log -type f -exec sh -c ': > \"$1\"' _ {} \\; 2>/dev/null || true"
    )
    _run(
        [get_settings().docker_binary, "exec", container_id, "sh", "-c", script],
        timeout=60,
    )


def _inspect_commands(container_id: str) -> dict[str, str]:
    script = (
        "for c in sh bash python python3 pip pip3 node npm npx uv java javac mvn "
        "git ssh curl wget jq mysql zip unzip make gcc ip ping lark-cli agent-server; do "
        'if command -v "$c" >/dev/null 2>&1; then '
        'p=$(command -v "$c"); v=$($c --version 2>&1 | head -n 1 || true); '
        'printf \'%s\t%s\t%s\n\' "$c" "$p" "$v"; fi; done'
    )
    output = _run(
        [get_settings().docker_binary, "exec", container_id, "sh", "-c", script],
        timeout=60,
    )
    commands: dict[str, str] = {}
    for line in output.splitlines():
        parts = line.split("\t", 2)
        if len(parts) == 3:
            commands[parts[0]] = parts[2][:300]
    return commands


def publish_container(container_id: str, *, environment_id: str, version_no: int) -> PublishedImage:
    require_backend()
    # Some version commands create per-user state as a side effect (notably
    # ``lark-cli --version`` creates ``~/.lark-cli/cache``). Inspect tools
    # before the final cleanup so the image is committed from the exact
    # filesystem state that passed the sensitive-path scan.
    commands = _inspect_commands(container_id)
    _clean_ephemeral_files(container_id)
    diff = container_diff(container_id)
    sensitive = _sensitive_paths(diff)
    if sensitive:
        raise DomainError(
            "ENVIRONMENT_SENSITIVE_FILES_DETECTED",
            "Potential credential files must be removed before publishing",
            409,
            {"paths": sensitive[:100]},
        )
    if not _run(
        [
            get_settings().docker_binary,
            "exec",
            container_id,
            "sh",
            "-c",
            "command -v agent-server",
        ],
        timeout=30,
    ):
        raise DomainError(
            "ENVIRONMENT_RUNTIME_INCOMPATIBLE",
            "The environment must retain the OpenHands agent-server executable",
            409,
        )
    slug = _SAFE_NAME.sub("-", environment_id.lower()).strip("-.")[:32]
    reference = f"flowweave/environment-{slug}:v{version_no}"
    image_id = _run(
        [
            get_settings().docker_binary,
            "commit",
            "--pause=true",
            "--change",
            "ENTRYPOINT []",
            container_id,
            reference,
        ],
        timeout=get_settings().terminal_environment_publish_timeout_seconds,
    )
    inspected = json.loads(
        _run(
            [
                get_settings().docker_binary,
                "image",
                "inspect",
                reference,
                "--format",
                "{{json .}}",
            ],
            timeout=30,
        )
    )
    inspection = cast(dict[str, object], inspected) if isinstance(inspected, dict) else {}
    digest = str(inspection.get("Id") or image_id)
    manifest = {
        "schema_version": 1,
        "image_id": digest,
        "reference": reference,
        "architecture": inspection.get("Architecture"),
        "os": inspection.get("Os"),
        "commands": commands,
        "filesystem_change_count": len(diff),
        "filesystem_change_digest": hashlib.sha256("\n".join(diff).encode()).hexdigest(),
    }
    return PublishedImage(reference=reference, digest=digest, manifest=manifest)


def start_runtime_container(image: str, execution_id: str) -> tuple[str, str]:
    require_backend()
    image = validate_image(image)
    settings = get_settings()
    safe_execution = _SAFE_NAME.sub("-", execution_id.lower()).strip("-.")[:30]
    name = f"fw-runtime-{safe_execution}-{uuid4().hex[:8]}"
    command = [
        settings.docker_binary,
        "run",
        "--detach",
        "--name",
        name,
        "--network",
        settings.terminal_environment_runtime_network,
        "--security-opt",
        "no-new-privileges",
        "--cap-drop",
        "ALL",
        "--pids-limit",
        str(settings.terminal_environment_pids_limit),
        "--memory",
        settings.terminal_environment_memory,
        "--cpus",
        str(settings.terminal_environment_cpus),
        "--label",
        "flowweave.managed=agent-runtime",
        "--label",
        f"flowweave.execution={execution_id}",
        "--volumes-from",
        f"{settings.terminal_environment_workspace_source_container}:rw",
        "-e",
        f"SESSION_API_KEY={settings.openhands_session_api_key}",
        "-e",
        f"OH_SESSION_API_KEYS_0={settings.openhands_session_api_key}",
        image,
        "agent-server",
        "--host",
        "0.0.0.0",
        "--port",
        "8000",
    ]
    _run(command, timeout=settings.terminal_environment_start_timeout_seconds)
    base_url = f"http://{name}:8000"
    deadline = time.monotonic() + settings.terminal_environment_start_timeout_seconds
    while time.monotonic() < deadline:
        try:
            with socket.create_connection((name, 8000), timeout=1):
                return name, base_url
        except OSError:
            time.sleep(0.25)
    remove_runtime_container(name)
    raise DomainError(
        "ENVIRONMENT_RUNTIME_UNAVAILABLE",
        "The published environment Agent Server did not become ready",
        503,
    )
