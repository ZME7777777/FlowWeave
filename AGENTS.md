# FlowWeave 协作指南

本文件适用于整个 FlowWeave 仓库，并补充上级 `/Users/zhengmengen/WorkSpace/AGENTS.md`。处理任意子目录时，若存在更近的 `AGENTS.md`，继续优先遵守离目标文件最近的说明。

## OpenHands-first 架构原则

- FlowWeave 的产品设计、用户流程和业务边界是需求来源；OpenHands 是 Agent 执行能力的实现依赖，
  不是产品能力清单。不得以覆盖 OpenHands 全部能力、API 或版本差异为目标扩张 FlowWeave 范围。
- 接入顺序必须是：先确认 FlowWeave 的具体产品需求和当前设计缺口，再识别平台重复实现或未充分
  利用的 OpenHands 正式能力，最后针对该契约做最小接入与验证。没有 FlowWeave 产品需求的
  OpenHands 能力不进入路线图；版本差异只在具体切片需要时按需取证，不做前置式全量盘点。
- FlowWeave 是控制面，只负责能力治理、不可变版本冻结、权限、策略、审批、审计、资源隔离和业务投影。
- Tool、Skill、Plugin、MCP、Hook、Agent Definition、Task 子 Agent、Condenser、Memory、Critic、Fork 和 ACP 等执行能力应由 OpenHands 正式类型、事件、API 和生命周期实现。
- 不得用提示词、私有控制 JSON、文本约定或平台自建执行器模拟 OpenHands 已提供的能力。
- Runtime 输入必须可追溯到固定 version、digest、blob/hash 和 Snapshot Runtime Manifest；浮动来源、隐式环境状态和明文 Secret 不得进入 Runtime。
- 事件关联必须使用 OpenHands 正式的 `id`、`parent_id`、`action_id`、`tool_call_id`、cursor 等字段，不得按事件顺序、名称或文本猜测。

## OpenHands 当前源码适配基线

当前重构阶段以 OpenHands 源码仓库的固定 commit
`f09e03eac772290feeb51b7d7390ffaefeca1a09`（审计时 `v1.42.0-1-gf09e03eac`）为目标能力事实，
只修改 FlowWeave，并仅为已确认的 FlowWeave 产品需求适配该源码的正式类型、事件、API 和生命周期。OpenHands
源码工作树保持只读；不得在当前 T1-T9 主链中创建 fork、修改 OpenHands 源码或提前实施二开。

OpenHands 1.40.0（upstream commit `2f27653959f7596769427ee4657247b32c94504e`）只保留为历史
兼容与回归基线，不是当前目标能力集合。未来允许二开，但必须由后续任务明确授权，并在独立、
可审计的 OpenHands fork 中实现正式契约；无论是否二开，都不得在 FlowWeave 平台层新增私有 HTTP、
控制 JSON、文本协议或重复执行器。

当前已验证的目标镜像从摘要锁定的固定源码提交构建：

- `infra/openhands/pyproject.toml` 固定：
  - `openhands-agent-server==1.42.0`
  - `openhands-sdk==1.42.0`
  - `openhands-tools==1.42.0`
  - `openhands-workspace==1.42.0`
- 固定运行时镜像：`flowweave-openhands-runtime:1`
- 镜像内安装路径：`/runtime/.venv/lib/python3.13/site-packages/`
- 契约探针：`infra/openhands/contract_check.py`
- 构建和验证：

```bash
make openhands-image
make openhands-contract-check
make openhands-smoke
```

source lock 和运行时契约已经迁移到上述固定当前源码 commit。每个运行时版本必须冻结并
可追溯：repository、source commit、源码归档 digest、四个 Python 包版本、镜像 digest 和契约探针
结果。只有具体 FlowWeave 产品切片涉及正式类型、字段、默认值、事件、HTTP 路由或生命周期时，才必须针对该固定源码 commit 和
实际镜像取证，不得凭记忆、浮动 `main` 或最新版文档推断。

## 本地 OpenHands 源码

### SDK / Agent Server / Tools / Workspace（上游基线源码）

- 仓库：`/Users/zhengmengen/WorkSpace/openhands/software-agent-sdk`
- Remote：`https://github.com/OpenHands/software-agent-sdk.git`
- 当前工作树：`main`，审计时 HEAD 为 `f09e03eac772290feeb51b7d7390ffaefeca1a09`
  （`v1.42.0-1-gf09e03eac`）；这是当前 FlowWeave 适配目标。
