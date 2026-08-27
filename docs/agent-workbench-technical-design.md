# 独立 Agent 工作台技术设计

> 状态：`FR-18 FROZEN`
> 日期：2026-08-25
> 产品入口：一级导航 `Agent 会话`
> OpenHands 事实基线：`software-agent-sdk`
> `f09e03eac772290feeb51b7d7390ffaefeca1a09`（四包 `1.42.0`）
> 关联但不从属：`docs/flowrun-openhands-runtime-design.md`

本文冻结 FlowWeave 独立 Agent 工作台的产品、领域、持久化、Runtime、OpenHands、API、Web、故障恢复和
验收边界。本文不是 FlowRun 会话页面改版方案，也不表示数据库、API、Runtime 或 Web 已经实现。

## 1. 背景与目标

当前会话入口依赖 `FlowRun -> NodeRun -> NodeAttempt -> Conversation`，创建请求需要冻结 Snapshot、节点
执行器和 Environment Version。该链路适合流程运行，但不适合验证最基础的 Agent 能力，也导致会话问题
被流程状态、节点门禁和 Runtime 绑定问题放大。

本方案先建立一个类似 Codex 的独立 Agent 工作台：

1. 在平台最外层导航新增一级 `Agent 会话`，与节点、能力、环境、流程运行等模块平级。
2. 用户进入后直接创建、切换、重命名和删除会话，不需要 Flow、FlowRun、节点或 Attempt。
3. 平台启动时主动启动一个默认 Agent Runtime；浏览器进入页面不触发创建或启动容器。
4. 一个稳定 Agent Workspace 对应一个外置工作区、一个逻辑 Runtime Session 和一个当前可写 generation。
5. 多个 OpenHands Conversation 共用该 Workspace 和 Runtime，但各自保留正式 conversation ID 和事件树。
6. 物理容器只负责执行。会话 locator、工作区文件、OpenHands 状态、Secret 和审计均位于容器外。
7. 容器异常退出、被删除、镜像升级或平台重新部署后，必须挂载原数据并用原 conversation ID 恢复。
8. 首个闭环必须证明真实模型调用、Agent Tool、工作区、终端、刷新恢复和容器替换，而不是仅完成页面演示。

### 1.1 非目标

本阶段不做：

- 从 FlowRun、节点或 Attempt 启动工作台会话；
- 把默认 Agent Runtime 作为 FlowRun 的 Environment 或 fallback；
- 流程编排、Artifact lineage、节点输入输出和审批编排；
- 平台自建 Agent loop、Tool 执行器、消息状态机或 OpenHands 私有协议；
- 为了离线展示而复制 OpenHands 全量消息和事件到平台数据库；
- 多租户 Runtime 池、按会话独立容器、自动休眠或弹性缩容；
- 修改 OpenHands 源码。

## 2. 冻结决策

### 2.1 产品与领域

- `AgentWorkspace` 是一等领域对象，不是隐藏 FlowRun、特殊节点或伪 Attempt。
- 当前单机/单部署产品阶段只有一个 `scope_key = platform-default` 的默认 Workspace。
- 默认 Workspace 在平台启动时创建并保持 `desired_state = RUNNING`。
- `AgentConversationBinding` 只负责授权定位和可离线读取的展示元数据；OpenHands 仍是会话消息、事件树、
  HEAD、cursor、执行状态和 Tool Action/Observation 的唯一事实源。
- 顶层页面和公开 API 不出现 FlowRun、Environment Version、generation、Runtime Session ID 或物理容器 ID。
- 当前若未来引入多租户或用户隔离，必须按安全 scope 各建默认 Workspace/Runtime；禁止让互不信任的用户
  继续共享 `platform-default` 的工作区和会话。

### 2.2 Runtime

- 默认容器由 Worker 启动恢复流程主动预置，不由任何 GET 页面或“新建会话”请求懒启动。
- 默认容器使用平台配置的 OpenHands Runtime 镜像，但启动前必须解析并冻结内容 digest。
- Agent Workspace Runtime 是专属 owner `AGENT_WORKSPACE`；不得复用 `FLOW_RUN` 标签或数据库外键伪装。
- 一个 Workspace 任意时刻最多一个 generation 可以写 OpenHands Conversation 状态。
- Runtime 是永久期望运行资源，不应用临时 Sandbox 的 idle/hard TTL。
- 物理容器删除只触发 replacement；不删除 Workspace、OpenHands 状态、稳定 Secret 或 Conversation binding。

### 2.3 持久化

- PostgreSQL 保存 Workspace、Runtime 控制状态、Conversation locator、展示元数据、幂等命令和审计。
- 工作区和 OpenHands 文件状态保存于独立的外置持久根目录，作为 bind mount 挂入每个 generation。
- `OH_SECRET_KEY` 使用稳定加密 Secret Reference；replacement 取回同一 Secret 版本。
- 终端进程、Agent 进程、内存缓存和容器可写层可以丢失；会话、事件、文件和密钥不能只存在于其中。
- 浏览器 `localStorage` 只可保存布局偏好，不保存会话事实或恢复所需身份。

## 3. 术语与身份

| 对象 | 生命周期 | 稳定身份 | 事实所有者 |
|---|---|---|---|
| Agent Workspace | 平台级长期存在 | `agent_workspace_id` | FlowWeave |
| Runtime Session | Workspace 生命周期内稳定 | `runtime_session_id` | FlowWeave |
| Runtime generation | 单次物理 Agent Server 实例 | 单调 `generation` | FlowWeave Runtime Provider |
| Managed Runtime | 可删除的容器/provider 记录 | provider resource ID | Runtime Provider |
| Conversation binding | 长期授权 locator | `binding_id` | FlowWeave |
| OpenHands Conversation | 会话与事件树 | `openhands_conversation_id` | OpenHands |
| Workspace files | 会话共享项目文件 | 外置 allocation | Workspace 资源，OpenHands 读写 |

以下身份不得互相替代：

- 容器 ID、IP、端口和 API key 不是 Runtime Session 身份；
- Runtime generation 不是 Workspace 或 Conversation 身份；
- 前端 `binding_id` 不是 OpenHands event cursor；
- 浏览器当前选中项不是服务端恢复依据。

## 4. 目标拓扑

~~~text
浏览器 /agent/conversations/:bindingId
  |
  | 只访问 FlowWeave 授权 API、SSE/WS
  v
FlowWeave API -------------------------- PostgreSQL
  |                                      |- agent_workspaces
  | workspace_id + binding locator       |- agent_workspace_runtimes
  |                                      |- agent_conversation_bindings
  |                                      `- command / audit
  v
当前 active Agent Workspace generation
OpenHands Agent Server 容器（可替换）
  |- Conversation A
  |- Conversation B
  `- LocalWorkspace /runtime/workspace/project
        |
        v
