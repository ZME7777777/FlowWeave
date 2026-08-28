# FlowRun 级可替换 OpenHands Runtime 设计

> 状态：`FR-00 FROZEN`
> 日期：2026-08-21
> OpenHands 事实基线：`software-agent-sdk`
> `9a24f6c8866f353042a57df0514ccc900e3a0691`（四包 `1.44.0`）
> 本文冻结目标架构和后续迁移边界，不表示后续运行时代码已经落地。

## 1. 决策摘要

FlowWeave 后续采用以下唯一生产拓扑：

1. 不再运行顶层共享 OpenHands Agent Server，也不存在共享
   `OPENHANDS_BASE_URL` fallback。
2. 一个 `FlowRun` 只绑定一个稳定的逻辑 Runtime Session、一个宿主机持久工作空间和一个当前活跃的
   Agent Server generation。
3. 每个 generation 是完整的 OpenHands Agent Server 容器。该容器原生管理本 FlowRun 的全部
   Conversation；同一 FlowRun 的多个 Conversation 共享工作空间和 Agent Server，但保持各自的
   OpenHands conversation ID 和事件树。
4. 容器只是可替换的计算载体。Workspace、Conversation/Event、Bash Event 和 OpenHands 必需的
   persistence 均位于容器外，替换容器后必须使用原 conversation ID 重新加载。
5. FlowWeave 不保存 Conversation 消息、HEAD、事件树、运行状态、cursor 或消息状态机，不解释谁在
   “对话”。它只保存连接定位、FlowRun 引用、授权和独立审计事实。
6. FlowWeave 仍是能力治理控制面；由 FlowWeave 显式绑定的 Skill、MCP、Plugin、Hook、
   Agent Definition 等不可变版本由 FlowWeave 冻结，但只通过 OpenHands 正式创建字段、
   类型、Loader 和事件生命周期原生加载与执行。OpenHands 原生 ambient Plugin 发现允许保留。
7. Sandbox Controller 收缩并更名为 Runtime Provider。它仍负责物理容器和宿主机资源生命周期，但
   不管理 Conversation 或任何 Agent 功能。
8. 不提供默认 Environment Version。流程模板不绑定运行环境；每次创建 FlowRun 时必须由用户选择一个
   已验证且 digest 锁定的自定义 Environment Version，并由该 Run 及其 Snapshot 冻结。由同一流程模板
   创建的不同 FlowRun 可以使用不同的基础镜像。

## 2. OpenHands 能力事实与架构选择

固定目标源码已经证明：

- `StartConversationRequest.workspace` 的正式类型是 `LocalWorkspace`。
- Agent Server 的 Event Service 要求服务进程本地可见的 `LocalWorkspace`。
- `DockerWorkspace` 是 `RemoteWorkspace`，它启动的容器本身包含完整 Agent Server；它不是可挂到
  另一个中央 Agent Server 下的“纯 Tool 执行容器”。
- `RemoteConversation` 连接远端 Agent Server 时，会把远端工作目录作为该 Server 内部的
  `LocalWorkspace` 使用。
- `DockerDevWorkspace` 可用 OpenHands 正式 build 能力从 base image 动态打包 Agent Server 镜像；
  `DockerWorkspace` 使用已经打包好的 Agent Server 镜像启动容器。
- OpenHands Conversation 在文件系统中持久化，并以默认 45 秒 lease、owner instance 和递增
  generation 防止共享存储上的多实例同时写入。
- 正式 `POST /api/conversations/prepare-for-sandbox-pause` 可在暂停/替换前让服务准备释放运行态。

因此，固定目标版本不支持“一个中央 Agent Server 原生管理多个独立纯 DockerWorkspace 执行容器”。
若在 FlowWeave 中搭建该拓扑，就必须发明上游不存在的远程执行协议，违反 OpenHands-first 原则。
目标拓扑只能是每个 FlowRun 一个完整 Agent Server Runtime，FlowWeave 在其前面做连接定位和资源治理。

## 3. 目标拓扑

~~~text
FlowWeave API / Worker（控制面）
  |
  | flow_run_id + runtime_session_id + openhands_conversation_id
  v
