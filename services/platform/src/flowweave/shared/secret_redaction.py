"""Redact credentials at FlowWeave Runtime and observability boundaries."""

from __future__ import annotations

import logging
import re
from collections.abc import Mapping

_REDACTED = "[redacted]"
_SENSITIVE_KEY_NAMES = frozenset(
    {
        "api_key",
        "apikey",
        "authorization",
        "cookie",
        "credential",
        "key",
        "password",
        "secret",
        "session",
        "token",
    }
)
_REDACT_ALL_VALUES_KEYS = frozenset({"env", "environment", "headers"})
_SECRET_ASSIGNMENT_RE = re.compile(
    r"(?P<name>(?:[\"']?)(?:oh_secret_key|session_api_key|oh_session_api_keys_[a-z0-9_]*|api[_-]?key|authorization|cookie|credential|password|secret|token)(?:[\"']?)\s*[:=]\s*)(?:[\"']?)(?P<value>[^\s,;\]}\"']+)(?:[\"']?)",
    re.IGNORECASE,
)
_URL_CREDENTIALS_RE = re.compile(r"(https?://)[^/@\s]+@", re.IGNORECASE)
_URL_SECRET_PARAMETER_RE = re.compile(
    r"([?&](?:api[_-]?key|apikey|token|access_token|secret|key)=)[^&#\s]+",
    re.IGNORECASE,
)
_API_KEY_LITERAL_RE = re.compile(
    r"\b(?:"
    r"sk-oh-[A-Za-z0-9_-]{10,}"
    r"|fwrt_[A-Za-z0-9_-]{20,}"
    r"|sk-(?:or-v1|proj|ant-(?:api|oat)\d{2})-[A-Za-z0-9_-]{20,}"
    r"|gsk_[A-Za-z0-9]{20,}"
    r"|hf_[A-Za-z0-9]{20,}"
    r"|ghp_[A-Za-z0-9]{20,}"
    r"|github_pat_[A-Za-z0-9_]{20,}"
    r"|xox[bp]-[A-Za-z0-9_-]{20,}"
    r"|Bearer\s+[A-Za-z0-9_.-]{20,}"
    r")"
)


def is_sensitive_key(key: object) -> bool:
    """Return whether a structured field name conventionally carries a secret."""

    normalized = str(key).strip().lower().replace("-", "_")
    return (
        normalized in _SENSITIVE_KEY_NAMES
        or normalized in {"oh_secret_key", "session_api_key", "oh_session_api_keys"}
        or normalized.startswith("oh_session_api_keys_")
        or normalized.endswith(("_api_key", "_token", "_secret", "_password"))
    )


def redact_secret_text(value: str) -> str:
    """Replace recognizable secret literals and assignments in arbitrary text."""

    value = _URL_CREDENTIALS_RE.sub(r"\g<1>****@", value)
    value = _URL_SECRET_PARAMETER_RE.sub(r"\g<1>" + _REDACTED, value)
    value = _SECRET_ASSIGNMENT_RE.sub(r"\g<name>" + _REDACTED, value)
    return _API_KEY_LITERAL_RE.sub(_REDACTED, value)


def redact_secret_value(value: object, *, depth: int = 0) -> object:
    """Recursively copy a Runtime value while redacting sensitive fields."""

    if depth >= 6:
        return "[truncated]"
    if isinstance(value, Mapping):
        return {
            str(key): (
                _REDACTED
                if is_sensitive_key(key) or str(key).strip().lower() in _REDACT_ALL_VALUES_KEYS
                else redact_secret_value(child, depth=depth + 1)
            )
            for key, child in list(value.items())[:100]
        }
    if isinstance(value, list | tuple):
        return [redact_secret_value(item, depth=depth + 1) for item in value[:100]]
    if isinstance(value, str):
        return redact_secret_text(value[:20_000])
    return (
        value
        if value is None or isinstance(value, int | float | bool)
        else redact_secret_text(str(value))
    )


class SecretRedactionFilter(logging.Filter):
    """Prevent recognized credentials from entering a Python log handler."""

    def filter(self, record: logging.LogRecord) -> bool:
        if isinstance(record.msg, str):
            record.msg = redact_secret_text(record.msg)
        if record.args:
            if isinstance(record.args, Mapping):
                record.args = {
                    key: redact_secret_value(value) for key, value in record.args.items()
                }
            elif isinstance(record.args, tuple):
                record.args = tuple(redact_secret_value(value) for value in record.args)
            else:
                record.args = redact_secret_value(record.args)
        return True
