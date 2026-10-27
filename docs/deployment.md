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

### 独立 Agent Runtime 基线同步

每次推进或发布 FlowWeave 受治理的 OpenHands baseline，都必须将独立 Agent Workspace Runtime
同步到本次 source lock；不能以 SDK 已提交、镜像已构建或平台服务已更新代替运行容器升级。
这项要求覆盖已有会话使用的活跃 generation，不能只验证新建会话。浮动 baseline HEAD
尚未进入本次 source lock 的后续开发不自动成为发布输入。

1. 固定已提交的 FlowWeave 版本，核对 `infra/openhands/source.lock.json`、SDK 不可变源码归档、
   四包版本、Dockerfile 和 provenance 一致；执行正式 `runtime` 远端预检。
2. 从同一 commit 的最小构建输入构建 `linux/amd64` Runtime 镜像，执行
   `infra/openhands/contract_check.py`，核对实际镜像 digest、源码 commit、归档 SHA-256 和所需
   capability。切换前保留旧镜像的精确 rollback tag、原配置及服务日志。
3. 通过平台正式 Runtime 连接读取目标 Agent Workspace 的全部 OpenHands 原生活动状态，
   `running`／`starting`／`executing`／`stopping` 任一存在时不得切换；等待安全窗口后再次检查。
   保存原 binding／conversation 身份、持久挂载和恢复验证锚点，并在服务器侧保护原生持久状态备份。
4. 确认平台配置的 Agent Runtime 镜像引用已经解析到目标 digest 后，使用平台正式 replacement：
   `flowweave agent runtime-replace <workspace-id> -H 'Idempotency-Key: <本次唯一值>'`。
   先 `--dry-run` 核对请求；异步受理后轮询既有 Runtime，不重复提交，也不手工删除容器、
   清空目录或修改数据库 generation。沿用原 Workspace、原生 Conversation 持久状态和 Secret Reference。
5. 验收新的 active generation：实际容器 image digest、四包版本、`/server_info.build_git_sha`、
   provenance 必须与本次 source lock 一致；所需 capability 必须存在。以原 conversation ID
   读取原正式事件，核对 binding 身份及 Workspace／持久挂载保持，验证 readiness 和平台健康。
   认证目录动态同步变更还必须验证 `conversation_dynamic_context_v1` 及原生 late-secret context 契约。
   失败时保留日志，只按原镜像与受保护状态备份恢复受影响 Runtime，不清理业务数据。
6. 在进度文档记录代码 commit、source commit、镜像 digest、generation 和实际验证结果。
   若活跃会话或恢复验证阻止升级，明确记录未完成，不能把本次 baseline 发布记为同步完成。

此流程不改写已发布 Environment Version 或既有 FlowRun 的冻结镜像；这些资源仍按各自版本与
Runtime 生命周期发布。仅升级独立 Agent Runtime 镜像和 generation 时，不无故重启 Provider
或平台服务；确需更新 Provider／协议时，仍执行 `runtime` 范围的同步发布与全部受影响 Runtime 门禁。

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
回收超过 24 小时且全目录无近期写入的受管上下文，包括失败／未完成上下文。源码目录不因镜像
revision 而保留；回滚依靠已构建镜像。被挂载／进程 cwd／打开文件引用的目录始终保留，删除前
重读引用并核对目录身份。普通发布不自动删除未知、无 marker、旧版目录或符号链接。
平台／Web 各保留当前镜像和一个不同 image ID 的最新 rollback 镜像，以及所有容器引用和带其他
仓库 tag 的镜像；其余平台／Web 历史 tag 精确回收，不使用 force。单次超过 100 个目录会拒绝。
发布收尾不清理共享 BuildKit、容器、卷或 Workspace；每日任务按单独显式启用的配置维护缓存和
已核验的历史构建资产，见 storage-maintenance.md。

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

FR-566 的 PostgreSQL 16.9 实跑已通过真实空库到 0092、0092 故障回滚／重试与旧记录保留。FR-567 进一步将 0003 的 `flow_runs` 固定为初始十字段及历史约束，让后续迁移顺序引入环境、Lark、自动运行和调度字段；0093 仍正常添加两个可空调度标识及索引。已经执行 0003 的数据库不会重跑建表，已有 revision 不变。调度专项：

```bash
cd services/platform
uv run python scripts/migration_flow_run_check.py
# 完整链仍会以非零退出码报告后续独立阻塞
uv run python scripts/migration_flow_run_check.py --probe-head
```

