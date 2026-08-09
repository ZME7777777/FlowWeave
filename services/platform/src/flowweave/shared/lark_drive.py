from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar, Token

from flowweave.shared.application.lark_drive import LarkDrivePort

_current: ContextVar[LarkDrivePort | None] = ContextVar("flowweave_lark_drive", default=None)


def get_lark_drive() -> LarkDrivePort:
    value = _current.get()
    if value is None:
        raise RuntimeError("Lark Drive is not bound to the current FlowWeave context")
    return value


def bind_lark_drive(value: LarkDrivePort) -> Token[LarkDrivePort | None]:
    return _current.set(value)


def reset_lark_drive(token: Token[LarkDrivePort | None]) -> None:
    _current.reset(token)


@contextmanager
def lark_drive_context(value: LarkDrivePort) -> Iterator[None]:
    token = bind_lark_drive(value)
    try:
        yield
    finally:
        reset_lark_drive(token)
