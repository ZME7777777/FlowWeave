# 环境配置参考

从 `.env.example` 创建 `.env` 后按部署环境填写。本文件只说明需要理解或调整的变量；完整默认值以 `.env.example` 与 `infra/compose.yaml` 为准。

## 必填项

| 变量 | 要求 |
| --- | --- |
| `FLOWWEAVE_RUNTIME_HOST_WORKSPACE_ROOT` | Docker daemon 可见的绝对目录。存放持久工作区，不能使用相对路径。 |
| `SANDBOX_RUNTIME_NETWORK_MODE` | `egress`（需要访问模型/MCP）或 `isolated`（仅访问显式连接的资源）。 |
| `DOCKER_CONTROLLER_API_KEY` | 至少 32 字符，供 API 调用 Runtime Provider。 |
| `DOCKER_CONTROLLER_WORKER_API_KEY` | 至少 32 字符，且必须与 API key 不同，供 Worker 调用 Runtime Provider。 |

## 数据与平台安全

| 变量 | 用途 |
| --- | --- |
| `POSTGRES_DB` / `POSTGRES_USER` / `POSTGRES_PASSWORD` | PostgreSQL 数据库和账号。共享或生产环境必须替换示例密码。 |
| `CREDENTIALS_MASTER_KEY` | 加密保存的模型服务凭据主密钥；生产必须设置安全随机值。 |
| `OPENHANDS_SESSION_API_KEY` | Runtime 会话访问密钥；生产必须替换示例值。 |
| `FLOWWEAVE_ADMIN_PASSWORD` / `FLOWWEAVE_USER_PASSWORD` | 可选的本地/受控部署账号配置。 |
| `FLOWWEAVE_BIND_ADDRESS` | Compose 暴露服务的绑定地址；默认 `127.0.0.1`。 |
| `POSTGRES_PORT` | 宿主机 PostgreSQL 端口；默认 `55432`。 |

密钥只能保存在受权限保护的 `.env` 或受管 Secret Store 中。不要写入 Docker image、Runtime Manifest、Snapshot、工作区、日志或测试夹具。

## Runtime 与工作区

| 变量 | 用途 |
| --- | --- |
| `RUNTIME_ADAPTER` | 正常运行使用 `openhands`；`mock` 只用于明确的测试。 |
| `RUNTIME_BACKGROUND_SEARCH_PER_RUNTIME_CONCURRENCY` / `RUNTIME_BACKGROUND_SEARCH_SLOT_TIMEOUT_SECONDS` / `RUNTIME_BACKGROUND_SEARCH_PAGE_TIMEOUT_SECONDS` | 会话全文搜索每个 Runtime 的后台并发、排队等待及单页请求最长时间；默认 `1`／`300`／`300` 秒。搜索使用独立 HTTP 连接池，不占用 hydration 的正式读取舱壁；hydration 活跃时搜索会在下一页前让出。 |
| 会话搜索结果 | 搜索没有会话数、页数或命中数上限；它会以低优先级逐页遍历所选工作区的完整原生 EventLog，直到结果结束。每个 Runtime 同时只允许一个搜索，每页最多 `2` 秒且 hydration 活跃时先让出，因此搜索可能较慢，但不会因数量上限而遗漏较早结果。 |
| `AGENT_WORKSPACE_RUNTIME_IMAGE` | Agent Workspace 使用的固定 Runtime image。 |
| `AGENT_WORKSPACE_RUNTIME_MEMORY` / `AGENT_WORKSPACE_RUNTIME_CPUS` | Agent Workspace 专属内存／CPU 限额；默认分别为 `4g`、`3.0`，不影响 FlowRun 或 Environment Runtime。 |
| `OPENHANDS_RUNTIME_BUILDER_IMAGE` | 发布 Environment Version 时用于官方 OpenHands 构建链的镜像。 |
| `SANDBOX_MANAGER_SCOPE` | Runtime Provider 所管理资源的作用域标签。 |
| `SANDBOX_RUNTIME_IDLE_TTL_SECONDS` / `SANDBOX_RUNTIME_HARD_TTL_SECONDS` | 可控 Runtime 的空闲/硬性存活上限。 |
| `SANDBOX_STORAGE_SIZE` | 动态 Runtime 的存储配额。 |
| `DOCKER_SOCKET_GID` | Linux Docker socket 所属 GID；Docker Desktop 使用 `0`，Linux 可用 `stat -c '%g' /var/run/docker.sock` 查询。 |

持久根目录由平台按运行分配。不要手工递归 `chmod`、`chown`、删除 `.agent-workspaces` 或 OpenHands state 目录；权限和所有权不符合预期会使分配失败。

## API 并发通道

`API_BLOCKING_POOL_SIZE` 是每个 API worker 的同步数据库／线程总预算，不是每个 Runtime 的并发数。
默认 `8` 分为首屏 hydration `2`、消息派发 `1`、工作区文件／Git `1`、慢生命周期 `1`、普通交互／变更 `3`。
工作区通道从已有预算中预留，SQL pool 不允许 overflow；原有独立 history pool 继续处理历史事件和侧栏分页，
不会再被文件扫描或 Git 操作占用。`/metrics` 的数据库 pool 指标可分别观察 `workspace` 和 `history`。
API blocking 预算小于 `5` 时没有额外预留空间，工作区仍与 history 共用同一个 semaphore、executor 和 SQL pool；
单槽 stream-api 与 Worker 保持原有预算。默认 Compose 总连接预算仍为 `88`，没有增加 PostgreSQL 上限。

