"""Discovery and lifecycle management for temporary listening processes."""

from __future__ import annotations

from collections.abc import Iterable
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from flowweave.bootstrap.settings import Settings
from flowweave.modules.agent_sessions.infrastructure.models import (
    AgentConversationBinding,
    AgentTemporaryService,
)
from flowweave.modules.agent_workspaces.infrastructure.models import AgentWorkspaceRuntime
from flowweave.modules.sandboxes.infrastructure.docker import DockerSandboxProvider
from flowweave.modules.sandboxes.infrastructure.models import ManagedSandbox
from flowweave.shared.errors import DomainError

_DEFAULT_TTL_SECONDS = 3_600
_SESSION_IDLE_CLEANUP_SECONDS = 86_400


def _active_sandbox(db: Session, workspace_id: str) -> tuple[ManagedSandbox, int]:
    runtime = db.scalar(
        select(AgentWorkspaceRuntime).where(AgentWorkspaceRuntime.workspace_id == workspace_id)
    )
    if runtime is None or runtime.status != "ACTIVE" or runtime.active_generation is None:
        raise DomainError("AGENT_RUNTIME_RECOVERING", "Agent 运行环境正在恢复", 503)
    sandbox = db.scalar(
        select(ManagedSandbox).where(
            ManagedSandbox.owner_type == "AGENT_WORKSPACE",
            ManagedSandbox.owner_id == workspace_id,
            ManagedSandbox.generation == runtime.active_generation,
            ManagedSandbox.desired_state == "RUNNING",
            ManagedSandbox.observed_state == "RUNNING",
        )
    )
    if sandbox is None or not sandbox.backend_resource_id:
        raise DomainError("AGENT_RUNTIME_RECOVERING", "Agent 运行环境正在恢复", 503)
    return sandbox, runtime.active_generation


def _service_dict(item: AgentTemporaryService) -> dict[str, Any]:
    return {
        "id": item.id,
        "source": item.source,
        "state": item.state,
        "pid": item.pid,
        "command": item.command,
        "working_directory": item.working_directory,
        "ports": item.ports_json,
        "url": item.url,
        "expires_at": item.expires_at.isoformat() if item.expires_at else None,
        "last_seen_at": item.last_seen_at.isoformat(),
        "binding_id": item.binding_id,
    }


def _binding_for_directory(
    bindings: Iterable[AgentConversationBinding], working_directory: str | None
) -> str | None:
    if not working_directory:
        return None
    matches = [
        binding.id
        for binding in bindings
        if binding.working_directory
        and (
            working_directory == binding.working_directory
            or working_directory.startswith(f"{binding.working_directory}/")
        )
    ]
    return matches[0] if len(matches) == 1 else None


def discover_workspace_services(
    db: Session,
    *,
    workspace_id: str,
    settings: Settings,
) -> list[AgentTemporaryService]:
    """Synchronize listener processes from the active owned Runtime container."""

    sandbox, generation = _active_sandbox(db, workspace_id)
    observed = DockerSandboxProvider(settings).list_listening_processes(
        sandbox.backend_resource_name, sandbox.id
    )
    bindings = list(
        db.scalars(
            select(AgentConversationBinding).where(
                AgentConversationBinding.workspace_id == workspace_id,
                AgentConversationBinding.lifecycle == "ACTIVE",
            )
        )
    )
    now = datetime.now(UTC)
    seen: set[tuple[int, str]] = set()
    for process in observed:
        pid = int(process["pid"])
        started = str(process["started_ticks"])
        seen.add((pid, started))
        item = db.scalar(
            select(AgentTemporaryService).where(
                AgentTemporaryService.sandbox_id == sandbox.id,
                AgentTemporaryService.runtime_generation == generation,
                AgentTemporaryService.pid == pid,
                AgentTemporaryService.process_started_ticks == started,
            )
        )
        ports = [int(port) for port in process["ports"]]
        working_directory = process.get("working_directory")
        if item is None:
            item = AgentTemporaryService(
                workspace_id=workspace_id,
                binding_id=_binding_for_directory(bindings, working_directory),
                sandbox_id=sandbox.id,
                runtime_generation=generation,
                source="DISCOVERED",
                pid=pid,
                process_started_ticks=started,
                process_group_id=process.get("process_group_id"),
                command=str(process["command"]),
                working_directory=working_directory,
                ports_json=ports,
                url=(f"http://127.0.0.1:{ports[0]}/" if ports else None),
                expires_at=now + timedelta(seconds=_DEFAULT_TTL_SECONDS),
                last_seen_at=now,
            )
            db.add(item)
        else:
            item.state = "RUNNING"
            item.command = str(process["command"])
            item.working_directory = working_directory
            item.process_group_id = process.get("process_group_id")
            item.ports_json = ports
            item.url = f"http://127.0.0.1:{ports[0]}/" if ports else None
            item.last_seen_at = now
    active = list(
        db.scalars(
            select(AgentTemporaryService).where(
                AgentTemporaryService.sandbox_id == sandbox.id,
                AgentTemporaryService.runtime_generation == generation,
                AgentTemporaryService.state.in_(["RUNNING", "STOPPING"]),
            )
        )
    )
    for item in active:
        if (item.pid, item.process_started_ticks) not in seen:
            item.state = "MISSING"
            item.stopped_at = now
            item.stop_reason = "PROCESS_EXITED"
    db.flush()
    return list(
        db.scalars(
            select(AgentTemporaryService)
            .where(
                AgentTemporaryService.workspace_id == workspace_id,
                AgentTemporaryService.sandbox_id == sandbox.id,
                AgentTemporaryService.runtime_generation == generation,
                AgentTemporaryService.state == "RUNNING",
            )
            .order_by(AgentTemporaryService.created_at)
        )
    )


