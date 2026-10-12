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

普通 Platform API、stream-api、Worker 代码或 Alembic 迁移变更，先运行迁移，确认退出码为 0，再仅替换这些常驻服务：

```bash
docker compose --env-file .env -f infra/compose.yaml build --no-cache \
  migration api stream-api worker
docker compose --env-file .env -f infra/compose.yaml \
  up --no-deps --force-recreate migration
docker compose --env-file .env -f infra/compose.yaml \
  up -d --no-deps --force-recreate api stream-api worker
```

**不要**因普通 Platform 发布重建 `runtime-provider`：它负责与正在运行的 Agent Workspace / FlowRun Runtime 的连接。只有修改 `runtime-provider`、Docker Provider、Runtime 协议，或 `infra/openhands/**` 时，才使用 `runtime` 范围；该范围先迁移，然后同步重建 `runtime-provider`、`api`、`stream-api` 与 `worker`。发布前必须确认没有不允许中断的活跃会话；即使已运行 Runtime 镜像不原地更新，Provider 重启也会短暂中断正在进行的工具调用。

更新 `infra/openhands/**` 后，新镜像只会影响之后创建或正式 replacement 的 Runtime；已运行 Runtime 不会原地变更。发布新的 Environment Version 或通过正式 Runtime 生命周期替换 generation。

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

生产主机、域名、账号、路径和其他网络拓扑属于私有运行配置，严禁写入 Git、Issue、PR、构建日志或公开文档。将版本化模板复制到本地忽略目录后，再填入该环境的非凭据连接信息和唯一部署入口：主 Compose/env、源码构建目录、镜像目录，以及（仅在独立管理时）stream-api 的 Compose/env。路径均相对于私有配置中的部署根。

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

预检先确认本地配置中的目标主机、部署目录、提交与范围；随后只读 SSH，检查部署根、构建/镜像目录、声明的 Compose/env 文件，运行 `docker compose config --quiet` 并确认本次范围所需服务。`platform` 验证 `migration`、`api`、`worker` 和 `stream-api`，且不得重建 `runtime-provider`；`runtime` 额外验证 `runtime-provider`，只用于 Provider、Docker Provider、Runtime 协议或固定 OpenHands Runtime 变更。`stream-api` 可在显式声明的独立 Compose 项目中。预检不会构建镜像、重建服务或改写服务器文件。

不要猜测 SSH 别名、Compose 入口或 `stream-api` 所属项目，也不要使用本地 `infra/compose.yaml` 替换服务器 Compose 文件。若预检报缺少入口或服务契约，先修正本机受保护的 `.local/remote-deploy.env`，再重新预检。

对于请求人明确确认并授权的受管内部服务器，新增服务所需的远端 Compose／环境变更可以作为本次发布的一部分执行。先在服务器内为两个文件创建时间戳备份；仅对新增服务作最小服务级补丁，沿用已验证的网络和镜像约定。新增密钥必须仅在服务器内生成、直接写入权限受限的环境文件且不回显。更新后先验证 `docker compose config --quiet`，随后再继续构建和重建；出现问题时保留日志并只恢复本次触及的配置或服务。该例外不允许覆盖整个 Compose、删除卷／工作区、`docker compose down -v`、`--remove-orphans` 或 Docker 全局清理。

若已有仅含目标主机、用户和部署根的旧 `.local/remote-deploy.env`，可将新增入口字段单独写入同目录的 `.local/remote-deploy.entrypoints.env`（同样 `chmod 600`）。预检仅在该文件是普通文件时合并读取它；新环境仍建议将全部字段保存在单一 `remote-deploy.env`。

### Commit 绑定的构建与更新

1. 在本地确认 `git status --short --branch`、目标 commit 和受影响测试；运行 `git diff --check`。
2. 从目标 commit 使用 `git archive` 创建不可变源码包，记录 SHA-256，传至已由预检验证的私有构建目录，并在服务器再次校验 SHA-256。
3. 在服务器从该包构建所需 `linux/amd64` 镜像，检查 `docker image inspect` 输出为 `linux/amd64`。
4. 验证远端 Compose，再按影响范围 force-recreate。普通 `platform` 变更先运行 `migration`，随后只更新 `api`、`stream-api`、`worker`；不得重建 `runtime-provider`。只有 `runtime` 变更才在迁移后同步更新 `runtime-provider`、`api`、`stream-api`、`worker`，且须先确认活跃会话的中断影响。仅更新 Web 时只更新 `web`。
5. 检查服务健康、带 `/flowweave/` 前缀的 API/静态资源、Agent 深层路由及 FastGPT 根登录页。

