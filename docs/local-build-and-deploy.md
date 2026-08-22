# FlowWeave 本地编译、打包与部署

本文说明如何使用仓库自带的 Docker Compose 配置，在本机重新编译并部署 FlowWeave。命令均在仓库根目录执行。

## 1. 前置条件

- Docker Desktop 或 Docker Engine 已启动；
- 已从 `.env.example` 创建 `.env`；
- `.env` 至少正确设置：
  - `SANDBOX_RUNTIME_NETWORK_MODE=isolated|egress`；
  - 两个互不相同且不少于 32 字符的 `DOCKER_CONTROLLER_API_KEY`、`DOCKER_CONTROLLER_WORKER_API_KEY`；
  - 生产或共享环境还必须替换默认数据库密码、`CREDENTIALS_MASTER_KEY` 和 `OPENHANDS_SESSION_API_KEY`。

先验证 Compose 配置可以展开：

```bash
docker compose --env-file .env -f infra/compose.yaml config --services
```

下文用到的完整 Compose 命令前缀为：

```bash
docker compose --env-file .env -f infra/compose.yaml
```

## 2. 全部镜像重新编译并部署

仓库提供了标准目标：

```bash
make rebuild-deploy
```

该命令会：

1. 无缓存重建 Python/JavaScript Sandbox 镜像；
2. 无缓存重建 Dependency Builder；
3. 无缓存重建固定 OpenHands Runtime；
4. 无缓存重建 Migration、Runtime Provider、API、Worker 和 Web；
5. 重新创建完整 Compose 服务栈并等待依赖健康。

它会保留以下持久数据：

- PostgreSQL named volume `postgres-data`；
- Artifact named volume `artifacts`；
- `${FLOWWEAVE_RUNTIME_HOST_WORKSPACE_ROOT}` 以绝对路径指向 Docker daemon 可见的宿主机工作区，默认是 `${PWD}/var/workspaces`。

不要为了普通部署执行 `docker compose down -v`，`-v` 会删除 named volumes。全量重建会重启 API、Worker 和 Runtime Provider；已运行的 FlowRun generation 不会被 Compose 作为共享服务重建，但平台短暂不可用仍会中断连接，请先确认没有重要任务正在执行。

### 2.1 外部软件源临时失败

无缓存构建需要访问 Debian、npm、PyPI 和固定 OpenHands 源码归档。若出现 `502`、超时等外部错误，优先原样重试：

```bash
make rebuild-deploy
```

不要因为下载失败就修改固定版本或解除 digest 锁定。如果已确认 OpenHands、Sandbox 或 Dependency Builder 没有源码改动，可以只重建受影响服务，见下一节；这属于定向部署，不等同于一次完整全量重建。

## 3. 只编译并部署单个 Compose 服务

通用模板：

```bash
docker compose --env-file .env -f infra/compose.yaml build --no-cache <service>
docker compose --env-file .env -f infra/compose.yaml up -d --no-deps --force-recreate <service>
```

`--no-deps` 表示不重启当前仍健康的依赖服务；如果依赖尚未启动，应去掉该参数。`--force-recreate` 确保容器使用刚生成的镜像。

### 3.1 Web

适用于 `apps/web/**`、Web Nginx 配置或前端依赖变化：

```bash
docker compose --env-file .env -f infra/compose.yaml build --no-cache web
docker compose --env-file .env -f infra/compose.yaml up -d --no-deps --force-recreate web
curl -I http://127.0.0.1:5173/
```

### 3.2 API

仅当改动确定只影响 API 进程时使用：

```bash
docker compose --env-file .env -f infra/compose.yaml build --no-cache api
docker compose --env-file .env -f infra/compose.yaml up -d --no-deps --force-recreate api
curl -fsS http://127.0.0.1:8080/health
```

### 3.3 Worker

```bash
docker compose --env-file .env -f infra/compose.yaml build --no-cache worker
docker compose --env-file .env -f infra/compose.yaml up -d --no-deps --force-recreate worker
docker compose --env-file .env -f infra/compose.yaml logs --tail=100 worker
```

重启 Worker 可能打断当前租约，平台会按持久化任务和 fencing 机制恢复；仍应避免在关键任务执行时部署。

### 3.4 Runtime Provider

```bash
docker compose --env-file .env -f infra/compose.yaml build --no-cache runtime-provider
docker compose --env-file .env -f infra/compose.yaml up -d --no-deps --force-recreate runtime-provider
docker compose --env-file .env -f infra/compose.yaml ps runtime-provider
```

Runtime Provider 管理配置终端和动态 Runtime，重启可能使当前终端附件断开；持久 tmux 内的任务仍由对应容器保持，但浏览器需要重新连接。

### 3.5 Migration

