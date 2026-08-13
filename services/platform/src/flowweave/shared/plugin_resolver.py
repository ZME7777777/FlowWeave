from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar, Token

from flowweave.shared.application.plugin_resolver import PluginResolverPort

_current: ContextVar[PluginResolverPort | None] = ContextVar(
    "flowweave_plugin_resolver", default=None
)


def get_plugin_resolver() -> PluginResolverPort:
    resolver = _current.get()
    if resolver is None:
        raise RuntimeError("Plugin resolver is not bound to the current FlowWeave context")
    return resolver


def bind_plugin_resolver(
    resolver: PluginResolverPort,
) -> Token[PluginResolverPort | None]:
    return _current.set(resolver)


def reset_plugin_resolver(token: Token[PluginResolverPort | None]) -> None:
    _current.reset(token)


@contextmanager
def plugin_resolver_context(resolver: PluginResolverPort) -> Iterator[None]:
    token = bind_plugin_resolver(resolver)
    try:
        yield
    finally:
        reset_plugin_resolver(token)
