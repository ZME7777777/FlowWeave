from __future__ import annotations

import asyncio
import threading
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from typing import Literal

import httpx

from flowweave.bootstrap.settings import Settings
from flowweave.modules.agent_sessions.application.conversation_cache import (
    ConversationHydrationCache,
)
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
from flowweave.shared.infrastructure.http_transport import (
    HttpTransportPool,
    register_http_transport,
    unregister_http_transport,
)
from flowweave.shared.infrastructure.plugin_resolver import build_plugin_resolver
from flowweave.shared.infrastructure.sandbox import build_sandbox
from flowweave.shared.observability import Metrics, RateLimiter


@dataclass(slots=True)
class Container:
    settings: Settings
    role: Literal["api", "worker"]
    database: Database
    http: httpx.AsyncClient
    http_transport: HttpTransportPool
    metrics: Metrics
    rate_limiter: RateLimiter
    conversation_hydration_cache: ConversationHydrationCache
    runtime: RuntimePort
    artifact_store: ArtifactStorePort
    dependency_builder: DependencyBuilderPort
    plugin_resolver: PluginResolverPort
    sandbox: SandboxPort
    run_event_listener: RunEventListener
    audit_writer: AuditWriter
    blocking_executor: ThreadPoolExecutor
    blocking_io_slots: asyncio.Semaphore
    blocking_mutation_slots: asyncio.Semaphore
    message_executor: ThreadPoolExecutor
    message_io_slots: asyncio.Semaphore
    message_capacity: int
    hydration_executor: ThreadPoolExecutor
    hydration_io_slots: asyncio.Semaphore
    hydration_capacity: int
    blocking_capacity: int
    workspace_executor: ThreadPoolExecutor
    workspace_io_slots: asyncio.Semaphore
    workspace_capacity: int
    lifecycle_executor: ThreadPoolExecutor
    lifecycle_io_slots: asyncio.Semaphore
    lifecycle_capacity: int
    auxiliary_executor: ThreadPoolExecutor
    auxiliary_io_slots: asyncio.Semaphore
    admin_executor: ThreadPoolExecutor
    admin_io_slots: asyncio.Semaphore
    poll_executor: ThreadPoolExecutor
    poll_io_slots: asyncio.Semaphore
    history_read_executor: ThreadPoolExecutor
    history_read_slots: asyncio.Semaphore
    terminal_stream_executor: ThreadPoolExecutor
    terminal_control_executor: ThreadPoolExecutor
    terminal_slots: asyncio.Semaphore
    blocking_control_executor: ThreadPoolExecutor
    blocking_control_slots: asyncio.Semaphore
    heartbeat_slots: threading.BoundedSemaphore

    async def close(self) -> None:
        await self.run_event_listener.close()
        await self.audit_writer.close()
        await self.rate_limiter.close()
        await self.conversation_hydration_cache.close()
        await self.http_transport.aclose()
        unregister_http_transport(self.settings, self.http_transport)
        await asyncio.to_thread(
            self.blocking_executor.shutdown,
            wait=True,
            cancel_futures=True,
        )
        if self.hydration_executor is not self.blocking_executor:
            await asyncio.to_thread(
                self.hydration_executor.shutdown,
                wait=True,
                cancel_futures=True,
            )
        if self.message_executor is not self.blocking_executor:
            await asyncio.to_thread(
                self.message_executor.shutdown,
                wait=True,
                cancel_futures=True,
            )
        if self.lifecycle_executor is not self.blocking_executor:
            await asyncio.to_thread(
                self.lifecycle_executor.shutdown, wait=True, cancel_futures=True
            )
        if self.workspace_executor is not self.history_read_executor:
            await asyncio.to_thread(
                self.workspace_executor.shutdown, wait=True, cancel_futures=True
            )
        await asyncio.to_thread(
            self.auxiliary_executor.shutdown,
            wait=True,
            cancel_futures=True,
        )
        await asyncio.to_thread(
            self.admin_executor.shutdown,
            wait=True,
            cancel_futures=True,
        )
        await asyncio.to_thread(
            self.poll_executor.shutdown,
            wait=True,
            cancel_futures=True,
        )
        await asyncio.to_thread(
            self.history_read_executor.shutdown,
            wait=True,
            cancel_futures=True,
        )
        await asyncio.to_thread(
            self.terminal_stream_executor.shutdown,
            wait=True,
            cancel_futures=True,
        )
        await asyncio.to_thread(
            self.terminal_control_executor.shutdown,
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
    http_transport = HttpTransportPool.build()
    register_http_transport(settings, http_transport)
    if settings.runtime_adapter == "openhands":
        runtime: RuntimePort = OpenHandsRuntime(settings, http_transport=http_transport)
    elif settings.runtime_adapter == "mock":
        runtime = MockRuntime()
    else:
        raise ValueError(f"Unsupported runtime adapter: {settings.runtime_adapter}")
    # Reserve two of the existing API blocking connections for first-screen
    # reads. One-slot stream API configurations retain their existing lane.
    hydration_capacity = min(2, settings.blocking_pool_size - 1) if role == "api" else 0
    # Keep one of the remaining API slots for message delivery whenever the
    # configured capacity can still leave an ordinary interactive slot.
    message_capacity = (
        1 if role == "api" and settings.blocking_pool_size - hydration_capacity >= 2 else 0
    )
    # Workspace/Git operations must not queue behind native history HTTP reads.
    # Reserve from the existing budget only when an ordinary slot remains;
    # small/stream configurations retain their bounded history fallback.
    workspace_capacity = (
        1
        if role == "api"
        and settings.blocking_pool_size - hydration_capacity - message_capacity >= 2
        else 0
    )
    blocking_capacity = (
        settings.blocking_pool_size - hydration_capacity - message_capacity - workspace_capacity
    )
    # Slow native lifecycle operations must not hold the interactive write
    # admission slot (e.g. confirmation) or formal-event SQL connections.
    # Carve out one existing slot only if an ordinary slot remains.
    lifecycle_capacity = 1 if role == "api" and blocking_capacity >= 2 else 0
    blocking_capacity -= lifecycle_capacity
    database = Database(
        settings,
        poll_pool_size=settings.runtime_poll_worker_concurrency if role == "worker" else 0,
        auxiliary_pool_size=(settings.auxiliary_task_worker_concurrency if role == "worker" else 0),
        admin_pool_size=1 if role == "api" else 0,
        hydration_pool_size=hydration_capacity,
        message_pool_size=message_capacity,
        workspace_pool_size=workspace_capacity,
        lifecycle_pool_size=lifecycle_capacity,
    )
    metrics = Metrics()
    blocking_workers = settings.worker_concurrency if role == "worker" else blocking_capacity
    blocking_executor = ThreadPoolExecutor(
        max_workers=blocking_workers,
        thread_name_prefix=f"flowweave-{role}-blocking",
    )
    hydration_executor = (
        ThreadPoolExecutor(
            max_workers=hydration_capacity,
            thread_name_prefix="flowweave-api-hydration",
        )
        if hydration_capacity
        else blocking_executor
    )
    message_executor = (
        ThreadPoolExecutor(max_workers=message_capacity, thread_name_prefix="flowweave-api-message")
        if message_capacity
        else blocking_executor
    )
    lifecycle_executor = (
        ThreadPoolExecutor(
            max_workers=lifecycle_capacity, thread_name_prefix="flowweave-api-lifecycle"
        )
        if lifecycle_capacity
        else blocking_executor
    )
    auxiliary_executor = ThreadPoolExecutor(
        max_workers=settings.auxiliary_task_worker_concurrency,
        thread_name_prefix=f"flowweave-{role}-auxiliary",
    )
    admin_executor = ThreadPoolExecutor(
        max_workers=1,
        thread_name_prefix=f"flowweave-{role}-admin",
    )
    poll_executor = ThreadPoolExecutor(
        max_workers=settings.runtime_poll_worker_concurrency,
        thread_name_prefix=f"flowweave-{role}-runtime-poll",
    )
    history_read_executor = ThreadPoolExecutor(
        max_workers=settings.history_read_pool_size,
        thread_name_prefix=f"flowweave-{role}-history-read",
    )
    workspace_executor = (
        ThreadPoolExecutor(
            max_workers=workspace_capacity, thread_name_prefix="flowweave-api-workspace"
        )
        if workspace_capacity
        else history_read_executor
    )
    terminal_stream_executor = ThreadPoolExecutor(
        max_workers=settings.terminal_stream_pool_size,
        thread_name_prefix=f"flowweave-{role}-terminal-stream",
    )
    terminal_control_executor = ThreadPoolExecutor(
        max_workers=settings.terminal_stream_pool_size,
        thread_name_prefix=f"flowweave-{role}-terminal-control",
    )
    blocking_control_executor = ThreadPoolExecutor(
        max_workers=1,
        thread_name_prefix=f"flowweave-{role}-runtime-control",
    )
    history_slots = asyncio.Semaphore(settings.history_read_pool_size)
    blocking_slots = asyncio.Semaphore(blocking_workers)
    return Container(
        settings=settings,
        role=role,
        database=database,
        http=http_transport.async_regular,
        http_transport=http_transport,
        metrics=metrics,
        rate_limiter=RateLimiter(settings, metrics),
        conversation_hydration_cache=ConversationHydrationCache(),
        runtime=runtime,
        artifact_store=build_artifact_store(settings),
        dependency_builder=build_dependency_builder(settings),
        plugin_resolver=build_plugin_resolver(settings),
        sandbox=build_sandbox(settings),
        run_event_listener=RunEventListener(
            settings.database_url,
            max_subscribers=settings.sse_max_subscribers,
            subscriber_queue_size=settings.sse_subscriber_queue_size,
        ),
        audit_writer=AuditWriter(database.sessions),
        blocking_executor=blocking_executor,
        blocking_io_slots=blocking_slots,
        # Writes share the existing executor and DB pool, but cannot occupy
        # every slot needed to hydrate an unrelated conversation.
        blocking_mutation_slots=asyncio.Semaphore(min(2, max(1, blocking_capacity // 2))),
        message_executor=message_executor,
        message_io_slots=asyncio.Semaphore(message_capacity or blocking_workers),
        message_capacity=message_capacity,
        hydration_executor=hydration_executor,
        hydration_io_slots=asyncio.Semaphore(hydration_capacity or blocking_workers),
        hydration_capacity=hydration_capacity,
        blocking_capacity=blocking_capacity,
        workspace_executor=workspace_executor,
        workspace_io_slots=asyncio.Semaphore(workspace_capacity)
        if workspace_capacity
        else history_slots,
        workspace_capacity=workspace_capacity,
        lifecycle_executor=lifecycle_executor,
        lifecycle_io_slots=asyncio.Semaphore(lifecycle_capacity)
        if lifecycle_capacity
        else blocking_slots,
        lifecycle_capacity=lifecycle_capacity,
        auxiliary_executor=auxiliary_executor,
        auxiliary_io_slots=asyncio.Semaphore(settings.auxiliary_task_worker_concurrency),
        admin_executor=admin_executor,
        admin_io_slots=asyncio.Semaphore(1),
        poll_executor=poll_executor,
        poll_io_slots=asyncio.Semaphore(settings.runtime_poll_worker_concurrency),
        history_read_executor=history_read_executor,
        history_read_slots=history_slots,
        terminal_stream_executor=terminal_stream_executor,
        terminal_control_executor=terminal_control_executor,
        terminal_slots=asyncio.Semaphore(settings.terminal_stream_pool_size),
        blocking_control_executor=blocking_control_executor,
        blocking_control_slots=asyncio.Semaphore(1),
        heartbeat_slots=threading.BoundedSemaphore(settings.task_heartbeat_concurrency),
    )
