#!/usr/bin/env python3
"""Filter historical skips and flag previously completed fingerprints."""

from __future__ import annotations

import argparse
import datetime as dt
import difflib
import re
import sys
import unicodedata
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlparse


SKIPPED = "已跳过"
COMPLETED = "已完成"
INITIAL_STATUSES = {"未开始", "待处理"}
ACTIVE_STATUSES = {"处理中", "阻塞"}
AUDIT_HEADING = "## 历史跳过排除审计"
COMPLETED_AUDIT_HEADING = "## 历史已处理匹配审计"


@dataclass
class QueueRow:
    task: Path
    line_index: int
    cells: list[str]
    columns: dict[str, int]
    fingerprint_id: str
    app: str
    title: str
    status: str
    logger: str = ""
    endpoint: str = ""
    root_msg: str = ""
    first_frame: str = ""


@dataclass(frozen=True)
class SkipRule:
    task: Path
    source_app: str
    title_contains: str
    app_contains: str = ""


@dataclass
class Match:
    current: QueueRow
    source_task: Path
    source_id: str
    level: str
    rule: str


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("current_task", type=Path, help="New TASK.md draft to filter")
    parser.add_argument(
        "--history-root",
        action="append",
        required=True,
        type=Path,
        help="Root scanned recursively for historical TASK.md files; repeatable",
    )
    parser.add_argument(
        "--keep-current-id",
        action="append",
        default=[],
        help="Explicitly re-include a current fingerprint ID after user authorization; repeatable",
    )
    parser.add_argument("--override-reason", help="Required with --keep-current-id")
    parser.add_argument("--time", help="Audit time; defaults to current local time")
    parser.add_argument("--dry-run", action="store_true")
    return parser.parse_args()


def strip_markdown(value: str) -> str:
    value = value.strip()
    if len(value) >= 2 and value[0] == value[-1] == "`":
        value = value[1:-1]
    return value.strip()


def normalize(value: str) -> str:
    value = unicodedata.normalize("NFKC", strip_markdown(value)).casefold()
    value = re.sub(r"\s+", " ", value)
    return value.strip()


def normalize_title(value: str) -> str:
    value = normalize(value)
    aliases = (
        (r"jedis\s*pool\s*校验|jedis\s*校验", "jedis pool validation"),
        (r"会员\s*http\s*读取超时|member\s*实时查询超时", "read timed out"),
        (r"connection\s+or\s+outbound\s+has\s+closed|http\s+connection\s+closed|outbound\s+closed", "connection closed"),
    )
    for pattern, replacement in aliases:
        value = re.sub(pattern, replacement, value, flags=re.I)
    value = re.sub(r"\s+", " ", value)
    return value.strip()


def normalize_app(value: str) -> str:
    match = re.search(r"\bhq-[a-z0-9][a-z0-9_-]*\b", normalize(value))
    if match:
        return match.group(0).replace("_", "-")
    return re.split(r"\s*(?:\+|/|,|，)\s*", normalize(value), maxsplit=1)[0]


def normalize_endpoint(value: str) -> str:
    raw = normalize(value)
    if "://" in raw:
        parsed = urlparse(raw)
        raw = parsed.path or raw
    return raw.rstrip("/") or "/"


def split_cells(line: str) -> list[str]:
    return [cell.strip() for cell in line.strip().strip("|").split("|")]


def column_index(headers: list[str], *aliases: str) -> int | None:
    normalized = [normalize(item) for item in headers]
    wanted = {normalize(alias) for alias in aliases}
    return next((index for index, item in enumerate(normalized) if item in wanted), None)


