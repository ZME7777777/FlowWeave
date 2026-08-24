from __future__ import annotations

import asyncio
import base64
import json
import os
import re
import secrets
import subprocess
import time
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import datetime
from typing import Annotated, Any, Literal, cast
from uuid import UUID

from fastapi import FastAPI, Request, Response
from fastapi.responses import JSONResponse, StreamingResponse
from pydantic import BaseModel, ConfigDict, Field, SecretStr, field_validator, model_validator

from flowweave.bootstrap.settings import Settings
from flowweave.modules.environments.infrastructure import docker as environments_docker
from flowweave.modules.sandboxes.infrastructure.docker import (
    DockerSandboxProvider,
    backend_name,
)
from flowweave.modules.sandboxes.infrastructure.models import ManagedSandbox
from flowweave.shared.application.plugin_resolver import (
    MarketplaceCatalogRequest,
    MarketplacePluginResolveRequest,
    PluginResolveRequest,
)
from flowweave.shared.errors import DomainError
from flowweave.shared.infrastructure.dependency_builder import DockerDependencyBuilder
from flowweave.shared.infrastructure.docker_control import (
    DockerControlError,
    DockerOwnershipError,
    inspect_owned_container,
)
from flowweave.shared.infrastructure.docker_controller import (
    authorize_controller_request,
    validate_owned_runtime_plugin,
)
from flowweave.shared.infrastructure.plugin_resolver import (
    DockerPluginResolver,
    configured_plugin_hosts,
)
from flowweave.shared.infrastructure.sandbox import DockerSandbox
from flowweave.shared.settings import bind_settings, reset_settings

_MAX_REQUEST_BYTES = 2 * 1024 * 1024


class _StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class ScopedRequest(_StrictModel):
    manager_scope: str = Field(min_length=1, max_length=128)


class SetupSandboxSpec(_StrictModel):
    environment_id: UUID
    base_version_id: UUID | None
    base_version_no: int | None = Field(ge=1)
    base_image_reference: str = Field(min_length=1, max_length=500)
    base_image_digest: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")


class RuntimeProviderSpec(_StrictModel):
    workspace_relative: str | None = Field(
        default=None,
        max_length=500,
        pattern=r"^[A-Za-z0-9_.-]+(?:/[A-Za-z0-9_.-]+)*$",
    )
    port: Literal[8000]
    environment_id: UUID
    environment_version_id: UUID
    environment_version_no: int = Field(ge=1)
    flow_run_id: UUID | None = None
    runtime_allocation_id: UUID | None = None
    runtime_allocation_relative: str | None = Field(
        default=None,
        max_length=500,
        pattern=r"^\.flow-run-runtimes/[0-9a-f]{32}/[0-9a-f-]{36}$",
    )
    runtime_secret_reference_id: UUID | None = None

    @field_validator("workspace_relative")
    @classmethod
    def validate_workspace_relative(cls, value: str | None) -> str | None:
        if value is None:
            return None
        if any(part in {"", ".", ".."} for part in value.split("/")):
            raise ValueError("workspace_relative must not contain dot path segments")
        return value

    @model_validator(mode="after")
    def validate_workspace_contract(self):
        allocation_fields = (
            self.flow_run_id,
            self.runtime_allocation_id,
            self.runtime_allocation_relative,
            self.runtime_secret_reference_id,
        )
        if any(value is not None for value in allocation_fields) and not all(
            value is not None for value in allocation_fields
        ):
            raise ValueError("FlowRun Runtime allocation fields must be provided together")
        if (self.flow_run_id is not None) == (self.workspace_relative is not None):
            raise ValueError(
                "FlowRun and temporary Runtime workspace contracts are mutually exclusive"
            )
        return self


class _SandboxResourceBase(ScopedRequest):
    id: UUID
    owner_id: UUID
    backend_resource_name: str = Field(min_length=1, max_length=100)
    image_reference: str = Field(min_length=1, max_length=500)
    created_at: datetime


class SetupSandboxResourceWrite(_SandboxResourceBase):
    kind: Literal["ENVIRONMENT_SETUP"]
    owner_type: Literal["SETUP_SESSION"]
    spec: SetupSandboxSpec


