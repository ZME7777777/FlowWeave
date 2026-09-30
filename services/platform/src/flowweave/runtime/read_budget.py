"""Shared deadline for API admission and successive formal OpenHands reads."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Iterator
from contextlib import asynccontextmanager, contextmanager
from contextvars import ContextVar
from time import monotonic

from flowweave.shared.errors import DomainError

_deadline: ContextVar[float | None] = ContextVar("runtime_read_deadline", default=None)


def _expired() -> DomainError:
    return DomainError(
        "RUNTIME_BUSINESS_READ_TIMEOUT",
        "OpenHands formal read exceeded its deadline",
        504,
        {"outcome_unknown": False},
    )


@contextmanager
def formal_read_budget(seconds: float) -> Iterator[None]:
    parent = _deadline.get()
    deadline = monotonic() + seconds
    token = _deadline.set(min(parent, deadline) if parent is not None else deadline)
    try:
        hydration_time_left()
        yield
    finally:
        _deadline.reset(token)


@asynccontextmanager
async def formal_response_budget(seconds: float) -> AsyncIterator[None]:
    """Bound the API response while an uncancellable worker drains separately."""

    with formal_read_budget(seconds):
        try:
            async with asyncio.timeout(hydration_time_left()):
                yield
        except TimeoutError as exc:
            raise _expired() from exc


# Keep the hydration callers on the same deadline, including nested adapters.
hydration_read_budget = formal_read_budget
hydration_response_budget = formal_response_budget


def hydration_time_left() -> float | None:
    deadline = _deadline.get()
    if deadline is None:
        return None
    remaining = deadline - monotonic()
    if remaining <= 0:
        raise _expired()
    return remaining