模型切换、能力加载、凭据同步、fork、删除、原生重命名和节点创建／宿主预置走独立 lifecycle 通道，
确认决策保留普通 mutation 准入，发送／首屏／中断继续使用各自保留通道。`/metrics` 的数据库 pool 指标包含
`lifecycle`。API blocking 预算小于 `6` 或 Worker 不分配 lifecycle 保留容量，回退到原 mutation 的同一个
准入、executor 与 SQL pool。默认单槽 lifecycle 限制每进程的慢变更压力，不是跨进程或跨 Runtime 的全局锁；
同会话的数据库行锁及 OpenHands 原生状态锁仍然有效。

`RUNTIME_LIFECYCLE_SATURATED` 表示慢变更尚未开始执行，等待 `BLOCKING_POOL_TIMEOUT_SECONDS` 后未获得槽位。
取消排队请求不会启动写操作；取消已提交线程的浏览器请求不代表撤销写操作，槽位一直持有到实际执行、
事务提交／回滚及回调完成。此通道不自动重试写操作，也不把既有同步 API 改为后台任务。

`RUNTIME_AUXILIARY_SATURATED` 表示 API 工作区通道排队超时；
`RUNTIME_AUXILIARY_READ_SATURATED` 则表示适配器的每 Runtime 展示性读取通道饱和，两者不能混淆。
固定 OpenHands 的会话详情、按 ID 读取事件及无过滤事件窗口共用独立 read executor，默认 `8` 线程。
服务配置 `max_concurrent_reads`（或 Agent Server 启动环境 `OH_MAX_CONCURRENT_READS`）允许 `1`–`32`，
与后台搜索／Context 的两个线程及控制／生命周期通道隔离，deferred init 保留启动配置。
该配置属于 Agent Server；只写入平台 `.env` 不会调整已运行 Runtime。平台的
`RUNTIME_READ_PER_RUNTIME_CONCURRENCY` 仍默认每 API worker、每 generation `2`，四 worker 最多
提供 `8` 个正式读取工作单元，不应随 Runtime 线程数同时放大为每 worker `8`。线程扩容不增加数据库预算，
也不消除同会话原生锁等待；新来源版本须构建镜像并通过正式 Runtime 生命周期生效。
浏览器历史预取每页至少间隔 `1.5` 秒，
隐藏页面时停止并取消浏览器在途请求，恢复可见后从已加载的下一页继续。同入口游标完成后不自动重复扫描，
新入口游标仍可加载；这些措施不限制完整历史条数。取消浏览器请求不会提前释放仍在执行的后端线程。

## 网络、集成与开发体验

| 变量 | 用途 |
| --- | --- |
| `VITE_API_BASE_URL` | Web 构建时 API 基础地址。本地默认 `http://localhost:8080`；前缀部署必须使用正确的前缀地址。 |
| `MAVEN_SHARED_HOST_ROOT` | 可选的只读 Maven 根目录，需含 `Repository/` 和 `conf/settings.xml`，且必须是绝对路径。 |
| `IDE_SSH_HOST` / `IDE_SSH_USER` / `IDE_SSH_PORT` | 可选 JetBrains Gateway SSH Remote 入口。 |
| `PLUGIN_RESOLVER_ALLOWED_HOSTS` | Plugin resolver 可访问的来源域名白名单。 |
| `RATE_LIMIT_REDIS_URL` | 可选 Redis/Valkey 限流后端；未配置时每个 API 进程独立计数。 |
| `RATE_LIMIT_READ_REQUESTS_PER_MINUTE` | 每个登录用户的只读请求额度，默认 `600`；用于页面轮询、列表和状态查询。 |
| `RATE_LIMIT_USER_REQUESTS_PER_MINUTE` | 每个登录用户的普通操作请求额度，默认 `120`；不包含只读请求和会话消息。 |
| `RATE_LIMIT_CONVERSATION_MESSAGES_PER_MINUTE` | 同一用户在同一会话中的消息请求额度，默认 `20`。 |

三类请求分别计数，页面轮询不会消耗普通操作或会话消息额度。超限响应为 HTTP `429`，并携带 `Retry-After: 60`。

`egress` 只代表 Runtime 可以使用 Docker NAT；它不是出站白名单或代理策略。生产环境应在 Docker 主机或独立网络层施加出站控制。

## 环境版本与凭据

在 Web 中创建 Environment 后，使用 Setup Session 安装工具或完成受支持的登录，然后发布为不可变 Environment Version。发布记录实际 image digest；后续 FlowRun 使用已冻结版本，而非浮动 tag。

例如，需要 Lark CLI 时，在该 Environment 的 Setup 终端中执行：

```bash
lark-cli config init --new
lark-cli auth login --domain all
```

CLI 状态由 Controller 管理的环境凭据卷保存，不应复制到代码仓库、节点工作区、镜像层或 Agent 消息。