FR-567 远端隔离 PostgreSQL 实跑七项通过，覆盖真实链到 0093、失败回滚／重试和既有运行／调度记录保留；FR-566 七项回归也通过。FR-571 已固定 0004 的产物／绑定历史结构，消除提前引入的租户与 Runtime 字段；FR-572 进一步固定 0006 的 CapabilityImport 历史结构。FR-574／FR-575 已固定 0002 的 FlowEdge／FlowNode 历史结构。FR-576 已固定 0010 的端口映射历史结构。FR-577 已固定 0005 的门禁审计历史结构。FR-578 已固定 GatePolicy。FR-579 已固定 HumanAction 并验证用户内幂等转换。FR-580 已固定 RunEvent。FR-581 已固定 BackgroundTask；当前源码的真实独立空库完整链已通过 `0146_shared_flow_runs`，且完整 0094→0095 任务数据矩阵通过。FR-582 已完成 17 表混合数据的完整 0095 和当前 head 最终验收。上述脚本只创建／迁移／移除自己的唯一临时数据库，不连接生产库执行迁移，也不启动本地服务。

Artifact 专项入口：

```bash
cd services/platform
uv run python scripts/migration_artifact_check.py
uv run python scripts/migration_artifact_check.py --probe-head
```

该专项真实迁移空库至 0094，然后单独执行原样 0095 的 Artifact／Binding 租户 DDL，验证回填、强制 RLS、事务失败回滚与记录保留；不写入或 stamp 0095，不能替代完整 revision 验收。FR-571 八项通过，FR-566／FR-567 共十四项回归通过；`--probe-head` 的完整链仍会传播后续独立阻塞。只使用独立 TEST_DATABASE_URL、唯一临时数据库及临时角色。

CapabilityImport 专项入口：

```bash
cd services/platform
uv run python scripts/migration_capability_import_check.py
uv run python scripts/migration_capability_import_check.py --probe-head
```

FR-572 八项验证历史十二字段、索引、0006 原子回滚／重试、真实链至 0094 的导入记录保留，以及原样 0095 导入表 owner 回填／RLS 隔离；不记录或 stamp 0095。导入夹具覆盖 VALIDATED／EXPIRED，不覆盖 COMMITTED 内容重编译。既有三组回归共二十二项通过；完整链仍有后续门禁配置阻塞，不能以部分 DDL 成功代替完整 revision 验收。脚本只使用独立 TEST_DATABASE_URL，移除自身唯一临时数据库和角色，不启动服务。

FlowEdge 专项入口：

```bash
cd services/platform
uv run python scripts/migration_flow_edge_check.py
uv run python scripts/migration_flow_edge_check.py --probe-head
```

FR-574 八项验证历史控制边结构、0002 回滚／重试、真实链至 0094 的身份和顺序保留，以及原样 0095 FlowEdge owner 回填／RLS；不记录或 stamp 0095。FR-575 已将夹具调整为真实 NodeAsset 和不带 owner 的历史节点，通过原始 FK 创建控制边。既有四组回归三十项通过；完整链仍在门禁配置重复租户字段阻塞。脚本只使用独立 TEST_DATABASE_URL 和唯一临时数据库／角色，不启动服务。

FlowNode 专项入口：

```bash
cd services/platform
uv run python scripts/migration_flow_node_check.py
uv run python scripts/migration_flow_node_check.py --probe-head
```

FR-575 八项验证历史节点八字段、索引和 FK、0002 回滚／重试、真实链至 0094 的节点身份／配置／坐标保留，以及原样 0095 FlowNode owner 回填／RLS，不记录或 stamp 0095。更新后的 FlowEdge 及其他专项共三十八项回归通过；完整链仍在 GatePolicy 阻塞。只使用独立 TEST_DATABASE_URL 和自身唯一临时数据库／角色，不启动服务。

FlowPortMapping 专项入口：

```bash
cd services/platform
uv run python scripts/migration_flow_port_mapping_check.py
uv run python scripts/migration_flow_port_mapping_check.py --probe-head
```

FR-576 八项验证 0010 历史六字段、唯一目标输入键／索引／FK、原子回滚及真实链至 0094 的映射保留，以及原样 0095 owner 回填／RLS；不记录或 stamp 0095。既有回归四十六项通过；完整链仍在 GatePolicy 阻塞。只使用独立 TEST_DATABASE_URL 和自身唯一临时数据库／角色，不启动服务。

GateEvaluation 专项入口：

```bash
cd services/platform
uv run python scripts/migration_gate_evaluation_check.py
uv run python scripts/migration_gate_evaluation_check.py --probe-head
```

