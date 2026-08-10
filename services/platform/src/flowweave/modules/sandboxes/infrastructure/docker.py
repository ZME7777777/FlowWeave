from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess
import time
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import PurePosixPath
from typing import cast

from flowweave.bootstrap.settings import Settings
from flowweave.modules.sandboxes.infrastructure.models import ManagedSandbox
from flowweave.runtime.auth import derive_runtime_session_key
from flowweave.shared.errors import DomainError
from flowweave.shared.infrastructure.docker_control import (
    DockerControlError,
    DockerOwnershipError,
    docker_resource_is_absent,
    remove_owned_container,
    remove_owned_network,
    remove_owned_volume,
)
from flowweave.shared.infrastructure.docker_controller import (
    DockerControllerClient,
    DockerControllerError,
    controller_is_remote,
)


@dataclass(frozen=True, slots=True)
class DockerObservation:
    resource_id: str
    resource_name: str
    resource_identifier: str
    state: str
    labels: dict[str, str]
    resource_type: str = "container"


class DockerSandboxProvider:
    """Narrow Docker adapter. Callers cannot supply arbitrary flags or mounts."""

    def __init__(self, settings: Settings) -> None:
        self.settings = settings

    def require_enabled(self) -> None:
        if self.settings.terminal_environment_backend != "docker":
            raise DomainError(
                "ENVIRONMENT_BACKEND_DISABLED",
                "Terminal environment management is not enabled on this server",
                503,
            )

    def _run(self, command: list[str], *, timeout: int = 60) -> str:
        try:
            completed = subprocess.run(
                command,
                capture_output=True,
                text=True,
                timeout=timeout,
                check=False,
                env={"PATH": os.defpath},
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise DomainError(
                "SANDBOX_BACKEND_UNAVAILABLE",
                "The Docker sandbox backend is unavailable",
                503,
            ) from exc
        if completed.returncode:
            detail = (completed.stderr or completed.stdout)[-4000:]
            raise DomainError(
                "SANDBOX_DOCKER_FAILED",
                "The Docker sandbox operation failed",
                502,
                {"detail": detail},
            )
        return completed.stdout.strip()

    @staticmethod
    def _absent(exc: DomainError, resource: str = "container") -> bool:
        detail = str(exc.details.get("detail") or "").lower()
        return exc.code == "SANDBOX_DOCKER_FAILED" and docker_resource_is_absent(detail, resource)

    @staticmethod
    def _spec_hash(resource: ManagedSandbox) -> str:
        # The bound flag is a mutable lease marker. All remaining fields are
        # part of the immutable Docker contract.
        immutable = dict(resource.spec_json or {})
        immutable.pop("bound", None)
        encoded = json.dumps(
            immutable, ensure_ascii=True, separators=(",", ":"), sort_keys=True
        ).encode()
        return hashlib.sha256(encoded).hexdigest()

    @staticmethod
    def environment_credential_volume_name(environment_id: str) -> str:
        token = hashlib.sha256(environment_id.encode()).hexdigest()[:32]
        return f"fw-env-auth-{token}"

    def _inspect_environment_credential_volume(
        self, environment_id: str
    ) -> dict[str, object] | None:
        name = self.environment_credential_volume_name(environment_id)
        try:
            raw = self._run([self.settings.docker_binary, "volume", "inspect", name], timeout=30)
        except DomainError as exc:
            if self._absent(exc, "volume"):
                return None
            raise
        try:
            value = cast(object, json.loads(raw))
            if not isinstance(value, list):
                raise ValueError("volume inspect must contain one object")
            items = cast(list[object], value)
            if len(items) != 1 or not isinstance(items[0], dict):
                raise ValueError("volume inspect must contain one object")
            data = cast(dict[str, object], items[0])
            labels_value = data.get("Labels")
            labels = (
                {
                    str(key): str(item)
                    for key, item in cast(dict[object, object], labels_value).items()
                }
                if isinstance(labels_value, dict)
                else {}
            )
        except (json.JSONDecodeError, ValueError) as exc:
            raise DomainError(
                "SANDBOX_DOCKER_PROTOCOL_ERROR",
                "Docker returned invalid environment credential volume metadata",
                502,
            ) from exc
        if (
            str(data.get("Name") or "") != name
            or labels.get("flowweave.managed") != "true"
            or labels.get("flowweave.resource-type") != "environment-credential-volume"
            or labels.get("flowweave.environment-id") != environment_id
            or labels.get("flowweave.manager-scope") != self.settings.sandbox_manager_scope
        ):
            raise DomainError(
                "SANDBOX_RESOURCE_CONFLICT",
                "The environment credential volume does not match its ownership contract",
                409,
                {"resource_name": name, "environment_id": environment_id},
            )
        return data

    def _ensure_environment_credential_volume(self, environment_id: str) -> str:
        name = self.environment_credential_volume_name(environment_id)
        if self._inspect_environment_credential_volume(environment_id) is None:
            try:
                self._run(
                    [
                        self.settings.docker_binary,
                        "volume",
                        "create",
                        "--label",
                        "flowweave.managed=true",
                        "--label",
                        "flowweave.resource-type=environment-credential-volume",
                        "--label",
                        f"flowweave.manager-scope={self.settings.sandbox_manager_scope}",
                        "--label",
                        f"flowweave.environment-id={environment_id}",
                        name,
                    ],
                    timeout=30,
                )
            except DomainError:
                # A concurrent creator may have won. The ownership re-read is
                # authoritative and rejects any hostile same-name volume.
                if self._inspect_environment_credential_volume(environment_id) is None:
                    raise
            self._inspect_environment_credential_volume(environment_id)
        return name

    def _prepare_environment_home(self, volume_name: str, *, verified_image_reference: str) -> None:
        """Prepare one environment-owned HOME for the unprivileged Runtime.

        Environment setup containers intentionally run as root so operators can
        install system packages. Agent Runtimes run as uid/gid 10001. Both use
        this volume as their HOME, and this short, networkless helper bridges
        those identities without making the Runtime itself root.

        Older FlowWeave releases mounted this same volume directly at
        ``/root/.lark-cli``. Their Lark files therefore live at the volume root;
        migrate that recognizable layout into ``~/.lark-cli`` before the volume
        becomes a complete HOME. Files already present in the published image's
        root HOME (for example ``.ssh`` or ``.config/gh`` from an old release)
        are copied only when absent, preserving newer volume state.
        """

        script = r"""
set -eu
target=/flowweave-home
mkdir -p "$target"
if [ -f "$target/config.json" ] && [ ! -e "$target/.lark-cli/config.json" ] \
   && { [ -d "$target/cache" ] || [ -d "$target/logs" ] \
        || [ -f "$target/update-state.json" ]; }; then
  mkdir -p "$target/.lark-cli"
  for entry in config.json cache logs update-state.json skills.stamp; do
    if [ -e "$target/$entry" ] || [ -L "$target/$entry" ]; then
      mv "$target/$entry" "$target/.lark-cli/$entry"
    fi
  done
fi
if [ -d /root ]; then
  cp -a -n /root/. "$target/"
fi
chown -R 10001:10001 "$target"
chmod 0700 "$target"
"""
        self._run(
            [
                self.settings.docker_binary,
                "run",
                "--rm",
                "--network",
                "none",
                "--read-only",
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
                "--user",
                "0:0",
                "--mount",
                f"type=volume,src={volume_name},dst=/flowweave-home",
                "--entrypoint",
                "sh",
                verified_image_reference,
                "-c",
                script,
            ],
            timeout=60,
        )

    def delete_environment_credentials(self, environment_id: str) -> None:
        self.require_enabled()
        if controller_is_remote(self.settings):
            try:
                DockerControllerClient(self.settings).post(
                    "/v1/environments/remove-credentials",
                    {"environment_id": environment_id},
                    timeout=30,
                )
                return
            except DockerControllerError as exc:
                raise DomainError(
                    "SANDBOX_BACKEND_UNAVAILABLE",
                    "The Docker sandbox controller is unavailable",
                    503,
                ) from exc
        name = self.environment_credential_volume_name(environment_id)
        try:
            remove_owned_volume(
                self.settings.docker_binary,
                name,
                expected_environment_id=environment_id,
                expected_manager_scope=self.settings.sandbox_manager_scope,
                timeout=30,
            )
        except DockerOwnershipError as exc:
            raise DomainError(
                "SANDBOX_RESOURCE_CONFLICT",
                "The environment credential volume is owned by another environment",
                409,
                {"resource_name": name, "environment_id": environment_id},
            ) from exc
        except DockerControlError as exc:
            raise DomainError(
                "SANDBOX_BACKEND_UNAVAILABLE",
                "The environment credential volume could not be removed",
                503,
            ) from exc

    def _verify_resource_contract(
        self, observation: DockerObservation, resource: ManagedSandbox
    ) -> None:
        expected = {
            "flowweave.managed": "true",
            "flowweave.manager-scope": self.settings.sandbox_manager_scope,
            "flowweave.resource-id": resource.id,
            "flowweave.kind": resource.kind.lower().replace("_", "-"),
            "flowweave.owner-type": resource.owner_type,
            "flowweave.owner-id": resource.owner_id,
            "flowweave.image-reference": resource.image_reference,
            "flowweave.spec-hash": self._spec_hash(resource),
        }
        mismatches = {
            key: {"expected": value, "actual": observation.labels.get(key, "")}
            for key, value in expected.items()
            if observation.labels.get(key) != value
        }
        if mismatches:
            raise DomainError(
                "SANDBOX_RESOURCE_CONFLICT",
                "The Docker resource does not match the immutable sandbox contract",
                409,
                {"resource_name": observation.resource_name, "mismatches": mismatches},
            )

    @staticmethod
    def _is_image_digest(value: str) -> bool:
        return re.fullmatch(r"sha256:[0-9a-f]{64}", value) is not None

    def _inspect_image(self, reference: str) -> tuple[str, dict[str, str]]:
        raw = self._run(
            [
                self.settings.docker_binary,
                "image",
                "inspect",
                reference,
                "--format",
                "{{json .}}",
            ],
            timeout=30,
        )
        try:
            value = cast(object, json.loads(raw))
            if not isinstance(value, dict):
                raise ValueError("image inspect response must be an object")
            inspection = cast(dict[str, object], value)
            actual_digest = str(inspection.get("Id") or "")
            if not self._is_image_digest(actual_digest):
                raise ValueError("image inspect response omitted an immutable image ID")
            config_value = inspection.get("Config")
            config = cast(dict[str, object], config_value) if isinstance(config_value, dict) else {}
            labels_value = config.get("Labels")
            labels = (
                {
                    str(key): str(item)
                    for key, item in cast(dict[object, object], labels_value).items()
                }
                if isinstance(labels_value, dict)
                else {}
            )
        except (json.JSONDecodeError, ValueError) as exc:
            raise DomainError(
                "SANDBOX_DOCKER_PROTOCOL_ERROR",
                "Docker returned invalid environment image metadata",
                502,
            ) from exc
        return actual_digest, labels

    def _verify_image_trust(self, resource: ManagedSandbox) -> str:
        """Return the immutable Docker image ID that may be passed to `docker run`."""

        spec = resource.spec_json or {}
        base_version_id = str(spec.get("base_version_id") or "")
        if resource.kind == "ENVIRONMENT_SETUP" and not base_version_id:
            if resource.image_reference != self.settings.terminal_environment_base_image:
                raise DomainError(
                    "SANDBOX_IMAGE_UNTRUSTED",
                    "The setup sandbox image is not the administrator-approved base image",
                    422,
                )
            # The administrator-facing setting may be a convenient local tag.
            # Resolve it once and launch by the immutable image ID so a tag
            # replacement between validation and `docker run` cannot select a
            # different image.
            actual_digest, _labels = self._inspect_image(resource.image_reference)
            return actual_digest

        expected_digest = resource.image_reference
        if not self._is_image_digest(expected_digest):
            raise DomainError(
                "SANDBOX_IMAGE_UNTRUSTED",
                "The sandbox requires an immutable managed environment image",
                422,
            )
        actual_digest, labels = self._inspect_image(expected_digest)

        if resource.kind == "AGENT_RUNTIME":
            version_id = str(spec.get("environment_version_id") or "")
            version_no = str(spec.get("environment_version_no") or "")
        else:
            version_id = base_version_id
            version_no = str(spec.get("base_version_no") or "")
        expected_labels = {
            "flowweave.managed": "environment-image",
            "flowweave.manager-scope": self.settings.sandbox_manager_scope,
            "flowweave.environment-id": str(spec.get("environment_id") or ""),
            "flowweave.environment-version-id": version_id,
            "flowweave.environment-version-no": version_no,
        }
        if actual_digest != expected_digest or any(
            labels.get(key) != expected for key, expected in expected_labels.items()
        ):
            raise DomainError(
                "SANDBOX_IMAGE_UNTRUSTED",
                "The environment image digest or ownership labels do not match",
                409,
                {"image_reference": expected_digest, "version_id": version_id},
            )
        return actual_digest

    def ensure_running(self, resource: ManagedSandbox) -> DockerObservation:
        self.require_enabled()
        if controller_is_remote(self.settings):
            try:
                raw = DockerControllerClient(self.settings).post(
                    "/v1/sandboxes/ensure",
                    {
                        "id": resource.id,
                        "kind": resource.kind,
                        "owner_type": resource.owner_type,
                        "owner_id": resource.owner_id,
                        "backend_resource_name": resource.backend_resource_name,
                        "image_reference": resource.image_reference,
                        "spec": resource.spec_json or {},
                        "created_at": resource.created_at.isoformat(),
                    },
                    timeout=self.settings.terminal_environment_start_timeout_seconds + 30,
                )
            except DockerControllerError as exc:
                raise DomainError(
                    "SANDBOX_BACKEND_UNAVAILABLE",
                    "The Docker sandbox controller is unavailable",
                    503,
                ) from exc
            return self._observation_from_remote(raw)
        if resource.kind not in {"ENVIRONMENT_SETUP", "AGENT_RUNTIME"}:
            raise DomainError(
                "SANDBOX_KIND_UNSUPPORTED",
                "The Docker provider does not support this sandbox kind yet",
                422,
                {"kind": resource.kind},
            )
        # The controller must not turn possession of its API key into the
        # ability to launch an arbitrary local image. Verify the immutable
        # image identity before creating networks, mounts, or containers.
        verified_image_reference = self._verify_image_trust(resource)
        existing = self.inspect(resource.backend_resource_name)
        if existing is not None:
            # Never create or attach auxiliary resources until the immutable
            # container labels prove that the deterministic name is ours.
            self._verify_resource_contract(existing, resource)
            self._ensure_runtime_network(resource)
            if existing.state != "RUNNING":
                self._run(
                    [self.settings.docker_binary, "start", existing.resource_identifier],
                    timeout=30,
                )
                existing = self.inspect(resource.backend_resource_name)
                if existing is None:
                    raise DomainError(
                        "SANDBOX_RESOURCE_MISSING",
                        "The Docker sandbox disappeared while it was starting",
                        503,
                    )
                self._verify_resource_contract(existing, resource)
            self._isolate_runtime_container(resource, existing.resource_identifier)
            if resource.kind == "AGENT_RUNTIME":
                self._wait_for_agent_server(resource.backend_resource_name)
            return existing

        self._ensure_runtime_network(resource)
        home_volume = self._ensure_environment_credential_volume(
            str((resource.spec_json or {}).get("environment_id") or "")
        )
        self._prepare_environment_home(
            home_volume, verified_image_reference=verified_image_reference
        )
        command = self._create_command(resource, verified_image_reference=verified_image_reference)
        try:
            self._run(command, timeout=self.settings.terminal_environment_start_timeout_seconds)
        except DomainError as exc:
            detail = str(exc.details.get("detail") or "").lower()
            if "name is already in use" not in detail:
                raise
            # A concurrent retry may have created the same deterministic
            # container after our initial inspect. Ownership labels decide
            # whether this is idempotent success or a real conflict.
            observation = self.inspect(resource.backend_resource_name)
            if observation is None:
                raise
            self._verify_resource_contract(observation, resource)
            self._isolate_runtime_container(resource, observation.resource_identifier)
            if resource.kind == "AGENT_RUNTIME":
                self._wait_for_agent_server(resource.backend_resource_name)
            return observation
        observation = self.inspect(resource.backend_resource_name)
        if observation is None:
            raise DomainError(
                "SANDBOX_RESOURCE_MISSING",
                "Docker did not retain the newly created sandbox",
                503,
            )
        self._verify_resource_contract(observation, resource)
        self._isolate_runtime_container(resource, observation.resource_identifier)
        if resource.kind == "AGENT_RUNTIME":
            self._wait_for_agent_server(resource.backend_resource_name)
        return observation

    @staticmethod
    def _runtime_network_name(resource_id: str) -> str:
        return f"fw-net-{resource_id.replace('-', '')}"

    def _inspect_runtime_network(self, resource: ManagedSandbox) -> dict[str, object] | None:
        network_name = self._runtime_network_name(resource.id)
        network_mode = (
            self.settings.sandbox_runtime_network_mode
            if resource.kind == "AGENT_RUNTIME"
            else "egress"
        )
        network_purpose = (
            "agent-runtime" if resource.kind == "AGENT_RUNTIME" else "environment-setup"
        )
        expected_internal = network_mode == "isolated"
        try:
            raw = self._run(
                [self.settings.docker_binary, "network", "inspect", network_name],
                timeout=30,
            )
        except DomainError as exc:
            if self._absent(exc, "network"):
                return None
            raise
        try:
            value = cast(object, json.loads(raw))
            if not isinstance(value, list):
                raise ValueError("network inspect must contain one object")
            items = cast(list[object], value)
            if len(items) != 1 or not isinstance(items[0], dict):
                raise ValueError("network inspect must contain one object")
            data = cast(dict[str, object], items[0])
            labels_value = data.get("Labels")
            labels = (
                {
                    str(key): str(item)
                    for key, item in cast(dict[object, object], labels_value).items()
                }
                if isinstance(labels_value, dict)
                else {}
            )
            actual_name = str(data.get("Name") or "")
            driver = str(data.get("Driver") or "")
            internal = data.get("Internal")
        except (json.JSONDecodeError, ValueError) as exc:
            raise DomainError(
                "SANDBOX_DOCKER_PROTOCOL_ERROR",
                "Docker returned invalid Runtime network metadata",
                502,
            ) from exc
        if (
            actual_name != network_name
            or driver != "bridge"
            or internal is not expected_internal
            or labels.get("flowweave.managed") != "true"
            or labels.get("flowweave.resource-type") != "network"
            or labels.get("flowweave.resource-id") != resource.id
            or labels.get("flowweave.manager-scope") != self.settings.sandbox_manager_scope
            or labels.get("flowweave.network-purpose") != network_purpose
            or labels.get("flowweave.network-mode") != network_mode
        ):
            raise DomainError(
                "SANDBOX_RESOURCE_CONFLICT",
                "The sandbox network does not match its isolated network contract",
                409,
                {
                    "resource_name": network_name,
                    "expected_resource_id": resource.id,
                    "actual_name": actual_name,
                    "driver": driver,
                    "internal": internal,
                    "expected_network_mode": network_mode,
                },
            )
        return data

    def _trusted_runtime_clients(self) -> list[str]:
        identifiers = self._run(
            [
                self.settings.docker_binary,
                "ps",
                "--quiet",
                "--filter",
                "label=flowweave.runtime-client=true",
                "--filter",
                "label=flowweave.runtime-client-role=worker",
                "--filter",
                f"label=flowweave.manager-scope={self.settings.sandbox_manager_scope}",
            ],
            timeout=30,
        ).splitlines()
        clients = [item.strip() for item in identifiers if item.strip()]
        if not clients:
            raise DomainError(
                "SANDBOX_RUNTIME_CLIENT_UNAVAILABLE",
                "No trusted Runtime client is available",
                503,
            )
        return clients

    def _connect_network(self, network_name: str, container_id: str) -> None:
        try:
            self._run(
                [self.settings.docker_binary, "network", "connect", network_name, container_id],
                timeout=30,
            )
        except DomainError as exc:
            detail = str(exc.details.get("detail") or "").lower()
            if "already exists" not in detail and "already connected" not in detail:
                raise

    def _ensure_runtime_network(self, resource: ManagedSandbox) -> str:
        network_name = self._runtime_network_name(resource.id)
        if self._inspect_runtime_network(resource) is None:
            network_mode = (
                self.settings.sandbox_runtime_network_mode
                if resource.kind == "AGENT_RUNTIME"
                else "egress"
            )
            network_purpose = (
                "agent-runtime" if resource.kind == "AGENT_RUNTIME" else "environment-setup"
            )
            command = [
                self.settings.docker_binary,
                "network",
                "create",
                "--driver",
                "bridge",
            ]
            if network_mode == "isolated":
                command.append("--internal")
            command.extend(
                [
                    "--label",
                    "flowweave.managed=true",
                    "--label",
                    "flowweave.resource-type=network",
                    "--label",
                    f"flowweave.manager-scope={self.settings.sandbox_manager_scope}",
                    "--label",
                    f"flowweave.resource-id={resource.id}",
                    "--label",
                    f"flowweave.network-purpose={network_purpose}",
                    "--label",
                    f"flowweave.network-mode={network_mode}",
                    "--label",
                    f"flowweave.created-at={int(resource.created_at.timestamp())}",
                    network_name,
                ]
            )
            try:
                self._run(command, timeout=30)
            except DomainError as exc:
                detail = str(exc.details.get("detail") or "").lower()
                if "already exists" not in detail:
                    raise
            self._inspect_runtime_network(resource)
        if resource.kind == "AGENT_RUNTIME":
            for client_id in self._trusted_runtime_clients():
                self._connect_network(network_name, client_id)
        return network_name

    def _isolate_runtime_container(
        self, resource: ManagedSandbox, container_identifier: str
    ) -> None:
        network_name = self._ensure_runtime_network(resource)
        self._connect_network(network_name, container_identifier)
        raw = self._run(
            [
                self.settings.docker_binary,
                "inspect",
                container_identifier,
                "--format",
                "{{json .NetworkSettings.Networks}}",
            ],
            timeout=30,
        )
        try:
            value = cast(object, json.loads(raw))
            networks = list(cast(dict[object, object], value)) if isinstance(value, dict) else []
        except json.JSONDecodeError as exc:
            raise DomainError(
                "SANDBOX_DOCKER_PROTOCOL_ERROR",
                "Docker returned invalid Runtime attachment metadata",
                502,
            ) from exc
        for attached_name in networks:
            if str(attached_name) == network_name:
                continue
            self._run(
                [
                    self.settings.docker_binary,
                    "network",
                    "disconnect",
                    "--force",
                    str(attached_name),
                    container_identifier,
                ],
                timeout=30,
            )

    def _common_run_command(self, resource: ManagedSandbox) -> list[str]:
        return [
            self.settings.docker_binary,
            "run",
            "--detach",
            "--name",
            resource.backend_resource_name,
            "--log-driver",
            "local",
            "--log-opt",
            "max-size=8m",
            "--log-opt",
            "max-file=2",
            "--storage-opt",
            f"size={self.settings.sandbox_storage_size}",
            "--security-opt",
            "no-new-privileges",
            "--cap-drop",
            "ALL",
            "--pids-limit",
            str(self.settings.terminal_environment_pids_limit),
            "--memory",
            self.settings.terminal_environment_memory,
            "--cpus",
            str(self.settings.terminal_environment_cpus),
            "--label",
            "flowweave.managed=true",
            "--label",
            f"flowweave.manager-scope={self.settings.sandbox_manager_scope}",
            "--label",
            f"flowweave.resource-id={resource.id}",
            "--label",
            f"flowweave.kind={resource.kind.lower().replace('_', '-')}",
            "--label",
            f"flowweave.owner-type={resource.owner_type}",
            "--label",
            f"flowweave.owner-id={resource.owner_id}",
            "--label",
            f"flowweave.image-reference={resource.image_reference}",
            "--label",
            f"flowweave.spec-hash={self._spec_hash(resource)}",
            "--label",
            f"flowweave.created-at={int(resource.created_at.timestamp())}",
        ]

    def _create_command(
        self, resource: ManagedSandbox, *, verified_image_reference: str
    ) -> list[str]:
        if not self._is_image_digest(verified_image_reference):
            raise DomainError(
                "SANDBOX_IMAGE_UNTRUSTED",
                "The Docker sandbox launch image must be immutable",
                422,
            )
        command = self._common_run_command(resource)
        environment_id = str((resource.spec_json or {}).get("environment_id") or "")
        if not environment_id:
            raise DomainError(
                "SANDBOX_SPEC_INVALID",
                "The sandbox environment identity is missing",
                422,
            )
        credential_volume = self._ensure_environment_credential_volume(environment_id)
        if resource.kind == "ENVIRONMENT_SETUP":
            command.extend(
                [
                    "--interactive",
                    "--tty",
                    "--network",
                    self._runtime_network_name(resource.id),
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
                    "--mount",
                    f"type=volume,src={credential_volume},dst=/root",
                    "-e",
                    "HOME=/root",
                    verified_image_reference,
                    "sh",
                    "-c",
                    "trap : TERM INT; while :; do sleep 3600; done",
                ]
            )
            return command
        workspace_mount = self._runtime_workspace_mount(resource)
        session_key = derive_runtime_session_key(
            self.settings.openhands_session_api_key,
            self.settings.sandbox_manager_scope,
            resource.backend_resource_name,
        )
        command.extend(
            [
                "--network",
                self._runtime_network_name(resource.id),
                # Runtime images contain a full developer toolchain and execute
                # model-generated commands. Keep that workload away from root
                # and make the image itself immutable. Only the selected node
                # workspace and these bounded, in-memory runtime directories
                # remain writable. UID 10001 matches the platform workspace
                # owner used by the standard deployment.
                "--user",
                "10001:10001",
                "--read-only",
                "--tmpfs",
                "/tmp:rw,nosuid,nodev,size=128m,uid=10001,gid=10001,mode=1777",
                "--tmpfs",
                "/runtime/workspace:rw,nosuid,nodev,size=64m,uid=10001,gid=10001,mode=0700",
                "--mount",
                (f"type=volume,src={credential_volume},dst=/home/flowweave"),
                *workspace_mount,
                "-e",
                "HOME=/home/flowweave",
                "-e",
                "OPENHANDS_SUPPRESS_BANNER=1",
                "-e",
                f"SESSION_API_KEY={session_key}",
                "-e",
                f"OH_SESSION_API_KEYS_0={session_key}",
                verified_image_reference,
                "agent-server",
                "--host",
                "0.0.0.0",
                "--port",
                "8000",
            ]
        )
        return command

    def _runtime_workspace_mount(self, resource: ManagedSandbox) -> list[str]:
        relative_raw = str((resource.spec_json or {}).get("workspace_relative") or "")
        relative = PurePosixPath(relative_raw)
        if (
            not relative_raw
            or relative.is_absolute()
            or any(part in {"", ".", ".."} for part in relative.parts)
        ):
            raise DomainError(
                "SANDBOX_WORKSPACE_INVALID",
                "The Runtime workspace subdirectory is missing or invalid",
                422,
            )
        runtime_root = PurePosixPath(str(self.settings.openhands_workspace_root))
        if not runtime_root.is_absolute():
            raise DomainError(
                "SANDBOX_WORKSPACE_INVALID",
                "The Runtime workspace root must be absolute",
                503,
            )
        raw = self._run(
            [
                self.settings.docker_binary,
                "inspect",
                self.settings.terminal_environment_workspace_source_container,
                "--format",
                "{{json .}}",
            ],
            timeout=30,
        )
        try:
            value = cast(object, json.loads(raw))
            if not isinstance(value, dict):
                raise ValueError("source inspect data must be an object")
            source_data = cast(dict[str, object], value)
            config_value = source_data.get("Config")
            config = cast(dict[str, object], config_value) if isinstance(config_value, dict) else {}
            labels_value = config.get("Labels")
            labels = (
                {
                    str(key): str(item)
                    for key, item in cast(dict[object, object], labels_value).items()
                }
                if isinstance(labels_value, dict)
                else {}
            )
            if (
                labels.get("flowweave.workspace-source") != "true"
                or labels.get("flowweave.manager-scope") != self.settings.sandbox_manager_scope
            ):
                raise ValueError("source container ownership labels do not match")
            mounts_value = source_data.get("Mounts")
            if not isinstance(mounts_value, list):
                raise ValueError("mounts must be a list")
            matches = [
                cast(dict[str, object], item)
                for item in cast(list[object], mounts_value)
                if isinstance(item, dict)
                and str(cast(dict[str, object], item).get("Destination") or "") == str(runtime_root)
            ]
        except (json.JSONDecodeError, ValueError) as exc:
            raise DomainError(
                "SANDBOX_WORKSPACE_SOURCE_INVALID",
                "Docker returned invalid workspace mount metadata",
                503,
            ) from exc
        if len(matches) != 1:
            raise DomainError(
                "SANDBOX_WORKSPACE_SOURCE_INVALID",
                "The workspace source container must expose exactly one Runtime workspace mount",
                503,
            )
        source_mount = matches[0]
        mount_type = str(source_mount.get("Type") or "")
        target = runtime_root.joinpath(*relative.parts)
        if mount_type == "bind":
            source = PurePosixPath(str(source_mount.get("Source") or ""))
            if not source.is_absolute() or any(character in str(source) for character in ",="):
                raise DomainError(
                    "SANDBOX_WORKSPACE_SOURCE_INVALID",
                    "The workspace bind source must be an unambiguous absolute path",
                    503,
                )
            isolated_source = source.joinpath(*relative.parts)
            specification = f"type=bind,src={isolated_source},dst={target}"
        elif mount_type == "volume":
            name = str(source_mount.get("Name") or "")
            if not name or any(character in name for character in ",="):
                raise DomainError(
                    "SANDBOX_WORKSPACE_SOURCE_INVALID",
                    "The workspace volume name is missing or invalid",
                    503,
                )
            specification = (
                f"type=volume,src={name},dst={target},volume-subpath={relative.as_posix()}"
            )
        else:
            raise DomainError(
                "SANDBOX_WORKSPACE_SOURCE_INVALID",
                "The workspace source must be a bind mount or named volume",
                503,
            )
        return ["--mount", specification]

    def _wait_for_agent_server(self, resource_name: str) -> None:
        deadline = time.monotonic() + self.settings.terminal_environment_start_timeout_seconds
        while time.monotonic() < deadline:
            try:
                # The controller deliberately does not join the untrusted Runtime
                # network. Probe readiness from inside the owned container instead
                # of making the Docker-socket holder reachable from sandboxes.
                self._run(
                    [
                        self.settings.docker_binary,
                        "exec",
                        resource_name,
                        "/runtime/.venv/bin/python",
                        "-c",
                        (
                            "import socket; "
                            "connection=socket.create_connection(('127.0.0.1',8000),1); "
                            "connection.close()"
                        ),
                    ],
                    timeout=3,
                )
                return
            except DomainError:
                time.sleep(0.25)
        raise DomainError(
            "ENVIRONMENT_RUNTIME_UNAVAILABLE",
            "The published environment Agent Server did not become ready",
            503,
        )

    @staticmethod
    def _verify_owner(
        observation: DockerObservation,
        expected_resource_id: str,
        expected_manager_scope: str | None = None,
    ) -> None:
        scope_matches = (
            expected_manager_scope is None
            or observation.labels.get("flowweave.manager-scope") == expected_manager_scope
        )
        if (
            observation.labels.get("flowweave.managed") != "true"
            or observation.resource_id != expected_resource_id
            or not scope_matches
        ):
            raise DomainError(
                "SANDBOX_RESOURCE_CONFLICT",
                "The Docker resource name is owned by another sandbox",
                409,
                {
                    "resource_name": observation.resource_name,
                    "expected_resource_id": expected_resource_id,
                    "actual_resource_id": observation.resource_id,
                    "expected_manager_scope": expected_manager_scope,
                    "actual_manager_scope": observation.labels.get("flowweave.manager-scope"),
                },
            )

    def inspect(self, resource_name: str) -> DockerObservation | None:
        if controller_is_remote(self.settings):
            try:
                raw = (
                    DockerControllerClient(self.settings)
                    .post("/v1/sandboxes/inspect", {"resource_name": resource_name}, timeout=30)
                    .get("observation")
                )
            except DockerControllerError as exc:
                raise DomainError(
                    "SANDBOX_BACKEND_UNAVAILABLE",
                    "The Docker sandbox controller is unavailable",
                    503,
                ) from exc
            if raw is None:
                return None
            if not isinstance(raw, dict):
                raise DomainError(
                    "SANDBOX_DOCKER_PROTOCOL_ERROR", "Invalid controller inspect data", 502
                )
            return self._observation_from_remote(cast(dict[str, object], raw))
        try:
            raw = self._run(
                [
                    self.settings.docker_binary,
                    "inspect",
                    resource_name,
                    "--format",
                    "{{json .}}",
                ],
                timeout=30,
            )
        except DomainError as exc:
            if self._absent(exc):
                return None
            raise
        value = cast(object, json.loads(raw))
        if not isinstance(value, dict):
            raise DomainError("SANDBOX_DOCKER_PROTOCOL_ERROR", "Invalid Docker inspect data", 502)
        data = cast(dict[str, object], value)
        config_value = data.get("Config")
        config = cast(dict[str, object], config_value) if isinstance(config_value, dict) else {}
        state_value = data.get("State")
        state_data = cast(dict[str, object], state_value) if isinstance(state_value, dict) else {}
        labels_value: object = config.get("Labels")
        labels = (
            {str(key): str(item) for key, item in cast(dict[object, object], labels_value).items()}
            if isinstance(labels_value, dict)
            else {}
        )
        state = str(state_data.get("Status") or "unknown").upper()
        return DockerObservation(
            resource_id=labels.get("flowweave.resource-id", ""),
            resource_name=str(data.get("Name") or resource_name).lstrip("/"),
            resource_identifier=str(data.get("Id") or resource_name),
            state=state,
            labels=labels,
        )

    def delete_expected(self, resource_name: str, expected_resource_id: str) -> None:
        if controller_is_remote(self.settings):
            try:
                DockerControllerClient(self.settings).post(
                    "/v1/sandboxes/delete",
                    {
                        "resource_name": resource_name,
                        "resource_id": expected_resource_id,
                    },
                    timeout=30,
                )
                return
            except DockerControllerError as exc:
                raise DomainError(
                    "SANDBOX_BACKEND_UNAVAILABLE",
                    "The Docker sandbox controller is unavailable",
                    503,
                ) from exc
        try:
            remove_owned_container(
                self.settings.docker_binary,
                resource_name,
                expected_resource_id,
                expected_manager_scope=self.settings.sandbox_manager_scope,
                timeout=30,
            )
            remove_owned_network(
                self.settings.docker_binary,
                self._runtime_network_name(expected_resource_id),
                expected_resource_id,
                expected_manager_scope=self.settings.sandbox_manager_scope,
                timeout=30,
            )
        except DockerOwnershipError as exc:
            raise DomainError(
                "SANDBOX_RESOURCE_CONFLICT",
                "The Docker resource is owned by another sandbox",
                409,
                {
                    "resource_name": resource_name,
                    "expected_resource_id": expected_resource_id,
                },
            ) from exc
        except DockerControlError as exc:
            raise DomainError(
                "SANDBOX_BACKEND_UNAVAILABLE",
                "The Docker sandbox backend is unavailable",
                503,
            ) from exc

    def delete(self, resource: ManagedSandbox) -> None:
        self.delete_expected(resource.backend_resource_name, resource.id)

    def delete_orphan(self, observation: DockerObservation) -> None:
        if not observation.resource_id:
            raise DomainError(
                "SANDBOX_RESOURCE_UNOWNED",
                "A Docker resource without a sandbox resource ID cannot be deleted",
                409,
            )
        if observation.resource_type == "network":
            try:
                remove_owned_network(
                    self.settings.docker_binary,
                    observation.resource_name,
                    observation.resource_id,
                    expected_manager_scope=self.settings.sandbox_manager_scope,
                    timeout=30,
                )
            except DockerOwnershipError as exc:
                raise DomainError(
                    "SANDBOX_RESOURCE_CONFLICT",
                    "The Docker network is owned by another sandbox",
                    409,
                ) from exc
            except DockerControlError as exc:
                raise DomainError(
                    "SANDBOX_BACKEND_UNAVAILABLE",
                    "The Docker sandbox backend is unavailable",
                    503,
                ) from exc
            return
        # delete_expected performs the final inspect and atomically validates
        # both the resource ID and manager scope immediately before removal.
        self.delete_expected(observation.resource_name, observation.resource_id)

    def list_managed(self) -> list[DockerObservation]:
        if not self.control_enabled():
            return []
        if controller_is_remote(self.settings):
            try:
                raw = (
                    DockerControllerClient(self.settings)
                    .post("/v1/sandboxes/list", {}, timeout=30)
                    .get("observations")
                )
            except DockerControllerError as exc:
                raise DomainError(
                    "SANDBOX_BACKEND_UNAVAILABLE",
                    "The Docker sandbox controller is unavailable",
                    503,
                ) from exc
            if not isinstance(raw, list):
                raise DomainError(
                    "SANDBOX_DOCKER_PROTOCOL_ERROR", "Invalid controller list data", 502
                )
            observations: list[DockerObservation] = []
            for item in cast(list[object], raw):
                if isinstance(item, dict):
                    observations.append(
                        self._observation_from_remote(cast(dict[str, object], item))
                    )
            return observations
        identifiers = self._run(
            [
                self.settings.docker_binary,
                "ps",
                "--all",
                "--quiet",
                "--filter",
                "label=flowweave.managed=true",
                "--filter",
                f"label=flowweave.manager-scope={self.settings.sandbox_manager_scope}",
            ],
            timeout=30,
        ).splitlines()
        result: list[DockerObservation] = []
        for identifier in identifiers:
            observation = self.inspect(identifier.strip())
            if observation is not None:
                result.append(observation)
        network_identifiers = self._run(
            [
                self.settings.docker_binary,
                "network",
                "ls",
                "--quiet",
                "--filter",
                "label=flowweave.managed=true",
                "--filter",
                "label=flowweave.resource-type=network",
                "--filter",
                f"label=flowweave.manager-scope={self.settings.sandbox_manager_scope}",
            ],
            timeout=30,
        ).splitlines()
        for identifier in network_identifiers:
            observation = self._inspect_network_observation(identifier.strip())
            if observation is not None:
                result.append(observation)
        return result

    def _inspect_network_observation(self, identifier: str) -> DockerObservation | None:
        try:
            raw = self._run(
                [self.settings.docker_binary, "network", "inspect", identifier], timeout=30
            )
        except DomainError as exc:
            if self._absent(exc, "network"):
                return None
            raise
        try:
            value = cast(object, json.loads(raw))
            if not isinstance(value, list):
                raise ValueError("network inspect must contain one object")
            items = cast(list[object], value)
            if len(items) != 1 or not isinstance(items[0], dict):
                raise ValueError("network inspect must contain one object")
            data = cast(dict[str, object], items[0])
            labels_value = data.get("Labels")
            labels = (
                {
                    str(key): str(item)
                    for key, item in cast(dict[object, object], labels_value).items()
                }
                if isinstance(labels_value, dict)
                else {}
            )
        except (json.JSONDecodeError, ValueError) as exc:
            raise DomainError(
                "SANDBOX_DOCKER_PROTOCOL_ERROR",
                "Docker returned invalid managed network metadata",
                502,
            ) from exc
        return DockerObservation(
            resource_id=labels.get("flowweave.resource-id", ""),
            resource_name=str(data.get("Name") or identifier),
            resource_identifier=str(data.get("Id") or identifier),
            state="READY",
            labels=labels,
            resource_type="network",
        )

    def control_enabled(self) -> bool:
        return (
            self.settings.terminal_environment_backend == "docker"
            or self.settings.sandbox_backend == "docker"
            or self.settings.dependency_builder_backend == "docker"
        )

    @staticmethod
    def _observation_from_remote(raw: dict[str, object]) -> DockerObservation:
        labels_raw = raw.get("labels")
        labels = (
            {str(key): str(value) for key, value in cast(dict[object, object], labels_raw).items()}
            if isinstance(labels_raw, dict)
            else {}
        )
        required = ("resource_name", "resource_identifier", "state")
        if any(not str(raw.get(key) or "") for key in required):
            raise DomainError(
                "SANDBOX_DOCKER_PROTOCOL_ERROR", "Invalid controller observation", 502
            )
        return DockerObservation(
            resource_id=str(raw.get("resource_id") or ""),
            resource_name=str(raw["resource_name"]),
            resource_identifier=str(raw["resource_identifier"]),
            state=str(raw["state"]),
            labels=labels,
            resource_type=str(raw.get("resource_type") or "container"),
        )

    @staticmethod
    def orphan_is_stale(observation: DockerObservation, grace_seconds: int) -> bool:
        raw = observation.labels.get("flowweave.created-at", "")
        try:
            created_at = datetime.fromtimestamp(int(raw), tz=UTC)
        except (ValueError, OSError):
            return False
        return (datetime.now(UTC) - created_at).total_seconds() >= grace_seconds


def backend_name(
    resource_id: str,
    *,
    owner_type: str | None = None,
    owner_id: str | None = None,
) -> str:
    """Build a deterministic Docker name that remains easy to trace to its owner.

    The complete sandbox UUID is retained for uniqueness and ownership checks.
    Calls without owner information keep the legacy name so existing resources
    and older ledger rows remain fully compatible.
    """

    resource_key = resource_id.replace("-", "").lower()
    if not owner_type or not owner_id:
        return f"fw-sbx-{resource_key}"
    owner_kind = {
        "ATTEMPT": "auto",
        "CONVERSATION": "conv",
        "SETUP_SESSION": "setup",
    }.get(owner_type, "owner")
    owner_key = re.sub(r"[^a-z0-9]", "", owner_id.lower())[:8] or "unknown"
    return f"fw-sbx-{owner_kind}-{owner_key}-{resource_key}"
