# FlowWeave

FlowWeave 是面向内部研发流程的 Agent 工作台：可复用节点资产、可视化流程编排，以及以不可变快照和显式产物版本驱动的运行系统。Agent 负责执行，人工始终负责开始确认、结果验收、修订、快照同步和取消。

## 产品能力

- 节点目录与节点资产：模型、提示词、Skill/MCP/Hook、输入输出契约；支持从一个 ZIP 批量导入多个 Skill（每个 Skill 目录包含一个 `SKILL.md`）。Skill 的脚本与参考文件会完整落到节点宿主工作区，MCP 会作为真实工具注入 Agent。
- 模型服务：加密 API Key、模型发现、启用模型和默认模型。
- 流程编排：同一资产可重复放置为不同 Flow Node，边提供产物映射候选，节点拥有多条 START/END 门禁。
- 流程运行：任意节点启动、人工产物、显式 Input Binding、Node Run/Attempt 分离、不可变 Artifact Version。
- 人工控制：开始门禁后确认、结束门禁后验收、驳回创建新 Attempt、Snapshot 追加同步。
- 恢复与审计：PostgreSQL 任务租约/fencing、追加式事件、按 cursor 恢复的 SSE。
- 会话能力引用：在输入框键入 `$`，统一检索并引用当前节点的 Skill 或 MCP；选择结果随消息持久化、排队和引导。

## 仓库结构

```text
apps/web/          React + TypeScript 产品前端
services/platform/ Python 3.12 FastAPI API、Worker、五段核心加两段前向 Alembic 迁移
contracts/         跨进程 JSON Schema
agent-packages/    可导入 Agent Skill/Plugin 示例
infra/compose.yaml PostgreSQL、migration、API、Worker、Web、可选 OpenHands
```

## 启动

要求：Node.js 22+、pnpm 10.33.4、Python 3.12+、uv 0.7.8、Docker。

```bash
cp .env.example .env
make install
make infra-up
```

访问 <http://localhost:5173>。默认使用 OpenHands Agent Server，并在每次执行时读取节点所选模型服务、模型和加密保存的 API Key。Mock Runtime 只用于自动化测试；如需显式启动完整服务：

```bash
RUNTIME_ADAPTER=openhands make infra-up-openhands
```

节点资源保存在宿主机 `var/workspaces/nodes/<node-asset-id>/`：`skills/` 存放完整 Skill 包，`mcp/` 存放 Server 配置与配套文件，`files/` 可放文本或附件，`repositories/` 可放代码仓库，`sessions/` 保存该节点各次运行的会话工作区。可通过 `FLOWWEAVE_HOST_WORKSPACE_ROOT` 改为其他宿主机目录。

OpenHands 镜像内置 `sh`、`bash`、Python、Node.js、npm/npx、`js`、uv/uvx、Git、SSH、curl、jq、unzip 与 `lark-cli`。`lark-cli` 的持久状态位于宿主机 `var/tool-state/lark-cli/`；首次使用需在容器中完成授权：

```bash
docker compose -f infra/compose.yaml exec openhands-agent-server lark-cli auth login
```

本地分进程开发：

```bash
make api-dev
make worker-dev
make web-dev
```

验证：

```bash
make check
make migration-check
make e2e
```

公共接口统一位于 `/api/v1`，读写接口均可直接访问；命令通过 `Idempotency-Key` 保证幂等。模型密钥加密保存且不进入 API 响应、运行快照、Prompt 或事件。