- 历史兼容 tag：`v1.40.0`；commit：`2f27653959f7596769427ee4657247b32c94504e`。
- 相关包目录：
  - `openhands-agent-server/`
  - `openhands-sdk/`
  - `openhands-tools/`
  - `openhands-workspace/`

该现有工作树必须保持只读，不要切换分支、reset、clean、修改文件或用其承载 FlowWeave 二开。
当前能力判断优先读取固定目标 commit；历史差异才读取 v1.40.0：

```bash
git -C /Users/zhengmengen/WorkSpace/openhands/software-agent-sdk \
  show f09e03eac772290feeb51b7d7390ffaefeca1a09:<相对路径>

git -C /Users/zhengmengen/WorkSpace/openhands/software-agent-sdk \
  grep -n '<模式>' f09e03eac772290feeb51b7d7390ffaefeca1a09 -- \
  openhands-agent-server openhands-sdk openhands-tools openhands-workspace
```

需要跨多个文件检索时直接使用固定 commit 的 `git show` / `git grep`；当前任务不得为 OpenHands
创建可写 worktree 或 fork。未来若明确启动二开，必须另立任务并使用独立、可提交、可审计的 fork，
同时冻结 upstream base、fork commit、source digest 和兼容测试。

### OpenHands 应用仓库（非 1.40.0 SDK 契约来源）

- 仓库：`/Users/zhengmengen/WorkSpace/openhands/OpenHands`
- Remote：`https://github.com/OpenHands/OpenHands.git`
- 当前工作树：`main`，当前 describe 为 `v1.10.0`。

该仓库可用于理解 OpenHands 产品集成背景，但不能证明 SDK 目标 commit 的行为。协议判断优先使用
`software-agent-sdk` 的固定目标 commit 和由其构建的实际镜像。

## OpenHands 证据优先级

从强到弱依次为：

1. 由固定目标源码 commit 构建的部署镜像、source provenance、可执行契约探针和真实 smoke。
2. 固定目标 commit `f09e03eac772290feeb51b7d7390ffaefeca1a09` 的源码和测试。
3. FlowWeave 的 source lock、Dockerfile、overlay、适配器和契约测试。
4. upstream `v1.40.0` 源码、测试和既有镜像证据，仅用于历史兼容回归。
5. 与目标 commit 明确匹配的官方文档和设计记录；浮动 `main` 仅作未来参考。

目标源码与当前 1.40.0 镜像不一致时，先把差异作为 FlowWeave 适配工作处理；目标镜像完成后，以其
可复现运行结果为准。v1.40.0 只保留兼容回归，不得覆盖目标源码已经存在的能力。

## 重构任务恢复方式

开始 OpenHands-first 重构子任务时，先读取：

- `docs/openhands-refactor-task-list.md`
- `docs/openhands-refactor-audit.md`
- 当前 `git status --short --branch` 和未提交 diff
- 最新 Alembic 迁移头及相关测试

文档状态可能滞后；源码、迁移、测试和实际运行结果是当前进度的权威证据。若工作树中已有未闭环切片，先完成该切片；一次只完成一个最小可独立验收子任务，验证并更新任务清单后停止，不自动开始下一项。

## 重构验证分层

- T1-T8 的每个切片只运行与本次改动直接相关的定向测试、改动文件格式/Lint，以及确有需要的窄范围类型、API、迁移、安全或 OpenHands 契约检查；始终运行 `git diff --check`。
- 不要在普通切片中惯例性重复平台全量测试、全仓 Ruff/Pyright、Web 全套 lint/typecheck/build、历史迁移全链或 OpenHands contract/smoke；这些统一由 T9 最终验证门禁执行。
- 不得把必要的即时安全证据推迟到 T9：新增迁移至少证明可加载/可升级到新 head，安全和 fail-closed 改动必须有拒绝路径定向测试，新增 OpenHands 契约假设必须由固定目标源码和实际镜像定向证明。
- 实现门禁通过但尚未经过 T9 的任务标记为 `IMPLEMENTED`；只有 T9 全部门禁通过后才标记 `COMPLETE`。可复现的外部阻塞使用 `PAUSED` 并记录安全降级与解锁条件，不得伪造替代能力。
