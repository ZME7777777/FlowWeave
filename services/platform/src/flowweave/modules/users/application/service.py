from __future__ import annotations

import secrets
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from flowweave.modules.users.application.security import (
    FLOWWEAVE_USER_ID,
    USER_USER_ID,
    Principal,
    digest_session_token,
    hash_password,
    verify_password,
)
from flowweave.modules.users.infrastructure.models import User, UserSession
from flowweave.shared.errors import DomainError

SESSION_COOKIE = "flowweave_session"
SESSION_TTL = timedelta(hours=12)


@dataclass(frozen=True, slots=True)
class LoginResult:
    principal: Principal
    token: str


def ensure_builtin_users(
    db: Session, *, admin_password: str, user_password: str
) -> None:
    """Idempotently provision the two deployment-owned login principals."""

    configured = (
        (FLOWWEAVE_USER_ID, "flowweave", "SUPER_ADMIN", admin_password),
        (USER_USER_ID, "user", "USER", user_password),
    )
    for user_id, username, role, password in configured:
        if not password:
            continue
        existing = db.get(User, user_id)
        if existing is None:
            db.add(
                User(
                    id=user_id,
                    username=username,
                    password_hash=hash_password(password),
                    role=role,
                )
            )
            continue
        if existing.username != username or existing.role != role:
            raise RuntimeError(f"Built-in user identity drift: {username}")
        if not verify_password(password, existing.password_hash):
            existing.password_hash = hash_password(password)
            existing.updated_at = datetime.now(UTC)
        existing.is_active = True
    db.flush()


def _principal(user: User) -> Principal:
    return Principal(user_id=user.id, username=user.username, role=user.role)


def login(db: Session, username: str, password: str) -> LoginResult:
    normalized = username.strip()
    user = db.scalar(select(User).where(User.username == normalized))
    if user is None or not user.is_active or not verify_password(password, user.password_hash):
        raise DomainError("AUTHENTICATION_FAILED", "用户名或密码错误", 401)
    token = secrets.token_urlsafe(48)
    db.add(
        UserSession(
            user_id=user.id,
            token_digest=digest_session_token(token),
            expires_at=datetime.now(UTC) + SESSION_TTL,
        )
    )
    db.flush()
    return LoginResult(_principal(user), token)


def authenticate(db: Session, token: str | None) -> Principal | None:
    if not token:
        return None
    now = datetime.now(UTC)
    row = db.execute(
        select(UserSession, User)
        .join(User, User.id == UserSession.user_id)
        .where(
            UserSession.token_digest == digest_session_token(token),
            UserSession.expires_at > now,
            User.is_active.is_(True),
        )
    ).first()
    if row is None:
        return None
    session, user = row
    session.last_seen_at = now
    return _principal(user)


def logout(db: Session, token: str | None) -> None:
    if token:
        db.execute(
            delete(UserSession).where(
                UserSession.token_digest == digest_session_token(token)
            )
        )


def principal_dict(principal: Principal) -> dict[str, object]:
    return {
        "id": principal.user_id,
        "username": principal.username,
        "role": principal.role,
        "is_super_admin": principal.is_super_admin,
    }


__all__ = (
    "SESSION_COOKIE",
    "SESSION_TTL",
    "LoginResult",
    "authenticate",
    "ensure_builtin_users",
    "login",
    "logout",
    "principal_dict",
)
