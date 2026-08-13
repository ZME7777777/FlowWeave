from __future__ import annotations

import base64
import hmac
import json
import os
import subprocess
import time
from collections.abc import AsyncIterator
from dataclasses import dataclass
from typing import Any, Literal, cast

import httpx

from flowweave.bootstrap.settings import Settings
from flowweave.shared.errors import DomainError
from flowweave.shared.infrastructure.docker_control import (
    DockerControlError,
    DockerOwnershipError,
    inspect_owned_container,
)


class DockerControllerError(RuntimeError):
    """The narrow Docker controller could not complete an operation."""


def controller_is_remote(settings: Settings) -> bool:
    return settings.docker_controller_mode == "remote"


_PLUGIN_VALIDATION_SCRIPT = r"""
import json
import sys
from pathlib import Path

from openhands.sdk.plugin import Plugin

plugin = Plugin.load(Path(sys.argv[1]))
print(json.dumps({
    "plugin_name": plugin.name,
    "plugin_version": plugin.version,
    "skill_count": len(plugin.skills),
    "command_count": len(plugin.commands),
    "agent_count": len(plugin.agents),
    "mcp_server_count": len(plugin.mcp_config),
    "has_hooks": plugin.hooks is not None and not plugin.hooks.is_empty(),
}, sort_keys=True))
"""


