# Development

## 进程

FlowWeave 由独立的 migration、API、Worker、Web 和 PostgreSQL 进程组成。服务不会在启动时自动改 Schema；Compose 先运行 migration job，再启动 API/Worker。

```bash
cd services/platform
uv sync --frozen
uv run alembic upgrade head
uv run uvicorn flowweave.bootstrap.api:create_app --reload --port 8080
uv run python -m flowweave.bootstrap.worker
```

Web：`pnpm --filter @flowweave/web dev`。

## 认证与接口

所有公共接口使用 `/api/v1`。GET 和 SSE 匿名可读；POST/PUT/PATCH/DELETE 必须携带 `Authorization: Bearer <HUMAN_WRITE_TOKEN>`。命令支持 `Idempotency-Key`，错误体统一为 `{"error":{"code","message","details","request_id"}}`。

## 数据库和迁移

迁移链以 `0001_catalog`、`0002_flows`、`0003_runs`、`0004_artifacts`、`0005_execution` 为五段核心基线，并以前向迁移 `0006_capability_imports`、`0007_run_event_notify` 分别增加持久化导入会话和提交后通知。运行 `make migration-check` 验证空库升级、回退到 `0005` 和再次升级。生产、单元、集成、架构与契约测试统一使用 PostgreSQL；生产包和测试均不包含 SQLite 兼容路径。未显式设置 `TEST_DATABASE_URL`/`DATABASE_URL` 时，pytest 与迁移检查自动使用固定镜像 `postgres:16.9-alpine3.21` 的 Testcontainer；设置 URL 时可复用外部 PostgreSQL 进行诊断。

## 验证

- `make api-check`：Ruff format/lint、Pyright strict 与 PostgreSQL Pytest；默认自动启动 Testcontainer。
- `make web-check`：ESLint、TypeScript、Vite 生产构建。
- `make compose-check`：Compose 配置。
- `make platform-image-check`：构建多阶段平台镜像，验证原生 `quickjs` 可导入且最终运行镜像不含 gcc。
- `make e2e`：Chrome 产品闭环。

## 浏览器端到端测试

隔离 E2E API 与 Playwright 必须显式使用同一人工写令牌，例如：

```bash
HUMAN_WRITE_TOKEN=test-human-token EXECUTION_MODE=inline make api-dev
E2E_HUMAN_WRITE_TOKEN=test-human-token pnpm --filter @flowweave/web e2e
```

## 本地 Docker Sandbox

标准本地 Compose Worker 使用 `SANDBOX_BACKEND=docker`，并挂载 `/var/run/docker.sock`。`make infra-up` 会先构建固定标签的 Python/JavaScript Sandbox 镜像，再启动 Worker。Worker 主进程保持 UID 10001；Docker Desktop 的 Socket 为 `root:root 0660`，因此仅为 Worker 添加 supplemental GID 0。启动后可运行 `make sandbox-smoke`，通过生产 DockerSandbox 适配器分别执行一次 Python 和 JavaScript 容器，并验证两者返回 PASS。

Docker Socket 等价于宿主 Docker 管理权限：被攻陷的 Worker 可创建特权容器、挂载宿主路径或删除本机容器。该配置仅适用于已明确授权的本机开发环境，不应直接复制到生产。回退方式：从 `infra/compose.yaml` 的 Worker 移除 Socket volume 与 `group_add`，并将 `SANDBOX_BACKEND` 改回 `process`，然后重建 Worker。
