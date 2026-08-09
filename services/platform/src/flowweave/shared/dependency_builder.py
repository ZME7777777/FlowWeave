from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar, Token

from flowweave.shared.application.dependency_builder import DependencyBuilderPort

_current: ContextVar[DependencyBuilderPort | None] = ContextVar(
    "flowweave_dependency_builder", default=None
)


def get_dependency_builder() -> DependencyBuilderPort:
    builder = _current.get()
    if builder is None:
        raise RuntimeError("Dependency builder is not bound to this FlowWeave context")
    return builder


def bind_dependency_builder(
    builder: DependencyBuilderPort,
) -> Token[DependencyBuilderPort | None]:
    return _current.set(builder)


def reset_dependency_builder(token: Token[DependencyBuilderPort | None]) -> None:
    _current.reset(token)


@contextmanager
def dependency_builder_context(builder: DependencyBuilderPort) -> Iterator[None]:
    token = bind_dependency_builder(builder)
    try:
        yield
    finally:
        reset_dependency_builder(token)
