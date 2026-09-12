# 部署与运维

本指南覆盖本地 Compose 重新部署与受控远端发布。始终先检查工作树，并按实际改动范围更新服务。

## 本地 Compose

部署前先确认环境文件和 Compose 配置：

```bash
docker compose --env-file .env -f infra/compose.yaml config --quiet
```

### 全量本地重建

```bash
make rebuild-deploy
```

该命令无缓存重建 Sandbox、Dependency Builder、固定 OpenHands Runtime 以及平台/Web 镜像，然后重建本地栈；它保留 PostgreSQL named volume、Artifact volume 和宿主机工作区。

### 定向更新

只改 Web：

```bash
docker compose --env-file .env -f infra/compose.yaml build --no-cache web
docker compose --env-file .env -f infra/compose.yaml up -d --no-deps --force-recreate web
```

改动 `services/platform` 的共享代码或 Alembic 迁移时，Migration、Runtime Provider、API、stream-api 与 Worker 必须来自同一版本。先运行迁移，确认退出码为 0，再替换常驻进程：

```bash
docker compose --env-file .env -f infra/compose.yaml build --no-cache \
  migration runtime-provider api stream-api worker
docker compose --env-file .env -f infra/compose.yaml \
  up --no-deps --force-recreate migration
docker compose --env-file .env -f infra/compose.yaml \
  up -d --no-deps --force-recreate runtime-provider api stream-api worker
```

更新 `infra/openhands/**` 后，新镜像只会影响之后创建或替换的 Runtime；已运行 Runtime 不会原地变更。发布新的 Environment Version 或通过正式 Runtime 生命周期替换 generation。

### 部署后验证

```bash
docker compose --env-file .env -f infra/compose.yaml ps -a
docker compose --env-file .env -f infra/compose.yaml logs --tail=100 \
  migration runtime-provider api stream-api worker web
curl -fsS http://127.0.0.1:8080/health
curl -fsS http://127.0.0.1:8080/health/ready
curl -I http://127.0.0.1:5173/
```

不要用 `docker compose down -v`、删除 volume 或清空 Workspace 解决发布故障。保留日志，恢复上一个已知镜像并仅重建受影响服务。

## 远端发布

生产主机、域名、账号、路径和其他网络拓扑属于私有运行配置，严禁写入 Git、Issue、PR、构建日志或公开文档。将版本化模板复制到本地忽略目录后，再填入该环境的非凭据连接信息：

```bash
mkdir -p .local
cp deploy/remote-deploy.env.example .local/remote-deploy.env
chmod 600 .local/remote-deploy.env
```

发布前必须是一个已提交的 Git commit，并首先使用本地配置执行预检：

```bash
scripts/verify-remote-deploy.sh --config .local/remote-deploy.env \
  --commit <commit-sha> --scope <web|platform|runtime|other>
# 或
make remote-deploy-preflight REMOTE_DEPLOY_CONFIG=.local/remote-deploy.env \
  COMMIT=<commit-sha> SCOPE=<web|platform|runtime|other>
```

预检会确认本地配置中的目标主机、部署目录、提交与范围。不要猜测 SSH 别名、覆盖远端 Compose 或环境文件，也不要使用本地 `infra/compose.yaml` 替换服务器 Compose 文件。

### Commit 绑定的构建与更新

1. 在本地确认 `git status --short --branch`、目标 commit 和受影响测试；运行 `git diff --check`。
2. 从目标 commit 使用 `git archive` 创建不可变源码包，记录 SHA-256，传至私有配置所指向的构建目录，并在服务器再次校验 SHA-256。
3. 在服务器从该包构建所需 `linux/amd64` 镜像，检查 `docker image inspect` 输出为 `linux/amd64`。
4. 验证远端 Compose，再按影响范围 force-recreate。更新平台镜像时，先运行 `migration`，随后同时更新 `runtime-provider`、`api`、`stream-api`、`worker`。仅更新 Web 时只更新 `web`。
5. 检查服务健康、带 `/flowweave/` 前缀的 API/静态资源、Agent 深层路由及 FastGPT 根登录页。

服务器的持久数据包括 PostgreSQL、Artifact volume 与 Workspace bind mount。普通发布绝不执行 `docker compose down -v`，不删除数据、不覆盖环境文件，也不使用 `--remove-orphans` 忽略或删除 `stream-api`。

公网前缀部署还须在全新浏览器上下文确认 Network 请求使用 `/flowweave/api/v1/...`，而不是被 FastGPT 接管的根路径 `/api/v1/...`。

## 回滚与容量维护

发布前记录旧 image ID 并创建时间戳 rollback tag。发生回归时先保留日志和数据库错误状态，再将受影响服务恢复到该镜像并 `--no-deps --force-recreate`。

远端 Docker 容量维护默认是只读审计：

```bash
scripts/maintain-remote-docker-retention.sh --config .local/remote-deploy.env
```

任何删除操作需要明确授权、先审阅 dry-run，并遵循脚本的确认参数。不要使用 `docker system prune`、`docker image prune -a`、强制删除镜像或清理 volume/Workspace 作为日常维护手段。
