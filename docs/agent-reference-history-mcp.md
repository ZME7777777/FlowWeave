# 受限会话历史 MCP（FR-659–FR-663）

FlowWeave 提供只读历史服务；OpenHands 使用其正式远程 MCP 机制调用服务，无须修改
OpenHands 源码。当前已实现授权、分页和长正文读取，并验证固定 SDK 的原生加载／恢复；
已接入拖拽整会话引用：发送时只携带服务端来源元数据，经原生 MCP 按需查询历史。
上线前仍须完成 FR-664 的真实数据库／固定 Runtime 镜像验收并配置下述固定能力。

## 会话配置中的内置项

Agent Workspace 的新建、既有会话和定时任务预设均显示“会话历史查询”，名称旁有独立
“内置”标签；默认勾选且不可取消，批量取消和只看已选也保留该项。已冻结版本和能力
仓库中的同名版本不会重复展示。此项由平台管理，不进入用户可选版本载荷，也不使用
缺少会话凭据的通用 MCP 检测／注册；通用能力写入口拒绝手动替换保留的 MCP 名称。

内置标识表示默认接入策略，不表示原生客户端已经连接或拥有全部历史权限。实际连接
仍在显式引用发送时完成，并须通过下述固定版本配置校验。未引用时不签发历史来源授权。
查询两端 binding 的 SQL 显式限制当前用户、Workspace 和宿主类型；管理员及受信 ORM
隔离豁免也不能跨用户查询。凭据、来源白名单和读取后的授权复核继续保持用户隔离。

## 自动接入配置

通过平台既有能力导入／发布流程，发布名为 `flowweave-reference-history` 的 MCP 能力，
冻结配置为（替换示例域名及部署前缀）：

```json
{"mcpServers":{"flowweave-reference-history":{"url":"https://platform.example.test/api/v1/agent-history-mcp","transport":"streamable-http","headers":{"Authorization":"Bearer ${FLOWWEAVE_HISTORY_ACCESS}"}}}}
```

在 API 与 stream-api 的受保护配置中显式设置 `AGENT_HISTORY_MCP_CAPABILITY_VERSION_ID`
为已发布版本 UUID、`AGENT_HISTORY_MCP_URL` 为与该版本完全一致的可信地址。默认均为空，
不根据用户消息、浏览器地址或请求 Host 猜测地址；未配置时引用发送明确失败并恢复草稿。
配置只选择不可变版本，不修改已发布能力；版本固定后，既有会话不自动迁移到其他版本。
检查版本类型／名称、配置 digest、持久 blob SHA-256、精确地址和 Secret 占位符；禁止
额外命令、任意认证字段或脚本。配置地址不得包含用户信息、query、fragment，路径必须
以 `/api/v1/agent-history-mcp` 结束。

新建会话先冻结能力及来源授权并提交，然后把固定 MCP 配置和 Secret Registry 凭据放入
OpenHands 正式创建字段。既有会话首次引用在 idle／paused 边界使用正式 `secrets` 与
`load_plugin`；先通过目标 Runtime 的正式 `/api/mcp/test` 验证连接与工具目录，再读取
正式 Conversation state 确认配置。平台仅记录配置确认，不伪造原生工具执行成功状态。
加载响应不确定时按正式配置对账，禁止无条件重复加载同名工具。原生加载意图和确认与
能力 version／digest、blob hash、独立 manifest 一并冻结；能力包和 manifest 均无明文凭据。

已加载会话新增引用只续租和扩展白名单，不重新加载工具；运行中首次安装明确拒绝，
用户可等当前回复完成后重试。租约在每次引用发送前续期至一小时，不由 MCP 查询自续期；
超过一小时的执行、独立 resume 和原生 fork 的继承能力仍须遵守原凭据的到期、撤销及
正式身份边界，不自动为新目标重绑凭据。配置冲突要求使用新会话，不能覆盖既有客户端。

整会话引用只保存 `kind=conversation`、标题、`source_binding_id`、原生来源会话 ID 与
查询工具名，不在发送前读完整历史。片段引用继续按正式事件 ID 读取所选正文。旧消息
中的内联引用正常投影；历史来源卡可点击进入原会话。能力失败保留完整草稿快照；异步
失败只恢复原 binding，用户已继续编辑时保留为需手动重试的队列项，避免覆盖新输入或
自动重发。

## 单来源授权（兼容接口）

已登录且有 Agent 会话权限的用户通过下列公开 API 显式授权：

`POST /api/v1/agent-workspaces/{workspace_id}/conversations/{target_binding_id}/history-grants`

请求包含 `source_binding_id` 与可选 `ttl_seconds`，时长默认 900 秒，范围 60–3600 秒。
两端必须是同用户、同 Agent Workspace 的 ACTIVE binding。响应 201 包含 `grant_id`、
`access_token`、`token_type=Bearer`、`expires_at`、`source_binding_id`；响应禁止缓存。
令牌只在签发响应中返回一次，数据库保存 SHA-256 哈希，不能从平台再次取回明文。

撤销使用同一路径追加 `/{grant_id}` 的 DELETE。删除来源或目标 binding 也清理授权。
授权独立于登录 Cookie；令牌不能调用普通平台 API，平台 Cookie 不能代替 MCP Bearer。
每次查询重新检查用户启用状态和当前 Agent 权限，不继承历史管理员权限。

