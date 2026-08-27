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
_DIGEST_LOCKED_IMAGE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._/:+-]{0,430}@sha256:[0-9a-f]{64}$")
_SAFE_NAME = re.compile(r"[^a-z0-9_.-]+")
_TERMINAL_PROMPT = r"flowweave@\h:\w\$ "
_AGENT_PROJECT_ROOT = "/runtime/workspace/project"
_TERMINAL_SHELL_SCRIPT = (
    "exec 3<<<'PS1=" + _TERMINAL_PROMPT + "'; exec bash --noprofile --rcfile /dev/fd/3 -i"
)
_TERMINAL_TMUX_SCRIPT = (
    'session="$1"; shell_script="$2"; columns="$3"; rows="$4"; '
    'if ! tmux has-session -t "$session" 2>/dev/null; then '
    'tmux new-session -d -x "$columns" -y "$rows" -s "$session" '
    'bash -c "$shell_script" '
    '|| tmux has-session -t "$session"; fi; '
    # Let tmux receive pointer input so wheel events enter its persistent
    # copy-mode scrollback instead of being interpreted by the shell.
    'tmux set-option -t "$session" mouse on; '
    'tmux set-option -t "$session" status off; '
    'tmux resize-window -t "$session": -x "$columns" -y "$rows"; '
    'exec tmux attach-session -t "$session"'
)


def validate_image(value: str) -> str:
    image = value.strip()
    if not _IMAGE.fullmatch(image) or ".." in image:
        raise DomainError("ENVIRONMENT_IMAGE_INVALID", "Runtime image reference is invalid", 422)
    return image


def resolve_setup_image(value: str) -> tuple[str, str]:
    """Resolve the platform-owned setup seed and freeze its local content digest."""

    reference = validate_image(value)
    digest_locked = bool(_DIGEST_LOCKED_IMAGE.fullmatch(reference))
    settings = get_settings()
    require_backend()
    if controller_is_remote(settings):
        try:
            raw = DockerControllerClient(settings).post(
                "/v1/environments/resolve-base-image",
                {"reference": reference},
                timeout=settings.terminal_environment_publish_timeout_seconds,
            )
        except DockerControllerError as exc:
            raise DomainError(
                "ENVIRONMENT_BACKEND_UNAVAILABLE",
                "The terminal environment controller is unavailable",
                503,
            ) from exc
        canonical = str(raw.get("reference") or "")
        digest = str(raw.get("digest") or "")
        if (
            not _IMAGE.fullmatch(canonical)
            or ".." in canonical
            or not re.fullmatch(r"sha256:[0-9a-f]{64}", digest)
        ):
            raise DomainError(
                "ENVIRONMENT_DOCKER_PROTOCOL_ERROR",
                "The controller returned invalid base image provenance",
                502,
            )
        return canonical, digest

    # The operator may configure a local build tag for the platform-owned seed.
    # Freeze the inspected content ID on each Environment before any setup
    # session starts; later tag movement cannot change that Environment.
    try:
        _run(
            [settings.docker_binary, "image", "inspect", reference, "--format", "{{.Id}}"],
            timeout=30,
        )
    except DomainError as exc:
        if not _docker_resource_absent(exc, "image"):
            raise
        _run(
            [settings.docker_binary, "pull", reference],
            timeout=settings.terminal_environment_publish_timeout_seconds,
        )
    raw = _run(
        [settings.docker_binary, "image", "inspect", reference, "--format", "{{json .}}"],
        timeout=30,
    )
    try:
        value_json = cast(object, json.loads(raw))
        if not isinstance(value_json, dict):
            raise ValueError("inspect response must be an object")
        inspection = cast(dict[str, object], value_json)
        digest = str(inspection.get("Id") or "")
        repo_digests_value = inspection.get("RepoDigests")
        repo_digests = (
            [str(item) for item in cast(list[object], repo_digests_value)]
            if isinstance(repo_digests_value, list)
            else []
        )
        if digest_locked:
            requested_digest = reference.rsplit("@", 1)[1]
            canonical = next(item for item in repo_digests if item.endswith(f"@{requested_digest}"))
        else:
            canonical = reference
    except (json.JSONDecodeError, StopIteration, ValueError) as exc:
        raise DomainError(
            "ENVIRONMENT_BASE_IMAGE_PROVENANCE_INVALID",
            "Docker could not freeze the platform setup image",
            409,
        ) from exc
    if not re.fullmatch(r"sha256:[0-9a-f]{64}", digest):
        raise DomainError(
            "ENVIRONMENT_BASE_IMAGE_PROVENANCE_INVALID",
            "Docker omitted the immutable base image content digest",
            409,
        )
    return canonical, digest


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


