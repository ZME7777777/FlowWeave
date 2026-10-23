# 受限会话历史 MCP（FR-651／FR-652）

FlowWeave 提供只读历史服务；OpenHands 使用其正式远程 MCP 机制调用服务，无须修改
OpenHands 源码。当前已实现授权、分页和长正文读取，并验证固定 SDK 的原生加载／恢复；
尚未自动注册 Runtime 能力或改变拖拽引用发送。

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
| `event_id` | 可选的正式消息事件 ID，启用单条正文分段读取 |
| `content_offset` | 可见正文中的 Unicode 码点位置，默认 0；非零时必须提供 event_id |

来源会话由授权记录确定，参数不能替换来源 ID。输出包括 `source_binding_id`、
`messages`、`next_cursor`、`history_cursor`、`query_scope=current_page` 和长度上限；
空匹配页仍保留历史游标。消息含正式 event ID、来源角色、可见正文、原始长度及
`truncated`。工具结果、系统消息与隐藏引用传输背景不进入输出。

预览被截断时，使用 `{"event_id":"正式事件ID","content_offset":0,"max_content_chars":2000}`
读取首段，再将 `message.next_content_offset` 作为下一次位置，直到其为 null。
单条模式返回 `source_binding_id` 与 `message`，后者包含 `event_id`、`source`、`content`、
`content_chars`、`content_offset`、`next_content_offset` 和 `truncated`。位置针对分页输出
同样的可见正文，按 Unicode 码点计数，不能使用 UTF-8 字节或 JavaScript UTF-16 长度。
`event_id` 不能与 `history_cursor` 或 `query` 混用；超出正文长度的位置被拒绝，等于
正文长度时返回空段和 null 下一位置。服务只在授权的原生会话中按 ID 读取该事件，
不扫描完整历史；非消息／系统／工具事件和身份不匹配的事件不可读取。

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

FR-652 在 `.tmp/` 中从固定 commit 和 frozen lock 建立隔离 SDK `1.54.0` 环境；
探针 `infra/openhands/reference_history_check.py` 验证 vendor 归档 SHA-256、安装版本及
SDK 全部 Python 源码与归档逐文件一致。通过平台正式能力物化函数生成的 marketplace，
在已经初始化的 `LocalConversation` 中调用 `update_secrets`／`load_plugin`，随后用
原生 MCP Tool executor 通过真实本地 TCP 调用查询服务；不调用 LLM。使用 Cipher 保存后，
确认会话与能力 JSON 不含明文凭据，以同一 Cipher、原 conversation ID 和 `agent=None`
恢复持久 agent、Secret Registry 和正式事件 ID，并再次原生调用 MCP。数据与授权为
受控替身；该证据不替代完整 Agent Server 镜像、真实数据库或真实模型业务验收。

可复现运行：在平台目录设置 `HISTORY_MCP_NATIVE_PYTHON` 为上述固定源码环境的 Python，
执行 `pytest tests/test_history_mcp.py::test_locked_sdk_native_plugin_secret_and_restore`。
设置 `HISTORY_MCP_CLIENT_PYTHON` 可同时执行官方 MCP 客户端协议用例；两者不设置时对应
用例明确 skip。运行时指定 `--basetemp ../../.tmp/history-native-tests`，临时状态全部放入 `.tmp/`。

平台生成的 `.plugin/plugin.json` 包装由 SDK 的 Claude Code 格式回退加载器识别。
其 `.mcp.json` 必须先通过 FastMCP 配置校验，因此使用标准配置
`"headers":{"Authorization":"Bearer ${FLOWWEAVE_HISTORY_ACCESS}"}`；SDK 的结构化
`auth` 对象虽可用于原生 agent 配置，但不能直接放进该插件文件。占位符由正式
Secret Registry 展开，实际令牌不写入能力包。恢复时 `agent=None` 表示采用已保存的
agent，传入全新 agent 会替换原有配置，不能用于证明能力恢复。

认证头在插件加载时展开为客户端快照；仅更新 Secret Registry 不保证既有客户端换用
新凭据。重复加载同名插件也可能因不同 MCPClient 的重名工具冲突失败。下一片必须先
解决会话级凭据和来源授权的生命周期，不能简单轮换短期令牌并反复加载插件。

FR-653 必须先完成受治理 MCP version／digest／Manifest、受限凭据续期与撤销和既有
会话接入，再切换整会话引用为元数据。不得将短期令牌
直接嵌入 Plugin 文件或用户消息，也不得在能力不可用时悄悄丢掉来源上下文。

数据库新增迁移 `0159_agent_history_grants`，启用强制用户 RLS。现有验证包含 SQLite
授权持久化与 PostgreSQL 离线迁移 SQL；真实 PostgreSQL upgrade／downgrade、固定
Runtime 验收及部署仍需后续执行。
