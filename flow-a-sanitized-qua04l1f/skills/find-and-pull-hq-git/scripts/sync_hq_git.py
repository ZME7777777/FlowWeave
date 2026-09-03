#!/usr/bin/env python3
"""Clone or fast-forward selected repositories from the hq.team mapping."""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from dataclasses import asdict, dataclass
from pathlib import Path
from urllib.parse import urlparse

from search_hq_git import DEFAULT_MAPPING, Repository, parse_mapping


@dataclass(frozen=True)
class SyncResult:
    project: str
    status: str
    path: str
    message: str


def canonical_url(value: str) -> str:
    value = value.strip().rstrip("/")
    if value.endswith(".git"):
        value = value[:-4]
    parsed = urlparse(value)
    if parsed.scheme:
        return f"{parsed.hostname or ''}{parsed.path}".casefold().rstrip("/")
    if "@" in value and ":" in value:
        return value.split("@", 1)[1].replace(":", "/", 1).casefold().rstrip("/")
    return value.casefold()


def run_git(arguments: list[str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", *arguments],
        check=False,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
    )


def target_path(destination: Path, repo: Repository, layout: str) -> Path:
    if layout == "relative":
        relative = Path(repo.relative_path)
        if relative.is_absolute() or ".." in relative.parts:
            raise ValueError(f"不安全的相对路径：{repo.relative_path}")
        return destination / relative
    return destination / repo.project


def sync_one(repo: Repository, destination: Path, layout: str, dry_run: bool) -> SyncResult:
    target = target_path(destination, repo, layout)
    if not target.exists():
        if dry_run:
            return SyncResult(repo.project, "would-clone", str(target), repo.git_url)
        target.parent.mkdir(parents=True, exist_ok=True)
        completed = run_git(["clone", "--", repo.git_url, str(target)])
        if completed.returncode == 0:
            return SyncResult(repo.project, "cloned", str(target), completed.stdout.strip())
        return SyncResult(repo.project, "failed", str(target), completed.stdout.strip())

    if not target.is_dir():
        return SyncResult(repo.project, "failed", str(target), "目标存在但不是目录，拒绝覆盖")
    inside = run_git(["-C", str(target), "rev-parse", "--is-inside-work-tree"])
    if inside.returncode != 0 or inside.stdout.strip() != "true":
        return SyncResult(repo.project, "failed", str(target), "目标存在但不是 Git 工作树，拒绝覆盖")
    origin = run_git(["-C", str(target), "remote", "get-url", "origin"])
    if origin.returncode != 0:
        return SyncResult(repo.project, "failed", str(target), "现有仓库没有 origin，拒绝更新")
    actual_origin = origin.stdout.strip()
    if canonical_url(actual_origin) != canonical_url(repo.git_url):
        message = f"origin 不匹配，拒绝更新：实际 {actual_origin}；期望 {repo.git_url}"
        return SyncResult(repo.project, "failed", str(target), message)
    if dry_run:
        return SyncResult(repo.project, "would-update", str(target), "git pull --ff-only")
    completed = run_git(["-C", str(target), "pull", "--ff-only"])
    if completed.returncode == 0:
        return SyncResult(repo.project, "updated", str(target), completed.stdout.strip())
    return SyncResult(repo.project, "failed", str(target), completed.stdout.strip())


def resolve_projects(names: list[str], repos: list[Repository]) -> tuple[list[Repository], list[str]]:
    by_name = {repo.project.casefold(): repo for repo in repos}
    selected: list[Repository] = []
    missing: list[str] = []
    seen: set[str] = set()
    for name in names:
        key = name.casefold().strip()
        repo = by_name.get(key)
        if repo is None:
            missing.append(name)
        elif key not in seen:
            selected.append(repo)
            seen.add(key)
    return selected, missing


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--project", action="append", required=True, help="精确项目名；可重复")
    parser.add_argument("--destination", type=Path, required=True)
    parser.add_argument("--layout", choices=("flat", "relative"), default="flat")
    parser.add_argument("--mapping", type=Path, default=DEFAULT_MAPPING)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--json", action="store_true")
    return parser


def main() -> int:
    args = build_parser().parse_args()
    if not args.mapping.is_file():
        print(f"错误：映射文件不存在：{args.mapping}", file=sys.stderr)
        return 2
    try:
        repos = parse_mapping(args.mapping)
        selected, missing = resolve_projects(args.project, repos)
    except (OSError, UnicodeError, ValueError) as exc:
        print(f"错误：{exc}", file=sys.stderr)
        return 2
    if missing:
        print("错误：以下项目名不在映射中：" + ", ".join(missing), file=sys.stderr)
        return 2

    destination = args.destination.expanduser().resolve()
    results = []
    for repo in selected:
        try:
            results.append(sync_one(repo, destination, args.layout, args.dry_run))
        except (OSError, ValueError) as exc:
            results.append(SyncResult(repo.project, "failed", "", str(exc)))

    if args.json:
        print(json.dumps([asdict(item) for item in results], ensure_ascii=False, indent=2))
    else:
        for result in results:
            print(f"[{result.status}] {result.project} -> {result.path}")
            if result.message:
                print(f"  {result.message}")
    return 1 if any(item.status == "failed" for item in results) else 0


if __name__ == "__main__":
    raise SystemExit(main())
