# FlowRun OpenHands Runtime 重构进度

> 创建日期：2026-08-21
> 状态：`COMPLETE`
> 当前执行切片：无
> 下一可执行切片：无
> 架构设计：`docs/flowrun-openhands-runtime-design.md`
> Agent 工作台设计：`docs/agent-workbench-technical-design.md`

## 1. 跟踪边界

本文是 FlowRun OpenHands Runtime 重构的唯一任务进度来源，从零开始记录本次架构的决策、任务、依赖、
验证和恢复信息，不继承此前重构任务的阶段编号、完成状态、实现结论或验收结果。

此前的重构决策不能作为本任务已经完成、可以跳过验证或必须保留现有实现的依据。现有源码只作为
“当前行为”的审计对象；是否保留必须重新按照本设计、固定 OpenHands 源码和真实运行证据判断。

本任务只修改 FlowWeave。OpenHands 源码仓库保持只读，目标事实基线仍为固定 commit
`f09e03eac772290feeb51b7d7390ffaefeca1a09` 和由其构建的四个 `1.42.0` 包。

## 2. 最终目标

- 不存在顶层共享 OpenHands Agent Server。
- 不存在默认 Environment Version 或共享 Server fallback。
- 用户必须创建并发布自定义 Environment Version，并在每次创建 FlowRun 时选择；Flow Definition 不绑定
  环境，Run 与 Snapshot 冻结本次选择。
- 新建 Environment 不要求用户预先提供基础镜像；平台内部启动镜像只用于创建配置终端，
  不是默认 Environment Version。
- 每个 FlowRun 对应一个宿主机持久工作空间、一个稳定 Runtime Session 和一个可替换的 OpenHands
  Agent Server generation。
- 同一 FlowRun 的全部 Conversation 由该 Agent Server 原生管理，不因节点执行或用户新建会话重复启动
  容器。
- FlowWeave 不保存 Conversation 消息、事件树、HEAD、cursor 或运行状态，只保存
  `flow_run_id + runtime_session_id + openhands_conversation_id` 等最小 locator、授权和独立审计事实。
- Sandbox Controller 收缩为 Runtime Provider，只管理镜像、持久目录、容器、网络、配额、generation、
  健康、替换和删除，不实现 Agent 或 Conversation 功能。
- Skill、MCP、Plugin、Hook、Agent Definition、Profile、Policy 和 Memory 只通过 OpenHands 正式类型、
  创建字段、Loader、Tool、事件和生命周期加载执行。
- 产品统一为“会话、提问、回复”；OpenHands 线路层保留正式 `user/assistant` role，认证和审计 actor 独立。

## 3. 不可突破的约束

1. 容器是可替换计算载体，不能成为 Workspace、Conversation 或 Secret 的唯一持久位置。
2. 任意时刻一个 FlowRun 最多一个 generation 可写 OpenHands Conversation 状态。
3. replacement 必须恢复原 conversation ID 和正式事件身份，不得创建空会话冒充恢复。
4. Environment 和 Runtime Image 必须按 digest 冻结，禁止浮动 tag 和隐式默认值。
5. FlowWeave 不得以提示词、文本约定、私有 JSON、私有 HTTP 或平台执行器模拟 OpenHands 已有能力。
6. 历史数据不能安全转换时保持只读归档或要求显式重跑，不猜测迁移。
7. 一个 FlowRun 的 Runtime 故障不能影响其他 FlowRun 或 FlowWeave 控制面可用性。

## 4. 当前基线

- Git 分支：`feat/refactor`。
- Alembic 唯一 head：`0051_physical_delete`。
- 当前有 Environment 时，自动执行按 Attempt、人工会话按 Conversation 分配受管容器。
- 当前无 Environment 时会回退到共享 Agent Server。
- 当前 Sandbox Controller 直接启动包含 Agent Server 的容器，并把 `/runtime/workspace` 放在 tmpfs。
- 当前 FlowWeave 自行维护 Conversation、Message、状态机、cursor 和 Sandbox 绑定。
- 当前固定 OpenHands 不支持“中央 Agent Server + 多个纯远程执行 DockerWorkspace”；
  `DockerWorkspace` 启动的远端本身也是完整 Agent Server。

上述条目都是待改变或重新验证的当前事实，不是目标设计。

## 5. 状态规则

- `PENDING`：依赖未满足。
- `READY`：依赖满足，可以成为下一个执行切片。
- `CURRENT`：当前唯一正在实施的切片。
- `DONE`：切片实现且受影响代码通过基础语法/解析/编译检查；不代表任何业务功能已经验证。
- `BLOCKED`：有可复现阻塞、安全降级和明确解锁条件。
- `COMPLETE`：仅用于整个重构；全部切片完成且 `FR-12` 最终门禁通过。

任何时刻最多一个 `CURRENT`。一次只完成一个最小可独立验收切片，更新本文并提交该切片代码和文档；
确认提交成功后立即停止，不自动开始下一项。如果实现发现任务边界过大，先在本文拆分任务和依赖，
再继续编码。

### 普通切片检查规则

FR-01–FR-11 完成时只允许：

- 对受影响 Python 文件执行最窄 `py_compile`/等价语法检查；
- 对受影响 TypeScript/JavaScript 或其他语言文件执行最窄解析/编译检查；
- 检查迁移、配置和脚本文件可以被对应语言解析，但不连接数据库、不启动服务或容器；
- 运行 `git diff --check` 并核对任务状态唯一性。

FR-01–FR-11 不运行任何业务行为单元测试、集成测试、迁移 upgrade/downgrade、OpenAPI 生成、完整 Lint/
类型检查/构建、OpenHands contract/smoke、真实 Runtime、安全/恢复矩阵或 E2E。所有功能和系统验证统一
放到 FR-12；普通切片即使涉及安全或迁移，也只实现代码并做基础语法检查。

## 6. 任务清单

### FR-00 架构、边界和实施顺序冻结 — DONE

交付：

- 新建独立架构设计，冻结最终拓扑、持久化、generation、Conversation locator、Runtime Provider 和能力
  原生加载边界。
- 建立本独立进度跟踪，不继承此前重构任务状态。
- 将仓库恢复和验证说明切换到 `FR-*` 主线。

验证：设计覆盖用户全部明确约束；固定 OpenHands 源码事实与目标拓扑一致；文档引用和 whitespace 检查
通过。本切片不修改运行时代码。

### FR-01 强制绑定用户自定义 Environment Version — DONE

目标：

- Environment 必须由用户创建，Flow/Flow Snapshot 必须绑定一个不可变 `READY` Environment Version。
- 冻结用户 base image digest、OpenHands source/package/overlay provenance 和最终 Runtime Image digest。
- 使用 OpenHands 正式 `openhands.agent_server.docker.build` 链打包用户 base image；运行和 replacement 只
  启动已经发布的 digest 镜像。
- 删除默认 Environment、无 Environment 运行和隐式共享 Agent Server fallback。
- 历史无绑定 Flow/Snapshot fail closed，不猜测回填。

本切片退出条件：完成上述代码改造；受影响代码通过基础语法/解析/编译检查；`git diff --check` 通过。
数据库/API/发布/运行行为、镜像构建、迁移实跑和拒绝路径全部延后到 FR-12 验证。

### FR-02 FlowRun 外置 Workspace 与 OpenHands state allocation — DONE

依赖：`FR-01`。

目标：每个 FlowRun 分配租户隔离的 `workspace/project`、`state/conversations`、`state/bash-events`、
`state/persistence` 和只读 capability 目录；配置稳定 `OH_SECRET_KEY` Secret Reference；删除持久状态
tmpfs 依赖；完成路径、符号链接、权限、删除保护和失败回滚。

### FR-03 Sandbox Controller 收缩为 Runtime Provider — DONE

依赖：`FR-02`。

目标：主执行 owner 收敛为 `FLOW_RUN`；保留 Runtime Image、挂载、容器、网络、配额、健康、TTL、日志和
删除；移除 ATTEMPT/CONVERSATION 容器所有权及 Agent/Conversation 管理语义。构建、验证、OAuth 等临时
Runtime 必须使用明确的非会话 owner。

### FR-04 Runtime Session 与 generation 数据模型 — DONE

依赖：`FR-03`。

目标：建立或收敛 `flow_run_runtimes`、`runtime_generations`；保证每 Run 一个 stable session、generation
单调递增、active generation 唯一、命令 CAS fencing 和历史可审计。物理 endpoint、容器 ID 和连接凭据
不得成为 Conversation 身份。

### FR-05 同 FlowRun 多 Conversation 与最小 locator — DONE

依赖：`FR-04`。

目标：建立 `flow_run_conversation_bindings`；所有新建和连接都路由到 FlowRun active Agent Server；
同 Run 多 Conversation 不重复创建容器。Node/Attempt 只可引用 OpenHands conversation ID，不拥有会话。

### FR-06 OpenHands 原生状态外置与原 ID reload — DONE

依赖：`FR-05`。

目标：把 Workspace、Conversation/Event、Bash Event 和 `OH_PERSISTENCE_DIR` 全部挂载到 FlowRun 持久目录；
使用稳定 secret 和原 conversation ID reload；证明 Workspace 文件和 OpenHands 正式事件 identity 跨进程
重启不变。禁止平台消息、cursor、HEAD 或空会话替代恢复。

### FR-07 generation fencing、drain 与 replacement — DONE

依赖：`FR-06`。

目标：实现 replacement lease、N+1 预热、平台写入冻结、OpenHands 正式
`POST /api/conversations/prepare-for-sandbox-pause`、默认 45 秒 Conversation lease 协调、原 ID reload、
active generation CAS、旧 generation 隔离/停止/删除和 Worker 重启幂等。任意时刻最多一个 writer。

### FR-08 删除共享 Agent Server 和旧 Runtime 分支 — DONE

依赖：`FR-07`。

目标：删除 Compose 顶层 Agent Server、`OPENHANDS_BASE_URL` fallback、共享 Runtime 分支、
source-container mount、Attempt/Conversation 新启容器和持久状态 tmpfs；新增架构测试防止回归。

### FR-09 Conversation 模型 FlowRun 化与去角色化 — DONE

依赖：`FR-08`。

目标：停止并删除 AUTO/HUMAN_CREATED、节点/用户会话角色、平台 `AgentMessage` 真相、Conversation 状态机、
完整事件和 cursor 投影；只保留 locator、授权、流程引用和独立审计 actor。OpenHands `user/assistant` 只在
线路适配层存在。

### FR-10 OpenHands 能力原生加载复核 — DONE

依赖：`FR-09`。

目标：逐项复核 Skill、MCP、Plugin、Hook、Agent Definition、Profile、Policy 和 Memory 的创建、注册、
加载、执行和恢复路径；FlowWeave 显式传入的能力只允许固定 OpenHands 正式字段、Loader、Tool、
事件和生命周期。删除平台注册器、Agent 执行器、提示词/文本/私有 JSON 旁路以及 OpenHands 源码
补丁。真实固定镜像 create/smoke 按阶段规则集中在 FR-12。

