#!/usr/bin/env python3
"""校验 FlowWeave 重构任务清单是否可被新会话确定性恢复。"""

from __future__ import annotations

import re
import sys
from pathlib import Path

TOP_LEVEL_RE = re.compile(
    r"^### (?P<title>T\d+ .+?) — (?P<status>PENDING|IN_PROGRESS|PAUSED|IMPLEMENTED|COMPLETE|SKIP)$",
    re.MULTILINE,
)
ATOMIC_RE = re.compile(
    r"^- \*\*(?P<id>T(?:\d+\.\d+|\d+-B\d+)) (?P<title>.+?) — "
    r"(?P<status>DONE|CURRENT|READY|SKIP|UPSTREAM_BLOCKED|DECIDED_NO)\*\*",
    re.MULTILINE,
)
CURRENT_BATCH_LINE_RE = re.compile(r"^- 当前执行批次：(?P<items>.+)$", re.MULTILINE)
CURRENT_BATCH_ID_RE = re.compile(r"`(?P<id>T(?:\d+\.\d+|\d+-B\d+))(?: [^`]*)?`")
LEDGER_REF_RE = re.compile(r"T(?:\d+\.\d+|\d+-B\d+)")

REQUIRED_LEDGER_ROWS = (
    "11.3 Tool Action 确认",
    "11.4 Condenser / Memory",
    "11.5 MCP 验证与 OAuth",
    "11.6 费用与可观测性",
    "11.7 实时事件",
    "11.8 Conversation 分支",
    "11.9 Browser / 直接 Bash / IDE / Desktop",
    "11.10 Tool 集与 Tool Policy",
    "11.11 原生子 Agent",
    "11.12 Skills / Plugins / Marketplace",
    "11.13 Agent/LLM Profile",
    "11.14 ACP Agent",
    "11.16 File/Git/Workspace/Trajectory",
)
REQUIRED_DESIGN_FILES = (
    "docs/openhands-agent-server-design.md",
    "docs/openhands-capability-enhancement-roadmap.md",
)
REQUIRED_SKILL_RULES = (
    "两份设计文档每次新会话都必须从头到尾读取",
    "坚持产物驱动运行",
    "当前执行批次",
    "T1-T8 不运行行为单元测试",
    "全部集中到 T9",
)
REQUIRED_TASK_RULES = (
    "openhands-agent-server-design.md",
    "openhands-capability-enhancement-roadmap.md",
    "当前执行批次",
    "T1-T8 只做基本代码健康检查",
    "统一在 T9 执行",
)


def fail(errors: list[str], message: str) -> None:
    errors.append(message)


def main() -> int:
    repo_root = Path(__file__).resolve().parents[4]
    task_path = repo_root / "docs/openhands-refactor-task-list.md"
    audit_path = repo_root / "docs/openhands-refactor-audit.md"
    skill_path = repo_root / ".agents/skills/flowweave-refactor/SKILL.md"
    task_text = task_path.read_text(encoding="utf-8")
    audit_text = audit_path.read_text(encoding="utf-8")
    skill_text = skill_path.read_text(encoding="utf-8")
    errors: list[str] = []

    for relative_path in REQUIRED_DESIGN_FILES:
        design_path = repo_root / relative_path
        if not design_path.is_file():
            fail(errors, f"缺少必须实时参考的设计文档：{relative_path}")

    for rule in REQUIRED_SKILL_RULES:
        if rule not in skill_text:
            fail(errors, f"Skill 缺少防漂移规则：{rule}")

    for rule in REQUIRED_TASK_RULES:
        if rule not in task_text:
            fail(errors, f"任务清单缺少恢复/门禁规则：{rule}")

    top_levels = list(TOP_LEVEL_RE.finditer(task_text))
    in_progress = [match for match in top_levels if match["status"] == "IN_PROGRESS"]
    terminal_state = (
        bool(top_levels)
        and not in_progress
        and all(match["status"] in {"COMPLETE", "SKIP"} for match in top_levels)
    )
    if len(in_progress) != 1 and not terminal_state:
        fail(
            errors,
            f"执行中必须恰好一个顶层 IN_PROGRESS；终态必须全部 COMPLETE/SKIP，"
            f"当前 IN_PROGRESS 为 {len(in_progress)} 个",
        )

    atomic_matches = list(ATOMIC_RE.finditer(task_text))
    atomic_ids = [match["id"] for match in atomic_matches]
    duplicates = sorted({item for item in atomic_ids if atomic_ids.count(item) > 1})
    if duplicates:
        fail(errors, f"原子任务编号重复：{', '.join(duplicates)}")

    current = [match for match in atomic_matches if match["status"] == "CURRENT"]
    if not current and not terminal_state:
        fail(errors, "当前执行批次必须至少包含一个 CURRENT 任务")
    if current and terminal_state:
        fail(errors, "全部顶层任务完成后不得保留 CURRENT 任务")

    batch_line = CURRENT_BATCH_LINE_RE.search(task_text)
    batch_ids = (
        [match["id"] for match in CURRENT_BATCH_ID_RE.finditer(batch_line["items"])]
        if batch_line is not None
        else []
    )
    current_ids = [match["id"] for match in current]
    if batch_line is None:
        fail(errors, "文件顶部缺少“当前执行批次”指针")
    elif terminal_state and batch_line["items"].strip() != "无。":
        fail(errors, "全部顶层任务完成后当前执行批次必须为“无。”")
    elif not terminal_state and not batch_ids:
        fail(errors, "当前执行批次至少需要一个任务编号")
    elif not terminal_state and len(batch_ids) != len(set(batch_ids)):
        fail(errors, "当前执行批次包含重复任务编号")
    elif not terminal_state and set(batch_ids) != set(current_ids):
        fail(
            errors,
            f"顶部执行批次 {batch_ids} 与 CURRENT {current_ids} 不一致",
        )

    if current and in_progress:
        in_progress_phase = in_progress[0]["title"].split(maxsplit=1)[0]
        mismatched = [
            match["id"]
            for match in current
            if match["id"].split(".", maxsplit=1)[0] != in_progress_phase
        ]
        if mismatched:
            fail(
                errors,
                f"CURRENT {mismatched} 与顶层 IN_PROGRESS {in_progress_phase} 不一致",
            )

    ledger_start = task_text.find("### 第 11 章覆盖账本")
    ledger_end = task_text.find("### T1 ", ledger_start)
    if ledger_start < 0 or ledger_end < 0:
        fail(errors, "缺少完整的第 11 章覆盖账本")
        ledger_text = ""
    else:
        ledger_text = task_text[ledger_start:ledger_end]

    for row in REQUIRED_LEDGER_ROWS:
        if row not in ledger_text:
            fail(errors, f"覆盖账本缺少产品必做域：{row}")

    known_ids = set(atomic_ids)
    for reference in sorted(set(LEDGER_REF_RE.findall(ledger_text))):
        if reference not in known_ids:
            fail(errors, f"覆盖账本引用了不存在的原子任务：{reference}")

    missing_audit_ids = [match["id"] for match in current if match["id"] not in audit_text]
    if missing_audit_ids:
        fail(errors, f"审计文档未同步 CURRENT 批次：{', '.join(missing_audit_ids)}")

    if errors:
        for error in errors:
            print(f"ERROR: {error}", file=sys.stderr)
        return 1

    top_level = in_progress[0]["title"] if in_progress else "COMPLETE"
    print(
        "PASS: "
        f"top_level={top_level}; "
        f"current_batch={','.join(current_ids) or 'none'}; "
        f"atomic_tasks={len(atomic_matches)}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