FR-577 八项验证 0005 历史十三字段、审计唯一键／索引／FK、原子回滚与真实空链至 0094，以及原样 0095 owner 回填／RLS 后 FAIL／PASS 审计十三字段保持。夹具在 0094 植入，不覆盖有审计记录的早期完整历史迁移。既有五十四项回归通过；完整链仍在 GatePolicy 阻塞，不记录或 stamp 0095。只使用独立 TEST_DATABASE_URL 和自身唯一临时数据库／角色，不启动服务。

## 回滚与容量维护

发布前记录旧 image ID 并创建时间戳 rollback tag。发生回归时先保留日志和数据库错误状态，再将受影响服务恢复到该镜像并 `--no-deps --force-recreate`。

远端 Docker 容量维护默认是只读审计：

```bash
scripts/maintain-remote-docker-retention.sh --config .local/remote-deploy.env
```

任何删除操作需要明确授权、先审阅 dry-run，并遵循脚本的确认参数。不要使用 `docker system prune`、`docker image prune -a`、强制删除镜像或清理 volume/Workspace 作为日常维护手段。

### 原生 Linux 定时容量维护

需要持续回收受管发布上下文与精确 rollback tags 时，使用[定时维护说明](storage-maintenance.md)
安装固定提交的 systemd 服务／timer；安装先预检、审计并实际运行一次，再启用每日北京时间 03:30
任务。它与发布收尾共用锁，保留当前／容器引用及最近一个回滚版本、最近 24 小时的构建资产；
显式开启后清理超过 24 小时未使用的构建缓存，保留 HOME、卷、Workspace 和备份，不重启生产服务。

### 环境 HOME 缓存边界

固定 Runtime 镜像构建及新建 Runtime／环境配置终端默认关闭 uv、pip 的持久下载缓存，npm 下载缓存
写入有 1 GiB 上限的 `/tmp` tmpfs。HOME 初始化只排除已知的 uv／pip／pnpm／node 下载缓存和
`.npm`，仍保留认证、配置、已安装工具以及 `.cache/ms-playwright` 中的浏览器。已有 HOME 不因初始化
被清空；现有会话不为启用该默认值强制 replacement。用户仍可显式启用下载缓存；此时缓存生命周期
由其环境用途决定，不能与会话、附件、工作区或已安装依赖混为一类。

已有 uv 缓存维护前须确认没有包安装进程、没有安装目录以 symlink 依赖缓存；用原生 `uv cache clean`
回收下载缓存并检查实际可用空间。容器维护助手应挂载缓存的父目录，避免直接挂载 cache root 导致
删除挂载点时返回 EBUSY。JetBrains 缓存只在相关进程和文件引用均不存在时处理，配置与项目保留。


GatePolicy 专项：`uv run python scripts/migration_gate_policy_check.py`（在 services/platform 执行）；追加 `--probe-head` 运行真实全链并传播后续错误。九项专项及六十二项回归通过，验证正数 timeout 行为与配置保持；专项原样执行该表 0095 _add_owner，不记录／stamp 0095。完整链尚未通过。


HumanAction 专项：`uv run python scripts/migration_human_action_check.py`（services/platform）；`--probe-head` 真实执行全链。九项专项加七十一项回归通过；原样 0095 表 DDL 和该表唯一键元数据子集验证不记录／stamp 0095，不代替完整 revision。


RunEvent 专项：`uv run python scripts/migration_run_event_check.py`（services/platform）；追加 `--probe-head` 执行真实全链。九项专项与八十项回归通过，数据库 cursor sequence 继续递增、事件七字段保持；仅该表 owner DDL 不代替完整 0095，当前 BackgroundTask 阻塞仍存在。


BackgroundTask 专项：`uv run python scripts/migration_background_task_check.py --probe-head`（services/platform）。本专项执行完整正式 0095 迁移、故障回滚／重试／往返、用户内幂等、无 RLS 全局读取，并验证 populated tasks 与独立空库均到 head，后期队列索引保持正确。十二项专项加八十九项回归与空库 head 共 102 项通过；没有连接生产库或部署镜像。


最终完整链验收：在 services/platform 执行 `uv run python scripts/migration_chain_check.py`，只接收独立 TEST_DATABASE_URL。七项验证 17 表混合样本的完整 0095 故障回滚／重试／往返、owner/RLS 投影、当前 head 原字段保持、long revision 容量和另一独立空库完整链；全部通过。前序专项针对单表的成功结论仍保留原验收边界，最终脚本使用完整正式 revisions，未 stamp／跳步。历史空库链本轮已通过，生产尚未部署这些迁移源码修复。
