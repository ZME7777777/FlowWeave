from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar, Token

from flowweave.shared.application.artifact_store import ArtifactStorePort

_current_store: ContextVar[ArtifactStorePort | None] = ContextVar(
    "flowweave_artifact_store", default=None
)


def get_artifact_store() -> ArtifactStorePort:
    store = _current_store.get()
    if store is None:
        raise RuntimeError("Artifact store is not bound to the current FlowWeave context")
    return store


def bind_artifact_store(store: ArtifactStorePort) -> Token[ArtifactStorePort | None]:
    return _current_store.set(store)


def reset_artifact_store(token: Token[ArtifactStorePort | None]) -> None:
    _current_store.reset(token)


@contextmanager
def artifact_store_context(store: ArtifactStorePort) -> Iterator[None]:
    token = bind_artifact_store(store)
    try:
        yield
    finally:
        reset_artifact_store(token)