### 普通发布的容量收尾

普通 `web`／`platform` 发布统一用受管最小上下文，替代在构建根中不断复制完整仓库或创建临时 release
目录。以下命令仍使用本机受保护配置；工具不会输出私有目标或服务器路径，也不替换服务或服务器配置：

```bash
make remote-release-prepare COMMIT=<已提交 SHA> SCOPE=<web|platform>
```

工具先执行正式部署预检，再以目标 commit 的 `git archive` 流直接解包到声明构建根内的
`release-<完整 SHA>-<scope>`；只包含该服务 Dockerfile 所需的构建输入，不包含历史 SDK 归档、测试、
文档或其他服务源码。没有中间远端 tar 包。同一 commit／scope 的目录不可覆盖；状态、源归档 SHA-256
及时间记录在权限受限的 `.flowweave-release.json` 中。仓库 `.dockerignore` 另外排除本机私有配置、
临时产物、依赖目录、缓存和测试输出，防止普通 Docker build 上传这些内容。

从该上下文按前述步骤构建镜像，必须添加
`--label org.opencontainers.image.revision=<完整 SHA>`，检查 linux/amd64，保留旧镜像 rollback tag，
再通过已验证 Compose/env 迁移和定向更新；普通 platform 仍同步更新 api／stream-api／worker，
不重建 Runtime Provider。完成前述路由、静态资源及真实页面验收后，发布必须以容量收尾结束：

```bash
make remote-release-finish COMMIT=<同一已提交 SHA> SCOPE=<web|platform>
```

该命令显式授权本次受管发布资产回收：先复核相关服务均运行同一 commit 的 linux/amd64 镜像，
api／stream-api 的 health、readiness 和 Web 容器 HTTP 入口通过后才标记发布成功。然后打印候选并
回收超过每个 scope 最近三份成功上下文的旧目录；当前镜像、任何运行或退出容器和最近三个 rollback
镜像的 revision，以及被挂载／进程 cwd／打开文件引用的目录始终保留。每次删除前重新读取引用并核对
目录身份；未知、无 marker、旧版目录和符号链接不自动删除。失败／未完成上下文至少保留七天，仍被
引用的上下文不删除；保护集合中存在缺少 revision 的旧镜像时，所有失败／未完成上下文继续保留。
成功上下文必须已通过 finish 的镜像身份门禁，其相关镜像必有 revision，不受旧无标签镜像影响。
最后用现有镜像保留脚本执行只读候选核验和精确 rollback tag 回收，不清理共享
BuildKit 缓存、容器、卷或 Workspace。单次超过 100 个目录会在删除前拒绝。

只读审计使用 `make remote-release-audit COMMIT=<SHA> SCOPE=<web|platform>`。
Runtime 镜像／Provider 发布继续走独立 `runtime` 预检与活跃会话门禁；本工具不将其降级为普通发布。
历史目录需单独只读盘点和授权，不能因文件名相似将其纳入自动删除。

服务器的持久数据包括 PostgreSQL、Artifact volume 与 Workspace bind mount。普通发布绝不执行 `docker compose down -v`，不删除数据、不覆盖环境文件，也不使用 `--remove-orphans` 忽略或删除 `stream-api`。

公网前缀部署还须在全新浏览器上下文确认 Network 请求使用 `/flowweave/api/v1/...`，而不是被 FastGPT 接管的根路径 `/api/v1/...`。Web 客户端自行追加 `/api/v1`；`VITE_API_BASE_URL` 只能配置 origin／部署前缀，不能包含 `/api/v1`。标准前缀构建使用 `VITE_BASE_PATH=/flowweave/`、空 `VITE_API_BASE_URL`，由客户端采用部署前缀。

### Alembic 版本号容量检查

