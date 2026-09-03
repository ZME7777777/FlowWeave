# FlowWeave CLI

`flowweave` 是当前 FlowWeave 平台 API 的免登录命令行客户端。它只保存平台基础 URL，不会创建、保存或传输凭据。

## 设计说明

CLI 分为三层：

1. `config init` 配置当前部署唯一必要的平台事实：基础 URL，包含反向代理前缀。
2. `health` 与 `openapi` 分别用于安全连通性检查和在线契约发现。
3. `api` 是完整的平台接口面：它会向已配置平台发送 HTTP 方法、相对路径、JSON 请求体、查询参数和仅本次生效的请求头。`resource` 仅缩短常用集合路径，绝不成为第二份接口 schema。

因此，平台新部署的 REST 接口可以立刻通过 `api` 调用，无需等待 CLI 版本发布；CLI 仍以服务端 OpenAPI 契约为准。对于 multipart 和 WebSocket，使用显式的 `upload`、`ws` 命令，不把它们错误模拟为 JSON 请求。

## 安装与配置

在 Python 3.12 环境中安装平台包，然后一次性配置平台根地址。若部署地址包含 `/flowweave` 等前缀，必须保留。

```bash
cd services/platform
uv sync
uv run flowweave config init --base-url https://hq-ai.hszq8.com/flowweave
uv run flowweave health --ready
```

默认配置文件为 `~/.config/flowweave/config.toml`。如需使用项目本地或测试专用配置文件，可设置 `FLOWWEAVE_CONFIG_PATH`。

## 全平台接口访问

`flowweave api` 是稳定且完整的接口面：它会为相对资源路径加上 `/api/v1`，因此运行中平台暴露的每个 REST 路由都能直接调用，无需等待 CLI 发布。它接受内联 JSON 或 JSON 文件、重复查询参数和任意 HTTP 请求头。

```bash
# 先发现在线契约。
uv run flowweave openapi --paths

# 读取任意 API 资源。
uv run flowweave api get /flows
uv run flowweave api get /flow-runs -q limit=20

# 发送命令。若可重试的写接口契约要求幂等键，请传入 Idempotency-Key。
uv run flowweave api post /flows \
  --data-file ./flow.json \
  -H 'Idempotency-Key: create-flow-demo'

# 在不改变平台状态的情况下检查请求。
uv run flowweave api delete /flows/flow-id --dry-run
```

仅对 `/health`、`/openapi.json` 这类平台根路径使用 `--raw`；优先使用 `health` 与 `openapi` 快捷命令。

## 常用资源

`resource` 是常用集合路径的小型快捷层，不会隐藏 API 请求体或擅自补充默认值。

```bash
uv run flowweave resource flows list
uv run flowweave resource environments list
uv run flowweave resource capabilities get capability-id
uv run flowweave resource node-assets create --data-file ./node-asset.json
```

对于嵌套路由、WebSocket、文件上传或未被 `resource` 覆盖的接口，先执行 `flowweave openapi --paths`，再使用对应通用命令。

## 文件上传与 WebSocket 路由

对于不是 JSON REST 的平台接口，请使用通用传输命令：

```bash
# multipart 上传；可按需要重复传入 --form 或 --file。
uv run flowweave upload post /agent-workspaces/workspace-id/attachments \
  --file file=./brief.pdf

# 订阅 WebSocket 接口。无边界流可使用 Ctrl-C 停止。
uv run flowweave ws /agent-workspaces/workspace-id/runtime/stream
```

`ws --message-json` 会在连接后立即发送一条 JSON 消息；`--max-messages` 可将事件流限制为固定消息数，便于脚本使用。两者都会保留配置中的部署前缀，且绝不持久化请求头。

## 安全边界

当前平台没有终端用户登录接口，因此 CLI 有意不提供 `auth login`。CLI 会拒绝 URL 中的凭据和完整接口 URL，确保每次调用都限定在已配置平台内。`-H` 传入的请求头绝不持久化；将其视为仅本次生效的输入，并避免把密钥写入 shell 历史。
