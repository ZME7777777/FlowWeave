"""Credential CRUD and the single host matching policy.

Secret values are decrypted only while an OpenHands Conversation request is
being assembled.  They are never included in list/read API projections.
"""

from __future__ import annotations

import json
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
        "target_path": item.target_path,
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
        WebsiteCredential.target_host, WebsiteCredential.target_path, WebsiteCredential.name
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
            target_path=payload.target_path,
            include_subdomains=payload.include_subdomains,
            auth_type=payload.auth_type,
            encrypted_username=encrypt_secret(payload.username) if payload.username else None,
            encrypted_secret=encrypt_secret(secret or ""),
            secret_hint=(secret or "")[-4:] or None,
        )
        db.add(item)
    else:
        item.name, item.target_host, item.target_path, item.include_subdomains, item.auth_type = (
            payload.name,
            payload.target_host,
            payload.target_path,
            payload.include_subdomains,
            payload.auth_type,
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


def delete_credentials(db: Session, credential_ids: list[str]) -> list[str]:
    ids = sorted(set(credential_ids))
    items = db.scalars(
        select(WebsiteCredential).where(WebsiteCredential.id.in_(ids)).with_for_update()
    ).all()
    found = {item.id for item in items}
    missing = next((credential_id for credential_id in ids if credential_id not in found), None)
    if missing is not None:
        raise not_found("website_credential", missing)
    for item in items:
        db.delete(item)
    return ids


def matches_host(item: WebsiteCredential, host: str) -> bool:
    normalized = host.rstrip(".").lower()
    return normalized == item.target_host or (
        item.include_subdomains and normalized.endswith("." + item.target_host)
    )


def matches_path(item: WebsiteCredential, path: str) -> bool:
    """Match the configured path as a whole directory prefix."""

    target_path = item.target_path
    normalized = "/" + path.lstrip("/")
    if target_path == "/":
        return True
    return normalized == target_path or normalized.startswith(target_path + "/")


def credentials_for_agent(db: Session) -> tuple[dict[str, str], str]:
    """Return OpenHands secrets and only non-sensitive matching metadata.

    OpenHands exports a secret only to a command that references its variable
    name. The model receives domain/name metadata, not a plaintext value.
    """
    values: dict[str, str] = {}
    directory: list[dict[str, object]] = []
    query = select(WebsiteCredential).order_by(
        WebsiteCredential.target_host, WebsiteCredential.target_path, WebsiteCredential.name
    )
    for item in db.scalars(query):
        prefix = _env_prefix(item)
        if item.auth_type == "USERNAME_PASSWORD":
            values[f"{prefix}_USERNAME"] = decrypt_secret(item.encrypted_username or b"")
            values[f"{prefix}_PASSWORD"] = decrypt_secret(item.encrypted_secret)
            source_env = {
                "username": f"${prefix}_USERNAME",
                "password": f"${prefix}_PASSWORD",
            }
            auth_type = "username_password"
        else:
            values[f"{prefix}_TOKEN"] = decrypt_secret(item.encrypted_secret)
            source_env = {"token": f"${prefix}_TOKEN"}
            auth_type = "token"
        directory.append(
            {
                "target_host": item.target_host,
                "target_path": item.target_path,
                "host_scope": "subdomains" if item.include_subdomains else "exact",
                "auth_type": auth_type,
                "source_env": source_env,
            }
        )
    if not directory:
        return {}, ""
    instructions = (
        "# 受控认证协议\n\n"
        "你只能按照下方凭据目录使用认证变量。\n\n"
        "执行任何可能访问网络的命令前：\n"
        "1. 从该命令实际访问的 URL 提取并规范化主机名和路径；路径为空时视为 `/`，"
        "忽略 query 与 fragment。\n"
        "2. 先从主机匹配的条目中选择 `target_path` 最长且按完整目录边界匹配的条目：`/admin` 可匹配 "
        "`/admin` 或 `/admin/users`，不能匹配 `/administrator`。`/` 是整台主机的兜底路径。\n"
        "3. 若没有具体路径范围命中，才按主机从完整主机名开始逐层去掉最左标签，"
        "选择最具体的可用条目；"
        "仅当 `host_scope` 为 `subdomains` 时，才允许父域条目匹配子域。不得把公共后缀（如 `com`）"
        "当作认证范围。\n"
        "4. 两个条目在同一主机／路径优先级上并列时不得任选；不得使用认证变量，并请用户消除歧义。\n"
        "5. 如 Skill 或脚本需要自定义环境变量名，只能在这条访问已匹配主机的命令内，将该条目的 "
        "`source_env` 映射给它；不得全局 `export`。\n"
        "6. `token` 是按原样注入的认证值；平台绝不自动添加 `Bearer `、`token ` 或其他前缀。"
        "仅当目标服务的实际协议要求时，才在这条命令内构造所需表达。\n"
        "7. 不得跨条目、跨主机使用或猜测源变量；不得输出、写入文件、提交或向用户索取凭据值。\n"
        "8. 没有匹配条目时，不得使用认证变量；请用户在认证管理中新增条目。\n\n"
        "# 凭据目录（仅元数据；不含凭据明文）\n\n"
        "```json\n"
        + json.dumps(
            {"schema_version": 2, "credentials": directory},
            ensure_ascii=False,
            indent=2,
            sort_keys=True,
        )
        + "\n```"
    )
    return values, instructions
