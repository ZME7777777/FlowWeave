#!/usr/bin/env python3
"""Create and verify a Git-commit provenance baseline for a FlowWeave node.

The script is intentionally read-only with respect to every Git repository it
inspects.  A node calls ``snapshot`` before editing, then ``verify`` after its
commits.  ``verify`` emits a bounded JSON report suitable for upload as the
node's ``git_tag_report`` FILE output and review by an END Prompt Gate.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any


SCHEMA_VERSION = 1
CONTROLLED_DOMAINS = (
    "gitlab.inzwc.com",
    "bss-git.hszq8.com",
    "internal-git.hszq8.com",
)
REQUIRED_TAG = "AI_Generated"
TAG_PATTERN = re.compile(r"\[HST_AI_Tag:\s*AI_Generated\]")


class AuditError(RuntimeError):
    """Raised when the repository set cannot be audited safely."""


@dataclass(frozen=True)
class Repository:
    path: Path
    relative_path: str
    head: str | None
    origin_url: str


def run_git(repo: Path, *args: str, allow_failure: bool = False) -> str:
    completed = subprocess.run(
        ["git", "-C", str(repo), *args],
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )
    if completed.returncode and not allow_failure:
        message = completed.stderr.strip() or completed.stdout.strip() or "unknown Git error"
        raise AuditError(f"{repo}: git {' '.join(args)} failed: {message}")
    return completed.stdout.strip()


def git_head(repo: Path) -> str | None:
    value = run_git(repo, "rev-parse", "--verify", "HEAD", allow_failure=True)
    return value or None


def origin_url(repo: Path) -> str:
    return run_git(repo, "remote", "get-url", "origin", allow_failure=True)


def discover_repositories(scope: Path) -> list[Repository]:
    """Discover nested repositories too, while never traversing ``.git`` data."""

    repositories: dict[Path, Repository] = {}
    for directory, dirnames, filenames in os.walk(scope, followlinks=False):
        if ".git" not in dirnames and ".git" not in filenames:
            continue
        candidate = Path(directory)
        top_level = Path(run_git(candidate, "rev-parse", "--show-toplevel")).resolve()
        if not top_level.is_relative_to(scope):
            raise AuditError(f"{top_level} resolves outside audit scope {scope}")
        repositories[top_level] = Repository(
            path=top_level,
            relative_path="." if top_level == scope else str(top_level.relative_to(scope)),
            head=git_head(top_level),
            origin_url=origin_url(top_level),
        )
        # This skips Git internals only; nested worktrees/repositories remain discoverable.
        dirnames[:] = [name for name in dirnames if name != ".git"]
    return sorted(repositories.values(), key=lambda item: item.relative_path)


def controlled(origin: str) -> bool:
    return any(domain in origin for domain in CONTROLLED_DOMAINS)


def worktree_dirty(repo: Path) -> bool:
    return bool(run_git(repo, "status", "--porcelain=v1", "--untracked-files=all"))


def commits_after(repo: Path, baseline_head: str | None, current_head: str | None) -> list[str]:
    if current_head is None:
        if baseline_head is None:
            return []
        raise AuditError("HEAD disappeared after the baseline was created")
    if baseline_head is None:
        return run_git(repo, "rev-list", "--reverse", current_head).splitlines()
    if baseline_head == current_head:
        return []
    ancestor = subprocess.run(
        ["git", "-C", str(repo), "merge-base", "--is-ancestor", baseline_head, current_head],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
        text=True,
        check=False,
    )
    if ancestor.returncode != 0:
        raise AuditError(
            "baseline HEAD is not an ancestor of current HEAD; history was rewritten or switched"
        )
    return run_git(repo, "rev-list", "--reverse", f"{baseline_head}..{current_head}").splitlines()


def commit_subject(repo: Path, commit: str) -> str:
    return run_git(repo, "show", "-s", "--format=%s", commit)


def commit_has_required_tag(repo: Path, commit: str) -> bool:
    return bool(TAG_PATTERN.search(run_git(repo, "show", "-s", "--format=%B", commit)))


def write_json(payload: dict[str, Any], output: Path | None) -> None:
    encoded = json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    if output is None:
        sys.stdout.write(encoded)
        return
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(encoded, encoding="utf-8")


def snapshot(scope: Path, output: Path | None) -> int:
    repositories = discover_repositories(scope)
    write_json(
        {
            "schema_version": SCHEMA_VERSION,
            "kind": "flowweave_hst_ai_tag_baseline",
            "scope": str(scope),
            "repositories": [
                {
                    "path": repo.relative_path,
                    "head": repo.head,
                    "origin_url": repo.origin_url,
                }
                for repo in repositories
            ],
        },
        output,
    )
    return 0


def verify(scope: Path, baseline_path: Path, output: Path | None, strict: bool) -> int:
    try:
        baseline = json.loads(baseline_path.read_text(encoding="utf-8"))
        if baseline.get("schema_version") != SCHEMA_VERSION or baseline.get("kind") != "flowweave_hst_ai_tag_baseline":
            raise AuditError("baseline has an unsupported schema or kind")
        if Path(str(baseline.get("scope") or "")).resolve() != scope:
            raise AuditError("baseline scope does not match the verification scope")
        baseline_repositories = {
            str(item["path"]): item
            for item in baseline.get("repositories", [])
            if isinstance(item, dict) and isinstance(item.get("path"), str)
        }
    except (OSError, ValueError, TypeError, KeyError) as exc:
        raise AuditError(f"cannot read baseline {baseline_path}: {exc}") from exc

    reports: list[dict[str, Any]] = []
    violations: list[dict[str, str]] = []
    audit_errors: list[dict[str, str]] = []
    seen: set[str] = set()
    checked_commit_count = 0

    for repo in discover_repositories(scope):
        seen.add(repo.relative_path)
        baseline_repo = baseline_repositories.get(repo.relative_path)
        report: dict[str, Any] = {
            "path": repo.relative_path,
            "baseline_head": baseline_repo.get("head") if baseline_repo else None,
            "head": repo.head,
            "origin_url": repo.origin_url,
            "controlled_repository": controlled(repo.origin_url),
            "commits": [],
        }
        if baseline_repo is None:
            report["state"] = "UNVERIFIABLE_REPOSITORY"
            report["detail"] = "repository was not present when the baseline was created"
            audit_errors.append({"repository": repo.relative_path, "reason": report["detail"]})
            reports.append(report)
            continue
        if str(baseline_repo.get("origin_url") or "") != repo.origin_url:
            report["state"] = "ORIGIN_CHANGED"
            report["detail"] = "origin URL changed after the baseline was created"
            audit_errors.append({"repository": repo.relative_path, "reason": report["detail"]})
            reports.append(report)
            continue
        if worktree_dirty(repo.path):
            report["state"] = "UNCOMMITTED_CHANGES"
            report["detail"] = "working tree is not clean; commit or remove every change before END Gate"
            violations.append({"repository": repo.relative_path, "reason": report["detail"]})
            reports.append(report)
            continue
        try:
            commits = commits_after(repo.path, baseline_repo.get("head"), repo.head)
        except AuditError as exc:
            report["state"] = "HISTORY_UNVERIFIABLE"
            report["detail"] = str(exc)
            audit_errors.append({"repository": repo.relative_path, "reason": str(exc)})
            reports.append(report)
            continue
        if not commits:
            report["state"] = "UNCHANGED"
            reports.append(report)
            continue
        if not controlled(str(baseline_repo.get("origin_url") or "")):
            report["state"] = "SKIPPED_UNCONTROLLED_REPOSITORY"
            report["detail"] = "origin is outside the original HST commit-msg hook domain allowlist"
            report["commits"] = [{"id": commit, "status": "NOT_APPLICABLE"} for commit in commits]
            reports.append(report)
            continue
        invalid = False
        for commit in commits:
            checked_commit_count += 1
            valid = commit_has_required_tag(repo.path, commit)
            entry = {
                "id": commit,
                "subject": commit_subject(repo.path, commit),
                "status": "PASS" if valid else "FAIL",
            }
            report["commits"].append(entry)
            if not valid:
                invalid = True
                violations.append(
                    {
                        "repository": repo.relative_path,
                        "commit": commit,
                        "reason": "missing exact [HST_AI_Tag: AI_Generated] tag",
                    }
                )
        report["state"] = "TAG_VIOLATION" if invalid else "COMPLIANT"
        reports.append(report)

    for relative_path in sorted(set(baseline_repositories) - seen):
        audit_errors.append(
            {
                "repository": relative_path,
                "reason": "repository present at baseline is no longer inside the verification scope",
            }
        )
        reports.append(
            {
                "path": relative_path,
                "baseline_head": baseline_repositories[relative_path].get("head"),
                "head": None,
                "origin_url": baseline_repositories[relative_path].get("origin_url", ""),
                "controlled_repository": controlled(str(baseline_repositories[relative_path].get("origin_url", ""))),
                "commits": [],
                "state": "MISSING_REPOSITORY",
            }
        )

    decision = "ERROR" if audit_errors else "FAIL" if violations else "PASS"
    summary = {
        "PASS": "所有可审计的受控仓库新增提交均包含 AI_Generated 标签，且工作区干净。",
        "FAIL": "发现未提交变更或不符合 AI_Generated 标签规范的提交。",
        "ERROR": "无法完整审计仓库范围或提交历史，已按失败关闭。",
    }[decision]
    payload = {
        "schema_version": SCHEMA_VERSION,
        "kind": "flowweave_hst_ai_tag_report",
        "decision": decision,
        "summary": summary,
        "policy": {
            "required_tag": "[HST_AI_Tag: AI_Generated]",
            "controlled_origin_domains": list(CONTROLLED_DOMAINS),
            "uncontrolled_repository_behavior": "SKIPPED_UNCONTROLLED_REPOSITORY",
        },
        "scope": str(scope),
        "checked_commit_count": checked_commit_count,
        "repositories": reports,
        "violations": violations,
        "audit_errors": audit_errors,
    }
    write_json(payload, output)
    return 0 if decision == "PASS" or not strict else 1


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)
    for command in ("snapshot", "verify"):
        subparser = subparsers.add_parser(command)
        subparser.add_argument("--scope", required=True, type=Path, help="node workspace root to audit")
        subparser.add_argument("--output", type=Path, help="write JSON to this file instead of stdout")
    verify_parser = subparsers.choices["verify"]
    verify_parser.add_argument("--baseline", required=True, type=Path)
    verify_parser.add_argument(
        "--strict",
        action="store_true",
        help="return exit code 1 for a FAIL or ERROR report (default still emits a report and exits 0)",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    scope = args.scope.resolve()
    if not scope.is_dir():
        raise AuditError(f"audit scope is not a directory: {scope}")
    if args.command == "snapshot":
        return snapshot(scope, args.output)
    return verify(scope, args.baseline, args.output, args.strict)


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except AuditError as exc:
        error = {
            "schema_version": SCHEMA_VERSION,
            "kind": "flowweave_hst_ai_tag_report",
            "decision": "ERROR",
            "summary": "Git 提交标签审计无法执行，已按失败关闭。",
            "audit_errors": [{"reason": str(exc)}],
        }
        sys.stdout.write(json.dumps(error, ensure_ascii=False, indent=2, sort_keys=True) + "\n")
        raise SystemExit(2) from None
