from __future__ import annotations

import base64
import hmac
import json
import time
from collections.abc import AsyncIterator
from dataclasses import dataclass
from typing import Any, Literal, cast

import httpx

from flowweave.bootstrap.settings import Settings
from flowweave.shared.errors import DomainError


class DockerControllerError(RuntimeError):
    """The narrow Docker controller could not complete an operation."""


def controller_is_remote(settings: Settings) -> bool:
    return settings.docker_controller_mode == "remote"


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
    ) -> AsyncIterator[dict[str, Any]]:
        """Read a Runtime event stream through the ownership-checking controller."""

        payload = {
            "manager_scope": self.manager_scope,
            "resource_name": resource_name,
            "resource_id": resource_id,
            "conversation_id": conversation_id,
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


def wait_for_remote_terminal_output() -> None:
    time.sleep(0.05)


__all__ = (
    "ControllerRole",
    "DockerControllerClient",
    "DockerControllerError",
    "RemoteTerminal",
    "authorize_controller_request",
    "controller_is_remote",
    "wait_for_remote_terminal_output",
)