外置持久根目录 .agent-workspaces/platform-default/
  |- workspace/project/
  |- state/conversations/
  |- state/bash-events/
  |- state/persistence/
  `- capabilities/（后续只读能力物化）

Worker + Runtime Provider
  |- 平台启动时确保默认 Workspace 和 generation 已存在
  |- 持续探活、fence、替换、恢复
  `- 不读取或解释 Agent 消息和 Tool 事件
~~~

API 和 Worker 通过受保护的 Runtime 网络连接当前 active generation。浏览器永远不能获得容器 endpoint 或
Runtime API key；Runtime Provider 持有 Docker 权限但不接入 Agent 数据面。

## 5. 平台启动与保活

### 5.1 启动顺序

1. PostgreSQL 和 Runtime Provider 就绪。
2. Migration 只创建结构，不执行 Docker 副作用。
3. Worker `recover_startup()` 获取数据库 advisory lock，幂等执行
   `ensure_default_agent_workspace()`。
4. 若默认 Workspace 不存在，创建 Workspace、外置 allocation、稳定 Secret Reference 和逻辑 Runtime
   Session，并投递唯一 `PROVISION_AGENT_WORKSPACE_RUNTIME` 任务。
5. 若 Workspace 已存在，校验 allocation、Secret、镜像 digest 和 Runtime Session，不重新创建数据。
6. Worker 创建或恢复 generation N，Runtime Provider 探活并完成 OpenHands contract probe 后 CAS 激活。
7. 后台 reconcile 持续确保 `desired_state = RUNNING`；容器缺失或不健康时启动 replacement。

浏览器访问不是以上任何步骤的触发条件。API 的控制面 readiness 也不依赖 Agent Runtime：Runtime 故障时
用户仍可进入页面、读取会话列表和诊断状态，只有创建会话、发消息和终端写操作暂时不可用。

### 5.2 并发与幂等

- `scope_key = platform-default` 使用唯一约束，多个 Worker 同时启动只能得到同一 Workspace。
- provision task 使用 `(workspace_id, target_generation)` 唯一键。
- 失败重试复用同一 generation/provider 记录，不因每次 reconcile 无限创建容器。
- Runtime 已 READY 时再次执行 bootstrap 是无副作用操作。
- 浏览器请求不得调用 `ensure_*runtime`；公开 GET/POST 只读取 active connection 或返回可行动错误。

### 5.3 镜像升级

配置项使用 `AGENT_WORKSPACE_RUNTIME_IMAGE`，开发默认可指向 `flowweave-openhands-runtime:1`。启动边界必须
通过 Runtime Provider 解析为 `sha256:<64hex>` 并记录 provenance，运行时只使用 digest。

已有 Workspace 的目标 digest 与配置解析结果不同时，创建明确的 `RUNTIME_IMAGE_UPGRADE` replacement，
记录旧/新 digest 和操作者/发布版本；不能直接改写 active generation。升级失败时保留原外置数据并进入
`DEGRADED`，不得用空 Workspace 或新 Conversation 冒充成功。

该镜像是 Agent 工作台的平台运行镜像，不是用户 Environment Version，也不能成为 FlowRun fallback。

## 6. 数据持久化边界

| 数据 | 保存位置 | 容器删除后 | 说明 |
|---|---|---|---|
| Workspace 名称、默认配置 | PostgreSQL | 保留 | FlowWeave 事实 |
| Conversation locator、展示标题 | PostgreSQL | 保留 | 标题是离线展示投影，不是消息副本 |
| Conversation 消息、事件树、HEAD | 外置 `state/conversations` | 保留 | OpenHands 唯一事实 |
| Bash command/event 文件 | 外置 `state/bash-events` | 保留 | OpenHands 唯一事实 |
| OpenHands profiles/settings store | 外置 `state/persistence` | 保留 | 使用稳定 Secret |
| 项目文件 | 外置 `workspace/project` | 保留 | 多会话共享 |
| Secret 明文 | 不持久化 | 不适用 | 只在调用边界短暂解密注入 |
| Secret Reference/密文 | PostgreSQL | 保留 | 不返回前端 |
| Runtime/generation 审计 | PostgreSQL | 保留 | 物理实例可删除 |
| 当前 Agent/终端进程 | 容器内存 | 中断 | 恢复后可继续发起操作 |
| 临时缓存、日志缓冲 | 容器可写层 | 可丢失 | 不作为恢复依据 |

Runtime 不可用时，平台仍能从 PostgreSQL 展示会话列表、标题和时间。消息正文在 replacement 重新加载
OpenHands 状态后恢复读取；FlowWeave 不直接解析 OpenHands 内部状态文件，也不维护第二份事件数据库。

## 7. 外置目录契约

服务端推导目录，调用方不能传入绝对路径。默认本地布局：

~~~text
<FLOWWEAVE_RUNTIME_HOST_WORKSPACE_ROOT>/.agent-workspaces/platform-default/
  workspace/project/
  state/conversations/
  state/bash-events/
  state/persistence/
  capabilities/
~~~

容器内固定映射：

| 外置目录 | 容器路径 | 模式 |
|---|---|---|
| `workspace/project` | `/runtime/workspace/project` | 可写 |
| `state/conversations` | `/runtime/state/conversations` | 可写 |
| `state/bash-events` | `/runtime/state/bash-events` | 可写 |
| `state/persistence` | `/runtime/state/persistence` | 可写 |
| `capabilities/<digest>` | `/runtime/capabilities/<digest>` | 只读 |

Runtime 环境变量固定为：

~~~text
OH_WORKSPACE_PATH=/runtime/workspace/project
OH_CONVERSATIONS_PATH=/runtime/state/conversations
OH_BASH_EVENTS_DIR=/runtime/state/bash-events
OH_PERSISTENCE_DIR=/runtime/state/persistence
OH_SECRET_KEY=<调用边界注入的稳定 Secret>
~~~

`/runtime/workspace/project` 是面向用户和 Agent 的逻辑项目根目录，而不是要求把所有文件平铺在该
目录。Agent 可以按需求或功能创建任意子目录；所有需要保留的代码、配置、文档和用户产物必须在该根目录
或其子目录内。平台不向用户暴露宿主机路径、Docker mount 或容器生命周期细节。Agent 工作台终端的初始
工作目录也固定为这个项目根；用户之后主动 `cd` 到项目内其他目录的行为保持不受限制。

创建容器前必须检查真实目录、owner、权限、符号链接、相对根前缀和宿主机/Provider 视图配对。不得把上述
路径放入 tmpfs，也不得用容器 inspect 猜测宿主机路径。

## 8. 数据模型

