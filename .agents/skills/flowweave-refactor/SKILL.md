---
name: flowweave-refactor
description: 从持久化任务队列恢复并按用户选择的单项或任务批次推进 FlowWeave 的 OpenHands-first、产物驱动重构。用于新会话续跑、修正 FlowWeave 对 OpenHands 的错误或重复实现、补齐产品必需能力、适配固定只读源码提交、连续完成一个执行批次并交接状态；T1-T8 只做基本代码验证，功能、集成和真实运行验收统一集中到 T9。
---

# FlowWeave OpenHands-first 重构

## 语言

- 全程使用中文回复，包括进度、问题和最终交接；用户明确要求其他语言时除外。
- 代码、命令、路径、日志、标识符、协议字段和源码引文保持原样。

## 恢复现场

1. 完整读取所有适用的 `AGENTS.md`、本 Skill、`docs/openhands-agent-server-design.md`、`docs/openhands-capability-enhancement-roadmap.md`、`docs/openhands-refactor-task-list.md` 和 `docs/openhands-refactor-audit.md`。两份设计文档每次新会话都必须从头到尾读取，不得只读第 11 章、标题、摘要或“下一切片”。
2. 检查当前 Goal（若有）、`git status --short --branch`、`git diff --stat`、相关未提交 diff、Alembic heads、实现和测试。保留所有既有改动，不假设文档状态天然正确。
3. 运行 `python .agents/skills/flowweave-refactor/scripts/validate_state.py`，再核对两份设计文档、原子任务、审计矩阵和实际实现。路线图定义产品目标与产物驱动边界，Agent Server 设计定义差异和接入原则，固定源码提交与镜像定义 OpenHands 实际契约，源码/迁移定义 FlowWeave 当前实现事实；不得让其中任一来源被过时摘要替代。
4. 强制满足两个唯一性约束：恰好一个顶层任务为 `IN_PROGRESS`，恰好一个“当前执行批次”。批次包含一个或多个标为 `CURRENT` 的原子任务，并全部属于该顶层任务。只能执行当前批次；`DONE`、`SKIP`、`IMPLEMENTED`、`COMPLETE` 和已有通过证据的任务不得重跑，除非相关源码/契约已变化、验证已失效，或正在执行 T9。
5. 若批次中的 `CURRENT` 已被源码和测试实际完成但文档滞后，补齐验证后标为 `DONE`，继续处理批次内其余 `CURRENT`；整个批次处理完才交接。没有用户指定新批次时，将有序队列中第一个前置条件满足且未跳过的 `READY` 任务作为下一批次的唯一成员。
6. 只有可复现外部阻塞才使用 `BLOCKED`/`PAUSED`，并记录证据、安全降级、责任边界和明确解锁条件。产品必做域不得改成“不接入”或从队列删除。
7. 用户可以无条件把单个任务、任务列表、连续范围或整批任务标为 `SKIP`。`SKIP` 不要求理由、证据、审计说明或恢复条件；若跳过当前批次的部分成员，只从批次移除这些成员；若批次因此变空，再把下一个未跳过且可执行的 `READY` 任务提升为 `CURRENT`。
8. 用户可以选择单个任务、任务列表、连续范围或同一顶层任务内的整组任务作为执行批次。把所选且可执行的任务统一标为 `CURRENT`；批次内按依赖顺序连续实施，不因完成其中一个最小切片而停止。未满足前置条件的任务不得加入批次。

## 产品与架构准则

- 坚持产物驱动运行：Flow、Snapshot、Node Run、Attempt、Gate、Artifact Version 和验收属于 FlowWeave；Agent、Tool、Skill、Plugin、MCP、Hook、Task 子 Agent、Condenser、Memory、Critic、Fork、ACP 及其事件生命周期由 OpenHands 原生执行。
- FlowWeave 只补控制面：不可变版本和 digest、Snapshot Runtime Manifest、权限、策略、审批、Secret Reference、资源隔离、预算、审计和 Artifact 投影。
- 优先删除或替换提示词协议、自由文本 JSON、平台自建执行器、文本历史伪分叉、浮动来源和其他重复实现；不得为了快速接通再造 OpenHands 已有能力。
- 两份设计文档与固定源码出现差异时，保留产品目标，按固定源码的正式契约调整接入方案并更新持久化文档；不得因实现方便而缩减产品范围，也不得仅因 OpenHands 存在某接口而扩张范围。

## 第 11 章产品范围

以下能力是用户明确确认的本次产品必做域，必须在覆盖账本中落到原子任务和证据：Tool Action 确认、长会话上下文、MCP 验证与 OAuth、费用与可观测性、实时事件、Conversation 分支、Browser、Agent 工具集与 Tool Policy、原生子 Agent、Skills/Plugins/Marketplace、Agent/LLM Profile、ACP Agent、直接 Bash/File/Git/Workspace/Trajectory Runtime API。任务清单中已有明确产品范围的 Critic/Goal、VSCode/Desktop 和能力协商继续按编号切片闭环；不得仅因 OpenHands 提供某项能力而扩张产品范围。

