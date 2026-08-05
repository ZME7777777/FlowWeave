from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar, Token

from flowweave.bootstrap.settings import Settings

_current_settings: ContextVar[Settings | None] = ContextVar(
    "flowweave_current_settings", default=None
)


def get_settings() -> Settings:
    """Return settings explicitly bound by the process/request compatibility boundary.

    New code receives settings through ``Container``. This accessor exists only for
    synchronous modules being removed during the greenfield migration and deliberately
    has no environment-reading fallback.
    """

    settings = _current_settings.get()
    if settings is None:
        raise RuntimeError("Settings are not bound to the current FlowWeave context")
    return settings


def bind_settings(settings: Settings) -> Token[Settings | None]:
    return _current_settings.set(settings)


def reset_settings(token: Token[Settings | None]) -> None:
    _current_settings.reset(token)


@contextmanager
def settings_context(settings: Settings) -> Iterator[None]:
    token = bind_settings(settings)
    try:
        yield
    finally:
        reset_settings(token)
