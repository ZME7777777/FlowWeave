from __future__ import annotations

import base64
import json
import os
import re
import subprocess
from typing import Any, cast
from urllib.parse import urlsplit

from flowweave.bootstrap.settings import Settings
from flowweave.shared.application.plugin_resolver import (
    MarketplaceCatalogRequest,
    MarketplacePluginResolveRequest,
    PluginResolveBundle,
    PluginResolveRequest,
    PluginResolverPort,
)
from flowweave.shared.infrastructure.docker_control import (
    DockerControlError,
    EphemeralDockerLease,
    remove_owned_container,
    remove_owned_network,
)
from flowweave.shared.infrastructure.docker_controller import (
    DockerControllerClient,
    DockerControllerError,
    controller_is_remote,
)

_COMMIT = re.compile(r"^[0-9a-f]{40}$")
_REPO_PATH = re.compile(r"^[A-Za-z0-9_.-]+(?:/[A-Za-z0-9_.-]+)*$")
_SOURCE_SEGMENT = re.compile(r"^[A-Za-z0-9_.~-]{1,128}$")
_MAX_BUNDLE_BYTES = 25 * 1024 * 1024


def validate_plugin_git_source(
    request: PluginResolveRequest, allowed_hosts: frozenset[str]
) -> PluginResolveRequest:
    """Narrow OpenHands' flexible fetch contract to immutable public HTTPS Git."""

    parsed = urlsplit(request.source)
    host = (parsed.hostname or "").lower().rstrip(".")
    if (
        parsed.scheme != "https"
        or not host
        or parsed.username is not None
        or parsed.password is not None
        or parsed.port not in {None, 443}
        or bool(parsed.query)
        or bool(parsed.fragment)
        or host not in allowed_hosts
    ):
        raise ValueError("Plugin source must be a credential-free HTTPS URL on an allowed host")
    segments = parsed.path.strip("/").split("/")
    if (
        len(segments) < 2
        or len(segments) > 16
        or any(
            segment in {"", ".", ".."} or not _SOURCE_SEGMENT.fullmatch(segment)
            for segment in segments
        )
    ):
        raise ValueError("Plugin source repository path is invalid")
    commit = request.commit.lower()
    if not _COMMIT.fullmatch(commit):
        raise ValueError("Plugin source must use a complete 40-character commit SHA")
    repo_path = request.repo_path
    if repo_path is not None and (
        not _REPO_PATH.fullmatch(repo_path)
        or any(part in {"", ".", ".."} for part in repo_path.split("/"))
    ):
        raise ValueError("Plugin repository subpath is invalid")
    canonical_source = f"https://{host}/{'/'.join(segments)}"
    return PluginResolveRequest(canonical_source, commit, repo_path)


def _decode_bundle(
    response: dict[str, Any],
    expected_commit: str | None,
    *,
    allowed_hosts: frozenset[str] | None = None,
) -> PluginResolveBundle:
    try:
        content = base64.b64decode(str(response["content_base64"]), validate=True)
        resolved_commit = str(response["resolved_commit"]).lower()
        report = response.get("report")
        if (
            (expected_commit is not None and resolved_commit != expected_commit)
            or not isinstance(report, dict)
            or not content
            or len(content) > _MAX_BUNDLE_BYTES
        ):
            raise ValueError("invalid resolver response")
    except (KeyError, TypeError, ValueError) as exc:
        raise RuntimeError("Plugin resolver returned an invalid bundle") from exc
    resolved_source = response.get("resolved_source")
    resolved_repo_path = response.get("resolved_repo_path")
    if expected_commit is None:
        if allowed_hosts is None or not isinstance(resolved_source, str):
            raise RuntimeError("Marketplace resolver returned an invalid source")
        resolved = validate_plugin_git_source(
            PluginResolveRequest(
                resolved_source,
                resolved_commit,
                str(resolved_repo_path) if resolved_repo_path is not None else None,
            ),
            allowed_hosts,
        )
        resolved_source = resolved.source
        resolved_commit = resolved.commit
        resolved_repo_path = resolved.repo_path
    return PluginResolveBundle(
        content,
        resolved_commit,
        cast(dict[str, object], report),
        resolved_source=str(resolved_source) if resolved_source is not None else None,
        resolved_repo_path=(str(resolved_repo_path) if resolved_repo_path is not None else None),
    )