为避免把新产品伪装为 FlowRun，Agent Workspace 使用独立一等表。Runtime replacement 算法可以抽取共享
应用服务，但数据库 owner 和外键不能指向 `flow_runs`。

### 8.1 `agent_workspaces`

~~~text
id UUID PK
scope_key VARCHAR UNIQUE NOT NULL            -- 当前固定 platform-default
display_name VARCHAR NOT NULL                 -- 默认“Agent 工作区”
desired_state RUNNING | MAINTENANCE
default_model_provider_id FK NULL             -- 必须引用已测试可用配置
default_tool_policy_version_id FK NULL
created_at / updated_at
~~~

默认 Runtime 不依赖模型配置即可启动。没有默认模型时，工作台可查看历史，但创建首个 Conversation 前必须
让用户选择一个已测试成功的模型配置并显式设为工作台默认，不能猜测“最近一个”或隐藏 fallback。模型
选择器必须在工作台常驻可见，允许用户从“大模型配置”中所有已测试成功且有启用默认模型的供应商切换；
切换只影响后续新建 Conversation，既有 Conversation 保持其创建时由 OpenHands 冻结的模型配置。

### 8.2 `agent_workspace_runtime_secret_references`

~~~text
id UUID PK
workspace_id FK UNIQUE RESTRICT
encrypted_secret_key BYTEA
secret_digest CHAR(64) UNIQUE
created_at / rotated_at
~~~

Secret 轮换必须先证明 OpenHands 旧加密状态可迁移；普通 Runtime replacement 不轮换。

### 8.3 `agent_workspace_runtime_allocations`

~~~text
id UUID PK
workspace_id FK UNIQUE RESTRICT
secret_reference_id FK UNIQUE RESTRICT
relative_root VARCHAR UNIQUE                  -- .agent-workspaces/platform-default
created_at
~~~

### 8.4 `agent_workspace_runtimes`

~~~text
id UUID PK                                    -- stable runtime_session_id
workspace_id FK UNIQUE RESTRICT
runtime_image_digest VARCHAR NOT NULL
workspace_allocation_id FK UNIQUE RESTRICT
active_generation INT NULL
replacement_generation INT NULL
replacement_lease_token / owner / until
replacement_started_at / not_before
replacement_error_code / redacted_summary
status STARTING | ACTIVE | REPLACING | RECONNECTING | DEGRADED | MAINTENANCE
row_version INT
created_at / updated_at
~~~

Runtime Session 不因容器替换而变化。当前默认 Workspace 不提供产品级“停止”或“删除 Runtime”。

### 8.5 `agent_workspace_runtime_generations`

~~~text
id UUID PK
runtime_session_id FK
generation INT                                  -- session 内单调递增
managed_runtime_id FK NULL
instance_id VARCHAR NULL
runtime_image_digest VARCHAR NOT NULL
state PROVISIONING | READY | DRAINING | STOPPED | DELETED | FAILED
fence_token UUID UNIQUE
row_version INT
started_at / ready_at / draining_at / stopped_at / deleted_at
failure_code / redacted_summary
UNIQUE(runtime_session_id, generation)
~~~

### 8.6 `agent_conversation_bindings`

~~~text
id UUID PK                                      -- 前端和公开 API 使用
workspace_id FK
runtime_session_id FK
work_directory_version_id FK NULL              -- 新会话冻结选定工作目录版本；根工作区为 NULL
working_directory VARCHAR NULL                 -- 新会话冻结的 OpenHands LocalWorkspace working_dir
model_provider_id FK NULL                     -- 创建时冻结；历史未知时保持 NULL
model_name VARCHAR NULL                       -- 用户确认后立即保存的会话期望模型
reasoning_effort VARCHAR NULL                  -- 用户确认后立即保存的会话推理强度
openhands_conversation_id UUID
display_title VARCHAR NULL                      -- 离线列表投影
title_state PENDING | GENERATED | MANUAL | FALLBACK
title_generation INT >= 1                       -- 手动改名递增，用作自动标题 CAS 栅栏
lifecycle PROVISIONING | ACTIVE | DELETE_PENDING | DELETED | FAILED
create_idempotency_key VARCHAR UNIQUE
initial_user_event_id VARCHAR NULL             -- 首条正式 user MessageEvent 的 OpenHands ID
bootstrap_parent_event_id VARCHAR NULL          -- 投递前 HEAD；仅用于按正式 parent_id 重试对账
created_at / updated_at / last_connected_at / deleted_at
UNIQUE(runtime_session_id, openhands_conversation_id)
~~~

`lifecycle` 只描述 locator 创建/删除是否完成，不复制 OpenHands `execution_status`。该表禁止加入 message、
role、event payload、HEAD、cursor、Tool 状态或物理 endpoint。

### 8.7 `agent_conversation_commands`

只记录跨 PostgreSQL/OpenHands 边界的幂等控制命令：

~~~text
id / workspace_id / binding_id
command_type CREATE | DELETE | RENAME
idempotency_key UNIQUE
state PENDING | SUCCEEDED | AMBIGUOUS | FAILED
attempt_count / last_error_code / redacted_summary
created_at / updated_at
~~~

用户消息不写入该表。OpenHands `SendMessageRequest` 没有调用方 event ID 或原生幂等键；消息请求超时后平台
不能安全自动重发，否则可能产生重复用户消息。此时标为传输结果不确定，先重新读取事件，让用户决定是否
重试。

## 9. Runtime Provider 改造边界

Runtime Provider 必须显式支持：

- `owner_type = AGENT_WORKSPACE`；
- `kind = AGENT_RUNTIME`；
- Agent Workspace allocation 与稳定 Secret 注入；
- 确定性资源名和标签，例如 `fw-sbx-agent-<workspace-short-id>-g<generation>`；
- 与 FlowRun 相同等级的持久目录校验、网络隔离、资源配额和 generation fence；
- 永久期望运行策略，不应用临时 owner TTL；
- 删除物理 generation 与删除持久 allocation 的不同权限边界。

Provider 请求模型不能继续使用“只有 `FLOW_RUN` 才允许 allocation/secret”的等价判断，应改为显式持久
owner 集合 `{FLOW_RUN, AGENT_WORKSPACE}`，同时分别校验 owner 对应的 allocation 类型。不得简单放开为任意
字符串 owner。

Runtime 容器标签至少包含：

~~~text
flowweave.kind=agent-runtime
flowweave.owner-type=AGENT_WORKSPACE
flowweave.owner-id=<workspace_id>
flowweave.runtime-session-id=<runtime_session_id>
flowweave.generation=<n>
flowweave.manager-scope=<scope>
flowweave.runtime-client-role=agent-workspace
~~~

API/Worker 只通过标签授权的专属网络连接 Runtime；Provider 本身不加入数据面网络。