## 会话级租约与来源白名单

为避免续期时重建已初始化的 MCP 客户端，新增稳定凭据 `fwh2`，仅授权一个目标会话。
平台使用既有 `credentials_master_key` 加密保存令牌，同时保存 SHA-256 供鉴权；
令牌不写入能力包、消息或普通配置。新凭据只能调用历史 MCP，不能登录平台或访问
其他 API。Runtime 接入时仍须经正式 Secret Registry 注入，使用原生 Cipher 加密持久化。

已登录且保有当前 Agent 权限的用户调用：

`POST /api/v1/agent-workspaces/{workspace_id}/conversations/{target_binding_id}/history-session`

请求为 `{"source_binding_ids":["来源binding UUID"],"ttl_seconds":900}`，每次显式提供
1–10 个不同来源，时长范围 60–3600 秒。两端仍须是同用户、同工作区的 ACTIVE 普通
会话；冻结目标及每个来源的正式 Conversation／Runtime Session 身份。响应 200 包含
`session_id`、`access_token`、`token_type=Bearer`、`expires_at` 和本次来源 ID；禁止缓存。
后续续租返回同一令牌，只延长目标租约及本次显式列出的来源租约，不能通过列出一个
来源自动恢复其他已过期来源。服务对目标 binding 加行锁，串行化签发、续期与撤销，
数据库唯一约束保证每个用户／目标至多一条会话租约、每个租约／来源至多一条来源授权。

MCP 初始化及列工具仅验证目标凭据；每次工具查询必须另带 `source_binding_id`，且该
来源在有效白名单中。查询不会续期，也不能新增来源；读取后再次检查目标租约、来源
租约、撤销、用户权限和正式身份，读取期间发生的撤销不会返回历史正文。

撤销目标凭据使用同一路径的 DELETE；只撤销一个来源使用
`DELETE .../history-session/sources/{source_binding_id}`。撤销为永久拒绝，续租不能自动
复活；当前没有凭据旋转／重新授权接口，撤销后如需重新建立授权须使用新的目标会话。
正常过期允许原登录用户显式续租。删除来源只清理它的授权，删除目标清理整条租约与
所有来源。凭据或来源身份漂移时拒绝查询／续租，不悄悄改绑到新会话。

该接口也供消息发送链路在受信任的服务端授权上下文内使用；MCP 本身不能调用续租。
新增迁移 `0160_agent_history_sessions` 为两表
启用用户隔离与强制 RLS；真实 PostgreSQL 行锁／RLS 验收留在 FR-664。

## MCP

部署前缀之后的端点为 `/api/v1/agent-history-mcp`，认证仅使用 Authorization Bearer，
不得把令牌放进 URL、消息、日志、普通配置文件或能力包。服务采用 stateless Streamable
HTTP：POST 的 JSON-RPC 初始化、ping、tools/list、tools/call 返回 JSON，初始化通知
返回 202；不建立服务器会话，不开放写工具、通用 API 代理或其他会话枚举能力。

唯一工具 `read_referenced_conversation` 的参数：

| 参数 | 含义 |
| --- | --- |
| `history_cursor` | 上一页返回的正式历史游标；首次省略 |
| `source_binding_id` | 会话级凭据必填，仅可选择白名单来源；旧单来源凭据必须省略 |
| `query` | 可选的页内关键词，不是全会话全文搜索 |
| `max_content_chars` | 单条正文预览上限，默认 2000，范围 1–10000 |
| `event_id` | 可选的正式消息事件 ID，启用单条正文分段读取 |
| `content_offset` | 可见正文中的 Unicode 码点位置，默认 0；非零时必须提供 event_id |

来源会话由授权记录确定；会话级凭据只能在其白名单中选择来源。分页输出包括 `source_binding_id`、
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

FR-660 在 `.tmp/` 中从固定 commit 和 frozen lock 建立隔离 SDK `1.54.0` 环境；
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
新凭据。重复加载同名插件也可能因不同 MCPClient 的重名工具冲突失败。因此 FR-661
采用服务端续租与来源白名单，复用同一受限令牌；接入时不能反复加载插件来模拟续期。

FR-661 使用相同固定 SDK 探针，以真实 SQLite 租约鉴权和受控历史读取通过本地 TCP
查询两个显式来源；中间执行原生加密保存／恢复，原令牌与原生工具保持可用。该证据
不验证 PostgreSQL 行锁或完整 Runtime 镜像，不执行 LLM。

FR-662 完成受治理 MCP version／digest／Manifest、续租触发、新建和既有
会话接入。不得将受限令牌
直接嵌入 Plugin 文件或用户消息，也不得在能力不可用时悄悄丢掉来源上下文。

数据库新增迁移 `0159_agent_history_grants`、`0160_agent_history_sessions` 和
`0161_agent_history_capability`，授权表启用强制用户 RLS，0161 仅追加能力 provenance／确认字段。
现有验证包含 SQLite 授权持久化与 PostgreSQL 离线迁移 SQL；真实 PostgreSQL upgrade／downgrade、固定
Runtime 验收及部署仍需后续执行。
