from __future__ import annotations

import logging

from flowweave.runtime.base import RuntimeHandle, RuntimePort, RuntimeResult
from flowweave.shared.errors import DomainError
from flowweave.shared.observability import current_metrics

logger = logging.getLogger(__name__)

_RETRYABLE_COMPLETION_ERRORS = frozenset(
    {
        "RUNTIME_READ_PER_RUNTIME_SATURATED",
        "RUNTIME_BUSINESS_READ_TIMEOUT",
        "EXECUTOR_UNAVAILABLE",
    }
)


def read_activity_completion(runtime: RuntimePort, handle: RuntimeHandle) -> RuntimeResult | None:
    """Defer a transient completion read without discarding the native catalog.

    The caller retains its pending flag until a formal terminal result arrives.
    Authorization, missing-conversation and protocol failures still propagate.
    """

    try:
        return runtime.read_active_events(handle).result
    except DomainError as exc:
        if exc.status not in {503, 504} or exc.code not in _RETRYABLE_COMPLETION_ERRORS:
            raise
        if metrics := current_metrics():
            metrics.increment("flowweave_activity_completion_deferred_total", code=exc.code)
        logger.warning("activity completion deferred code=%s", exc.code)
        return None
