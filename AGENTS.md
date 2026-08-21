# FlowWeave 协作指南

本文件适用于整个 FlowWeave 仓库，并补充上级
`/Users/zhengmengen/WorkSpace/AGENTS.md`。若目标目录存在更近的 `AGENTS.md`，优先遵守离目标文件最近的说明。

## OpenHands-first 架构原则

- FlowWeave 的产品设计、用户流程和业务边界是需求来源；OpenHands 是 Agent 执行能力的实现依赖，
  不是产品能力清单。不得以覆盖 OpenHands 全部能力、API 或版本差异为目标扩张 FlowWeave 范围。
- FlowWeave 是控制面，只负责能力治理、不可变版本冻结、权限、策略、审批、审计、资源隔离和业务投影。
- Tool、Skill、Plugin、MCP、Hook、Agent Definition、Task 子 Agent、Condenser、Memory、Critic、Fork 和 ACP
  等执行能力应由 OpenHands 正式类型、事件、API 和生命周期实现。
- 不得用提示词、私有控制 JSON、文本约定、私有 HTTP 或平台自建执行器模拟 OpenHands 已提供的能力。
- Runtime 输入必须可追溯到固定 version、digest、blob/hash 和 Snapshot Runtime Manifest；浮动来源、
  隐式环境状态和明文 Secret 不得进入 Runtime。
- 事件关联必须使用 OpenHands 正式的 `id`、`parent_id`、`action_id`、`tool_call_id`、cursor 等字段，
  不得按事件顺序、名称或文本猜测。

## OpenHands 源码与镜像基线

当前目标能力事实固定为 OpenHands 源码 commit
`f09e03eac772290feeb51b7d7390ffaefeca1a09`（审计时 `v1.42.0-1-gf09e03eac`），只修改 FlowWeave。
OpenHands 源码工作树保持只读；不得在当前 `FR-*` 主线中创建 fork、修改 OpenHands 源码或提前实施二开。

- SDK 源码：`/Users/zhengmengen/WorkSpace/openhands/software-agent-sdk`
- 历史兼容基线：`v1.40.0` / `2f27653959f7596769427ee4657247b32c94504e`
- 固定包版本：`openhands-agent-server==1.42.0`、`openhands-sdk==1.42.0`、
  `openhands-tools==1.42.0`、`openhands-workspace==1.42.0`
- 固定运行时镜像：`flowweave-openhands-runtime:1`
- 契约探针：`infra/openhands/contract_check.py`

能力判断优先读取固定 commit，并只在当前切片确有需要时取证：

```bash
git -C /Users/zhengmengen/WorkSpace/openhands/software-agent-sdk \
  show f09e03eac772290feeb51b7d7390ffaefeca1a09:<相对路径>

git -C /Users/zhengmengen/WorkSpace/openhands/software-agent-sdk \
  grep -n '<模式>' f09e03eac772290feeb51b7d7390ffaefeca1a09 -- \
  openhands-agent-server openhands-sdk openhands-tools openhands-workspace
```

证据优先级从强到弱为：固定源码构建的实际镜像及可执行探针、固定 commit 源码和测试、FlowWeave
source lock 与适配代码、历史兼容源码和镜像、版本明确匹配的官方文档。不得凭记忆或浮动 `main` 推断契约。

## FlowRun Runtime 重构恢复方式

每次开始或恢复任务时依次执行：

1. 完整读取 `docs/flowrun-openhands-runtime-design.md` 和 `docs/flowrun-runtime-task-progress.md`。
2. 检查 `git status --short --branch`、未提交 diff 和当前 Alembic heads。
3. 检查进度文档是否最多只有一个 `CURRENT`，以及当前切片依赖是否全部 `DONE`。
4. 若工作树存在未闭环切片，先完成该切片；不得越过它开始后续切片。
5. 一次只完成一个最小可独立验收切片；实现边界过大时先拆分任务和依赖。
6. 按进度文档要求完成实现、基础检查和状态更新。
7. 切片完成后提交该切片代码和文档；确认提交成功后立即停止，不自动开始下一切片。

源码、迁移、测试和实际运行结果是当前进度的权威证据。旧的 OpenHands 重构文档和历史验收结论不得
替代当前 `FR-*` 任务重新实施与验证。

## 切片验证与提交规则

- `FR-01`–`FR-11` 只运行进度文档允许的最窄语法、解析或编译检查，以及 `git diff --check` 和任务状态
  唯一性核对；不得提前运行集中在 `FR-12` 的业务测试、迁移实跑、完整构建、Runtime、安全或 E2E 门禁。
- `FR-12` 负责进度文档列明的完整故障恢复、安全、契约、迁移和 E2E 验证。
- 提交前必须复核 staged diff，确保只包含当前切片和必要的协作文档变更，不混入无关改动、密钥、缓存
  或生成物。
- 每个完成切片使用独立、可审计的 Git commit；提交信息应包含切片编号和结果，例如
  `feat(runtime): complete FR-01 environment binding`。
- 提交成功是切片收尾的一部分。提交后只报告提交哈希、验证结果和下一可执行切片，不继续实现下一项。