范围决策：

- OpenHands 1.42.0 原生 `LocalConversation._ensure_plugins_loaded()` 对 HOME/项目目录的 ambient Plugin
  扫描允许保留，不再要求它受 Snapshot Runtime Manifest 唯一性约束。FlowWeave 不传入私有
  `load_ambient_plugins` 字段，也不在 Runtime Image 构建时修改 OpenHands 源码。
- FlowWeave 显式绑定的 Plugin 仍按冻结 `PluginSource` 交给 OpenHands 正式 Loader；ambient Plugin
  是上游默认行为，不成为 FlowWeave 注册器或执行器。
- 冻结 Memory 的 USER/PROJECT 内容合并为 digest-scoped 只读 bundle，由每个 Conversation/Attempt
  的工作目录暴露为 `<working_dir>/.openhands/memory/MEMORY.md`，并只通过 OpenHands 正式
  `AgentContext.load_memory`/`load_memory()` 生命周期加载。不再创建 Attempt/Conversation 私有 Memory
  mount 或清理状态机。

### FR-11 API、UI、历史数据与运维收口 — DONE

依赖：`FR-10`。

目标：客户端只持 FlowRun/locator，通过 FlowWeave 授权代理连接 active generation；统一“会话、提问、
回复”和 `RECONNECTING/DEGRADED` 展示；不暴露物理 endpoint。旧 Run 只读归档或显式重跑；补齐健康、
替换、删除、保留、诊断和运维入口。

### FR-12 最终故障恢复、安全与 E2E 门禁 — DONE

依赖：`FR-01`–`FR-11` 全部 `DONE`。

集中验证：

1. 平台 Ruff/Pyright/全量测试，Web lint/typecheck/build，OpenAPI 和架构边界。
2. PostgreSQL 空库、当前库、历史数据和 downgrade/upgrade 迁移矩阵。
3. 固定 OpenHands source/image provenance、contract check 和真实 Runtime smoke。
4. 多 Conversation 单 Runtime、原 ID reload、Workspace/事件 identity 持久化。
5. kill、进程崩溃、健康失败、网络分区、替换中 Worker 重启、旧容器复活和双 writer 拒绝。
6. 一个 FlowRun 故障不影响其他 FlowRun 或控制面。
7. Environment 强制绑定、镜像 digest、Secret、路径、权限、租户隔离和删除保护。
8. Skill/MCP/Plugin/Hook/Agent Definition 等 OpenHands 原生加载，无平台 Agent 旁路。
9. 会话/提问/回复、reconnecting、历史归档和 FlowRun 删除产品 E2E。

全部通过后将本文件顶层状态改为 `COMPLETE`；失败项必须回到对应 `FR-*` 修复，不能以历史验收结果覆盖。

### FR-13 内部 Setup 启动镜像与 Environment 创建语义收口 — DONE

依赖：`FR-12`。

目标：新建 Environment 只接收名称和说明；由平台配置的 Setup 启动镜像创建首次隔离终端，
并在 Environment 创建边界解析、冻结内容 digest。用户不能通过公开 API 或 UI 传入或修改该内部
启动镜像，且它不再作为 Environment 业务字段展示。发布后的自定义 Environment Version 仍是 Flow 唯一可绑定的不可变运行环境，不引入默认
Environment Version 或运行 fallback。

验收：定向平台环境测试、OpenAPI 契约、Web lint/typecheck/build 和定向 E2E 静态断言通过；
Alembic head 不变；`git diff --check` 和任务状态唯一性通过。

### FR-14 Runtime Provider Setup 镜像引用契约修复 — DONE

依赖：`FR-13`。

目标：修复 Runtime Provider 的 `resolve-base-image` 请求模型仍只接受 digest 引用、导致 FR-13 配置的
本地平台 Setup 镜像 tag 在到达 Docker 解析前被 422 拒绝的问题。该控制接口只接受受限镜像引用语法，
并继续由 `resolve_setup_image` 在创建 Environment 时解析和冻结实际内容 digest；不放宽用户公开 API，
不引入默认 Environment Version 或运行 fallback。

验收：Runtime Provider 定向单元测试、受影响 Python 格式/静态检查、Compose 配置解析、实际 Compose
创建 Environment、Alembic head、`git diff --check` 和任务状态唯一性通过。

### FR-15 FlowRun 启动环境与会话交互闭环 — DONE

依赖：`FR-14`。

目标：Flow Definition 不再保存或要求 Environment Version；创建每个 FlowRun 时显式选择一个 `READY`
Environment Version，并由 Run、Snapshot 和 Runtime Session 冻结，使同一流程模板的不同运行可使用不同
基础镜像。FlowRun 即使尚未创建节点执行，也必须能基于冻结 Snapshot 的默认入口上下文创建 OpenHands
原生 Conversation；Web 启动成功后创建首会话并直接进入会话工作台，手动新建、错误反馈和流程取消必须
立即产生可见结果。取消 Run 后禁止新会话和新写入，已有 Runtime 进入只读终态。

验收：新增迁移 upgrade/downgrade、平台定向测试与 OpenAPI、Ruff/Pyright、Web lint/typecheck/build、
定向 Playwright、Alembic head、任务状态唯一性与 `git diff --check` 通过；不修改 OpenHands 源码。

### FR-16 FlowRun Runtime 预置与按节点显式启动会话 — DONE

依赖：`FR-15`。

目标：纠正 FR-15 的会话启动语义。Flow Definition 和流程编排页不绑定或选择 Environment Version；
用户仅在创建 FlowRun 时选择本次运行的基础镜像。FlowRun 创建后由 Worker 静默预置唯一 Runtime
generation 并绑定该 Run 的持久工作空间，Web 保持在流程运行界面，不自动创建 Conversation，也不跳转
会话页。用户必须在运行界面选择一个节点并显式启动，届时才基于该节点的冻结 Snapshot 上下文创建
OpenHands 原生 Conversation 并进入会话页；未选节点、Runtime 未就绪或 Run 已取消时均禁止创建。

验收：平台定向测试与 OpenAPI、Ruff/Pyright、Web lint/typecheck/build、定向 Playwright、真实 Compose
Runtime 预置与按节点启动会话、取消流程、Alembic head、任务状态唯一性和 `git diff --check` 通过；
重新编译打包部署且服务健康；不修改 OpenHands 源码。

### FR-17 流程编排页面恢复与异常数据容错 — DONE

依赖：`FR-16`。

目标：修复进入“流程编排”后发生前端渲染异常的问题；不再把渲染异常归因为浏览器版本不兼容，也不向用户
暴露“重置页面状态”入口。浏览器刷新必须自动迁移或丢弃过期的深层运行导航上下文，并回到安全页面；流程
画布必须对历史或异常的流程、节点资产、端口、门禁和目录数据做只读容错，单条异常数据不能令整个控制面
页面崩溃。

验收：受影响 Web lint/typecheck/build、流程编排定向 Playwright、损坏浏览器状态回归、`git diff --check`、
任务状态唯一性与实际部署后页面验证通过；不修改 OpenHands 源码。

### FR-18 独立 Agent 工作台架构与实施边界冻结 — DONE

依赖：`FR-17`。

目标：在不修改业务代码的前提下，冻结与 Flow、FlowRun、节点和 Attempt 完全解耦的一级“Agent 会话”
工作台技术方案。平台启动时必须主动预置并持续保活一个 Agent Workspace 专属 OpenHands Runtime；浏览器
进入页面不得触发容器创建。Workspace、OpenHands Conversation/Event、Bash Event、持久 Store、稳定
Secret 和会话 locator 必须位于容器外，物理容器只能作为可替换计算载体。方案还必须覆盖独立领域模型、
默认镜像 digest、Runtime generation/fencing、OpenHands 正式 API、外层导航和刷新恢复、错误状态、数据
删除边界、故障恢复、安全、可观测性、真实 E2E 以及后续最小实施切片。

验收：新增详细技术设计；固定 OpenHands `1.42.0` 源码契约与现有 FlowWeave 代码边界取证一致；方案明确
不以隐藏 FlowRun、共享 FlowRun fallback、平台消息副本或浏览器重置实现工作台；文档引用、任务状态唯一性
和 `git diff --check` 通过。本切片不修改数据库、API、Worker、Runtime Provider、Web 或部署行为。

### FR-19 Agent Workspace 持久化与预启动 Runtime — DONE

依赖：`FR-18`。

目标：新增一等 Agent Workspace、外置 allocation、稳定 Secret、Runtime Session/generation 模型和迁移；
Runtime Provider 显式支持 `AGENT_WORKSPACE` 持久 owner；Worker 在平台启动恢复阶段幂等创建默认 Workspace
并主动预置唯一 Runtime。浏览器访问不得触发 provision；物理容器 kill/delete 后外置数据不被清理，且
reconcile 使用同一 Workspace 启动受 fence 保护的新 generation。

### FR-20 独立 Conversation API 与 OpenHands 契约 — DONE

依赖：`FR-19`。

目标：新增独立 binding/command/locator 和 Workspace 嵌套 API，使用已测试的默认模型配置创建 OpenHands
正式 Conversation；实现 events、message、interrupt、resume、rename、delete、stream 和 Workspace
terminal。按固定源码把 `persistence_dir` 校验为
`/runtime/state/conversations/<conversation_id.hex>`，加入 create 幂等、消息不确定投递和非瞬态错误停止规则。

### FR-21 顶层 Agent 会话工作台 — DONE

依赖：`FR-20`。

目标：在最外层新增一级 `Agent 会话` Tab 和稳定 URL，完成会话列表、新建、对话、停止、文件/终端抽屉、
刷新恢复、Runtime 产品状态和 WS 断线补洞；独立页面不得出现 FlowRun、Node、Attempt、Environment
Version、generation、Runtime Session ID、容器 ID、事实边界或用户重置入口。保留现有 FlowRun 按节点显式
启动会话逻辑，不把独立工作台回接为 FlowRun fallback。

### FR-22 Agent 工作台故障恢复、安全与真实 E2E — DONE

依赖：`FR-21`。

目标：执行完整迁移、静态检查、测试、构建、Compose 安全、固定 OpenHands contract/smoke 和真实产品
E2E；证明平台启动前置 Runtime、双 Conversation 单容器、真实模型/Tool/terminal、浏览器刷新、物理容器
kill/delete、lease takeover、旧 writer 拒绝、无缓存重新部署和数据保留。全部通过后重新编译、打包和部署；
FR-22 完成前不扩展 Agent Workspace 与 Flow/节点/流程运行的集成。

### FR-23 Agent 工作台模型配置显式切换 — DONE

依赖：`FR-22`。

目标：在一级 Agent 工作台持续展示“新会话模型配置”选择器，列出“大模型配置”中连接测试成功且存在启用
默认模型的全部供应商。用户可显式切换或清空该配置；不得自动选用 Codex OAuth、最近创建项或任意隐藏
fallback。切换仅影响后续新建会话，已有 OpenHands Conversation 保持其创建时冻结的模型。补齐页面定向
Playwright 和 Web 质量检查；不新建 Runtime、不切换既有 Conversation LLM，也不改变 FlowRun 语义。