FlowRun Runtime Provider（现 Sandbox Controller 收缩）
  |- 校验 Environment Version / runtime image digest
  |- 分配宿主机持久目录、网络、配额和 Secret Reference
  |- 启动、探活、fence、替换、drain、停止和删除 generation
  `- 返回当前 active generation 的受保护连接
       |
       v
OpenHands Agent Server generation N（可替换容器）
  |- Conversation A（OpenHands 原生状态）
  |- Conversation B（OpenHands 原生状态）
  `- LocalWorkspace -> /runtime/workspace/project
       |
       v
FlowRun 宿主机持久根目录（不随容器删除）
  |- workspace/project/
  |- state/conversations/
  |- state/bash-events/
  |- state/persistence/
  `- capabilities/<manifest-digest>/（只读）
~~~

稳定对象是 FlowRun Runtime Session 和上述持久目录；容器 ID、IP、端口、进程 ID、临时 API key 和
generation 都不是 Conversation 身份。FlowWeave 的 API 不向客户端暴露可绕过授权的物理容器地址。
创建 FlowRun 后由 Worker 主体预置首个 generation；API 只在 Session 激活后调用 OpenHands 正式会话
接口。Runtime Provider 按 manager scope 和显式角色标签把 API/Worker 接入该 Run 的专属网络：API
代理已授权的会话交互，Worker 执行创建、替换和恢复任务；二者都不持有 Docker Socket，Provider 也不
接入 Runtime 网络。

预置 generation 只是为该 FlowRun 静默准备计算环境并绑定持久 Workspace，不创建 Conversation，也不
改变客户端页面。用户继续停留在流程运行界面；只有选中一个节点、建立该节点 Attempt 并显式执行
“启动节点会话”后，API 才使用该 Attempt 的冻结 Snapshot、输入绑定和工作目录创建 OpenHands 原生
Conversation，并进入会话页面。服务端不得在缺少节点上下文时回退到默认入口或最近一次 Attempt。

## 4. 强制自定义 Environment Version

### 4.1 产品约束

- Environment 由用户显式创建；Environment Version 是追加式、不可变且 digest 锁定的发布物。
- 新建 Environment 只收集名称和说明。首次 Setup Session 由平台选择并冻结内部启动镜像；
  该镜像只是建造配置终端的种子，不是用户 Environment Version，也不能被 Flow 绑定。
- Flow Definition 不保存 Environment Version。创建 FlowRun 的命令必须显式选择一个 `READY`
  Environment Version，Run 与其每个 Snapshot 冻结同一版本；一个 FlowRun 内所有 Conversation 使用
  该 Run 冻结的同一版本，不允许节点在运行时切换环境。
- 未选择、仍在构建、验证失败、已退役、digest 漂移或目标平台不兼容时，FlowRun 不得启动；这些条件
  不阻止流程模板独立保存和复用。
- 删除默认 Environment、隐式平台镜像选择和共享 Agent Server fallback。历史数据不通过猜测补默认值。

### 4.2 OpenHands 原生打包链

Setup Session 从平台内部启动镜像或本 Environment 已发布版本建立可交互终端。发布时先将
用户在终端中完成的文件系统冻结为不可变用户 base image，再由 Runtime Provider 调用
OpenHands 正式 `openhands.agent_server.docker.build` 能力（即 `DockerDevWorkspace` 使用的 build 链）
将该 base image 打包为包含固定 OpenHands Agent Server、SDK、Tools、Workspace 和 FlowWeave 固定
overlay 的 Runtime Image。构建结果必须冻结：

- 平台 Setup 启动镜像引用及创建会话时解析的内容 digest；
- 用户 base image repository 和 digest；
- OpenHands source commit、source archive digest 和四包版本；
- overlay digest、build target、platform 和构建日志摘要；
- 最终 Runtime Image repository 和 image digest；
- `contract_check.py`、基础 Tool/Workspace 探针和安全扫描结果。

FlowRun 启动和 replacement 只使用已发布 Runtime Image digest，遵循 `DockerWorkspace` 的预构建镜像
启动语义，不在故障恢复关键路径重新构建镜像。这样既使用 OpenHands 原生动态打包能力，又保证替换
快速、可复现且不依赖浮动 tag。FlowWeave 不自建另一套 Agent Server Dockerfile 生成协议。

“基本预置能力”来自上述固定 OpenHands Runtime 层和用户 base image。FlowRun/Conversation 特定且由
FlowWeave 显式绑定的 Skill、MCP、Plugin、Hook、Agent Definition、Policy 与 Memory 不烘焙进可变
镜像，而按 Snapshot Runtime Manifest 只读物化，并在创建 Conversation 时通过 OpenHands 正式字段或
Loader 加载。OpenHands 1.44.0 对 HOME 和项目目录的 ambient Plugin 原生扫描按上游默认保留；
FlowWeave 不再用私有请求字段或构建时源码补丁禁用它。

## 5. 持久化与可替换性

每个 FlowRun Runtime Session 分配一个服务端推导、租户隔离、禁止调用方指定绝对路径的宿主机根目录。
至少持久化并重新挂载：

| 宿主机目录 | 容器内用途 | 事实所有者 |
|---|---|---|
| `workspace/project` | `LocalWorkspace` 工作目录 | FlowRun 资源；OpenHands 执行读写 |
| `state/conversations` | Conversation、Event、lease、HEAD | OpenHands |
| `state/bash-events` | Bash command/event 文件 | OpenHands |
| `state/persistence` | `OH_PERSISTENCE_DIR` 所需持久 Store | OpenHands |
| `capabilities/<digest>` | 固定 Skill/Plugin/Memory 等显式物化内容 | FlowWeave 治理；OpenHands 只读加载 |

禁止把上述目录继续放在 `/runtime/workspace` tmpfs。容器只允许把缓存、进程临时文件、日志缓冲和
worktree 临时目录放在可丢弃层；若启用 OpenHands worktree，必须先另行把其持久化和清理契约纳入
Runtime Provider，默认保持关闭。

每个 Runtime Session 使用稳定的 `OH_SECRET_KEY` Secret Reference。该 Secret 不进入镜像、Manifest、
普通数据库列、日志或前端；replacement generation 取回同一版本的 Secret，保证 OpenHands 已加密状态
可恢复。连接 API key 可轮换，但不得替代持久 secret key。所有挂载在容器启动前进行 owner、普通目录、
符号链接、权限和租户边界复核。

Runtime 被物理删除不等于 FlowRun 状态被删除。只有显式删除 FlowRun 且引用保护、审计和保留策略均
满足时，Runtime Provider 才可物理删除外部持久目录。

## 6. Runtime Session、generation 与替换协议

### 6.1 身份与 fencing

- `runtime_session_id`：FlowRun 生命周期内稳定，一对一绑定 `flow_run_id`。
- `generation`：从 1 开始单调递增，标识某次物理 Agent Server 实例。
- `active_generation`：唯一允许 FlowWeave 新建连接和写命令的 generation。
- `instance_id/container_id/endpoint`：generation 级物理事实，不进入 Conversation locator。
- 所有启动、激活、替换、停止和删除命令携带 expected generation/row version；过期 Worker fail closed。

FlowWeave 的 generation fence 与 OpenHands Conversation lease 同时生效：前者阻止平台把新命令路由给
旧容器，后者保护共享 Conversation 存储。任何时刻最多一个 generation 可对持久 Conversation 写入。

### 6.2 正常替换

1. Runtime Provider 取得 replacement lease，CAS 将 Session 标为 `REPLACING`，冻结 generation N 的
   新连接并创建 generation N+1 记录。
2. 用同一 Runtime Image digest、持久挂载、稳定 secret 和新临时连接凭据启动 N+1；只做 Server
   health/capability 探针，不在其上打开 Conversation。
3. 在平台已冻结新写入后，对 N 调用 OpenHands 正式 `prepare-for-sandbox-pause`，关闭活动 event service
   并释放其运行态；在途操作必须先进入可判定状态。
4. 停止 N；只有 OpenHands lease 已释放、owner 已判死或默认 45 秒 lease 到期后，N+1 才能用原 ID
   reload Conversation。不得将 lease TTL 配成 0 来掩盖替换竞态。
5. 对已绑定的至少一个原 conversation ID 执行只读 reload/inspect 探针，核对持久事件身份；成功后
   CAS 激活 N+1 并恢复路由。
6. 优雅删除 N 的物理容器，但保留宿主机持久目录和 generation 审计记录。

### 6.3 异常替换

若 N 已退出或无法响应，第 3 步记录为不可达，直接停止/隔离残留容器并等待 lease takeover 条件。N+1
可以立即并行完成镜像和 Server 预热，但在取得 Conversation 写所有权前不得接收消息。恢复失败只把该
FlowRun 标为 `DEGRADED/RECONNECTING`，不得影响其他 FlowRun、FlowWeave API、Environment 管理或流程
查询。重试必须复用同一 N+1 记录，禁止每次轮询无限创建容器。

若 N 在网络分区后重新出现，generation fence 必须继续拒绝其路由，Runtime Provider 随后停止并删除它。
如果新 generation 的 reload 校验失败，保持 FlowRun 可诊断且禁止写入，不得创建一个空 Conversation
冒充恢复成功。

## 7. Conversation 边界与产品语义

### 7.1 唯一事实源

OpenHands 是以下数据的唯一事实源：

- Conversation 生命周期和可恢复状态；
- `user` / `assistant` 线路 role、消息内容、Tool Action/Observation；
- Event tree、正式 `id/parent_id/action_id/tool_call_id`、HEAD、fork 和 navigate；
- run/pause/confirmation/condense/task/goal 等 Agent 生命周期；
- Conversation cursor、stats 和服务内 lease。

FlowWeave 不再写 `AgentMessage` 副本，不维护 Conversation 业务状态机，不把 WebSocket/REST cursor 变成
第二事件存储，不按 AUTO、HUMAN_CREATED、节点、用户或 Agent 创建不同类型的 Conversation。断线后由
客户端经 FlowWeave 授权代理，使用 locator 重新连接 OpenHands 原生 Conversation。

### 7.2 最小 locator

FlowWeave 只持久化：

~~~text
flow_run_id
runtime_session_id
openhands_conversation_id
可选的无语义 display label / created_at / last_connected_at
~~~

locator 不保存物理 endpoint、容器 ID、消息 role、事件 cursor、HEAD、Conversation 运行状态或创建者
角色。权限来自 FlowRun/Project 的现有授权；“谁进行了连接或发出操作”进入独立审计日志，不进入
Conversation 内容模型。

Node/Attempt 可以保存 `openhands_conversation_id` 作为执行引用和 Artifact lineage，但它们不拥有、
创建一种特殊 Conversation，也不能在删除 Node/Attempt 时级联删除 OpenHands Conversation。一个
Conversation 可被 FlowRun 中多次执行引用；是否新建或复用由显式流程动作决定。

公开创建会话命令必须携带当前 FlowRun 中显式选择的节点 Attempt 上下文，且该 Attempt 已通过输入与
开始门禁、正处于可启动状态。FlowRun 创建、Runtime Ready、进入会话路由或会话列表为空都不是创建
Conversation 的触发器。

### 7.3 提问与回复

产品 UI/API 统一展示“会话、提问、回复”，删除“自动会话/人工会话”“节点消息/用户消息”“谁回答”
等产品角色和分支状态机。OpenHands 线协议仍必须保留其正式 `user` 和 `assistant` role：向 OpenHands
发送提问时使用正式 `user` 消息，展示 Agent 结果时读取正式 `assistant` 消息。该技术映射不重新成为
FlowWeave 的领域角色。

认证主体、权限主体和审计 actor 继续保留，因为它们回答“谁有权执行/谁执行了操作”，而不是“谁是
Conversation 中的发言角色”。系统/Tool 事件按 OpenHands 原生事件展示为运行活动，不伪装成提问或回复。

## 8. Runtime Provider 职责

现 Sandbox Controller 收缩后的 Runtime Provider 保留：

- 平台 Setup 启动镜像验证、digest 冻结和 OpenHands 正式 Runtime Image 打包；
- FlowRun 持久目录分配、挂载校验和生命周期；
- 容器启动、健康、网络、端口、资源配额、日志、TTL、停止和物理删除；
- Runtime Session/generation、replacement lease、fencing、drain 和故障恢复；
- Runtime Image/source/capability digest 与实际容器的启动前后校验；
- Secret Reference 的临时注入与连接定位，且不返回明文 Secret。

Runtime Provider 不得：

- 创建或维护 Conversation 消息、状态、cursor、HEAD 或分支；
- 解释 Agent Action/Observation 或实现 Tool loop；
- 自建 Skill/MCP/Plugin/Hook/Agent Definition 注册器或执行器；
- 用提示词、私有 JSON、私有 HTTP 或文本规则模拟 OpenHands 能力；
- 把物理容器身份当作 Conversation 身份。

“收缩”不等于只做镜像仓库。若没有 Runtime Provider 管理物理容器和宿主机挂载，可替换性、隔离和
故障恢复都无法成立；这些是基础设施控制面职责，不是 Agent 功能。

## 9. 能力原生加载契约

FlowWeave 保留版本、digest、权限、审批、Secret Reference、供应链扫描和 Snapshot Runtime Manifest；
Conversation 创建时将固定内容物化为 OpenHands 正式输入：

| 能力 | FlowWeave 仅负责 | OpenHands 原生负责 |
|---|---|---|
| Skill | 固定 Version/内容/digest、只读物化、触发配置 | `AgentSkills`、trigger、activation、invoke |
| MCP | 固定 server 配置和 Secret Reference，调用边界解密 | 正式 MCP 配置、连接、Tool 注册和 OAuth state |
| Plugin | 固定来源 commit/ZIP/逐文件 digest、只读物化 | `PluginSource`、Loader、贡献合并 |
| Hook | 固定 Hook Set 和脚本 digest、只读物化 | 正式 `hook_config` 和 Hook 生命周期 |
| Agent Definition | 固定定义、Policy 和依赖子集 | `agent_definitions`、`task_tool_set` 和 Task 生命周期 |
| Profile/Policy | 固定无 Secret spec 和允许边界 | 正式 `agent`、confirmation/security/condenser 等字段 |
| Memory | 固定来源内容和 digest，物化为只读 bundle | 正式 `load_memory` 生命周期从 `<working_dir>/.openhands/memory/MEMORY.md` 加载 |

每次创建 Conversation 都从该 FlowRun 冻结 Snapshot 编译 FlowWeave 显式绑定能力的正式请求；不得
从浮动 Marketplace、可变 Server Profile Store 或旧平台消息状态恢复这些显式绑定能力。OpenHands 对
HOME/项目的 ambient Plugin 扫描是例外：它保持上游原生默认语义，不纳入 FlowWeave Snapshot 唯一性
承诺。FR-10 必须删除仍由 FlowWeave 注册、执行、按文本投影或修改 OpenHands 源码的旁路；Memory
的 USER/PROJECT 冻结内容按会话工作目录合并成只读 project Memory bundle，避免进程级 HOME 在同一
FlowRun 多 Conversation 间串扰，再由 OpenHands 正式 `load_memory` 生命周期原生加载。固定镜像真实
create/smoke 验证仍集中在 FR-12。

固定 OpenHands 1.44.0 已正式负责 Agent Profile v1→v2 迁移、LLM Profile 预检、Provider Connection
凭据的 read-at-use 解析、Secret serializer 探测，以及 subscription LLM 的 condenser dispatch。FlowWeave
不复制这些存储迁移、凭据刷新或 condenser 调度生命周期；镜像门禁以实际迁移、轮换后重读、嵌套 Secret
识别和 subscription condenser 行为验收，而不冻结上游字段全集或默认值表。FlowWeave 仍只持有不可变
Snapshot 引用、权限、用量归属和调用边界 Secret Reference；显式 Agent JSON 继续避免从可变 Server
Profile Store 恢复产品事实。

远程标题生成的 Profile 解析、调用上下文和 metadata cache 修复不属于当前冻结提交
`9a24f6c8866f353042a57df0514ccc900e3a0691`。在升级到包含这些修复且完成行为验收的源码前，Agent
Workspace 必须继续关闭 OpenHands `autotitle`，保留 FlowWeave 独立标题任务、失败兜底和手动标题 CAS；
不得仅因后续上游 `main` 已修复而提前删除。
没有对应 FlowWeave 产品需求的 OpenHands 能力不因此进入范围。

## 10. 数据模型草案

最终名称可在迁移切片按现有模块命名调整，但语义不得扩大。

### `flow_run_runtimes`

~~~text
id / runtime_session_id (stable UUID)
flow_run_id (unique FK)
environment_version_id
runtime_image_digest
workspace_allocation_id
active_generation
status
row_version
created_at / updated_at / stopped_at
~~~

### `runtime_generations`

~~~text
id
runtime_session_id (FK)
generation (unique per session, monotonically increasing)
managed_runtime_id / physical provider reference
instance_id
runtime_image_digest
state
fence_token / row_version
started_at / ready_at / draining_at / stopped_at / deleted_at
failure_code / redacted failure summary
~~~

物理 endpoint 只存在受保护的 provider connection record 中，可轮换且不作为 Conversation 外键。
现有 `ManagedSandbox.generation` 若能满足单调性、唯一性和历史审计，可以迁移复用；不得同时保留两套
active generation 真相。

### `flow_run_conversation_bindings`

~~~text
id
flow_run_id (FK)
runtime_session_id (FK)
openhands_conversation_id
display_label (optional, non-authoritative)
created_at / last_connected_at
~~~

唯一约束至少覆盖 `(runtime_session_id, openhands_conversation_id)`。该表不是 Conversation Store，不得
加入 message、role、status、cursor、head、last_event、sandbox_id、node owner 或 user owner 字段。

## 11. 迁移原则

1. 在 FlowRun 创建边界建立 Environment Version 强制选择和 Runtime Image 发布门禁；没有 READY
   自定义镜像时 fail closed，Flow Definition 不持有该引用。
2. 再建立 FlowRun 持久目录、Runtime Session/generation 和 Runtime Provider 路径。
3. 新创建的 FlowRun 只走新路径；共享 Agent Server fallback 同时禁止，避免双真相。
4. 已有运行中的旧 FlowRun/Conversation 不做在线无损搬迁，因为当前平台消息投影、tmpfs Workspace 和
   Sandbox owner 不能证明可转成 OpenHands 原生持久状态。它们保持只读归档或由用户显式重跑。
5. 新路径稳定后停止 `AgentConversation`/`AgentMessage`、AUTO/HUMAN_CREATED、平台 Conversation 状态机、
   cursor/完整事件投影和按 Attempt/Conversation 分配 Sandbox 的新写入，再执行引用审计和删除迁移。
6. Sandbox owner 收敛为 `FLOW_RUN`；验证、构建、OAuth 等短生命周期 Runtime 若仍有产品需求，使用
   明确的非会话型临时 owner，不得混入 FlowRun Conversation。
7. Compose 顶层 Agent Server、默认 `OPENHANDS_BASE_URL`、source-container mount 和 tmpfs Workspace
   依赖最后删除，并由架构测试防止回归。

## 12. 分阶段实施与统一验收

FR-01–FR-11 每个切片只实现一个独立代码边界，完成时只对受影响文件运行最窄语法/解析/编译检查和
`git diff --check`，不做业务、迁移、协议、安全、恢复、容器或 E2E 验证。所有验证统一由 FR-12 执行。

必须在 FR-12 证明：

- 没有自定义 READY Environment Version 时发布和运行均拒绝；不存在默认/fallback。
- 一个 FlowRun 的多个 Conversation 只产生一个 active Agent Server generation，并能以各自原 ID reload。
- kill -9、进程崩溃、健康失败、网络分区、替换中 Worker 重启和旧容器复活都不会产生双 writer。
- replacement 使用相同持久状态恢复原 conversation ID、event identity 和 Workspace 文件；不创建空会话
  冒充恢复。
- 一个 FlowRun 的容器异常不影响其他 FlowRun 和 FlowWeave 控制面可用性。
- Skill、MCP、Plugin、Hook、Agent Definition 等均由 OpenHands 正式 create/Loader/事件加载，无平台
  Agent 执行器或私有协议旁路。
- API/UI 只呈现会话、提问、回复和 reconnecting；认证/授权/审计 actor 正确但不污染内容角色。
- 删除 FlowRun 前外部目录受引用保护；删除完成后容器、Secret 临时物化和持久目录按保留策略清理。

## 13. 已知风险与非目标

- OpenHands 默认 lease 会使异常 takeover 最坏需要等待约 45 秒；可以预热新 Server，但不能以双写换取
  表面上的秒级恢复。只有固定源码提供并验证更安全的显式 lease release 后才调整。
- OpenHands Agent Server 是相对较重的 Runtime。后续可用指标驱动冷启动缓存或镜像预拉取，但不得复用
  含其他 FlowRun 状态的 warm instance；当前不引入共享池。
- 本任务不修改 OpenHands 源码。若后续需要中央 Server/远程纯 Workspace、显式 lease handoff 或其他
  正式契约，必须单独授权 OpenHands fork，并冻结 upstream base、fork commit、source digest 和兼容测试。
- FR-00 只冻结设计、迁移顺序和任务跟踪，不修改数据库、API、Runtime 或 UI 实现。