- 产品必做域通常应进入 `DONE` 后等待 T9、`COMPLETE`，或有正式证据和解锁条件的 `UPSTREAM_BLOCKED`；用户明确选择时可直接标为 `SKIP`。不得用 `DECIDED_NO`、模糊延期或静默删除代替这些状态。
- `UPSTREAM_BLOCKED` 不等于完成：保持 fail closed，并在后续正式能力出现或用户明确授权独立 OpenHands fork 后恢复。
- 第 11.17 节中未被用户确认为必做、且不符合 FlowWeave 单一事实来源、安全边界或当前规模需求的接口，可以标为 `DECIDED_NO`，但必须逐项记录理由和回归约束，不得用一个“大类不接入”概括。

## 实现一个执行批次

1. 只完成任务清单中标记为 `CURRENT` 的当前批次。按依赖顺序连续完成批次内各原子任务，整个批次完成后停止；不得实现批次外的 `READY` 任务。任务条目中的行为、恢复、集成和真实 Runtime 条件是 T9 功能验收条件，不要求普通批次当场执行。
2. 仅修改 FlowWeave，并适配任务清单固定的只读 OpenHands 源码提交。除非后续任务获得明确授权，不得创建、修改或切换 OpenHands fork/工作树。
3. 优先使用固定源码已有的正式类型、事件、API 和生命周期。正式能力缺失时保持 fail closed、记录 `UPSTREAM_BLOCKED`，不得在 FlowWeave 中发明私有 HTTP、JSON、提示词、文本协议或重复执行器。
4. 目标 Runtime 事实必须来自固定源码提交和由其构建的镜像；v1.40.0 只作历史兼容证据。相关源码或契约未变化时复用已有证据。
5. 新增 API、UI、迁移、安全或恢复代码时，在所属原子任务内完成可编译、可加载的最小实现，不留下明显半成品；同一批次可以连续完成多个原子任务，跨能力页面仍留给任务清单明确列出的 T8 任务，功能正确性统一由 T9 验证。
6. 未经明确要求，不执行 reset、restore、clean、stash、覆盖、删除、commit、push 或创建 PR。

## T1-T8 基本代码门禁

普通执行批次只运行与批次改动相称的基本代码健康检查：

- 改动文件的 formatter/linter 或语法检查；
- 受影响文件或包的最窄 typecheck/compile；
- API 变更只生成并检查 schema/OpenAPI 能成功生成，不跑端到端 API 行为；
- 新增迁移只检查可导入、单一 head 和升级到新 head，不跑历史迁移矩阵；
- `git diff --check`.

T1-T8 不运行行为单元测试、集成测试、恢复/竞态矩阵、真实 Runtime、容器 smoke、浏览器链路、E2E、平台全量测试、全仓 Ruff/Pyright、完整 Web build 或历史迁移矩阵；全部集中到 T9。仅有两个即时例外：安全/fail-closed 改动必须用最小拒绝路径证明不会放开危险行为；新增 OpenHands 契约假设必须用固定源码或最小镜像探针证明正式契约存在。例外检查不得扩张为功能验收。

`DONE` 和顶层 `IMPLEMENTED` 只表示代码已经落地并通过上述基本门禁，不表示功能已验收。只有 T9 可以运行任务条目中的功能验收条件并把状态晋级为 `COMPLETE`。

## 持久化交接

1. 更新 `docs/openhands-refactor-task-list.md`：批次中完成的任务改为 `DONE`；用户跳过的任务只改为 `SKIP`，不追加说明。整个批次结束后，若用户未指定下一批次，把有序队列中第一个未跳过且可执行的 `READY` 任务改为 `CURRENT`，并同步文件顶部的当前批次。
2. 更新 `docs/openhands-refactor-audit.md`：只写实现事实、仍存在的缺口、正式上游阻塞、能力矩阵和唯一下一指针；详细队列只在任务清单维护，不把计划写成现状。
3. 重新检查源码、迁移头和工作树，并再次运行 `python .agents/skills/flowweave-refactor/scripts/validate_state.py`。所有产品必做域必须能追溯到 `DONE/CURRENT/READY/SKIP/UPSTREAM_BLOCKED` 原子项，且不得存在无编号的大类待办。
4. 顶层实现任务通过全部原子切片门禁后标为 `IMPLEMENTED`；只有 T9 的集中验证可以晋级 `COMPLETE`。
5. 最终回复说明：完成的执行批次及其原子任务、变更文件、基本代码检查、统一留到 T9 的功能验证、当前状态、下一执行批次，以及任何 `UPSTREAM_BLOCKED` 的解锁条件。