@dataclass(frozen=True, slots=True)
class OpenHandsBuild:
    reference: str
    log_digest: str
    telemetry: dict[str, Any]
    target: str
    platform: str


_OPENHANDS_BUILD_SCRIPT = r"""
import json
import sys

from openhands.agent_server.docker.build import BuildOptions, build_with_telemetry

options = BuildOptions.model_validate(json.loads(sys.argv[1]))
result = build_with_telemetry(options)
print("FLOWWEAVE_OPENHANDS_BUILD=" + result.model_dump_json())
"""


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
    working_dir: str | None = None,
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
        command = [
            get_settings().docker_binary,
            "exec",
            "-it",
            "-e",
            "TERM=xterm-256color",
        ]
        if working_dir is not None:
            if not working_dir.startswith("/") or ".." in working_dir.split("/"):
                raise DomainError(
                    "ENVIRONMENT_TERMINAL_WORKDIR_INVALID",
                    "The terminal working directory is invalid",
                    422,
                )
            command.extend(["--workdir", working_dir])
        command.extend([container_id, *shell_command])
        process = subprocess.Popen(
            command,
            stdin=slave,
            stdout=slave,
            stderr=slave,
            close_fds=True,
            env={"PATH": os.defpath},
        )
    finally:
        os.close(slave)
    return master, process


def destroy_terminal_session(container_id: str, session_name: str) -> None:
    """Stop one exact persistent tmux session without affecting the Runtime."""

    require_backend()
    safe_session = _SAFE_NAME.sub("-", session_name.lower()).strip("-.")[:64]
    if not safe_session:
        raise DomainError(
            "ENVIRONMENT_TERMINAL_SESSION_INVALID",
            "The terminal session name is invalid",
            422,
        )
    _run(
        [
            get_settings().docker_binary,
            "exec",
            container_id,
            "bash",
            "-c",
            'tmux has-session -t "$1" 2>/dev/null && tmux kill-session -t "$1" || true',
            "--",
            safe_session,
        ],
        timeout=15,
    )


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
    working_dir: str | None = None,
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
                working_dir=working_dir,
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
    if environment_id is not None:
        immutable_id = resolve_setup_container(
            resource_name, sandbox_id=resource_id, environment_id=environment_id
        )
    else:
        try:
            immutable_id = inspect_owned_container(
                settings.docker_binary,
                resource_name,
                resource_id,
                expected_manager_scope=settings.sandbox_manager_scope,
                expected_kind="agent-runtime",
                timeout=30,
            )
        except DockerOwnershipError as exc:
            raise DomainError(
                "AGENT_TERMINAL_OWNERSHIP_MISMATCH",
                "The Agent Runtime container is owned by another resource",
                409,
            ) from exc
        except DockerControlError as exc:
            raise DomainError(
                "AGENT_TERMINAL_BACKEND_UNAVAILABLE",
                "The Agent Runtime container could not be verified",
                503,
            ) from exc
        if immutable_id is None:
            raise DomainError(
                "AGENT_TERMINAL_UNAVAILABLE",
                "The Agent Runtime container no longer exists",
                409,
            )
    master, process = open_terminal(
        immutable_id,
        session_name=session_name,
        working_dir=(None if environment_id is not None else working_dir or _AGENT_PROJECT_ROOT),
        rows=rows,
        columns=columns,
    )
    return ManagedTerminal(master=master, process=process)