### FR-24 Agent 工作台 Codex 风格会话呈现基础 — DONE

依赖：`FR-23`。

目标：以 OpenHands 正式事件树为唯一内容来源，建立可复用的 Web 会话呈现基础，并首先接入一级 Agent
工作台。WebSocket `delta` 必须直接驱动临时可见正文，`message_complete` 后以正式 REST 事件回填；`TOOL_CALL`
与 `TOOL_RESULT` 必须以可折叠的工作过程卡片展示，空 `STATE` 等内部噪声不得占用消息流。不得展示或传输
供应商隐藏的原始推理；仅展示已由 OpenHands 事件安全投影的活动摘要。将“新会话模型配置”从标题栏迁入
左侧会话栏，仍只影响后续会话。不得修改 Runtime、OpenHands、FlowRun 或 Conversation 持久化契约。

### FR-25 Agent 工作台会话状态机与 Codex 交互收口 — DONE

依赖：`FR-24`。

目标：修复新建会话 URL 被旧缓存列表重定向覆盖的问题；将停止/继续收口为 OpenHands 原生 pause/run
控制，运行时允许将新输入暂存于浏览器队列并在当前轮完成后顺序投递，后端仍以当前正式 execution status
拒绝任何绕过队列的并发发送。最后一条用户消息始终可编辑并重新发送：服务端按正式 event id/parent_id
将 OpenHands HEAD navigate 到该消息之前，再发送编辑内容触发重新思考；旧分支保留在 OpenHands append-only
日志中但不再作为活动对话呈现。一级 Agent 工作台重做为 Codex 风格的简洁问答：用户消息仅右侧气泡，
Agent 正文直接渲染，无头像或身份标签；已安全投影的思考和工具活动在完成后归组折叠于最终回复上方、
执行中保持展开；输入区采用紧凑圆角 composer 和明确的发送/暂停/继续状态。不得持久化平台会话状态、
消息或推理，且不迁移 FlowRun 节点会话页面。

### FR-26 Agent 工作台真实附件、会话配置与上下文呈现 — DONE

依赖：`FR-25`。

目标：把 Agent 工作台输入区中的静态或误导性状态替换为固定 OpenHands 1.42.0 的正式能力。左侧 `+`
通过正式 Workspace file upload 路由把文件写入外置共享工作区，FlowWeave 固定目标路径、文件名、大小与
类型边界；图片同时按正式 `SendMessageRequest.content` 的 `ImageContent.image_urls` 传给模型，普通文件
以真实工作区路径作为消息上下文。会话内模型和思考强度调用正式 `/{conversation_id}/switch_llm`；强度
仅对模型配置明确声明支持的值开放。上下文信息只展示 OpenHands `ConversationInfo.stats` 与 LLM 已提供
的窗口容量，不得编造比例。原生 `CondensationRequest` 与 `Condensation` 事件在对话流以低干扰状态呈现，
且不由平台自行总结。最后一条消息的编辑/重跑继续只调用正式 `navigate` 与 `events`，前端改为
Codex/ChatGPT 风格的悬停编辑、无双边框自动增高输入区和会话内配置菜单。不得写入平台消息、事件、HEAD、
上下文或推理副本；不修改 OpenHands 或 FlowRun 语义。

### FR-27 Agent 工作台最新回复导航提示 — DONE

依赖：`FR-26`。

目标：在独立 Agent 工作台会话流中提供纯浏览器端的最新回复导航控件。Agent 正在生成时，控件显示低干扰的
动态三点提示；本轮结束后切换为向下箭头。两种状态点击均平滑定位到当前会话的最新内容，且用户上滚阅读历史
时不被每一个 WebSocket delta 强制拉回底部。该控件不得写入或重解释 OpenHands Conversation、事件、HEAD
或运行状态，也不改变 FlowRun 会话。

### FR-28 Agent 工作台原生分叉与手动压缩 — DONE

依赖：`FR-27`。

目标：在 Agent Workspace 的每个已完成回复下提供“从此处分叉会话”，使用固定 OpenHands 原生 fork API，
以用户选择的正式事件身份创建一个新的独立 Conversation 与最小 locator，并导航至它；在会话标题栏提供
手动“压缩上下文”，调用原生 condense，并继续从正式 Condensation 事件读取状态。Fork/condense 在会话
运行中必须 fail closed；分叉命令与绑定必须可幂等、可审计，且不复制消息、事件、HEAD 或 Runtime。不得
修改 FlowRun 会话或模拟 OpenHands 分支/摘要能力。

### FR-29 Agent 工作台会话供应商冻结与模型简化 — DONE

依赖：`FR-28`。

目标：新建 Agent Conversation 时冻结其已显式选择的模型供应商；会话内模型切换只能使用该供应商的已启用
模型，后端不得接受或执行跨供应商的 `switch_llm`。模型选择器仅显示模型名称，不泄露供应商名称、OAuth
账户或其他配置标识。历史会话若缺少可审计的供应商绑定，不猜测回填或静默改写 OpenHands 状态，而是保留
读取与继续能力并禁用模型切换；用户可新建会话使用明确供应商。分叉会话继承源会话的冻结供应商。

### FR-30 Agent 工作台根事件分支读取修复 — DONE

依赖：`FR-29`。

目标：修复 OpenHands 正式事件树以 `parent_id = "__root__"` 结束时，被 FlowWeave 误判为“活动分支不完整”
并返回 409 的问题。该修复只把 `__root__` 识别为树的合法终止符，不按时间或文本重建分支、不重发已接收
的用户消息，也不屏蔽 OpenHands 已返回的真实模型错误。页面应能读取并渲染正式 ERROR 事件，而不是显示空白
会话与误导性的“处理中”提示。

### FR-31 Agent 工作台实时事件中继与最新回复导航修复 — DONE

依赖：`FR-30`。

目标：修复 Runtime Provider 事件中继每次仅转发一个 OpenHands WebSocket 帧便退出、导致状态帧抢占 token
delta 或完成消息，从而出现“发送下一条才看到上一条回复”的问题。中继必须在同一已授权连接中持续转发正式
OpenHands 事件，直到客户端断开或上游关闭；FlowWeave 仍只安全投影可见文本 delta 和完成通知，不持久化或
泄露原始推理。Agent 工作台的“跳到最新回复”控件仅在用户离开会话底部时显示；生成中显示动态提示，完成后
显示向下箭头，处于底部时不显示。不得修改 OpenHands 源码、模拟流式输出或改写 Conversation/Event/HEAD。

### FR-32 Agent 工作台失败可见性、延迟换模与上下文容量 — DONE

依赖：`FR-31`。

目标：保留并显示 OpenHands 正式 `ConversationErrorEvent` 的安全错误详情，避免上游模型调用失败后页面仅
显示用户消息；模型和思考强度选择在浏览器中保持为“下次发送生效”，不得因选择控件变化立即调用
`switch_llm`。下一条消息发出前才通过该会话创建时冻结的供应商调用正式 OpenHands `switch_llm`，随后创建
同一条正式 user event。上下文区域显示 OpenHands 持久累计 token 使用与已声明的窗口容量；只对固定模型
目录已明确声明的容量显示数值，其他模型保持“容量未返回”，不得伪造实时窗口占用百分比。

### FR-33 Agent 工作台实时过程投影与友好工具呈现 — DONE

依赖：`FR-32`。

目标：修复发送后 OpenHands 尚未切入执行态时，浏览器以短暂 `ready` 响应错误结束本轮渲染，导致正式事件只能
在刷新后才显示的问题。会话必须持续处于生成态，直到收到同一轮正式 assistant/error 终止事件；暂停路径仍以
原生 readiness 确认。实时通道除安全可见正文 delta 外，还要在浏览器内存中转发并合并 OpenHands 已安全投影的
Thought、Tool Action/Observation、Condensation 与错误事件，REST 事件继续是刷新恢复的唯一事实源。用户界面不
显示 `TerminalAction`、`FileEditorAction` 或原始 JSON，而是按正式事件语义显示简洁的分析、查看/编辑文件、运行
命令、浏览器操作、Skill/MCP 调用及完成状态；运行中展开、完成后折叠。不得保存前端临时事件、伪造思考内容、
传输供应商隐藏推理或修改 OpenHands 源码/FlowRun 契约。

### FR-34 Agent 工作台可靠排队发送 — DONE

依赖：`FR-33`。

目标：当用户在当前轮回复尚未完成时发送下一条消息，浏览器必须保留原始文本、附件与下次发送生效的模型选择，
显示为排队并在 OpenHands 原生会话可接收输入后按顺序投递；不能显示“Agent 正在处理”错误。即使浏览器的实时
状态与 Runtime 短暂不同、首次投递收到正式 `AGENT_CONVERSATION_BUSY`，也要转为本地排队并从 readiness 恢复，
而非丢弃输入或向用户报错。当前轮完成必须关联到本轮正式 user event，不能因历史已完成回复提前清空运行状态。
队列仅存在于当前浏览器会话；刷新前未投递项明确丢弃，不写入 FlowWeave 或 OpenHands。不得改写 OpenHands
并发输入约束、伪造已发送消息或修改 FlowRun。

### FR-35 Agent 工作台会话绑定与终端可靠性 — DONE

依赖：`FR-34`。

目标：会话流按 OpenHands 正式 `parent_id` 拓扑渲染，确保 user event 先于其所有后代 assistant/activity
呈现；最后一条 user 消息的编辑入口为气泡外独立图标。每个 Conversation 明确显示其当前生效的模型供应商，
新建会话在创建时显式选择供应商，不再写入或依赖 Workspace 全局默认配置；已有 Conversation 可以在下一次
发送时原生切换到任一已测试成功的供应商及其启用模型，成功后更新该会话的受审计绑定。模型/思考选择器只显示
当前有效值一次，不制造重复的空占位选项。修复 Agent Workspace 常驻 Runtime 终端被误按 Environment Setup
容器校验、导致 Runtime Provider 返回 422 的问题；终端必须使用同一受 owner/fence 校验的 `agent-runtime`
容器连接，不得把容器地址、Docker socket 或未校验的资源暴露给浏览器。不得修改 OpenHands 源码、持久化消息、
事件、HEAD 或另建按会话容器。

### FR-36 Agent 会话免确认默认值、自动压缩与单一动作按钮 — DONE

依赖：`FR-35`。

目标：顶层 Agent Conversation 创建时显式使用 OpenHands `NeverConfirm` 与
`LLMSummarizingCondenser`，避免 `pwd` 等工具动作因 FlowWeave 内部默认值进入不可见的确认等待；历史显式确认
配置仍可通过 OpenHands 原生批次确认完成处理。平台统一覆盖客户端或 Agent Profile 的确认选择，所有新保存流程
节点均持久化免确认；新建节点默认 LLM 摘要压缩，并迁移既有可变节点配置。不可变 Snapshot 与 Attempt 不回写。
Agent 工作台在模型尚未返回首个文本或工具进度时展示可计时等待状态，
并将发送、暂停、继续收敛为输入框右下角同一个状态按钮；不得扩大 Runtime 隔离边界或授予宿主机权限。

