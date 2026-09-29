"""Request-scoped deadline for first-screen OpenHands reads."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Iterator
from contextlib import asynccontextmanager, contextmanager
from contextvars import ContextVar
from time import monotonic

from flowweave.shared.errors import DomainError

_deadline: ContextVar[float | None] = ContextVar("hydration_read_deadline", default=None)


def _expired() -> DomainError:
    return DomainError(
        "RUNTIME_BUSINESS_READ_TIMEOUT",
        "OpenHands hydration exceeded its deadline",
        504,
        {"outcome_unknown": False},
    )


@contextmanager
def hydration_read_budget(seconds: float) -> Iterator[None]:
    token = _deadline.set(monotonic() + seconds)
    try:
        yield
    finally:
        _deadline.reset(token)


@asynccontextmanager
async def hydration_response_budget(seconds: float) -> AsyncIterator[None]:
    """Bound the API response while an uncancellable worker drains separately."""

    with hydration_read_budget(seconds):
        try:
            async with asyncio.timeout(seconds):
                yield
        except TimeoutError as exc:
            raise _expired() from exc


def hydration_time_left() -> float | None:
    deadline = _deadline.get()
    if deadline is None:
        return None
    remaining = deadline - monotonic()
    if remaining <= 0:
        raise _expired()
    return remaining
