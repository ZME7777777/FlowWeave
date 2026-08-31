# Development

## 进程

FlowWeave 由独立的 migration、API、Worker、Web 和 PostgreSQL 进程组成。服务不会在启动时自动改 Schema；Compose 先运行 migration job，再启动 API/Worker。

标准 Compose 环境同时启动 OpenHands Agent Server。Worker 会在创建会话时读取节点选择的模型服务、默认/指定模型及加密 API Key，并注入节点的提示词、输入产物、完整 Skill 包和 MCP Server；`RUNTIME_ADAPTER=mock` 仅供测试显式使用。OpenHands 容器与 API/Worker 通过宿主机 bind mount 共享工作区，默认提供终端、文件编辑和任务跟踪工具。

## 节点宿主工作区

默认根目录为 `var/workspaces`，可用绝对路径配置 `FLOWWEAVE_RUNTIME_HOST_WORKSPACE_ROOT` 覆盖。每个节点资产拥有可写目录 `nodes/<node-asset-id>`，其中：

- `skills/<能力名>`：从导入 ZIP 完整解压的 `SKILL.md`、scripts、references 和资源文件。
- `files`：用户自行放置的文本、附件或其他上下文。
- `repositories`：节点长期使用的代码仓库。
- `sessions/<run-id>/<node-run-id>/<attempt-no>`：各执行轮次的隔离工作目录。

上传的 MCP/Hook 配置与脚本由平台物化到 `.managed-assets/nodes/<node-asset-id>/`，Runtime 只在 `/runtime/capabilities/nodes/<node-asset-id>/` 看到独立只读挂载。脚本不位于节点可写挂载中，物化时校验路径、文件类型、数量、大小和 SHA-256 摘要。Agent 启动时会收到这些容器内绝对路径，MCP 配置通过 OpenHands `mcp_config` 注册为真实工具。能力仓库提供“表单配置”和“JSON 配置”两种视图，但底层始终是同一份 `mcpServers` JSON。新建弹窗一次发布一个 MCP 能力；左侧只区分“远程”和“本地”两种连接形态，Server 名称在右侧作为 OpenHands 唯一键单独填写。远程连接只能使用 URL、远程协议和远程认证字段，本地连接固定使用 stdio，只能使用 command、args、env、cwd 等命令字段；两类字段不可混用。命令型 MCP 只保存配置，对应 CLI 必须先在节点绑定的终端环境中安装并发布。聊天输入框用统一的 `$能力名` 引用 Skill 或 MCP，消息会保存结构化 `capability_refs`，不依赖模型自行猜测文本。

FlowWeave 的 MCP JSON 使用以下结构。新建弹窗一次只配置一个具名 Server。远程 Server 的 `transport` 支持 `streamable-http`（推荐）、`http` 或 `sse`；本地 Server 使用 `stdio`。输入中的兼容别名 `type` 会被规范化为 `transport`，`shttp` 会被规范化为 `http`。配置禁止保存 token、secret、password、Authorization 等敏感字段。

```json
{
  "mcpServers": {
    "remote": {
      "url": "https://mcp.example.com/mcp",
      "transport": "streamable-http",
      "description": "查询团队文档",
      "timeout": 30
    }
  }
}
```

本地连接对应的单 Server 配置为：

```json
{
  "mcpServers": {
    "local": {
      "command": "mcp-tool-server",
      "args": ["--stdio"],
      "transport": "stdio",
      "description": "调用终端环境中已安装的 MCP CLI"
    }
  }
}
```

Skill ZIP 的压缩包上限为 25 MiB、解压后总量上限为 100 MiB、单文件上限为 25 MiB。单 Skill 可把 `SKILL.md` 直接放在 ZIP 根目录；批量导入时，每个 Skill 目录各自包含一个 `SKILL.md`。ZIP 最多包含 5000 个原始条目，过滤 `__MACOSX`、`.DS_Store` 与 `._*` 后最多保留 1000 个有效条目；常见脚本、文档、图片及 `.jsx`、`.tsx`、`.html`、`.xml`、`.css` 等 Skill 资源可随包保存。Web 代理允许 40 MiB 请求体，用于容纳 Base64 编码产生的额外体积。

