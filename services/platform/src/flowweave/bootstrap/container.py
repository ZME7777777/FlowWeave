from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

import httpx

from flowweave.bootstrap.settings import Settings
from flowweave.modules.runs.infrastructure.event_listener import RunEventListener
from flowweave.runtime.base import RuntimePort
from flowweave.runtime.mock import MockRuntime
from flowweave.runtime.openhands import OpenHandsRuntime
from flowweave.shared.application.artifact_store import ArtifactStorePort
from flowweave.shared.application.sandbox import SandboxPort
from flowweave.shared.infrastructure.artifact_store import build_artifact_store
from flowweave.shared.infrastructure.database import Database
from flowweave.shared.infrastructure.sandbox import build_sandbox


@dataclass(slots=True)
class Container:
    settings: Settings
    role: Literal["api", "worker"]
    database: Database
    http: httpx.AsyncClient
    runtime: RuntimePort
    artifact_store: ArtifactStorePort
    sandbox: SandboxPort
    run_event_listener: RunEventListener

    async def close(self) -> None:
        await self.http.aclose()
        await self.database.dispose()


def build_container(settings: Settings, *, role: Literal["api", "worker"]) -> Container:
    timeout = httpx.Timeout(connect=5, read=30, write=30, pool=5)
    if settings.runtime_adapter == "openhands":
        runtime: RuntimePort = OpenHandsRuntime(settings)
    elif settings.runtime_adapter == "mock":
        runtime = MockRuntime()
    else:
        raise ValueError(f"Unsupported runtime adapter: {settings.runtime_adapter}")
    return Container(
        settings=settings,
        role=role,
        database=Database(settings),
        http=httpx.AsyncClient(timeout=timeout, follow_redirects=False),
        runtime=runtime,
        artifact_store=build_artifact_store(settings),
        sandbox=build_sandbox(settings),
        run_event_listener=RunEventListener(settings.database_url),
    )
