from __future__ import annotations

import base64
import json
import os
import subprocess
from typing import Any, cast

from flowweave.bootstrap.settings import Settings
from flowweave.shared.application.dependency_builder import (
    DependencyBuilderPort,
    DependencyBundle,
)
from flowweave.shared.infrastructure.docker_control import (
    DockerControlError,
    EphemeralDockerLease,
    remove_owned_container,
    remove_owned_network,
    run_docker_with_storage_quota_fallback,
)
from flowweave.shared.infrastructure.docker_controller import (
    DockerControllerClient,
    DockerControllerError,
    controller_is_remote,
)

_MAX_BUNDLE_BYTES = 100 * 1024 * 1024


class DisabledDependencyBuilder:
    def build(self, dependencies: dict[str, dict[str, str]]) -> DependencyBundle:
        raise RuntimeError("Dependency builder is disabled")


class DockerDependencyBuilder:
    """Runs a fixed builder entrypoint without passing user-controlled shell text."""

    def __init__(
        self,
        image: str,
        *,
        docker_binary: str = "docker",
        manager_scope: str,
        timeout_seconds: int = 300,
        cleanup_grace_seconds: int = 300,
        storage_size: str = "4g",
    ) -> None:
        self.image = image
        self.docker_binary = docker_binary
        self.manager_scope = manager_scope
        self.timeout_seconds = timeout_seconds
        self.cleanup_grace_seconds = cleanup_grace_seconds
        self.storage_size = storage_size

    def command(self, lease: EphemeralDockerLease) -> list[str]:
        return [
            self.docker_binary,
            "run",
            "--rm",
            "--interactive",
            "--name",
            lease.resource_name,
            *lease.label_args(),
            "--log-driver",
            "local",
            "--log-opt",
            "max-size=4m",
            "--log-opt",
            "max-file=2",
            "--storage-opt",
            f"size={self.storage_size}",
            "--network",
            lease.network_name(),
            "--read-only",
            "--cap-drop",
            "ALL",
            "--security-opt",
            "no-new-privileges",
            "--pids-limit",
            "128",
            "--memory",
            "512m",
            "--cpus",
            "1",
            "--user",
            "65534:65534",
            "--tmpfs",
            "/tmp:rw,noexec,nosuid,size=128m",
            "--tmpfs",
            "/work:rw,nosuid,size=256m",
            self.image,
        ]

    def _create_network(self, lease: EphemeralDockerLease) -> None:
        command = [
            self.docker_binary,
            "network",
            "create",
            "--driver",
            "bridge",
            *lease.network_label_args(purpose="dependency-build", mode="egress"),
            lease.network_name(),
        ]
        try:
            completed = subprocess.run(
                command,
                capture_output=True,
                text=True,
                timeout=30,
                check=False,
                env={"PATH": os.defpath},
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise RuntimeError("Dependency build network is unavailable") from exc
        if completed.returncode:
            raise RuntimeError(
                f"Dependency build network failed: {(completed.stderr or completed.stdout)[:2000]}"
            )

    def _cleanup(self, lease: EphemeralDockerLease) -> None:
        try:
            remove_owned_container(
                self.docker_binary,
                lease.resource_name,
                lease.resource_id,
                expected_manager_scope=lease.manager_scope,
                timeout=5,
            )
        except DockerControlError:
            pass
        try:
            remove_owned_network(
                self.docker_binary,
                lease.network_name(),
                lease.resource_id,
                expected_manager_scope=lease.manager_scope,
                timeout=5,
            )
        except DockerControlError:
            # Absolute expiry labels let the reconciler safely retry cleanup.
            pass

    def build(self, dependencies: dict[str, dict[str, str]]) -> DependencyBundle:
        lease = EphemeralDockerLease.create(
            kind="dependency-build",
            owner_type="DEPENDENCY_BUILD",
            manager_scope=self.manager_scope,
            ttl_seconds=self.timeout_seconds + self.cleanup_grace_seconds,
        )
        payload = json.dumps(
            {"schema_version": 1, "dependencies": dependencies},
            ensure_ascii=True,
            separators=(",", ":"),
        )
        try:
            self._create_network(lease)
            completed = run_docker_with_storage_quota_fallback(
                self.command(lease),
                input_text=payload,
                timeout=self.timeout_seconds,
                runner=subprocess.run,
            )
        except subprocess.TimeoutExpired as exc:
            raise RuntimeError("Dependency build timed out") from exc
        except OSError as exc:
            raise RuntimeError("Dependency builder is unavailable") from exc
        finally:
            self._cleanup(lease)
        if completed.returncode != 0:
            raise RuntimeError(
                f"Dependency build failed: {(completed.stderr or completed.stdout)[:2000]}"
            )
        try:
            raw = cast(object, json.loads(completed.stdout))
            if not isinstance(raw, dict):
                raise ValueError("response must be an object")
            response = cast(dict[str, Any], raw)
            content = base64.b64decode(str(response["content_base64"]), validate=True)
            manifest = response.get("manifest")
            if not isinstance(manifest, dict) or len(content) > _MAX_BUNDLE_BYTES:
                raise ValueError("invalid dependency bundle")
        except (KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
            raise RuntimeError("Dependency builder returned an invalid bundle") from exc
        return DependencyBundle(content, cast(dict[str, object], manifest))


class RemoteDependencyBuilder:
    """Builds dependency bundles through the fixed controller operation."""

    def __init__(self, settings: Settings) -> None:
        self.settings = settings

    def build(self, dependencies: dict[str, dict[str, str]]) -> DependencyBundle:
        try:
            response = DockerControllerClient(self.settings).post(
                "/v1/dependencies/build",
                {"dependencies": dependencies},
                timeout=self.settings.dependency_builder_timeout_seconds + 10,
            )
        except DockerControllerError as exc:
            raise RuntimeError("Dependency builder controller is unavailable") from exc
        try:
            content = base64.b64decode(str(response["content_base64"]), validate=True)
            manifest = response.get("manifest")
            if not isinstance(manifest, dict) or len(content) > _MAX_BUNDLE_BYTES:
                raise ValueError("invalid dependency bundle")
        except (KeyError, TypeError, ValueError) as exc:
            raise RuntimeError("Dependency builder returned an invalid bundle") from exc
        return DependencyBundle(content, cast(dict[str, object], manifest))


def build_dependency_builder(settings: Settings) -> DependencyBuilderPort:
    if settings.dependency_builder_backend == "disabled":
        return DisabledDependencyBuilder()
    if settings.dependency_builder_backend == "docker":
        if controller_is_remote(settings):
            return RemoteDependencyBuilder(settings)
        return DockerDependencyBuilder(
            settings.dependency_builder_image,
            docker_binary=settings.docker_binary,
            manager_scope=settings.sandbox_manager_scope,
            timeout_seconds=settings.dependency_builder_timeout_seconds,
            cleanup_grace_seconds=settings.sandbox_orphan_grace_seconds,
            storage_size=settings.sandbox_storage_size,
        )
    raise ValueError("Unsupported dependency builder backend")
