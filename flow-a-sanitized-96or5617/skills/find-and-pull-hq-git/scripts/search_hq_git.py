#!/usr/bin/env python3
"""Fuzzy-search repositories in the hq.team Markdown mapping."""

from __future__ import annotations

import argparse
import html
import json
import re
import sys
import unicodedata
from dataclasses import asdict, dataclass
from difflib import SequenceMatcher
from pathlib import Path
from typing import Iterable
from urllib.parse import urlparse

SKILL_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_MAPPING = SKILL_ROOT / "references" / "hq_team_project_git_mapping.md"
SEPARATOR_RE = re.compile(r"[^0-9a-z\u4e00-\u9fff]+")
BR_RE = re.compile(r"<br\s*/?>", re.IGNORECASE)


@dataclass(frozen=True)
class Repository:
    project: str
    git_url: str
    relative_path: str
    applications: tuple[str, ...]


@dataclass(frozen=True)
class SearchResult:
    rank: int
    score: float
    matched_field: str
    matched_value: str
    project: str
    git_url: str
    relative_path: str
    applications: tuple[str, ...]


def normalize(value: str) -> str:
    value = unicodedata.normalize("NFKC", html.unescape(value)).casefold().strip()
    return " ".join(SEPARATOR_RE.sub(" ", value).split())


def compact(value: str) -> str:
    return normalize(value).replace(" ", "")


def split_apps(value: str) -> tuple[str, ...]:
    if not value.strip() or value.strip() in {"—", "-"}:
        return ()
    return tuple(
        part.strip()
        for part in BR_RE.split(html.unescape(value))
        if part.strip() and part.strip() not in {"—", "-"}
    )


def parse_mapping(path: Path) -> list[Repository]:
    repositories: list[Repository] = []
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line.startswith("|"):
            continue
        cells = [cell.strip() for cell in line.strip("|").split("|")]
        if len(cells) != 4:
            continue
        project, git_url, relative_path, apps = cells
        if project in {"项目名", "---"} or set(project) <= {"-", ":"}:
            continue
        if not re.match(r"^(?:https?|ssh|git|file)://|^[^@\s]+@[^:\s]+:", git_url):
            continue
        repositories.append(
            Repository(project, git_url, relative_path, split_apps(apps))
        )
    if not repositories:
        raise ValueError(f"未在映射文件中解析到仓库记录：{path}")
    return repositories


def repository_name(url: str) -> str:
    parsed = urlparse(url)
    path = parsed.path if parsed.scheme else url.split(":", 1)[-1]
    return Path(path).name.removesuffix(".git")


def similarity(query: str, candidate: str, field: str) -> float:
    qn, cn = normalize(query), normalize(candidate)
    qc, cc = compact(query), compact(candidate)
    if not qn or not cn:
        return 0.0

    exact_scores = {
        "项目名": 100.0,
        "VOPS 应用名": 99.0,
        "仓库名": 98.0,
        "相对路径": 95.0,
        "Git 地址": 92.0,
    }
    base = exact_scores[field]
    if qn == cn or qc == cc:
        return base

    score = 0.0
    if qc in cc:
        coverage = len(qc) / max(len(cc), 1)
        score = max(score, 72.0 + 18.0 * coverage + (3.0 if cc.startswith(qc) else 0.0))
    elif cc in qc:
        coverage = len(cc) / max(len(qc), 1)
        score = max(score, 64.0 + 15.0 * coverage)

    q_tokens, c_tokens = qn.split(), cn.split()
    q_set, c_set = set(q_tokens), set(c_tokens)
    if q_set and q_set <= c_set:
        score = max(score, 78.0 + 12.0 * len(q_set) / max(len(c_set), 1))
    overlap = len(q_set & c_set) / max(len(q_set | c_set), 1)
    ratio = SequenceMatcher(None, qc, cc).ratio()
    ordered_ratio = SequenceMatcher(None, qn, cn).ratio()
    score = max(score, 62.0 * ratio, 56.0 * ordered_ratio + 24.0 * overlap)

    # Prefer direct names to path/URL matches when the textual similarity is equal.
    field_adjustment = {
        "项目名": 2.0,
        "VOPS 应用名": 1.5,
        "仓库名": 1.0,
        "相对路径": -1.0,
        "Git 地址": -3.0,
    }[field]
    return min(base - 0.1, max(0.0, score + field_adjustment))


def score_repository(query: str, repo: Repository) -> tuple[float, str, str]:
    candidates: list[tuple[str, str]] = [
        ("项目名", repo.project),
        ("仓库名", repository_name(repo.git_url)),
        ("相对路径", repo.relative_path),
        ("Git 地址", repo.git_url),
    ]
    candidates.extend(("VOPS 应用名", app) for app in repo.applications)
    scored = [(similarity(query, value, field), field, value) for field, value in candidates]
    return max(scored, key=lambda item: (item[0], item[1] == "项目名"))


def search(
    query: str,
    repositories: Iterable[Repository],
    limit: int = 15,
    min_score: float = 28.0,
) -> list[SearchResult]:
    scored = []
    for repo in repositories:
        score, field, value = score_repository(query, repo)
        if score >= min_score:
            scored.append((score, repo.project.casefold(), field, value, repo))
    scored.sort(key=lambda item: (-item[0], item[1]))
    return [
        SearchResult(
            rank=index,
            score=round(score, 1),
            matched_field=field,
            matched_value=value,
            project=repo.project,
            git_url=repo.git_url,
            relative_path=repo.relative_path,
            applications=repo.applications,
        )
        for index, (score, _, field, value, repo) in enumerate(scored[:limit], start=1)
    ]


def escape_cell(value: str) -> str:
    return value.replace("|", "\\|").replace("\n", " ")


def print_table(results: list[SearchResult]) -> None:
    if not results:
        print("未找到达到最低匹配分的仓库。")
        return
    print("| # | 分数 | 匹配依据 | 项目名 | VOPS 应用名 | 相对路径 | Git 地址 |")
    print("|---:|---:|---|---|---|---|---|")
    for result in results:
        apps = "<br>".join(result.applications) if result.applications else "—"
        basis = f"{result.matched_field}: {result.matched_value}"
        values = [
            str(result.rank),
            f"{result.score:.1f}",
            basis,
            result.project,
            apps,
            result.relative_path,
            result.git_url,
        ]
        print("| " + " | ".join(escape_cell(value) for value in values) + " |")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("query", help="应用名、项目名或其他相关名称")
    parser.add_argument("--mapping", type=Path, default=DEFAULT_MAPPING)
    parser.add_argument("--limit", type=int, default=15)
    parser.add_argument("--min-score", type=float, default=28.0)
    parser.add_argument("--json", action="store_true", help="输出 JSON")
    return parser


def main() -> int:
    args = build_parser().parse_args()
    if args.limit < 1:
        print("错误：--limit 必须大于 0", file=sys.stderr)
        return 2
    if not args.mapping.is_file():
        print(f"错误：映射文件不存在：{args.mapping}", file=sys.stderr)
        return 2
    try:
        repositories = parse_mapping(args.mapping)
    except (OSError, UnicodeError, ValueError) as exc:
        print(f"错误：{exc}", file=sys.stderr)
        return 2
    results = search(args.query, repositories, args.limit, args.min_score)
    if args.json:
        print(json.dumps([asdict(item) for item in results], ensure_ascii=False, indent=2))
    else:
        print_table(results)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