## 10. Runtime 状态机与恢复

### 10.1 正常状态

~~~text
STARTING -> ACTIVE
ACTIVE -> REPLACING -> ACTIVE
ACTIVE -> RECONNECTING -> ACTIVE
任意可恢复状态 -> DEGRADED -> RECONNECTING
~~~

产品只映射为：

| 内部状态 | 用户状态 | 可读历史列表 | 新建/发送/终端 |
|---|---|---|---|
| ACTIVE | Agent 已就绪 | 是 | 是 |
| STARTING/RECONNECTING/REPLACING | 正在恢复运行环境 | 是 | 否 |
| DEGRADED | 运行环境暂不可用，数据未丢失 | 是 | 否 |
| MAINTENANCE | 工作台维护中 | 是 | 否 |

generation、Session ID 和容器错误堆栈只进入运维日志，不在普通页面展示。

### 10.2 优雅替换

1. 持久 replacement lease 和目标 N+1，冻结新写入。
2. 使用相同外置 allocation、稳定 Secret、固定镜像 digest和新临时连接凭据启动 N+1。
3. N+1 只做 Server health/contract 预热，不加载会话写入。
4. 调用 N 的 OpenHands 正式 `POST /api/conversations/prepare-for-sandbox-pause`。
5. 停止 N，并确认 Conversation lease 已释放。
6. N+1 使用至少一个已有 conversation ID reload，校验 workspace、persistence 和正式 event identity。
7. CAS 激活 N+1，恢复 API/WS 路由，再删除 N 的物理容器。

### 10.3 异常退出或人工删除容器

- 立即 fence 旧 generation，记录中断，不删除外置目录。
- N+1 可以预热，但在旧 OpenHands 默认 45 秒 lease 释放/过期前不能取得写权限。
- reload 失败时保持 `DEGRADED`，不创建空 Conversation，不重置工作区。
- 旧容器重新出现时 generation fence 继续拒绝路由并由 Provider 清理。
- 运行中的 Agent/终端进程会中断；已持久化事件和文件保留。恢复后用户可以继续发送或显式重试。

### 10.4 数据删除

- “删除 Conversation”是明确的永久操作：先建立 `DELETE` 命令，再调用 OpenHands 正式 DELETE，最后形成
  binding tombstone。失败可重试，不用删除 binding 冒充成功。
- 普通用户页面不提供“重置 Workspace”或“重置 Runtime”。
- 删除物理容器不授权删除 allocation。
- 默认 Workspace 的持久数据只允许通过单独的管理员销毁流程删除，必须展示目标路径、Conversation 数量、
  备份状态并二次确认；这不属于首个产品闭环。

## 11. OpenHands 1.42.0 正式契约

### 11.1 创建与读取

工作台只调用固定版本正式接口：

| 功能 | OpenHands API |
|---|---|
| 创建 | `POST /api/conversations` |
| 读取 | `GET /api/conversations/{conversation_id}` |
| 列表/恢复审计 | `GET /api/conversations/search` |
| 事件分页 | `GET /api/conversations/{id}/events/search` |
| 单事件身份探针 | `GET /api/conversations/{id}/events/{event_id}` |
| 发送并运行 | `POST /api/conversations/{id}/events`，`role=user, run=true` |
| 立即停止 | `POST /api/conversations/{id}/interrupt` |
| 继续 | `POST /api/conversations/{id}/run` |
| 重命名 | `PATCH /api/conversations/{id}` |
| 删除 | `DELETE /api/conversations/{id}` |
| 替换前释放 | `POST /api/conversations/prepare-for-sandbox-pause` |

创建请求必须：

- 由平台预分配 canonical UUID，作为幂等 create identity；
- 使用 `LocalWorkspace(working_dir=/runtime/workspace/project)`；
- `worktree=false`，避免会话文件进入容器 `/tmp`；
- 使用已测试成功的模型配置编译正式 OpenHands `agent`/`agent_settings`；
- 只通过正式字段加载后续 Tool Policy、Skill、Plugin、MCP 等能力。

### 11.2 `persistence_dir` 校验规则

固定源码的 `ConversationState` 会调用：

~~~text
BaseConversation.get_persistence_dir(persistence_base_dir, conversation_id)
= <persistence_base_dir>/<conversation_id.hex>
~~~

因此 `GET /api/conversations/{id}` 正式返回的 `persistence_dir` 是会话专属目录，例如：

~~~text
/runtime/state/conversations/8659c2040fcb479f96a79b0097671ac8
~~~

不是基础目录 `/runtime/state/conversations`。身份校验必须使用规范化路径并严格等于：

~~~text
PurePosixPath("/runtime/state/conversations") / UUID(conversation_id).hex
~~~

同时要求 `workspace.kind = LocalWorkspace` 且 `working_dir` 精确为
`/runtime/workspace/project`。不得因为错误比较基础目录而把已经成功持久化的会话判成 identity drift。

这也是当前截图中“创建成功后首次拉取事件立即 409”的已确认根因：现有适配器把正式返回的会话专属
目录错误地与基础目录做字符串相等比较。后续实现必须先修正该契约并加入固定源码/真实镜像回归测试。

### 11.3 事件与 cursor

- 事件只按 OpenHands 正式 `id`、`parent_id`、`action_id`、`tool_call_id` 和分页 token 关联。
- FlowWeave 不按时间顺序、事件名称或文本猜测 Action/Observation 关系。
- Web 首次进入以 REST 分页加载历史，再连接 WS；断线后用最后已见 event ID 做缺口检查并重新分页。
- cursor 只存在于客户端连接期，不持久化为平台第二真相。
- WS 正常时不再每 2.5 秒轮询 events。

## 12. 模型与能力配置

### 12.1 默认模型

Agent Runtime 启动不依赖模型配置。创建 Conversation 时必须满足：

1. Workspace 已显式配置 `default_model_provider_id`；
2. 模型配置为启用状态且最近一次连接测试成功；
3. Secret 在服务端调用边界解密，不进入数据库普通列、日志、前端或 Conversation display metadata；
4. 后端编译为 OpenHands 正式 LLM/Agent 请求。

创建成功后，binding 必须记录本次实际使用的 `model_provider_id + model_name + reasoning_effort`。用户在
当前会话选择另一个已测试供应商、模型或推理强度时，该选择即代表确认：平台必须先通过 OpenHands 正式
`switch_llm` 应用到空闲会话，再原子更新 binding；选择失败则回滚页面显示并保留原绑定，不等待下一条消息
才保存。固定 OpenHands `1.42.0` 的 `switch_llm` 只替换当前 Event Service 的活跃 LLM，不持久化替换值，
Runtime reload 后正式 `agent.llm` 可能恢复为创建会话时的供应商。因此每次发送前必须重新应用 binding 中
完整的供应商、模型和推理强度，失败则阻止 user event 并返回 `AGENT_MODEL_REBIND_FAILED`，不得静默使用
旧配置。迁移前没有可审计选择的历史 binding 保持 `NULL`，不得依据 Workspace 默认值猜测回填；用户首次
明确选择后建立持久绑定，由原生 fork 创建的会话继承源 binding 的当前完整模型选择。