def destroy_managed_terminal_session(
    resource_name: str,
    *,
    resource_id: str,
    session_name: str,
) -> None:
    """Destroy one owned Agent Runtime terminal session locally or remotely."""

    settings = get_settings()
    require_backend()
    if controller_is_remote(settings):
        try:
            DockerControllerClient(settings).destroy_terminal_session(
                resource_name=resource_name,
                resource_id=resource_id,
                session_name=session_name,
            )
        except DockerControllerError as exc:
            raise DomainError(
                "AGENT_TERMINAL_BACKEND_UNAVAILABLE",
                "The Agent Runtime terminal could not be closed",
                503,
            ) from exc
        return
    try:
        immutable_id = inspect_owned_container(
            settings.docker_binary,
            resource_name,
            resource_id,
            expected_manager_scope=settings.sandbox_manager_scope,
            expected_kind="agent-runtime",
            timeout=30,
        )
    except DockerOwnershipError as exc:
        raise DomainError(
            "AGENT_TERMINAL_OWNERSHIP_MISMATCH",
            "The Agent Runtime container is owned by another resource",
            409,
        ) from exc
    except DockerControlError as exc:
        raise DomainError(
            "AGENT_TERMINAL_BACKEND_UNAVAILABLE",
            "The Agent Runtime container could not be verified",
            503,
        ) from exc
    if immutable_id is None:
        raise DomainError(
            "AGENT_TERMINAL_UNAVAILABLE",
            "The Agent Runtime container no longer exists",
            409,
        )
    destroy_terminal_session(immutable_id, session_name)


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

source = {}
try:
    provenance_path = Path("/runtime/openhands-source-provenance.json")
    if not provenance_path.is_file():
        provenance_path = Path(
            "/agent-server/openhands-agent-server/openhands/agent_server/"
            "openhands-source-provenance.json"
        )
    provenance = json.loads(provenance_path.read_text(encoding="utf-8"))
    build = provenance.get("build_input", {})
    source = {
        "repository": build.get("repository"),
        "source_commit": build.get("source_commit"),
        "source_ref": build.get("upstream_base_commit") or build.get("source_commit"),
        "source_archive_digest": provenance.get("source_archive_sha256"),
        "overlays": provenance.get("overlays", {}),
    }
except (OSError, TypeError, ValueError):
    pass

print(json.dumps({
    "package_versions": packages,
    **source,
}))
"""
    raw = _run(
        [
            get_settings().docker_binary,
            "exec",
            container_id,
            "/agent-server/.venv/bin/python",
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


def _build_openhands_runtime(
    *, base_image: str, environment_id: str, version_id: str, version_no: int, platform: str
) -> OpenHandsBuild:
    """Invoke the pinned OpenHands Agent Server's formal Docker build entrypoint."""

    settings = get_settings()
    slug = _SAFE_NAME.sub("-", environment_id.lower()).strip("-.")[:32]
    version_token = version_id.replace("-", "").lower()
    repository = f"flowweave/environment-{slug}-runtime"
    custom_tag = f"v{version_no}-{version_token}"
    options = {
        "base_image": base_image,
        "custom_tags": custom_tag,
        "image": repository,
        "target": "source-minimal",
        "platforms": [platform],
        "push": False,
        "include_base_tag": False,
        "include_versioned_tag": False,
        "git_sha": "f09e03eac772290feeb51b7d7390ffaefeca1a09",
        "git_ref": "f09e03eac772290feeb51b7d7390ffaefeca1a09",
    }
    output = _run(
        [
            settings.docker_binary,
            "run",
            "--rm",
            "--user",
            "0:0",
            "--mount",
            "type=bind,src=/var/run/docker.sock,dst=/var/run/docker.sock",
            "--workdir",
            "/opt/openhands-source",
            "--entrypoint",
            "/runtime/.venv/bin/python",
            settings.openhands_runtime_builder_image,
            "-c",
            _OPENHANDS_BUILD_SCRIPT,
            json.dumps(options, sort_keys=True),
        ],
        timeout=settings.terminal_environment_publish_timeout_seconds,
    )
    marker = "FLOWWEAVE_OPENHANDS_BUILD="
    result_line = next(
        (
            line.removeprefix(marker)
            for line in reversed(output.splitlines())
            if line.startswith(marker)
        ),
        None,
    )
    try:
        result = cast(dict[str, Any], json.loads(result_line or ""))
        tags = cast(list[object], result["tags"])
        reference = str(tags[0])
        telemetry = cast(dict[str, Any], result.get("telemetry") or {})
    except (KeyError, IndexError, TypeError, ValueError) as exc:
        raise DomainError(
            "ENVIRONMENT_OPENHANDS_BUILD_PROTOCOL_ERROR",
            "The OpenHands image builder returned invalid metadata",
            502,
        ) from exc
    return OpenHandsBuild(
        reference=reference,
        log_digest=hashlib.sha256(output.encode()).hexdigest(),
        telemetry=telemetry,
        target="source-minimal",
        platform=platform,
    )