class RuntimeProviderResourceWrite(_SandboxResourceBase):
    kind: Literal["AGENT_RUNTIME"]
    owner_type: Literal[
        "FLOW_RUN",
        "CAPABILITY_VALIDATION",
        "MCP_OAUTH_AUTHORIZATION",
    ]
    image_reference: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    spec: RuntimeProviderSpec
    runtime_secret_key: SecretStr | None = None

    @model_validator(mode="after")
    def validate_runtime_secret(self):
        has_allocation = self.spec.runtime_allocation_relative is not None
        if has_allocation != (self.runtime_secret_key is not None):
            raise ValueError(
                "runtime_secret_key must be injected exactly for FlowRun Runtime allocations"
            )
        if has_allocation != (self.owner_type == "FLOW_RUN"):
            raise ValueError("FlowRun Runtime allocation owner is invalid")
        if (
            self.runtime_secret_key is not None
            and len(self.runtime_secret_key.get_secret_value()) < 32
        ):
            raise ValueError("runtime_secret_key is too short")
        return self


SandboxResourceWrite = Annotated[
    SetupSandboxResourceWrite | RuntimeProviderResourceWrite, Field(discriminator="kind")
]


class SandboxNameWrite(ScopedRequest):
    # Accept both legacy fw-sbx-<uuid> names and the newer owner-labelled
    # deterministic names. Resource-ID labels remain the ownership authority.
    resource_name: str = Field(min_length=8, max_length=100, pattern=r"^fw-sbx-[a-z0-9-]+$")


class SandboxDeleteWrite(SandboxNameWrite):
    resource_id: UUID


class ResolveContainerWrite(SandboxDeleteWrite):
    environment_id: UUID


class LegacyRemoveWrite(ScopedRequest):
    resource_name: str = Field(
        min_length=1,
        max_length=100,
        pattern=r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,99}$",
    )
    resource_id: Literal["legacy"]
    environment_id: UUID


class RemoveImageWrite(ScopedRequest):
    reference: str = Field(
        pattern=r"^flowweave/environment-[a-z0-9_.-]+:v[1-9][0-9]*-[0-9a-f]{32}$"
    )
    expected_digest: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    environment_id: UUID
    version_id: UUID
    version_no: int | None = Field(default=None, ge=1)


class ResolveBaseImageWrite(ScopedRequest):
    reference: str = Field(
        max_length=500,
        pattern=r"^[A-Za-z0-9][A-Za-z0-9._/:+-]{0,430}@sha256:[0-9a-f]{64}$",
    )


class PublishImageWrite(SandboxDeleteWrite):
    environment_id: UUID
    version_id: UUID
    version_no: int = Field(ge=1)
    base_image_reference: str = Field(min_length=1, max_length=500)
    base_image_digest: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")


class EnvironmentCredentialsWrite(ScopedRequest):
    environment_id: UUID


class GateExecuteWrite(ScopedRequest):
    language: Literal["PYTHON", "JAVASCRIPT"]
    code: str = Field(min_length=1, max_length=32_768)
    context: dict[str, Any]
    timeout_seconds: int = Field(ge=1, le=300)


class DependencyBuildWrite(ScopedRequest):
    dependencies: dict[str, dict[str, str]]

    @field_validator("dependencies")
    @classmethod
    def validate_dependencies(cls, value: dict[str, dict[str, str]]) -> dict[str, dict[str, str]]:
        if set(value) - {"python", "node", "cli"}:
            raise ValueError("unsupported dependency ecosystem")
        if sum(len(group) for group in value.values()) > 50:
            raise ValueError("too many dependencies")
        name_pattern = r"^[A-Za-z0-9][A-Za-z0-9._@/-]{0,127}$"
        version_pattern = r"^[0-9]+(?:\.[0-9]+){1,3}(?:[-+][A-Za-z0-9.-]+)?$"
        for group in value.values():
            for name, version in group.items():
                if (
                    not re.fullmatch(name_pattern, name)
                    or ".." in name
                    or name.startswith(("/", "."))
                    or not re.fullmatch(version_pattern, version)
                ):
                    raise ValueError("dependency names and versions must be exact and safe")
        return value


class PluginResolveWrite(ScopedRequest):
    source: str = Field(min_length=1, max_length=2048)
    commit: str = Field(pattern=r"^[0-9a-f]{40}$")
    repo_path: str | None = Field(
        default=None,
        max_length=500,
        pattern=r"^[A-Za-z0-9_.-]+(?:/[A-Za-z0-9_.-]+)*$",
    )