FlowWeave 通过 OpenHands 正式 LLM 字段把 Agent 工作台请求限制为 2 次尝试、2–4 秒指数退避和 60 秒
单次超时。供应商额度耗尽或网关不可达时必须尽快形成正式 `ConversationErrorEvent`，避免 OpenHands SDK
默认 5 次尝试、8–64 秒退避和 300 秒单次超时令简单问题长时间停留在无过程事件的运行态。

没有默认模型时，页面保留导航和历史列表，在空白区提示“先选择已测试成功的模型配置”，并链接现有模型
配置页面。禁止自动选最近创建的模型或回退到环境变量中的隐藏模型。

### 12.2 能力边界

首个闭环只要求 OpenHands 固定 Runtime 自带的正式基础 Agent/Tool 能力和终端可用。Tool Policy、Skill、
Plugin、MCP、Hook、Agent Definition 和 Memory 后续可以由 Workspace Profile 冻结版本并传入创建请求，
但不能依赖 Flow Snapshot，也不能由 FlowWeave 自建执行旁路。

同一 Conversation 创建后的 Agent 配置由 OpenHands 持久状态恢复。平台可以保存配置版本引用用于授权和
审计，但不得据此重建一份平台 Conversation 状态机。

## 13. FlowWeave API 设计

公开 API 使用 binding ID，不暴露物理 endpoint。`default` 只用于解析入口，解析后客户端持有稳定
Workspace ID。

### 13.1 Workspace 与 Runtime

~~~text
GET   /api/v1/agent-workspaces/default
GET   /api/v1/agent-workspaces/{workspace_id}
PATCH /api/v1/agent-workspaces/{workspace_id}/settings
GET   /api/v1/agent-workspaces/{workspace_id}/runtime
GET   /api/v1/agent-workspaces/{workspace_id}/runtime/stream
~~~

Runtime 响应只包含产品状态、`write_available`、恢复提示和最近更新时间，不返回 generation、Session ID、
容器名、endpoint、镜像 digest 或 Secret。

### 13.2 Conversation

~~~text
GET    /api/v1/agent-workspaces/{workspace_id}/conversations
POST   /api/v1/agent-workspaces/{workspace_id}/conversations
GET    /api/v1/agent-workspaces/{workspace_id}/conversations/{binding_id}
PATCH  /api/v1/agent-workspaces/{workspace_id}/conversations/{binding_id}
DELETE /api/v1/agent-workspaces/{workspace_id}/conversations/{binding_id}
~~~

创建和删除要求 `Idempotency-Key`。创建流程：

1. 事务内预分配 OpenHands UUID、写 `PROVISIONING` binding 和 `CREATE` command。
2. 解析 active fenced connection 和默认模型配置。
3. 使用同一 UUID 调用 OpenHands create；201 和同规范 200 都视为幂等成功。
4. 校验正式 ID、Workspace 和会话专属 persistence path。
5. 更新 binding 为 `ACTIVE` 并返回。
6. API/Worker 崩溃后由 recover job 使用原 UUID 继续，不创建第二个会话。

### 13.3 消息、事件和控制

~~~text
GET  /api/v1/agent-workspaces/{workspace_id}/conversations/{binding_id}/events
POST /api/v1/agent-workspaces/{workspace_id}/conversations/{binding_id}/model
POST /api/v1/agent-workspaces/{workspace_id}/conversations/{binding_id}/messages
POST /api/v1/agent-workspaces/{workspace_id}/conversations/{binding_id}/interrupt
POST /api/v1/agent-workspaces/{workspace_id}/conversations/{binding_id}/resume
WS   /api/v1/agent-workspaces/{workspace_id}/conversations/{binding_id}/stream
WS   /api/v1/agent-workspaces/{workspace_id}/terminal
~~~

消息 body 只使用产品字段 `content` 和可选附件，适配层转换为正式 `SendMessageRequest`。模型选择使用独立
会话配置命令，在用户选择时立即持久化，消息发送不是配置保存触发器。服务端每次写操作都重新从 Workspace
Runtime Session 解析当前 active generation、校验 fence，并在发送前重新应用 binding 的完整 LLM 配置；
binding 不保存物理 Runtime 或消息状态。

Terminal 属于共享 Workspace，不依附某个 Conversation。它连接同一 active generation，并固定 cwd 为
`/runtime/workspace/project`。容器替换会断开终端进程，但新连接继续看到相同文件。

### 13.4 错误契约

| 错误码 | HTTP | 前端动作 |
|---|---:|---|
| `AGENT_MODEL_CONFIGURATION_REQUIRED` | 409 | 展示模型配置入口 |
| `AGENT_RUNTIME_RECOVERING` | 503 | 保留页面，按 `Retry-After` 重连 |
| `AGENT_RUNTIME_DEGRADED` | 503 | 停止写入，展示恢复失败摘要 |
| `AGENT_CONVERSATION_PROVISIONING` | 409 | 展示创建中，不重复创建 |
| `AGENT_CONVERSATION_NOT_FOUND` | 404 | 返回会话列表 |
| `AGENT_CONVERSATION_IDENTITY_DRIFT` | 409 | 停止轮询，进入诊断状态 |
| `AGENT_MODEL_REBIND_FAILED` | 503 | 不发送消息，提示新建会话或稍后重试 |
| `AGENT_MESSAGE_DELIVERY_AMBIGUOUS` | 504 | 先刷新事件，不自动重发 |

错误响应必须保留 `code`、可理解中文 message、脱敏 details 和 request ID。前端遇到非瞬态 4xx 后停止自动
轮询；瞬态恢复采用 1、2、5、10、30 秒上限的指数退避。

## 14. Web 产品与状态设计

### 14.1 导航和 URL

- 顶层导航新增 `Agent 会话`，不再通过 FlowRun 页面进入独立工作台。
- 稳定路由为 `/agent` 和 `/agent/conversations/:bindingId`。
- 刷新时由 URL 和服务端 binding 恢复，不依赖 Zustand 中的 Run/Attempt/Conversation 临时 ID。
- 浏览器历史前进/后退可以切换会话。
- `localStorage` 只保存侧栏宽度、是否展开终端等 UI 偏好；损坏数据直接回默认布局，不提供重置页。

### 14.2 页面布局