def parse_queue(task: Path) -> list[QueueRow]:
    lines = task.read_text(encoding="utf-8").splitlines()
    in_queue = False
    headers: list[str] | None = None
    columns: dict[str, int] = {}
    rows: list[QueueRow] = []

    for index, line in enumerate(lines):
        if re.match(r"^##+\s+(Task queue|任务队列)\s*$", line, re.I):
            in_queue = True
            continue
        if in_queue and line.startswith("## "):
            break
        if not in_queue or not line.lstrip().startswith("|"):
            continue
        cells = split_cells(line)
        if headers is None:
            id_index = column_index(cells, "ID", "指纹ID")
            status_index = column_index(cells, "状态", "主状态", "status", "main status")
            if id_index is None or status_index is None:
                continue
            headers = cells
            columns = {
                "id": id_index,
                "status": status_index,
                "app": column_index(cells, "应用/仓库", "应用", "app/repo", "app") or 2,
                "title": column_index(cells, "异常指纹标题", "指纹标题", "title") or 3,
            }
            optional = {
                "logger": ("logger",),
                "endpoint": ("endpoint/任务", "endpoint/task"),
                "root_msg": ("root msg", "root message"),
                "first_frame": ("首业务栈", "first business frame"),
            }
            for key, aliases in optional.items():
                found = column_index(cells, *aliases)
                if found is not None:
                    columns[key] = found
            continue
        if set("".join(cells)) <= {"-", ":"}:
            continue
        if len(cells) <= max(columns.values()):
            continue
        fingerprint_id = strip_markdown(cells[columns["id"]]).upper()
        if not re.fullmatch(r"F\d+", fingerprint_id):
            continue
        rows.append(
            QueueRow(
                task=task,
                line_index=index,
                cells=cells,
                columns=columns,
                fingerprint_id=fingerprint_id,
                app=strip_markdown(cells[columns["app"]]),
                title=strip_markdown(cells[columns["title"]]),
                status=strip_markdown(cells[columns["status"]]),
                logger=strip_markdown(cells[columns["logger"]]) if "logger" in columns else "",
                endpoint=strip_markdown(cells[columns["endpoint"]]) if "endpoint" in columns else "",
                root_msg=strip_markdown(cells[columns["root_msg"]]) if "root_msg" in columns else "",
                first_frame=strip_markdown(cells[columns["first_frame"]]) if "first_frame" in columns else "",
            )
        )
    return rows


def canonical_key(row: QueueRow) -> tuple[str, str, str, str, str] | None:
    values = (row.logger, row.endpoint, row.root_msg, row.first_frame)
    if not all(normalize(value) for value in values):
        return None
    return (
        normalize_app(row.app),
        normalize(row.logger),
        normalize_endpoint(row.endpoint),
        normalize(row.root_msg),
        normalize(row.first_frame),
    )


def legacy_key(row: QueueRow) -> tuple[str, str]:
    return normalize_app(row.app), normalize_title(row.title)


def task_default_app(rows: list[QueueRow]) -> str:
    apps = [normalize_app(row.app) for row in rows if normalize_app(row.app)]
    return max(set(apps), key=apps.count) if apps else ""


def extract_rules(task: Path, rows: list[QueueRow]) -> list[SkipRule]:
    if not any(row.status == SKIPPED for row in rows):
        return []
    text = task.read_text(encoding="utf-8")
    source_app = task_default_app(rows)
    rules: list[SkipRule] = []
    for condition in re.findall(r"条件=`([^`]*)`", text):
        title_match = re.search(r"title\s+contains\s+(['\"])(.*?)\1", condition, re.I)
        if not title_match:
            continue
        app_match = re.search(r"app\s+contains\s+(['\"])(.*?)\1", condition, re.I)
        rules.append(
            SkipRule(
                task=task,
                source_app=source_app,
                title_contains=title_match.group(2),
                app_contains=app_match.group(2) if app_match else "",
            )
        )
    return rules


def discover_history(current_task: Path, roots: list[Path]) -> list[Path]:
    current = current_task.resolve()
    found: set[Path] = set()
    for root in roots:
        if root.is_file() and root.name == "TASK.md":
            candidates = [root]
        elif root.is_dir():
            candidates = root.rglob("TASK.md")
        else:
            continue
        for candidate in candidates:
            resolved = candidate.resolve()
            if resolved != current:
                found.add(resolved)
    return sorted(found)


def find_match(
    current: QueueRow,
    historical: list[QueueRow],
    rules: list[SkipRule],
    historical_state: str,
) -> Match | None:
    current_canonical = canonical_key(current)
    if current_canonical:
        for old in historical:
            if canonical_key(old) == current_canonical:
                return Match(current, old.task, old.fingerprint_id, "完整指纹键", historical_state)
    current_legacy = legacy_key(current)
    for old in historical:
        if legacy_key(old) == current_legacy:
            return Match(current, old.task, old.fingerprint_id, "旧账本标题回退", "应用+规范化标题相同")
    current_app = normalize_app(current.app)
    current_title = normalize_title(current.title)
    for rule in rules:
        app_ok = normalize(rule.app_contains) in normalize(current.app) if rule.app_contains else current_app == rule.source_app
        if app_ok and normalize_title(rule.title_contains) in current_title:
            return Match(current, rule.task, "批量规则", "历史批量规则", f"title contains {rule.title_contains!r}")
    return None