新增或修改 Alembic 迁移时，先构建并运行一次 Migration，再更新平台进程：

```bash
docker compose --env-file .env -f infra/compose.yaml build --no-cache migration api worker runtime-provider
docker compose --env-file .env -f infra/compose.yaml up --no-deps --force-recreate migration
docker compose --env-file .env -f infra/compose.yaml up -d --no-deps --force-recreate runtime-provider api worker
```

Migration 必须以退出码 `0` 结束。失败时不要继续部署依赖新 Schema 的 API/Worker。

## 4. 平台共享代码改动应一起部署

`api`、`worker`、`runtime-provider` 和 `migration` 都使用 `services/platform/Dockerfile`，并复制同一份 `services/platform/src`。如果改动位于共享模块，不能只更新其中一个进程，否则会出现代码版本不一致。推荐：

```bash
docker compose --env-file .env -f infra/compose.yaml build --no-cache migration runtime-provider api worker
docker compose --env-file .env -f infra/compose.yaml up --no-deps --force-recreate migration
docker compose --env-file .env -f infra/compose.yaml up -d --no-deps --force-recreate runtime-provider api worker
```

若没有迁移变更，Migration 仍可安全执行 `alembic upgrade head`；它应快速以 `0` 退出。

## 5. 独立运行时镜像

这些镜像不是长期运行的普通 Compose 服务，而是由 Runtime Provider 按需创建容器。

### 5.1 OpenHands Runtime

```bash
docker build --no-cache -f infra/openhands/Dockerfile -t flowweave-openhands-runtime:1 .
docker image inspect flowweave-openhands-runtime:1
```

新镜像也会被后续动态 Agent Runtime 和终端环境草稿使用。已经运行的动态容器不会被原地替换。OpenHands 镜像必须继续满足 `source.lock.json`、包版本和契约探针约束，不要用浮动上游版本替代。

### 5.2 Python/JavaScript Sandbox

```bash
docker build --no-cache -f infra/sandbox/python/Dockerfile -t flowweave-sandbox-python:1 .
docker build --no-cache -f infra/sandbox/javascript/Dockerfile -t flowweave-sandbox-javascript:1 .
```

新镜像用于之后创建的 Sandbox；已有容器不会自动替换。需要验证时：

```bash
make sandbox-smoke
```

### 5.3 Dependency Builder

```bash
docker build --no-cache -f infra/dependency-builder/Dockerfile -t flowweave-dependency-builder:1 .
```

Controller 之后创建的依赖构建任务会使用新镜像。

## 6. 部署后的检查

查看所有服务：

```bash
docker compose --env-file .env -f infra/compose.yaml ps -a
```

期望结果：

- `postgres`、`runtime-provider`、`api` 为 `healthy`；
- `worker`、`web` 为 `Up`；
- `workspace-init`、`migration` 为 `Exited (0)`。

检查入口：

```bash
curl -fsS http://127.0.0.1:8080/health
curl -fsS http://127.0.0.1:8080/health/ready
curl -I http://127.0.0.1:5173/
```

检查日志：

```bash
docker compose --env-file .env -f infra/compose.yaml logs --tail=100 api worker web runtime-provider
```

确认运行容器使用当前镜像：

```bash
for service in api worker runtime-provider web; do
  container=$(docker compose --env-file .env -f infra/compose.yaml ps -q "$service")
  running=$(docker inspect --format '{{.Image}}' "$container")
  declared=$(docker image inspect --format '{{.Id}}' "flowweave-$service:latest")
  printf '%s running=%s declared=%s\n' "$service" "$running" "$declared"
done
```

## 7. 常用选择表

| 改动范围 | 推荐操作 |
|---|---|
| 只改 `apps/web/**` | 只重建、重启 `web` |
| 只改 API 路由且没有共享行为变化 | 只重建、重启 `api` |
| 改 `services/platform/src` 共享模块 | 一起重建 `migration runtime-provider api worker` |
| 新增 Alembic 迁移 | 先运行 `migration` 成功，再重启平台服务 |
| 改 `infra/openhands/**` | 重建 OpenHands Runtime；后续发布的 Environment Version 和新 generation 使用新镜像 |
| 改 `infra/sandbox/**` | 重建对应 Sandbox 镜像；后续新 Sandbox 生效 |
| 改 `infra/dependency-builder/**` | 重建 Dependency Builder；后续新构建任务生效 |
| 改 Compose、环境变量或多个子系统 | 使用 `make rebuild-deploy` |

## 8. 停止服务

保留数据停止：

```bash
make infra-down
```

再次启动完整服务：

```bash
make infra-up
```

除非明确要永久清空本地数据库、Artifact 和 FlowRun Runtime 外置状态，否则不要添加 `-v`。
