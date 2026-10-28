"""Public discovery only; imports still pass governed Skill validation/publication."""

from __future__ import annotations

import base64
import io
import os
import re
import subprocess
import time
import zipfile
from collections import OrderedDict
from dataclasses import replace
from pathlib import PurePosixPath
from threading import Lock

import httpx
import yaml

from flowweave.modules.catalog.application import capability_imports, market_sources, plugin_sources
from flowweave.shared.errors import DomainError
from flowweave.shared.market_schemas import MarketSkillImportWrite, MarketSource
from flowweave.shared.schemas import CapabilityValidateWrite, MarketplaceCatalogWrite

_MAX_BYTES = 25 * 1024 * 1024
_cache: OrderedDict[tuple[str, str], tuple[float, bytes]] = OrderedDict()
_lock = Lock()


def unavailable(message: str = "市场源暂时不可用，请稍后重试") -> DomainError:
    return DomainError("MARKET_SOURCE_UNAVAILABLE", message, 503)


def pin_commit(url: str, ref: str) -> str:
    if re.fullmatch(r"[0-9a-f]{40}", ref):
        return ref
    refs = (
        ["HEAD"]
        if ref == "HEAD"
        else [f"refs/heads/{ref}", f"refs/tags/{ref}", f"refs/tags/{ref}^{{}}"]
    )
    try:
        result = subprocess.run(
            ["git", "ls-remote", "--", url, *refs],
            capture_output=True,
            text=True,
            timeout=25,
            env={"PATH": os.defpath, "GIT_TERMINAL_PROMPT": "0"},
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise unavailable("无法解析仓库版本，请检查来源和网络") from exc
    candidates = {
        line.split()[1]: line.split()[0]
        for line in result.stdout.splitlines()
        if re.fullmatch(r"[0-9a-f]{40}\s+\S+", line)
    }
    # Annotated tags resolve to the peeled commit, never a tag object.
    commit = next((candidates[key] for key in reversed(refs) if key in candidates), None)
    if result.returncode or commit is None:
        raise unavailable("无法解析仓库版本，请检查分支或标签")
    return commit


def _download(repository: str, commit: str) -> bytes:
    key = (repository, commit)
    with _lock:
        cached = _cache.get(key)
        if cached and time.monotonic() - cached[0] < 300:
            _cache.move_to_end(key)
            return cached[1]
    try:
        with httpx.stream(
            "GET",
            f"https://codeload.github.com/{repository}/zip/{commit}",
            timeout=45,
            follow_redirects=False,
        ) as response:
            response.raise_for_status()
            content = bytearray()
            for chunk in response.iter_bytes():
                content.extend(chunk)
                if len(content) > _MAX_BYTES:
                    raise DomainError(
                        "MARKET_ARCHIVE_TOO_LARGE", "来源仓库超过 25 MiB，请使用较小的技能仓库", 422
                    )
    except httpx.HTTPError as exc:
        raise unavailable("无法下载公开仓库，请检查来源和网络") from exc
    frozen = bytes(content)
    with _lock:
        _cache[key] = (time.monotonic(), frozen)
        _cache.move_to_end(key)
        while len(_cache) > 4:
            _cache.popitem(last=False)
    return frozen


class RepositoryFiles(dict[str, bytes]):
    """Keep link metadata so unrelated repo links do not block a safe Skill."""

    def __init__(self) -> None:
        super().__init__()
        self.symlinks: set[str] = set()


def _files(content: bytes) -> RepositoryFiles:
    """Read bounded ZIP in memory, without extracting upstream paths on disk."""
    try:
        with zipfile.ZipFile(io.BytesIO(content)) as archive:
            entries = archive.infolist()
            if len(entries) > 10000 or sum(item.file_size for item in entries) > 100 * 1024 * 1024:
                raise DomainError("MARKET_ARCHIVE_TOO_LARGE", "来源仓库解压体积或文件数量超限", 422)
            result = RepositoryFiles()
            roots = set()
            for item in entries:
                path = PurePosixPath(item.filename)
                if path.is_absolute() or ".." in path.parts or "\\" in item.filename:
                    raise DomainError(
                        "MARKET_ARCHIVE_UNSAFE", "来源仓库含不安全路径或符号链接", 422
                    )
                if not path.parts:
                    continue
                roots.add(path.parts[0])
                if item.is_dir():
                    continue
                if item.file_size > _MAX_BYTES:
                    raise DomainError("MARKET_ARCHIVE_TOO_LARGE", "来源仓库文件超限", 422)
                relative = "/".join(path.parts[1:])
                if not relative or relative in result:
                    raise DomainError("MARKET_ARCHIVE_UNSAFE", "来源仓库结构无效", 422)
                if (item.external_attr >> 16) & 0o170000 == 0o120000:
                    result.symlinks.add(relative)
                result[relative] = archive.read(item)
            if len(roots) != 1:
                raise DomainError("MARKET_ARCHIVE_UNSAFE", "来源仓库结构无效", 422)
            return result
    except (zipfile.BadZipFile, RuntimeError, OSError, ValueError) as exc:
        raise unavailable("来源仓库不是可读取的 ZIP 包") from exc


def _skills(files: dict[str, bytes], root: str = "") -> list[dict]:
    result = []
    for path, content in sorted(files.items()):
        if path in getattr(files, "symlinks", set()):
            continue
        if PurePosixPath(path).name != "SKILL.md" or (root and not path.startswith(root + "/")):
            continue
        if len(content) > 256 * 1024:
            continue
        try:
            text = content.decode("utf-8")
            meta = (
                yaml.safe_load(text.split("---", 2)[1])
                if text.startswith("---\n") and len(text.split("---", 2)) == 3
                else {}
            )
        except (UnicodeDecodeError, yaml.YAMLError):
            continue
        if not isinstance(meta, dict):
            continue
        parent = str(PurePosixPath(path).parent)
        parent = "" if parent == "." else parent
        name = str(meta.get("name") or PurePosixPath(parent).name or "skill")[:200]
        result.append(
            {
                "id": path,
                "skill_id": name,
                "name": name,
                "description": str(meta.get("description") or "")[:1024],
                "path": parent,
            }
        )
    return result


def search(source: MarketSource, query: str) -> dict:
    if source.kind == "SKILLS_SH":
        if len(query.strip()) < 2:
            return {"skills": [], "query": query, "search_required": True}
        try:
            with httpx.stream(
                "GET",
                "https://skills.sh/api/search",
                params={"q": query.strip(), "limit": 50},
                timeout=20,
                follow_redirects=False,
            ) as response:
                response.raise_for_status()
                raw = bytearray()
                for chunk in response.iter_bytes():
                    raw.extend(chunk)
                    if len(raw) > 1024 * 1024:
                        raise unavailable()
                import json

                data = json.loads(raw)
            if not isinstance(data, dict) or not isinstance(data.get("skills"), list):
                raise ValueError("Invalid catalog")
            skills = []
            for item in data["skills"][:50]:
                if not isinstance(item, dict):
                    continue
                repo, skill_id = item.get("source"), item.get("skillId")
                if (
                    not isinstance(repo, str)
                    or not isinstance(skill_id, str)
                    or not skill_id
                    or len(skill_id) > 200
                ):
                    continue
                try:
                    market_sources.github_repository(f"https://github.com/{repo}")
                except DomainError:
                    continue
                skills.append(
                    {
                        "id": f"{repo}/{skill_id}",
                        "repository": repo,
                        "skill_id": skill_id,
                        "name": str(item.get("name") or skill_id)[:200],
                        "description": repo,
                        "installs": item.get("installs")
                        if isinstance(item.get("installs"), int)
                        else None,
                        "commit": None,
                        "path": None,
                    }
                )
            return {"skills": skills, "query": query, "search_required": False}
        except (httpx.HTTPError, ValueError, TypeError) as exc:
            raise unavailable("skills.sh 搜索暂时不可用，请稍后重试或切换 Git 来源") from exc
    repository = market_sources.github_repository(source.url)
    commit = pin_commit(source.url, source.ref)
    skills = _skills(_files(_download(repository, commit)), source.repo_path)
    return {
        "skills": [
            {**item, "id": f"{repository}/{item['id']}", "repository": repository, "commit": commit}
            for item in skills
            if query.lower().strip() in f"{item['name']} {item['description']}".lower()
        ],
        "commit": commit,
        "search_required": False,
        "query": query,
    }


def plugin_catalog(source: MarketSource) -> dict:
    if source.id == "openhands":
        return plugin_sources.list_openhands_marketplace_catalog()
    return plugin_sources.list_marketplace_catalog(
        MarketplaceCatalogWrite(
            marketplace_source_url=source.url,
            marketplace_commit=pin_commit(source.url, source.ref),
            marketplace_repo_path=source.repo_path or None,
        )
    )


def prepare_import(
    source: MarketSource, payload: MarketSkillImportWrite
) -> capability_imports.PreparedCapabilityImport:
    repository = market_sources.github_repository(f"https://github.com/{payload.repository}")
    if source.kind == "SKILL_GIT" and repository != market_sources.github_repository(source.url):
        raise DomainError("MARKET_SOURCE_INVALID", "所选技能不属于当前来源", 422)
    url = f"https://github.com/{repository}.git"
    commit = payload.commit or pin_commit(url, source.ref if source.kind == "SKILL_GIT" else "HEAD")
    files = _files(_download(repository, commit))
    skills = _skills(files, source.repo_path if source.kind == "SKILL_GIT" else "")
    if payload.path is not None:
        market_sources.safe_path(payload.path)
        candidates = [
            item
            for item in skills
            if item["path"] == payload.path and item["skill_id"] == payload.skill_id
        ]
    else:
        candidates = [
            item
            for item in skills
            if item["skill_id"] == payload.skill_id
            or PurePosixPath(item["path"]).name == payload.skill_id
        ]
    if len(candidates) != 1:
        raise DomainError(
            "MARKET_SKILL_AMBIGUOUS", "无法唯一定位所选 Skill，请使用 Git 来源指定目录后重试", 422
        )
    root = candidates[0]["path"]
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for path, content in sorted(files.items()):
            if root and not path.startswith(root + "/"):
                continue
            if path in files.symlinks:
                raise DomainError(
                    "MARKET_ARCHIVE_UNSAFE", "所选技能目录含符号链接，请使用独立的完整技能目录", 422
                )
            name = path[len(root) + 1 :] if root else path
            info = zipfile.ZipInfo(name, date_time=(1980, 1, 1, 0, 0, 0))
            info.compress_type = zipfile.ZIP_DEFLATED
            info.external_attr = 0o100644 << 16
            archive.writestr(info, content)
    prepared = capability_imports.prepare_validation(
        CapabilityValidateWrite(
            capability_type="SKILL",
            filename="market-skill.zip",
            content_base64=base64.b64encode(output.getvalue()).decode(),
        )
    )
    provenance = {
        "market_source_id": source.id,
        "market_source_name": source.name,
        "market_source_url": source.url,
        "repository_url": url,
        "commit": commit,
        "skill_path": root,
        "content_hash": prepared.content_hash,
    }
    return replace(prepared, preview={**prepared.preview, "source": provenance})