~~~text
平台顶栏：Agent 会话（一级 active tab）
┌──────────────┬──────────────────────────────┬──────────────┐
│ 新建会话      │ 当前会话                     │ 文件/终端抽屉 │
│ 搜索          │ 消息与 Agent 活动             │ 默认收起      │
│ 历史会话列表  │                              │              │
│              │ 输入框 / 停止                 │              │
└──────────────┴──────────────────────────────┴──────────────┘
~~~

普通页面不展示：FlowRun、Node、Attempt、Environment Version、generation、Runtime Session ID、容器 ID、
“事实边界”说明或运维按钮。

### 14.3 页面状态

- Runtime ACTIVE 且无会话：显示“新建会话开始协作”。
- 无默认模型：显示模型选择/配置引导，不阻塞历史列表。
- 左侧会话栏底部持续展示“新会话模型配置”卡片；用户可切换或清空选择，不能把 Codex OAuth 或任何
  其他供应商隐式视为默认值。该卡片不属于当前会话，也不改变已创建 Conversation 的冻结模型。
- Runtime 恢复中：历史列表可读；消息区说明“数据已保留，运行环境恢复后加载消息”；输入和终端禁用。
- 当前 Conversation ACTIVE：REST 补齐历史后建立 WS，输入可用。
- WebSocket `delta` 作为当前轮工作过程中的临时模型输出逐段展示。OpenHands 的正式最终回复有两条路径：
  assistant `MessageEvent`，或 Agent 调用 finish tool 时的 `FinishAction.message`；任一路径到达后清除临时
  文本，并只在过程区下方渲染一次最终回复。`FinishObservation` 只确认工具执行，不生成第二份回复。浏览器
  临时文本不写入 FlowWeave 数据库或本地持久状态。
- `THOUGHT`、`TOOL_CALL`、`TOOL_RESULT` 与 `ERROR` 作为可折叠的工作过程显示。固定 OpenHands 1.42.0
  的 `ActionEvent.thought` 和 `ActionEvent.summary` 是事件顶层正式字段，不属于嵌套 `action` 参数；平台
  分别安全投影为 `payload.thought` 和 `payload.summary`，并让普通 Tool Action 的兼容 `payload.content`
  继续等于可见 thought。`FinishAction` 同一正式事件可同时携带顶层 thought 和 `action.message`，前端必须
  将前者展开为工作过程、后者展开为唯一最终回复。Observation 原始输出不默认展开。TaskTracker 按
  正式 `command=view/plan` 显示“查看任务列表”或“更新任务列表”，其他工具也显示可识别名称。空 `STATE`
  与未承载用户价值的协议帧不显示；`reasoning_content`、thinking blocks、Responses reasoning item 等隐藏推理
  不得进入安全投影。
- 工具 Action 与 Observation 必须使用 OpenHands 正式 `action_id`、`tool_call_id` 关联为同一条活动，不得以
  `parent_id`、相邻顺序、工具名称或文本猜测；`parent_id` 只负责事件树拓扑。折叠态标题直接说明动作、对象
  和状态，例如“已读取 工作区/src/a.ts”“已编辑 工作区/src/b.ts”“已运行 git status”。每条工具活动内部
  记录整行带箭头并可点击展开默认折叠的详情区，只展示平台安全投影后的原始操作信息：Terminal 命令、退出码和输出，File Editor
  命令、路径、行范围或变更片段，以及其他工具的脱敏输入和结果。不得把未经安全投影的原始事件、隐藏推理、
  Secret、Runtime 物理路径或内部观察文件位置透传到浏览器。
- REST 与实时安全投影保留 OpenHands 正式事件 `timestamp`。完成轮次按正式 user 到 assistant/error 的
  墙钟时间显示耗时，运行中可在正式时间回读前使用浏览器请求开始时间计时；缺失或非法时间不猜测。
- Agent 执行中：显示停止按钮和正式 Tool 活动；不把全部原始 JSON 默认展开。
- 暂停按钮只表示“已向 OpenHands 请求暂停”，此时 composer 进入 `pausing`。页面通过 OpenHands 当前正式
  execution status 的瞬态读取确认会话已暂停后显示“继续”；继续调用 OpenHands 正式 run。执行期间可把新
  输入放进浏览器内存队列，在当前轮完成后依次发送；平台不保存一份执行状态、消息或队列，也不在网络不确定
  窗口自动重发。服务端仍拒绝任何绕过该队列的并发 OpenHands send。
- 当前活动分支的最后一条用户消息始终可编辑。运行中提交编辑时，页面先自动暂停；暂停确认或会话已经
  停止后，服务端只接受正式 user event id，并以其正式 parent_id 调用 OpenHands navigate、再发送编辑内容
  运行。旧事件分支仍由 OpenHands 保存但不在当前活动会话中渲染，新的回复与工作过程取代旧分支；不得以
  隐藏前端消息伪造该行为。
- 对话视觉采用简洁问答：用户消息仅以右侧气泡呈现，Agent 最终回复直接渲染在正文流中；不显示头像、
  “你”或“Agent”身份标签。每轮固定按“用户消息 → 工作过程 → 最终回复或失败结果”排列；安全投影的
  流式文本、`THOUGHT`、`TOOL_CALL`、`TOOL_RESULT`、Condensation 与 `ERROR` 均进入工作过程。执行中
  过程区保持展开并实时计时，完成后默认折叠并显示耗时；没有中间条目的直接回复只显示耗时摘要，不生成
  空白详情区域。用户仍停留在最新位置时，长最终回复到达后将视口定位到该回复开头；“跳转到最新回复”
  控件不得位于尾部锚点之后或以自身尺寸参与“是否到达最新”的判断。
- 用户消息气泡旁提供快速复制，复制内容只取该正式 user event 的可见文本。浏览器原生复制若选区起始于
  用户消息但在动态会话流中跨入后续工作过程或回复，前端仅将该用户消息正文写入剪贴板；选区完全位于用户
  消息时保留用户实际选中的片段。此保护不拦截助手回复、代码块或工作过程的正常复制，也不改变 OpenHands
  事件或平台持久化。
- Composer 采用紧凑圆角样式，当前会话的供应商、模型和（仅在模型明确支持时的）推理强度归入同一轻量
  浮层。任一选择都立即调用会话模型配置 API，并在成功后持久化为该 Conversation 的当前选择；刷新直接
  从 binding 恢复，不依赖下一条消息或浏览器本地状态。会话执行中或等待确认时禁用选择；浮层在输入被禁用
  或用户点击其外部区域时自动收起，且外部点击的原页面动作继续执行。
- 顶层 Agent Conversation 的首个 LLM 即设置 OpenHands 正式 `stream=true`，使 Agent Server Event
  Service 在创建时绑定 token callback；后续原生 `switch_llm` 切换到 Codex OAuth 等强制流式供应商时
  复用该 callback，不会被 SDK 降级为非流式请求。FlowWeave 继续自行刷新并传入 OAuth 令牌，不把它伪装成
  OpenHands 本地凭据存储拥有的 subscription credential。
