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
| `AGENT_WORKSPACE_RUNTIME_IMAGE` | Agent Workspace 使用的固定 Runtime image。 |
| `OPENHANDS_RUNTIME_BUILDER_IMAGE` | 发布 Environment Version 时用于官方 OpenHands 构建链的镜像。 |
| `SANDBOX_MANAGER_SCOPE` | Runtime Provider 所管理资源的作用域标签。 |
| `SANDBOX_RUNTIME_IDLE_TTL_SECONDS` / `SANDBOX_RUNTIME_HARD_TTL_SECONDS` | 可控 Runtime 的空闲/硬性存活上限。 |
| `SANDBOX_STORAGE_SIZE` | 动态 Runtime 的存储配额。 |
| `DOCKER_SOCKET_GID` | Linux Docker socket 所属 GID；Docker Desktop 使用 `0`，Linux 可用 `stat -c '%g' /var/run/docker.sock` 查询。 |

持久根目录由平台按运行分配。不要手工递归 `chmod`、`chown`、删除 `.agent-workspaces` 或 OpenHands state 目录；权限和所有权不符合预期会使分配失败。

## 网络、集成与开发体验

| 变量 | 用途 |
| --- | --- |
| `VITE_API_BASE_URL` | Web 构建时 API 基础地址。本地默认 `http://localhost:8080`；前缀部署必须使用正确的前缀地址。 |
| `MAVEN_SHARED_HOST_ROOT` | 可选的只读 Maven 根目录，需含 `Repository/` 和 `conf/settings.xml`，且必须是绝对路径。 |
| `IDE_SSH_HOST` / `IDE_SSH_USER` / `IDE_SSH_PORT` | 可选 JetBrains Gateway SSH Remote 入口。 |
| `PLUGIN_RESOLVER_ALLOWED_HOSTS` | Plugin resolver 可访问的来源域名白名单。 |
| `RATE_LIMIT_REDIS_URL` | 可选 Redis 限流后端。 |

`egress` 只代表 Runtime 可以使用 Docker NAT；它不是出站白名单或代理策略。生产环境应在 Docker 主机或独立网络层施加出站控制。

## 环境版本与凭据

在 Web 中创建 Environment 后，使用 Setup Session 安装工具或完成受支持的登录，然后发布为不可变 Environment Version。发布记录实际 image digest；后续 FlowRun 使用已冻结版本，而非浮动 tag。

例如，需要 Lark CLI 时，在该 Environment 的 Setup 终端中执行：

```bash
lark-cli config init --new
lark-cli auth login --domain all
```

CLI 状态由 Controller 管理的环境凭据卷保存，不应复制到代码仓库、节点工作区、镜像层或 Agent 消息。
