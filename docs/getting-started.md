# 本地启动

本指南用于在本机运行完整 FlowWeave 开发栈。所有命令均从仓库根目录执行。

## 前置条件

- Docker Desktop 或 Docker Engine 正在运行；
- Node.js 22+ 与 pnpm 10.33.4；
- Python 3.12+ 与 uv；
- Docker daemon 可访问所配置的宿主机工作区路径。

先创建环境文件：

```bash
cp .env.example .env
```

最少需要在 `.env` 中完成以下配置：

1. 将 `FLOWWEAVE_RUNTIME_HOST_WORKSPACE_ROOT` 改为 Docker daemon 可见的绝对路径，例如 `$PWD/var/workspaces` 展开后的绝对值。
2. 设置两个不同且长度至少 32 字符的 `DOCKER_CONTROLLER_API_KEY` 与 `DOCKER_CONTROLLER_WORKER_API_KEY`。
3. 明确设置 `SANDBOX_RUNTIME_NETWORK_MODE=egress` 或 `isolated`。

详细变量说明见[环境配置](environment-reference.md)。不要把 `.env`、令牌或凭据提交到仓库。

## 启动完整本地栈

```bash
make install
make infra-up
```

`infra-up` 会构建所需的 Sandbox、Dependency Builder 与固定 OpenHands Runtime 镜像，再启动 PostgreSQL、迁移任务、Runtime Provider、API、Worker 和 Web。打开 <http://localhost:5173>。

确认服务状态：

```bash
docker compose --env-file .env -f infra/compose.yaml ps -a
curl -fsS http://127.0.0.1:8080/health
curl -fsS http://127.0.0.1:8080/health/ready
curl -I http://127.0.0.1:5173/
```

期望 `postgres`、`runtime-provider`、`api` 为 healthy，`worker` 和 `web` 为 Up，`migration` 与 `workspace-init` 为 `Exited (0)`。

## 分进程开发

完整 Compose 启动后，或在已准备好 PostgreSQL 与 Runtime Provider 的开发环境中，可分别运行：

```bash
make api-dev
make worker-dev
make web-dev
```

`make dev` 只打印这些入口；它不会替你启动进程。Web 开发服务器由 Vite 管理，API 默认监听 8080。

## 常用检查

```bash
make check            # Python、Web 与 Compose 安全检查
make migration-check  # Alembic revision/head 检查
make web-check        # Web lint、类型检查和生产构建
make e2e              # Playwright 端到端测试
```

需要检查固定 OpenHands Runtime 时：

```bash
make openhands-contract-check
make openhands-smoke
make sandbox-smoke
```

这些检查会使用 Docker；若 Docker 未运行，应先修复 Docker 连接而不是把检查结果视为通过。

## 停止与再次启动

```bash
make infra-down
make infra-up
```

`infra-down` 不带 `-v`，会保留数据库、Artifact 和宿主机工作区。不要在日常开发或排障中使用 `docker compose down -v`。
