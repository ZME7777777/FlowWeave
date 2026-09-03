"""Credential CRUD and the single host matching policy.

Secret values are decrypted only while an OpenHands Conversation request is
being assembled.  They are never included in list/read API projections.
"""

from __future__ import annotations

import re
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from flowweave.modules.credentials.infrastructure.models import WebsiteCredential
from flowweave.shared.credentials_crypto import decrypt_secret, encrypt_secret
from flowweave.shared.errors import DomainError, not_found
from flowweave.shared.schemas import WebsiteCredentialWrite

_ENV_SAFE = re.compile(r"[^A-Za-z0-9]")


def _env_prefix(item: WebsiteCredential) -> str:
    return f"FLOWWEAVE_AUTH_{_ENV_SAFE.sub('', item.id).upper()}"


def _summary(item: WebsiteCredential) -> dict[str, Any]:
    prefix = _env_prefix(item)
    environment_names = (
        {
            "username": f"{prefix}_USERNAME",
            "password": f"{prefix}_PASSWORD",
        }
        if item.auth_type == "USERNAME_PASSWORD"
        else {"token": f"{prefix}_TOKEN"}
    )
    return {
        "id": item.id,
        "name": item.name,
        "target_host": item.target_host,
        "include_subdomains": item.include_subdomains,
        "auth_type": item.auth_type,
        "has_username": item.encrypted_username is not None,
        "has_secret": True,
        "secret_hint": item.secret_hint,
        "row_version": item.row_version,
        "environment_names": environment_names,
        "created_at": item.created_at.isoformat(),
        "updated_at": item.updated_at.isoformat(),
    }


def list_credentials(db: Session) -> list[dict[str, Any]]:
    query = select(WebsiteCredential).order_by(
        WebsiteCredential.target_host, WebsiteCredential.name
    )
    return [_summary(item) for item in db.scalars(query)]


def _item(db: Session, credential_id: str, *, lock: bool = False) -> WebsiteCredential:
    query = select(WebsiteCredential).where(WebsiteCredential.id == credential_id)
    if lock:
        query = query.with_for_update()
    item = db.scalar(query)
    if item is None:
        raise not_found("website_credential", credential_id)
    return item


def save_credential(
    db: Session, payload: WebsiteCredentialWrite, credential_id: str | None = None
) -> dict[str, Any]:
    secret = payload.secret.get_secret_value() if payload.secret is not None else None
    item = _item(db, credential_id, lock=True) if credential_id else None
    if item is not None and payload.row_version != item.row_version:
        raise DomainError("VERSION_CONFLICT", "认证信息已被其他操作修改，请刷新后重试。", 409)
    if item is None and not secret:
        raise DomainError("CREDENTIAL_SECRET_REQUIRED", "新建认证信息时必须填写密码或 Token。", 422)
    username_is_available = payload.username or (item and item.encrypted_username)
    if payload.auth_type == "USERNAME_PASSWORD" and not username_is_available:
        raise DomainError("CREDENTIAL_USERNAME_REQUIRED", "用户名密码认证必须填写用户名。", 422)
    if item is None:
        item = WebsiteCredential(
            name=payload.name,
            target_host=payload.target_host,
            include_subdomains=payload.include_subdomains,
            auth_type=payload.auth_type,
            encrypted_username=encrypt_secret(payload.username) if payload.username else None,
            encrypted_secret=encrypt_secret(secret or ""),
            secret_hint=(secret or "")[-4:] or None,
        )
        db.add(item)
    else:
        item.name, item.target_host, item.include_subdomains, item.auth_type = (
            payload.name, payload.target_host, payload.include_subdomains, payload.auth_type
        )
        if payload.username is not None:
            item.encrypted_username = encrypt_secret(payload.username) if payload.username else None
        if secret:
            item.encrypted_secret, item.secret_hint = encrypt_secret(secret), secret[-4:]
        item.row_version += 1
    db.flush()
    return _summary(item)


def delete_credential(db: Session, credential_id: str) -> None:
    db.delete(_item(db, credential_id, lock=True))


def matches_host(item: WebsiteCredential, host: str) -> bool:
    normalized = host.rstrip(".").lower()
    return normalized == item.target_host or (
        item.include_subdomains and normalized.endswith("." + item.target_host)
    )


def credentials_for_agent(db: Session) -> tuple[dict[str, str], str]:
    """Return OpenHands secrets and only non-sensitive matching metadata.

    OpenHands exports a secret only to a command that references its variable
    name. The model receives domain/name metadata, not a plaintext value.
    """
    values: dict[str, str] = {}
    lines: list[str] = []
    query = select(WebsiteCredential).order_by(
        WebsiteCredential.target_host, WebsiteCredential.name
    )
    for item in db.scalars(query):
        prefix = _env_prefix(item)
        if item.auth_type == "USERNAME_PASSWORD":
            values[f"{prefix}_USERNAME"] = decrypt_secret(item.encrypted_username or b"")
            values[f"{prefix}_PASSWORD"] = decrypt_secret(item.encrypted_secret)
            variables = f"${prefix}_USERNAME / ${prefix}_PASSWORD"
        else:
            values[f"{prefix}_TOKEN"] = decrypt_secret(item.encrypted_secret)
            variables = f"${prefix}_TOKEN"
        scope = item.target_host + (" 及其子域" if item.include_subdomains else "（仅精确主机）")
        lines.append(f"- {scope}：{item.name}（{item.auth_type}；变量 {variables}）")
    if not lines:
        return {}, ""
    instructions = (
        "受控网站认证：先从目标 URL 提取主机，再只选择精确匹配的条目；"
        "仅当条目明确允许子域时，才可匹配其子域。不得为不匹配的主机引用变量。"
        "不要输出、写入文件、提交或向用户索取这些值；未匹配时请求用户在认证管理中新增条目。\n"
        + "\n".join(lines)
    )
    return values, instructions