def validate_owned_runtime_plugin(
    settings: Settings,
    *,
    resource_name: str,
    resource_id: str,
    validation_id: str,
    plugin_path: str,
) -> dict[str, Any]:
    """Run only OpenHands' native Plugin loader in an owned Runtime container."""

    expected_prefix = f"/runtime/capabilities/nodes/plugin-probe-{validation_id}/plugins/"
    if not plugin_path.startswith(expected_prefix):
        raise DomainError(
            "PLUGIN_TARGET_PATH_INVALID",
            "The Plugin path does not belong to this validation",
            422,
        )

    try:
        container_id = inspect_owned_container(
            settings.docker_binary,
            resource_name,
            resource_id,
            expected_manager_scope=settings.sandbox_manager_scope,
            expected_kind="agent-runtime",
            timeout=10,
        )
    except DockerOwnershipError as exc:
        raise DomainError(
            "SANDBOX_RESOURCE_CONFLICT",
            "The Runtime container is owned by another sandbox",
            409,
        ) from exc
    except DockerControlError as exc:
        raise DomainError(
            "SANDBOX_BACKEND_UNAVAILABLE",
            "The Runtime container could not be verified",
            503,
        ) from exc
    if container_id is None:
        raise DomainError(
            "SANDBOX_RESOURCE_MISSING",
            "The Runtime container no longer exists",
            409,
        )
    try:
        completed = subprocess.run(
            [
                settings.docker_binary,
                "exec",
                container_id,
                "/runtime/.venv/bin/python",
                "-I",
                "-c",
                _PLUGIN_VALIDATION_SCRIPT,
                plugin_path,
            ],
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
            env={"PATH": os.defpath},
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise DomainError(
            "PLUGIN_TARGET_VALIDATION_UNAVAILABLE",
            "The target Runtime Plugin loader is unavailable",
            503,
        ) from exc
    if completed.returncode:
        raise DomainError(
            "PLUGIN_TARGET_VALIDATION_FAILED",
            "The target Runtime rejected the frozen Plugin",
            422,
        )
    try:
        raw = cast(object, json.loads(completed.stdout))
    except ValueError as exc:
        raise DomainError(
            "PLUGIN_TARGET_VALIDATION_PROTOCOL_ERROR",
            "The target Runtime returned an invalid Plugin loader report",
            502,
        ) from exc
    if not isinstance(raw, dict):
        raise DomainError(
            "PLUGIN_TARGET_VALIDATION_PROTOCOL_ERROR",
            "The target Runtime returned an invalid Plugin loader report",
            502,
        )
    return cast(dict[str, Any], raw)


ControllerRole = Literal["api", "worker"]


def authorize_controller_request(
    settings: Settings, authorization: str | None
) -> ControllerRole | None:
    if not authorization:
        return None
    candidates: tuple[tuple[ControllerRole, str], ...] = (
        ("api", settings.docker_controller_api_key),
        ("worker", settings.docker_controller_worker_api_key),
    )
    for role, key in candidates:
        if key and hmac.compare_digest(authorization, f"Bearer {key}"):
            return role
    return None


@dataclass(frozen=True, slots=True)
class RemoteTerminal:
    id: str


class DockerControllerClient:
    """Synchronous client for fixed, high-level controller operations."""

    def __init__(self, settings: Settings) -> None:
        self.base_url = settings.docker_controller_url.rstrip("/")
        self.api_key = settings.docker_controller_api_key
        self.manager_scope = settings.sandbox_manager_scope

    def _request(
        self, path: str, payload: dict[str, Any], *, timeout: float = 60
    ) -> dict[str, Any]:
        try:
            response = httpx.post(
                f"{self.base_url}{path}",
                json={"manager_scope": self.manager_scope, **payload},
                headers={"Authorization": f"Bearer {self.api_key}"},
                timeout=timeout,
                follow_redirects=False,
            )
        except httpx.HTTPError as exc:
            raise DockerControllerError("Docker controller is unavailable") from exc
        try:
            raw = cast(object, response.json())
        except ValueError as exc:
            raise DockerControllerError("Docker controller returned invalid JSON") from exc
        body = cast(dict[str, Any], raw) if isinstance(raw, dict) else {}
        if response.is_error:
            error = body.get("error")
            detail = cast(dict[str, Any], error) if isinstance(error, dict) else {}
            raise DomainError(
                str(detail.get("code") or "DOCKER_CONTROLLER_FAILED"),
                str(detail.get("message") or "Docker controller operation failed"),
                response.status_code,
                cast(dict[str, Any], detail.get("details") or {}),
            )
        return body

    def post(self, path: str, payload: dict[str, Any], *, timeout: float = 60) -> dict[str, Any]:
        return self._request(path, payload, timeout=timeout)

    def start_terminal(
        self,
        *,
        resource_name: str,
        resource_id: str,
        environment_id: str | None,
        session_name: str | None,
        rows: int = 24,
        columns: int = 80,
    ) -> RemoteTerminal:
        body = self._request(
            "/v1/terminals/start",
            {
                "resource_name": resource_name,
                "resource_id": resource_id,
                "environment_id": environment_id,
                "session_name": session_name,
                "rows": rows,
                "columns": columns,
            },
            timeout=30,
        )
        terminal_id = str(body.get("terminal_id") or "")
        if not terminal_id:
            raise DockerControllerError("Docker controller omitted the terminal ID")
        return RemoteTerminal(terminal_id)

    def read_terminal(self, terminal: RemoteTerminal) -> tuple[bytes, bool]:
        body = self._request("/v1/terminals/read", {"terminal_id": terminal.id}, timeout=10)
        try:
            content = base64.b64decode(str(body.get("content_base64") or ""), validate=True)
        except ValueError as exc:
            raise DockerControllerError("Docker controller returned invalid terminal data") from exc
        return content, bool(body.get("eof"))

    def write_terminal(self, terminal: RemoteTerminal, content: bytes) -> None:
        self._request(
            "/v1/terminals/write",
            {
                "terminal_id": terminal.id,
                "content_base64": base64.b64encode(content).decode(),
            },
            timeout=10,
        )

    def resize_terminal(self, terminal: RemoteTerminal, rows: int, columns: int) -> None:
        self._request(
            "/v1/terminals/resize",
            {"terminal_id": terminal.id, "rows": rows, "columns": columns},
            timeout=10,
        )

    def close_terminal(self, terminal: RemoteTerminal) -> None:
        try:
            self._request("/v1/terminals/close", {"terminal_id": terminal.id}, timeout=10)
        except (DockerControllerError, DomainError):
            # Closing an attachment is best-effort. The controller also reaps
            # abandoned attachments after a bounded idle timeout.
            pass

    async def stream_runtime_events(
        self,
        *,
        resource_name: str,
        resource_id: str,
        conversation_id: str,
        channel: Literal["CONVERSATION", "BASH"] = "CONVERSATION",
        timeout_seconds: float = 10.0,
    ) -> AsyncIterator[dict[str, Any]]:
        """Read a Runtime event stream through the ownership-checking controller."""

        payload = {
            "manager_scope": self.manager_scope,
            "resource_name": resource_name,
            "resource_id": resource_id,
            "conversation_id": conversation_id,
            "channel": channel,
            "timeout_seconds": timeout_seconds,
        }
        try:
            async with httpx.AsyncClient(timeout=None, follow_redirects=False) as client:
                async with client.stream(
                    "POST",
                    f"{self.base_url}/v1/runtimes/events",
                    json=payload,
                    headers={"Authorization": f"Bearer {self.api_key}"},
                ) as response:
                    if response.is_error:
                        await response.aread()
                        try:
                            raw = cast(object, response.json())
                        except ValueError:
                            raw = {}
                        body = cast(dict[str, Any], raw) if isinstance(raw, dict) else {}
                        error = body.get("error")
                        detail = cast(dict[str, Any], error) if isinstance(error, dict) else {}
                        raise DomainError(
                            str(detail.get("code") or "DOCKER_CONTROLLER_FAILED"),
                            str(detail.get("message") or "Docker controller operation failed"),
                            response.status_code,
                            cast(dict[str, Any], detail.get("details") or {}),
                        )
                    async for line in response.aiter_lines():
                        if not line:
                            continue
                        try:
                            value = cast(object, json.loads(line))
                        except ValueError:
                            continue
                        if isinstance(value, dict):
                            yield cast(dict[str, Any], value)
        except DomainError:
            raise
        except httpx.HTTPError as exc:
            raise DockerControllerError("Docker controller stream is unavailable") from exc

    def wait_runtime_event(
        self,
        *,
        resource_name: str,
        resource_id: str,
        conversation_id: str,
        channel: Literal["CONVERSATION", "BASH"],
        timeout_seconds: float,
    ) -> bool:
        """Wait for one ownership-checked Runtime frame without an async-loop bridge."""

        payload = {
            "manager_scope": self.manager_scope,
            "resource_name": resource_name,
            "resource_id": resource_id,
            "conversation_id": conversation_id,
            "channel": channel,
            "timeout_seconds": timeout_seconds,
        }
        try:
            with httpx.Client(
                timeout=httpx.Timeout(timeout_seconds + 15), follow_redirects=False
            ) as client:
                with client.stream(
                    "POST",
                    f"{self.base_url}/v1/runtimes/events",
                    json=payload,
                    headers={"Authorization": f"Bearer {self.api_key}"},
                ) as response:
                    response.raise_for_status()
                    return any(line for line in response.iter_lines())
        except httpx.HTTPError as exc:
            raise DockerControllerError("Docker controller wake-up is unavailable") from exc


def wait_for_remote_terminal_output() -> None:
    time.sleep(0.05)


__all__ = (
    "ControllerRole",
    "DockerControllerClient",
    "DockerControllerError",
    "RemoteTerminal",
    "authorize_controller_request",
    "controller_is_remote",
    "validate_owned_runtime_plugin",
    "wait_for_remote_terminal_output",
)
