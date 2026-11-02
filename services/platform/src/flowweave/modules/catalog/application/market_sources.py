"""Current user's market preferences, shared by Plugin and Skill discovery."""

from __future__ import annotations

import hashlib
import json
import re
from urllib.parse import urlsplit

from sqlalchemy import select
from sqlalchemy.orm import Session

from flowweave.modules.users.application.security import current_user_id
from flowweave.modules.users.infrastructure.models import User
from flowweave.shared.application.transactions import finish
from flowweave.shared.errors import DomainError
from flowweave.shared.market_schemas import MarketSource, MarketSourcesWrite

BUILTINS = (
    MarketSource(
        id="openhands",
        name="OpenHands",
        kind="PLUGIN_GIT",
        url="https://github.com/OpenHands/extensions.git",
        builtin=True,
    ),
    MarketSource(
        id="skills-sh", name="skills.sh", kind="SKILLS_SH", url="https://skills.sh", builtin=True
    ),
)


def safe_path(value: str) -> str:
    if value and (
        not re.fullmatch(r"[A-Za-z0-9_.-]+(?:/[A-Za-z0-9_.-]+)*", value)
        or any(p in {".", ".."} for p in value.split("/"))
    ):
        raise DomainError("MARKET_SOURCE_INVALID", "目录必须是仓库内的安全相对路径", 422)
    return value


def github_repository(url: str) -> str:
    parsed = urlsplit(url)
    if parsed.scheme != "https" or parsed.netloc != "github.com" or parsed.query or parsed.fragment:
        raise DomainError("MARKET_SOURCE_INVALID", "Skill 仓库请使用公开 GitHub HTTPS 地址", 422)
    path = parsed.path.rstrip("/").removesuffix(".git").lstrip("/")
    if not re.fullmatch(r"[A-Za-z0-9_-][A-Za-z0-9_.-]*/[A-Za-z0-9_-][A-Za-z0-9_.-]*", path):
        raise DomainError(
            "MARKET_SOURCE_INVALID", "请填写 GitHub 仓库地址，不包含分支或文件路径", 422
        )
    return path


def _revision(saved: list) -> str:
    return hashlib.sha256(
        json.dumps(saved, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


def _snapshot(saved: list) -> dict:
    overrides = {item["id"]: item for item in saved}
    sources = [
        item.model_copy(
            update={"enabled": overrides.get(item.id, {}).get("enabled", True)}
        ).model_dump()
        for item in BUILTINS
    ]
    sources.extend(item for item in saved if item["id"] not in {b.id for b in BUILTINS})
    return {"revision": _revision(saved), "sources": sources}


def read_sources(db: Session) -> dict:
    user = db.get(User, current_user_id())
    if user is None:
        raise DomainError("AUTH_REQUIRED", "请先登录", 401)
    return _snapshot(user.market_sources_json or [])


def save_sources(db: Session, payload: MarketSourcesWrite) -> dict:
    user = db.scalar(select(User).where(User.id == current_user_id()).with_for_update())
    if user is None:
        raise DomainError("AUTH_REQUIRED", "请先登录", 401)
    if _revision(user.market_sources_json or []) != payload.revision:
        raise DomainError(
            "MARKET_SOURCES_CONFLICT", "市场源已在其他页面更新，请重新打开管理后再保存", 409
        )
    known = {item.id: item for item in BUILTINS}
    result = []
    seen: set[str] = set()
    for item in payload.sources:
        if item.id in seen:
            raise DomainError("MARKET_SOURCE_INVALID", "市场源 ID 重复", 422)
        seen.add(item.id)
        if item.id in known:
            expected = known[item.id].model_copy(update={"enabled": item.enabled})
            if item != expected:
                raise DomainError(
                    "MARKET_SOURCE_INVALID", "内置源地址固定，可启停或添加自定义源", 422
                )
        else:
            if item.builtin or item.kind == "SKILLS_SH":
                raise DomainError(
                    "MARKET_SOURCE_INVALID", "自定义源请选择 Plugin 或 Skill Git 仓库", 422
                )
            item.name = item.name.strip()
            if not item.name:
                raise DomainError("MARKET_SOURCE_INVALID", "请填写来源名称", 422)
            safe_path(item.repo_path)
            if (
                not re.fullmatch(
                    r"(?:HEAD|[0-9a-f]{40}|[A-Za-z0-9_][A-Za-z0-9_./-]{0,119})", item.ref
                )
                or ".." in item.ref
                or item.ref.endswith("/")
            ):
                raise DomainError("MARKET_SOURCE_INVALID", "分支、标签或提交号格式不正确", 422)
            if item.kind == "SKILL_GIT":
                github_repository(item.url)
            else:
                from flowweave.shared.application.plugin_resolver import PluginResolveRequest
                from flowweave.shared.infrastructure.plugin_resolver import (
                    configured_plugin_hosts,
                    validate_plugin_git_source,
                )
                from flowweave.shared.settings import get_settings

                validate_plugin_git_source(
                    PluginResolveRequest(item.url, "a" * 40, item.repo_path or None),
                    configured_plugin_hosts(get_settings()),
                )
        result.append(item.model_dump())
    # Built-ins cannot disappear; enabling/disabling is explicit.
    if not set(known).issubset(seen):
        raise DomainError("MARKET_SOURCE_INVALID", "请保留内置源，可通过开关停用", 422)
    user.market_sources_json = result
    finish(db)
    return _snapshot(result)


def select_source(db: Session, source_id: str, kinds: set[str]) -> MarketSource:
    item = next((s for s in read_sources(db)["sources"] if s["id"] == source_id), None)
    if not item or not item["enabled"] or item["kind"] not in kinds:
        raise DomainError(
            "MARKET_SOURCE_UNAVAILABLE", "来源不存在、已停用或不支持当前能力类型", 422
        )
    return MarketSource.model_validate(item)