def _probe_runtime_image(
    image_digest: str, *, probe_token: str
) -> tuple[dict[str, str], dict[str, Any], str]:
    settings = get_settings()
    probe_name = f"fw-env-probe-{probe_token[:24]}"
    try:
        _run(
            [
                settings.docker_binary,
                "run",
                "--detach",
                "--name",
                probe_name,
                "--entrypoint",
                "sh",
                image_digest,
                "-c",
                "trap : TERM INT; while :; do sleep 3600; done",
            ],
            timeout=30,
        )
        commands = _inspect_commands(probe_name)
        runtime_provenance = _inspect_runtime_provenance(probe_name)
        contract_output = _run(
            [
                settings.docker_binary,
                "exec",
                "--env",
                "HOME=/tmp",
                probe_name,
                "/agent-server/.venv/bin/python",
                # The FlowWeave probe is inherited from the fixed base image.
                # Do not execute a same-named file from OpenHands' source
                # directory: that would put its ``openai`` subpackage ahead
                # of the third-party dependency on sys.path.
                "/runtime/contract_check.py",
            ],
            timeout=120,
        )
        return (
            commands,
            runtime_provenance,
            hashlib.sha256(contract_output.encode()).hexdigest(),
        )
    finally:
        try:
            _run([settings.docker_binary, "rm", "--force", probe_name], timeout=30)
        except DomainError as exc:
            if not _docker_resource_absent(exc, "container"):
                raise