### FR-37 Agent 会话工作过程与最终回复分层呈现 — DONE

依赖：`FR-36`。

目标：顶层 Agent 工作台每轮固定按“用户消息 → 可折叠工作过程 → 最终回复或失败结果”呈现。安全投影的
流式文本、Thought、Tool Action/Observation、Condensation 与错误均属于工作过程；运行中保持展开并显示
实时耗时，正式 assistant Message 或错误到达后自动折叠并在其下方显示唯一终态。REST 与实时投影携带
OpenHands 正式事件 timestamp，使刷新后仍可按 user 到 assistant/error 的墙钟时间恢复耗时；缺失或非法时间
不猜测。不得持久化消息、事件或耗时，不得泄露隐藏推理、修改 OpenHands 源码或扩展 FlowRun/Runtime 边界。

验收：OpenHands 事件投影定向测试、Web lint/typecheck/build、Agent 工作台定向 Playwright、
`git diff --check`、Alembic head 与任务状态唯一性通过；本切片使用独立 Git commit。

### FR-38 Agent 会话 UTC 计时修正 — DONE

依赖：`FR-37`。

目标：按固定 OpenHands `1.42.0` 在 UTC Runtime 中生成的无时区 ISO-8601 Event timestamp 正确计算
运行中耗时和模型等待时间，避免浏览器按本地时区解释后叠加 UTC offset；显式携带 `Z` 或 offset 的正式
时间保持原语义。等待详情与工作过程摘要统一使用格式化时长。不得修改 OpenHands、事件原值、持久化或
Runtime 边界。另取证实际停滞会话的正式事件和受保护日志，区分前端计时错误与模型供应商不可达。

验收：Web lint/typecheck/build、非 UTC 浏览器时区下的 Agent 工作台定向 Playwright、部署后真实页面检查、
`git diff --check`、Alembic head 与任务状态唯一性通过；本切片使用独立 Git commit。

### FR-39 Agent 会话 Codex 式输入区与上下文进度 — DONE

依赖：`FR-38`。

目标：将顶层 Agent 工作台输入区收口为紧凑的 Codex 式圆角 composer；会话供应商、模型和推理强度使用同一
轻量浮层，且所有选择继续只在下一条正式消息发送时生效。OpenHands stats 必须仅按当前 LLM `usage_id`
匹配正式 bucket，读取 `per_turn_token` 与 `context_window`，不得混入 condenser 或子任务用量。只有这两个
正数同时存在时，输入区才显示对应进度图标和紧凑 `k`/`m` 数值；读取中、缺失、非法或只有累计 token 的统计
一律不渲染上下文占位。不得估算 token、持久化浏览器状态、修改 OpenHands、Runtime、FlowRun 或 API 契约。

验收：Agent 工作台定向 Playwright 覆盖可用和缺失上下文、模型浮层与发送；Web lint/typecheck/build、
`git diff --check`、Alembic head 与任务状态唯一性通过；本切片使用独立 Git commit。

### FR-40 Agent 会话 Codex OAuth 流式切换与 IME 发送保护 — DONE

依赖：`FR-39`。

目标：所有顶层 Agent Conversation 从首个供应商开始即通过 OpenHands 正式 `LLM.stream` 启用 token callback，
使 Event Service 在创建时绑定流式回调，后续原生 `switch_llm` 到 Codex OAuth 等只接受流式请求的供应商时
不会因缺少 callback 被降级为非流式；不得将 FlowWeave 托管的 OAuth 凭据复制到 OpenHands 自有凭据存储或
修改 OpenHands。Agent 工作台发送框在中文等 IME 组合输入期间必须忽略 Enter，只允许组合完成后的独立
Enter 发送；Shift+Enter 仍换行。

验收：OpenHands 适配器定向测试覆盖初始 LLM 与 Codex OAuth 切换的正式 stream 配置；Agent 工作台定向
Playwright 覆盖 IME 确认候选不发送和后续独立 Enter 正常发送；Web lint/typecheck/build、平台定向测试、
部署后真实配置与健康检查、`git diff --check`、Alembic head及任务状态唯一性通过；本切片使用独立 Git commit。

### FR-41 Agent 会话消息复制与选区隔离 — DONE

依赖：`FR-40`。

目标：在顶层 Agent Workspace 的用户消息气泡旁增加快速复制按钮；浏览器原生复制时，如选区从该用户消息
开始却意外跨入后续工作过程或回复，剪贴板内容必须收敛为该用户消息正文。不得改变正式 OpenHands event、
回复内容、持久化、Runtime 或 FlowRun 边界；不影响助手回复、代码块和工作过程的正常浏览器复制。

验收：Agent 工作台定向 Playwright 覆盖快速复制、选区跨越时的剪贴板隔离与普通回复复制不受影响；Web
lint/typecheck/build、`git diff --check`、Alembic head 与任务状态唯一性通过；本切片使用独立 Git commit。

### FR-42 Agent 会话正式终态、过程输出与供应商一致性 — DONE

依赖：`FR-41`。

目标：按固定 OpenHands `1.42.0` 正式契约同时识别 assistant `MessageEvent` 与
`FinishAction.message` 两种 Agent 最终回复；实时流在正式 Finish 生命周期到达后结束当前轮，刷新后仍从
同一正式事件恢复唯一最终回复。工具 `ActionEvent.thought` 中的安全可见模型文本必须进入工作过程，
`TaskTrackerAction/Observation` 按正式 `command=view/plan` 显示具体“任务跟踪”动作，其他工具不得退化为
无法识别的泛化文案。由于 OpenHands 正式 `switch_llm` 不持久化替换 LLM，每次发送正式 user event 前必须
按 Conversation binding 重新应用当前供应商/模型；失败时阻止发送并明确报错，不得让 Runtime reload 后
静默回退到创建会话时的旧供应商。通过 OpenHands 正式 LLM 重试与超时字段缩短额度耗尽、网关不可达等
失败形成正式终态的等待时间。不得展示隐藏 reasoning、修改 OpenHands、持久化消息/事件或新增数据库字段。

验收：固定 OpenHands `FinishAction`、response dispatch 与 `switch_llm` 源码取证；OpenHands 适配器和
Agent Workspace 定向 pytest；Agent 工作台定向 Playwright 覆盖过程 commentary、任务跟踪名称、
Finish 最终回复与完成态；定向测试覆盖正式 LLM 重试配置；Web lint/typecheck/production build、部署后真实
会话探针、`git diff --check`、Alembic head 与任务状态唯一性通过；本切片使用独立 Git commit。

### FR-43 历史会话流式模型切换迁移 — DONE

依赖：`FR-42`。

目标：修复创建时未绑定 token callback 的历史 Agent Conversation 在切换 Codex OAuth 等强制流式模型后
仍产生 `Stream must be set to true` 的问题。Conversation binding 必须记录创建时是否具备正式流式回调；
历史值不得猜测为可用。历史会话下一次发送前，平台使用 OpenHands 正式 `switch_llm` 设置目标 LLM，再以
当前正式 HEAD 原生 fork 完整活动事件分支，使新 Event Service 从 `stream=true` Agent 绑定 token callback；
Web 自动切换到新 binding URL 后只发送一次原用户消息。服务端必须拒绝绕过迁移直接写入历史 binding，
迁移失败不得向源会话追加 user event，不得复制消息到 FlowWeave、修改 OpenHands 或按错误文本猜测能力。

验收：新增迁移 upgrade/downgrade；Agent Workspace/OpenHands 定向测试覆盖历史标记、正式 switch→fork 顺序、
事件/HEAD/provider identity 与失败不发送；Agent 工作台 Playwright 覆盖发送时自动迁移、URL 切换和消息只发
一次；平台 Ruff/Pyright/pytest、Web lint/typecheck/build、真实 Maven 会话迁移探针、Compose 重建部署、
`git diff --check`、Alembic head 与任务状态唯一性通过；本切片使用独立 Git commit。

### FR-44 Agent Workspace 持久化项目根目录 — DONE

依赖：`FR-43`。

目标：将独立 Agent Workspace 的项目根目录明确为唯一持久化的用户项目边界。新建会话通过 OpenHands
正式 `agent_context.system_message_suffix` 告知 Agent：所有应保留的代码、配置、文档和用户产物必须写入
`/runtime/workspace/project` 或其自行创建的需求/功能子目录；不得向用户暴露宿主机或 Docker 挂载细节。
Agent 工作台共享终端在本地和 Runtime Provider 远程控制器两条路径中均以该目录作为初始工作目录；用户仍可在
项目根内创建任意目录或自行切换目录。不得把这一产品约束伪装成 OpenHands 私有协议、限制用户正常终端操作，
或将 HOME/凭据卷作为项目文件位置。

验收：Agent Workspace 创建请求、终端本地执行和 Runtime Provider 远程终端的定向测试覆盖固定项目根目录；
受影响 Python 格式/类型检查、`git diff --check`、Alembic head 与任务状态唯一性通过；本切片使用独立 Git commit。

## 7. 恢复工作检查表

每次开始新切片必须依次检查：

1. 完整读取 `docs/flowrun-openhands-runtime-design.md` 和本文。
2. 检查 `git status --short --branch`、未提交 diff 和当前 Alembic heads。
3. 检查本文是否只有一个 `CURRENT`，以及其依赖是否全部 `DONE`。
4. 读取当前切片涉及的 FlowWeave 源码、迁移和测试。
5. 只在该切片需要时，读取固定 OpenHands commit 源码或运行最小镜像探针。
6. 完成实现、基础语法检查、`git diff --check` 和本文状态更新。
7. 复核并提交当前切片的代码和文档；确认提交成功后停止，不自动开始下一切片。

## 8. 验证日志