class MarketplacePluginResolveWrite(PluginResolveWrite):
    plugin_name: str = Field(
        min_length=1,
        max_length=128,
        pattern=r"^[A-Za-z0-9][A-Za-z0-9_.-]*$",
    )


class MarketplaceCatalogWrite(PluginResolveWrite):
    pass


class TerminalStartWrite(ResolveContainerWrite):
    session_name: str | None = Field(default=None, max_length=64)
    rows: int = Field(default=24, ge=2, le=200)
    columns: int = Field(default=80, ge=20, le=400)


class TerminalIdWrite(ScopedRequest):
    terminal_id: str = Field(min_length=32, max_length=64)


class TerminalWrite(TerminalIdWrite):
    content_base64: str = Field(max_length=131_072)


class TerminalResizeWrite(TerminalIdWrite):
    rows: int = Field(ge=2, le=200)
    columns: int = Field(ge=20, le=400)


class RuntimeEventsWrite(SandboxDeleteWrite):
    channel: Literal["CONVERSATION", "BASH"] = "CONVERSATION"
    conversation_id: str = Field(
        default="",
        min_length=0,
        max_length=200,
        pattern=r"^(?:[A-Za-z0-9][A-Za-z0-9_.:-]{0,199})?$",
    )
    timeout_seconds: float = Field(default=10.0, gt=0, le=25)

    @model_validator(mode="after")
    def validate_channel_target(self) -> RuntimeEventsWrite:
        if self.channel == "CONVERSATION" and not self.conversation_id:
            raise ValueError("conversation_id is required for Conversation events")
        return self


class RuntimePluginValidationWrite(SandboxDeleteWrite):
    validation_id: UUID
    plugin_path: str = Field(
        min_length=1,
        max_length=1000,
        pattern=r"^/runtime/capabilities/nodes/plugin-probe-[A-Za-z0-9_.-]+/plugins/[A-Za-z0-9_.-]+$",
    )


@dataclass(slots=True)
class _TerminalAttachment:
    master: int
    process: subprocess.Popen[bytes]
    last_activity: float


class _TerminalManager:
    def __init__(self, *, idle_seconds: int) -> None:
        self.idle_seconds = idle_seconds
        self.attachments: dict[str, _TerminalAttachment] = {}

    def start(self, container_id: str, session_name: str | None, rows: int, columns: int) -> str:
        master, process = environments_docker.open_terminal(
            container_id, session_name=session_name, rows=rows, columns=columns
        )
        os.set_blocking(master, False)
        terminal_id = secrets.token_hex(24)
        self.attachments[terminal_id] = _TerminalAttachment(master, process, time.monotonic())
        return terminal_id

    def get(self, terminal_id: str) -> _TerminalAttachment:
        item = self.attachments.get(terminal_id)
        if item is None:
            raise DomainError("TERMINAL_NOT_FOUND", "Terminal attachment is unavailable", 404)
        item.last_activity = time.monotonic()
        return item

    def read(self, terminal_id: str) -> tuple[bytes, bool]:
        item = self.get(terminal_id)
        try:
            content = os.read(item.master, 65_536)
        except BlockingIOError:
            content = b""
        except OSError:
            content = b""
        return content, item.process.poll() is not None

    def resize(self, terminal_id: str, rows: int, columns: int) -> None:
        item = self.get(terminal_id)
        environments_docker.resize_terminal(item.master, rows, columns, item.process)

    def close(self, terminal_id: str) -> None:
        item = self.attachments.pop(terminal_id, None)
        if item is None:
            return
        if item.process.poll() is None:
            item.process.terminate()
            try:
                item.process.wait(timeout=2)
            except subprocess.TimeoutExpired:
                item.process.kill()
                item.process.wait(timeout=2)
        os.close(item.master)

    def reap(self) -> None:
        now = time.monotonic()
        for terminal_id, item in list(self.attachments.items()):
            if item.process.poll() is not None or now - item.last_activity >= self.idle_seconds:
                self.close(terminal_id)

    def close_all(self) -> None:
        for terminal_id in list(self.attachments):
            self.close(terminal_id)


