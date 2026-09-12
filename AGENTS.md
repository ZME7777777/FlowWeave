# FlowWeave 协作指南

本文件适用于整个 FlowWeave 仓库，并补充上级
`/Users/zhengmengen/WorkSpace/AGENTS.md`。若目标目录存在更近的 `AGENTS.md`，优先遵守离目标文件最近的说明。

## 远程部署与私有配置

生产服务器地址、域名、账号、SSH 配置、部署根目录、网络拓扑和运行时路径都不得提交到 Git，亦不得写入公开文档、Issue、PR、聊天记录或构建日志。将此类信息仅保存在本机受权限保护且被 Git 忽略的 `.local/remote-deploy.env` 中；从 `deploy/remote-deploy.env.example` 创建该文件。

任何远程部署、发布、远端镜像、生产、SSH 或远端排障，先完整读取 `docs/deployment.md` 和 `docs/local-build-and-deploy.md`，然后运行：

```bash
scripts/verify-remote-deploy.sh --config .local/remote-deploy.env \
  --commit <已提交的完整或短 SHA> --scope <web|platform|runtime|other>
```

预检必须复述从本地配置读取的目标、部署根、发布范围和 commit。未提供有效本地配置时停止，不得猜测 SSH 别名或目标环境。普通部署严禁 `docker compose down -v`、删除 volume/Workspace、覆盖远端 Compose 或环境文件。`make rebuild-deploy` 和 `infra/compose.yaml` 仅用于本地；绝不可当作远端部署入口。

远程 Compose、环境文件、持久数据路径和私有入口均为服务器侧资产，不得复制进仓库。更新 API 时，必须从同一已提交版本同步更新并 recreate `stream-api`；不得以 orphan 或 `--remove-orphans` 忽略它。发生故障时先保留日志和数据库错误状态，再仅回滚受影响服务；禁止使用删除 volume、清空 Workspace、`reset --hard` 或 `clean -f` 代替回滚。

## OpenHands-first 架构原则

- FlowWeave 的产品设计、用户流程和业务边界是需求来源；OpenHands 是 Agent 执行能力的实现依赖，不是产品能力清单。
- FlowWeave 是控制面，只负责能力治理、不可变版本冻结、权限、策略、审批、审计、资源隔离和业务投影。
- Tool、Skill、Plugin、MCP、Hook、Agent Definition、Task 子 Agent、Condenser、Memory、Critic、Fork 和 ACP 等执行能力应由 OpenHands 正式类型、事件、API 和生命周期实现。
- 不得用提示词、私有控制 JSON、文本约定、私有 HTTP 或平台自建执行器模拟 OpenHands 已提供的能力。
- FlowWeave 显式传入的 Runtime 能力必须可追溯到固定 version、digest、blob/hash 和 Snapshot Runtime Manifest，明文 Secret 不得持久化进入 Runtime。OpenHands 1.47.0 原生的 HOME/项目 ambient Plugin 发现明确允许，它不是 FlowWeave 冻结 Plugin 的替代事实源，也不得用私有字段或源码补丁禁用。
- 事件关联必须使用 OpenHands 正式的 `id`、`parent_id`、`action_id`、`tool_call_id`、cursor 等字段，不得按事件顺序、名称或文本猜测。

## OpenHands 源码与镜像基线

当前目标能力事实固定为 OpenHands 源码 commit `30cf5832e42c71c24daa82a1a4fd5d25eb70d1b9`（`v1.47.0-4-g30cf5832e`），只修改 FlowWeave。OpenHands 源码工作树保持只读；不得在当前 `FR-*` 主线中创建 fork、修改 OpenHands 源码或提前实施二开。

- SDK 源码：`/Users/zhengmengen/WorkSpace/openhands/software-agent-sdk`
- 历史兼容基线：`v1.42.0` / `f09e03eac772290feeb51b7d7390ffaefeca1a09`
- 固定包版本：`openhands-agent-server==1.47.0`、`openhands-sdk==1.47.0`、`openhands-tools==1.47.0`、`openhands-workspace==1.47.0`
- 固定运行时镜像：`flowweave-openhands-runtime:1`
- 契约探针：`infra/openhands/contract_check.py`

能力判断优先读取固定 commit，并只在当前切片确有需要时取证：

```bash
git -C /Users/zhengmengen/WorkSpace/openhands/software-agent-sdk \
  show 30cf5832e42c71c24daa82a1a4fd5d25eb70d1b9:<相对路径>

git -C /Users/zhengmengen/WorkSpace/openhands/software-agent-sdk \
  grep -n '<模式>' 30cf5832e42c71c24daa82a1a4fd5d25eb70d1b9 -- \
  openhands-agent-server openhands-sdk openhands-tools openhands-workspace
```

证据优先级从强到弱为：固定源码构建的实际镜像及可执行探针、固定 commit 源码和测试、FlowWeave source lock 与适配代码、历史兼容源码和镜像、版本明确匹配的官方文档。不得凭记忆或浮动 `main` 推断契约。

## FlowRun Runtime 重构恢复方式

每次开始或恢复任务时依次执行：

1. 完整读取 `docs/flowrun-openhands-runtime-design.md` 和 `docs/flowrun-runtime-task-progress.md`。
2. 检查 `git status --short --branch`、未提交 diff 和当前 Alembic heads。
3. 检查进度文档是否最多只有一个 `CURRENT`，以及当前切片依赖是否全部 `DONE`。
4. 若工作树存在未闭环切片，先完成该切片；不得越过它开始后续切片。
5. 一次只完成一个最小可独立验收切片；实现边界过大时先拆分任务和依赖。
6. 按进度文档要求完成实现、基础检查和状态更新。
7. 切片完成后提交该切片代码和文档；确认提交成功后立即停止，不自动开始下一项。

源码、迁移、测试和实际运行结果是当前进度的权威证据。旧的 OpenHands 重构文档和历史验收结论不得替代当前 `FR-*` 任务重新实施与验证。

## 本地临时产物

- 一次性 API 请求体、dry-run 结果、平台响应快照、临时能力包、调试导出和工具中间文件必须写入仓库根目录的 `.tmp/`。
- 禁止在仓库根目录或任何受版本控制的源码/文档目录创建临时文件。
- `.tmp/` 已由 `.gitignore` 忽略；提交前仍须检查 `git status`，确保没有临时产物被暂存。
- 仅在需要保留为正式、可复现资产时，才将内容移入相应的版本化目录并使用明确、非临时的文件名。

## 切片验证与提交规则

- `FR-01`–`FR-11` 只运行进度文档允许的最窄语法、解析或编译检查，以及 `git diff --check` 和任务状态唯一性核对；不得提前运行集中在 `FR-12` 的业务测试、迁移实跑、完整构建、Runtime、安全或 E2E 门禁。
- `FR-12` 负责进度文档列明的完整故障恢复、安全、契约、迁移和 E2E 验证。
- 提交前必须复核 staged diff，确保只包含当前切片和必要的协作文档变更，不混入无关改动、密钥、缓存或生成物。
- 每个完成切片使用独立、可审计的 Git commit；提交信息应包含切片编号和结果，例如 `feat(runtime): complete FR-01 environment binding`。
- 提交成功是切片收尾的一部分。提交后只报告提交哈希、验证结果和下一可执行切片，不继续实现下一项。
