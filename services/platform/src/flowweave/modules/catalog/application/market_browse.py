"""Bounded discovery projections; publication still uses the native Plugin resolver."""

from __future__ import annotations

import copy
import json
import re
import time
from collections import OrderedDict
from collections.abc import Callable
from concurrent.futures import Future
from threading import Lock
from typing import Any
from urllib.parse import urljoin, urlsplit

import httpx
from pydantic import BaseModel, Field

from flowweave.modules.catalog.application import plugin_sources
from flowweave.shared.application.plugin_resolver import PluginResolveRequest
from flowweave.shared.errors import DomainError
from flowweave.shared.infrastructure.plugin_resolver import (
    configured_plugin_hosts,
    validate_plugin_git_source,
)
from flowweave.shared.market_schemas import MarketSource
from flowweave.shared.schemas import MarketplaceCatalogRead, MarketplaceCatalogWrite
from flowweave.shared.settings import get_settings

_cache: OrderedDict[tuple[object, ...], tuple[float, Any]] = OrderedDict()
_inflight: dict[tuple[object, ...], Future[Any]] = {}
_lock = Lock()


def cached(key: tuple[object, ...], loader: Callable[[], Any], ttl: int = 300) -> Any:
    with _lock:
        found = _cache.get(key)
        if found and time.monotonic() < found[0]:
            _cache.move_to_end(key)
            return copy.deepcopy(found[1])
        pending = _inflight.get(key)
        owner = pending is None
        if owner:
            pending = Future[Any]()
            _inflight[key] = pending
    assert pending is not None
    if not owner:
        return copy.deepcopy(pending.result(timeout=330))
    try:
        value = loader()
        with _lock:
            _cache[key] = (time.monotonic() + ttl, value)
            _cache.move_to_end(key)
            while len(_cache) > 32:
                _cache.popitem(last=False)
        pending.set_result(value)
        return copy.deepcopy(value)
    except BaseException as exc:
        pending.set_exception(exc)
        raise
    finally:
        with _lock:
            _inflight.pop(key, None)


def public_json(url: str, *, missing_ok: bool = False, repository_root: str | None = None) -> Any:
    for _ in range(4):
        with httpx.stream("GET", url, timeout=20, follow_redirects=False) as response:
            if missing_ok and response.status_code == 404:
                return None
            response.raise_for_status()
            raw = bytearray()
            for chunk in response.iter_bytes():
                raw.extend(chunk)
                if len(raw) > 2 * 1024 * 1024:
                    raise ValueError("Discovery response exceeds its size limit")
        try:
            return json.loads(raw)
        except ValueError:
            # GitHub Raw serves a Git symlink as its relative target text. Follow
            # only bounded paths inside this exact repository/commit snapshot.
            target = raw.decode("utf-8").strip()
            next_url = urljoin(url, target)
            if (
                not repository_root
                or not re.fullmatch(r"[A-Za-z0-9_./-]{1,500}", target)
                or target.startswith("/")
                or not next_url.startswith(repository_root)
            ):
                raise ValueError("Invalid discovery JSON or repository link") from None
            url = next_url
    raise ValueError("Too many repository links")


class _Author(BaseModel):
    name: str


class _Metadata(BaseModel):
    description: str | None = None
    version: str | None = None


class _Entry(BaseModel):
    name: str = Field(min_length=1)
    description: str | None = None
    version: str | None = None
    category: str | None = None
    author: _Author | str | None = None


class _Manifest(BaseModel):
    name: str
    owner: _Author
    description: str | None = None
    metadata: _Metadata | None = None
    plugins: list[_Entry] = Field(default_factory=lambda: list[_Entry]())


def _catalog(source: MarketSource, commit: str) -> dict[str, Any]:
    request = validate_plugin_git_source(
        PluginResolveRequest(source.url, commit, source.repo_path or None),
        configured_plugin_hosts(get_settings()),
    )
    parsed = urlsplit(request.source)
    segments = parsed.path.strip("/").removesuffix(".git").split("/")
    if parsed.hostname != "github.com" or len(segments) != 2:
        # Other allowed hosts retain the formal isolated resolver contract.
        return plugin_sources.list_marketplace_catalog(
            MarketplaceCatalogWrite(
                marketplace_source_url=request.source,
                marketplace_commit=commit,
                marketplace_repo_path=request.repo_path,
            )
        )
    # Only project the bounded public manifest. The native Marketplace resolver
    # still validates and freezes the selected Plugin during actual import.
    root = f"{request.repo_path}/" if request.repo_path else ""
    for directory in (".plugin", ".claude-plugin"):
        raw = public_json(
            f"https://raw.githubusercontent.com/{'/'.join(segments)}/{commit}/"
            f"{root}{directory}/marketplace.json",
            missing_ok=True,
            repository_root=f"https://raw.githubusercontent.com/{'/'.join(segments)}/{commit}/",
        )
        if raw is None:
            continue
        manifest = _Manifest.model_validate(raw)
        names = [entry.name for entry in manifest.plugins]
        if len(names) != len(set(names)):
            raise ValueError("Duplicate marketplace entry")
        metadata = manifest.metadata or _Metadata()
        return MarketplaceCatalogRead.model_validate(
            {
                "schema_version": 1,
                "source": request.source,
                "commit": commit,
                "repo_path": request.repo_path,
                "marketplace_name": manifest.name,
                "description": manifest.description or metadata.description,
                "version": metadata.version,
                "owner": manifest.owner.name,
                "plugins": [
                    {
                        **entry.model_dump(exclude={"author"}),
                        "author": entry.author.name
                        if isinstance(entry.author, _Author)
                        else entry.author,
                    }
                    for entry in manifest.plugins
                ],
            }
        ).model_dump()
    raise ValueError("Marketplace manifest was not found")


def plugin_page(
    source: MarketSource, query: str, page: int, page_size: int, commit: str | None
) -> dict[str, Any]:
    from flowweave.modules.catalog.application.skill_market import pin_commit

    try:
        pinned = commit or cached(
            ("plugin-ref", source.url, source.ref),
            lambda: pin_commit(source.url, source.ref),
            ttl=120,
        )
        catalog = cached(
            ("plugin-catalog", source.url, source.repo_path, pinned),
            lambda: _catalog(source, pinned),
        )
        term = query.strip().casefold()
        plugins = [
            item
            for item in catalog["plugins"]
            if not term
            or term
            in " ".join(
                str(item.get(key) or "") for key in ("name", "description", "category")
            ).casefold()
        ]
        return {
            **catalog,
            "plugins": plugins[(page - 1) * page_size : page * page_size],
            "total": len(plugins),
            "page": page,
            "page_size": page_size,
            "query": query,
        }
    except (httpx.HTTPError, ValueError, TypeError, TimeoutError, RuntimeError) as exc:
        raise DomainError(
            "MARKET_SOURCE_UNAVAILABLE", "插件目录暂时不可用，请重试或切换来源", 503
        ) from exc
