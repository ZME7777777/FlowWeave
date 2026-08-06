# Development

## 进程

FlowWeave 由独立的 migration、API、Worker、Web 和 PostgreSQL 进程组成。服务不会在启动时自动改 Schema；Compose 先运行 migration job，再启动 API/Worker。

标准 Compose 环境同时启动 OpenHands Agent Server。Worker 会在创建会话时读取节点选择的模型服务、默认/指定模型及加密 API Key，并注入节点的提示词、输入产物、完整 Skill 包和 MCP Server；`RUNTIME_ADAPTER=mock` 仅供测试显式使用。OpenHands 容器与 API/Worker 通过宿主机 bind mount 共享工作区，默认提供终端、文件编辑和任务跟踪工具。

## 节点宿主工作区

默认根目录为 `var/workspaces`，可用 `FLOWWEAVE_HOST_WORKSPACE_ROOT` 覆盖。每个节点资产拥有 `nodes/<node-asset-id>`，其中：

- `skills/<能力名>`：从导入 ZIP 完整解压的 `SKILL.md`、scripts、references 和资源文件。
- `mcp/<能力名>`：规范化后的 `config.json`，也可放置本地 MCP Server 脚本。
- `files`：用户自行放置的文本、附件或其他上下文。
- `repositories`：节点长期使用的代码仓库。
- `sessions/<run-id>/<node-run-id>/<attempt-no>`：各执行轮次的隔离工作目录。

Agent 启动时会收到上述容器内绝对路径，MCP 配置通过 OpenHands `mcp_config` 注册为真实工具。聊天输入框用统一的 `$能力名` 引用 Skill 或 MCP，消息会保存结构化 `capability_refs`，不依赖模型自行猜测文本。

OpenHands 镜像提供 shell、Python、Node.js/npm/npx、uv/uvx、Git/SSH 与 `lark-cli`。Lark CLI 状态通过 `FLOWWEAVE_HOST_LARK_CLI_HOME` 持久化，默认是 `var/tool-state/lark-cli`；本地首次授权使用：

```bash
docker compose -f infra/compose.yaml exec openhands-agent-server lark-cli auth login
```

```bash
cd services/platform
uv sync --frozen
uv run alembic upgrade head
uv run uvicorn flowweave.bootstrap.api:create_app --reload --port 8080
uv run python -m flowweave.bootstrap.worker
```

Web：`pnpm --filter @flowweave/web dev`。

## 认证与接口

所有公共接口使用 `/api/v1`，读写接口均可直接访问。命令支持 `Idempotency-Key`，错误体统一为 `{"error":{"code","message","details","request_id"}}`。

## 数据库和迁移

迁移链以 `0001_catalog`、`0002_flows`、`0003_runs`、`0004_artifacts`、`0005_execution` 为五段核心基线，并以前向迁移 `0006_capability_imports`、`0007_run_event_notify` 分别增加持久化导入会话和提交后通知。运行 `make migration-check` 验证空库升级、回退到 `0005` 和再次升级。生产、单元、集成、架构与契约测试统一使用 PostgreSQL；生产包和测试均不包含 SQLite 兼容路径。未显式设置 `TEST_DATABASE_URL`/`DATABASE_URL` 时，pytest 与迁移检查自动使用固定镜像 `postgres:16.9-alpine3.21` 的 Testcontainer；设置 URL 时可复用外部 PostgreSQL 进行诊断。

## 验证

- `make api-check`：Ruff format/lint、Pyright strict 与 PostgreSQL Pytest；默认自动启动 Testcontainer。
- `make web-check`：ESLint、TypeScript、Vite 生产构建。
- `make compose-check`：Compose 配置。
- `make platform-image-check`：构建多阶段平台镜像，验证原生 `quickjs` 可导入且最终运行镜像不含 gcc。
- `make e2e`：Chrome 产品闭环。

## 浏览器端到端测试

隔离 E2E API 与 Playwright 可直接使用同一套本地服务，例如：

```bash
EXECUTION_MODE=inline make api-dev
pnpm --filter @flowweave/web e2e
```

## 本地 Docker Sandbox

标准本地 Compose Worker 使用 `SANDBOX_BACKEND=docker`，并挂载 `/var/run/docker.sock`。`make infra-up` 会先构建固定标签的 Python/JavaScript Sandbox 镜像，再启动 Worker。Worker 主进程保持 UID 10001；Docker Desktop 的 Socket 为 `root:root 0660`，因此仅为 Worker 添加 supplemental GID 0。启动后可运行 `make sandbox-smoke`，通过生产 DockerSandbox 适配器分别执行一次 Python 和 JavaScript 容器，并验证两者返回 PASS。

Docker Socket 等价于宿主 Docker 管理权限：被攻陷的 Worker 可创建特权容器、挂载宿主路径或删除本机容器。该配置仅适用于已明确授权的本机开发环境，不应直接复制到生产。回退方式：从 `infra/compose.yaml` 的 Worker 移除 Socket volume 与 `group_add`，并将 `SANDBOX_BACKEND` 改回 `process`，然后重建 Worker。
