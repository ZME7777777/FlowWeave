from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol


@dataclass(frozen=True, slots=True)
class DependencyBundle:
    content: bytes
    manifest: dict[str, object]


class DependencyBuilderPort(Protocol):
    """Builds inert dependency files from an already validated lock manifest."""

    def build(self, dependencies: dict[str, dict[str, str]]) -> DependencyBundle: ...