def publish_container(
    container_id: str,
    *,
    environment_id: str,
    version_id: str,
    version_no: int,
    base_image_reference: str,
    base_image_digest: str,
) -> PublishedImage:
    """Package a customized user base through OpenHands' formal build chain."""

    require_backend()
    settings = get_settings()
    slug = _SAFE_NAME.sub("-", environment_id.lower()).strip("-.")[:32]
    version_token = version_id.replace("-", "").lower()
    reference = f"flowweave/environment-{slug}:v{version_no}-{version_token}"
    customized_reference = f"flowweave/environment-{slug}-base:v{version_no}-{version_token}"
    expected_labels = {
        "flowweave.managed": "environment-image",
        "flowweave.manager-scope": settings.sandbox_manager_scope,
        "flowweave.environment-id": environment_id,
        "flowweave.environment-version-id": version_id,
        "flowweave.environment-version-no": str(version_no),
    }

    # A retry may reuse only a final image carrying the exact immutable
    # Environment Version identity.
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
        commands, runtime_provenance, contract_digest = _probe_runtime_image(
            digest, probe_token=version_token
        )
        platform = f"{existing.get('Os')}/{existing.get('Architecture')}"
        manifest: dict[str, Any] = {
            "schema_version": 1,
            "image_id": digest,
            "reference": reference,
            "architecture": existing.get("Architecture"),
            "os": existing.get("Os"),
            "commands": commands,
            "runtime_provenance": runtime_provenance,
            "build": {
                "builder": "openhands.agent_server.docker.build",
                "target": labels.get("flowweave.openhands-build-target", "source-minimal"),
                "platform": labels.get("flowweave.openhands-build-platform", platform),
                "log_digest": labels.get("flowweave.openhands-build-log-digest"),
                "user_base_image_reference": base_image_reference,
                "user_base_image_digest": base_image_digest,
                "runtime_image_reference": reference,
                "runtime_image_digest": digest,
            },
            "validation": {
                "contract_check": {"status": "PASSED", "output_digest": contract_digest},
                "tool_workspace_probe": {
                    "status": "PASSED",
                    "command_count": len(commands),
                },
                "security_scan": {"status": "NOT_RUN", "reason": "Deferred to FR-12"},
            },
            "recovered": True,
        }
        return PublishedImage(reference=reference, digest=digest, manifest=manifest)

    # Freeze the interactive setup filesystem first. This intermediate image
    # becomes the immutable user base passed to OpenHands BuildOptions.
    diff = container_diff(container_id)
    customized_digest = _run(
        [
            settings.docker_binary,
            "commit",
            "--pause=true",
            "--change",
            "ENTRYPOINT []",
            container_id,
            customized_reference,
        ],
        timeout=settings.terminal_environment_publish_timeout_seconds,
    )
    base_inspected = cast(
        dict[str, object],
        json.loads(
            _run(
                [
                    settings.docker_binary,
                    "image",
                    "inspect",
                    customized_reference,
                    "--format",
                    "{{json .}}",
                ],
                timeout=30,
            )
        ),
    )
    customized_digest = str(base_inspected.get("Id") or customized_digest)
    architecture = str(base_inspected.get("Architecture") or "")
    os_name = str(base_inspected.get("Os") or "linux")
    architecture_by_docker = {"x86_64": "amd64", "aarch64": "arm64"}
    platform_arch = architecture_by_docker.get(architecture, architecture)
    if os_name != "linux" or platform_arch not in {"amd64", "arm64"}:
        raise DomainError(
            "ENVIRONMENT_PLATFORM_UNSUPPORTED",
            "The user base image platform is not supported",
            422,
            {"os": os_name, "architecture": architecture},
        )
    platform = f"linux/{platform_arch}"
    build = _build_openhands_runtime(
        # ``docker image inspect`` returns a config digest (``sha256:…``),
        # which Dockerfile ``FROM`` treats as a registry image name rather
        # than a local image reference.  The unique commit tag is the local
        # addressable reference for that exact inspected image; the digest is
        # retained below as immutable provenance.
        base_image=customized_reference,
        environment_id=environment_id,
        version_id=version_id,
        version_no=version_no,
        platform=platform,
    )
    official_inspected = cast(
        dict[str, object],
        json.loads(
            _run(
                [
                    settings.docker_binary,
                    "image",
                    "inspect",
                    build.reference,
                    "--format",
                    "{{json .}}",
                ],
                timeout=30,
            )
        ),
    )
    official_digest = str(official_inspected.get("Id") or "")
    if not re.fullmatch(r"sha256:[0-9a-f]{64}", official_digest):
        raise DomainError(
            "ENVIRONMENT_OPENHANDS_BUILD_PROTOCOL_ERROR",
            "The OpenHands builder omitted the Runtime image digest",
            502,
        )

    # Add only governance labels on top of the formal OpenHands output. The
    # filesystem and entrypoint remain those produced by docker.build.
    dockerfile = "\n".join(
        [f"FROM {official_digest}"]
        + [f'LABEL {key}="{value}"' for key, value in expected_labels.items()]
        + [
            f'LABEL flowweave.openhands-build-log-digest="{build.log_digest}"',
            f'LABEL flowweave.openhands-build-target="{build.target}"',
            f'LABEL flowweave.openhands-build-platform="{build.platform}"',
            f'LABEL flowweave.user-base-image-digest="{base_image_digest}"',
            'ENV PATH="/agent-server/.venv/bin:${PATH}"',
            "ENTRYPOINT []",
            "",
        ]
    )
    _run(
        [settings.docker_binary, "build", "--tag", reference, "-"],
        timeout=settings.terminal_environment_publish_timeout_seconds,
        input_text=dockerfile,
    )
    inspection = cast(
        dict[str, object],
        json.loads(
            _run(
                [
                    settings.docker_binary,
                    "image",
                    "inspect",
                    reference,
                    "--format",
                    "{{json .}}",
                ],
                timeout=30,
            )
        ),
    )
    digest = str(inspection.get("Id") or "")
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

    commands, runtime_provenance, contract_digest = _probe_runtime_image(
        digest, probe_token=version_token
    )
    manifest = {
        "schema_version": 1,
        "image_id": digest,
        "reference": reference,
        "architecture": inspection.get("Architecture"),
        "os": inspection.get("Os"),
        "commands": commands,
        "runtime_provenance": runtime_provenance,
        "build": {
            "builder": "openhands.agent_server.docker.build",
            "target": build.target,
            "platform": build.platform,
            "log_digest": build.log_digest,
            "telemetry": build.telemetry,
            "user_base_image_reference": base_image_reference,
            "user_base_image_digest": base_image_digest,
            "customized_base_image_digest": customized_digest,
            "openhands_output_reference": build.reference,
            "openhands_output_digest": official_digest,
            "runtime_image_reference": reference,
            "runtime_image_digest": digest,
        },
        "validation": {
            "contract_check": {
                "status": "PASSED",
                "output_digest": contract_digest,
            },
            "tool_workspace_probe": {
                "status": "PASSED",
                "command_count": len(commands),
            },
            "security_scan": {
                "status": "NOT_RUN",
                "reason": "Deferred to the FR-12 security gate",
            },
        },
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
    base_image_reference: str,
    base_image_digest: str,
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
                    "base_image_reference": base_image_reference,
                    "base_image_digest": base_image_digest,
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
        base_image_reference=base_image_reference,
        base_image_digest=base_image_digest,
    )
