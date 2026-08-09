from __future__ import annotations

import base64
import json
import os
import subprocess
from typing import Any, cast
from uuid import uuid4

from flowweave.bootstrap.settings import Settings
from flowweave.shared.application.dependency_builder import (
    DependencyBuilderPort,
    DependencyBundle,
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
        network: str,
        *,
        docker_binary: str = "docker",
        timeout_seconds: int = 300,
    ) -> None:
        self.image = image
        self.network = network
        self.docker_binary = docker_binary
        self.timeout_seconds = timeout_seconds

    def command(self, name: str) -> list[str]:
        return [
            self.docker_binary,
            "run",
            "--rm",
            "--interactive",
            "--name",
            name,
            "--network",
            self.network,
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

    def build(self, dependencies: dict[str, dict[str, str]]) -> DependencyBundle:
        name = f"flowweave-dependency-{uuid4().hex}"
        payload = json.dumps(
            {"schema_version": 1, "dependencies": dependencies},
            ensure_ascii=True,
            separators=(",", ":"),
        )
        try:
            completed = subprocess.run(
                self.command(name),
                input=payload,
                capture_output=True,
                text=True,
                timeout=self.timeout_seconds,
                check=False,
                env={"PATH": os.defpath},
            )
        except subprocess.TimeoutExpired as exc:
            subprocess.run(
                [self.docker_binary, "rm", "--force", name],
                capture_output=True,
                text=True,
                timeout=5,
                check=False,
                env={"PATH": os.defpath},
            )
            raise RuntimeError("Dependency build timed out") from exc
        except OSError as exc:
            raise RuntimeError("Dependency builder is unavailable") from exc
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


def build_dependency_builder(settings: Settings) -> DependencyBuilderPort:
    if settings.dependency_builder_backend == "disabled":
        return DisabledDependencyBuilder()
    if settings.dependency_builder_backend == "docker":
        return DockerDependencyBuilder(
            settings.dependency_builder_image,
            settings.dependency_builder_network,
            timeout_seconds=settings.dependency_builder_timeout_seconds,
        )
    raise ValueError("Unsupported dependency builder backend")