OpenHands 镜像提供 shell、Python、Node.js/npm/npx、uv/uvx、Git/SSH 与 `lark-cli`。平台不接收、不保存也不向 Runtime 注入 Lark OAuth token。每个终端环境拥有独立的 Controller 管理 Docker 卷；Setup 容器把它挂载到 `/root/.lark-cli`，发布后的 Runtime 把同一卷挂载到 `/home/flowweave/.lark-cli`。卷内容不会进入 `docker commit` 生成的镜像，也不会在不同环境之间共享。

## IDEA / JetBrains Gateway SSH Remote

要从 IDEA 打开当前 Agent 或 FlowRun 的持久工作区，启用 Docker 宿主机的 SSH，并在 `.env` 配置：

```dotenv
FLOWWEAVE_RUNTIME_HOST_WORKSPACE_ROOT=/srv/flowweave/workspaces
IDE_SSH_HOST=flowweave-dev.example.com
IDE_SSH_USER=flowweave
IDE_SSH_PORT=22
```

重启 `api` 和 `worker` 后，Agent 工作台右侧的“IDEA / Gateway”会显示并可复制“用户名、主机 / IP、端口、项目目录、SSH 命令”。每位连接者在自己的设备上选择其私钥；部署方只授权对应的公钥。FlowWeave 不接收、保存、显示或返回客户端私钥路径或内容。在目标为 Linux 时，JetBrains Gateway 选择 SSH，填入页面所示连接信息并选择自己的已授权私钥，连接后打开项目目录。这个目录是 Docker 宿主机上的持久 Workspace；不要连接 Runtime 容器内的 `/runtime/workspace/project`，因为 Runtime generation 可以被替换。

Linux 服务器的 SSH 服务、权限、部署、历史会话和 Gateway 操作步骤见 [IDEA / JetBrains Gateway SSH Remote](idea-gateway-ssh-remote.md)。

需要 Lark 能力时，在目标环境的 Setup 终端内运行 `lark-cli config init --new` 和 `lark-cli auth login --domain all`，按 CLI 给出的地址完成授权。发布前平台只调整卷内文件的 UID/GID，使非 root Runtime 可读写；删除环境时，Worker 在确认该环境没有存活 Sandbox 后通过所有权标签校验删除凭据卷。不要把 token、cookie 或 `.lark-cli` 内容复制到节点工作区、镜像层或 Agent 消息。

Agent 消息中的 `file:///workspaces/...`、`/workspaces/...` 或相对 Markdown 图片会通过消息级工作区图片接口转换为浏览器可访问的 HTTP 地址。接口将文件限定在该消息所属 Attempt 的工作目录内，只允许常见图片格式，阻止目录穿越和跨 Attempt 读取。

```bash
cd services/platform
uv sync --frozen
uv run alembic upgrade head
uv run uvicorn flowweave.bootstrap.api:create_app --reload --port 8080
uv run python -m flowweave.bootstrap.worker
```

Web：`pnpm --filter @flowweave/web dev`。

## Runtime 取消生命周期

取消流程会先把业务运行置为只读终态，再由持久化 `CANCEL_RUNTIME` 任务停止活动轮次拥有的全部 Runtime 会话。轮次通过独立的 `runtime_phase` 展示停止进度：`CANCELLING` 表示正在确认，`CANCELLED` 表示原 Runtime 已确认 Agent 不再运行，`CANCEL_FAILED` 表示重试耗尽。失败后可调用 `POST /api/v1/node-attempts/{attempt_id}/retry-runtime-cancel` 重试。

轮次和 Agent 会话都会持久化 `runtime_adapter`，避免配置切换后把旧 Mock 或 OpenHands 句柄发送给错误的执行器。OpenHands 返回会话不存在时按“已经停止”处理，使重复取消保持幂等。

