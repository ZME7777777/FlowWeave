from __future__ import annotations

from flowweave.runtime.base import RuntimeHandle, RuntimePort
from flowweave.runtime.dependencies import get_runtime
from flowweave.runtime.mock import MockRuntime
from flowweave.runtime.openhands import OpenHandsRuntime
from flowweave.shared.errors import DomainError
from flowweave.shared.settings import get_settings


def infer_runtime_adapter(adapter: str | None, handle: RuntimeHandle) -> str:
    if adapter:
        return adapter
    if handle.job_id.startswith("mock-") or handle.conversation_id.startswith("mock-"):
        return "mock"
    return "openhands"


def runtime_for(adapter: str | None, handle: RuntimeHandle) -> RuntimePort:
    resolved = infer_runtime_adapter(adapter, handle)
    settings = get_settings()
    if resolved == settings.runtime_adapter:
        return get_runtime()
    if resolved == "mock":
        # Mock jobs are process-local and never own an external process. After a
        # restart or adapter switch there is therefore nothing external to stop.
        return MockRuntime()
    if resolved == "openhands":
        return OpenHandsRuntime(settings)
    raise DomainError(
        "RUNTIME_ADAPTER_UNAVAILABLE",
        "The runtime adapter that created this execution is unavailable",
        503,
        {"runtime_adapter": resolved},
    )