| 日期 | 切片 | 验证 | 结果 |
|---|---|---|---|
| 2026-08-27 | FR-44 | Agent Workspace、Environment terminal 与 Runtime Provider controller 定向 pytest（80 passed）；Ruff format/check；Pyright（0 errors）；实际 Agent Runtime 项目目录可写探针；API 与 Runtime Provider 重建替换及健康检查；Alembic head、任务状态唯一性与 `git diff --check` | PASS：独立 Agent Workspace 的新 Conversation 通过 OpenHands 正式 `agent_context.system_message_suffix` 将 `/runtime/workspace/project` 定义为对用户透明的逻辑项目根。Agent 被要求将应保留的代码、配置、文档和用户产物保存于该目录或其自行创建的需求/功能子目录，不暴露宿主机与 Docker 细节。Agent 工作台终端无论通过 API 本地 Docker 调用还是 Runtime Provider 远程控制器，均在创建时以该目录作为初始工作目录；用户后续自行进入项目子目录的终端行为不受影响。API 和 Runtime Provider 已用新镜像替换并返回健康。未改变 OpenHands、FlowRun、宿主机挂载、HOME/凭据卷或数据库契约。 |
| 2026-08-27 | FR-43 | 固定 OpenHands `1.42.0` Event Service、`switch_llm` 与原生 fork 契约取证；Agent Workspace 定向 pytest（13 passed）与平台全量 pytest（452 passed）；Ruff/Pyright；OpenAPI；PostgreSQL 完整迁移矩阵；Web lint/typecheck/production build；源码与部署后 Agent 工作台定向 Playwright（各 1 passed）；Compose 安全、镜像重建部署、真实 Maven 历史会话迁移与独立执行探针；HTTP、API health、Alembic head、任务状态唯一性与 `git diff --check` | PASS：新增 `streaming_callback_ready` 明确区分历史 Event Service，历史 binding 直接写入 fail-closed；发送前使用当前供应商/目录模型/推理强度执行正式 `switch_llm → fork`，Web 先切换新 binding URL 后只发送一次，失败恢复输入且不向源会话追加 user event。手动 fork 继承源能力标记，新会话直接标记可流式；OpenHands context 的 `openai/<model>` 规范名会映射回同供应商已启用目录项。真实 Maven fork 保留全部 57 个正式事件、HEAD 终态和供应商身份；独立 fork 的流式发送越过原 `Stream must be set to true`，约 7 秒形成供应商正式 `LLMRateLimitError`，两个临时探针均已删除且原 Maven 事件树未改写。完整无缓存重建在未修改的 OpenHands Runtime 阶段被 TUNA Debian 镜像 403 中断；本切片涉及的 migration/API/worker/Web 随后全部无缓存重建并部署，固定 Runtime 镜像保持不变。API、Postgres、Runtime Provider、Worker 与 Web 健康，Web 返回 200，唯一 head/current 为 `0064_agent_streaming_callback`；无 `CURRENT`、`READY` 或下一切片。 |
| 2026-08-27 | FR-42 | 固定 OpenHands `1.42.0` Finish、Event Service streaming、`switch_llm` 与 LLM retry 正式契约取证；OpenHands/Agent Workspace 定向 pytest（74 passed）与平台全量 pytest（451 passed）；Ruff/Pyright；Web lint/typecheck/production build；Agent 工作台定向 Playwright（1 passed）；Compose 镜像重建部署、真实原会话恢复与两个临时新会话探针；HTTP、API health、Alembic head、任务状态唯一性与 `git diff --check` | PASS：assistant `MessageEvent` 与 `FinishAction.message` 均成为工作过程下方的唯一最终回复，实时 Finish 正确结束本轮且 `FinishObservation` 不重复；安全可见 `ActionEvent.thought` 进入过程 commentary，TaskTracker 按 `view/plan` 显示具体动作，其他工具显示可识别名称，隐藏 reasoning 字段继续剔除。发送前以正式 `agent.llm.usage_id` 对账 binding，Runtime reload 后供应商漂移会先重绑定，失败则不发送 user event。正式 LLM 配置收紧为 2 次尝试、2–4 秒退避和 60 秒单次超时；额度耗尽探针立即形成 `LLMRateLimitError`，不再按 SDK 默认策略长时间无事件等待。原 Maven binding `0b333d3a-d7bf-4b8e-8831-eac0c48f6bc6` 按原 OpenHands Conversation 恢复，实际供应商与 binding 均为 `1476d1a7-fef5-44b2-9035-653e3eb75cf3`，历史 Finish 回复和最新正式错误终态均可读取。两个临时探针已删除。无缓存平台构建被 Debian 软件源安装错误中止，随后使用相同锁定依赖缓存成功重打包并部署；API、Postgres、Runtime Provider、Worker 与 Web 健康，Web 返回 200，唯一 head 为 `0063_autonomous_defaults`。未修改 OpenHands、数据库迁移、消息持久化或 FlowRun 边界；无 `CURRENT`、`READY` 或下一切片。 |
| 2026-08-27 | FR-41 | Web lint/typecheck/production build；独立源码 Vite 上 Agent 工作台定向 Playwright（1 passed，覆盖快速复制、用户消息到后续回复的跨选区复制、助手回复正常复制）；Web Docker 镜像重建与替换；HTTP、API readiness、Alembic head、任务状态唯一性与 `git diff --check` | PASS：用户消息气泡的悬浮操作区增加快速复制按钮并以短暂勾选反馈成功。浏览器原生 copy 如 anchor 位于用户消息正文且 range 跨到后续工作过程或回复，剪贴板被限制为该用户 event 的正文；若 range 完全在用户消息中则保留实际选区，助手回复和其他内容不会被该处理器拦截。未修改 OpenHands event、平台持久化、Runtime 或 FlowRun 边界。Web 已重新构建部署，返回 200，API readiness 通过；唯一 Alembic head 为 `0063_autonomous_defaults`，无 `CURRENT`、`READY` 或下一切片。 |
| 2026-08-27 | FR-40 | 固定 OpenHands `1.42.0` Event Service、`switch_llm` 与 SDK streaming fallback 源码取证；OpenHands 适配器定向 pytest（62 passed）及 Ruff；Web lint/typecheck/production build；独立源码 Vite 上 Agent 工作台定向 Playwright（1 passed）；共享平台与 Web 镜像重建部署；Compose、HTTP、Runtime generation、原 Conversation/event identity、Alembic head、任务状态唯一性与 `git diff --check` | PASS：报错由会话从普通供应商切换到强制流式的 Codex OAuth 触发，根因是该历史 Conversation 首个 LLM 为 `stream=false`，Event Service 创建时未绑定 token callback；后续 `switch_llm(stream=true)` 因没有 callback 被 SDK 降级为非流式，Codex 端点因此拒绝请求。所有新顶层 Conversation 从首个供应商开始即使用正式 `stream=true`，后续供应商切换复用已绑定回调；FlowWeave 仍自行托管 OAuth 凭据，未伪装成 OpenHands subscription。Composer 在 IME composition 或 `keyCode=229` 时忽略 Enter，候选确认不再发送，组合结束后的独立 Enter 正常发送。部署后 API/Runtime Provider 健康、Web 返回 200，Agent Runtime 恢复为 generation 10，原会话与正式事件均按原 ID 读回。固定版本的 `switch_llm` 不持久化新 LLM 且不重建 token callback，因此未篡改旧 Conversation 的 OpenHands state；该修复适用于新建 Conversation，既有 `stream=false` 会话切换 Codex 需新建会话。唯一 Alembic head 为 `0063_autonomous_defaults`；无 `CURRENT`、`READY` 或下一切片。 |
| 2026-08-27 | FR-39 | OpenHands 适配器定向 pytest（62 passed）；Web lint/typecheck/production build；独立源码 Vite 上 Agent 工作台定向 Playwright（1 passed）；迁移 head、任务状态唯一性、`git diff --check`；Compose 共享平台与 Web 重建部署、健康检查和真实 `/context` API | PASS：Composer 的模型设置改为轻量浮层；无正式当前上下文数据时不渲染占位，数据存在时显示环形图。适配器只从活跃 LLM `usage_id` 的正式 OpenHands bucket 读取 `per_turn_token/context_window`，不会把 condenser 的用量混入当前上下文。真实 `openai/gpt-5.6-luna` 会话恢复后返回 `used_tokens=6380`、`window_tokens=922000`，页面将显示 `6.4k / 922k`；922k 是供应商报告的真实窗口，不会伪造成 256k 或 1m。无缓存构建曾三次被 Debian 临时 502 阻断，随后使用同一锁定依赖的本地构建缓存成功生成新代码镜像并部署；API、Postgres、Runtime Provider、Worker 与 Web 健康，唯一 Alembic head 仍为 `0063_autonomous_defaults`。未修改 OpenHands、数据库迁移、持久化或 FlowRun/Runtime 边界。无 `CURRENT`、`READY` 或下一切片。 |
| 2026-08-27 | FR-38 | Web lint/typecheck/production build；Asia/Shanghai 浏览器时区下使用 OpenHands 无时区 timestamp 的 Agent 工作台定向 Playwright（1 passed）；部署后真实故障会话页面、Compose 健康、运行镜像、Alembic head、任务状态唯一性与 `git diff --check` | PASS：OpenHands `1.42.0` 在 UTC Runtime 中生成的无时区 ISO-8601 timestamp 仅在耗时计算边界按 UTC 解释，显式 `Z`/offset 保持原语义；运行中耗时不再叠加浏览器 8 小时时区偏移，等待详情统一显示格式化时长。真实“你是谁”会话恢复为正式 user 到 error 的 `15分钟9秒`，OpenHands 五次重试后因供应商 `kiro-go行情号池` 的模型端点 `192.168.91.58:6699` 不可达而终止为 `LLMServiceUnavailableError`，期间没有产生隐藏分析或最终回复。Web 镜像已重建并重新部署，运行镜像为 `sha256:989d28e63a3a90bfd3f261491b394248a9b3f53dee674101eb3a24c4d9778080`；API、Postgres、Runtime Provider、Worker 与 Web 已恢复，健康检查通过。唯一 head 仍为 `0063_autonomous_defaults`；未修改 OpenHands、Runtime、数据库迁移或持久化边界。无 `CURRENT`、`READY` 或下一切片。 |
| 2026-08-26 | FR-37 | OpenHands 全量适配器测试（61 passed）与定向时间戳投影回归（4 passed）；Ruff/Pyright；Web lint/typecheck/production build；隔离源码 Agent 工作台 Playwright（1 passed）；Alembic head、任务状态唯一性与 `git diff --check` | PASS：REST 与实时安全投影保留 OpenHands 正式 timestamp；每轮固定按用户消息、工作过程、最终回复或失败结果排列。运行中过程区展开、实时计时并承载可见 delta/Thought/工具/压缩/错误，正式 assistant Message 到达后只在过程区下方显示一次最终回复；完成轮次按正式 user 到 assistant/error 的墙钟时间自动折叠并显示耗时，直接回复不生成空白详情。唯一 head 仍为 `0063_autonomous_defaults`；未修改 OpenHands、数据库迁移、FlowRun 或 Runtime 边界。无 `CURRENT`、`READY` 或下一切片。 |
| 2026-08-26 | FR-36 | 平台全量 pytest（449 passed）与定向回归（173 passed）；Ruff/Pyright；Web lint/typecheck/build；OpenAPI 基线；0063 PostgreSQL downgrade/upgrade；部署后 Agent 工作台与节点编辑器 Playwright；Compose 服务健康与真实 Agent Runtime 探针；`git diff --check` | PASS：新建 Agent Conversation 显式使用 OpenHands `NeverConfirm` 和原生 `LLMSummarizingCondenser`；平台统一把新保存节点冻结为免确认，29 个既有可变节点迁移为 `NEVER + LLM_SUMMARIZING`，2 个历史 Attempt 与 2 个 Snapshot 摘要往返不变。发送、暂停、继续及等待确认收口为同一按钮；无首个文本或工具事件时显示可计时等待状态，历史确认批次仍可原生处理。API、Postgres、Runtime Provider、Worker 与 Web 已重新部署，唯一 head 为 `0063_autonomous_defaults`。真实 `pwd` 探针全程 `pending=false`，模型在产生 Tool Action 前因 OpenAI Plus `usage_limit_reached` 终止，因此未执行命令；该外部额度限制与确认策略无关，验收会话已删除。Runtime 白名单、容器、网络、宿主机及 Docker 权限边界未放宽。无 `CURRENT`、`READY` 或下一切片。 |
| 2026-08-26 | FR-35 | Agent Workspace 与 Runtime Provider 定向 pytest（46 passed）；Web lint/typecheck/production build；隔离本地源码 Playwright；`git diff --check` 与任务状态核对 | PASS：会话流按正式 `parent_id` 稳定拓扑排序，实时与 REST 合并时 parent 必定先于后代渲染；最后 user 消息的编辑入口为气泡外图标。新建会话显式冻结所选已连接供应商；会话内供应商、模型和思考程度只在下一条原生消息发送边界切换，OpenHands `switch_model` 成功后才更新该会话绑定，不再依赖 Workspace 全局默认模型。选择器不重复展示当前模型或思考程度。常驻 Agent Runtime 终端在本地与 Runtime Provider 远程控制器路径均按 `agent-runtime` kind、owner 和 scope 校验后打开，不再要求 Environment Setup 的 `environment_id`，从而消除 422。 |
| 2026-08-26 | FR-34 | Agent 工作台定向 Playwright（本地源码 Vite）；Web typecheck/lint；`git diff --check` 与任务状态核对 | PASS：运行中输入保留文本、附件、模型和思考选择并在浏览器内排队；若原生接口因短暂状态差异返回 `AGENT_CONVERSATION_BUSY`，输入改入队而非显示错误。队列通过正式 input-readiness 顺序投递，刷新前未投递内容不持久化。当前轮只会在正式事件树中属于该 user event 后代的 assistant/error 终态结束，历史回复或无关联的完成帧不会使新轮提前结束。 |
| 2026-08-26 | FR-33 | OpenHands 实时流定向 pytest（3 passed）；受影响 Python Ruff；Web typecheck/lint；`git diff --check` 与任务状态核对 | PASS：浏览器不再在刚发送消息时依据短暂的 native `ready` 提前结束本轮；只有正式 assistant/error 终止事件或 REST 读回的同轮终态才结束。实时流连续投影安全 delta、Thought、Tool Action/Observation、Condensation、错误和完成事件，仅存在浏览器内存，刷新仍从 REST/OpenHands 读取。Agent 工作台把 Terminal/File Editor/Browser/Skill/MCP/Task 正式事件展示为可读动作、必要命令或工作区路径，不展示 OpenHands 类名或原始 JSON；运行中展开、完成后折叠。无 CURRENT、READY 或下一切片。 |
| 2026-08-26 | FR-32 | Agent Workspace/OpenHands 定向 pytest（5 passed）；受影响 Python Ruff；Web typecheck/lint；`git diff --check` | PASS：`ConversationErrorEvent.code/detail/classification` 由 OpenHands 正式事件安全投影，工作台将 rate limit 呈现为可操作的失败卡片，不再把失败轮次伪装成无回复。模型和思考程度不再有“应用”按钮或即时网络写入；它们随下次 `messages` 请求原生切换并发送。已为固定 Codex `openai/gpt-5.6-sol` 目录声明 922,000-token 窗口，累计 usage 与未知容量的语义分别清晰呈现，不估算当前 View 占用。 |
| 2026-08-26 | FR-31 | Runtime Provider 定向事件流测试；Web typecheck/lint；`git diff --check` | PASS：Provider 保持同一条经所有权校验的 OpenHands WebSocket，连续转发状态、delta 与完成帧，不再在首个状态帧后断开；Agent 工作台仅从正式可见 delta 渲染流式文本。最新回复控件仅在用户离开底部时出现；完成后为向下箭头。模型/思考程度应用动作改用勾选语义，当前窗口上下文提供真实统计浮层；未知窗口上限明确不估算。 |
| 2026-08-21 | FR-00 | 固定 OpenHands 源码取证；`git status`；`alembic heads`；任务状态、引用和 whitespace 检查 | PASS：新架构与独立任务主线已冻结；分支 `feat/refactor`；唯一 head `0051_physical_delete`；无 `CURRENT`，仅 FR-01 为 `READY` |
| 2026-08-21 | 全阶段验证策略 | 文档规则一致性与 `git diff --check` | PASS：FR-01–FR-11 仅做受影响代码语法/解析/编译检查；所有业务、迁移、协议、安全、恢复、容器和 E2E 验证统一推迟到 FR-12 |
| 2026-08-21 | FR-01 | 受影响 Python `py_compile`；受影响 Web 文件定向 TypeScript 编译；Compose YAML 解析；`alembic heads`；任务状态唯一性；`git diff --check` | PASS：Flow 强制绑定用户创建的 READY Environment Version，Run/Snapshot 冻结同一版本；历史空绑定运行与会话入口 fail closed；用户 base image 与最终 Runtime Image 均按 digest 冻结，发布调用固定 OpenHands `docker.build` 并保存 provenance。静态 head 为 `0052_flow_environment`；未运行数据库、镜像构建、业务测试或服务/容器验证，统一留待 FR-12 |
| 2026-08-21 | FR-02 | 受影响 Python `py_compile`；Compose YAML 解析；`alembic heads`；任务状态唯一性；`git diff --check` | PASS：每个新 FlowRun 建立 scope 隔离、服务端推导且受权限/符号链接校验的 `workspace/project`、Conversation、Bash Event、Persistence 与只读 capability 外置目录；稳定 `OH_SECRET_KEY` 仅以加密 Secret Reference 持久化并在 Runtime 创建边界解密注入；FlowRun Runtime 不再把持久 Workspace 放入 tmpfs；创建失败回滚与受引用删除保护已实现。静态 head 为 `0053_runtime_allocation`；未运行数据库迁移、业务测试、Runtime、容器、安全或 E2E 验证，统一留待 FR-12 |
| 2026-08-21 | FR-03 | 受影响 Python `py_compile`；Compose YAML 解析；`alembic heads`；任务状态唯一性；`git diff --check` | PASS：主执行 Runtime owner 收敛为 `FLOW_RUN`，同一 Run 通过 owner 锁复用唯一 active 物理容器；Attempt/Conversation 不再创建、续租或删除自己的容器，旧 owner 只进入回收；验证与 OAuth 使用显式临时 Runtime owner；FlowRun Runtime 生命周期不受单次执行 TTL/取消控制，显式删除 Run 时才清理物理 Runtime 和受保护外置 allocation；特权 Compose 服务及入口已从 Sandbox Controller 更名为 Runtime Provider。静态 head 仍为 `0053_runtime_allocation`；未运行业务测试、服务、容器、安全、恢复或 E2E 验证，统一留待 FR-12 |
| 2026-08-22 | FR-04 | 受影响 Python 与迁移 `py_compile`；ORM metadata 导入；`alembic heads`；任务状态唯一性；`git diff --check` | PASS：每个新 FlowRun 建立 stable Runtime Session，冻结 Environment Version、Runtime Image digest 和外置 allocation；generation 按 Session 单调分配并保留物理实例审计，Session 复合外键是唯一 active-generation 真相；激活、失败和命令校验携带 generation、fence token 与双 row version 并以 CAS fail closed；Attempt/Conversation 清理不能删除 Run 级 Runtime，显式删除 Run 按物理 generation、Session、allocation 顺序解除引用。静态 head 为 `0054_runtime_sessions`；未运行数据库迁移、业务测试、Runtime、容器、安全、恢复或 E2E 验证，统一留待 FR-12 |
| 2026-08-22 | FR-05 | 受影响 Python 与迁移 `py_compile`；ORM metadata 导入；`alembic heads`；任务状态唯一性；`git diff --check` | PASS：新增 `flow_run_conversation_bindings` 最小 locator，以复合外键保证 Conversation 所属 FlowRun 与 Runtime Session 一致；自动、人工和原生 fork 的 OpenHands conversation ID 均绑定到同一 FlowRun Session；创建、消息、轮询、控制、流和终端连接每次从 locator 解析唯一 active Agent Server generation，不再把首次容器名作为路由真相；Attempt 仅保留无 FK 的 OpenHands conversation ID 执行引用。同 Run 多 Conversation 继续复用 FR-04 唯一 active generation。静态 head 为 `0055_conversation_bindings`；未运行数据库迁移、业务测试、Runtime、容器、安全、恢复或 E2E 验证，统一留待 FR-12 |
| 2026-08-22 | FR-06 | 固定 OpenHands commit 的 Config、Conversation catalog/lazy reload、Event route 与正式 Event 字段取证；受影响 Python `py_compile`；`alembic heads`；任务状态唯一性；`git diff --check` | PASS：FlowRun Runtime 继续以正式 `OH_WORKSPACE_PATH`/`OH_CONVERSATIONS_PATH`/`OH_BASH_EVENTS_DIR`/`OH_PERSISTENCE_DIR` 和稳定 `OH_SECRET_KEY` 绑定外置目录；Conversation create 显式禁用 worktree，所有读取校验原 UUID、`LocalWorkspace` 子路径和 OpenHands persistence 路径；新增仅存于内存的 original-ID reload 身份探针，可在 generation 间核对已存在的正式 `id/parent_id/action_id/tool_call_id`，即使 OpenHands 追加 crash-recovery 事件也不会把新 HEAD 当作旧事件的替代；丢失 cursor anchor、缺失/重复/合成事件身份和空会话伪恢复均 fail closed；Runtime contract 升级为 schema 2 并冻结 event-by-id 与 `worktree` 契约。静态 head 为 `0055_conversation_bindings`；未运行业务测试、真实 Runtime/进程重启、容器、恢复或 E2E 验证，统一留待 FR-12 |
| 2026-08-22 | FR-07 | 固定 OpenHands commit 的正式 pause route、Event Service 关闭/租约释放和默认 45 秒 lease 取证；受影响 Python 与迁移 `py_compile`；ORM metadata 导入；`alembic heads`；任务状态唯一性；`git diff --check` | PASS：新增持久 replacement lease 和唯一 N+1 目标，健康异常同事务冻结路由并投递 generation 级幂等任务；N+1 只做 Server health/source/package/capability 预热，旧 generation 先断开数据面、再调用正式 `POST /api/conversations/prepare-for-sandbox-pause`、最后停机并写入单调删除意图；异常停机等待默认 45 秒 lease takeover 窗口，原 conversation ID/正式事件身份 reload 探针通过后才 CAS 激活 N+1；旧 writer 未物理删除、空会话或身份漂移均不激活。Worker 重启复用同一 N+1，已提交激活的过期任务不会创建 N+2。静态 head 为 `0056_runtime_replacement`；未运行业务测试、迁移实跑、真实 Runtime/容器、故障恢复或 E2E 验证，统一留待 FR-12 |
| 2026-08-22 | FR-08 | 受影响 Python `py_compile`；Compose 配置解析；`alembic heads`；任务状态唯一性；`git diff --check` | PASS：删除 Compose 顶层共享 Agent Server、共享 state volume、依赖关系和 `OPENHANDS_BASE_URL` fallback；OpenHands 适配器对缺失 FlowRun generation 路由 fail closed，创建 Conversation 必须携带已发布 Environment Runtime allocation；Runtime Provider 以显式绝对宿主机根目录配对只读校验根目录，不再 inspect source container；旧 Attempt/Conversation owner 无法进入新建 Agent Runtime 路径，临时验证/OAuth Runtime 的短期 OpenHands state 与 FlowRun 外置持久状态明确分离；新增静态架构守卫阻止上述分支回归。静态 head 仍为 `0056_runtime_replacement`；未运行业务测试、迁移实跑、真实 Runtime/容器、安全、恢复或 E2E 验证，统一留待 FR-12 |
| 2026-08-22 | FR-09 | 受影响 Python 与迁移 `py_compile`；受影响模块导入；ORM metadata 导入；`alembic heads`；任务状态唯一性；`git diff --check` | PASS：活跃数据模型只保留 FlowRun Conversation locator 和不含 cursor 的独立 Runtime 审批审计；旧 `AgentConversation`/`AgentMessage`、AUTO/HUMAN_CREATED、平台状态机、消息/事件/cursor 与 Goal/Critic/Task/Condensation 投影整体转为只读归档，旧 Conversation worker task 停止恢复和派发；自动执行和人工提问均直接绑定或操作同一 FlowRun Runtime 中的 OpenHands 原生 conversation ID，Attempt 只保留该 ID 引用，不再持久化物理 Runtime 或 cursor；API 兼容路径读取的是实时 OpenHands 事件且不落库，审计仅保存 actor、digest 和长度等独立事实；新增静态架构守卫。静态 head 为 `0057_flow_run_conversations`；未运行数据库迁移、业务测试、OpenAPI、服务、真实 Runtime/容器、安全、恢复或 E2E 验证，统一留待 FR-12 |
| 2026-08-22 | FR-10 | 固定 OpenHands commit 的 `StartConversationRequest`、Plugin discovery、resolved Plugin persistence 与 Memory loader 取证；`alembic heads`；任务状态唯一性；`git diff --check` | BLOCKED：固定 `1.42.0` 没有正式 ambient Plugin 禁用/allowlist 字段，且会无条件加载不进入 resolved identity 的用户/项目 Plugin；当前 FlowWeave 依赖构建时源码补丁和私有请求字段才能 fail closed，不能在“不修改 OpenHands、只用正式字段”的约束下同时删除旁路并保持 Snapshot 隔离。另确认冻结 Memory 的现有物化目录未挂入 FlowRun generation，也不匹配正式 loader 路径。保留现有安全降级，未运行测试、镜像、Runtime 或 create/smoke；唯一静态 head 为 `0057_flow_run_conversations`，无 `CURRENT`/`READY`，FR-11/FR-12 继续等待。解锁需上游正式契约或另行授权并冻结 OpenHands fork。 |
| 2026-08-22 | FR-10 解锁与完成 | 固定 OpenHands Plugin/Memory loader 契约取证；受影响 Python `py_compile`；`alembic heads`；任务状态唯一性；`git diff --check` | PASS：用户明确允许 OpenHands 1.42.0 原生 HOME/项目 ambient Plugin 扫描，Snapshot 唯一性只约束 FlowWeave 显式绑定能力；删除了 OpenHands 源码补丁、私有 `load_ambient_plugins` 创建字段及 Runtime contract/Environment provenance 依赖，contract schema 升为 3 且新镜像要求无 source overlay。冻结 USER/PROJECT Memory 合并为 FlowRun capability 挂载中 digest-scoped 只读 bundle，每个工作目录仅暴露 OpenHands 正式 `<working_dir>/.openhands/memory/MEMORY.md` 入口，不再维护 Attempt/Conversation 私有 Memory mount 和清理状态机。唯一静态 head 为 `0057_flow_run_conversations`；未运行业务测试、镜像构建、Runtime 或 create/smoke，统一留待 FR-12。无 `CURRENT`/`BLOCKED`，仅 FR-11 为 `READY`。 |
| 2026-08-22 | FR-11 | 受影响 Python `py_compile`；受影响 Web 文件定向 TypeScript 编译；`alembic heads`；旧会话路由、领域角色、cursor 和物理 Runtime 字段静态扫描；任务状态唯一性；`git diff --check` | PASS：公开 Conversation API 收口为 FlowRun/locator 嵌套路由，REST/WebSocket/Terminal 每次经 FlowWeave 校验 locator 并解析 active generation；Web 统一展示会话、提问、回复以及 `RECONNECTING/DEGRADED`，移除 AUTO/HUMAN_CREATED、平台消息/cursor 和物理容器信息。新增脱敏 Runtime 健康、generation 审计、CAS replacement 与保留策略入口；永久删除继续只走 FlowRun 生命周期。缺失新 Runtime Session 的历史 Run 明确只读归档并要求显式重跑，不兼容迁移旧数据。唯一静态 head 仍为 `0057_flow_run_conversations`；未运行业务测试、OpenAPI、完整 Web 构建、真实 Runtime、迁移实跑、安全、恢复或 E2E 验证，统一留待 FR-12。无 `CURRENT`/`BLOCKED`，仅 FR-12 为 `READY`。 |
| 2026-08-24 | FR-12 | Ruff format/lint；Pyright strict；平台全量 pytest、OpenAPI 与架构边界；Web ESLint/typecheck/build 与完整 Playwright；PostgreSQL 迁移矩阵；Compose 安全与平台最终镜像；固定 OpenHands provenance/contract/真实 smoke；Sandbox smoke；replacement、恢复、fencing、故障隔离及路径/Secret/删除保护 | PASS：252 个 Python 文件格式一致，Pyright `0 errors`，平台 `425 passed`，Web 7 个 E2E 场景全部通过；空库、当前库、`0005_execution` 与含历史 Snapshot 的 `0028_condensation_commands` downgrade/upgrade 均到唯一 head `0057_flow_run_conversations`；固定源码 commit `f09e03eac772290feeb51b7d7390ffaefeca1a09`、source archive SHA-256 `a33dfae9a55732cfb6ffe0b7d5cf02b557a041bc82629df5c61459400d35c832`、四包 `1.42.0`、无源码 overlay 的镜像契约通过，真实 confirmation、condenser、Task 子 Agent 以及 Python/JavaScript Sandbox smoke 通过；N→N+1 原 ID/event identity reload、45 秒 lease takeover、Worker 重启复用、旧 writer 拒绝、网络故障与 identity drift 降级及跨 FlowRun 隔离通过；自定义 Environment/digest、只读 capability、稳定 Secret Reference、路径/权限/租户边界与引用删除保护通过。重构状态为 `COMPLETE`，无 `CURRENT`、`READY` 或下一切片。 |
| 2026-08-24 | FR-13 | 平台环境定向 pytest 与 OpenAPI 基线；Ruff/Pyright；Web lint/typecheck/build；定向 Playwright；Compose YAML；Alembic head；任务状态和 `git diff --check` | PASS：新建 Environment 仅接收名称和说明，多传 `base_image` 以 422 拒绝；平台 Setup 启动镜像可使用本地建造 tag，但在 Environment 创建时冻结实际内容 digest；公开 Environment 字典和 UI 不再展示该内部字段。31 个定向平台测试、1 个定向 E2E、Web 三项门禁及 Pyright `0 errors` 通过；唯一 Alembic head 仍为 `0057_flow_run_conversations`，无 `CURRENT` 或下一切片。 |
| 2026-08-24 | FR-14 | Runtime Provider 与 Environment 定向 pytest；Ruff format/lint；Pyright strict；Compose YAML；实际镜像重建、Provider 替换和 Environment 创建；Alembic head；任务状态和 `git diff --check` | PASS：`resolve-base-image` 控制接口接受平台配置的本地 Setup 镜像 tag，并继续拒绝非法路径引用；64 个定向测试通过，Pyright `0 errors`；实际 Compose 中 `flowweave-openhands-runtime:1` 成功冻结内容 digest 并创建 Environment `f5f796ab-2f5f-4dd9-87fe-1634d1136141`；Runtime Provider 健康，唯一 Alembic head 仍为 `0057_flow_run_conversations`，无 `CURRENT` 或下一切片。 |
| 2026-08-24 | FR-15 | 迁移 0058 downgrade/upgrade；平台全量与定向 pytest；Ruff/Pyright；OpenAPI/架构/PostgreSQL/Compose 安全契约；Web lint/typecheck/build；真实 Compose Runtime、首会话、节点执行、取消与删除定向 Playwright；Alembic head；任务状态和 `git diff --check` | PASS：Flow Definition 不再绑定环境，每个 Run 强制选择并冻结 READY Environment Version；空 Run 由 Worker 主体预置唯一 Runtime generation，API/Worker 仅按 scope 标签接入 Run 专属网络，API 不获得 Docker Socket 或 Worker 创建权限；首会话自动创建并进入会话页，人工新建、提问、取消、只读和永久删除恢复可用。平台全量 `431 passed`，新增 Controller 定向 `35 passed`，最终契约/集成 `14 passed`，Pyright `0 errors`；两条真实产品 E2E 通过。0058 实际往返成功，唯一 head `0058_run_environment`；重构恢复为 `COMPLETE`，无 `CURRENT` 或下一切片。 |
| 2026-08-25 | FR-16 | 平台全量 pytest；Ruff/Pyright；OpenAPI；Compose 安全；Web lint/typecheck/build；全量无缓存镜像重建与 Compose 部署；真实 Runtime 预置、按节点会话启动及取消只读定向 Playwright；服务健康；Alembic head | PASS：创建 FlowRun 后保持运行详情且 Conversation 列表为 0，Worker 静默预置绑定该 Run 持久 Workspace 的唯一 Runtime，连接状态进入 `READY`；选择节点并创建 Attempt 后，只有显式点击“启动节点会话”才创建 OpenHands Conversation 并进入会话页，不再提供脱离节点上下文的新建入口；取消流程立即展示 `CANCELLED` 并隐藏后续执行入口，服务端继续拒绝终态 Run 的新会话与新问题。平台全量 `432 passed`，Pyright `0 errors`，Web 与契约/安全门禁通过；两条真实产品 E2E 通过。全部平台与固定 OpenHands Runtime 镜像无缓存重建并部署成功，API、Postgres、Runtime Provider 健康，Web 返回 200；唯一 head/current 均为 `0058_run_environment`。重构状态为 `COMPLETE`，无 `CURRENT` 或下一切片。 |
| 2026-08-25 | FR-17 | Web ESLint/typecheck/build；真实部署后的“损坏与历史浏览器状态恢复”和“节点资产编辑与重复流程节点画布”定向 Playwright；Compose 服务健康；Alembic head/current；任务状态和 `git diff --check` | PASS：流程编排加载阶段不再因临时空节点资产数组触发 React Flow 的无限更新；历史/异常流程、端口、门禁和目录数据均在只读渲染边界容错。刷新只保留稳定顶层导航，过期的 Run/节点执行/会话上下文自动回到流程运行列表；全局异常页不再指向浏览器不兼容或提供“重置页面状态”。Web 镜像无缓存重建并替换成功，API 健康、Web 返回 200，唯一 Alembic head/current 均为 `0058_run_environment`；无 `CURRENT` 或下一切片。 |
| 2026-08-25 | FR-18 | 完整读取现有 Runtime 设计/进度；固定 OpenHands `1.42.0` Conversation、Event、persistence 和 pause 契约取证；现有 Agent 页面、Runtime 模型、Provider、Worker 与 Compose 边界审计；任务状态和 `git diff --check` | PASS：冻结独立 Agent Workspace、平台启动预置单 Runtime、外置 Workspace/OpenHands state/Secret、可替换 generation、独立 Conversation API、一级导航、刷新恢复、故障矩阵和真实 E2E；确认 OpenHands 正式 `persistence_dir` 为 `<base>/<conversation_id.hex>`，现有基础目录字符串相等校验是首次 events 409 的根因；本切片只修改技术设计与进度文档，无业务代码、迁移、构建或部署变更。无 `CURRENT`，仅 FR-19 为 `READY`。 |
| 2026-08-25 | FR-19 | Agent Workspace 与 Runtime Provider 定向 pytest（37 passed）；Sandbox 定向 pytest（51 passed）；0059 PostgreSQL downgrade/upgrade 往返；受影响 Python Ruff format/check 与 `py_compile`；Alembic head、任务状态和 `git diff --check` | PASS：新增独立的默认 Agent Workspace、稳定 Secret Reference 与外置 `workspace/state/capabilities` allocation；Worker 恢复阶段幂等创建并投递预置 Runtime，未暴露浏览器触发路径。Runtime Provider/Docker/reconcile 显式支持 `AGENT_WORKSPACE` 持久 owner；物理容器删除后 generation 审计保留，并以同一外置 allocation 启动单调递增的新 generation。迁移实际往返至唯一 head `0059_agent_workspace_runtime`；无 `CURRENT`，FR-20 为下一待实施切片。 |
| 2026-08-25 | FR-20 | 固定 OpenHands `1.42.0` create/rename/delete 契约取证；0059 downgrade/0060 upgrade 往返；Agent Workspace/OpenHands/Runtime contract 定向 pytest（69 passed）；Ruff、Pyright、受影响模块 `py_compile`、Alembic head、任务状态和 `git diff --check` | PASS：新增独立 Workspace 设置、产品化 Runtime 状态、Conversation binding/command/locator 与嵌套 REST/WS API；创建仅使用已测试成功的默认模型配置并以预分配 UUID、外置会话专属 persistence 路径完成幂等校验。消息传输不确定返回 `AGENT_MESSAGE_DELIVERY_AMBIGUOUS` 且不自动重发；rename/delete 走 OpenHands 正式 PATCH/DELETE，terminal 绑定共享 Workspace 的 active Runtime，不关联 FlowRun。适配器将 Agent Workspace 作为独立受管 Runtime 路由，并冻结包含 `conversation_id`、PATCH 与 DELETE 的专属契约，不改变既有 FlowRun Snapshot 合同；唯一 head 为 `0060_agent_conversations`，无 `CURRENT`，FR-21 为下一待实施切片。 |
| 2026-08-25 | FR-21 | Web ESLint/typecheck/build；独立 Vite 开发服务器上的定向 Playwright；任务状态和 `git diff --check` | PASS：新增一级 `Agent 会话` Tab 与 `/agent`、`/agent/conversations/:bindingId` 稳定 URL；刷新从 URL 与服务端 binding 恢复当前会话，不依赖 localStorage 深层 ID。工作台仅使用 Agent Workspace API，提供模型配置引导、会话列表/新建/重命名/删除、OpenHands 事件读取与流连接、消息发送/停止、恢复提示和共享 Workspace 终端抽屉；页面不展示 FlowRun、Node、Attempt、Environment、generation、Runtime Session、容器或重置入口。定向 Playwright 覆盖默认模型设置、直接新建会话和 URL 刷新恢复；无 `CURRENT`，FR-22 为下一待实施切片。 |
| 2026-08-25 | FR-22 | 全量 API Ruff/Pyright/pytest；Web lint/typecheck/build 与 10 个 Playwright 场景；迁移 0060 往返；Compose 安全；固定 OpenHands provenance/contract/smoke；Sandbox smoke；全无缓存 `make rebuild-deploy`；真实 Codex OAuth 会话、Runtime 替换后原会话 reload | PASS：Ruff/ Pyright 通过，迁移 head/current 均为 `0060_agent_conversations`，Web 和 Compose 门禁通过，固定 OpenHands `1.42.0` 契约与 Sandbox smoke 通过。Codex OAuth catalog 仅同步固定 OpenHands 可原生流式执行的模型；`model_canonical_name=openai/codex-auto-review` 保持 Responses 流式请求且避免 Codex 端点不支持的 `max_output_tokens`。无缓存全量重建部署后，默认 Agent Runtime 以新 generation 恢复外置 Workspace 与原 Conversation/Event，真实新会话精确返回 `FR22_FINAL_DEPLOY_OK`。所有服务健康；无 `CURRENT`、`READY` 或下一切片。 |
| 2026-08-25 | FR-23 | 独立 Vite 服务上的 Agent 工作台模型切换定向 Playwright；Web ESLint/typecheck/build；`git diff --check`；任务状态核对 | PASS：标题栏常驻“新会话模型配置”，仅显示已连接且有启用默认模型的供应商；用户可显式设置、切换或清空，未再隐式选择 Codex OAuth 或任何 fallback。定向浏览器测试覆盖从首个配置切换至第二个配置，并确认既有会话 URL 保持不变；不新建 Runtime、不重启或换模既有 Conversation。无 `CURRENT`、`READY` 或下一切片。 |
| 2026-08-26 | FR-24 | Web ESLint/typecheck/build；独立 Vite 上 Agent 工作台定向 Playwright；`git diff --check`、Alembic head 与任务状态核对 | PASS：新增可复用的会话呈现 Surface，正式 OpenHands 消息、工作过程和工具活动不再渲染为空 `STATE {}`；WebSocket `delta` 只存在于浏览器内存的临时可见回复，`message_complete` 后回读正式事件。模型配置移入左侧会话栏，仍只影响后续新会话。定向浏览器用例覆盖配置切换、工具活动、空状态过滤、回复呈现和 URL 刷新恢复；未修改 Runtime、OpenHands 或 FlowRun 持久化契约。唯一 head 为 `0060_agent_conversations`；无 `CURRENT`、`READY` 或下一切片。 |
| 2026-08-26 | FR-25 | Agent Workspace/OpenHands 定向 pytest（68 passed）；Ruff/Pyright；Web lint/typecheck/build；独立 Vite 上 Agent 工作台 Playwright；Alembic head、`git diff --check` 与任务状态核对 | PASS：新建 binding 先进入本地查询缓存再导航，避免旧列表竞态覆盖 URL。运行中的会话支持暂停、继续和浏览器内顺序排队；服务端以 OpenHands 正式 execution status 阻止并发 send。最后一条用户消息始终可编辑；运行时编辑自动暂停，随后使用正式 `navigate(parent_id)` 和新 user event 重新驱动，活动视图按 OpenHands HEAD 分支读取，旧回答不再出现在当前对话。页面收口为无身份标签的问答、右侧用户气泡、折叠工作过程及紧凑 composer；唯一 head 为 `0060_agent_conversations`，无 `CURRENT`、`READY` 或下一切片。 |
| 2026-08-26 | FR-26 | Agent Workspace/OpenHands 定向 pytest（69 passed）；受影响 Python Ruff/Pyright；Web ESLint/typecheck/build；`git diff --check` 与任务状态核对 | PASS：附件经 OpenHands 正式 file upload 写入外置共享工作区，图片同时作为原生 ImageContent 传入，普通文件路径作为消息上下文；会话内模型和思考强度使用正式 switch_llm；上下文只展示原生累计 usage 与已声明窗口，不伪造当前 token 百分比。新会话使用原生 LLM summarizing condenser，Condensation 事件在对话流中呈现；新增定向回归覆盖附件、图片、原生上下文和模型切换。无 CURRENT、READY 或下一切片。 |
| 2026-08-26 | FR-27 | Web ESLint/typecheck/build；独立源码 Vite 服务上的 Agent Workspace 定向 Playwright；`git diff --check` 与任务状态核对 | PASS：会话流在首次进入或新一轮开始时定位最新内容，但流式 delta 不再强制拉回用户；生成中显示动态三点，完成后显示向下箭头，二者均可平滑跳转至最新回复。未改动 OpenHands、会话事件或 FlowRun。无 CURRENT、READY 或下一切片。 |
| 2026-08-26 | FR-28 | Agent Workspace/OpenHands 定向 pytest（70 passed）；Ruff、Pyright；Web ESLint/typecheck/build；Alembic head 与 `git diff --check` | PASS：完成回复可从其正式 event identity 原生 fork 为独立 Conversation，持久化新的最小 locator 与审计命令，重放相同幂等键返回同一 binding；手动压缩只调用原生 condense，完成情况继续由 Condensation 事件渲染。运行中会话拒绝这两项控制操作；新增 0061 migration 允许 FORK 审计类型。无 CURRENT、READY 或下一切片。 |
| 2026-08-26 | FR-29 | Agent Workspace 定向 pytest（10 passed）；受影响 Python Ruff/Pyright；Web ESLint/typecheck/build；本地 Alembic head、`git diff --check` 与任务状态核对 | PASS：新建会话与原生 fork 都冻结 `model_provider_id`；会话内模型切换 API 不再接受供应商参数，并只按 binding 的冻结供应商构造正式 `switch_llm`。前端下拉仅显示该供应商的模型名称。没有可审计供应商身份的历史 binding 不被猜测回填，继续可读写但模型切换被明确拒绝；新增 0062 迁移。无 CURRENT、READY 或下一切片。 |
| 2026-08-26 | FR-30 | OpenHands active-head 定向 pytest（1 passed）；受影响 Python Ruff/Pyright；`git diff --check` 与任务状态核对 | PASS：`parent_id = "__root__"` 被识别为固定 OpenHands 事件树的合法终点，不再作为缺失 event 查找。正式 ERROR 事件可返回至页面；不重发消息、不改写事件树或掩盖上游模型错误。无 CURRENT、READY 或下一切片。 |