## 认证与接口

所有公共接口使用 `/api/v1`，读写接口均可直接访问。命令支持 `Idempotency-Key`，错误体统一为 `{"error":{"code","message","details","request_id"}}`。

## 数据库和迁移

迁移链以 `0001_catalog`、`0002_flows`、`0003_runs`、`0004_artifacts`、`0005_execution` 为五段核心基线，并以前向迁移 `0006_capability_imports`、`0007_run_event_notify`、`0008_agent_conversations`、`0009_runtime_cancellation` 增加持久化导入、提交后通知、Agent 会话和 Runtime 取消闭环。运行 `make migration-check` 验证空库升级、回退到 `0005` 和再次升级。生产、单元、集成、架构与契约测试统一使用 PostgreSQL；生产包和测试均不包含 SQLite 兼容路径。未显式设置 `TEST_DATABASE_URL`/`DATABASE_URL` 时，pytest 与迁移检查自动使用固定镜像 `postgres:16.9-alpine3.21` 的 Testcontainer；设置 URL 时可复用外部 PostgreSQL 进行诊断。

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

标准 Compose 使用独立 `runtime-provider` 统一管理 Docker Runtime。只有 Runtime Provider 挂载 `/var/run/docker.sock`；API 与 Worker 通过带 Bearer 认证的固定高层接口访问，并与 Provider 一起接入独立的 `internal` 控制网络。Provider 不持有数据库、OAuth 或业务凭据；Runtime 创建还会校验 manager scope、确定性资源名、镜像发布标签、不可变规格签名和所有权标签。每个 FlowRun Runtime 使用独立 bridge 网络；只有当前 manager scope 下显式标记为 API 或 Worker 的客户端会接入：API 代理 OpenHands 原生会话交互，Worker 执行受管任务。Runtime 会从其他 Docker 网络断开，因此不同 Runtime 之间不共享二层网络。Runtime 使用数值 UID/GID 10001、只读根文件系统、受限 tmpfs，并挂载所属 FlowRun 的外置工作空间与 OpenHands 状态。

启动前必须设置两把互异且至少 32 字符的随机密钥 `DOCKER_CONTROLLER_API_KEY` 与 `DOCKER_CONTROLLER_WORKER_API_KEY`，例如分别运行 `openssl rand -hex 32`，并显式选择 `SANDBOX_RUNTIME_NETWORK_MODE=isolated|egress`。`isolated` 使用 Docker internal 网络、默认禁止 Runtime 直接出网；`egress` 用于访问外部模型供应商、MCP 和工具，但它只是开放 Docker NAT，不是域名白名单或受控代理，生产环境仍应在独立 Docker 主机/VM 上配置出口防火墙或代理。网络模式由 Runtime Provider 决定并写入所有权标签，客户端请求不能提升权限，旧网络或模式漂移会被拒绝复用。Linux 主机还应将 `DOCKER_SOCKET_GID` 设为 `stat -c '%g' /var/run/docker.sock` 的结果；Docker Desktop 默认使用 `0`。Runtime Provider 由 Bearer 密钥识别 API/Worker 主体：API 管理 Setup/终端/发布，并可在用户永久删除 FlowRun 时同步删除经过所有权复核的 Runtime；Worker 管理 Runtime 创建/替换、Gate、依赖构建和回收。`make infra-up` 会构建固定的 Gate 与依赖构建镜像；启动后运行 `make sandbox-smoke`，Smoke 会通过与生产相同的 Worker Provider 路径执行 Python/JavaScript 容器。不要把 Docker Socket 添加回 API 或 Worker，也不要把 Runtime Provider 暴露端口或接入 Runtime 网络。Docker Socket 等价于宿主管理权限；生产环境应进一步使用独立 Docker 主机或受限 VM 作为 Runtime Provider 的故障域。OpenSandbox Runtime 的上游基础镜像按 digest 固定；升级时必须显式更新 digest 并重跑镜像与 Sandbox Smoke 检查。