def workspace_services(
    db: Session, *, workspace_id: str, binding_id: str | None, settings: Settings
) -> list[dict[str, Any]]:
    del binding_id
    try:
        services = discover_workspace_services(db, workspace_id=workspace_id, settings=settings)
    except DomainError as exc:
        if exc.status in {409, 503}:
            return []
        raise
    return [_service_dict(item) for item in services]


def stop_service(
    db: Session,
    *,
    workspace_id: str,
    service_id: str,
    reason: str,
    settings: Settings,
) -> None:
    item = db.scalar(
        select(AgentTemporaryService)
        .where(
            AgentTemporaryService.id == service_id,
            AgentTemporaryService.workspace_id == workspace_id,
        )
        .with_for_update()
    )
    if item is None:
        raise DomainError("AGENT_TEMPORARY_SERVICE_NOT_FOUND", "临时服务不存在", 404)
    if item.state not in {"RUNNING", "STOPPING"}:
        return
    sandbox, generation = _active_sandbox(db, workspace_id)
    if item.sandbox_id != sandbox.id or item.runtime_generation != generation:
        item.state = "MISSING"
        item.stopped_at = datetime.now(UTC)
        item.stop_reason = "RUNTIME_REPLACED"
        return
    stopped = DockerSandboxProvider(settings).stop_listening_process(
        sandbox.backend_resource_name,
        sandbox.id,
        pid=item.pid,
        started_ticks=item.process_started_ticks,
        process_group_id=item.process_group_id,
    )
    now = datetime.now(UTC)
    item.state = "STOPPED" if stopped else "MISSING"
    item.stopped_at = now
    item.stop_reason = reason
    item.last_seen_at = now


def cleanup_expired_services(db: Session, *, settings: Settings) -> int:
    """Stop expired listeners and listeners attributable to 24-hour-idle conversations."""

    now = datetime.now(UTC)
    items = list(
        db.scalars(
            select(AgentTemporaryService).where(AgentTemporaryService.state == "RUNNING")
        )
    )
    cleaned = 0
    bindings = {
        binding.id: binding
        for binding in db.scalars(
            select(AgentConversationBinding).where(AgentConversationBinding.id.in_(
                [item.binding_id for item in items if item.binding_id]
            ))
        )
    }
    for item in items:
        idle = (
            item.binding_id is not None
            and (binding := bindings.get(item.binding_id)) is not None
            and (binding.last_message_at or binding.created_at)
            <= now - timedelta(seconds=_SESSION_IDLE_CLEANUP_SECONDS)
        )
        expired = item.expires_at is not None and item.expires_at <= now
        if not (idle or expired):
            continue
        try:
            stop_service(
                db,
                workspace_id=item.workspace_id,
                service_id=item.id,
                reason="SESSION_IDLE" if idle else "TTL_EXPIRED",
                settings=settings,
            )
            cleaned += 1
        except DomainError:
            item.state = "MISSING"
            item.stopped_at = now
            item.stop_reason = "RUNTIME_UNAVAILABLE"
    db.flush()
    return cleaned


def stop_binding_services(
    db: Session, *, workspace_id: str, binding_id: str, reason: str, settings: Settings
) -> int:
    services = list(
        db.scalars(
            select(AgentTemporaryService).where(
                AgentTemporaryService.workspace_id == workspace_id,
                AgentTemporaryService.binding_id == binding_id,
                AgentTemporaryService.state == "RUNNING",
            )
        )
    )
    for item in services:
        try:
            stop_service(
                db,
                workspace_id=workspace_id,
                service_id=item.id,
                reason=reason,
                settings=settings,
            )
        except DomainError:
            item.state = "MISSING"
            item.stopped_at = datetime.now(UTC)
            item.stop_reason = "RUNTIME_UNAVAILABLE"
    db.flush()
    return len(services)


__all__ = (
    "cleanup_expired_services",
    "discover_workspace_services",
    "stop_binding_services",
    "stop_service",
    "workspace_services",
)
