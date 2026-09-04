from __future__ import annotations

import hashlib
import json
import re
import stat
import subprocess
import time
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath
from typing import cast

from flowweave.bootstrap.settings import Settings
from flowweave.modules.sandboxes.infrastructure.models import ManagedSandbox
from flowweave.runtime.auth import derive_runtime_session_key
from flowweave.shared.domain.openhands import (
    OPENHANDS_SOURCE_COMMIT,
    OPENHANDS_VERSION,
)
from flowweave.shared.errors import DomainError
from flowweave.shared.infrastructure.docker_control import (
    DockerControlError,
    DockerOwnershipError,
    docker_resource_is_absent,
    remove_owned_container,
    remove_owned_network,
    remove_owned_volume,
    run_docker_with_storage_quota_fallback,
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


@dataclass(frozen=True, slots=True)
class DockerDrainResult:
    """Physical fencing result for one old Agent Server generation."""

    graceful: bool
    stopped: bool


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
            completed = run_docker_with_storage_quota_fallback(
                command,
                timeout=timeout,
                runner=subprocess.run,
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
mkdir -p "$target/.openhands"
chmod 0700 "$target/.openhands"
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
                    "The Docker Runtime Provider is unavailable",
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
            expected_digest = str(spec.get("base_image_digest") or "")
            base_reference = str(spec.get("base_image_reference") or "")
            if (
                not base_reference
                or not self._is_image_digest(expected_digest)
                or resource.image_reference != expected_digest
            ):
                raise DomainError(
                    "SANDBOX_IMAGE_UNTRUSTED",
                    "The setup sandbox requires a digest-locked user base image",
                    422,
                )
            actual_digest, _labels = self._inspect_image(resource.image_reference)
            if actual_digest != expected_digest:
                raise DomainError(
                    "SANDBOX_IMAGE_UNTRUSTED",
                    "The user base image content digest drifted",
                    409,
                    {"base_image_reference": base_reference},
                )
            return actual_digest

        expected_digest = resource.image_reference
        if not self._is_image_digest(expected_digest):
            raise DomainError(
                "SANDBOX_IMAGE_UNTRUSTED",
                "The sandbox requires an immutable managed environment image",
                422,
            )
        actual_digest, labels = self._inspect_image(expected_digest)

        if resource.kind == "AGENT_RUNTIME" and resource.owner_type == "AGENT_WORKSPACE":
            if actual_digest != expected_digest:
                raise DomainError(
                    "SANDBOX_IMAGE_UNTRUSTED",
                    "The default Agent Workspace Runtime image content digest drifted",
                    409,
                    {"image_reference": expected_digest},
                )
            return actual_digest

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

    def ensure_running(
        self, resource: ManagedSandbox, *, runtime_secret_key: str | None = None
    ) -> DockerObservation:
        self.require_enabled()
        if controller_is_remote(self.settings):
            payload: dict[str, object] = {
                "id": resource.id,
                "kind": resource.kind,
                "owner_type": resource.owner_type,
                "owner_id": resource.owner_id,
                "backend_resource_name": resource.backend_resource_name,
                "image_reference": resource.image_reference,
                "spec": resource.spec_json or {},
                "created_at": resource.created_at.isoformat(),
            }
            if resource.kind == "AGENT_RUNTIME":
                payload["runtime_secret_key"] = runtime_secret_key
            try:
                raw = DockerControllerClient(self.settings).post(
                    "/v1/sandboxes/ensure",
                    payload,
                    timeout=self.settings.terminal_environment_start_timeout_seconds + 30,
                )
            except DockerControllerError as exc:
                raise DomainError(
                    "SANDBOX_BACKEND_UNAVAILABLE",
                    "The Docker Runtime Provider is unavailable",
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
        existing = self.inspect(resource.backend_resource_name)
        if existing is not None:
            # A running container retains its image layer even if a later
            # deployment prunes the historical image digest locally. Its
            # immutable labels remain the identity proof for an existing
            # resource, so do not require that old image merely to observe,
            # reattach, or probe an already-owned container.
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

        # No owned container exists. Verify the immutable image identity
        # before any new container can be created from it.
        verified_image_reference = self._verify_image_trust(resource)
        self._ensure_runtime_network(resource)
        runtime_home_id = str(
            (resource.spec_json or {}).get("agent_workspace_id")
            or (resource.spec_json or {}).get("environment_id")
            or ""
        )
        if not runtime_home_id:
            raise DomainError(
                "SANDBOX_SPEC_INVALID",
                "The sandbox Runtime home identity is missing",
                422,
            )
        home_volume = self._ensure_environment_credential_volume(runtime_home_id)
        self._prepare_environment_home(
            home_volume, verified_image_reference=verified_image_reference
        )
        command = self._create_command(
            resource,
            verified_image_reference=verified_image_reference,
            runtime_secret_key=runtime_secret_key,
        )
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
        clients: list[str] = []
        for role in ("api", "worker"):
            identifiers = self._run(
                [
                    self.settings.docker_binary,
                    "ps",
                    "--quiet",
                    "--filter",
                    "label=flowweave.runtime-client=true",
                    "--filter",
                    f"label=flowweave.runtime-client-role={role}",
                    "--filter",
                    f"label=flowweave.manager-scope={self.settings.sandbox_manager_scope}",
                ],
                timeout=30,
            ).splitlines()
            clients.extend(item.strip() for item in identifiers if item.strip())
        clients = list(dict.fromkeys(clients))
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
        self,
        resource: ManagedSandbox,
        *,
        verified_image_reference: str,
        runtime_secret_key: str | None = None,
    ) -> list[str]:
        if not self._is_image_digest(verified_image_reference):
            raise DomainError(
                "SANDBOX_IMAGE_UNTRUSTED",
                "The Docker sandbox launch image must be immutable",
                422,
            )
        command = self._common_run_command(resource)
        environment_id = str((resource.spec_json or {}).get("environment_id") or "")
        runtime_home_id = str(
            (resource.spec_json or {}).get("agent_workspace_id") or environment_id
        )
        if not runtime_home_id:
            raise DomainError(
                "SANDBOX_SPEC_INVALID",
                "The sandbox Runtime home identity is missing",
                422,
            )
        credential_volume = self._ensure_environment_credential_volume(runtime_home_id)
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
                    "-e",
                    "NPM_CONFIG_PREFIX=/root/.local",
                    verified_image_reference,
                    "sh",
                    "-c",
                    "trap : TERM INT; while :; do sleep 3600; done",
                ]
            )
            return command
        workspace_mounts = self._runtime_workspace_mount(resource)
        session_key = derive_runtime_session_key(
            self.settings.openhands_session_api_key,
            self.settings.sandbox_manager_scope,
            resource.backend_resource_name,
        )
        persistent_runtime = resource.owner_type in {"FLOW_RUN", "AGENT_WORKSPACE"}
        runtime_tmpfs = [
            "--tmpfs",
            "/tmp:rw,nosuid,nodev,size=128m,uid=10001,gid=10001,mode=1777",
        ]
        runtime_environment: list[str] = []
        if persistent_runtime:
            if runtime_secret_key is None or len(runtime_secret_key) < 32:
                raise DomainError(
                    "RUNTIME_SECRET_REFERENCE_INVALID",
                    "The persistent Runtime Secret Reference is unavailable",
                    409,
                )
            runtime_environment = [
                "-e",
                "OH_WORKSPACE_PATH=/runtime/workspace/project",
                "-e",
                "OH_CONVERSATIONS_PATH=/runtime/state/conversations",
                "-e",
                "OH_BASH_EVENTS_DIR=/runtime/state/bash-events",
                "-e",
                "OH_PERSISTENCE_DIR=/runtime/state/persistence",
                "-e",
                f"OH_SECRET_KEY={runtime_secret_key}",
            ]
        else:
            relative = PurePosixPath(
                str((resource.spec_json or {}).get("workspace_relative") or "")
            )
            runtime_tmpfs.extend(
                [
                    "--tmpfs",
                    "/runtime/ephemeral-state:rw,nosuid,nodev,size=64m,uid=10001,gid=10001,mode=0700",
                ]
            )
            runtime_environment = [
                "-e",
                (
                    "OH_WORKSPACE_PATH="
                    f"{PurePosixPath(str(self.settings.openhands_workspace_root)).joinpath(*relative.parts)}"
                ),
                "-e",
                "OH_CONVERSATIONS_PATH=/runtime/ephemeral-state/conversations",
                "-e",
                "OH_BASH_EVENTS_DIR=/runtime/ephemeral-state/bash-events",
                "-e",
                "OH_PERSISTENCE_DIR=/runtime/ephemeral-state/persistence",
                "-e",
                f"OH_SECRET_KEY={session_key}",
            ]
        command.extend(
            [
                "--network",
                self._runtime_network_name(resource.id),
                # Runtime images contain a full developer toolchain and execute
                # model-generated commands. Keep that workload away from root
                # and make the image itself immutable. FlowRun project/state
                # paths are external bind mounts; transient validation Runtime
                # data remains bounded tmpfs. Executable capability assets use
                # a separate read-only mount. UID 10001 matches the platform
                # workspace owner used by the standard deployment.
                "--user",
                "10001:10001",
                "--read-only",
                *runtime_tmpfs,
                "--mount",
                (f"type=volume,src={credential_volume},dst=/home/flowweave"),
                *workspace_mounts,
                "-e",
                "HOME=/home/flowweave",
                "-e",
                "NPM_CONFIG_PREFIX=/home/flowweave/.local",
                "-e",
                "OPENHANDS_SUPPRESS_BANNER=1",
                "-e",
                f"SESSION_API_KEY={session_key}",
                "-e",
                f"OH_SESSION_API_KEYS_0={session_key}",
                *runtime_environment,
                verified_image_reference,
                "sh",
                "-c",
                'export PATH="$NPM_CONFIG_PREFIX/bin:$PATH"; exec agent-server "$@"',
                "--",
                "--host",
                "0.0.0.0",
                "--port",
                "8000",
            ]
        )
        return command

    def _runtime_workspace_mount(self, resource: ManagedSandbox) -> list[str]:
        if (resource.spec_json or {}).get("runtime_allocation_relative"):
            return self._flow_run_runtime_mounts(resource)
        if resource.owner_type not in {
            "CAPABILITY_VALIDATION",
            "MCP_OAUTH_AUTHORIZATION",
        }:
            raise DomainError(
                "RUNTIME_PROVIDER_OWNER_INVALID",
                "Only persistent or explicit temporary owners may start an Agent Runtime",
                422,
                {"owner_type": resource.owner_type, "owner_id": resource.owner_id},
            )
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
        managed_runtime_root = PurePosixPath(str(self.settings.openhands_managed_assets_root))
        if (
            not runtime_root.is_absolute()
            or not managed_runtime_root.is_absolute()
            or managed_runtime_root == runtime_root
            or managed_runtime_root.is_relative_to(runtime_root)
            or runtime_root.is_relative_to(managed_runtime_root)
            or any(character in str(managed_runtime_root) for character in ",=")
        ):
            raise DomainError(
                "SANDBOX_WORKSPACE_INVALID",
                "The Runtime workspace and managed asset roots must be isolated absolute paths",
                503,
            )
        source, validation_source = self._workspace_source_roots()
        target = runtime_root.joinpath(*relative.parts)
        managed_relative = PurePosixPath(".managed-assets").joinpath(*relative.parts)
        managed_target = managed_runtime_root.joinpath(*relative.parts)
        for candidate in (
            validation_source.joinpath(*relative.parts),
            validation_source.joinpath(*managed_relative.parts),
        ):
            try:
                metadata = candidate.lstat()
            except OSError as exc:
                raise DomainError(
                    "SANDBOX_WORKSPACE_SOURCE_INVALID",
                    "The temporary Runtime workspace is unavailable",
                    409,
                ) from exc
            if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISDIR(metadata.st_mode):
                raise DomainError(
                    "SANDBOX_WORKSPACE_SOURCE_INVALID",
                    "The temporary Runtime workspace failed integrity validation",
                    409,
                )
        specifications = [
            f"type=bind,src={source.joinpath(*relative.parts)},dst={target}",
            (
                f"type=bind,src={source.joinpath(*managed_relative.parts)},"
                f"dst={managed_target},readonly"
            ),
        ]
        return [item for specification in specifications for item in ("--mount", specification)]

    def _workspace_source_roots(self) -> tuple[Path, Path]:
        source = self.settings.runtime_host_workspace_root
        validation_source = self.settings.flow_run_runtime_validation_root
        if not source.is_absolute() or any(character in str(source) for character in ",="):
            raise DomainError(
                "SANDBOX_WORKSPACE_SOURCE_INVALID",
                "The Runtime host workspace root must be an unambiguous absolute path",
                503,
            )
        if not validation_source.is_absolute():
            validation_source = source
        return source, validation_source

    def _flow_run_runtime_mounts(self, resource: ManagedSandbox) -> list[str]:
        spec = resource.spec_json or {}
        relative_raw = str(spec.get("runtime_allocation_relative") or "")
        flow_run_id = str(spec.get("flow_run_id") or "")
        node_attempt_id = str(spec.get("node_attempt_id") or "")
        agent_workspace_id = str(spec.get("agent_workspace_id") or "")
        runtime_allocation_id = str(
            spec.get("runtime_allocation_id") or spec.get("agent_workspace_allocation_id") or ""
        )
        relative = PurePosixPath(relative_raw)
        is_flow_run = (
            resource.owner_type == "FLOW_RUN"
            and resource.owner_id == flow_run_id
            and len(relative.parts) == 3
            and relative.parts[0] == ".flow-run-runtimes"
            and relative.parts[-1] == flow_run_id
        )
        is_node_attempt = (
            resource.owner_type == "FLOW_NODE_ATTEMPT"
            and resource.owner_id == node_attempt_id
            and bool(flow_run_id)
            and len(relative.parts) == 3
            and relative.parts[0] == ".flow-run-runtimes"
            and relative.parts[-1] == node_attempt_id
        )
        is_agent_workspace = (
            resource.owner_type == "AGENT_WORKSPACE"
            and resource.owner_id == agent_workspace_id
            and relative == PurePosixPath(".agent-workspaces/platform-default")
        )
        if (
            not (is_flow_run or is_node_attempt or is_agent_workspace)
            or relative.is_absolute()
            or not re.fullmatch(r"[0-9a-f-]{36}", runtime_allocation_id)
            or any(part in {"", ".", ".."} for part in relative.parts)
        ):
            raise DomainError(
                "SANDBOX_WORKSPACE_INVALID",
                "The persistent Runtime allocation path is invalid",
                422,
            )
        source_root, validation_root = self._workspace_source_roots()
        allocation_root = source_root.joinpath(*relative.parts)
        validation_allocation_root = validation_root.joinpath(*relative.parts)
        paths = {
            "workspace/project": 0o700,
            "workspace/nodes": 0o700,
            "state/conversations": 0o700,
            "state/bash-events": 0o700,
            "state/persistence": 0o700,
            "state/persistence/profiles": 0o700,
            # The control-plane root stays owner-writable so immutable digest
            # bundles can be published after a FlowRun Runtime starts. Runtime
            # access is read-only at the bind-mount boundary.
            "capabilities": 0o700,
        }
        try:
            root_metadata = validation_allocation_root.lstat()
            marker = validation_allocation_root / ".flowweave-allocation"
            marker_metadata = marker.lstat()
            if (
                stat.S_ISLNK(root_metadata.st_mode)
                or not stat.S_ISDIR(root_metadata.st_mode)
                or stat.S_IMODE(root_metadata.st_mode) != 0o700
                or not stat.S_ISREG(marker_metadata.st_mode)
                or stat.S_IMODE(marker_metadata.st_mode) != 0o400
                or marker_metadata.st_uid != root_metadata.st_uid
                or marker_metadata.st_gid != root_metadata.st_gid
                or marker.read_text(encoding="ascii") != runtime_allocation_id
            ):
                raise ValueError("invalid allocation root")
            for path, mode in paths.items():
                metadata = (validation_allocation_root / path).lstat()
                if (
                    stat.S_ISLNK(metadata.st_mode)
                    or not stat.S_ISDIR(metadata.st_mode)
                    or stat.S_IMODE(metadata.st_mode) != mode
                    or metadata.st_uid != root_metadata.st_uid
                    or metadata.st_gid != root_metadata.st_gid
                ):
                    raise ValueError(f"invalid allocation path: {path}")
        except (FileNotFoundError, OSError, ValueError) as exc:
            raise DomainError(
                "SANDBOX_WORKSPACE_SOURCE_INVALID",
                "The persistent Runtime host directories failed integrity validation",
                409,
            ) from exc
        specifications = [
            (
                f"type=bind,src={allocation_root / 'workspace/project'},"
                "dst=/runtime/workspace/project"
            ),
            (f"type=bind,src={allocation_root / 'workspace/nodes'},dst=/runtime/workspace/nodes"),
            (
                f"type=bind,src={allocation_root / 'state/conversations'},"
                "dst=/runtime/state/conversations"
            ),
            (
                f"type=bind,src={allocation_root / 'state/bash-events'},"
                "dst=/runtime/state/bash-events"
            ),
            (
                f"type=bind,src={allocation_root / 'state/persistence'},"
                "dst=/runtime/state/persistence"
            ),
            # OpenHands' Profile API honors OH_PERSISTENCE_DIR while SDK
            # conversations resolve auxiliary profiles from
            # $HOME/.openhands/profiles. Map only that child store: mounting
            # the whole .openhands directory would hide credentials prepared
            # in the Environment HOME volume.
            (
                f"type=bind,src={allocation_root / 'state/persistence/profiles'},"
                "dst=/home/flowweave/.openhands/profiles"
            ),
            (
                f"type=bind,src={allocation_root / 'capabilities'},"
                "dst=/runtime/capabilities,readonly"
            ),
        ]
        return [item for value in specifications for item in ("--mount", value)]

    def _wait_for_agent_server(self, resource_name: str) -> None:
        deadline = time.monotonic() + self.settings.terminal_environment_start_timeout_seconds
        session_key = derive_runtime_session_key(
            self.settings.openhands_session_api_key,
            self.settings.sandbox_manager_scope,
            resource_name,
        )
        while time.monotonic() < deadline:
            try:
                # The controller deliberately does not join the untrusted Runtime
                # network. Probe readiness and immutable server capabilities from
                # inside the owned container instead of making the Docker-socket
                # holder reachable from sandboxes. This does not hydrate a
                # Conversation, so N+1 remains a non-writer during prewarm.
                self._run(
                    [
                        self.settings.docker_binary,
                        "exec",
                        resource_name,
                        "/runtime/.venv/bin/python",
                        "-c",
                        (
                            "import json,sys,urllib.request;"
                            "headers={'X-Session-API-Key':sys.argv[1]};"
                            "get=lambda path:json.load(urllib.request.urlopen("
                            "urllib.request.Request('http://127.0.0.1:8000'+path,"
                            "headers=headers),timeout=2));"
                            "ready=get('/ready');info=get('/server_info');"
                            "assert ready.get('status')=='ready';"
                            "assert [info.get(key) for key in ('version','sdk_version',"
                            "'tools_version','workspace_version')]==[sys.argv[2]]*4;"
                            "assert info.get('build_git_sha')==sys.argv[3];"
                            "assert info.get('build_git_ref')==sys.argv[3];"
                            "assert isinstance(info.get('capabilities'),list);"
                            "assert isinstance(info.get('usable_tools'),list)"
                        ),
                        session_key,
                        OPENHANDS_VERSION,
                        OPENHANDS_SOURCE_COMMIT,
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
                    "The Docker Runtime Provider is unavailable",
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

    def drain_expected(self, resource_name: str, expected_resource_id: str) -> DockerDrainResult:
        """Disconnect writers, invoke OpenHands pause, then stop the old container."""

        if controller_is_remote(self.settings):
            try:
                raw = DockerControllerClient(self.settings).post(
                    "/v1/sandboxes/drain",
                    {
                        "resource_name": resource_name,
                        "resource_id": expected_resource_id,
                    },
                    timeout=60,
                )
            except DockerControllerError as exc:
                raise DomainError(
                    "SANDBOX_BACKEND_UNAVAILABLE",
                    "The Docker Runtime Provider is unavailable",
                    503,
                ) from exc
            graceful = raw.get("graceful")
            stopped = raw.get("stopped")
            if not isinstance(graceful, bool) or not isinstance(stopped, bool):
                raise DomainError(
                    "SANDBOX_DOCKER_PROTOCOL_ERROR",
                    "Invalid controller drain data",
                    502,
                )
            return DockerDrainResult(graceful=graceful, stopped=stopped)

        observation = self.inspect(resource_name)
        if observation is None:
            return DockerDrainResult(graceful=False, stopped=True)
        self._verify_owner(
            observation,
            expected_resource_id,
            self.settings.sandbox_manager_scope,
        )
        if observation.labels.get("flowweave.kind") != "agent-runtime":
            raise DomainError(
                "SANDBOX_RESOURCE_CONFLICT",
                "Only an owned Agent Runtime can be drained",
                409,
                {"resource_name": resource_name},
            )

        # Remove every data-plane attachment before asking the process to
        # release its OpenHands leases. docker exec remains available through
        # the daemon, so no late API/Worker request can reopen a Conversation
        # in the interval between prepare-for-sandbox-pause and stop.
        raw_networks = self._run(
            [
                self.settings.docker_binary,
                "inspect",
                observation.resource_identifier,
                "--format",
                "{{json .NetworkSettings.Networks}}",
            ],
            timeout=30,
        )
        try:
            value = cast(object, json.loads(raw_networks))
            networks = (
                [str(name) for name in cast(dict[object, object], value)]
                if isinstance(value, dict)
                else []
            )
        except json.JSONDecodeError as exc:
            raise DomainError(
                "SANDBOX_DOCKER_PROTOCOL_ERROR",
                "Docker returned invalid Runtime attachment metadata",
                502,
            ) from exc
        for network_name in networks:
            self._run(
                [
                    self.settings.docker_binary,
                    "network",
                    "disconnect",
                    "--force",
                    network_name,
                    observation.resource_identifier,
                ],
                timeout=30,
            )

        session_key = derive_runtime_session_key(
            self.settings.openhands_session_api_key,
            self.settings.sandbox_manager_scope,
            resource_name,
        )
        graceful = True
        try:
            self._run(
                [
                    self.settings.docker_binary,
                    "exec",
                    observation.resource_identifier,
                    "/runtime/.venv/bin/python",
                    "-c",
                    (
                        "import sys,urllib.request;"
                        "request=urllib.request.Request("
                        "'http://127.0.0.1:8000/api/conversations/"
                        "prepare-for-sandbox-pause',method='POST',"
                        "headers={'X-Session-API-Key':sys.argv[1]});"
                        "response=urllib.request.urlopen(request,timeout=30);"
                        "assert response.status==204"
                    ),
                    session_key,
                ],
                timeout=35,
            )
        except DomainError:
            graceful = False
        try:
            self._run(
                [
                    self.settings.docker_binary,
                    "stop",
                    "--time",
                    "30",
                    observation.resource_identifier,
                ],
                timeout=40,
            )
        except DomainError as exc:
            if self.inspect(resource_name) is not None:
                raise exc
        return DockerDrainResult(graceful=graceful, stopped=True)

    def drain(self, resource: ManagedSandbox) -> DockerDrainResult:
        return self.drain_expected(resource.backend_resource_name, resource.id)

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
                    "The Docker Runtime Provider is unavailable",
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
                    "The Docker Runtime Provider is unavailable",
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
        "FLOW_RUN": "run",
        "AGENT_WORKSPACE": "agent",
        "SETUP_SESSION": "setup",
        "CAPABILITY_VALIDATION": "probe",
        "MCP_OAUTH_AUTHORIZATION": "oauth",
    }.get(owner_type, "owner")
    owner_key = re.sub(r"[^a-z0-9]", "", owner_id.lower())[:8] or "unknown"
    return f"fw-sbx-{owner_kind}-{owner_key}-{resource_key}"
