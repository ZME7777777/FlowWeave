from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar, Token

from flowweave.runtime.base import RuntimePort

_current_runtime: ContextVar[RuntimePort | None] = ContextVar(
    "flowweave_current_runtime", default=None
)


def get_runtime() -> RuntimePort:
    runtime = _current_runtime.get()
    if runtime is None:
        raise RuntimeError("Runtime is not bound to the current FlowWeave context")
    return runtime


def bind_runtime(runtime: RuntimePort) -> Token[RuntimePort | None]:
    return _current_runtime.set(runtime)


def reset_runtime(token: Token[RuntimePort | None]) -> None:
    _current_runtime.reset(token)


@contextmanager
def runtime_context(runtime: RuntimePort) -> Iterator[None]:
    token = bind_runtime(runtime)
    try:
        yield
    finally:
        reset_runtime(token)
