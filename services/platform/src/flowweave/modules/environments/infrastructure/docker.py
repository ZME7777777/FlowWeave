from __future__ import annotations

import hashlib
import json
import os
import pty
import re
import signal
import struct
import subprocess
import termios
from dataclasses import dataclass
from typing import Any, cast

from flowweave.shared.errors import DomainError
from flowweave.shared.infrastructure.docker_control import (
    DockerControlError,
    DockerOwnershipError,
    inspect_owned_container,
)
from flowweave.shared.infrastructure.docker_controller import (
    DockerControllerClient,
    DockerControllerError,
    RemoteTerminal,
    controller_is_remote,
    wait_for_remote_terminal_output,
)
from flowweave.shared.settings import get_settings

_IMAGE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._/@:+-]{0,511}$")
_SAFE_NAME = re.compile(r"[^a-z0-9_.-]+")
_TERMINAL_PROMPT = r"flowweave@\h:\w\$ "
_TERMINAL_SHELL_SCRIPT = (
    "exec 3<<<'PS1=" + _TERMINAL_PROMPT + "'; exec bash --noprofile --rcfile /dev/fd/3 -i"
)
_TERMINAL_TMUX_SCRIPT = (
    'session="$1"; shell_script="$2"; columns="$3"; rows="$4"; '
    'if ! tmux has-session -t "$session" 2>/dev/null; then '
    'tmux new-session -d -x "$columns" -y "$rows" -s "$session" '
    'bash -c "$shell_script" '
    '|| tmux has-session -t "$session"; fi; '
    # Let xterm own pointer selection so dragged text remains selected and can
    # be copied after mouseup. tmux mouse mode would consume the drag and enter
    # its transient copy-mode selection instead.
    'tmux set-option -t "$session" mouse off; '
    'tmux set-option -t "$session" status off; '
    'tmux resize-window -t "$session": -x "$columns" -y "$rows"; '
    'exec tmux attach-session -t "$session"'
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


def _docker_resource_absent(exc: DomainError, resource: str) -> bool:
    detail = str(exc.details.get("detail") or "").lower()
    return exc.code == "ENVIRONMENT_DOCKER_FAILED" and (
        f"no such {resource}" in detail or f"{resource} not found" in detail
    )


def remove_legacy_setup_container(container_id: str, *, environment_id: str) -> None:
    """Safely remove a pre-ledger setup container after revalidating its labels."""

    if not container_id or not environment_id:
        return
    settings = get_settings()
    if controller_is_remote(settings):
        try:
            DockerControllerClient(settings).post(
                "/v1/environments/remove-legacy",
                {
                    "resource_name": container_id,
                    "resource_id": "legacy",
                    "environment_id": environment_id,
                },
                timeout=30,
            )
            return
        except DockerControllerError as exc:
            raise DomainError(
                "ENVIRONMENT_BACKEND_UNAVAILABLE",
                "The terminal environment controller is unavailable",
                503,
            ) from exc
    try:
        immutable_id = resolve_setup_container(
            container_id, sandbox_id=None, environment_id=environment_id
        )
    except DomainError as exc:
        if exc.code == "ENVIRONMENT_SETUP_CONTAINER_MISSING":
            return
        raise
    try:
        _run([get_settings().docker_binary, "rm", "--force", immutable_id], timeout=30)
    except DomainError as exc:
        if _docker_resource_absent(exc, "container"):
            return
        raise


def resolve_setup_container(
    resource_name: str,
    *,
    sandbox_id: str | None,
    environment_id: str,
) -> str:
    """Resolve a setup container name to an ownership-verified immutable ID."""

    require_backend()
    if controller_is_remote(get_settings()):
        raise DomainError(
            "ENVIRONMENT_LOCAL_DOCKER_DISABLED",
            "Container resolution is available only inside the sandbox controller",
            503,
        )
    if sandbox_id:
        try:
            identifier = inspect_owned_container(
                get_settings().docker_binary,
                resource_name,
                sandbox_id,
                expected_manager_scope=get_settings().sandbox_manager_scope,
                timeout=30,
            )
        except DockerOwnershipError as exc:
            raise DomainError(
                "ENVIRONMENT_CONTAINER_OWNERSHIP_MISMATCH",
                "The setup container is owned by another sandbox",
                409,
                {"container_id": resource_name, "sandbox_id": sandbox_id},
            ) from exc
        except DockerControlError as exc:
            raise DomainError(
                "ENVIRONMENT_BACKEND_UNAVAILABLE",
                "The terminal environment Docker backend is unavailable",
                503,
            ) from exc
        if identifier is None:
            raise DomainError(
                "ENVIRONMENT_SETUP_CONTAINER_MISSING",
                "The setup container no longer exists",
                409,
                {"container_id": resource_name, "sandbox_id": sandbox_id},
            )
        return identifier

    # Migration compatibility is deliberately strict. Pre-ledger containers
    # must still prove the historical environment ownership label.
    try:
        raw = _run(
            [
                get_settings().docker_binary,
                "inspect",
                resource_name,
                "--format",
                "{{json .}}",
            ],
            timeout=30,
        )
    except DomainError as exc:
        if _docker_resource_absent(exc, "container"):
            raise DomainError(
                "ENVIRONMENT_SETUP_CONTAINER_MISSING",
                "The setup container no longer exists",
                409,
                {"container_id": resource_name},
            ) from exc
        raise
    try:
        value = cast(object, json.loads(raw))
        if not isinstance(value, dict):
            raise ValueError("inspect response must be an object")
        data = cast(dict[str, object], value)
        config_value = data.get("Config")
        config = cast(dict[str, object], config_value) if isinstance(config_value, dict) else {}
        labels_value = config.get("Labels")
        labels = (
            {str(key): str(item) for key, item in cast(dict[object, object], labels_value).items()}
            if isinstance(labels_value, dict)
            else {}
        )
        identifier = str(data.get("Id") or "")
    except (json.JSONDecodeError, ValueError) as exc:
        raise DomainError(
            "ENVIRONMENT_DOCKER_PROTOCOL_ERROR",
            "Docker returned invalid setup container ownership data",
            502,
        ) from exc
    if (
        labels.get("flowweave.managed") != "terminal-environment"
        or labels.get("flowweave.environment") != environment_id
        or not identifier
    ):
        raise DomainError(
            "ENVIRONMENT_CONTAINER_OWNERSHIP_MISMATCH",
            "Legacy setup container ownership could not be verified",
            409,
            {"container_id": resource_name, "environment_id": environment_id},
        )
    return identifier


def remove_image(
    reference: str,
    *,
    expected_digest: str,
    environment_id: str | None = None,
    version_id: str | None = None,
    version_no: int | None = None,
) -> None:
    """Remove one image tag after verifying its digest and optional ownership."""

    if not reference or not expected_digest:
        return
    require_backend()
    settings = get_settings()
    if controller_is_remote(settings):
        try:
            DockerControllerClient(settings).post(
                "/v1/environments/remove-image",
                {
                    "reference": reference,
                    "expected_digest": expected_digest,
                    "environment_id": environment_id,
                    "version_id": version_id,
                    "version_no": version_no,
                },
                timeout=30,
            )
            return
        except DockerControllerError as exc:
            raise DomainError(
                "ENVIRONMENT_BACKEND_UNAVAILABLE",
                "The terminal environment controller is unavailable",
                503,
            ) from exc
    try:
        raw = _run(
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
    except DomainError as exc:
        if _docker_resource_absent(exc, "image"):
            return
        raise
    try:
        value = cast(object, json.loads(raw))
        if not isinstance(value, dict):
            raise ValueError("inspect response must be an object")
        inspection = cast(dict[str, object], value)
        actual_digest = str(inspection.get("Id") or "")
        config_value = inspection.get("Config")
        config = cast(dict[str, object], config_value) if isinstance(config_value, dict) else {}
        labels_value = config.get("Labels")
        labels = (
            {str(key): str(item) for key, item in cast(dict[object, object], labels_value).items()}
            if isinstance(labels_value, dict)
            else {}
        )
    except (json.JSONDecodeError, ValueError) as exc:
        raise DomainError(
            "ENVIRONMENT_DOCKER_PROTOCOL_ERROR",
            "Docker returned invalid image ownership data",
            502,
        ) from exc
    if actual_digest != expected_digest:
        raise DomainError(
            "ENVIRONMENT_IMAGE_OWNERSHIP_MISMATCH",
            "The environment image tag no longer points to the expected image",
            409,
            {
                "reference": reference,
                "expected_digest": expected_digest,
                "actual_digest": actual_digest,
            },
        )
    if version_id is not None:
        expected_labels = {
            "flowweave.managed": "environment-image",
            "flowweave.manager-scope": settings.sandbox_manager_scope,
            "flowweave.environment-id": environment_id or "",
            "flowweave.environment-version-id": version_id,
            "flowweave.environment-version-no": str(version_no or ""),
        }
        if any(labels.get(key) != expected for key, expected in expected_labels.items()):
            raise DomainError(
                "ENVIRONMENT_IMAGE_OWNERSHIP_MISMATCH",
                "The environment image ownership labels no longer match",
                409,
                {"reference": reference, "version_id": version_id},
            )
    try:
        # Remove only this version tag. Docker retains the image while another
        # tag or a container still references the same immutable content.
        _run([get_settings().docker_binary, "image", "rm", reference], timeout=30)
    except DomainError as exc:
        if _docker_resource_absent(exc, "image"):
            return
        raise


def open_terminal(
    container_id: str,
    *,
    session_name: str | None = None,
    rows: int = 24,
    columns: int = 80,
) -> tuple[int, subprocess.Popen[bytes]]:
    """Open an interactive shell, optionally backed by a persistent tmux session.

    The docker exec process is only an attachment. When its PTY disappears, tmux
    keeps the shell and its child processes alive inside the runtime container so a
    browser can reconnect without interrupting work.
    """

    require_backend()
    # Runtime containers deliberately use an isolated UID that may not have a
    # passwd entry. Bash's generated prompt then says `I have no name!`. Use a
    # stable product-facing prompt without changing the container identity or
    # weakening its user isolation.
    # Bash intentionally does not trust an inherited PS1 for a new interactive
    # shell. Feed the prompt through a private rcfile descriptor instead.
    shell_command = ["bash", "-c", _TERMINAL_SHELL_SCRIPT]
    if session_name:
        safe_session = _SAFE_NAME.sub("-", session_name.lower()).strip("-.")[:64]
        if not safe_session:
            raise DomainError(
                "ENVIRONMENT_TERMINAL_SESSION_INVALID",
                "The terminal session name is invalid",
                422,
            )
        shell_command = [
            "bash",
            "-c",
            _TERMINAL_TMUX_SCRIPT,
            "--",
            safe_session,
            _TERMINAL_SHELL_SCRIPT,
            str(columns),
            str(rows),
        ]
    master, slave = pty.openpty()
    try:
        # Full-screen programs inspect the terminal size during startup. Set a
        # valid size before docker exec starts instead of relying on a later
        # browser resize event.
        resize_terminal(master, rows, columns)
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


def resize_terminal(
    master: int,
    rows: int,
    columns: int,
    process: subprocess.Popen[bytes] | None = None,
) -> None:
    import fcntl

    # A transient zero-width browser layout used to reach this boundary as a
    # two-column resize. Keep the PTY usable even if an old or malformed client
    # bypasses the WebSocket/controller validation.
    safe_rows = max(2, min(rows, 200))
    safe_columns = max(20, min(columns, 400))
    size = struct.pack("HHHH", safe_rows, safe_columns, 0, 0)
    fcntl.ioctl(master, termios.TIOCSWINSZ, size)
    # docker exec copies the host PTY size into the container TTY when its CLI
    # process receives SIGWINCH. ioctl alone only changes the outer PTY and
    # leaves full-screen programs in the container at their previous size.
    if process is not None and process.poll() is None:
        process.send_signal(signal.SIGWINCH)


class ManagedTerminal:
    """Uniform attachment used by API WebSockets in local and remote modes."""

    def __init__(
        self,
        *,
        master: int | None = None,
        process: subprocess.Popen[bytes] | None = None,
        client: DockerControllerClient | None = None,
        remote: RemoteTerminal | None = None,
    ) -> None:
        self.master = master
        self.process = process
        self.client = client
        self.remote = remote

    def read(self) -> tuple[bytes, bool]:
        if self.client is not None and self.remote is not None:
            content, eof = self.client.read_terminal(self.remote)
            if not content and not eof:
                wait_for_remote_terminal_output()
            return content, eof
        if self.master is None or self.process is None:
            return b"", True
        try:
            content = os.read(self.master, 8192)
        except OSError:
            return b"", True
        return content, self.process.poll() is not None

    def write(self, content: bytes) -> None:
        if self.client is not None and self.remote is not None:
            self.client.write_terminal(self.remote, content)
        elif self.master is not None:
            os.write(self.master, content)

    def resize(self, rows: int, columns: int) -> None:
        if self.client is not None and self.remote is not None:
            self.client.resize_terminal(self.remote, rows, columns)
        elif self.master is not None:
            resize_terminal(self.master, rows, columns, self.process)

    def close(self) -> None:
        if self.client is not None and self.remote is not None:
            self.client.close_terminal(self.remote)
            return
        if self.process is not None and self.process.poll() is None:
            self.process.terminate()
            try:
                self.process.wait(timeout=2)
            except subprocess.TimeoutExpired:
                self.process.kill()
                self.process.wait(timeout=2)
        if self.master is not None:
            os.close(self.master)
            self.master = None


def open_managed_terminal(
    resource_name: str,
    *,
    resource_id: str,
    environment_id: str | None = None,
    session_name: str | None = None,
    rows: int = 24,
    columns: int = 80,
) -> ManagedTerminal:
    """Open an ownership-checked terminal locally or through the controller."""

    settings = get_settings()
    require_backend()
    if controller_is_remote(settings):
        client = DockerControllerClient(settings)
        try:
            remote = client.start_terminal(
                resource_name=resource_name,
                resource_id=resource_id,
                environment_id=environment_id,
                session_name=session_name,
                rows=rows,
                columns=columns,
            )
        except DockerControllerError as exc:
            raise DomainError(
                "ENVIRONMENT_BACKEND_UNAVAILABLE",
                "The terminal environment controller is unavailable",
                503,
            ) from exc
        return ManagedTerminal(client=client, remote=remote)
    immutable_id = resolve_setup_container(
        resource_name, sandbox_id=resource_id, environment_id=environment_id or ""
    )
    master, process = open_terminal(
        immutable_id, session_name=session_name, rows=rows, columns=columns
    )
    return ManagedTerminal(master=master, process=process)


def container_diff(container_id: str) -> list[str]:
    require_backend()
    output = _run([get_settings().docker_binary, "diff", container_id], timeout=30)
    return [line for line in output.splitlines() if line.strip()]


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


def _inspect_runtime_provenance(container_id: str) -> dict[str, Any]:
    script = r"""
import json
from importlib.metadata import PackageNotFoundError, version
from pathlib import Path

packages = {}
for name in (
    "openhands-agent-server",
    "openhands-sdk",
    "openhands-tools",
    "openhands-workspace",
):
    try:
        packages[name] = version(name)
    except PackageNotFoundError:
        packages[name] = None

source_commit = None
source_ref = None
try:
    provenance = json.loads(
        Path("/runtime/openhands-source-provenance.json").read_text(encoding="utf-8")
    )
    build = provenance.get("build_input", {})
    source_commit = build.get("source_commit")
    source_ref = build.get("upstream_base_commit") or source_commit
except (OSError, TypeError, ValueError):
    pass

print(json.dumps({
    "package_versions": packages,
    "source_commit": source_commit,
    "source_ref": source_ref,
}))
"""
    raw = _run(
        [
            get_settings().docker_binary,
            "exec",
            container_id,
            "/runtime/.venv/bin/python",
            "-c",
            script,
        ],
        timeout=30,
    )
    try:
        value = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise DomainError(
            "ENVIRONMENT_RUNTIME_INCOMPATIBLE",
            "The environment Runtime provenance is invalid",
            409,
        ) from exc
    if not isinstance(value, dict):
        raise DomainError(
            "ENVIRONMENT_RUNTIME_INCOMPATIBLE",
            "The environment Runtime provenance is invalid",
            409,
        )
    return cast(dict[str, Any], value)


def publish_container(
    container_id: str,
    *,
    environment_id: str,
    version_id: str,
    version_no: int,
) -> PublishedImage:
    require_backend()
    settings = get_settings()
    slug = _SAFE_NAME.sub("-", environment_id.lower()).strip("-.")[:32]
    # Include the immutable database identity in the tag. The human version
    # number remains visible, while two publishers can never target the same
    # mutable Docker name between the ownership preflight and commit.
    version_token = version_id.replace("-", "").lower()
    reference = f"flowweave/environment-{slug}:v{version_no}-{version_token}"
    expected_labels = {
        "flowweave.managed": "environment-image",
        "flowweave.manager-scope": settings.sandbox_manager_scope,
        "flowweave.environment-id": environment_id,
        "flowweave.environment-version-id": version_id,
        "flowweave.environment-version-no": str(version_no),
    }

    # A retry after Docker commit but before the database CAS reuses only an
    # image carrying the exact immutable version identity. Never retarget a
    # predictable managed tag that another actor already owns.
    try:
        existing_raw = _run(
            [settings.docker_binary, "image", "inspect", reference, "--format", "{{json .}}"],
            timeout=30,
        )
    except DomainError as exc:
        if not _docker_resource_absent(exc, "image"):
            raise
    else:
        try:
            existing_value = cast(object, json.loads(existing_raw))
            if not isinstance(existing_value, dict):
                raise ValueError("inspect response must be an object")
            existing = cast(dict[str, object], existing_value)
            config_value = existing.get("Config")
            config = cast(dict[str, object], config_value) if isinstance(config_value, dict) else {}
            labels_value = config.get("Labels")
            labels = (
                {
                    str(key): str(value)
                    for key, value in cast(dict[object, object], labels_value).items()
                }
                if isinstance(labels_value, dict)
                else {}
            )
            digest = str(existing.get("Id") or "")
        except (json.JSONDecodeError, ValueError) as exc:
            raise DomainError(
                "ENVIRONMENT_DOCKER_PROTOCOL_ERROR",
                "Docker returned invalid image ownership data",
                502,
            ) from exc
        if not digest or any(
            labels.get(key) != expected for key, expected in expected_labels.items()
        ):
            raise DomainError(
                "ENVIRONMENT_IMAGE_TAG_CONFLICT",
                "The target environment image tag is already owned by another image",
                409,
                {"reference": reference, "version_id": version_id},
            )
        manifest: dict[str, Any] = {
            "schema_version": 1,
            "image_id": digest,
            "reference": reference,
            "architecture": existing.get("Architecture"),
            "os": existing.get("Os"),
            "commands": {},
            "runtime_provenance": _inspect_runtime_provenance(container_id),
            "recovered": True,
        }
        return PublishedImage(reference=reference, digest=digest, manifest=manifest)

    # Publishing intentionally preserves the container filesystem as-is,
    # including authentication files, caches, shell history, and other local
    # state created during setup.
    commands = _inspect_commands(container_id)
    runtime_provenance = _inspect_runtime_provenance(container_id)
    diff = container_diff(container_id)
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
    image_id = _run(
        [
            settings.docker_binary,
            "commit",
            "--pause=true",
            "--change",
            "ENTRYPOINT []",
            *[
                part
                for key, value in expected_labels.items()
                for part in ("--change", f"LABEL {key}={value}")
            ],
            container_id,
            reference,
        ],
        timeout=settings.terminal_environment_publish_timeout_seconds,
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
    config_value = inspection.get("Config")
    config = cast(dict[str, object], config_value) if isinstance(config_value, dict) else {}
    labels_value = config.get("Labels")
    labels = (
        {str(key): str(value) for key, value in cast(dict[object, object], labels_value).items()}
        if isinstance(labels_value, dict)
        else {}
    )
    if not digest or any(labels.get(key) != expected for key, expected in expected_labels.items()):
        raise DomainError(
            "ENVIRONMENT_IMAGE_OWNERSHIP_MISMATCH",
            "Docker did not preserve the environment image ownership labels",
            502,
            {"reference": reference, "version_id": version_id},
        )
    manifest = {
        "schema_version": 1,
        "image_id": digest,
        "reference": reference,
        "architecture": inspection.get("Architecture"),
        "os": inspection.get("Os"),
        "commands": commands,
        "runtime_provenance": runtime_provenance,
        "filesystem_change_count": len(diff),
        "filesystem_change_digest": hashlib.sha256("\n".join(diff).encode()).hexdigest(),
    }
    return PublishedImage(reference=reference, digest=digest, manifest=manifest)


def publish_setup_container(
    resource_name: str,
    *,
    sandbox_id: str,
    environment_id: str,
    version_id: str,
    version_no: int,
) -> PublishedImage:
    """Publish only after the controller/local adapter revalidates ownership."""

    settings = get_settings()
    require_backend()
    if controller_is_remote(settings):
        try:
            raw = DockerControllerClient(settings).post(
                "/v1/environments/publish",
                {
                    "resource_name": resource_name,
                    "resource_id": sandbox_id,
                    "environment_id": environment_id,
                    "version_id": version_id,
                    "version_no": version_no,
                },
                timeout=settings.terminal_environment_publish_timeout_seconds + 30,
            )
        except DockerControllerError as exc:
            raise DomainError(
                "ENVIRONMENT_BACKEND_UNAVAILABLE",
                "The terminal environment controller is unavailable",
                503,
            ) from exc
        manifest = raw.get("manifest")
        if not isinstance(manifest, dict):
            raise DomainError(
                "ENVIRONMENT_DOCKER_PROTOCOL_ERROR",
                "The controller returned invalid image metadata",
                502,
            )
        return PublishedImage(
            reference=str(raw.get("reference") or ""),
            digest=str(raw.get("digest") or ""),
            manifest=cast(dict[str, Any], manifest),
        )
    immutable_id = resolve_setup_container(
        resource_name, sandbox_id=sandbox_id, environment_id=environment_id
    )
    return publish_container(
        immutable_id,
        environment_id=environment_id,
        version_id=version_id,
        version_no=version_no,
    )