_RUNTIME_EVENT_RELAY = r"""
import asyncio
import json
import os
import sys

from websockets.asyncio.client import connect


async def main():
    channel = sys.argv[1]
    conversation_id = sys.argv[2]
    timeout_seconds = float(sys.argv[3])
    path = (
        f"/sockets/events/{conversation_id}"
        if channel == "CONVERSATION"
        else "/sockets/bash-events"
    )
    async with connect(
        f"ws://127.0.0.1:8000{path}",
        open_timeout=10,
        ping_interval=20,
        ping_timeout=20,
        max_size=2 * 1024 * 1024,
    ) as upstream:
        await upstream.send(json.dumps({
            "type": "auth",
            "session_api_key": os.environ["SESSION_API_KEY"],
        }))
        try:
            frame = await asyncio.wait_for(upstream.recv(), timeout=timeout_seconds)
        except TimeoutError:
            return
        if isinstance(frame, str):
            print(frame, flush=True)


asyncio.run(main())
"""


async def _runtime_event_stream(
    configured: Settings,
    container_id: str,
    channel: str,
    conversation_id: str,
    timeout_seconds: float,
) -> AsyncIterator[bytes]:
    """Run the fixed relay inside one ownership-verified Runtime container."""

    process = await asyncio.create_subprocess_exec(
        configured.docker_binary,
        "exec",
        container_id,
        "/runtime/.venv/bin/python",
        "-u",
        "-c",
        _RUNTIME_EVENT_RELAY,
        channel,
        conversation_id,
        str(timeout_seconds),
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        env={"PATH": os.defpath},
    )
    assert process.stdout is not None
    try:
        while line := await process.stdout.readline():
            # The Runtime owns the JSON schema. Preserve only valid JSON objects
            # and normalize framing so one upstream event is one NDJSON record.
            try:
                value = json.loads(line)
            except ValueError:
                continue
            if isinstance(value, dict):
                yield json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode() + b"\n"
        return_code = await process.wait()
        if return_code:
            assert process.stderr is not None
            detail = (await process.stderr.read()).decode(errors="replace")[-2000:]
            raise RuntimeError(f"Runtime event relay exited with {return_code}: {detail}")
    finally:
        if process.returncode is None:
            process.terminate()
            try:
                await asyncio.wait_for(process.wait(), timeout=2)
            except TimeoutError:
                process.kill()
                await process.wait()


def _observation_dict(value: object) -> dict[str, Any] | None:
    if value is None:
        return None
    observation = cast(Any, value)
    return {
        "resource_id": observation.resource_id,
        "resource_name": observation.resource_name,
        "resource_identifier": observation.resource_identifier,
        "state": observation.state,
        "labels": observation.labels,
        "resource_type": observation.resource_type,
    }


def _resource(payload: SandboxResourceWrite) -> ManagedSandbox:
    resource_id = str(payload.id)
    expected_name = backend_name(
        resource_id,
        owner_type=payload.owner_type,
        owner_id=str(payload.owner_id),
    )
    legacy_name = backend_name(resource_id)
    if payload.backend_resource_name not in {expected_name, legacy_name}:
        raise DomainError("SANDBOX_NAME_INVALID", "Sandbox name is not deterministic", 422)
    return ManagedSandbox(
        id=resource_id,
        kind=payload.kind,
        owner_type=payload.owner_type,
        owner_id=str(payload.owner_id),
        backend="docker",
        backend_resource_name=payload.backend_resource_name,
        image_reference=payload.image_reference,
        runtime_allocation_id=(
            str(payload.spec.runtime_allocation_id)
            if isinstance(payload, RuntimeProviderResourceWrite)
            and payload.spec.runtime_allocation_id is not None
            else None
        ),
        spec_json=payload.spec.model_dump(mode="json"),
        created_at=payload.created_at,
        hard_expires_at=payload.created_at,
    )


