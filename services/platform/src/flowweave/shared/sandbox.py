from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar, Token

from flowweave.shared.application.sandbox import SandboxPort

_current_sandbox: ContextVar[SandboxPort | None] = ContextVar("flowweave_sandbox", default=None)


def get_sandbox() -> SandboxPort:
    sandbox = _current_sandbox.get()
    if sandbox is None:
        raise RuntimeError("Sandbox is not bound to the current FlowWeave context")
    return sandbox


def bind_sandbox(sandbox: SandboxPort) -> Token[SandboxPort | None]:
    return _current_sandbox.set(sandbox)


def reset_sandbox(token: Token[SandboxPort | None]) -> None:
    _current_sandbox.reset(token)


@contextmanager
def sandbox_context(sandbox: SandboxPort) -> Iterator[None]:
    token = bind_sandbox(sandbox)
    try:
        yield
    finally:
        reset_sandbox(token)
