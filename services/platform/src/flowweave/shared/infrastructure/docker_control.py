from __future__ import annotations

import json
import os
import re
import subprocess
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import cast
from uuid import uuid4


class DockerControlError(RuntimeError):
    """A Docker control operation failed without weakening ownership checks."""


class DockerOwnershipError(DockerControlError):
    """The selected container is not owned by the expected FlowWeave resource."""


def docker_resource_is_absent(detail: str, resource_type: str) -> bool:
    """Recognize Docker's type-specific missing-resource responses.

    Docker versions do not use one stable phrase: container inspection may
    report ``No such object`` while network inspection commonly reports
    ``network <name> not found``. Keep the match type-scoped so an unrelated
    missing object is never treated as idempotent success.
    """

    normalized = detail.lower()
    if f"no such {resource_type}" in normalized or f"{resource_type} not found" in normalized:
        return True
    if resource_type == "container" and "no such object" in normalized:
        return True
    return (
        re.search(
            rf"(?:^|[\s:]){re.escape(resource_type)}\s+[^\r\n]+?\s+not found(?:$|[\r\n])",
            normalized,
        )
        is not None
    )


@dataclass(frozen=True, slots=True)
class EphemeralDockerLease:
    resource_id: str
    resource_name: str
    kind: str
    owner_type: str
    owner_id: str
    manager_scope: str
    created_at: datetime
    expires_at: datetime

    @classmethod
    def create(
        cls,
        *,
        kind: str,
        owner_type: str,
        manager_scope: str,
        ttl_seconds: int,
    ) -> EphemeralDockerLease:
        resource_id = str(uuid4())
        now = datetime.now(UTC)
        safe_kind = "".join(character for character in kind.lower() if character.isalnum())[:20]
        return cls(
            resource_id=resource_id,
            resource_name=f"fw-ep-{safe_kind}-{resource_id.replace('-', '')}",
            kind=kind,
            owner_type=owner_type,
            owner_id=resource_id,
            manager_scope=manager_scope,
            created_at=now,
            expires_at=now + timedelta(seconds=ttl_seconds),
        )

    def label_args(self) -> list[str]:
        labels = {
            "flowweave.managed": "true",
            "flowweave.manager-scope": self.manager_scope,
            "flowweave.resource-id": self.resource_id,
            "flowweave.kind": self.kind,
            "flowweave.owner-type": self.owner_type,
            "flowweave.owner-id": self.owner_id,
            "flowweave.lifecycle": "ephemeral",
            "flowweave.created-at": str(int(self.created_at.timestamp())),
            "flowweave.expires-at": str(int(self.expires_at.timestamp())),
        }
        return [part for key, value in labels.items() for part in ("--label", f"{key}={value}")]

    def network_name(self) -> str:
        """Return the deterministic auxiliary network owned by this lease."""

        return f"fw-net-{self.resource_id.replace('-', '')}"

    def network_label_args(self, *, purpose: str, mode: str) -> list[str]:
        return [
            *self.label_args(),
            "--label",
            "flowweave.resource-type=network",
            "--label",
            f"flowweave.network-purpose={purpose}",
            "--label",
            f"flowweave.network-mode={mode}",
        ]


def ephemeral_lease_is_expired(labels: dict[str, str], *, now: datetime | None = None) -> bool:
    if labels.get("flowweave.lifecycle") != "ephemeral":
        return False
    try:
        expires_at = datetime.fromtimestamp(int(labels["flowweave.expires-at"]), tz=UTC)
    except (KeyError, ValueError, OSError):
        return False
    return expires_at <= (now or datetime.now(UTC))