Alembic 默认的 `alembic_version.version_num` 是 `VARCHAR(32)`，而 `0138_admin_resource_cleanup_operations` 的编号为 38 个字符。FR-565 的源码在 0138 开头自动将不足 128 字符的有界 VARCHAR 列扩为 `VARCHAR(128)`，随后才执行原有建表和版本记录；已有更宽 VARCHAR、无界 VARCHAR 或 TEXT 保持不变。扩宽与本次迁移同一事务，失败整体回滚，可直接重跑正式 migration 服务。扩宽锁等待上限为 5 秒，成功后恢复调用方原有 lock_timeout；降级不缩窄元数据列。

此兼容仅在包含 FR-565 源码的迁移入口执行。尚未包含修复的旧镜像仍须先检查容量，并在经过预检的目标上以有界锁等待扩为 `VARCHAR(128)`，再重跑；FR-564 已实跑该操作。不得改写已有 revision、手动 stamp head 或删除业务表代替迁移。已到达 0138 的数据库不会重执行该迁移。

可复现容量回归要求通过 `TEST_DATABASE_URL` 提供独立测试 PostgreSQL，并允许创建临时数据库；脚本仅迁移及移除自己创建的唯一数据库，不启动本地服务：

```bash
cd services/platform
uv run python scripts/migration_version_check.py --metadata-only
```

`--metadata-only` 使用无操作的 0137 前置 fixture、原样 0138 与正式迁移 env，验证空 Alembic 元数据、旧 32 字符列、失败回滚／重试、锁等待和既有宽列。这不等于完整业务迁移链验收；去掉该参数会执行真实完整迁移链。

### 历史空库迁移检查

FR-566 将 0003 中 `node_runs` 的定义固定为引入 `name` 前的结构，避免读取当前 ORM 提前创建未来字段；0092 仍正常添加可空 `VARCHAR(220)`。已经执行 0003 的数据库不会重跑该建表代码，revision 和现有表不变。使用独立 TEST_DATABASE_URL 执行真实迁移链与回滚／记录保留专项：

```bash
cd services/platform
uv run python scripts/migration_node_run_check.py
# 另外从独立空库向 head 验证；后续失败会以非零退出码返回
uv run python scripts/migration_node_run_check.py --probe-head
```

FR-566 的 PostgreSQL 16.9 实跑已通过真实空库到 0092、0092 故障回滚／重试与旧记录保留。但完整空库链随后在 0093 因重复添加 `flow_runs.schedule_id` 失败，由 FR-567 单独修复；仍不能声称完整空库初始化已通过。上述脚本只创建／迁移／移除自己的唯一临时数据库，不连接生产库执行迁移，也不启动本地服务。

## 回滚与容量维护

发布前记录旧 image ID 并创建时间戳 rollback tag。发生回归时先保留日志和数据库错误状态，再将受影响服务恢复到该镜像并 `--no-deps --force-recreate`。

远端 Docker 容量维护默认是只读审计：

```bash
scripts/maintain-remote-docker-retention.sh --config .local/remote-deploy.env
```

任何删除操作需要明确授权、先审阅 dry-run，并遵循脚本的确认参数。不要使用 `docker system prune`、`docker image prune -a`、强制删除镜像或清理 volume/Workspace 作为日常维护手段。

### 环境 HOME 缓存边界

固定 Runtime 镜像构建及新建 Runtime／环境配置终端默认关闭 uv、pip 的持久下载缓存，npm 下载缓存
写入有 1 GiB 上限的 `/tmp` tmpfs。HOME 初始化只排除已知的 uv／pip／pnpm／node 下载缓存和
`.npm`，仍保留认证、配置、已安装工具以及 `.cache/ms-playwright` 中的浏览器。已有 HOME 不因初始化
被清空；现有会话不为启用该默认值强制 replacement。用户仍可显式启用下载缓存；此时缓存生命周期
由其环境用途决定，不能与会话、附件、工作区或已安装依赖混为一类。

已有 uv 缓存维护前须确认没有包安装进程、没有安装目录以 symlink 依赖缓存；用原生 `uv cache clean`
回收下载缓存并检查实际可用空间。容器维护助手应挂载缓存的父目录，避免直接挂载 cache root 导致
删除挂载点时返回 EBUSY。JetBrains 缓存只在相关进程和文件引用均不存在时处理，配置与项目保留。