- binding 显式记录 Event Service 创建时是否已经具备上述流式 callback。迁移前的历史 binding 一律
  fail-closed，不能因当前 LLM 已显示 `stream=true` 就猜测 callback 存在。用户下一次发送时，平台先对源
  Conversation 调用正式 `switch_llm` 应用当前选择，再从正式 HEAD 调用原生 fork；新 Conversation 的
  Event Service 由该流式 LLM 重新创建并绑定 callback。Web 先切换到新 binding URL，再只向新 binding
  发送一次原消息；迁移失败不向源事件树追加 user event，也不按供应商错误文本触发迁移。
- Composer 的 Enter 快捷发送必须先排除浏览器正式 IME composition 状态及兼容性的 `keyCode=229`；中文
  输入法确认候选期间只完成文本输入，不发送消息，组合完成后的独立 Enter 才发送，Shift+Enter 保持换行。
- 上下文进度只读取当前 LLM `usage_id` 对应的 OpenHands 正式 stats bucket：`per_turn_token` 是当前轮
  View 用量，`context_window` 是供应商报告的窗口容量；不得混入 condenser 或子任务的 bucket。仅当两者
  都是正整数时显示环形图和紧凑 `k`/`m` 标签（如 `6.4k / 922k`、`256k`、`1m`）；缺失、非法或只有累计
  usage 时完全不显示上下文控件，不估算或伪造窗口。
- 非瞬态错误：停止请求风暴，显示错误码、request ID、重试或返回列表。
- 浏览器刷新：恢复同一 binding；binding 不存在时回 `/agent`，不进入全局异常页。

### 14.4 新建会话体验

点击“新建会话”只在浏览器内创建草稿，不弹出 Flow、节点、镜像或 Runtime 表单，也不请求创建 binding、
Conversation 或稳定 URL。草稿可绑定隐式根工作区，或一个工作目录；工作目录在首条消息发送时解析并冻结其当前
不可变版本。首条消息以浏览器生成的幂等键提交 bootstrap：平台先保留隐藏的命令记录，再以同一 UUID 创建
OpenHands Conversation、投递唯一正式 user event，拿到正式事件 ID 后才激活 binding、写入查询缓存并进入
稳定 URL。刷新或离开未发送草稿直接丢弃。

OpenHands 1.42.0 的发送接口没有客户端幂等键；网络结果不确定时，平台只按正式 user `MessageEvent` ID 及
`parent_id` 对账，绝不根据文本、事件顺序或名称猜测、更不会重复投递。明确失败会调用正式 delete 清理隐藏
空会话；无法确认的投递保持不可见，等待同一 bootstrap 键继续对账。首条正式 user event 接受后，平台立即以
首个非空行的规范化文本写入本地兜底标题并标记 `PENDING`，然后投递一次性标题元数据任务。该任务通过独立的
供应商请求生成短标题：API Key 供应商使用 OpenAI-compatible Chat Completions，Codex OAuth 使用其正式
Responses 端点；它不调用 OpenHands Runtime、不会写入 Conversation Event，也不会改变 HEAD 或上下文。
任务完成后立即清除其临时首条文本种子。用户双击标题改名会把本地状态设为 `MANUAL` 并递增
`title_generation`，延迟任务只可在 generation 与 `PENDING` 同时匹配时 CAS 写入，因此永不覆盖手动名称。
供应商不可用或响应无效时保留规范化首句并标记 `FALLBACK`；列表不得回退为“未命名会话 N”。

## 15. 一致性和恢复对账

平台必须定期做只读对账，不解析 OpenHands 内部 JSON 文件：

1. PostgreSQL ACTIVE binding 在 OpenHands GET 中存在，且 ID、Workspace、persistence path 一致。
2. `PROVISIONING` binding 使用原 UUID 重试 create；不得生成新 UUID。
3. OpenHands 存在但 binding 尚未完成的 create，可由 command 记录认领并完成绑定。
4. 没有可信 command/binding 的 OpenHands orphan 不自动暴露给用户，进入运维审计。
5. binding 已 DELETED 但 OpenHands 仍存在时重试正式 delete。
6. replacement 激活前至少探测一个已有会话及一个已有正式 event ID；Workspace 无会话时使用文件哨兵和
   Server contract probe，不能创建隐藏业务会话充当探针。

display title 可以从 OpenHands 定期同步，但 OpenHands 不可用时数据库投影继续服务列表。任何投影漂移都
不能改写消息、事件或 Conversation identity。

## 16. 安全边界

- 默认 Workspace 当前仅适用于明确的单部署可信用户边界；多租户上线前必须拆分 scope。
- API 对 Workspace、binding、stream 和 terminal 统一鉴权；不能仅依赖 UUID 不可猜。
- Runtime API key 每 generation 轮换，稳定 `OH_SECRET_KEY` 仅由 Worker/Provider 调用边界取回。
- Secret 不进入镜像、普通数据库列、日志、审计 payload、前端响应或 Agent display title。
- Runtime 继续 `cap_drop=ALL`、`no-new-privileges`、资源配额和受控 egress；Docker socket 只在 Provider。
- bind mount 创建前拒绝符号链接穿越、非预期 owner、错误权限和超出批准根目录的路径。
- 文件/终端 API 固定在 Workspace 根内，拒绝客户端绝对路径和 `..` 穿越。
- 公开诊断只返回脱敏错误分类；容器 endpoint、宿主机路径和 Secret digest 只进受限运维面。

## 17. 可观测性

至少提供以下指标：

~~~text
agent_workspace_runtime_ready (gauge)
agent_workspace_runtime_generation_total
agent_workspace_runtime_replacement_total{reason,result}
agent_workspace_runtime_recovery_seconds
agent_workspace_conversation_create_total{result}
agent_workspace_conversation_reload_total{result}
agent_workspace_message_delivery_total{result}
agent_workspace_stream_connections
agent_workspace_terminal_connections
~~~

结构化日志统一带 `workspace_id`、`runtime_session_id`、generation、binding ID、OpenHands conversation ID
和 request ID；对外错误不带 endpoint、API key、模型 Secret、完整 prompt 或 Tool 输出。

关键审计事件：默认 Workspace bootstrap、模型默认值变更、Conversation 创建/重命名/删除、消息发送 actor
和内容 digest/长度、interrupt、Runtime replacement、镜像升级、恢复失败和管理员数据销毁。审计不复制
消息正文和完整事件。

## 18. 备份、保留和发布

- PostgreSQL 与 `.agent-workspaces/platform-default` 必须作为同一恢复单元制定备份；仅备份其中之一不能
  保证 locator 与 OpenHands 状态一致。
