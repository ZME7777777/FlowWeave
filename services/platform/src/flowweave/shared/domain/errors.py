from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


def _empty_details() -> dict[str, Any]:
    return {}


@dataclass(slots=True)
class DomainError(Exception):
    """Business error without framework or transport dependencies."""

    code: str
    message: str
    status: int = 400
    details: dict[str, Any] = field(default_factory=_empty_details)


def not_found(resource: str, identifier: str) -> DomainError:
    return DomainError("RESOURCE_NOT_FOUND", f"{resource} not found", 404, {"id": identifier})


def conflict(message: str, **details: Any) -> DomainError:
    return DomainError("VERSION_CONFLICT", message, 409, details)


def illegal(message: str, **details: Any) -> DomainError:
    return DomainError("ILLEGAL_STATE_TRANSITION", message, 409, details)