def _run(command: list[str], *, timeout: int) -> subprocess.CompletedProcess[str]:
    try:
        return subprocess.run(
            command,
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
            env={"PATH": os.defpath},
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise DockerControlError("Docker ownership verification is unavailable") from exc


def inspect_owned_container(
    docker_binary: str,
    resource_name: str,
    expected_resource_id: str,
    *,
    expected_manager_scope: str,
    timeout: int = 10,
) -> str | None:
    """Return the immutable ID only when all ownership labels still match."""

    inspected = _run(
        [docker_binary, "inspect", resource_name, "--format", "{{json .}}"],
        timeout=timeout,
    )
    if inspected.returncode:
        detail = (inspected.stderr or inspected.stdout).lower()
        if docker_resource_is_absent(detail, "container"):
            return None
        raise DockerControlError("Docker inspect failed during ownership verification")
    try:
        raw = cast(object, json.loads(inspected.stdout))
        if not isinstance(raw, dict):
            raise ValueError("inspect response must be an object")
        data = cast(dict[str, object], raw)
        config_value = data.get("Config")
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
    except (json.JSONDecodeError, ValueError) as exc:
        raise DockerControlError("Docker inspect returned invalid ownership data") from exc
    actual_resource_id = labels.get("flowweave.resource-id", "")
    actual_manager_scope = labels.get("flowweave.manager-scope", "")
    if (
        labels.get("flowweave.managed") != "true"
        or actual_resource_id != expected_resource_id
        or actual_manager_scope != expected_manager_scope
    ):
        raise DockerOwnershipError(
            f"Container ownership mismatch for {resource_name}: "
            f"expected {expected_manager_scope}/{expected_resource_id}, got "
            f"{actual_manager_scope or 'unscoped'}/{actual_resource_id or 'unmanaged'}"
        )
    identifier = str(data.get("Id") or "")
    if not identifier:
        raise DockerControlError("Docker inspect omitted the container identifier")
    return identifier


def remove_owned_container(
    docker_binary: str,
    resource_name: str,
    expected_resource_id: str,
    *,
    expected_manager_scope: str,
    timeout: int = 10,
) -> bool:
    """Remove a container only after re-reading immutable ownership labels."""

    identifier = inspect_owned_container(
        docker_binary,
        resource_name,
        expected_resource_id,
        expected_manager_scope=expected_manager_scope,
        timeout=timeout,
    )
    if identifier is None:
        return False
    removed = _run([docker_binary, "rm", "--force", identifier], timeout=timeout)
    if removed.returncode:
        detail = (removed.stderr or removed.stdout).lower()
        if docker_resource_is_absent(detail, "container"):
            return False
        raise DockerControlError("Docker remove failed after ownership verification")
    return True


def remove_owned_network(
    docker_binary: str,
    resource_name: str,
    expected_resource_id: str,
    *,
    expected_manager_scope: str,
    timeout: int = 10,
) -> bool:
    """Remove one managed network after re-reading its immutable ownership labels."""

    inspected = _run(
        [docker_binary, "network", "inspect", resource_name],
        timeout=timeout,
    )
    if inspected.returncode:
        detail = (inspected.stderr or inspected.stdout).lower()
        if docker_resource_is_absent(detail, "network"):
            return False
        raise DockerControlError("Docker network inspect failed during ownership verification")
    try:
        raw = cast(object, json.loads(inspected.stdout))
        if not isinstance(raw, list):
            raise ValueError("network inspect response must contain one object")
        items = cast(list[object], raw)
        if len(items) != 1 or not isinstance(items[0], dict):
            raise ValueError("network inspect response must contain one object")
        data = cast(dict[str, object], items[0])
        labels_value = data.get("Labels")
        labels = (
            {
                str(key): str(value)
                for key, value in cast(dict[object, object], labels_value).items()
            }
            if isinstance(labels_value, dict)
            else {}
        )
        containers_value = data.get("Containers")
        containers = (
            list(cast(dict[object, object], containers_value))
            if isinstance(containers_value, dict)
            else []
        )
        identifier = str(data.get("Id") or "")
    except (json.JSONDecodeError, ValueError) as exc:
        raise DockerControlError("Docker network inspect returned invalid ownership data") from exc
    if (
        labels.get("flowweave.managed") != "true"
        or labels.get("flowweave.resource-type") != "network"
        or labels.get("flowweave.resource-id") != expected_resource_id
        or labels.get("flowweave.manager-scope") != expected_manager_scope
    ):
        raise DockerOwnershipError(
            f"Network ownership mismatch for {resource_name}: expected "
            f"{expected_manager_scope}/{expected_resource_id}"
        )
    if not identifier:
        raise DockerControlError("Docker network inspect omitted the network identifier")

    # A Runtime network contains only the owned Runtime and explicitly trusted
    # control-plane clients. The Runtime container is removed first; disconnect
    # any remaining clients so the owned network itself can be reclaimed.
    for container_id in containers:
        disconnected = _run(
            [docker_binary, "network", "disconnect", "--force", identifier, str(container_id)],
            timeout=timeout,
        )
        if disconnected.returncode:
            detail = (disconnected.stderr or disconnected.stdout).lower()
            if "not connected" not in detail and "no such container" not in detail:
                raise DockerControlError("Docker network disconnect failed during cleanup")
    removed = _run([docker_binary, "network", "rm", identifier], timeout=timeout)
    if removed.returncode:
        detail = (removed.stderr or removed.stdout).lower()
        if docker_resource_is_absent(detail, "network"):
            return False
        raise DockerControlError("Docker network remove failed after ownership verification")
    return True


def remove_owned_volume(
    docker_binary: str,
    resource_name: str,
    *,
    expected_environment_id: str,
    expected_manager_scope: str,
    timeout: int = 10,
) -> bool:
    """Remove one environment credential volume after immutable label checks."""

    inspected = _run(
        [docker_binary, "volume", "inspect", resource_name],
        timeout=timeout,
    )
    if inspected.returncode:
        detail = (inspected.stderr or inspected.stdout).lower()
        if docker_resource_is_absent(detail, "volume"):
            return False
        raise DockerControlError("Docker volume inspect failed during ownership verification")
    try:
        raw = cast(object, json.loads(inspected.stdout))
        if not isinstance(raw, list):
            raise ValueError("volume inspect response must contain one object")
        items = cast(list[object], raw)
        if len(items) != 1 or not isinstance(items[0], dict):
            raise ValueError("volume inspect response must contain one object")
        data = cast(dict[str, object], items[0])
        labels_value = data.get("Labels")
        labels = (
            {
                str(key): str(value)
                for key, value in cast(dict[object, object], labels_value).items()
            }
            if isinstance(labels_value, dict)
            else {}
        )
        actual_name = str(data.get("Name") or "")
    except (json.JSONDecodeError, ValueError) as exc:
        raise DockerControlError("Docker volume inspect returned invalid ownership data") from exc
    if (
        actual_name != resource_name
        or labels.get("flowweave.managed") != "true"
        or labels.get("flowweave.resource-type") != "environment-credential-volume"
        or labels.get("flowweave.environment-id") != expected_environment_id
        or labels.get("flowweave.manager-scope") != expected_manager_scope
    ):
        raise DockerOwnershipError(
            f"Volume ownership mismatch for {resource_name}: expected "
            f"{expected_manager_scope}/{expected_environment_id}"
        )
    removed = _run([docker_binary, "volume", "rm", resource_name], timeout=timeout)
    if removed.returncode:
        detail = (removed.stderr or removed.stdout).lower()
        if docker_resource_is_absent(detail, "volume"):
            return False
        raise DockerControlError("Docker volume remove failed after ownership verification")
    return True
