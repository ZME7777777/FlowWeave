from __future__ import annotations

import hashlib
import hmac
import secrets
from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar, Token
from dataclasses import dataclass

_PASSWORD_ITERATIONS = 600_000
_current_principal: ContextVar[Principal | None] = ContextVar(
    "flowweave_current_principal", default=None
)
_tenant_bypass: ContextVar[bool] = ContextVar("flowweave_tenant_bypass", default=False)
_tenant_user_id: ContextVar[str | None] = ContextVar("flowweave_tenant_user_id", default=None)

FLOWWEAVE_USER_ID = "00000000-0000-0000-0000-000000000001"
USER_USER_ID = "00000000-0000-0000-0000-000000000002"
_RUNTIME_WORKSPACE_ROOT = "/runtime/workspace"


@dataclass(frozen=True, slots=True)
class Principal:
    user_id: str
    username: str
    role: str

    @property
    def is_super_admin(self) -> bool:
        return self.role == "SUPER_ADMIN"


def hash_password(password: str, *, salt: bytes | None = None) -> str:
    actual_salt = salt or secrets.token_bytes(16)
    digest = hashlib.pbkdf2_hmac(
        "sha256", password.encode("utf-8"), actual_salt, _PASSWORD_ITERATIONS
    )
    return f"pbkdf2_sha256${_PASSWORD_ITERATIONS}${actual_salt.hex()}${digest.hex()}"


def verify_password(password: str, encoded: str) -> bool:
    try:
        algorithm, iterations, salt, expected = encoded.split("$", 3)
        if algorithm != "pbkdf2_sha256":
            return False
        actual = hashlib.pbkdf2_hmac(
            "sha256", password.encode("utf-8"), bytes.fromhex(salt), int(iterations)
        )
        return hmac.compare_digest(actual.hex(), expected)
    except (TypeError, ValueError):
        return False


def digest_session_token(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def current_principal() -> Principal | None:
    return _current_principal.get()


def current_user_id(*, default: str | None = None) -> str:
    tenant_user_id = _tenant_user_id.get()
    if tenant_user_id is not None:
        return tenant_user_id
    principal = current_principal()
    if principal is not None:
        return principal.user_id
    if default is not None:
        return default
    raise RuntimeError("A user principal is required for this operation")


def bind_principal(principal: Principal | None) -> Token[Principal | None]:
    return _current_principal.set(principal)


def reset_principal(token: Token[Principal | None]) -> None:
    _current_principal.reset(token)


def tenant_filter_bypassed() -> bool:
    return _tenant_bypass.get()


def agent_workspace_runtime_root(workspace_id: str) -> str:
    """Return the server-derived Runtime root for one Agent Workspace record."""

    return f"{_RUNTIME_WORKSPACE_ROOT}/{workspace_id}"


def user_runtime_project_root(workspace_id: str) -> str:
    """Return the current user's directory inside one Agent Workspace record."""

    return (
        f"{agent_workspace_runtime_root(workspace_id)}/users/"
        f"{current_user_id(default=FLOWWEAVE_USER_ID)}"
    )


@contextmanager
def tenant_user(user_id: str) -> Iterator[None]:
    token = _tenant_user_id.set(user_id)
    try:
        yield
    finally:
        _tenant_user_id.reset(token)


@contextmanager
def tenant_bypass() -> Iterator[None]:
    token = _tenant_bypass.set(True)
    try:
        yield
    finally:
        _tenant_bypass.reset(token)


__all__ = (
    "Principal",
    "FLOWWEAVE_USER_ID",
    "USER_USER_ID",
    "agent_workspace_runtime_root",
    "bind_principal",
    "current_principal",
    "current_user_id",
    "digest_session_token",
    "hash_password",
    "reset_principal",
    "tenant_bypass",
    "tenant_filter_bypassed",
    "tenant_user",
    "user_runtime_project_root",
    "verify_password",
)
