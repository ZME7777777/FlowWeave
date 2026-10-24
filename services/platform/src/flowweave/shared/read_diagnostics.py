"""Safe correlation for bounded Runtime reads; never format exception messages."""

from __future__ import annotations

import logging
import re
from contextvars import ContextVar, Token
from uuid import UUID, uuid4

from sqlalchemy.exc import OperationalError
from sqlalchemy.exc import TimeoutError as PoolTimeout

from flowweave.shared.errors import DomainError

logger = logging.getLogger(__name__)
_request_id: ContextVar[str] = ContextVar("runtime_read_request_id", default="none")


def canonical_request_id(value: str | None) -> str:
    # Header values are untrusted and can contain URLs, secrets or log delimiters.
    try:
        return str(UUID(value)) if value else str(uuid4())
    except (ValueError, AttributeError):
        return str(uuid4())


def bind_read_request_id(value: str) -> Token[str]:
    return _request_id.set(value)


def reset_read_request_id(token: Token[str]) -> None:
    _request_id.reset(token)


def failure_code(error: Exception) -> tuple[str, str]:
    if isinstance(error, DomainError):
        code = error.code if re.fullmatch(r"[A-Z][A-Z0-9_]{0,79}", error.code) else "DOMAIN_ERROR"
        return code, "admission" if code.endswith("SATURATED") else "runtime"
    if isinstance(error, PoolTimeout):
        return "DATABASE_POOL_TIMEOUT", "database_pool"
    if isinstance(error, OperationalError):
        state = getattr(error.orig, "sqlstate", None)
        if state == "57014":
            return "DATABASE_STATEMENT_TIMEOUT", "database_statement"
        if state == "55P03":
            return "DATABASE_LOCK_TIMEOUT", "database_lock"
        if state == "40P01":
            return "DATABASE_DEADLOCK", "database_lock"
        return "DATABASE_CONNECTION_ERROR", "database_connection"
    return "UNEXPECTED_READ_FAILURE", "unexpected"


def log_read_failure(error: Exception, *, lane: str, phase: str) -> None:
    if isinstance(error, DomainError) and error.status < 500:
        return
    code, category = failure_code(error)
    logger.warning(
        "Runtime read failed request_id=%s lane=%s phase=%s error_code=%s category=%s",
        _request_id.get(),
        lane,
        phase,
        code,
        category,
    )
