# FlowWeave

FlowWeave 是面向内部研发流程的 Agent 工作台：可复用节点资产、可视化流程编排，以及以不可变快照和显式产物版本驱动的运行系统。Agent 负责执行，人工始终负责开始确认、结果验收、修订、快照同步和取消。

## 产品能力

- 节点目录与节点资产：模型、提示词、Skill/MCP/Hook、输入输出契约。
- 模型服务：加密 API Key、模型发现、启用模型和默认模型。
- 流程编排：同一资产可重复放置为不同 Flow Node，边提供产物映射候选，节点拥有多条 START/END 门禁。
- 流程运行：任意节点启动、人工产物、显式 Input Binding、Node Run/Attempt 分离、不可变 Artifact Version。
- 人工控制：开始门禁后确认、结束门禁后验收、驳回创建新 Attempt、Snapshot 追加同步。
- 恢复与审计：PostgreSQL 任务租约/fencing、追加式事件、按 cursor 恢复的 SSE。

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

访问 <http://localhost:5173>。默认使用确定性 Mock Runtime；真实 OpenHands 模式使用：

```bash
RUNTIME_ADAPTER=openhands make infra-up-openhands
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

公共接口统一位于 `/api/v1`。GET/SSE 可匿名，所有写操作要求 `HUMAN_WRITE_TOKEN`；令牌仅保存在浏览器当前标签页，不会注入 Runtime。模型密钥加密保存且不进入 API 响应、运行快照、Prompt 或事件。