def title_subject(value: str) -> str:
    title = normalize_title(value).lstrip("/")
    subject = re.split(r"\s+-\s+", title, maxsplit=1)[0].strip()
    return subject.rsplit("/", maxsplit=1)[-1]


def title_detail_tokens(value: str) -> set[str]:
    detail = title_detail(value)
    generic = {
        "a",
        "an",
        "because",
        "cannot",
        "error",
        "exception",
        "get",
        "invoke",
        "is",
        "npe",
        "null",
        "of",
        "return",
        "the",
        "to",
        "value",
    }
    return {token for token in re.findall(r"[a-z0-9_]+|[\u4e00-\u9fff]+", detail) if token not in generic}


def title_detail(value: str) -> str:
    title = normalize_title(value)
    return re.split(r"\s+-\s+", title, maxsplit=1)[1].strip() if " - " in title else ""


def find_completed_match(current: QueueRow, completed: list[QueueRow]) -> Match | None:
    exact = find_match(current, completed, [], COMPLETED)
    if exact:
        return exact

    current_app = normalize_app(current.app)
    current_title = normalize_title(current.title)
    current_subject = title_subject(current.title)
    current_detail = title_detail(current.title)
    current_tokens = title_detail_tokens(current.title)
    candidates: list[tuple[float, QueueRow]] = []
    for old in completed:
        if normalize_app(old.app) != current_app or title_subject(old.title) != current_subject:
            continue
        old_detail = title_detail(old.title)
        if current_detail and current_detail == old_detail:
            return Match(
                current,
                old.task,
                old.fingerprint_id,
                "旧账本标题回退",
                "应用+标题主体+规范化错误详情相同，仅用于询问、不自动排除",
            )
        old_tokens = title_detail_tokens(old.title)
        if not current_tokens or not old_tokens or not (current_tokens & old_tokens):
            continue
        score = difflib.SequenceMatcher(None, current_title, normalize_title(old.title)).ratio()
        if score >= 0.60:
            candidates.append((score, old))
    if not candidates:
        return None
    score, old = max(candidates, key=lambda item: (item[0], item[1].fingerprint_id))
    return Match(
        current,
        old.task,
        old.fingerprint_id,
        "旧账本标题相似候选",
        f"应用+标题主体相同；标题相似度={score:.3f}，仅用于询问、不自动排除",
    )


def display_path(path: Path) -> str:
    try:
        return str(path.resolve().relative_to(Path.cwd().resolve()))
    except ValueError:
        return str(path.resolve())


def build_audit(
    matches: list[Match],
    history_tasks: list[Path],
    roots: list[Path],
    current_count: int,
    kept: list[str],
    override_reason: str | None,
    stamp: str,
) -> list[str]:
    lines = [
        AUDIT_HEADING,
        "",
        f"- 扫描时间：{stamp}",
        "- 扫描范围：" + "；".join(display_path(root) for root in roots),
        f"- 历史账本：扫描 {len(history_tasks)} 个 `TASK.md`；只读取任务队列表与批量跳过审计，未读取证据正文、日志或代码。",
        "- 排除规则：优先匹配完整指纹键（应用+logger+endpoint/任务+root msg+首业务栈）；旧账本缺少字段时回退到应用+规范化标题；历史明确批量标题规则继续生效。",
        f"- 汇总：原始指纹={current_count}；历史排除={len(matches)}；实际入队={current_count - len(matches)}；明确重新纳入={len(kept)}。",
        "- 队列语义：下表仅为排除审计；这些指纹不进入本批次任务队列，不创建 Item record，也不参与待处理选择。",
    ]
    if kept:
        lines.append(f"- 明确重新纳入：{','.join(sorted(kept))}；授权理由：{override_reason}。")
    lines.extend(
        [
            "",
            "| 当前指纹ID | 应用 | 异常指纹标题 | 历史来源 | 历史ID/规则 | 匹配级别 | 排除依据 |",
            "|---|---|---|---|---|---|---|",
        ]
    )
    if matches:
        for match in matches:
            lines.append(
                "| "
                + " | ".join(
                    [
                        match.current.fingerprint_id,
                        match.current.app.replace("|", "\\|"),
                        match.current.title.replace("|", "\\|"),
                        display_path(match.source_task),
                        match.source_id,
                        match.level,
                        match.rule.replace("|", "\\|"),
                    ]
                )
                + " |"
            )
    else:
        lines.append("| 无 | - | - | - | - | - | 未命中历史已跳过指纹 |")
    return lines


