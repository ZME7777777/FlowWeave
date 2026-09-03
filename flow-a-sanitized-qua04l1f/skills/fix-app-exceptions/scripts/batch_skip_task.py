#!/usr/bin/env python3
"""Atomically skip TASK.md queue rows using title-level metadata only."""

from __future__ import annotations

import argparse
import datetime as dt
import re
import sys
from pathlib import Path

KNOWN_STATUSES = {"未开始", "待处理", "处理中", "阻塞", "已完成", "已跳过"}
ACTIVE = {"待处理", "处理中", "阻塞"}
ELIGIBLE = {"未开始", "待处理"}


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser()
    p.add_argument("task", type=Path)
    p.add_argument("--ids", help="Comma-separated fingerprint IDs")
    p.add_argument("--app-contains", help="Case-insensitive literal substring matched against app/repo")
    p.add_argument("--title-contains", help="Case-insensitive literal substring matched against title")
    p.add_argument("--confirm-text", required=True)
    p.add_argument("--time", help="Audit time; defaults to current local time")
    p.add_argument("--dry-run", action="store_true")
    return p.parse_args()


def split_cells(line: str) -> list[str]:
    return [cell.strip() for cell in line.strip().strip("|").split("|")]


def contains(value: object, needle: str | None) -> bool:
    return needle is None or needle.casefold() in str(value).casefold()


def main() -> int:
    args = parse_args()
    text = args.task.read_text(encoding="utf-8")
    lines = text.splitlines()
    wanted = {x.strip().upper() for x in (args.ids or "").split(",") if x.strip()}
    if not (wanted or args.app_contains or args.title_contains):
        raise SystemExit("Provide at least one of --ids, --app-contains, --title-contains")

    in_queue = False
    rows: list[dict[str, object]] = []
    for i, line in enumerate(lines):
        if re.match(r"^##+\s+(Task queue|任务队列)\s*$", line, re.I):
            in_queue = True
            continue
        if in_queue and line.startswith("##"):
            break
        if not in_queue or not re.match(r"^\|\s*\d+\s*\|\s*F\d+\s*\|", line):
            continue
        cells = split_cells(line)
        if len(cells) < 6:
            continue
        rows.append({
            "index": i,
            "id": cells[1].upper(),
            "app": cells[2],
            "title": cells[3],
            "status": cells[-1],
            "cells": cells,
        })

    if not rows:
        raise SystemExit("No task queue rows found")
    ids = [str(row["id"]) for row in rows]
    duplicates = sorted({item for item in ids if ids.count(item) > 1})
    if duplicates:
        raise SystemExit("Duplicate queue IDs: " + ",".join(duplicates))
    unknown = [f"{row['id']}({row['status']})" for row in rows if row["status"] not in KNOWN_STATUSES]
    if unknown:
        raise SystemExit("Unknown queue statuses: " + ",".join(unknown))
    active_before = [row for row in rows if row["status"] in ACTIVE]
    if len(active_before) > 1:
        raise SystemExit("Multiple active items: " + ",".join(str(row["id"]) for row in active_before))

    matched: list[dict[str, object]] = []
    excluded: list[str] = []
    for row in rows:
        selected = (
            (not wanted or row["id"] in wanted)
            and contains(row["app"], args.app_contains)
            and contains(row["title"], args.title_contains)
        )
        if not selected:
            continue
        status = str(row["status"])
        if status in ELIGIBLE:
            matched.append(row)
        else:
            excluded.append(f"{row['id']}({status})")

    if not matched:
        print("matched=none")
        print("excluded=" + (",".join(excluded) or "none"))
        return 2

    for row in matched:
        cells = list(row["cells"])
        cells[-1] = "已跳过"
        lines[int(row["index"])] = "| " + " | ".join(cells) + " |"
        row["status"] = "已跳过"

    active = [row for row in rows if row["status"] in ACTIVE]
    if active:
        next_id = str(active[0]["id"])
    else:
        next_row = next((row for row in rows if row["status"] == "未开始"), None)
        next_id = "无"
        if next_row:
            cells = list(next_row["cells"])
            cells[-1] = "待处理"
            lines[int(next_row["index"])] = "| " + " | ".join(cells) + " |"
            next_row["status"] = "待处理"
            next_id = str(next_row["id"])

    stamp = args.time or dt.datetime.now().astimezone().strftime("%Y-%m-%d %H:%M:%S %z")
    batch_id = "BS-" + dt.datetime.now().strftime("%Y%m%d%H%M%S")
    conditions = []
    if wanted:
        conditions.append(f"IDs={','.join(sorted(wanted))}")
    if args.app_contains:
        conditions.append(f"app contains {args.app_contains!r}")
    if args.title_contains:
        conditions.append(f"title contains {args.title_contains!r}")
    condition = " AND ".join(conditions)
    audit = (
        f"- `{batch_id}`：时间={stamp}；原始确认=`{args.confirm_text}`；条件=`{condition}`；"
        f"命中=`{','.join(str(r['id']) for r in matched)}`；排除=`{','.join(excluded) or '无'}`；"
        f"预读=`未读取正文/代码`；下一项=`{next_id}`。"
    )
    headings = ("## Batch skip audit", "## 批量跳过审计")
    heading = next((item for item in headings if item in lines), None)
    if heading:
        pos = lines.index(heading) + 1
        while pos < len(lines) and not lines[pos].startswith("## " ):
            pos += 1
        lines[pos:pos] = ["", audit]
    else:
        queue_end = next((i for i, line in enumerate(lines) if in_queue and line.startswith("## ") and i > int(rows[-1]["index"])), len(lines))
        lines[queue_end:queue_end] = ["", headings[1], "", audit, ""]

    output = "\n".join(lines) + ("\n" if text.endswith("\n") else "")
    print("matched=" + ",".join(str(r["id"]) for r in matched))
    print("excluded=" + (",".join(excluded) or "none"))
    print("next=" + next_id)
    if not args.dry_run:
        tmp = args.task.with_suffix(args.task.suffix + ".tmp")
        tmp.write_text(output, encoding="utf-8")
        tmp.replace(args.task)
    return 0


if __name__ == "__main__":
    sys.exit(main())