- 本地开发使用宿主机 bind 目录；生产应使用支持快照的持久卷或等价文件存储。
- 备份点记录数据库时间、Workspace allocation identity、Runtime image digest 和 OpenHands source commit。
- 恢复后先以写入冻结模式做 binding/Conversation/event identity 对账，再开放 Agent 写入。
- Web/API/Worker/Runtime 镜像发布不得清理默认 Workspace 目录或 PostgreSQL binding。
- Schema/Runtime contract 不兼容时 fail closed 并保留旧数据，不能要求浏览器重置。

## 19. 故障矩阵

| 故障 | 必须保留 | 自动动作 | 用户结果 |
|---|---|---|---|
| 浏览器刷新 | URL/binding | REST 恢复 + WS 重连 | 回到原会话 |
| Web/API 重启 | 全部持久数据 | 无 Runtime 重建 | 会话继续可用 |
| Worker 重启 | 任务/lease/generation | 认领未完成任务 | 不产生第二容器 |
| Agent 容器 kill | 文件、会话、事件、Secret | N+1 replacement | 短暂恢复后继续 |
| Agent 容器被删除 | 同上 | reconcile 重建 | 不出现空工作台 |
| OpenHands 进程崩溃 | 同上 | 同 generation 重启或 replacement | 历史不丢失 |
| 网络分区 | 同上 | fence、退避、必要时 replacement | 停止写入，列表可读 |
| 旧 generation 复活 | 同上 | fence 并清理旧实例 | 无双写 |
| 会话 create 超时 | 预分配 UUID/command | 原 UUID 对账重试 | 不产生重复会话 |
| 消息 send 超时 | OpenHands 已有事件（若已接收） | 不自动重发，刷新事件 | 不产生重复消息 |
| 镜像升级失败 | 原 allocation/Secret | DEGRADED 或回到已知 generation | 不重置数据 |
| 主机持久卷丢失 | 无法由容器恢复 | 从一致备份恢复 | 明确数据故障，不造空会话 |

## 20. 首个闭环验收

以下全部通过才算“Agent 会话跑通”：

1. 全新部署后，在未打开浏览器前已经存在且只有一个
   `owner-type=AGENT_WORKSPACE` 的 READY Runtime 容器。
2. 顶层存在 `Agent 会话` Tab，点击直接进入 `/agent`，不要求任何 Flow/节点/Run 上下文。
3. 配置一个测试成功的模型为 Workspace 默认后，可直接新建 Conversation。
4. 发送真实问题，收到 OpenHands assistant `MessageEvent` 或 `FinishAction.message` 最终回复；执行
   Terminal/File Tool 并在工作区生成可验证文件。
5. 新建第二个 Conversation，两个原生 conversation ID 不同，但 Runtime 容器数仍为 1。
6. 终端 `pwd` 为 `/runtime/workspace/project`，可读取 Agent 创建的文件。
7. 浏览器刷新后恢复相同 binding、完整事件和文件，不依赖 localStorage 深层 ID。
8. 手工 `docker kill`/删除 Runtime 后，页面保留会话列表；N+1 使用原 conversation ID、原 event ID 和原
   文件恢复，未产生双 writer 或空会话。
9. 无缓存重新构建并部署 Web/API/Worker/Runtime 后，默认 Workspace 数据仍存在。
10. 创建后首次 GET 不再出现 `RUNTIME_PERSISTENCE_IDENTITY_DRIFT`；真实返回的会话专属 persistence path
    通过固定契约校验。
11. Runtime 故障期间没有 2–4 秒无限 409 请求；恢复使用状态流和有上限退避。
12. 页面不出现 FlowRun、Node、Attempt、Environment Version、generation、Runtime Session ID、容器 ID、
    “事实边界”或用户重置入口。
13. 平台测试、OpenAPI、Ruff/Pyright、Web lint/typecheck/build、迁移矩阵、Compose 安全检查、固定
    OpenHands contract/smoke 和真实 Playwright E2E 全部通过。

## 21. 实施切片

设计完成后严格按以下顺序一次实施一个切片；每项独立提交，不能跨项混合：

### FR-19 Agent Workspace 持久化与预启动 Runtime

- 新增一等 Workspace、allocation、Secret、Runtime Session/generation 模型和迁移；
- Runtime Provider 支持严格的 `AGENT_WORKSPACE` owner；
- Worker startup 幂等创建并主动预置默认 Runtime；
- 外置挂载、稳定 Secret、digest、永久期望运行和 replacement 适配；
- 证明未访问浏览器前容器已经 READY，物理删除后数据目录仍在。

### FR-20 独立 Conversation API 与 OpenHands 契约

- 新增 binding/command、独立 locator 和公开 API；
- 使用默认模型创建正式 OpenHands Conversation；
- 修正会话专属 `persistence_dir` 校验；
- 实现 events、message、interrupt、rename、delete、stream 和 Workspace terminal；
- 加入 create 幂等、消息 ambiguous delivery 和非瞬态错误停止规则。

### FR-21 顶层 Agent 会话工作台

- 新增一级 Tab 和稳定 URL；
- 实现会话列表、创建、对话、输入/停止、文件/终端抽屉；
- 删除独立页面中的全部 FlowRun/Runtime 内部信息；
- 实现刷新恢复、状态机、WS 补洞和错误退避；
- 不修改现有 FlowRun 按节点启动会话产品逻辑。

### FR-22 故障恢复、安全与真实 E2E

- 完整迁移、静态检查、测试、构建和 Compose 安全门禁；
- 固定 OpenHands 真实模型/Tool/terminal smoke；
- 双 Conversation 单 Runtime、刷新、kill/delete、lease takeover、旧 writer、重新部署和数据保留；
- 无缓存重新编译、打包、部署并按第 20 节全部验收。

FR-22 通过之前，不继续扩展 Agent Workspace 与 FlowRun/节点/流程编排的集成。

## 22. 最终边界

独立 Agent 工作台的稳定关系是：

~~~text
Agent Workspace（长期存在、平台启动时预置）
  |- Conversation locator 和展示元数据（PostgreSQL）
  |- OpenHands Conversation/Event（外置状态）
  |- 项目文件（外置 Workspace）
  |- 稳定 Secret Reference（PostgreSQL 加密引用）
  `- Runtime Session
       `- active generation（可随时替换的容器）
~~~

只要 Workspace、外置状态和稳定身份仍在，物理容器就可以被销毁和重建；只要 Runtime 暂时不可用，用户
仍能进入工作台并确认数据存在。FlowRun 以后可以引用这套已经验证的 Agent 执行基础设施，但不能反过来
成为独立 Agent 会话成立的前置条件。