def build_completed_audit(matches: list[Match]) -> list[str]:
    lines = [
        COMPLETED_AUDIT_HEADING,
        "",
        f"- 汇总：历史已处理同指纹={len(matches)}。这些指纹保留在本批次队列，不自动排除。",
        "- 处理门禁：当命中项成为唯一 `待处理` 项时，必须先展示历史来源并询问用户“本次是否跳过”；用户选择不跳过才按正常处理流程读取正文和代码。",
        "- 冲突优先级：同一当前指纹同时命中历史 `已跳过` 与 `已完成` 时，以 `已完成` 复现审查优先，保留入队并询问，不静默排除。",
        "",
        "| 当前指纹ID | 应用 | 异常指纹标题 | 历史来源 | 历史ID | 匹配级别 | 入队依据 |",
        "|---|---|---|---|---|---|---|",
    ]
    if matches:
        for match in matches:
            lines.append(
                "| "
                + " | ".join(
                    [
                        match.current.fingerprint_id,
                        match.current.app.replace("|", "\\|"),
                        match.current.title.replace("|", "\\|"),
                        display_path(match.source_task),
                        match.source_id,
                        match.level,
                        "历史已完成；保留并在轮到时询问是否跳过",
                    ]
                )
                + " |"
            )
    else:
        lines.append("| 无 | - | - | - | - | - | 未命中历史已处理同指纹 |")
    return lines


def apply_filter(
    task: Path,
    matches: list[Match],
    audit: list[str],
    completed_audit: list[str],
) -> None:
    text = task.read_text(encoding="utf-8")
    if AUDIT_HEADING in text or COMPLETED_AUDIT_HEADING in text:
        raise SystemExit("Historical terminal-state audit already exists; refuse to overwrite it")
    lines = text.splitlines()
    excluded_ids = {match.current.fingerprint_id for match in matches}
    for match in matches:
        if re.search(rf"^###\s+{re.escape(match.current.fingerprint_id)}\b", text, re.M):
            raise SystemExit(f"Item record already exists for excluded {match.current.fingerprint_id}")
    rows = parse_queue(task)
    for row in rows:
        if row.status in ACTIVE_STATUSES or row.status not in INITIAL_STATUSES:
            raise SystemExit(f"Queue is not in initialization state: {row.fingerprint_id}={row.status}")
    for index in sorted((row.line_index for row in rows if row.fingerprint_id in excluded_ids), reverse=True):
        del lines[index]

    remaining = [row for row in rows if row.fingerprint_id not in excluded_ids]
    for position, row in enumerate(remaining):
        adjusted_index = row.line_index - sum(1 for match in matches if match.current.line_index < row.line_index)
        cells = split_cells(lines[adjusted_index])
        cells[row.columns["status"]] = "待处理" if position == 0 else "未开始"
        lines[adjusted_index] = "| " + " | ".join(cells) + " |"

    queue_heading = next(
        index for index, line in enumerate(lines) if re.match(r"^##+\s+(Task queue|任务队列)\s*$", line, re.I)
    )
    insert_at = next((index for index in range(queue_heading + 1, len(lines)) if lines[index].startswith("## ")), len(lines))
    lines[insert_at:insert_at] = [""] + audit + [""] + completed_audit + [""]
    output = "\n".join(lines) + ("\n" if text.endswith("\n") else "")
    temporary = task.with_suffix(task.suffix + ".tmp")
    temporary.write_text(output, encoding="utf-8")
    temporary.replace(task)


