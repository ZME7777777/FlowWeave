from __future__ import annotations

import asyncio
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from typing import Literal

import httpx

from flowweave.bootstrap.settings import Settings
from flowweave.modules.runs.infrastructure.event_listener import RunEventListener
from flowweave.modules.users.application.audit import AuditWriter
from flowweave.runtime.base import RuntimePort
from flowweave.runtime.mock import MockRuntime
from flowweave.runtime.openhands import OpenHandsRuntime
from flowweave.shared.application.artifact_store import ArtifactStorePort
from flowweave.shared.application.dependency_builder import DependencyBuilderPort
from flowweave.shared.application.plugin_resolver import PluginResolverPort
from flowweave.shared.application.sandbox import SandboxPort
from flowweave.shared.infrastructure.artifact_store import build_artifact_store
from flowweave.shared.infrastructure.database import Database
from flowweave.shared.infrastructure.dependency_builder import build_dependency_builder
from flowweave.shared.infrastructure.plugin_resolver import build_plugin_resolver
from flowweave.shared.infrastructure.sandbox import build_sandbox


@dataclass(slots=True)
class Container:
    settings: Settings
    role: Literal["api", "worker"]
    database: Database
    http: httpx.AsyncClient
    runtime: RuntimePort
    artifact_store: ArtifactStorePort
    dependency_builder: DependencyBuilderPort
    plugin_resolver: PluginResolverPort
    sandbox: SandboxPort
    run_event_listener: RunEventListener
    audit_writer: AuditWriter
    blocking_executor: ThreadPoolExecutor
    blocking_io_slots: asyncio.Semaphore
    blocking_control_executor: ThreadPoolExecutor
    blocking_control_slots: asyncio.Semaphore

    async def close(self) -> None:
        await self.audit_writer.close()
        await self.http.aclose()
        await asyncio.to_thread(
            self.blocking_executor.shutdown,
            wait=True,
            cancel_futures=True,
        )
        await asyncio.to_thread(
            self.blocking_control_executor.shutdown,
            wait=True,
            cancel_futures=True,
        )
        await self.database.dispose()


def build_container(settings: Settings, *, role: Literal["api", "worker"]) -> Container:
    timeout = httpx.Timeout(connect=5, read=30, write=30, pool=5)
    if settings.runtime_adapter == "openhands":
        runtime: RuntimePort = OpenHandsRuntime(settings)
    elif settings.runtime_adapter == "mock":
        runtime = MockRuntime()
    else:
        raise ValueError(f"Unsupported runtime adapter: {settings.runtime_adapter}")
    database = Database(settings)
    blocking_executor = ThreadPoolExecutor(
        max_workers=settings.blocking_pool_size,
        thread_name_prefix=f"flowweave-{role}-blocking",
    )
    blocking_control_executor = ThreadPoolExecutor(
        max_workers=1,
        thread_name_prefix=f"flowweave-{role}-runtime-control",
    )
    return Container(
        settings=settings,
        role=role,
        database=database,
        http=httpx.AsyncClient(timeout=timeout, follow_redirects=False),
        runtime=runtime,
        artifact_store=build_artifact_store(settings),
        dependency_builder=build_dependency_builder(settings),
        plugin_resolver=build_plugin_resolver(settings),
        sandbox=build_sandbox(settings),
        run_event_listener=RunEventListener(settings.database_url),
        audit_writer=AuditWriter(database.sessions),
        blocking_executor=blocking_executor,
        blocking_io_slots=asyncio.Semaphore(settings.blocking_pool_size),
        blocking_control_executor=blocking_control_executor,
        blocking_control_slots=asyncio.Semaphore(1),
    )