class DisabledPluginResolver:
    def resolve(self, request: PluginResolveRequest) -> PluginResolveBundle:
        del request
        raise RuntimeError("Plugin resolver is disabled")

    def resolve_marketplace_plugin(
        self, request: MarketplacePluginResolveRequest
    ) -> PluginResolveBundle:
        del request
        raise RuntimeError("Plugin resolver is disabled")

    def list_marketplace(self, request: MarketplaceCatalogRequest) -> dict[str, object]:
        del request
        raise RuntimeError("Plugin resolver is disabled")


class DockerPluginResolver:
    """Run the fixed OpenHands resolver without accepting shell or Docker arguments."""

    def __init__(
        self,
        image: str,
        *,
        allowed_hosts: frozenset[str],
        docker_binary: str = "docker",
        manager_scope: str,
        timeout_seconds: int = 300,
        cleanup_grace_seconds: int = 300,
        storage_size: str = "4g",
    ) -> None:
        self.image = image
        self.allowed_hosts = allowed_hosts
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
            "/tmp:rw,noexec,nosuid,nodev,size=128m",
            "--tmpfs",
            "/work:rw,nosuid,nodev,size=256m",
            "--env",
            "HOME=/tmp",
            "--env",
            "OPENHANDS_SUPPRESS_BANNER=1",
            "--entrypoint",
            "/runtime/.venv/bin/python",
            self.image,
            "-I",
            "/runtime/plugin_resolver.py",
        ]

    def _create_network(self, lease: EphemeralDockerLease) -> None:
        command = [
            self.docker_binary,
            "network",
            "create",
            "--driver",
            "bridge",
            *lease.network_label_args(purpose="plugin-resolve", mode="egress"),
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
            raise RuntimeError("Plugin resolver network is unavailable") from exc
        if completed.returncode:
            raise RuntimeError(
                f"Plugin resolver network failed: {(completed.stderr or completed.stdout)[:2000]}"
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
            pass

    def resolve(self, request: PluginResolveRequest) -> PluginResolveBundle:
        request = validate_plugin_git_source(request, self.allowed_hosts)
        lease = EphemeralDockerLease.create(
            kind="plugin-resolve",
            owner_type="PLUGIN_RESOLUTION",
            manager_scope=self.manager_scope,
            ttl_seconds=self.timeout_seconds + self.cleanup_grace_seconds,
        )
        payload = json.dumps(
            {
                "schema_version": 1,
                "source": request.source,
                "commit": request.commit,
                "repo_path": request.repo_path,
                "allowed_hosts": sorted(self.allowed_hosts),
            },
            ensure_ascii=True,
            separators=(",", ":"),
        )
        try:
            self._create_network(lease)
            completed = subprocess.run(
                self.command(lease),
                input=payload,
                capture_output=True,
                text=True,
                timeout=self.timeout_seconds,
                check=False,
                env={"PATH": os.defpath},
            )
        except subprocess.TimeoutExpired as exc:
            raise RuntimeError("Plugin resolution timed out") from exc
        except OSError as exc:
            raise RuntimeError("Plugin resolver is unavailable") from exc
        finally:
            self._cleanup(lease)
        if completed.returncode:
            raise RuntimeError(
                f"Plugin resolution failed: {(completed.stderr or completed.stdout)[-2000:]}"
            )
        try:
            raw_object = cast(object, json.loads(completed.stdout))
            if not isinstance(raw_object, dict):
                raise ValueError("response must be an object")
            raw = cast(dict[object, object], raw_object)
        except (ValueError, json.JSONDecodeError) as exc:
            raise RuntimeError("Plugin resolver returned invalid JSON") from exc
        return _decode_bundle(cast(dict[str, Any], raw), request.commit)

    def resolve_marketplace_plugin(
        self, request: MarketplacePluginResolveRequest
    ) -> PluginResolveBundle:
        marketplace = validate_plugin_git_source(
            PluginResolveRequest(
                request.marketplace_source,
                request.marketplace_commit,
                request.marketplace_repo_path,
            ),
            self.allowed_hosts,
        )
        if re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,127}", request.plugin_name) is None:
            raise ValueError("Marketplace Plugin name is invalid")
        payload = {
            "schema_version": 2,
            "source_kind": "MARKETPLACE",
            "source": marketplace.source,
            "commit": marketplace.commit,
            "repo_path": marketplace.repo_path,
            "plugin_name": request.plugin_name,
            "allowed_hosts": sorted(self.allowed_hosts),
        }
        lease = EphemeralDockerLease.create(
            kind="plugin-resolve",
            owner_type="PLUGIN_RESOLUTION",
            manager_scope=self.manager_scope,
            ttl_seconds=self.timeout_seconds + self.cleanup_grace_seconds,
        )
        try:
            self._create_network(lease)
            completed = subprocess.run(
                self.command(lease),
                input=json.dumps(payload, ensure_ascii=True, separators=(",", ":")),
                capture_output=True,
                text=True,
                timeout=self.timeout_seconds,
                check=False,
                env={"PATH": os.defpath},
            )
        except subprocess.TimeoutExpired as exc:
            raise RuntimeError("Marketplace Plugin resolution timed out") from exc
        except OSError as exc:
            raise RuntimeError("Plugin resolver is unavailable") from exc
        finally:
            self._cleanup(lease)
        if completed.returncode:
            raise RuntimeError(
                f"Marketplace Plugin resolution failed: "
                f"{(completed.stderr or completed.stdout)[-2000:]}"
            )
        try:
            raw_object = cast(object, json.loads(completed.stdout))
            if not isinstance(raw_object, dict):
                raise ValueError("response must be an object")
            raw = cast(dict[object, object], raw_object)
        except (ValueError, json.JSONDecodeError) as exc:
            raise RuntimeError("Plugin resolver returned invalid JSON") from exc
        return _decode_bundle(cast(dict[str, Any], raw), None, allowed_hosts=self.allowed_hosts)

    def list_marketplace(self, request: MarketplaceCatalogRequest) -> dict[str, object]:
        marketplace = validate_plugin_git_source(
            PluginResolveRequest(
                request.marketplace_source,
                request.marketplace_commit,
                request.marketplace_repo_path,
            ),
            self.allowed_hosts,
        )
        payload = {
            "schema_version": 3,
            "source_kind": "MARKETPLACE_CATALOG",
            "source": marketplace.source,
            "commit": marketplace.commit,
            "repo_path": marketplace.repo_path,
            "allowed_hosts": sorted(self.allowed_hosts),
        }
        lease = EphemeralDockerLease.create(
            kind="plugin-resolve",
            owner_type="PLUGIN_RESOLUTION",
            manager_scope=self.manager_scope,
            ttl_seconds=self.timeout_seconds + self.cleanup_grace_seconds,
        )
        try:
            self._create_network(lease)
            completed = subprocess.run(
                self.command(lease),
                input=json.dumps(payload, ensure_ascii=True, separators=(",", ":")),
                capture_output=True,
                text=True,
                timeout=self.timeout_seconds,
                check=False,
                env={"PATH": os.defpath},
            )
        except subprocess.TimeoutExpired as exc:
            raise RuntimeError("Marketplace catalog resolution timed out") from exc
        except OSError as exc:
            raise RuntimeError("Plugin resolver is unavailable") from exc
        finally:
            self._cleanup(lease)
        if completed.returncode:
            raise RuntimeError(
                "Marketplace catalog resolution failed: "
                f"{(completed.stderr or completed.stdout)[-2000:]}"
            )
        try:
            raw_object = cast(object, json.loads(completed.stdout))
            if not isinstance(raw_object, dict):
                raise ValueError("response must be an object")
            raw = cast(dict[object, object], raw_object)
        except (ValueError, json.JSONDecodeError) as exc:
            raise RuntimeError("Plugin resolver returned invalid JSON") from exc
        if raw.get("commit") != marketplace.commit or not isinstance(raw.get("plugins"), list):
            raise RuntimeError("Plugin resolver returned an invalid Marketplace catalog")
        return cast(dict[str, object], raw)


class RemotePluginResolver:
    def __init__(self, settings: Settings, allowed_hosts: frozenset[str]) -> None:
        self.settings = settings
        self.allowed_hosts = allowed_hosts

    def resolve(self, request: PluginResolveRequest) -> PluginResolveBundle:
        request = validate_plugin_git_source(request, self.allowed_hosts)
        try:
            response = DockerControllerClient(self.settings).post(
                "/v1/plugins/resolve",
                {
                    "source": request.source,
                    "commit": request.commit,
                    "repo_path": request.repo_path,
                },
                timeout=self.settings.plugin_resolver_timeout_seconds + 10,
            )
        except DockerControllerError as exc:
            raise RuntimeError("Plugin resolver controller is unavailable") from exc
        return _decode_bundle(response, request.commit)

    def resolve_marketplace_plugin(
        self, request: MarketplacePluginResolveRequest
    ) -> PluginResolveBundle:
        marketplace = validate_plugin_git_source(
            PluginResolveRequest(
                request.marketplace_source,
                request.marketplace_commit,
                request.marketplace_repo_path,
            ),
            self.allowed_hosts,
        )
        try:
            response = DockerControllerClient(self.settings).post(
                "/v1/plugins/resolve-marketplace",
                {
                    "source": marketplace.source,
                    "commit": marketplace.commit,
                    "repo_path": marketplace.repo_path,
                    "plugin_name": request.plugin_name,
                },
                timeout=self.settings.plugin_resolver_timeout_seconds + 10,
            )
        except DockerControllerError as exc:
            raise RuntimeError("Plugin resolver controller is unavailable") from exc
        return _decode_bundle(response, None, allowed_hosts=self.allowed_hosts)

    def list_marketplace(self, request: MarketplaceCatalogRequest) -> dict[str, object]:
        marketplace = validate_plugin_git_source(
            PluginResolveRequest(
                request.marketplace_source,
                request.marketplace_commit,
                request.marketplace_repo_path,
            ),
            self.allowed_hosts,
        )
        try:
            response = DockerControllerClient(self.settings).post(
                "/v1/plugins/list-marketplace",
                {
                    "source": marketplace.source,
                    "commit": marketplace.commit,
                    "repo_path": marketplace.repo_path,
                },
                timeout=self.settings.plugin_resolver_timeout_seconds + 10,
            )
        except DockerControllerError as exc:
            raise RuntimeError("Plugin resolver controller is unavailable") from exc
        if response.get("commit") != marketplace.commit or not isinstance(
            response.get("plugins"), list
        ):
            raise RuntimeError("Plugin resolver returned an invalid Marketplace catalog")
        return cast(dict[str, object], response)


def configured_plugin_hosts(settings: Settings) -> frozenset[str]:
    hosts = frozenset(
        value.strip().lower().rstrip(".")
        for value in settings.plugin_resolver_allowed_hosts.split(",")
        if value.strip()
    )
    if not hosts or any(
        re.fullmatch(r"[a-z0-9](?:[a-z0-9.-]{0,251}[a-z0-9])?", host) is None for host in hosts
    ):
        raise ValueError("PLUGIN_RESOLVER_ALLOWED_HOSTS must contain valid DNS names")
    return hosts


def build_plugin_resolver(settings: Settings) -> PluginResolverPort:
    hosts = configured_plugin_hosts(settings)
    if settings.plugin_resolver_backend == "disabled":
        return DisabledPluginResolver()
    if settings.plugin_resolver_backend == "docker":
        if controller_is_remote(settings):
            return RemotePluginResolver(settings, hosts)
        return DockerPluginResolver(
            settings.plugin_resolver_image,
            allowed_hosts=hosts,
            docker_binary=settings.docker_binary,
            manager_scope=settings.sandbox_manager_scope,
            timeout_seconds=settings.plugin_resolver_timeout_seconds,
            cleanup_grace_seconds=settings.sandbox_orphan_grace_seconds,
            storage_size=settings.sandbox_storage_size,
        )
    raise ValueError("Unsupported Plugin resolver backend")
