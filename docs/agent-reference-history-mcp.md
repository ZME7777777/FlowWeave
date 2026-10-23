# 受限会话历史 MCP（FR-651）

FlowWeave 提供只读历史服务；OpenHands 使用其正式远程 MCP 机制调用服务，无须修改
OpenHands 源码。本片只实现授权和服务，不自动注册 Runtime 能力，不改变拖拽引用发送。

## 授权

已登录且有 Agent 会话权限的用户通过下列公开 API 显式授权：

`POST /api/v1/agent-workspaces/{workspace_id}/conversations/{target_binding_id}/history-grants`

请求包含 `source_binding_id` 与可选 `ttl_seconds`，时长默认 900 秒，范围 60–3600 秒。
两端必须是同用户、同 Agent Workspace 的 ACTIVE binding。响应 201 包含 `grant_id`、
`access_token`、`token_type=Bearer`、`expires_at`、`source_binding_id`；响应禁止缓存。
令牌只在签发响应中返回一次，数据库保存 SHA-256 哈希，不能从平台再次取回明文。

撤销使用同一路径追加 `/{grant_id}` 的 DELETE。删除来源或目标 binding 也清理授权。
授权独立于登录 Cookie；令牌不能调用普通平台 API，平台 Cookie 不能代替 MCP Bearer。
每次查询重新检查用户启用状态和当前 Agent 权限，不继承历史管理员权限。

## MCP

部署前缀之后的端点为 `/api/v1/agent-history-mcp`，认证仅使用 Authorization Bearer，
不得把令牌放进 URL、消息、日志、普通配置文件或能力包。服务采用 stateless Streamable
HTTP：POST 的 JSON-RPC 初始化、ping、tools/list、tools/call 返回 JSON，初始化通知
返回 202；不建立服务器会话，不开放写工具、通用 API 代理或其他会话枚举能力。

唯一工具 `read_referenced_conversation` 的参数：

| 参数 | 含义 |
| --- | --- |
| `history_cursor` | 上一页返回的正式历史游标；首次省略 |
| `query` | 可选的页内关键词，不是全会话全文搜索 |
| `max_content_chars` | 单条正文预览上限，默认 2000，范围 1–10000 |

来源会话由授权记录确定，参数不能替换来源 ID。输出包括 `source_binding_id`、
`messages`、`next_cursor`、`history_cursor`、`query_scope=current_page` 和长度上限；
空匹配页仍保留历史游标。消息含正式 event ID、来源角色、可见正文、原始长度及
`truncated`。工具结果、系统消息与隐藏引用传输背景不进入输出。超过预览上限的长正文
还没有范围读取接口，必须在后续接入片补齐，不能将预览当成完整正文。

读取复用既有低优先级 history lane 和 prepare → Runtime read → project 三段边界，
不持有数据库连接等待 Runtime。准备及投影均复核授权有效期、撤销、用户权限、两端
正式 Conversation／Runtime Session 身份；投影额外保留原有 generation、宿主与根目录
fence。每用户每分钟最多 60 次 MCP 请求；响应不缓存，审计记录用户与固定端点，不记录令牌。

## 原生契约取证与接入边界

本片读取 source lock 的固定 commit `ae178d86f4e0e268cf31f8f9c5deda892f4ebf12`，
没有修改该源码、lock 或镜像。`openhands-sdk/openhands/sdk/mcp/config.py` 定义正式
`MCPBearerAuthCredential` 和 SecretStr serializer；`openhands-agent-server/openhands/agent_server/models.py`
中的 `UpdateConversationRequest` 只更新 title／tags，不能通过它替换 MCP 配置。
现有 FlowWeave 能力热加载调用正式 `load_plugin`，后续应沿这条受治理链验证认证与加载。

本地测试通过官方 MCP Python 客户端对真实本地 TCP 服务执行初始化、列工具、调工具；
服务端数据与授权为受控测试替身。客户端来自本机旧 1.49.5 验证环境，不能据此声称
固定 1.54.0 Runtime 的会话加载／加密持久化已完成验收。

FR-652 必须先完成受治理 MCP version／digest／Manifest、原生认证加密持久化、短期
凭据续期与撤销、既有会话升级及长正文读取，再切换整会话引用为元数据。不得将短期令牌
直接嵌入 Plugin 文件或用户消息，也不得在能力不可用时悄悄丢掉来源上下文。

数据库新增迁移 `0159_agent_history_grants`，启用强制用户 RLS。现有验证包含 SQLite
授权持久化与 PostgreSQL 离线迁移 SQL；真实 PostgreSQL upgrade／downgrade、固定
Runtime 验收及部署仍需后续执行。
