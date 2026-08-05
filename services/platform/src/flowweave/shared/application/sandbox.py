from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Literal, Protocol

SandboxLanguage = Literal["PYTHON", "JAVASCRIPT"]
SandboxStatus = Literal["COMPLETED", "ERROR", "TIMEOUT"]


@dataclass(frozen=True, slots=True)
class SandboxExecution:
    status: SandboxStatus
    result: object | None = None
    error: str | None = None
    log: str = ""


class SandboxPort(Protocol):
    """Executes untrusted gate code without exposing application credentials."""

    def execute(
        self,
        language: SandboxLanguage,
        code: str,
        context: dict[str, Any],
        timeout_seconds: int,
    ) -> SandboxExecution: ...