def main() -> int:
    args = parse_args()
    current_task = args.current_task.resolve()
    if not current_task.is_file():
        raise SystemExit(f"Current TASK.md not found: {current_task}")
    kept = {item.strip().upper() for value in args.keep_current_id for item in value.split(",") if item.strip()}
    if kept and not args.override_reason:
        raise SystemExit("--override-reason is required with --keep-current-id")

    current_rows = parse_queue(current_task)
    if not current_rows:
        raise SystemExit("No current task queue rows found")
    current_text = current_task.read_text(encoding="utf-8")
    if AUDIT_HEADING in current_text or COMPLETED_AUDIT_HEADING in current_text:
        raise SystemExit("Historical terminal-state audit already exists; refuse to overwrite it")
    duplicate_ids = sorted(
        fingerprint_id
        for fingerprint_id in {row.fingerprint_id for row in current_rows}
        if sum(row.fingerprint_id == fingerprint_id for row in current_rows) > 1
    )
    if duplicate_ids:
        raise SystemExit("Duplicate current queue IDs: " + ",".join(duplicate_ids))
    for row in current_rows:
        if row.status in ACTIVE_STATUSES or row.status not in INITIAL_STATUSES:
            raise SystemExit(f"Queue is not in initialization state: {row.fingerprint_id}={row.status}")
    item_records = sorted(set(re.findall(r"^###\s+(F\d+)\b", current_text, re.M | re.I)))
    if item_records:
        raise SystemExit("Current draft already contains Item records: " + ",".join(item_records))
    current_ids = {row.fingerprint_id for row in current_rows}
    unknown_kept = sorted(kept - current_ids)
    if unknown_kept:
        raise SystemExit("--keep-current-id not found in current queue: " + ",".join(unknown_kept))
    history_tasks = discover_history(current_task, args.history_root)
    skipped: list[QueueRow] = []
    completed: list[QueueRow] = []
    rules: list[SkipRule] = []
    for history_task in history_tasks:
        rows = parse_queue(history_task)
        skipped.extend(row for row in rows if row.status == SKIPPED)
        completed.extend(row for row in rows if row.status == COMPLETED)
        rules.extend(extract_rules(history_task, rows))

    candidate_skip_matches: dict[str, Match] = {}
    candidate_completed_matches: dict[str, Match] = {}
    for current in current_rows:
        completed_match = find_completed_match(current, completed)
        if completed_match:
            candidate_completed_matches[current.fingerprint_id] = completed_match
            continue
        skip_match = find_match(current, skipped, rules, SKIPPED)
        if skip_match:
            candidate_skip_matches[current.fingerprint_id] = skip_match

    ineffective_kept = sorted(kept - candidate_skip_matches.keys())
    if ineffective_kept:
        raise SystemExit(
            "--keep-current-id does not match a historical skip and cannot be recorded as re-included: "
            + ",".join(ineffective_kept)
        )
    effective_kept = sorted(kept & candidate_skip_matches.keys())
    matches = [
        candidate_skip_matches[row.fingerprint_id]
        for row in current_rows
        if row.fingerprint_id in candidate_skip_matches and row.fingerprint_id not in kept
    ]
    completed_matches = [
        candidate_completed_matches[row.fingerprint_id]
        for row in current_rows
        if row.fingerprint_id in candidate_completed_matches
    ]

    stamp = args.time or dt.datetime.now().astimezone().strftime("%Y-%m-%d %H:%M:%S %z")
    audit = build_audit(
        matches,
        history_tasks,
        args.history_root,
        len(current_rows),
        effective_kept,
        args.override_reason,
        stamp,
    )
    completed_audit = build_completed_audit(completed_matches)
    print(f"scanned_tasks={len(history_tasks)}")
    print(f"historical_skipped_rows={len(skipped)}")
    print(f"historical_completed_rows={len(completed)}")
    print(f"historical_title_rules={len(rules)}")
    print(f"excluded={len(matches)}")
    print(f"completed_review={len(completed_matches)}")
    print(f"remaining={len(current_rows) - len(matches)}")
    for match in matches:
        print(
            f"exclude {match.current.fingerprint_id} | {match.current.app} | {match.current.title} | "
            f"source={display_path(match.source_task)}#{match.source_id} | level={match.level} | rule={match.rule}"
        )
    for match in completed_matches:
        print(
            f"review-completed {match.current.fingerprint_id} | {match.current.app} | {match.current.title} | "
            f"source={display_path(match.source_task)}#{match.source_id} | level={match.level} | "
            "action=keep-and-ask-whether-to-skip"
        )
    if effective_kept:
        print("re_included=" + ",".join(effective_kept))
    if not args.dry_run:
        apply_filter(current_task, matches, audit, completed_audit)
    return 0


if __name__ == "__main__":
    sys.exit(main())