def create_app(settings: Settings | None = None) -> FastAPI:
    configured = (settings or Settings()).model_copy(update={"docker_controller_mode": "local"})
    if len(configured.docker_controller_api_key) < 32:
        raise ValueError("DOCKER_CONTROLLER_API_KEY must contain at least 32 characters")
    if len(configured.docker_controller_worker_api_key) < 32:
        raise ValueError("DOCKER_CONTROLLER_WORKER_API_KEY must contain at least 32 characters")
    if configured.docker_controller_worker_api_key == configured.docker_controller_api_key:
        raise ValueError("Docker controller API and Worker keys must be different")
    terminals = _TerminalManager(idle_seconds=configured.docker_controller_terminal_idle_seconds)

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        async def reaper() -> None:
            while True:
                await asyncio.sleep(30)
                terminals.reap()

        task = asyncio.create_task(reaper())
        try:
            yield
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
            terminals.close_all()

    app = FastAPI(title="FlowWeave Runtime Provider", version="1.0.0", lifespan=lifespan)

    @app.middleware("http")
    async def context(
        request: Request, call_next: Callable[[Request], Awaitable[Response]]
    ) -> Response:
        token = bind_settings(configured)
        try:
            role = authorize_controller_request(configured, request.headers.get("Authorization"))
            if request.url.path != "/health" and role is None:
                return JSONResponse(
                    status_code=401,
                    content={
                        "error": {
                            "code": "CONTROLLER_UNAUTHORIZED",
                            "message": "Controller authentication failed",
                            "details": {},
                        }
                    },
                )
            # Docker socket access is fail-closed. Every high-level operation
            # must be assigned explicitly to a principal before it can reach a
            # route handler; a newly added endpoint therefore starts denied.
            allowed_roles_by_path: dict[str, frozenset[str]] = {
                "/v1/sandboxes/ensure": frozenset({"api", "worker"}),
                "/v1/sandboxes/inspect": frozenset({"worker"}),
                "/v1/sandboxes/drain": frozenset({"worker"}),
                "/v1/sandboxes/delete": frozenset({"worker"}),
                "/v1/sandboxes/list": frozenset({"worker"}),
                "/v1/environments/remove-image": frozenset({"worker"}),
                "/v1/environments/resolve-base-image": frozenset({"api"}),
                "/v1/environments/remove-credentials": frozenset({"worker"}),
                # Both principals can encounter pre-ledger setup resources
                # during the bounded migration compatibility period. The
                # handler independently verifies legacy ownership labels.
                "/v1/environments/remove-legacy": frozenset({"api", "worker"}),
                "/v1/environments/publish": frozenset({"api"}),
                "/v1/gates/execute": frozenset({"worker"}),
                "/v1/dependencies/build": frozenset({"worker"}),
                "/v1/plugins/resolve": frozenset({"worker"}),
                "/v1/plugins/resolve-marketplace": frozenset({"worker"}),
                "/v1/plugins/list-marketplace": frozenset({"api"}),
                "/v1/runtimes/events": frozenset({"api"}),
                "/v1/runtimes/validate-plugin": frozenset({"api"}),
                "/v1/terminals/start": frozenset({"api"}),
                "/v1/terminals/read": frozenset({"api"}),
                "/v1/terminals/write": frozenset({"api"}),
                "/v1/terminals/resize": frozenset({"api"}),
                "/v1/terminals/close": frozenset({"api"}),
            }
            if request.url.path != "/health" and role not in allowed_roles_by_path.get(
                request.url.path, frozenset()
            ):
                return JSONResponse(
                    status_code=403,
                    content={
                        "error": {
                            "code": "CONTROLLER_FORBIDDEN",
                            "message": "The controller principal cannot use this operation",
                            "details": {},
                        }
                    },
                )
            request.state.controller_role = role
            content_length = request.headers.get("content-length")
            try:
                declared_too_large = (
                    content_length is not None and int(content_length) > _MAX_REQUEST_BYTES
                )
            except ValueError:
                declared_too_large = True
            body = bytearray()
            if not declared_too_large:
                async for chunk in request.stream():
                    body.extend(chunk)
                    if len(body) > _MAX_REQUEST_BYTES:
                        break
            if declared_too_large or len(body) > _MAX_REQUEST_BYTES:
                return JSONResponse(
                    status_code=413,
                    content={
                        "error": {
                            "code": "CONTROLLER_REQUEST_TOO_LARGE",
                            "message": "Controller request body is too large",
                            "details": {"max_bytes": _MAX_REQUEST_BYTES},
                        }
                    },
                )
            # The decorator middleware uses Starlette's cached request wrapper;
            # restoring the bounded body lets the route parse it without a
            # second socket read.
            request.__dict__["_body"] = bytes(body)
            return await call_next(request)
        finally:
            reset_settings(token)

    @app.exception_handler(DomainError)
    async def domain_error(_request: Request, exc: DomainError) -> JSONResponse:
        return JSONResponse(
            status_code=exc.status,
            content={
                "error": {
                    "code": exc.code,
                    "message": exc.message,
                    "details": exc.details,
                }
            },
        )

    def check_scope(scope: str) -> None:
        if scope != configured.sandbox_manager_scope:
            raise DomainError("CONTROLLER_SCOPE_MISMATCH", "Manager scope is not allowed", 403)

    @app.get("/health")
    async def health() -> dict[str, str]:
        return {"status": "ok"}

    @app.post("/v1/sandboxes/ensure")
    async def ensure(request: Request, payload: SandboxResourceWrite) -> dict[str, Any]:
        check_scope(payload.manager_scope)
        role = cast(str, request.state.controller_role)
        allowed = (role == "api" and payload.kind == "ENVIRONMENT_SETUP") or (
            role == "worker" and payload.kind == "AGENT_RUNTIME"
        )
        if not allowed:
            raise DomainError(
                "CONTROLLER_FORBIDDEN",
                "The controller principal cannot create this sandbox kind",
                403,
            )
        runtime_secret_key = (
            payload.runtime_secret_key.get_secret_value()
            if isinstance(payload, RuntimeProviderResourceWrite)
            and payload.runtime_secret_key is not None
            else None
        )
        observation = DockerSandboxProvider(configured).ensure_running(
            _resource(payload), runtime_secret_key=runtime_secret_key
        )
        return cast(dict[str, Any], _observation_dict(observation))

    @app.post("/v1/sandboxes/inspect")
    async def inspect(payload: SandboxNameWrite) -> dict[str, Any]:
        check_scope(payload.manager_scope)
        if not payload.resource_name.startswith("fw-sbx-"):
            raise DomainError("SANDBOX_NAME_INVALID", "Sandbox name is not allowed", 422)
        observation = DockerSandboxProvider(configured).inspect(payload.resource_name)
        return {"observation": _observation_dict(observation)}

    @app.post("/v1/sandboxes/delete")
    async def delete(payload: SandboxDeleteWrite) -> dict[str, bool]:
        check_scope(payload.manager_scope)
        DockerSandboxProvider(configured).delete_expected(
            payload.resource_name, str(payload.resource_id)
        )
        return {"deleted": True}

    @app.post("/v1/sandboxes/drain")
    async def _drain(payload: SandboxDeleteWrite) -> dict[str, bool]:
        check_scope(payload.manager_scope)
        result = DockerSandboxProvider(configured).drain_expected(
            payload.resource_name, str(payload.resource_id)
        )
        return {"graceful": result.graceful, "stopped": result.stopped}

    @app.post("/v1/sandboxes/list")
    async def list_sandboxes(payload: ScopedRequest) -> dict[str, Any]:
        check_scope(payload.manager_scope)
        return {
            "observations": [
                _observation_dict(item) for item in DockerSandboxProvider(configured).list_managed()
            ]
        }

    @app.post("/v1/environments/remove-image")
    async def remove_image(payload: RemoveImageWrite) -> dict[str, bool]:
        check_scope(payload.manager_scope)
        if not payload.reference.startswith("flowweave/environment-"):
            raise DomainError("ENVIRONMENT_IMAGE_INVALID", "Image tag is not managed", 422)
        environments_docker.remove_image(
            payload.reference,
            expected_digest=payload.expected_digest,
            environment_id=str(payload.environment_id),
            version_id=str(payload.version_id),
            version_no=payload.version_no,
        )
        return {"deleted": True}

    @app.post("/v1/environments/resolve-base-image")
    async def _resolve_base_image(payload: ResolveBaseImageWrite) -> dict[str, str]:
        check_scope(payload.manager_scope)
        reference, digest = environments_docker.resolve_setup_image(payload.reference)
        return {"reference": reference, "digest": digest}

    @app.post("/v1/environments/remove-credentials")
    async def remove_credentials(payload: EnvironmentCredentialsWrite) -> dict[str, bool]:
        check_scope(payload.manager_scope)
        DockerSandboxProvider(configured).delete_environment_credentials(
            str(payload.environment_id)
        )
        return {"deleted": True}

    @app.post("/v1/environments/remove-legacy")
    async def remove_legacy(payload: LegacyRemoveWrite) -> dict[str, bool]:
        check_scope(payload.manager_scope)
        environments_docker.remove_legacy_setup_container(
            payload.resource_name, environment_id=str(payload.environment_id)
        )
        return {"deleted": True}

    @app.post("/v1/environments/publish")
    async def publish(payload: PublishImageWrite) -> dict[str, Any]:
        check_scope(payload.manager_scope)
        container_id = environments_docker.resolve_setup_container(
            payload.resource_name,
            sandbox_id=str(payload.resource_id),
            environment_id=str(payload.environment_id),
        )
        image = environments_docker.publish_container(
            container_id,
            environment_id=str(payload.environment_id),
            version_id=str(payload.version_id),
            version_no=payload.version_no,
            base_image_reference=payload.base_image_reference,
            base_image_digest=payload.base_image_digest,
        )
        return {"reference": image.reference, "digest": image.digest, "manifest": image.manifest}

    @app.post("/v1/gates/execute")
    async def gate(payload: GateExecuteWrite) -> dict[str, Any]:
        check_scope(payload.manager_scope)
        sandbox = DockerSandbox(
            configured.sandbox_image_python,
            configured.sandbox_image_javascript,
            docker_binary=configured.docker_binary,
            manager_scope=configured.sandbox_manager_scope,
            cleanup_grace_seconds=configured.sandbox_orphan_grace_seconds,
            storage_size=configured.sandbox_storage_size,
        )
        result = sandbox.execute(
            cast(Any, payload.language), payload.code, payload.context, payload.timeout_seconds
        )
        return {
            "status": result.status,
            "result": result.result,
            "error": result.error,
            "log": result.log,
        }

    @app.post("/v1/dependencies/build")
    async def dependencies(payload: DependencyBuildWrite) -> dict[str, Any]:
        check_scope(payload.manager_scope)
        builder = DockerDependencyBuilder(
            configured.dependency_builder_image,
            docker_binary=configured.docker_binary,
            manager_scope=configured.sandbox_manager_scope,
            timeout_seconds=configured.dependency_builder_timeout_seconds,
            cleanup_grace_seconds=configured.sandbox_orphan_grace_seconds,
            storage_size=configured.sandbox_storage_size,
        )
        bundle = builder.build(payload.dependencies)
        return {
            "content_base64": base64.b64encode(bundle.content).decode(),
            "manifest": bundle.manifest,
        }

    async def resolve_plugin(payload: PluginResolveWrite) -> dict[str, Any]:
        check_scope(payload.manager_scope)
        resolver = DockerPluginResolver(
            configured.plugin_resolver_image,
            allowed_hosts=configured_plugin_hosts(configured),
            docker_binary=configured.docker_binary,
            manager_scope=configured.sandbox_manager_scope,
            timeout_seconds=configured.plugin_resolver_timeout_seconds,
            cleanup_grace_seconds=configured.sandbox_orphan_grace_seconds,
            storage_size=configured.sandbox_storage_size,
        )
        bundle = resolver.resolve(
            PluginResolveRequest(payload.source, payload.commit, payload.repo_path)
        )
        return {
            "content_base64": base64.b64encode(bundle.content).decode(),
            "resolved_commit": bundle.resolved_commit,
            "report": bundle.report,
        }

    app.add_api_route(
        "/v1/plugins/resolve",
        resolve_plugin,
        methods=["POST"],
    )

    async def resolve_marketplace_plugin(
        payload: MarketplacePluginResolveWrite,
    ) -> dict[str, Any]:
        check_scope(payload.manager_scope)
        resolver = DockerPluginResolver(
            configured.plugin_resolver_image,
            allowed_hosts=configured_plugin_hosts(configured),
            docker_binary=configured.docker_binary,
            manager_scope=configured.sandbox_manager_scope,
            timeout_seconds=configured.plugin_resolver_timeout_seconds,
            cleanup_grace_seconds=configured.sandbox_orphan_grace_seconds,
            storage_size=configured.sandbox_storage_size,
        )
        bundle = resolver.resolve_marketplace_plugin(
            MarketplacePluginResolveRequest(
                payload.source, payload.commit, payload.repo_path, payload.plugin_name
            )
        )
        return {
            "content_base64": base64.b64encode(bundle.content).decode(),
            "resolved_source": bundle.resolved_source,
            "resolved_commit": bundle.resolved_commit,
            "resolved_repo_path": bundle.resolved_repo_path,
            "report": bundle.report,
        }

    app.add_api_route(
        "/v1/plugins/resolve-marketplace",
        resolve_marketplace_plugin,
        methods=["POST"],
    )

    async def list_marketplace(payload: MarketplaceCatalogWrite) -> dict[str, object]:
        check_scope(payload.manager_scope)
        resolver = DockerPluginResolver(
            configured.plugin_resolver_image,
            allowed_hosts=configured_plugin_hosts(configured),
            docker_binary=configured.docker_binary,
            manager_scope=configured.sandbox_manager_scope,
            timeout_seconds=configured.plugin_resolver_timeout_seconds,
            cleanup_grace_seconds=configured.sandbox_orphan_grace_seconds,
            storage_size=configured.sandbox_storage_size,
        )
        return resolver.list_marketplace(
            MarketplaceCatalogRequest(payload.source, payload.commit, payload.repo_path)
        )

    app.add_api_route(
        "/v1/plugins/list-marketplace",
        list_marketplace,
        methods=["POST"],
    )

    @app.post("/v1/runtimes/events")
    async def runtime_events(payload: RuntimeEventsWrite) -> StreamingResponse:
        check_scope(payload.manager_scope)
        try:
            container_id = inspect_owned_container(
                configured.docker_binary,
                payload.resource_name,
                str(payload.resource_id),
                expected_manager_scope=configured.sandbox_manager_scope,
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
        return StreamingResponse(
            _runtime_event_stream(
                configured,
                container_id,
                payload.channel,
                payload.conversation_id,
                payload.timeout_seconds,
            ),
            media_type="application/x-ndjson",
            headers={
                "Cache-Control": "no-store",
                "X-Content-Type-Options": "nosniff",
            },
        )

    @app.post("/v1/runtimes/validate-plugin")
    async def validate_runtime_plugin(payload: RuntimePluginValidationWrite) -> dict[str, Any]:
        check_scope(payload.manager_scope)
        expected_prefix = (
            f"/runtime/capabilities/nodes/plugin-probe-{payload.validation_id}/plugins/"
        )
        if not payload.plugin_path.startswith(expected_prefix):
            raise DomainError(
                "PLUGIN_TARGET_PATH_INVALID",
                "The Plugin path does not belong to this validation",
                422,
            )
        return validate_owned_runtime_plugin(
            configured,
            resource_name=payload.resource_name,
            resource_id=str(payload.resource_id),
            validation_id=str(payload.validation_id),
            plugin_path=payload.plugin_path,
        )

    @app.post("/v1/terminals/start")
    async def terminal_start(payload: TerminalStartWrite) -> dict[str, str]:
        check_scope(payload.manager_scope)
        container_id = environments_docker.resolve_setup_container(
            payload.resource_name,
            sandbox_id=str(payload.resource_id),
            environment_id=str(payload.environment_id),
        )
        return {
            "terminal_id": terminals.start(
                container_id, payload.session_name, payload.rows, payload.columns
            )
        }

    @app.post("/v1/terminals/read")
    async def terminal_read(payload: TerminalIdWrite) -> dict[str, Any]:
        check_scope(payload.manager_scope)
        content, eof = terminals.read(payload.terminal_id)
        return {"content_base64": base64.b64encode(content).decode(), "eof": eof}

    @app.post("/v1/terminals/write")
    async def terminal_write(payload: TerminalWrite) -> dict[str, bool]:
        check_scope(payload.manager_scope)
        try:
            content = base64.b64decode(payload.content_base64, validate=True)
        except ValueError as exc:
            raise DomainError("TERMINAL_INPUT_INVALID", "Terminal input is invalid", 422) from exc
        if len(content) > 65_536:
            raise DomainError("TERMINAL_INPUT_TOO_LARGE", "Terminal input is too large", 422)
        os.write(terminals.get(payload.terminal_id).master, content)
        return {"written": True}

    @app.post("/v1/terminals/resize")
    async def terminal_resize(payload: TerminalResizeWrite) -> dict[str, bool]:
        check_scope(payload.manager_scope)
        terminals.resize(payload.terminal_id, payload.rows, payload.columns)
        return {"resized": True}

    @app.post("/v1/terminals/close")
    async def terminal_close(payload: TerminalIdWrite) -> dict[str, bool]:
        check_scope(payload.manager_scope)
        terminals.close(payload.terminal_id)
        return {"closed": True}

    # FastAPI retains these callables through the registered routes/handlers.
    # Explicitly access them so strict static analysis recognizes that use.
    _registered = (
        context,
        domain_error,
        health,
        ensure,
        inspect,
        delete,
        _drain,
        list_sandboxes,
        remove_image,
        _resolve_base_image,
        remove_credentials,
        remove_legacy,
        publish,
        gate,
        dependencies,
        runtime_events,
        validate_runtime_plugin,
        terminal_start,
        terminal_read,
        terminal_write,
        terminal_resize,
        terminal_close,
    )
    del _registered
    return app


__all__ = ("create_app",)
