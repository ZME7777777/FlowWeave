# FlowRun OpenHands Runtime 重构进度

> 创建日期：2026-08-21
> 状态：`ACTIVE`
> 当前执行切片：无
> 下一可执行切片：无
> 架构设计：`docs/flowrun-openhands-runtime-design.md`
> Agent 工作台设计：`docs/agent-workbench-technical-design.md`

## 1. 跟踪边界

本文是 FlowRun OpenHands Runtime 重构的唯一任务进度来源，从零开始记录本次架构的决策、任务、依赖、
验证和恢复信息，不继承此前重构任务的阶段编号、完成状态、实现结论或验收结果。

此前的重构决策不能作为本任务已经完成、可以跳过验证或必须保留现有实现的依据。现有源码只作为
“当前行为”的审计对象；是否保留必须重新按照本设计、固定 OpenHands 源码和真实运行证据判断。

本任务只修改 FlowWeave。OpenHands 源码仓库保持只读，当前目标事实基线为固定 commit
`9a24f6c8866f353042a57df0514ccc900e3a0691` 和由其构建的四个 `1.44.0` 包；此前完成记录中的旧版本号
继续表示当时实际验收的历史基线，不做追溯改写。

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

### FR-45 Agent 会话模型选择持久化与浮层关闭 — DONE

依赖：`FR-44`。

目标：Agent 工作台输入区选择会话供应商、模型或推理强度时立即调用独立配置 API，在 OpenHands 正式
`switch_llm` 成功后把完整期望配置持久化到 Conversation binding；刷新从 binding 恢复，不再等待下一条消息
发送才保存。由于固定 OpenHands `1.42.0` 不持久化替换 LLM，每次发送前仍重新应用已保存配置，但消息请求
不再携带或改变模型选择。历史非流式 binding 先保存选择，并在原生流式 fork 时应用。模型浮层点击外部区域
自动关闭，且不阻止被点击的原页面控件继续响应。

验收：新增迁移 upgrade/downgrade；Agent Workspace 定向测试覆盖创建、选择即持久化、发送前重应用和原生
fork 继承；Agent 工作台定向 Playwright 覆盖选择后刷新恢复、消息不再承载模型字段、打开浮层、点击外部关闭
和重新打开后正常选择；平台 Ruff/Pyright/定向 pytest、Web lint/typecheck/build、`git diff --check`、
Alembic head 与任务状态唯一性通过；本切片使用独立 Git commit。

### FR-46 Agent Workspace 终端贴底与滚轮滚屏修复 — DONE

依赖：`FR-45`。

目标：修复 Agent Workspace 共享终端的两个独立浏览器交互问题。终端抽屉尺寸变化后必须重新计算 xterm
行列数，使可见 viewport 始终填满终端容器，连续回车或持续输出到达末行时光标贴近终端底部，不保留由过期
fit 尺寸产生的大块空白。鼠标滚轮必须只滚动终端 scrollback，不得在没有可滚动历史或已位于边界时把 wheel
事件转换成 shell 上下方向键并切换命令历史；终端键盘方向键的原生行为保持不变。不得修改 OpenHands、PTY、
Runtime Provider 权限、Workspace 持久化或 FlowRun 语义。

验收：Agent 工作台定向 Playwright 覆盖抽屉打开与尺寸变化后的终端贴底、滚轮只改变 scrollback 且不向
WebSocket 写入方向键序列；Web lint/typecheck/build、相关终端单元或组件测试、`git diff --check`、Alembic
head 与任务状态唯一性通过；本切片使用独立 Git commit。

### FR-47 Agent 会话模型摘要显示思考程度 — DONE

依赖：`FR-46`。

目标：Agent 工作台输入区右下角的模型摘要同时显示当前模型与思考程度，并将已知思考程度显示为紧凑中文
标签。长模型名或窄视口下模型名称可以省略，但思考程度必须保持可见；模型配置浮层、选择即持久化和发送行为
保持不变。

验收：Agent 工作台定向 Playwright 覆盖模型摘要初始值、选择后立即更新和刷新恢复；Web
lint/typecheck/build、桌面与窄视口浏览器检查、`git diff --check`、Alembic head 与任务状态唯一性通过；
本切片使用独立 Git commit。

### FR-48 Agent Workspace tmux 滚屏与终端底部布局修复 — DONE

依赖：`FR-46`。

目标：修复持久 tmux 终端的鼠标滚轮交互和抽屉底部布局。滚轮应进入 tmux 原生复制模式滚动终端历史，
不得被 shell 解释为方向键或切换命令历史；终端宿主按边框盒计算高度，连续输出和回车到达末行时最后一行
保持完整可见。不得修改 OpenHands、PTY、Runtime Provider 权限、Workspace 持久化或 FlowRun 语义。

验收：Agent 工作台定向 Playwright 覆盖终端宿主无垂直溢出、底部行可见和滚轮事件不产生 shell 输入；
持久 tmux 脚本定向测试；Web lint/typecheck/build、受影响 Python 语法检查、`git diff --check`、Alembic
head 与任务状态唯一性通过；本切片使用独立 Git commit。

### FR-49 Agent 会话正式 Action commentary 与最终回复定位修复 — DONE

依赖：`FR-48`。

目标：修复平台把固定 OpenHands 1.42.0 的 `ActionEvent.thought` 错误地从嵌套 `action` 读取，导致真实工具
调用 commentary 在正式事件到达时消失的问题。REST 与实时安全投影必须读取事件顶层正式 `thought` 和
`summary`，继续屏蔽 `reasoning_content`、`thinking_blocks` 与 `responses_reasoning_item`；普通 Tool Action
的正式 commentary 接替浏览器流式 delta，`FinishAction` 同一事件的顶层 thought 与 `action.message` 分别
进入工作过程和唯一最终回复。工具标题优先使用正式 summary，工具类型作为辅助状态。长最终回复完成时视口
定位到回复开头，“跳转到最新”控件不得因自身布局造成假未到底状态。不得修改 OpenHands、持久化消息或事件，
不新增数据库字段或 FlowRun/Runtime 协议。

验收：固定 OpenHands `ActionEvent` 源码取证；平台事件投影定向 pytest；Web lint/typecheck/production build；
Agent 工作台定向 Playwright 覆盖真实 Tool Action 顶层 commentary、Finish thought/final 分层、长回复开头定位
和真实尾部判断；API/Web 无缓存重建部署与真实历史会话复验；`git diff --check`、Alembic head 和任务状态
唯一性通过；本切片使用独立 Git commit。

### FR-50 Agent Runtime exhausted recovery task 自愈 — DONE

依赖：`FR-49`。

目标：修复 Agent Workspace Runtime 的 Docker 后端故障耗尽 20 次重试后，`DEAD` provision/recovery task
永久阻塞、导致历史 Conversation 的 events/context 持续返回 503 的问题。仅当当前 generation 的受管资源
同时满足 `desired_state=RUNNING`、`observed_state=READY` 且已有后端资源 ID 时，才视为可写活动 Runtime；
残留的 `CREATING/ERROR` 资源不得抑制恢复。启动恢复和 Worker 周期维护均需识别最新的 `DEAD` provision
task，将其重置为 `RETRY` 并清零尝试计数，且不能被已成功的旧 provision task 遮蔽。不得修改 Conversation、
事件、FlowRun 或 Runtime 持久化协议，不新增迁移。

验收：Agent Workspace 定向 pytest 覆盖残留不健康资源和旧 task 遮蔽；受影响 Python `py_compile`、
`git diff --check`、Alembic head 与任务状态唯一性通过；本切片使用独立 Git commit。

### FR-51 Agent 会话工具操作详情与正式结果关联 — DONE

依赖：`FR-50`。

目标：将 Agent 工作过程中的工具事件从笼统的“文件操作已完成/命令已执行”改为可核对的具体操作。
平台安全投影补齐固定 OpenHands 正式 `action_id`、`tool_call_id` 与 `tool_name`，前端只按这些身份把
Action 与 Observation 合并为一条工具记录，不按相邻顺序或文本猜测。折叠标题直接说明操作、对象与状态，
例如“已读取 工作区/src/a.ts”“已编辑 工作区/src/b.ts”“已运行 git status”；每条记录附带默认折叠的
详情，Shell 显示原命令、退出码和安全投影输出，File Editor 显示命令、路径、行范围或变更片段，其他工具
显示脱敏后的正式输入与结果。工作过程整体的运行时展开、完成后折叠语义保持不变；不得展示隐藏 reasoning、
Secret、未经安全投影的原始事件或宿主机实现信息，不修改 OpenHands、数据库或事件持久化协议。

验收：平台投影定向 pytest 覆盖 REST/实时一致的正式关联字段及敏感字段剔除；Agent 工作台定向 Playwright
覆盖 Action/Observation 正式合并、明确文件/命令标题、工具详情默认折叠及展开后的原始操作与结果；Web
lint/typecheck/production build、API/Web 重建部署、真实历史会话复验、`git diff --check`、Alembic head 与
任务状态唯一性通过；本切片使用独立 Git commit。

### FR-52 Agent Workspace 工作目录模型与安全 API — DONE

依赖：`FR-51`。

目标：在默认 Agent Workspace 的持久项目根 `/runtime/workspace/project` 下建立一等“工作目录”上下文。
一个工作目录可选择一个或多个项目根子目录；单目录版本的 OpenHands 工作目录为该子目录，多目录版本的
OpenHands 工作目录为共同项目根，所选路径只作为产品分组与导航范围，不伪装成 OpenHands 多 Workspace 或
安全隔离。根工作区保持隐式上下文，不创建默认项目记录。新增不可变版本和路径明细，工作目录修改时追加
版本，使后续 Conversation 能冻结原版本。公开 CRUD API 只接受相对普通目录，拒绝绝对路径、`..`、反斜杠、
符号链接、非目录、重复路径和父子路径同时选择；归档保留版本供历史引用。不得创建 Conversation、调用模型、
修改 OpenHands 或 FlowRun。

验收：新增 migration upgrade/downgrade；Agent Workspace 定向测试覆盖根上下文、单/多目录、版本追加、
归档与路径拒绝；平台 Ruff/Pyright/定向 pytest、OpenAPI、`git diff --check`、Alembic head 与任务状态唯一性
通过；本切片使用独立 Git commit。

### FR-53 Agent 会话首条消息原子创建 — DONE

依赖：`FR-52`。

目标：新建会话仅在浏览器内形成绑定根工作区或工作目录版本的草稿；首条消息时通过幂等 bootstrap 命令创建
OpenHands Conversation、投递唯一正式 user event 并激活 binding。发送前不写数据库、不出现在列表、不产生
稳定会话 URL；明确失败清理隐藏空会话，不确定投递按正式事件身份对账且不得重发。Conversation 冻结工作目录
版本及最终 working directory，根会话的版本引用保持 NULL。

### FR-54 Agent 会话一次性标题元数据任务 — DONE

依赖：`FR-53`。

目标：首条正式 user event 被接受后启动一次性标题元数据任务；使用独立供应商调用，只更新展示标题和生成
状态，不向 OpenHands Conversation 写事件或污染上下文。手动双击改名以 CAS 阻止延迟自动标题覆盖，失败时
使用首句规范化标题，不生成带序号的未命名会话。

### FR-55 Agent 工作台工作目录分组与内存草稿 — DONE

依赖：`FR-54`。

目标：左侧顶层“新建会话”绑定根工作区并与所有工作目录平级；每个工作目录提供独立新建入口并在其下展示
已激活会话。所有新建入口只打开浏览器内草稿，发送前左侧无临时会话项、无持久化；首条消息 bootstrap 成功
后才插入标题生成中的正式会话并导航。刷新或离开未发送草稿时直接丢弃。

### FR-56 Agent Workspace 右侧工作区与文件/IDE 信息 — DONE

依赖：`FR-55`。

目标：实现默认收缩的右侧工作区摘要及概览、文件、终端多 Tab；展示当前根/工作目录范围、各 Git 仓库、
会话输入附件和 IDEA/Gateway 连接信息。文件第一版只读并按授权范围列树、预览和下载；终端复用现有 Runtime，
以 Conversation 冻结 working directory 启动。标题栏移除编辑/缩放按钮，保留双击标题修改。

### FR-57 工作目录与懒创建会话完整门禁 — DONE

依赖：`FR-52`–`FR-56`。

目标：集中完成迁移矩阵、静态检查、平台/Web 测试、OpenAPI、Compose 安全、真实 Runtime/OpenHands、根与
单/多目录会话、草稿刷新丢弃、首条消息幂等、标题隔离、文件/终端/IDE 和部署后 E2E；确认多目录只表达产品
范围而非权限隔离，既有根工作区 Conversation 保持可读写且不猜测迁移。

完成：迁移矩阵、Compose 安全、Ruff、Pyright、Web lint/typecheck/build、OpenAPI、跨模块 façade 门禁和
Agent Workspace 定向 Playwright 已通过；Runtime 镜像恢复、会话恢复期间的工作区读取及右侧栏交互问题已
修复并部署。用户完成部署后验收并明确确认本切片标记为 `DONE`。

### FR-58 新会话完整能力与显式新增工作区 — DONE

依赖：`FR-57`。

目标：延迟创建只影响 Conversation 持久化时机，发送首条消息前的页面、模型供应商、模型、推理强度、
附件、工作区摘要、文件和独立终端能力必须与正式会话一致，且界面不强调“草稿”概念。首条消息通过既有
幂等 bootstrap 原子冻结当前工作目录、模型配置和附件，成功后才创建列表项和稳定 URL，并将发送前打开的
工具状态迁移到正式 binding。左侧工作区分组提供明确的“新增工作区”按钮，复用既有工作目录创建契约，
允许从 `/runtime/workspace/project` 下选择一个或多个合法子目录并创建可新建会话的工作区；不得因新增
工作区、会话、文件或终端启动第二个 Agent Runtime 容器。同步收口 Codex 式会话列表、标题双击编辑、默认
环境摘要、可调宽度工具区、单文件页签、多独立终端、目录树和显式关闭终端生命周期的产品回归。

验收：实现完成后集中执行平台全量测试、Ruff/Pyright、Web lint/typecheck/production build、OpenAPI、
PostgreSQL 迁移矩阵、Compose 安全、固定 OpenHands contract/smoke、Agent Workspace 定向 Playwright、
部署后真实新增工作区与首条消息、附件、文件树、多终端隔离/关闭及单 Runtime 容器检查；最后核对唯一
Alembic head、任务状态和 `git diff --check`。本切片使用独立 Git commit。

完成：新会话在首条消息前已具备正式会话同款的模型、推理强度、附件、环境摘要、文件和多终端能力；
显式新增工作区复用服务端目录校验并保持单一 Agent Runtime。工具页签状态在 bootstrap 后迁移到正式
binding，终端显式关闭会调用服务端销毁接口；会话列表固定为紧凑两行。平台全量、迁移、OpenAPI、
Compose、固定 OpenHands contract/smoke、Web 静态构建和 3 项部署后 Agent E2E 均已通过。

### FR-59 Agent Workspace 终端空间与宽屏重排修复 — DONE

依赖：`FR-58`。

目标：增大右侧工具区终端的有效显示面积，使终端宿主始终占满页签内容区剩余高度，并以更清晰的字号、
行高、内边距和连接状态呈现。修复工具区连续拖宽至上限时 xterm、FitAddon、WebSocket PTY 与持久 tmux
窗口尺寸不同步导致的右侧重复竖线/点阵残影；宽度变化必须在布局稳定后合并重排，前后端只接受当前有效
行列数，隐藏页签重新显示时再次同步。不得重建 Agent Runtime、共享终端实例、修改 OpenHands 或改变
终端关闭/最小化生命周期。

验收：Agent 工作台定向 Playwright 覆盖终端占满可用高度、最小/默认/最大宽度重排、xterm screen/viewport
边界一致、列数随宽度增长且无横向残影或溢出；Web lint/typecheck/production build、相关平台终端测试、
真实 Runtime/tmux 尺寸同步与单容器检查、`git diff --check`、Alembic head 和任务状态唯一性通过。本切片
与 FR-58 后续审计修复一并形成独立 Git commit，排除用户已有 README 和项目总览文档改动。

完成：右侧终端使用完整工具主体高度，字号、行高、内边距和连接状态样式同步优化；拖动宽度时由
FitAddon 在布局稳定后更新 xterm，并合并向 PTY 发送最终行列数。持久 tmux 不再被 `resize-window` 切换为
`manual`，而以 `window-size latest` 跟随当前客户端，消除宽屏后的竖线和点阵空白。正式会话工具区仅传
binding，未发送会话仅传工作目录，历史工作目录会话可正常打开文件和终端。真实部署在 1280×720 视口下
将抽屉从 300px 拖至 580px 后，终端宿主高 569px、screen 宽 515px，screen/viewport 无水平溢出；tmux
client/window 均为 `66×25`、模式为 `latest`，Agent Runtime 容器数量保持 1。

### FR-60 Agent 会话实时反馈、重思考与终端关闭交互收口 — DONE

依赖：`FR-59`。

目标：修复 Agent 工作台发送首条流式正文前即丢失“正在思考”反馈、计时错误显示为 0 秒，以及高频
WebSocket delta 逐帧同步 React state 导致的可见流式卡顿。浏览器必须从正式请求提交起连续展示可计时的
等待/工作过程，并以动画帧合并安全可见 delta；同一轮收到正式 assistant、FinishAction 或 ERROR 后按其
正式 event identity 收束临时运行状态，不再把已完成的分析保留为“分析中”。最后一条用户消息编辑并重新
思考时，页面只按正式 `parent_id` 拓扑裁剪该 user event 的活动后代和临时投影，保留更早轮次，等待新的
active branch 回填；不得全会话刷新、残留旧回复或按文本/时序猜测分支。终端关闭确认改为符合工作台样式的
应用内无障碍对话框，明确确认后才关闭并停止运行命令，取消不改变终端。不得修改 OpenHands、事件持久化、
Conversation HEAD、工作区或 Runtime 容器边界。

验收：补充 Agent 工作台定向 Playwright，覆盖请求提交即显示动态等待、连续 delta 可见追加、正式终态结束
过程、编辑重思考只移除目标后代、关闭终端的应用内确认/取消/确认关闭；集中运行 Web lint/typecheck/
production build、相关平台 OpenHands 投影测试、Ruff/Pyright、`git diff --check`、Alembic head 和任务状态
唯一性。完成后独立 Git commit，排除用户已有 README 与项目总览文档改动。

完成：前端在正式消息请求提交的同一渲染周期插入浏览器内的临时 user event 并启动“正在思考”计时，
不会因标题异步更新而重置当前轮。安全可见 delta 使用 `requestAnimationFrame` 批量追加；正式 commentary、
assistant、FinishAction 或 ERROR 到达后清除临时文本，并将已完成 Thought 显示为“分析 / 已完成”。重思考按
正式 `parent_id` 递归隐藏被编辑 user event 及其活动后代，再以新 user event 局部替换，较早轮次不刷新。
终端关闭改为可取消、支持 Escape 的工作台内确认对话框，确认后才调用关闭 API。Web lint/typecheck/production
build、源码 Vite 上 3 个相关 Playwright 场景、Ruff/Pyright 和 Agent Workspace/OpenHands 定向 pytest
（103 passed）通过；全量平台 pytest 仍有 23 个未改动的 API/Environment/Sandbox 既有失败项，未将其伪记为
通过。唯一 Alembic head 为 `0068_agent_title_metadata`，`git diff --check` 通过。

### FR-61 Agent 会话用户消息刻度导航 — DONE

依赖：`FR-60`。

目标：在 Agent 会话内容区增加仅针对正式 user MessageEvent 的左侧刻度导航。刻度必须按各用户消息在
滚动内容中的实际位置分布，当前阅读位置清晰高亮；悬停或键盘聚焦显示经规范化、截断的消息摘要，点击后在
会话内容容器内平滑定位到对应消息。锚点仅使用 OpenHands 正式 user event `id`，不按文本、时序或虚构
位置猜测，不持久化阅读位置，不修改 Conversation/Event/HEAD、OpenHands、Runtime 或工作区边界。窄屏隐藏
该辅助导航，避免覆盖消息内容。

验收：补充 Agent 工作台定向 Playwright，覆盖多个用户消息的刻度数量、正式 event id 锚点、摘要、当前态与
点击定位；集中运行 Web lint/typecheck/production build、Alembic head、任务状态唯一性与 `git diff --check`。
本切片使用独立 Git commit，排除用户已有 README 与项目总览文档改动。

完成：会话内容区在宽屏显示仅对应正式 user MessageEvent 的左侧刻度；每个刻度以事件 `id` 绑定用户消息，
由 ResizeObserver、MutationObserver 和滚动事件按真实内容高度重算位置及当前阅读项。悬停或键盘聚焦可查看
规范化、截断的消息摘要，点击在自身滚动容器内平滑定位；窄屏不显示导航。未新增接口、持久化数据或
OpenHands 会话状态。

### FR-62 Agent 会话标题栏重复工作区入口移除 — DONE

依赖：`FR-61`。

目标：移除 Agent 会话标题栏中与右侧工作区摘要重复的“打开/关闭工作区工具”按钮，避免同一侧边栏
出现两个入口。保留右侧“环境信息”摘要本身的展开入口，以及已打开工具区内的关闭入口；不得改变
工作区、文件、终端、会话或 Runtime 行为。

验收：补充 Agent 工作台定向 Playwright，确认标题栏不再含工作区工具按钮、环境信息摘要入口仍可见；
集中运行 Web lint/typecheck/production build、Alembic head、任务状态唯一性与 `git diff --check`。
本切片使用独立 Git commit，排除用户已有 README 与项目总览文档改动。

完成：标题栏仅保留正式会话可用的删除入口，不再提供与右侧栏重复的工作区工具开关。右侧“环境信息”
摘要仍保留展开入口，打开后的工具区仍保留关闭入口；不改变文件、终端或会话生命周期。

### FR-63 Agent Runtime 事件中继清理与控制面故障隔离 — DONE

依赖：`FR-62`。

目标：修复 Agent Workspace 浏览器会话断开、页面刷新或重新部署后，API 未能取消 Runtime Provider 内
`docker exec` 事件中继，持续累积子进程与线程并耗尽 Provider PID 配额的问题。WebSocket 路由必须在
客户端断开时主动关闭上游异步事件迭代器，使 Provider 可靠终止对应 relay；同一条连接不得残留
孤儿 relay。Sandbox reconcile 遇到 Docker 控制面暂不可用等暂态验证错误时，只记录可诊断降级并保留
当前受管 Runtime，不得把它误判为物理丢失、删除容器或切换为 `RECONNECTING`。真正确认资源不存在的
路径仍按既有 fenced recovery 执行。不得修改 OpenHands、Conversation/Event 持久化、Workspace 外置
存储或 Runtime 容器隔离边界。

验收：新增 Provider relay 取消、Agent Workspace WebSocket 断开和暂态 reconcile 故障定向测试；平台
Ruff/Pyright、相关 pytest、Web lint/typecheck/build、`git diff --check`、Alembic head 和任务状态唯一性
通过；在实际 Compose 中验证 Provider PID 不会随重复连接累积，重新部署后 Runtime 收敛回可写状态。本
切片使用独立 Git commit，排除已有附件功能的未提交改动。

完成：Agent Workspace 与 FlowRun Conversation WebSocket 现在并行监听 Runtime 上游事件和浏览器
断连；空闲断连时取消 pending 读取并显式关闭异步生成器，Runtime Provider 随即终止对应
`docker exec` relay。Sandbox reconcile 仅在确认 `RUNTIME_LOST` 或资源冲突时触发 Agent Runtime
replacement；Docker 控制面暂时不可用只写入可诊断错误与退避，保留受管资源的 `RUNNING` 意图和
既有活动 Runtime。新增 relay 终止、空闲 WebSocket 断连和控制面 503 隔离回归。

### FR-64 Agent 会话刷新恢复与连续过程反馈 — DONE

依赖：`FR-63`。

目标：Agent 工作台在浏览器刷新、重连或初始 REST 回填时，必须通过 OpenHands 正式 input-readiness
和正式事件树恢复尚未终止的当前轮，持续显示动态“正在思考”或具体工具执行状态与墙钟耗时；不能把仍在
运行的过程错误呈现为静态“耗时 0 秒”。安全可见 delta 和过程事件使用动画帧批量合并，流式阶段不重复
解析整段 Markdown，避免高频输出卡顿。收到本轮正式 assistant、FinishAction 或 ERROR 后，过程区域必须可靠
自动折叠，最终回复或失败结果单独显示。不得持久化浏览器运行状态、伪造 Agent 思考、修改 OpenHands、
Runtime 或 FlowRun 契约。

完成：刷新后的当前轮仅在 OpenHands 正式 `input-readiness` 返回不可接收输入、且正式事件树存在未终止 user
event 时恢复为运行中；过程摘要按正式 Action 语义显示思考、后台命令、文件、浏览器、MCP、Skill 或任务状态。
流式正文和过程事件均在动画帧合并，正文在终态前以轻量文本呈现；正式 assistant、FinishAction 或 ERROR
到达后过程卡片受控关闭，最终内容独立呈现。未新增平台会话状态或 OpenHands 旁路。

### FR-65 Agent 会话原生动态能力加载 — DONE

依赖：`FR-64`。

目标：为每个新建 Agent 会话在创建时通过正式 `AgentContext.registered_marketplaces` 注册会话专属、只读的
FlowWeave Marketplace，并维持现有创建时的显式能力加载。会话设置可在空闲时追加已发布的 Skill、MCP 或
Plugin；FlowWeave 必须将固定版本/digest 物化为原生 Marketplace Plugin，再调用固定 OpenHands 1.42.0 的
`POST /api/conversations/{conversation_id}/load_plugin`。Skill 的 `$` trigger、Plugin 的 `/plugin:command` 与
MCP runtime tools 必须由 OpenHands 原生 Loader 生效；Runtime 拒绝时不得写入会话能力引用。历史会话如果创建时
没有该 Marketplace，不得伪造已加载状态，应清晰提示上游正式 API 无法原地补注册。不得修改 OpenHands 源码、
直接修改 OpenHands persisted state，或使用 prompt/私有执行器模拟能力。

验收：新增动态 Marketplace/原生 `load_plugin` 定向测试，受影响 Python Ruff/Pyright、Web
lint/typecheck/build、固定 OpenHands contract（含 load_plugin route）、`git diff --check` 与任务状态唯一性；
完成后以 Compose 部署并在新建会话上真实验证动态添加能力和 `$`/`/` 候选。单独提交本切片，排除无关改动。

完成：每条新 Agent Conversation 创建前均物化并注册独立的空 Marketplace，后续追加的已发布 Skill、MCP
或 Plugin 都以固定版本/digest 进入该 Marketplace，并只在 OpenHands 正式 `load_plugin` 成功后写入会话
能力引用。Skill wrapper 明确声明 `$<capability_key>` trigger；Composer 按当前会话已加载能力提供 `$`
Skill、`/plugin:command` 和 MCP 入口候选。创建时没有注册 Marketplace 的旧会话保持可读写，但追加能力返回
明确的上游限制错误，不伪造已加载状态。

### FR-66 历史会话能力入口与迁移指引 — DONE

依赖：`FR-65`。

目标：消除历史会话在 Composer 输入 `$` 或 `/` 时无候选、无解释的静默失败。当前会话尚未加载任何
Skill、Plugin 命令或 MCP 时，Composer 必须直接提供“管理能力”入口；对于固定 OpenHands `1.42.0`
因创建时没有 `registered_marketplaces` 而拒绝动态加载的历史会话，能力设置必须保留明确失败原因，并提供
创建一个带当前“新会话默认能力”的新会话入口，同时保持源会话和其原生事件历史不变。不得伪造旧会话能力已
加载、修改 OpenHands persisted state、修改 OpenHands 源码、复制会话事件或以提示词模拟能力。

验收：Web lint/typecheck/production build；源码 Vite 定向浏览器验证 `$`/`/` 空能力提示、管理入口和历史
Marketplace 拒绝后的新会话入口；`git diff --check`、任务状态唯一性通过。完成后独立 Git commit 并部署。

完成：当当前会话没有已加载的能力时，Composer 的 `$` 显示“当前会话还没有加载 Skill”，`/` 显示
“当前会话还没有加载命令或 MCP”，均可直接打开能力管理。历史会话加载能力被正式 OpenHands API 拒绝时，
弹窗说明创建时缺少原生能力市场，并提供“新建可使用能力的会话”；它使用当前新会话默认能力、保留源会话
及其事件历史，未伪造旧会话已加载状态。

### FR-67 当前会话能力增量注册与锁定 — DONE

依赖：`FR-66`。

目标：当前 Agent 会话在 Composer 输入 `$` 或 `/` 时，无论是否已有匹配候选，均保留“管理当前会话能力”
入口。能力管理器对已经通过固定 OpenHands 原生 `load_plugin` 注册的能力必须置灰、禁用且不可取消；用户只能
选择并注册新的已发布能力。新会话默认能力仍可自由调整，且只影响后续新会话。不得伪造 OpenHands 卸载、
删除已加载 Runtime 工具、修改 OpenHands persisted state 或修改 OpenHands 源码。

完成：Composer 的 `$` 和 `/` 候选面板始终提供管理入口；当前会话已注册项带有锁定视觉和“不能取消”说明，
禁用交互，新增项继续经原生 `load_plugin` 追加。全局默认配置保留可编辑语义。

### FR-68 Agent 工作台分栏独立滚动 — DONE

依赖：`FR-67`。

目标：一级 Agent 工作台在桌面浏览器中必须始终占用可用视口，不能再由页面根纵向滚动而将全局导航、会话标题
或右侧工作区标题滑出视野。左侧会话列表应独立滚动，同时底部“能力”入口固定；中间仅会话内容滚动，标题和
输入区保持固定；右侧环境摘要、文件树/预览与终端分别在自身区域滚动。不得修改 OpenHands、Conversation、
Runtime、工作区或 FlowRun 协议。

完成：Agent 路由使用视口高度的 flex 外壳，移除工作台的外层最小高度溢出；三栏全部在受限高度内布局。左侧
只让会话列表滚动并保留能力入口，中央只让 Conversation Surface 滚动，右侧摘要与工具内容维持其各自的
滚动容器；窄屏仍保留原有的堆叠式浏览行为。

### FR-69 Agent 首条消息不确定投递恢复 — DONE

依赖：`FR-68`。

目标：修复新会话首条消息在网络响应丢失或 Runtime 暂时不可用后，客户端为每次重试生成新的幂等键、导致
服务端无法继续按原生事件身份对账，页面停留在新会话且刷新后失去恢复入口的问题。首发与所有对账必须重用
同一个草稿 UUID；结果不确定时浏览器仅保留可恢复的草稿、模型和附件引用并自动有限次重试原命令。刷新后必须
恢复该上下文并继续安全对账；不得重新投递 user event、持久化平台消息或将未确认的 Conversation 伪装进列表。

完成：bootstrap API 现在显式接收并复用草稿 UUID 作为 `Idempotency-Key`。不确定结果被浏览器暂存为会话级
恢复记录，自动以同一 key 有限次执行原生身份对账；刷新后恢复该草稿并继续对账。成功后清理恢复记录并导航到
激活的会话；仍未确认时提供明确的安全重试提示，保证不会重复发送。服务端错误文案不再要求用户刷新页面。

### FR-70 Agent Workspace MCP 就绪状态与首发故障隔离 — DONE

依赖：`FR-69`。

目标：Agent Workspace 的能力管理必须在同一受管 OpenHands Runtime 中，以正式
`POST /api/mcp/test` 显示每个选中 MCP 的检测中、已连接或不可用状态；失败信息只说明超时、连接失败或未知，
不得返回 URL、Secret、原始异常或 MCP Tool 内容。保存新会话默认能力前必须复检所有选中 MCP，任何一个
不可用均拒绝保存，避免把已知坏配置冻结到下一条首发消息。检测不创建 Conversation、不写入消息、事件、HEAD
或 Runtime 私有状态；能力管理器应提供重新检测入口。

首条 bootstrap 若遇到已经明确可归类的 MCP 初始化故障，必须删除不可见预留并返回对应 MCP 可用性错误，不能
伪装成不确定投递或安全核对。真正网络响应丢失的原生 event identity 对账与 `FR-69` 的稳定草稿 UUID 语义保持
不变。不得修改 OpenHands 源码或用平台私有执行器模拟 MCP。

验收：新增 Agent Workspace MCP readiness/首条失败清理定向 pytest；Web lint/typecheck/production build
与定向 Playwright 覆盖检测中、已连接、失败提示、重新检测和阻止保存；固定 OpenHands MCP probe 契约、
`git diff --check`、Alembic head 与任务状态唯一性通过。完成后单独提交并部署 API、Worker（如受影响）和 Web。

完成：能力管理器对每个已选 MCP 自动显示“检测中 / 已连接 / 连接超时、连接失败或不可用”，并提供
“重新检测 MCP”。检测通过当前 Agent Workspace 的受管 OpenHands Runtime 正式 `POST /api/mcp/test`，不创建
Conversation 或写入消息/事件；保存默认能力或向当前会话追加能力前会重新检测，失败 MCP 不会被保存。
OpenHands 明确的 `MCPTimeoutError` 和连接错误在首条 bootstrap 中会释放不可见预留、返回对应 MCP 名称与安全
状态，保留真正不确定投递的原生 event identity 对账。API 与 Web 已重建部署，全部服务健康。

### FR-71 新会话斜杠能力入口可见性 — DONE

依赖：`FR-70`。

目标：新会话草稿尚未拥有 Conversation binding 时，输入 `/` 也必须打开能力候选面板。已经保存为新会话默认能力的
MCP 应直接列为候选；尚未加载 MCP 时，面板必须说明当前没有可用 MCP，并提供进入“能力”管理的入口，不能静默地
让 `/` 看似失效。该入口只管理默认能力，不创建 Conversation、不改变消息或 Runtime 状态。

验收：Web lint/typecheck/production build、定向浏览器用例覆盖新会话 `/` 的空态管理入口与已加载 MCP 候选，
以及 `git diff --check` 和任务状态唯一性检查。完成后单独提交并部署 Web。

完成：新会话草稿也把“管理能力”入口传递给斜杠候选组件，因此输入 `/` 始终显示候选面板。默认已加载的 MCP
继续直接列出；没有默认命令或 MCP 时，面板明确说明未加载并提供“管理”按钮，用于打开新会话默认能力管理，不会创建
Conversation 或触发 Runtime 写入。

### FR-72 Agent 工作区会话分组折叠 — DONE

依赖：`FR-71`。

目标：左侧 Agent 工作区的根工作区、活动工作目录和归档工作目录分组标题支持单击折叠与展开，只影响当前分组
下的会话列表。标题必须提供 `aria-expanded` 与明确的展开/收起名称；右侧“+”继续作为独立的新建会话入口，不能
因切换分组而触发创建或被隐藏。默认保持展开，不持久化浏览器状态，不修改 Conversation、OpenHands、Runtime、
工作区或 FlowRun 契约。

验收：Web ESLint/typecheck/production build；定向 Playwright 覆盖根工作区和普通工作目录的会话列表隐藏/恢复、
ARIA 状态及独立“+”入口；Alembic head、任务状态唯一性与 `git diff --check` 通过。本切片使用独立 Git commit。

### FR-73 Agent 工作区会话行紧凑样式回归修复 — DONE

依赖：`FR-72`。

目标：修复 FR-72 为会话列表加入折叠容器后，原先仅匹配分组直接子按钮的紧凑会话行样式失效问题。根工作区、
活动工作目录和归档工作目录中的会话行必须继续使用 38px 两行布局、截断标题和活动态样式；分组标题与独立“+”
入口不受影响。不得改变 Conversation、OpenHands、Runtime、工作区或 FlowRun 契约。

验收：Web ESLint/typecheck/production build；定向 Playwright 验证折叠容器内会话行保持 38px 紧凑高度，并保留
既有折叠/展开与独立“+”入口行为；Alembic head、任务状态唯一性与 `git diff --check` 通过。本切片使用独立 Git commit。

### FR-74 OpenHands 1.44.0 精确源码基线升级 — DONE

依赖：`FR-73`。

目标：把 FlowWeave 的 OpenHands 事实基线从四包 `1.42.0`、commit
`f09e03eac772290feeb51b7d7390ffaefeca1a09` 升级到用户已拉取并审计的四包 `1.44.0`、精确 commit
`9a24f6c8866f353042a57df0514ccc900e3a0691`（`v1.44.0-6-g9a24f6c88`）。冻结新的源码 archive digest、
四包依赖、Runtime build provenance、Tool catalog 与正式 HTTP/Event/Plugin/Task/Condenser 契约；历史已经发布的
Environment Runtime image 和 Snapshot 继续引用原 digest，不就地漂移。升级只修改 FlowWeave，不修改 OpenHands
源码，不以浮动 `main`、PyPI 最新版或私有 overlay 代替精确 source lock。

验收：源码锁下载与四包版本校验；Runtime 镜像无缓存构建、provenance、`contract_check.py` 和真实 smoke；
受影响平台 Ruff/Pyright/pytest、OpenAPI/架构契约、Compose 安全、Alembic head、任务状态唯一性与
`git diff --check`。确认现有 Conversation 原 ID reload、Plugin/MCP/Skill/Task/Condenser 和事件流契约在新镜像中
保持正式可用。本切片独立提交，提交后停止。

### FR-75 OpenHands 事件订阅隔离与远程结构化 Tool 收缩 — DONE

依赖：`FR-74`。

目标：利用 `1.44.0-6` 正式的按订阅者定向 delta 投递和远程结构化内置 Tool 解析，复核并删除 FlowWeave 中
已经重复的广播隔离、Tool spec 展平或文本兼容逻辑；保留 FlowWeave 授权代理、安全投影和正式事件身份关联。

### FR-76 OpenHands Profile、Secret、Condenser 与标题兼容收缩 — DONE

依赖：`FR-75`。

目标：利用正式 Profile 持久迁移/预检、read-at-use connection、Secret serializer 探测、订阅模型 condenser
dispatch 和远程标题生成修复，删除可由 OpenHands 生命周期接管的 FlowWeave 兼容分支；保留不可变供应商引用、
权限、用量、手动标题 CAS 和产品状态投影。

### FR-77 OpenHands Oracle、结构化 Task Outcome 与可选 ACP — DONE

依赖：`FR-76`。

目标：以 FlowWeave 冻结的模型 Profile、Tool Policy、预算和审计启用原生 `ask_oracle`，消费结构化 Task Outcome；
仅在产品明确选择 ACP Agent 时使用上游 `INSTALL_ACP_PROVIDERS` 构建参数，默认 Runtime 不安装或暴露未治理 Provider。

### FR-78 Agent 会话裸链接识别与安全跳转 — DONE

依赖：`FR-77`。

目标：在 Agent 工作台共享的消息 Markdown 渲染层启用 GFM 裸链接识别，使已发送的用户输入、Agent 回复与可见
过程文本中的 `https://…` 和 `www.…` 可以直接点击跳转。所有外部链接必须以安全的新标签页打开，并保留
`noopener`/`noreferrer` 隔离；代码块、图片渲染和流式 delta 的纯文本显示保持现有语义，避免在高频 token
期间反复解析 Markdown。不得修改 OpenHands、Conversation/Event、Runtime 或 FlowRun 契约。

验收：Web ESLint/typecheck/production build、定向浏览器验证用户输入和回复中的裸链接可点击、链接安全属性、
流式过程不触发 Markdown 重解析，以及 `git diff --check`、任务状态唯一性通过。完成后单独提交并部署 Web。

完成：共享消息渲染使用 `remark-gfm` 自动识别 `https://…` 与 `www.…`，用户输入、回复和可见过程文本
共用该路径；外部 HTTP(S)/邮件链接统一带 `target="_blank"` 与 `noopener noreferrer`。流式 delta 仍按纯文本
渲染，只有正式消息回填才解析 Markdown。补充了用户输入与 Agent 回复裸链接的浏览器回归，Web 已无缓存重建并
替换部署，未重启 API、Worker 或 Agent Runtime。

### FR-79 Agent 会话来源统一管理 — DONE

依赖：`FR-78`。

目标：将 Agent 工作台右侧环境摘要中的“本会话附件”改为“来源”，统一展示当前会话用户消息提供的文件、
图片及 HTTP(S) URL；URL 必须从用户输入中安全提取并提供受隔离的新标签页打开，文件/图片继续复用现有工作区
文件预览。去重后的来源须有明确类型，草稿附件与已发送来源不得混淆。不得持久化消息或来源副本，不修改
OpenHands、Runtime 或 FlowRun 契约。

验收：Web lint/typecheck/build、定向浏览器验证用户 URL、图片与文件来源的聚合、打开/预览入口以及
`git diff --check`、任务状态唯一性通过；独立提交本切片。

完成：右侧环境摘要的“本会话附件”已替换为“来源”。它仅从当前会话正式 user/human MessageEvent 聚合
HTTP(S) URL、文件与图片；同一 URL 或路径只出现一次。链接以隔离的新标签页打开，文件与图片继续打开既有
工作区预览。尚未发送的附件会明确标记为“待发送”，不会与已发送来源混淆；不保存平台来源副本。

### FR-80 Agent 会话终端选择与复制 — DONE

依赖：`FR-79`。

目标：修复 Agent 会话工作区终端在持久 tmux 开启鼠标上报时，拖拽选区会在松开鼠标后被 xterm 当作
PTY 鼠标输入清除的问题。普通左键拖拽必须始终创建并保留 xterm 选区，随后可用系统复制快捷键复制；
不向 PTY 发送该次选择手势。终端管理页、滚动、右键、显式 Shift 强制选择、键盘输入、tmux 和 Runtime
协议保持不变。

验收：Agent 工作台定向 Playwright 覆盖启用 xterm 鼠标上报后的普通拖拽选区、复制内容及无 PTY 鼠标
输入；Web lint/typecheck/production build、`git diff --check`、任务状态唯一性通过；独立提交本切片。

完成：持久 tmux 开启 xterm 鼠标上报时，终端捕获普通左键拖拽，以公开的 Buffer 与 selection API 建立
选区并拦截整段拖拽事件；松开鼠标后选区仍可由浏览器 `copy` 事件复制，PTY 不再收到这次鼠标按下、移动或
释放。终端管理页和现有滚轮/键盘语义未修改。

### FR-81 Agent 会话当前轮末尾动态运行提示 — DONE

依赖：`FR-80`。

目标：Agent 工作台只要当前轮仍由 OpenHands 正式 readiness 与事件状态判定为运行中，就必须在当前轮可见内容
末尾持续显示低干扰动态状态，避免已完成工具记录与后续模型思考之间的空白被误认为页面卡住。没有未完成工具时
显示“正在思考”；正式工具 Action 尚无按 `action_id` / `tool_call_id` 关联的 Observation 时显示对应的后台执行
状态；收到安全可见流式正文时显示“正在生成回复”。提示随当前轮正式 assistant、FinishAction 或 ERROR 终态
立即消失，不持久化或伪造思考内容，不修改 OpenHands、Conversation/Event、Runtime 或 FlowRun 契约。

验收：Web ESLint/typecheck/production build；定向 Playwright 覆盖工具完成后仍显示当前轮末尾动态“正在思考”、
未完成后台命令与正式终态移除提示；Alembic head、任务状态唯一性与 `git diff --check`。完成后独立提交本切片。

完成：当前运行轮在已有工作过程之后持续追加低干扰动态状态。尚无进度或已关联到正式 Observation 的工具全部
完成时显示“正在思考”；存在尚无正式结果的工具 Action 时显示具体后台状态；安全可见流式正文到达后显示
“正在生成回复”。正式 assistant、FinishAction 或 ERROR 收束当前轮后提示立即移除；未新增平台状态或事件推断。

### FR-82 Agent 会话运行反馈去重 — DONE

依赖：`FR-81`。

目标：当前轮只在内容末尾保留一处轻量动态状态。删除工作过程内占据大块空间的“等待模型响应”提示卡；运行中
工作过程标题只显示墙钟耗时，不再重复显示“正在思考”或工具状态。工具明细继续展示各自正式执行状态，当前轮
末尾继续按 FR-81 显示“正在思考”“正在后台执行命令”或“正在生成回复”。不得改变 OpenHands 正式 readiness、
事件关联、终态判定或任何持久化契约。

验收：Web ESLint/typecheck/production build；定向 Playwright 覆盖无等待卡、纯耗时标题、唯一末尾动态状态与
正式终态收束；Alembic head、任务状态唯一性与 `git diff --check`。完成后独立提交本切片。

完成：删除等待模型时的大块提示卡及其延迟警告样式。尚无过程明细时，工作过程只显示不可展开的“已耗时”；
有正式过程明细时仍可展开核对，但标题不再重复“正在思考”或工具状态。当前运行语义只在轮末轻量动态提示中
出现一次，正式终态到达后照常移除。

### FR-84 Agent 上下文压缩连续性与部署恢复 — DONE

依赖：`FR-83`。

完成：新 Agent 会话将 OpenHands 原生 summarizing condenser 的常规触发改为正式模型窗口的 90%，
事件数 10,000 仅保留为兜底，并以 `keep_first=4` 保留早期上下文。主动或手动压缩必须等待新的
`CondensationRequest → Condensation` 正式父链；摘要需保留 `USER_CONTEXT`、`COMPLETED`、`PENDING`，
且不得遗忘首条用户目标或最近纠偏。验收失败时恢复压缩前 HEAD，下一条用户消息不发送。历史
`max_size=240` 会话保持可浏览但禁止发送、重写、继续和手动压缩，页面可在相同工作目录创建新会话。

部署时发现无缓存镜像替换会将 Runtime 置为 `STARTING`，但固定幂等 provision 任务若已 `SUCCEEDED`
则不会再次被领取。恢复逻辑现仅在不存在 Provider 确认的健康可写 Runtime 时，将最新 `SUCCEEDED`
或 `DEAD` provision 任务重置为 `RETRY`；健康 writer 存在时绝不重复创建 generation。部署后原
Conversation ID 恢复可读，真实 `/context` 返回 90% 阈值并将旧 240 策略标记为只读。

### FR-85 Agent 会话模型网络失败有界重试与终态 — DONE

依赖：`FR-84`。

目标：模型网关无法连接、服务暂不可用、请求超时或无响应时，使用 OpenHands 正式 LLM 重试链总计最多尝试
5 次，并以短退避和有界单次超时避免页面长时间停留在“正在思考”。重试耗尽后必须由 OpenHands 原生
`ConversationErrorEvent` 结束当前轮；页面显示明确的网络问题和已停止状态，不暴露上游连接细节。额度、鉴权、
上下文等非网络错误继续保留独立语义，不由浏览器假超时篡改正式运行状态。

验收：固定 OpenHands `stop_after_attempt`、网络异常映射和正式错误事件链取证；OpenHands 适配器定向 pytest
覆盖创建、切换模型与 condenser 的 5 次策略；Web lint/typecheck/production build；Agent 工作台定向
Playwright 覆盖网络错误终止“正在思考”并展示安全提示；Alembic head、任务状态唯一性与 `git diff --check`。

完成：主模型与 condenser 均通过 OpenHands 正式 LLM 配置使用总计最多 5 次调用、1–4 秒短退避和 20 秒
单次超时。连接失败、服务不可用、超时或无响应在重试耗尽后继续由原生 `ConversationErrorEvent` 结束当前轮；
页面停止动态“正在思考”，显示不含上游连接细节的网络问题提示。额度等非网络错误保持原有独立提示。

### FR-86 Agent 会话最终回复分叉完整边界与历史恢复 — DONE

依赖：`FR-85`。

目标：用户对已完成的最终回复执行分叉时，新会话必须完整、空闲且可继续输入。`FinishAction.message` 仍是
产品层唯一最终回复；FlowWeave 在调用 OpenHands 原生 fork 前，使用正式 `id`、`action_id` 与
`tool_call_id` 将其解析到配对 `FinishObservation` 所形成的完整执行边界，并在创建后回读验证 HEAD 与输入
readiness。不得要求用户理解或规避 Action/Observation 生命周期，也不得把旧最终回复重新执行为下一轮结果。

完成：新分叉以配对 `FinishObservation` 作为原生 HEAD，并处理 Observation 在边界解析期间落盘导致源 HEAD
合法推进的竞态。历史上停在旧 `FinishAction`、且后续活动父链仅包含用户重试与同一旧 Action 的补偿
Observation 的异常分叉，会在下一次发送前以确定性原生 Conversation ID 幂等重建；FlowWeave binding ID、页面
URL、冻结模型和能力保持不变，用户消息只投递一次。已产生新 assistant/Action/error 的正常分叉不会重建。
`FinishObservation` 只投影为无正文的 `TOOL_RESULT` 执行确认，不再生成第二份最终回复或错误收束当前轮。

### FR-87 Agent 会话共享页面唯一入口 — DONE

依赖：`FR-86`。

目标：将当前一级 Agent Workspace 的完整会话页面从路由页迁移至唯一的共享 `agent-session` 页面模块；
`/agent` 保留为只装配该模块的薄入口。共享页面必须原样保留会话草稿、首发 bootstrap、事件流、模型与能力、
附件、来源、工作目录、文件和终端等已验证行为；不得新增第二套 reducer、WebSocket、Composer 或终端逻辑，
不得改动 `/agent-workspaces/**` API、数据库、OpenHands 事件身份、FlowRun、节点或旧节点会话代码。此切片只
建立前端唯一入口与静态依赖边界，不接入节点。

验收：受影响 Web ESLint/typecheck/production build；现有 Agent 工作台定向 Playwright；`git diff --check`、
Alembic head 与任务状态唯一性检查。完成后独立提交；后续后端内核迁移另开切片。

完成：完整 Agent 会话实现已移入 `components/agent-session/AgentSessionWorkbench.tsx`，路由页
`AgentWorkbenchPage` 只装配该共享页面；会话状态、Composer、事件流、能力、工作目录、附件、文件和终端
均未复制或改写。`/agent` 原有 URL 和 API 调用保持不变。同步修正两条已落后于当前页面的浏览器断言：
空会话展示“会话已就绪”，Plugin 命令保留原生命名空间 `/lark-tools:summarize`。

### FR-88 Agent 会话后端唯一 facade — DONE

依赖：`FR-87`。

目标：将完整 Agent Workspace Conversation 应用服务迁移到中性 `modules/agent_sessions`，以唯一 facade
承载 bootstrap、事件安全投影、消息与控制、模型、能力、附件和会话上下文。`agent_workspaces` 仅作为
当前宿主、模型与 Runtime 适配器，继续保留原有公开路由和响应；不得复制会话逻辑、改变数据库、API、
OpenHands identity、FlowRun、节点或 Runtime 行为。此切片不接入节点。

验收：Agent Workspace 定向 pytest；受影响 Python Ruff/Pyright；`git diff --check`、Alembic head 与
任务状态唯一性检查。完成后独立提交。

完成：完整 Conversation 与标题服务已移至 `modules/agent_sessions/application`；
`agent_workspaces` 的历史导入路径改为指向相同 Python 模块对象的兼容别名，路由、工作区工具与任务处理
均直接使用共享实现。保留现有路由、API、数据库模型、OpenHands 调用和测试 monkeypatch 语义，未复制任何
会话业务代码；`agent_sessions.public` 是后续新宿主的唯一入口。

### FR-89 Agent 会话共享前端传输网关 — DONE

依赖：`FR-88`。

目标：将共享 `AgentSessionWorkbench` 对 Agent Workspace 的 API、文件/终端 URL 和事件订阅依赖收敛为一个
可注入的宿主网关；`/agent` 继续注入现有 Agent Workspace 网关并保持全部产品行为不变。共享组件必须仍独占
会话草稿、首发 bootstrap、实时流、模型/能力、附件、工作目录、文件与终端渲染及交互；网关仅承接既有传输，
不得创建第二个页面、复制状态逻辑、改变公开 API、数据库、OpenHands identity、FlowRun、节点或 Runtime 行为。
本切片不实现 FlowRun/节点网关。

验收：受影响 Web ESLint/typecheck/production build；现有 Agent 工作台首发/URL 恢复和 Runtime 恢复抽屉
定向 Playwright；`git diff --check`、Alembic head 与任务状态唯一性检查。完成后独立提交。

完成：`AgentSessionWorkbench` 通过 `AgentSessionGateway` 注入完整宿主传输；Agent Workspace 的默认网关
仅封装现有 API、文件/终端 URL 与事件订阅。会话状态、页面渲染、Composer、事件流、能力、附件、工作目录、
文件和终端仍只有共享组件一份实现，`/agent` 不传参即保留原有行为。共享组件不再直接引用 Agent Workspace
传输函数；本切片未新增 FlowRun/节点网关，也未修改其代码。

### FR-90 Agent 会话 FlowRun 节点宿主整合设计 — DONE

依赖：`FR-89`。

目标：以现有代码和固定 Runtime 设计为证据，冻结 FlowRun/节点接入唯一 Agent 会话页面和内核的宿主合同、
数据迁移、API/路由、工作目录隔离、首发提示词、删除边界、实施切片及最终验收；明确旧 FlowRun 聊天页和
服务必须被替换而不是继续维护。此切片只建立后续实现的可审计边界，不修改业务代码、数据库或部署。

验收：设计覆盖独立 Agent Workspace 不变、同 Run 单 Runtime、节点 N 对 N 会话、节点隔离工作目录、首发
提示词、稳定 URL、无重复内核与真实部署验收；文档引用、任务状态唯一性和 `git diff --check` 通过。完成后
独立提交。

完成：新增 `docs/agent-session-flowrun-integration-design.md`，冻结唯一共享 Workbench/网关/内核与两个
宿主 adapter 的关系。FlowRun 节点只提供 Run Runtime、Attempt 门禁、服务端推导的节点工作目录和可选首发
提示词；旧 FlowRun 会话页面与重复服务被列为最终删除对象。后续实现依次收敛共享 binding/host protocol、
FlowRun adapter、完整 gateway 和共享页面装配，未在本切片修改业务代码。

### FR-91 Agent 会话共享前端宿主路由合同 — DONE

依赖：`FR-90`。

目标：将 `AgentSessionWorkbench` 的绑定 URL 解析、会话 URL 生成、根路径回退与首发恢复存储键从组件
硬编码的 `/agent` 中抽为可注入的宿主合同；一级 Agent Workspace 使用默认合同且产品行为不变。该合同只
描述页面路由与浏览器恢复边界，不能承载会话状态或传输逻辑；本切片不新增 FlowRun 页面、路由或节点代码。

验收：受影响 Web ESLint/typecheck/production build、既有 Agent Workspace 首发/URL 恢复定向 Playwright、
宿主合同依赖扫描与 `git diff --check`。完成后独立提交。

完成：新增 `AgentSessionHost`，共享 Workbench 从宿主读取根路径、会话路径、URL binding 解析和首发恢复
存储键；所有页面导航和恢复操作不再硬编码 `/agent`。默认 Agent Workspace host 保持原 URL 与
`sessionStorage` key，工作台状态和传输网关职责不变。本切片未新增 FlowRun 页面、路由或节点代码。

### FR-92 Agent 会话中性前端传输协议 — DONE

依赖：`FR-91`。

目标：将 `AgentSessionGateway` 与共享 Workbench 内部调用使用的 Agent Workspace 专有方法名收敛为中性
会话 host 协议；默认 gateway 仅逐项映射至原 Agent Workspace API，不能改变任何请求、URL、响应或产品
行为。此协议是将来 FlowRun node host 实现完整 gateway 的前置；本切片不新增 FlowRun/节点 API 或页面。

验收：受影响 Web ESLint/typecheck/production build、既有 Agent Workspace 首发/URL 恢复定向 Playwright、
内部组件不得出现专有 gateway 方法调用，以及 `git diff --check`。完成后独立提交。

完成：`AgentSessionGateway.api` 的内部协议已改为 `defaultHost`、`runtime`、`conversations`、
`workspaceDetails`、`bootstrapConversation`、`conversationEvents`、`sendMessage` 等中性会话操作；默认
Agent Workspace gateway 逐项映射到原有 API。共享 Workbench 不再调用 Agent Workspace 专有 gateway
方法，网络请求、响应、URL 和产品行为均未变化。本切片未新增 FlowRun/节点 API 或页面。

### FR-93 FlowRun 节点会话宿主解析器 — DONE

依赖：`FR-92`。

目标：在 `agent_sessions` 中建立 FlowRun/node Attempt 的唯一宿主解析器，集中验证 Run、Snapshot、节点、
Attempt 归属与启动门禁、active Runtime session 和服务端冻结工作目录，并暴露不含物理 endpoint 的宿主
上下文。旧 FlowRun 会话创建入口改用该解析器，保留公开 API 和错误语义；本切片不迁移 binding 表或新增
完整 FlowRun gateway。

验收：新增/调整 FlowRun 会话定向 pytest，受影响 Python Ruff/py_compile，`git diff --check`、Alembic head
与任务状态唯一性检查。完成后独立提交。

完成：`agent_sessions` 现有唯一 FlowRun/node 宿主解析器，集中校验 Run、Snapshot、NodeRun、Attempt、
启动门禁、active Runtime 与服务端工作目录；它返回带 Runtime/目录/策略/权限冻结副本的中性
`AgentSessionHostContext`，不暴露物理 endpoint。旧 FlowRun 会话创建入口先解析该宿主，后续仍沿用既有
OpenHands 创建路径及公开错误语义；未引入新的 binding 或页面。

### FR-94 Agent 会话内核回归基线 — DONE

依赖：`FR-92`。

目标：以一级 `/agent` 为唯一会话产品，建立可执行的完整能力回归和静态边界护栏，覆盖共享页面、共享
gateway、共享 facade、OpenHands 正式事件与默认 Agent Workspace 宿主。此切片不修改 FlowRun、节点、
Attempt、旧节点会话或 Runtime 拓扑；`FR-93` 保持待办，不能混入本切片。

验收：补齐/调整 Agent 会话定向 pytest、Web Playwright 和架构断言，覆盖首发 bootstrap、URL 刷新恢复、
流式事件、控制操作、模型/能力、附件、文件/终端和默认 Runtime 恢复；受影响 Python/Web 静态检查、
`git diff --check`、Alembic head 与任务状态唯一性检查。完成后独立提交。

### FR-95 Agent 会话中性宿主合同 — DONE

依赖：`FR-94`。

目标：在不改变 `/agent` 行为的前提下，完成共享 Web host/gateway DTO 与后端 host context 合同收敛，
使 `AgentSessionWorkbench` 和 `agent_sessions` 不再依赖默认 Agent Workspace 的私有传输或宿主语义。只
实现默认 Agent Workspace 适配器；不接入 FlowRun/节点。

验收：共享 Web gateway 显式声明中性 `AgentSessionApi`，共享 Workbench 的 query 与浏览器工具状态均通过
宿主命名空间隔离；后端 host context 复制并冻结 Runtime、工作目录、策略和权限事实，默认 Agent Workspace
仅作为其 ORM/Runtime 适配器。定向 pytest、Ruff/py_compile、Web lint/typecheck/build、既有 `/agent` 首发/
URL 恢复 Playwright、Alembic head、任务状态唯一性和 `git diff --check` 通过。

完成：默认 `/agent` 的公开路由、API URL、浏览器交互与 Runtime 行为保持不变；新增合同未引入 FlowRun、
节点、Attempt 或第二套会话页面/服务。

### FR-96 共享 Agent 会话 locator 与默认宿主所有权 — DONE

依赖：`FR-95`。

目标：将 Agent 会话 binding、能力冻结、附件投影、命令幂等与标题 projection 的 ORM 所有权从
`agent_workspaces` 收敛到 `agent_sessions`，但维持表名、SQL 身份、迁移历史、公开导入路径和 `/agent`
行为完全兼容。此切片不改变 host 字段或接入 FlowRun/节点。

验收：共享 persistence model 与历史 Agent Workspace 导入路径必须为同一个 Python ORM 类；共享会话、标题
和 shared model export 必须只引用共享映射；核心 Agent Workspace 定向 pytest、共享架构 pytest、Ruff、
py_compile、Alembic head 和 `git diff --check` 通过。无需数据库迁移，因为表定义与 SQL 身份未变。

完成：会话 binding、命令、能力冻结与附件投影已由 `agent_sessions.infrastructure.models` 唯一声明；
`agent_workspaces.infrastructure.models` 只保留兼容导出，不再声明第二套 ORM 映射。`/agent` 的数据、
API 和行为不变。

### FR-97 Agent 会话内核与默认宿主分层 — DONE

依赖：`FR-96`。

目标：让 `agent_sessions` 的 OpenHands 会话逻辑只依赖中性 host context、共享 locator 和 OpenHands bridge；
Agent Workspace 仅作为默认 Runtime、工作目录、能力、文件/终端和策略解析器及 API 适配。保持 `/agent`
全部功能和历史身份兼容，不改动 FlowRun、节点或 Attempt。

完成：共享会话内核不再导入 Agent Workspace 的 application/infrastructure 私有模块；默认工作区通过
公开宿主适配面提供 Runtime allocation、工作目录冻结和附件清理。所有历史兼容模块、ORM 导出和标题任务同样
只通过 `agent_sessions.public` 访问共享能力，公开 facade 使用惰性 application 导出避免宿主模型与共享内核
之间的导入环。未接入 FlowRun、节点或 Attempt，也未复制任意会话逻辑。

### FR-98 FlowRun 共享 locator 切换 — DONE

依赖：`FR-93`、`FR-97`。

目标：将 FlowRun 的活跃 OpenHands locator、确认审批、Run 删除和 Runtime replacement 从
`flow_run_conversation_bindings` 切换为共享 `agent_conversation_bindings`，并以 `FLOW_NODE`
宿主、FlowRun/NodeRun/Attempt lineage 与冻结工作目录隔离节点会话。

完成：新 FlowRun 会话、自动 Attempt、确认审批、Runtime replacement 和删除路径均查询共享 binding；
旧 locator 与无法证明节点 scope/目录的审批历史在 `0072_flow_node_locator` 中显式清除，而非猜测迁入。
共享 binding 的 OpenHands identity 扩展为 100 字符，且唯一活跃 locator 表由迁移检查验证。

### FR-99 FlowRun 节点共享 Agent Workbench gateway — DONE

依赖：`FR-90`、`FR-98`。

目标：为一个 FlowRun/node Attempt 提供完整的共享 Workbench 宿主 gateway、稳定 URL、事件流、文件和终端
代理；让节点会话复用唯一 `AgentSessionWorkbench`，并删除旧 FlowRun 平行聊天页、Zustand 聊天视图及其
重复 composer/timeline 状态机。

完成：节点 gateway 使用 `FLOW_NODE` binding 和服务端验证的 Run/Attempt scope，公开 host、runtime、
规范化会话、事件、消息、原生压缩、暂停/继续、文件、终端和 WebSocket 路由。Web 在
`/flow-runs/{run}/nodes/{node}/attempts/{attempt}/agent-sessions[/binding]` 装配同一个
`AgentSessionWorkbench`；节点启动控制直接创建 scoped session 并导航至该 URL。节点 adapter 显式隐藏未
实现的 Workspace 专属目录/能力/附件/模型/删除/fork/rewrite/确认操作，不会回退调用旧 FlowRun API。
`AgentChatPage`、其 CSS、Zustand `agent-chat` 视图和旧启动路径已删除。

### FR-100 能力仓库与新会话能力选择收口 — DONE

依赖：`FR-99`。

目标：在能力仓库为 MCP 提供与不可变版本模型一致的编辑入口；收紧仓库页面为固定模块导航和标题区、局部数据滚动；将 Skill 组合显示为紧凑的逻辑选择项；新建 Agent 会话默认不冻结任何能力，并允许在会话能力管理中一键展开 Skill 组合。

完成：MCP 编辑会读取冻结配置及本地脚本、禁止更改 Server 身份、经既有校验与对象存储发布新的 MCP Version，且不重绑历史消费者。能力仓库移除了重复的大标题，模块导航和操作头固定于页面视口，只有数据区滚动；Skill 组合改为单行摘要。新会话的能力选择存于浏览器草稿并在 bootstrap 时显式冻结具体 version ID，未选择时传入空集合；组合仅作为紧凑逻辑快捷项，展开并冻结其包含的真实 Skill Version。

### FR-101 环境发布中状态与终端重连隔离 — DONE

依赖：`FR-100`。

目标：环境配置终端开始发布时，服务端必须先持久化 `PUBLISHING` 会话状态，再执行容器冻结、OpenHands Runtime 打包与契约探针。发布期间不得重新附着终端、创建第二个配置会话或停止并丢弃该会话；关闭视图后重新进入应展示可关闭的后台发布状态，而非误导为终端重连。发布失败时会话恢复为可继续配置的 `RUNNING` 状态，已创建的 Version 仍按既有失败语义保留诊断。不得改变不可变镜像构建链、Runtime digest、OpenHands 或 FlowRun 契约。

验收：环境服务定向测试覆盖发布中状态、禁止终端连接/二次创建/停止、成功完成及失败回退；Web 定向 Playwright 覆盖关闭后重新进入显示发布中、不建立终端 WebSocket；受影响 Python/Web 静态检查、Alembic head、任务状态唯一性和 `git diff --check` 通过。本切片使用独立 Git commit。

### FR-102 页面局部滚动与 Skill 组合删除 — DONE

依赖：`FR-101`。

目标：大模型配置、流程编排和流程运行均固定页面标题和操作区，只允许其数据区域滚动；流程编排的资源库、画布和配置器分别使用独立滚动边界。Skill 组合仅为逻辑选择模板，删除未被运行时消费者引用的 Skill 时应自动从组合移除该版本，并在组合清空后删除该空模板；页面应刷新组合状态并说明变更。不得放宽 Agent Workspace、治理记录或系统内置策略的删除保护。

验收：受影响 Web typecheck/lint/build；Skill 组合定向 pytest 覆盖删除自动解除逻辑组合关系；`git diff --check`、任务状态唯一性和独立提交通过。

完成：模型配置与流程运行将标题/操作区固定在视口内，只有其数据区滚动；流程编排的资源库、画布和配置器在固定标题下各自占用独立滚动区域。删除 Skill 会同步移除组合中的逻辑成员，自动清理空组合，并刷新组合列表和提示信息；真实工作区、治理与内置策略引用仍继续阻止删除。

### FR-103 终端视图隐藏连接保活 — DONE

依赖：`FR-101`。

目标：关闭环境配置终端视图只能隐藏界面，不能卸载 xterm、关闭 WebSocket 或在重新打开时创建新的终端连接；终端不可用、停止丢弃或会话从服务端消失时才释放本地连接。发布期间已附着的连接可在后台保留，但重新打开的界面仍只展示发布进度，绝不重新附着。

验收：Web 定向 Playwright 覆盖发布中关闭/重新进入零次终端 WebSocket，以及运行中终端关闭/重新打开保持一次既有连接；受影响 Web typecheck、ESLint、production build 和 `git diff --check` 通过。本切片使用独立 Git commit。

### FR-104 节点会话草稿首发与工作台一致性 — DONE

依赖：`FR-103`。

目标：节点进入共享 Agent 工作台时必须直接打开浏览器草稿，不创建 OpenHands Conversation、不显示会话标题、也不在左侧列表持久化会话；首条用户消息以同一幂等命令创建、reload 并发送，随后才产生标题、绑定 URL、可用终端和正式事件。节点路径继承 FlowRun 冻结工作目录，并保持与一级 Agent 工作台一致的会话表面；唯一额外产品差异是可见且尺寸正确的“返回节点执行”按钮。修复首发的 native 初始化 NotFoundError 和客户端 optimistic/正式 user event 双重渲染。不得修改 OpenHands 源码、不得复制第二套 Workbench、不得在草稿阶段打开需要绑定的终端。

验收：节点入口/草稿首发定向 Web 回归，节点服务首发与 native reload 定向 pytest，受影响 Python/Web 静态检查、Alembic head、任务状态唯一性和 `git diff --check` 通过。本切片使用独立 Git commit。

完成：节点入口不再在进入页面时创建 binding；共享 Workbench 在节点根路径立即创建仅浏览器可见的草稿，并隐藏草稿标题。首条消息经过节点原子 bootstrap 路由，在原生 Conversation reload 后发送、以正式 cursor 完成身份确认，再持久化标题和启用绑定终端。首发 optimistic bubble 在路由切换后清除，只由正式事件渲染；返回按钮采用文字按钮尺寸，不再被通用图标动作样式压缩。

### FR-105 节点会话模型与会话可见性一致性 — DONE

依赖：`FR-104`。

目标：节点会话继续装配唯一共享 Agent 工作台，并补回与一级 Agent 会话相同的模型选择、会话标题、会话列表和
OpenHands 原生上下文信息。首条消息必须将用户所选的已连接模型供应商、模型和推理强度冻结到该节点 binding，
而非静默忽略浏览器选择或回退到其他 Workspace；同一节点 Attempt 只列出、打开和操作自己的会话。节点标题由
服务端解析的节点名称提供，不显示泛化的“FlowRun 会话”。所有会话仍复用创建 FlowRun 时预置的唯一 Runtime，
并固定在节点 Attempt 的服务端工作目录；不修改 OpenHands、不会启动新容器，也不恢复旧聊天页面。

验收：节点会话服务与共享 Workbench 的定向回归覆盖节点名称、模型首发冻结、节点作用域列表、上下文读取和
消息发送；受影响 Python/Web 静态检查、Alembic head、任务状态唯一性与 `git diff --check` 通过。本切片使用
独立 Git commit。

完成：节点侧恢复了与一级会话相同的模型选择和模型切换，首发冻结选中的 provider/model/reasoning；同一
Attempt 只能列出和操作自己的 binding，节点页显示具体节点名称。共享 Workbench 以固定 OpenHands 1.44.0
Runtime 已声明的模型窗口显示真实零基线（`Token 0 / window`、`工具 0 / 10k 事件`），首轮完成后按正式
active `usage_id` bucket 的 `per_turn_token` 更新；不再把 `0` 误判为无上下文数据。

### FR-106 FlowRun Runtime 初始化门禁与可见状态 — DONE

依赖：`FR-105`。

目标：创建 FlowRun 后立即持久化运行记录并异步预置唯一 Runtime，但浏览器必须留在运行列表。Runtime
尚未达到可写 ACTIVE 状态时，运行行明确显示“运行环境初始化中”、置灰且不可进入；Runtime 可写后自动变为
可进入。不得把 Runtime 初始化、节点启动门禁或 OpenHands Conversation 创建笼统显示为“正在创建”。流程运行
页和节点会话只允许在 Runtime 就绪后进入，因此节点首发不再承担容器冷启动等待。恢复/替换期间沿用明确的
“运行环境恢复中”不可写状态，不新增容器或改变单 FlowRun 单 Runtime 拓扑。

验收：新建 FlowRun 记录在 Runtime 启动期间可见但不可进入，状态/提示可随轮询或事件刷新；Runtime 变为
ACTIVE 后可进入流程运行页；运行页及节点启动不再承担冷启动等待。平台与 Web 定向回归、受影响静态检查、
Alembic head、任务状态唯一性和 `git diff --check` 通过。本切片使用独立 Git commit。

完成：运行摘要以 Runtime 的逻辑状态聚合为只读字段，不泄露 generation 或容器连接信息。创建成功后浏览器留在
运行列表；`STARTING`、恢复和降级状态的运行行会明确展示状态、置灰并禁止打开，轮询发现可写 `ACTIVE` 后才
恢复入口。运行环境更新时间也计入摘要更新时间，避免列表继续显示过期时间。

### FR-107 节点 Agent 会话完整功能对等 — DONE

依赖：`FR-106`。

目标：节点 Agent 会话与外层 Agent 会话在能力、交互、页面和权限语义上完全一致；唯一允许的差异是其工作
目录固定为当前 Attempt 的冻结目录。不得以 FlowRun Runtime、节点宿主、gateway feature flag、路由或服务端
拒绝为由删减任何外层 Agent 会话能力。对照共享 Workbench，节点必须同样支持草稿/正式会话的附件上传、
图片粘贴和多模态发送、文件与来源、独立终端、会话列表/标题、模型/推理强度、暂停/继续、压缩、重思考、
分叉、删除及原生 NeverConfirm 交互；唯一额外 UI 为返回节点执行。节点页显示具体节点名称。固定已知模型的
窗口容量优先于漂移的 usage bucket：`gpt-5.6-sol` 等显示 922k，并在首发前显示真实 0 基线。不得复制第二套
Workbench、启动第二个容器或改变 OpenHands 正式事件身份。

验收：外层/节点 gateway 全量能力矩阵无非宿主差异；节点附件、粘贴图片、终端、模型、上下文、控制操作与
共享 Workbench 定向回归；固定 Runtime/正式 API 验证单 Run 单 Runtime；受影响 Python/Web 静态检查、
Alembic head、任务状态唯一性与 `git diff --check` 通过。本切片使用独立 Git commit。

完成：节点 gateway 不再包含能力禁用项，所有会话操作都由带 FlowRun/Attempt 授权的薄路由转接到同一
OpenHands 原生 Runtime：附件上传/图片粘贴、草稿附件、会话删除、原生确认、分叉、重思考、暂停/恢复、
压缩、模型切换、能力加载和终端均已可用。草稿终端不再要求已创建 Conversation，仍使用唯一 FlowRun Runtime。
节点唯一保留的产品差异是服务端固定的 Attempt 工作目录：工作目录列表只投影该目录，不能创建或选择另一目录。
已知 Runtime 模型以固定 catalog 窗口优先，`gpt-5.6-sol` 的界面容量稳定为 922k；token 用量仍读取同一
active OpenHands usage bucket。

### FR-108 统一 Agent 工具集并删除 Tool Policy — DONE

依赖：`FR-107`。

目标：从能力仓库、导入/发布、节点/Runtime manifest、Agent Profile 引用、运行时契约和前端彻底移除
`TOOL_POLICY`。节点不再保存、选择或冻结任何 Agent 工具/确认配置。所有 FlowRun 节点与顶层 Agent
会话都使用同一固定的 OpenHands 1.44.0 可用 Tool 集，并在正式 `NeverConfirm` 策略下执行；不复制工具执行器，
不以 FlowWeave 私有规则模拟确认。历史含 Tool Policy 的 Snapshot 保持只读可审计或要求显式重跑，绝不猜测
转换为新的运行配置。

验收：固定 Runtime 的可用 Tool 集探针、创建/恢复/发送的正式 Agent payload、删除/拒绝旧 Tool Policy API
和能力仓库 UI 定向回归；受影响 Python/Web 静态检查、迁移和 `git diff --check` 通过。本切片使用独立 Git commit。

完成：Tool Policy 已从能力类型、导入/发布、前端能力仓库、API、节点 Runtime manifest、Agent Profile
关联和运行时编译中删除。顶层与节点会话共同使用固定 OpenHands 1.44.0 全工具集合
(`ask_oracle`、`file_editor`、`task`、`task_tool_set`、`task_tracker`、`terminal`、`workflow`、
`workflow_tool_set`)，统一传入正式 `NeverConfirm`；不再存在按节点、容器或宿主原因产生的工具差异。
历史包含已移除配置的 Snapshot fail-closed 并要求重跑，不转换其冻结语义。

### FR-109 通用节点输入输出与流程编排体验 — DONE

依赖：`FR-108`。

目标：节点资产输入输出定义从仅支持飞书 URL 扩展为正式的 `URL` 与 `FILE` 类型；运行节点时严格按照
冻结节点定义生成逐字段表单，允许绑定同类型已有产物、输入安全 HTTP(S) URL 或上传任意受大小、文件名和
MIME 约束的文件。自动提示词启动必须把字段名、类型和输入来源写入首条正式用户消息，图片继续使用
OpenHands 正式多模态内容，附件使用 Runtime 工作区上传路径，使共享 Agent Workbench 的来源区自然投影
URL、图片和文件。节点运行页新增清晰的本轮输出区，URL 可打开、文件可预览或下载，文案不再绑定飞书。
Flow Definition 删除历史飞书 Wiki 根节点字段及数据库列；流程设计器默认进入流程走向模式，同时保留独立的
流程边和端口映射边，避免将控制流错误地从数据映射猜测出来。只修改 FlowWeave，不改变单 FlowRun 单
Runtime、Snapshot 冻结和 OpenHands 正式事件/API 边界。

验收：Flow/Node Asset/Artifact schema 与迁移 upgrade/downgrade、URL 安全校验、文件上传与 Artifact Store、
节点人工/自动启动输入上下文、Runtime 输入输出契约和节点输出展示的定向回归通过；Web TypeScript、ESLint、
production build、相关 Playwright、受影响 Python 静态检查与 pytest、Alembic 唯一 head、任务状态唯一性和
`git diff --check` 通过。本切片使用独立 Git commit。

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
| 2026-08-31 | FR-109 部署修复 | OpenHands 正式 generation/Workspace 身份回归、文件输入输出闭环与 FlowRun 相关 pytest（222 passed）；受影响 Ruff、`py_compile`；Alembic 唯一 head、任务状态唯一性与 `git diff --check` | PASS：自动运行的 FILE 输入上传不再使用私有 `attempt-inputs:*` 路由或空会话标识，改为绑定 Attempt 已冻结的正式 Conversation UUID，并通过当前活跃 generation 的 `env-exec:<resource_name>` 路由调用 OpenHands Workspace API；Conversation reload 同时接受正式项目根 `/runtime/workspace/project` 与节点持久根 `/runtime/workspace/nodes`，继续拒绝其他 Runtime 路径；缺少正式 Runtime generation 时 fail closed。全量 Pyright 仍只有 FR-109 前已存在的 `flow_node_workspace.py` 4 项 unknown 与 `runtime/workspace.py` 1 项 unused import，共 5 项，未由本修复新增。唯一 Alembic head 仍为 `0077_generalized_node_io`；无 `CURRENT` 或下一切片。 |
| 2026-08-31 | FR-109 | 通用节点 IO、Artifact Store、OpenHands 首发/输出与 FlowRun 相关 pytest（205 passed）；PostgreSQL 对象存储/基线集成 pytest（9 passed）；0077 空库与历史状态 upgrade/downgrade/upgrade；受影响 Ruff/`py_compile`；OpenAPI；Web TypeScript、ESLint、production build；源码 Vite 新增流程编排断言在部署前旧 API 保存边界前通过；Alembic 唯一 head、任务状态唯一性与 `git diff --check` | PASS：节点 IO 正式支持安全 HTTP(S) URL 与 25 MiB 内任意文件/图片附件；运行表单严格来自冻结字段定义，自动启动首条正式用户消息携带字段与来源，图片使用 OpenHands 正式多模态内容，文件使用正式 Workspace API 并投影到共享来源区。节点输出区可打开 URL、预览/下载文件；流程删除飞书根节点约束并默认显示流程走向，同时保留独立控制流与产物映射。唯一 Alembic head 为 `0077_generalized_node_io`；无 `CURRENT` 或下一切片。 |
| 2026-08-30 | FR-108 | 固定工具集、Runtime 契约、会话与 replacement 定向 pytest（20 passed）；环境定向 pytest（35 passed）；受影响 Ruff/格式检查；Web TypeScript、ESLint、production build；Alembic fresh upgrade/head/current、`git diff --check` | PASS：能力仓库与 `/tool-policy-catalog` 已移除，所有新会话共享完整固定 Runtime Tool 集和 `NeverConfirm`。旧 Snapshot 不进行猜测迁移，明确要求重跑；部署迁移删除已废弃能力记录而不重写历史 Snapshot。 |
| 2026-08-30 | FR-107 | FlowRun 节点附件/API、OpenHands 上下文与共享会话定向 pytest（118 passed）；受影响 Ruff/`py_compile`；Web ESLint、TypeScript、production build；Alembic head/current、`git diff --check` 与任务状态唯一性 | PASS：节点会话 gateway 不再保留任何功能禁用；附件与图片粘贴、草稿终端、删除、原生确认、分叉、重思考、暂停/恢复、压缩、模型及能力入口均由同一 OpenHands Runtime 提供。节点文件范围和工作目录严格固定为当前 Attempt；固定 catalog 将 `gpt-5.6-sol` 显示为 922k，活跃 usage bucket 仍提供 token 用量。 |
| 2026-08-30 | FR-106 | FlowRun API 定向 pytest（36 passed）；受影响 Python `py_compile`；Web TypeScript typecheck、ESLint；Alembic head；`git diff --check` 与任务状态唯一性 | PASS：新建 FlowRun 在 Runtime `STARTING` 时返回明确的不可写摘要；运行列表保留创建记录、显示“运行环境初始化中”且禁用进入，只有 Runtime `ACTIVE` 才允许打开。恢复、替换、降级同样不允许进入，未新增 Runtime 或暴露物理容器信息。 |
| 2026-08-30 | FR-105 | OpenHands 上下文定向 pytest（2 passed）；节点会话作用域/API 定向 pytest（2 passed）；OpenHands、节点会话与 API 合并 pytest（118 passed）；受影响 Ruff 与 Python `py_compile`；Web typecheck/ESLint；源码 Vite 上一级 Agent 会话定向 Playwright（1 passed）；固定 Runtime LiteLLM catalog 探针；`git diff --check` 与任务状态唯一性 | PASS：固定 Runtime 明确给出 Codex 模型窗口（`gpt-5.4` 为 1,050,000，`gpt-5.6-*` 为 922,000）；共享 Workbench 显示可信 `0 / window` 与 `0 / 10k` 基线，正式 active usage bucket 在消息后可替换零值。节点会话复用同一 Runtime，首发模型选择与后续模型切换均冻结到 binding，列表和操作不能越过 Attempt。 |
| 2026-08-30 | FR-104 | 节点首发 API 定向 pytest（1 passed）；受影响 Python `py_compile`/Ruff；Web TypeScript typecheck、ESLint、production build；Alembic head；`git diff --check` | PASS：节点根路由保持未持久化草稿，首条消息通过 reload 后的原生 cursor 激活 binding/标题，终端仅在 binding 存在后可打开，首发 optimistic user event 不会与正式 event 重复渲染。定向 Playwright 在创建测试节点资产前被现有 `INVALID_COMMAND`（fixture 仍发送已移除的 executor/capabilities 字段）拦截，未进入本切片断言。 |
| 2026-08-30 | FR-103 | Web TypeScript typecheck、ESLint、production build；源码 Vite 定向 Playwright（2 passed）；`git diff --check` | PASS：关闭环境终端仅以 `display:none` 隐藏视图，保留 xterm 与已有 WebSocket；重新点击“继续配置”不会产生第二次 terminal attachment。发布中的会话继续只显示进度，不建立 terminal WebSocket。 |
| 2026-08-30 | FR-102 | Skill 组合定向 pytest（4 passed）；Web TypeScript typecheck、ESLint、production build；受影响 Python `py_compile` 与 `git diff --check` | PASS：三页局部滚动布局通过编译；Skill 删除不再被逻辑组合引用阻塞，自动解除组合成员并清理空组合，真实受保护引用仍保留。 |
| 2026-08-30 | FR-101 | 环境服务定向 pytest（35 passed）；Web ESLint/typecheck；发布中视图定向 Playwright（1 passed）；Alembic head/current；任务状态唯一性与 `git diff --check` | PASS：服务端先持久化 Setup Session 的 `PUBLISHING` 状态后再执行容器冻结、正式 Runtime 打包和契约探针；发布期间禁止 WebSocket 终端附着、二次创建及停止丢弃，发布失败回到 `RUNNING` 并保留失败 Version 诊断。页面关闭并重新进入时展示可关闭的后台发布视图，不会创建终端 WebSocket。唯一 Alembic head/current 均为 `0075_shared_agent_runtime_config`；无 `CURRENT` 或下一切片。 |
| 2026-08-30 | FR-100 | 受影响 Python `py_compile` 与 schema import；Web TypeScript typecheck、ESLint 和 `git diff --check` | PASS：MCP 编辑、局部滚动、紧凑 Skill 组合和草稿级能力冻结均通过静态检查。Agent Workspace/bootstrap 定向 pytest 因本机 Docker daemon 未运行而无法创建 testcontainers PostgreSQL，未将该环境前置条件失败视为代码失败。 |
| 2026-08-29 | FR-98 | FlowRun locator/节点宿主与 Runtime replacement 定向 pytest（10 passed）；PostgreSQL 空库、回退重升及历史快照迁移矩阵；受影响 Ruff/py_compile、Alembic `0072_flow_node_locator` head 与 `git diff --check` | PASS：FlowRun 的新建、读取、路由、确认、replacement 与删除均使用共享 `agent_conversation_bindings` 的 `FLOW_NODE` 行；旧 locator/无可证明节点 scope 的历史审批被显式淘汰，未保留第二套活跃会话映射。 |
| 2026-08-29 | FR-93 | FlowRun locator 与节点宿主定向 pytest（7 passed）；共享会话兼容导入与跨模块 public facade 架构 pytest（2 passed）；受影响 Ruff/py_compile；Alembic `0070_agent_caps` head 与 `git diff --check` | PASS：节点会话入口仅通过共享 `agent_sessions` 的 FlowRun/node 宿主解析器验证 Run、Snapshot、节点、Attempt、启动门禁、Runtime 和冻结工作目录；未新建第二套会话页面、binding 或服务。 |
| 2026-08-29 | FR-97 | Agent Workspace 宿主/标题定向 pytest（6 passed）与 bootstrap、目录、能力、标题定向 pytest（4 passed）；共享 Workbench/facade、宿主合同与跨模块公开 facade 架构 pytest（4 passed）；受影响 Ruff/py_compile；Alembic `0070_agent_caps` head 与 `git diff --check` | PASS：共享会话内核只经默认宿主的 public facade 获取 Workspace 事实，所有跨模块依赖门禁通过；惰性公开导出消除了宿主与核心的导入循环，`/agent` 的会话、目录、能力、标题、附件、文件与终端行为保持已有定向回归。 |
| 2026-08-29 | FR-96 | 共享/兼容 ORM identity 与唯一业务 facade 架构 pytest（8 passed）；Agent Workspace 首发、Runtime、标题、附件、模型、能力与分叉定向 pytest（21 passed）；受影响 Ruff/py_compile；Alembic `0070_agent_caps` head 与 `git diff --check` | PASS：共享会话 binding、能力、附件和命令映射只在 `agent_sessions` 声明；旧 Agent Workspace 导入仍是同一 Python class/同一 SQL table，未产生迁移或 `/agent` 行为变化。 |
| 2026-08-29 | FR-95 | 共享 host/gateway 静态边界 pytest（3 passed）；默认 Workspace adapter 与不可变 host context pytest（2 passed）；受影响 Ruff/py_compile；Web ESLint、TypeScript、production build；顶层 Agent 会话首发/URL 恢复 Playwright（1 passed）；Alembic `0070_agent_caps` head、任务状态唯一性与 `git diff --check` | PASS：共享工作台只接受显式中性 transport 与宿主命名空间，默认 Agent Workspace 是唯一已实现的宿主适配器；`/agent` 功能、路由、API 和渲染不变，未接入 FlowRun/节点。 |
| 2026-08-29 | FR-94 | 新增共享页面/后端 facade 静态边界 pytest（2 passed）；Agent Workspace 核心 runtime、bootstrap、附件/模型、确认、文件/终端与 replacement 定向 pytest（8 passed）；Agent 工作台首发/URL 恢复 Playwright（1 passed）；Web ESLint、TypeScript、production build；受影响 Ruff/py_compile；`git diff --check` | PASS：`/agent` 只通过薄路由宿主装配唯一 `AgentSessionWorkbench`，历史 Agent Workspace conversations 导入路径与 `agent_sessions` 指向同一模块对象。旧 `AgentChatPage` 仍只作为未迁移 FlowRun 页面遗留入口，不参与 `/agent` 路由；本切片未改动 FlowRun/节点代码或 Runtime 拓扑。 |
| 2026-08-29 | FR-92 | Web ESLint、typecheck、production build；定向 Playwright：顶层 Agent 会话首发/URL 恢复、Runtime 恢复时工作区抽屉；共享组件专有 gateway 方法扫描与 `git diff --check` | PASS：共享 Workbench 只使用中性 host 传输协议；默认 gateway 保持对既有 Agent Workspace API 的逐项映射，浏览器回归 2 passed。无 `CURRENT` 或下一切片。 |
| 2026-08-29 | FR-91 | Web ESLint、typecheck、production build；定向 Playwright：顶层 Agent 会话首发/URL 恢复、Runtime 恢复时工作区抽屉；共享组件路由依赖扫描与 `git diff --check` | PASS：共享 Workbench 的绑定 URL、根回退和首发恢复均由注入 host 决定；默认 Agent Workspace 仍使用原 `/agent` 路由和恢复键，浏览器回归 2 passed。无 `CURRENT` 或下一切片。 |
| 2026-08-29 | FR-90 | 当前 Agent Workspace 共享页面/facade/gateway、旧 FlowRun 页面/API/service 与固定 FlowRun Runtime 设计交叉审计；设计文档引用、任务状态唯一性与 `git diff --check` | PASS：设计明确了唯一会话页面和内核、FlowRun 仅为宿主、节点 N 对 N 会话和工作目录隔离、首发提示词与单 Run 单 Runtime；旧平行聊天页和服务不属于最终状态。无 `CURRENT` 或下一切片。 |
| 2026-08-29 | FR-89 | Web ESLint、typecheck、production build；定向 Playwright：顶层 Agent 会话首发/URL 恢复、Runtime 恢复时工作区抽屉；共享传输依赖扫描；Alembic head 与 `git diff --check` | PASS：`/agent` 仍通过默认 Agent Workspace 网关运行完整唯一工作台；共享页面的 API、文件 URL、终端 URL 和事件订阅均可由宿主注入，未复制会话状态或渲染逻辑。唯一 Alembic head 为 `0070_agent_caps`；无 `CURRENT` 或下一切片。 |
| 2026-08-29 | FR-88 | Agent Workspace 定向 pytest（会话创建、标题、事件流、能力、bootstrap、文件/终端共 26 passed）；受影响 Python `py_compile`、Ruff；全量 Pyright（0 errors）；共享/宿主导入边界扫描；Alembic head 与 `git diff --check` | PASS：会话与标题业务只有 `agent_sessions` 一份实现；旧 Agent Workspace 导入路径为同模块别名，历史 monkeypatch 与公开路由均继续指向共享实现。唯一 Alembic head 为 `0070_agent_caps`；无 `CURRENT` 或下一切片。 |
| 2026-08-29 | FR-87 | Web ESLint/typecheck/production build；定向 Playwright：顶层 Agent 会话首发与 URL 恢复、Runtime 恢复时工作区抽屉；共享模块依赖扫描；Alembic head 与 `git diff --check` | PASS：`/agent` 已只渲染唯一 `AgentSessionWorkbench`，首发 bootstrap/稳定 URL/刷新恢复和 Runtime 恢复抽屉均通过；共享模块只依赖 Agent API、共享渲染器、类型及样式，未引用 FlowRun、Node 或旧节点会话。唯一 Alembic head 为 `0070_agent_caps`；无 `CURRENT` 或下一切片。 |
| 2026-08-29 | FR-86 | 固定 OpenHands 1.44.0 Finish Tool、事件父链、原生 fork/HEAD 与 send_message 源码取证；OpenHands/Agent Workspace 合并 pytest（124 passed）；Ruff；全量 Pyright（0 errors）；Web ESLint/typecheck/production build；Agent 工作台定向 Playwright（1 passed）；Alembic head、正式事件关联扫描与 `git diff --check` | PASS：最终回复分叉统一落在完整执行边界，创建后强制验证可写；即时 Observation 落盘竞态可正常 fork；既有异常分叉保持 binding/URL 无损幂等恢复并只发送一次，正常分叉不误修复。`FinishObservation` 保留正式关联身份但不重复回复正文。唯一 Alembic head 为 `0070_agent_caps`；无 `CURRENT` 或下一切片。 |
| 2026-08-29 | FR-85 | 固定 OpenHands 1.44.0 `stop_after_attempt`、网络异常映射与正式错误事件取证；OpenHands 适配器定向 pytest（6 passed）；Ruff、Pyright；Web ESLint/typecheck/production build；当前源码 Vite 上 Agent 工作台定向 Playwright（1 passed）；Alembic head、任务状态唯一性与 `git diff --check` | PASS：主模型创建、原生模型切换与 condenser 均使用总计最多 5 次、1–4 秒退避和 20 秒单次超时；正式 `LLMServiceUnavailableError` 结束失败轮并移除“正在思考”，页面显示安全网络提示且不泄露原始连接错误。唯一 Alembic head 为 `0070_agent_caps`；无 `CURRENT` 或下一切片。 |
| 2026-08-29 | FR-84 | Agent Workspace/OpenHands 压缩定向 pytest；Runtime 终结任务恢复 pytest；受影响 Python Ruff/Pyright；Web ESLint/typecheck/production build；Agent 工作台定向 Playwright；`git diff --check`；无缓存 `make rebuild-deploy`；固定 OpenHands 1.44.0 contract check；Compose、HTTP、Alembic head/current；真实历史 Conversation `/context` 与 Runtime 恢复 | PASS：新会话使用 90% token 原生压缩、正式请求/完成父链验收、关键用户事件保护和失败回滚；旧 240 策略会话服务端与页面均只读。完整栈无缓存重建成功。部署发现并修复已成功幂等 provision 任务无法在镜像替换后重新领取的问题；健康 writer 存在时不重置，缺失时安全进入 RETRY 并恢复新 generation。最终 API/Postgres/Runtime Provider 健康、Web 200、Alembic `0070_agent_caps` head/current；原会话返回 `proactive_compaction_ratio=0.9`、阈值 `829800`、`compaction_policy_current=false`。 |
| 2026-08-29 | FR-82 | Web ESLint/typecheck/production build；源码 Vite 上 Agent Workspace 定向 Playwright（纯耗时、无等待卡、唯一动态状态、工具过程与正式终态）；Alembic head、任务状态唯一性与 `git diff --check` | PASS：运行中工作过程标题只显示耗时，大块等待模型响应提示已删除；当前状态仅在轮末显示一次，工具和终态切换保持正确。 |
| 2026-08-29 | FR-81 | Web ESLint/typecheck/production build；源码 Vite 上 Agent Workspace 定向 Playwright（未完成命令、工具结果后继续思考、正式终态）；Alembic head、任务状态唯一性与 `git diff --check` | PASS：当前轮末尾持续显示灰色动态运行提示；未完成命令显示“正在后台执行命令”，正式 Observation 到达后切换为“正在思考”，FinishAction 到达后提示消失。 |
| 2026-08-29 | FR-80 | Web ESLint/typecheck/production build；源码 Vite 上 Agent Workspace 定向 Playwright（启用 xterm 鼠标上报后的普通拖拽选区、copy 事件内容、无新增 PTY 鼠标序列）；`git diff --check`、任务状态唯一性 | PASS：会话终端普通拖拽可保留并复制 xterm 选区；tmux 鼠标上报不会再清空选区或接收该拖拽手势。 |
| 2026-08-29 | FR-79 | Web ESLint/typecheck/production build；源码 Vite 上定向 Playwright（用户 URL、图片来源、类型标记、外部回复 URL 不混入来源）；`git diff --check`、任务状态唯一性 | PASS：右侧环境摘要统一展示用户提供的链接、图片和文件来源；链接可安全打开，图片/文件复用工作区预览，不引入消息或来源持久化。 |
| 2026-08-29 | FR-78 | Web ESLint/typecheck/production build；源码 Vite 与部署后 Web 的定向 Playwright（用户输入与 Agent 回复中的 `https://…`/`www.…` 裸链接、href、`_blank`、`noopener noreferrer`）；`git diff --check`、任务状态唯一性；Web 无缓存镜像构建、强制替换和 HTTP 200 | PASS：已发送输入、Agent 回复及可见过程文本中的裸链接通过 GFM 自动识别并安全打开新标签页；流式 delta 保持纯文本，避免逐 token Markdown 重解析。部署仅替换 Web 服务，API、Worker 和 Agent Runtime 未重启。 |
| 2026-08-29 | FR-77 | 固定 OpenHands 1.44.0 Oracle/Profile 正式契约取证；原生 `ask_oracle`、结构化 Task Outcome、可选 ACP 构建与默认无 Provider 回归；最终合并 pytest 259 passed；受影响 Ruff、全量 Pyright 0 errors；Runtime 镜像 build/provenance/contract check；真实 confirmation、Condenser、Task、Oracle 与四会话原 ID reload smoke；部署后 migration、服务健康、Tool Catalog、Agent Runtime generation 26 与历史会话恢复复验；Alembic head、任务状态唯一性及 `git diff --check` | PASS：Oracle 仅由显式 Tool Policy 启用，并冻结 Runtime 级 Provider/Model 绑定；正式 Profile API 持久化到受治理的 `~/.openhands/profiles` 子目录，凭据保持 read-at-use，同名异模型冲突 fail closed，用量归入独立 `oracle:oracle` bucket。Task Observation 保留安全的结构化 Outcome。ACP Provider 默认不安装，仅显式 allowlist 安装固定版本；历史 Tool Catalog digest `b053075e…` 保持不变。部署后 API、Postgres、Runtime Provider、Worker 与 Web 正常，Agent Runtime 从 generation 25 安全滚动至 26，旧 generation 删除，历史 Conversation/Event 按原 ID 可读。 |
| 2026-08-29 | FR-76 | 固定提交 `9a24f6c8866f353042a57df0514ccc900e3a0691` 的 Profile migration/pre-flight、Provider Connection read-at-use、Secret serializer probe、subscription condenser 与标题源码取证；Runtime 镜像 contract check 行为探针；增强真实 smoke；Runtime/Agent Workspace/架构定向 pytest 142 passed；架构/源码供应 pytest 25 passed；受影响 Ruff、全量 Pyright 0 errors；任务状态唯一性与 `git diff --check` | PASS：删除镜像门禁对 Agent Profile 字段全集和 condenser 默认值表的脆弱镜像，改为实际验证 v1→v2 Profile 迁移、Provider 凭据轮换后重读、嵌套 Secret serializer 识别和 subscription condenser dispatch，并冻结正式预检及 Provider Connection HTTP 方法。FlowWeave 继续保留不可变 Snapshot/供应商引用、权限、独立 condenser 用量和调用边界 Secret Reference。远程标题 Profile 解析、调用上下文与 metadata cache 修复均晚于冻结提交，故保留 `autotitle=false`、独立标题任务、失败投影和手动标题 CAS，并以负向架构门禁防止误删。confirmation、Condenser、Task 与第二容器原 ID reload 继续通过。 |
| 2026-08-29 | FR-75 | 固定上游提交 `e38e02b38` 与 1.44.0 PubSub/WebSocket 源码取证；Runtime 镜像 contract check 行为探针；增强真实 smoke；Runtime/Agent Workspace/架构定向 pytest 141 passed；架构/源码供应 pytest 24 passed；受影响 Ruff、全量 Pyright 0 errors；任务状态唯一性与 `git diff --check` | PASS：OpenHands 仅向显式 opt-in 的 WebSocket 订阅者投递 `StreamingDeltaEvent`，普通内部订阅者不接收；FlowWeave 保留授权 relay 与隐藏推理过滤，不再承担或声明重复广播隔离。删除镜像门禁对进程级 Tool registry/module qualname 精确映射的耦合，改为实际清空 `FinishTool` 注册项后验证带 `response_schema` 的内置 Tool 仍由正式 built-in resolver 解析。平台未发现自建 Tool spec 展平旁路，故保留 Tool Policy、安全投影和正式事件身份关联。真实 confirmation、Condenser、Task 与第二容器原 ID reload 继续通过。 |
| 2026-08-29 | FR-74 | 精确源码 commit/describe 与 codeload SHA-256；四包 1.44.0 lock；Runtime 无缓存构建、镜像 provenance、contract check；增强真实 smoke（confirmation、LLM condenser、Task 子 Agent、正式 pause handoff、第二容器按三个原 ID reload 且事件 ID 序列不变）；定向平台 pytest 255 passed；契约/架构/Compose 安全 36 passed；源码供应链/Plugin resolver/架构 35 passed；Agent Workspace 46 passed；OpenAPI 基线；受影响 Ruff、全量 Pyright 0 errors；Alembic head、任务状态唯一性与 git diff --check | PASS：基线固定为 commit 9a24f6c8866f353042a57df0514ccc900e3a0691、archive SHA-256 94e0bc26a670c552f8bed2dfba048d9a5c6d7bc66778e7844009db6785da6d21 和四包 1.44.0；镜像无 overlay。正式 Tool 数增至 16，ask_oracle 仅登记为上游存在并默认禁用，未提前启用 Oracle/ACP。Plugin/MCP/Skill/Task/Condenser 与事件契约保持可用；外置 Workspace/Conversation 状态经正式 prepare-for-sandbox-pause 后由新容器恢复三个原 conversation ID，正式事件身份不变。附带修复既有跨模块内部仓储引用，改经 Catalog public façade；以纯类型收窄恢复 Pyright，并同步此前遗漏的 FR-69/FR-70 OpenAPI 快照。历史已发布 Environment/Snapshot digest 未改写。 |
| 2026-08-28 | FR-73 | Web ESLint/typecheck/production build；源码 Vite 上定向 Playwright（折叠容器内根会话行固定 38px、根与 `ai-playbook` 折叠/展开、ARIA 与独立新建入口，1 passed）；Alembic head/current、任务状态唯一性与 `git diff --check` | PASS：会话行紧凑样式选择器已随 FR-72 的内容容器迁移，恢复 38px 两行栅格、标题截断和 hover/active 样式；折叠标题与独立“+”入口保持原行为，未改变任何平台或 OpenHands 契约。 |
| 2026-08-28 | FR-72 | Web ESLint/typecheck/production build；源码 Vite 上定向 Playwright（根工作区与 `ai-playbook` 单击折叠/展开、ARIA 与独立新建入口，1 passed）；Alembic head、任务状态唯一性与 `git diff --check` | PASS：分组标题现在是可访问的折叠控件，默认展开；根、活动及归档工作目录均复用同一行为。收起只隐藏对应会话列表，“+”保持独立可用；不写入浏览器或平台状态，也不触及 OpenHands、Runtime、工作区或 FlowRun 契约。 |
| 2026-08-28 | FR-71 | Web ESLint/typecheck/production build；当前源码 Vite 上定向 Playwright（新会话 `/` 空态与“管理”入口，1 passed）；`git diff --check` 与任务状态唯一性 | PASS：新会话草稿输入 `/` 不再静默；已配置 MCP 保持候选可见，未配置时显示明确说明和默认能力管理入口。不会创建 Conversation 或写入 Runtime。 |
| 2026-08-28 | FR-70 | Agent Workspace MCP readiness 与 MCP 初始化失败清理定向 pytest（2 passed）；OpenHands MCP probe/显式 timeout 映射定向 pytest（2 passed）；Web ESLint/typecheck/production build；受影响 Python `py_compile`、Ruff check、`git diff --check`；API/Web 镜像重建、部署后 health/OpenAPI 路由检查 | PASS：MCP readiness 通过当前受管 Agent Runtime 调用固定 OpenHands `POST /api/mcp/test`，不创建 Conversation；已选 MCP 在能力管理中显示检测中、已连接或连接失败，并可重新检测。保存前复检，连接失败则拒绝保存。明确 `MCPTimeoutError` 不再被归为首条消息不确定投递，隐藏预留会被清理并返回安全 MCP 错误；真实网络响应不确定仍复用 FR-69 草稿 UUID 对账。API、Web、Runtime Provider、Worker 和 Postgres 均健康。 |
| 2026-08-28 | FR-69 | Web ESLint/typecheck/production build；源码 Vite 定向 Playwright（首发 504、刷新、同 key 对账后创建会话）；Agent Workspace bootstrap 定向 pytest（3 passed）；`git diff --check` 与任务状态唯一性 | PASS：首条消息的请求体 conversation UUID 与 Idempotency-Key 一致；模拟首次投递不确定后刷新页面，浏览器恢复同一草稿并再次使用同一 key，对账成功后进入正式会话。平台既有三条 bootstrap 语义回归全部通过，未重发 native user event。 |
| 2026-08-28 | FR-67 | Web ESLint/typecheck/production build；源码 Vite 定向 Playwright（历史会话能力入口 1 passed）；`git diff --check` 与任务状态唯一性 | PASS：`$` 有 Skill 候选与 `/` 无匹配候选时均显示管理入口；当前会话已注册 Skill 以禁用状态展示并明确不能取消，新 Skill 仍可选择注册。 |
| 2026-08-28 | FR-68 | Web ESLint/typecheck/production build；源码 Vite 桌面视口布局测量；Alembic head；`git diff --check` 与任务状态唯一性 | PASS：1440×900 下 Agent 路由外壳、文档和三栏高度均受限于视口；左侧会话列表与中间 Conversation Surface 保持独立 `overflow:auto`，左下能力入口仍在 rail 固定行，右侧摘要/工具内容保持自身滚动边界。唯一 Alembic head 为 `0070_agent_caps`。 |
| 2026-08-28 | FR-66 | Web ESLint/typecheck/production build；源码 Vite 定向 Playwright；`git diff --check` 与任务状态唯一性 | PASS：历史会话输入 `$` 和 `/` 均不再静默无候选，而是给出可点击的“管理能力”入口；模拟固定 OpenHands `load_plugin` 对无 `registered_marketplaces` 历史会话的 409 拒绝后，能力弹窗显示原生限制和新建预加载默认能力会话入口。新草稿的 `$` 候选可见，源会话未被改写。 |
| 2026-08-28 | FR-65 | 受影响平台 Ruff；`workspace.py` Pyright（0 errors）；动态 Marketplace 与 Runtime contract 定向 pytest（8 passed）；Web ESLint/typecheck/production build；固定 OpenHands Runtime `contract_check.py`；Compose 配置与 `git diff --check`；Compose 部署后的真实 Agent Workspace 会话 | PASS：新建会话 `ed30f38d-5a12-485a-9c9a-52221939959e` 空闲时追加已发布 Skill `ui-product-skill`，FlowWeave 返回冻结引用；Runtime 日志确认读取会话 Marketplace、加载 wrapper Plugin，并对正式 `POST /api/conversations/a510ef50-c499-4465-8df2-bab3057e7540/load_plugin` 返回 200。随后发送 `$ui-product-skill` 的 OpenHands 正式 user event 含 `activated_skills:["ui-product-skill"]`，Agent 返回预期结果；部署后页面输入 `$` 显示 `$ui-product-skill` 原生 Skill 候选。为隔离旧默认 MCP `remote` 的外部服务超时，验证期间暂时清空新会话默认能力，验证完成后已恢复原默认能力。API、Web 健康，唯一 Alembic head `0070_agent_caps`。 |
| 2026-08-28 | FR-64 | Web ESLint/typecheck/production build；源码 Vite 上 Agent Workspace 定向 Playwright（1 passed：刷新中恢复动态等待、工具过程、终态自动折叠）；`git diff --check` 与任务状态唯一性 | PASS：刷新中的 OpenHands 原生未就绪会话重新显示实时“正在思考”与墙钟耗时；正式工具事件按同一工作过程归组，终态到达后过程自动折叠。 |
| 2026-08-28 | FR-63 | Provider relay、Agent Workspace 空闲 WebSocket 断连与 Docker 控制面暂时不可用定向 pytest（3 passed）；受影响 Python Ruff；两条 WebSocket 路由定向 Pyright（0 errors）；`git diff --check` | PASS：浏览器刷新/断连关闭上游 async generator，Provider 终止其 `docker exec` relay；暂态 `SANDBOX_BACKEND_UNAVAILABLE` 保留 Agent Runtime 的 `RUNNING` 意图与 `ACTIVE` 状态，不删除资源、不进入 `RECONNECTING`。全量 Pyright 未作为本切片通过项：附件功能的既有未提交改动另有类型错误，未纳入本提交。无 `CURRENT` 或下一切片。 |
| 2026-08-28 | FR-62 | Web ESLint/typecheck/production build；源码 Vite 上 Agent Workspace 定向 Playwright（1 passed：标题栏无重复入口、环境信息入口保留）；Alembic head、任务状态唯一性与 `git diff --check` | PASS：标题栏不再包含“打开/关闭工作区工具”按钮；右侧环境信息摘要仍可打开工具区，工具区内关闭入口保持可用。唯一 Alembic head 为 `0068_agent_title_metadata`；无 `CURRENT` 或下一切片。 |
| 2026-08-28 | FR-61 | Web ESLint/typecheck/production build；源码 Vite 上 Agent Workspace 定向 Playwright（1 passed：多条用户消息刻度、摘要、正式 event id 定位和当前态）；Alembic head、任务状态唯一性与 `git diff --check` | PASS：左侧刻度仅从正式 user event `id` 生成，随消息高度和滚动位置更新；悬停显示截断摘要，点击平滑定位至对应用户消息。唯一 Alembic head 为 `0068_agent_title_metadata`；无 `CURRENT` 或下一切片。 |
| 2026-08-28 | FR-60 | Web ESLint/typecheck/production build；源码 Vite 上 Agent Workspace Playwright（3 passed：实时思考/流式完成、局部重思考、终端关闭确认）；平台 Ruff/Pyright；Agent Workspace/OpenHands 定向 pytest（103 passed）；Alembic head、任务状态唯一性与 `git diff --check` | PASS：请求提交即呈现可计时“正在思考”，异步标题生成不再把当前轮重置为 0 秒；delta 以浏览器动画帧合并，正式终态会收束活动状态并将 Thought 标为已完成；编辑最后 user event 时仅隐藏该正式分支后代并保留历史轮次；终端关闭不再调用浏览器原生确认框。平台全量 pytest 仍报告 23 个未改动的 API/Environment/Sandbox 失败，未伪记为通过；唯一 head `0068_agent_title_metadata`。 |
| 2026-08-28 | FR-59 | Web ESLint/typecheck/production build；Agent Workspace 定向 Playwright（3 passed）；Agent Workspace 与 Environment terminal 定向 pytest（71 passed）；平台 Ruff/Pyright；部署后真实历史工作目录会话、终端高度、最大宽度、xterm 边界、tmux 行列同步与单 Runtime 容器检查；Alembic head 与 `git diff --check` | PASS：终端占满工具区可用高度，300px 到 580px 连续拖宽后 screen/viewport 均无横向溢出；tmux `window-size=latest`，client/window 同步为 `66×25`，不再出现 manual 模式的竖线/点阵填充。正式 binding 与未发送工作目录参数互斥，历史工作目录会话工具区恢复可用。唯一 head 为 `0068_agent_title_metadata`，Agent Runtime 数量为 1；无 `CURRENT` 或下一切片。 |
| 2026-08-27 | FR-58 | Ruff format/check；Pyright strict；平台全量 pytest（479 passed）；OpenAPI 基线；PostgreSQL migration-check；Compose security；Web ESLint/typecheck/production build；固定 OpenHands `1.42.0` contract/smoke；源码与部署后 Agent Workspace 定向 Playwright（最终 3 passed）；镜像重建、API/Runtime Provider 健康、唯一 Alembic head 与 `git diff --check` | PASS：发送首条消息前可切换供应商、模型和推理强度，上传附件并使用文件树和多个独立终端；bootstrap 原子绑定工作目录、完整模型配置和附件后才创建 URL/列表项，发送前工具页签迁移到正式 binding。左栏可从项目根合法目录显式新增工作区，会话项固定为 38px 紧凑两行；关闭终端调用服务端销毁且不创建第二个 Agent Runtime。全仓 12 项浏览器套件曾有 7 项通过、5 项失败：FR-58 三项均通过；其余真实 Environment/Tool Catalog 场景因 E2E 遗留 Setup 容器将 API/Runtime Provider 拖入不健康状态而超时，服务重建恢复健康后未将这些无关场景伪记为通过。唯一 head 为 `0068_agent_title_metadata`；无 `CURRENT` 或下一切片。 |
| 2026-08-27 | FR-57（最终验收） | 已完成的迁移、静态检查、Web 构建、OpenAPI/架构、Compose、平台测试及定向 E2E；部署后 Runtime、历史会话和工作区恢复复验；用户验收 | PASS：Runtime 使用当前可用镜像恢复到可写 `ACTIVE` generation，历史会话独立加载，工作区概览和文件读取不再被 Runtime 恢复阻塞，右侧栏可正常打开并在失败时提供错误与重试。用户完成部署后验证并明确确认 FR-57 可以标记 `DONE`；无 `CURRENT` 或下一切片。 |
| 2026-08-27 | FR-57（阻塞记录） | PostgreSQL Testcontainers migration-check；Compose security；Ruff format/check；Pyright strict；Web ESLint/typecheck/production build；OpenAPI 基线与跨模块 public façade 定向门禁；源码 Vite 上 Agent Workspace 定向 Playwright | 部分 PASS：migration-check、Compose security、Ruff、Pyright、Web 静态/构建、OpenAPI/边界门禁和 Agent Workspace E2E 通过。BLOCKED：Docker daemon socket 不存在，导致 407 个需要 Testcontainers 的平台用例在 fixture 初始化失败；同时无法运行固定 OpenHands image contract/smoke、真实 Runtime 与部署后 E2E。Docker 恢复后必须从完整平台 pytest、固定 image 合同/烟雾、真实根/单/多目录会话及部署后完整 E2E 继续；FR-57 不得标记 DONE。 |
| 2026-08-27 | FR-56 | Agent Workspace 与 OpenHands 定向 pytest（98 passed）；受影响 Python Ruff；Web ESLint/typecheck；独立源码 Vite 上 Agent 工作台定向 Playwright（1 passed，覆盖概览、文件预览/下载、终端 Tab、根与草稿范围）；`git diff --check` | PASS：右侧抽屉默认收起，概览显示当前根/冻结工作目录、Git 仓库、待发送附件与不虚构凭据的 IDEA/Gateway 状态；文件树由 OpenHands 正式 archive/download API 提供，只读预览和下载均由服务端按 binding 冻结目录或仍为 ACTIVE 的草稿目录重新校验，拒绝越界、`.git`/`.openhands` 与目录下载。终端仅接收服务端解析的目录 ID，根/草稿/会话分别以有效目录或冻结 working directory 启动；该 `working_dir` 同时贯通本地 Docker 与远程 Controller。标题仅支持双击编辑，标题栏移除编辑与压缩按钮。唯一 head 保持 `0068_agent_title_metadata`；无 `CURRENT`，FR-57 为下一可执行切片。 |
| 2026-08-27 | FR-55 | Web ESLint/typecheck/production build；独立源码 Vite 上 Agent 工作台定向 Playwright（1 passed，覆盖根与工作目录草稿入口、发送前无 bootstrap/list 项、首条消息后导航）；Agent Workspace/OpenHands 定向 pytest（97 passed）；受影响 Python Ruff、平台 Pyright（0 errors）；OpenAPI 基线、Alembic head、任务状态唯一性与 `git diff --check` | PASS：左侧顶层“新建会话”固定打开根工作区草稿并与工作目录平级；根工作区和每个活动工作目录均有新建入口，已激活会话按冻结版本所属目录分组，目录归档后仍在历史分组可见。浏览器草稿不写 binding、不产生会话 URL 或列表项；刷新/离开即丢弃。首条消息才向 bootstrap API 发送所选目录与模型配置；成功后将带 `PENDING` 标题状态的 binding 插入对应分组并导航。前端移除“未命名会话 N”回退，旧/异常空标题仅显示无序号“新会话”。API 列表/ bootstrap 投影正式返回工作目录 ID，按冻结 version 而非路径文本归属。唯一 head 为 `0068_agent_title_metadata`；无 `CURRENT`，FR-56 为下一可执行切片。 |
| 2026-08-27 | FR-54 | `0068` 隔离 PostgreSQL migration-check upgrade/downgrade/upgrade；Agent Workspace 与 OpenHands 定向 pytest（97 passed，含标题独立调用、手动改名 CAS 和失败兜底）；受影响 Python Ruff、平台 Pyright（0 errors）；OpenAPI 基线；Alembic head、任务状态唯一性与 `git diff --check` | PASS：首条正式 user event 接受后，binding 立即显示由首个非空行规范化的标题并标记 `PENDING`，同时以 binding ID + generation 只投递一次标题任务。任务使用独立供应商调用（API Key Chat Completions / Codex OAuth Responses），不调用 OpenHands Runtime、不写 Conversation Event、HEAD 或上下文；临时首条文本种子在任务结束后清除。成功仅 CAS 写入本地 `display_title`/`GENERATED`；供应商失败保留首句并标记 `FALLBACK`。手动改名仍使用既有正式 OpenHands rename，并将本地标题标记 `MANUAL`、递增 generation，使所有延迟标题任务 no-op，绝不覆盖用户输入；不再生成带序号的未命名标题。唯一 head 为 `0068_agent_title_metadata`；无 `CURRENT`，FR-55 为下一可执行切片。 |
| 2026-08-29 | FR-83 | 固定 OpenHands 1.44.0 `/condense` router/service/EventService/LocalConversation 同步语义取证；Agent Workspace/OpenHands pytest 115 passed；Ruff、全量 Pyright 0 errors；Web ESLint/typecheck/production build；独立源码 Vite 与部署后 Web 定向 Playwright 各 3 passed；Alembic head/current 与 `git diff --check`；API/Worker/Web 最终镜像构建、滚动替换和服务健康；真实 Conversation `/context` | PASS：上下文进度继续只取活跃主 LLM 的正式 `per_turn_token/context_window`。新会话原生 summarizing condenser 的事件数兜底提高到 10,000；正式 View 达到 80% 时，消息发送严格按同步原生 `condense → send` 执行，压缩失败不写 user event。`/` 菜单独立展示 OpenHands 原生“压缩上下文”，不与 MCP/插件命令混排；手动压缩后旧 usage 在下一次主模型调用前隐藏并明确提示等待更新，不伪造压缩后 token。最终 API、Worker、Web 已部署，真实历史会话返回当前 View `110,275 / 922,000`、80% 阈值 `737,600` 与 `usage_current=true`；历史 condenser 的 `max_size=240` 保持不改写，新会话使用 10,000。Postgres、Runtime Provider 和现有 Agent Runtime 容器身份保持不变。 |
| 2026-08-27 | FR-53 | 0067 隔离 PostgreSQL upgrade/downgrade/upgrade；Agent Workspace 与 OpenHands 定向 pytest（94 passed，其中 bootstrap 6 项）；受影响 Python Ruff、平台 Pyright（0 errors）；OpenAPI 基线；Alembic head、任务状态唯一性与 `git diff --check` | PASS：浏览器草稿在首条消息前无 API 创建路径、无 binding、无 OpenHands Conversation、无稳定 URL 和列表项。bootstrap 以必填幂等键先保留不可见命令，再冻结根/工作目录版本和 native working directory，使用原 UUID 创建 OpenHands Conversation 并投递唯一正式 user event；收到正式事件 ID 后才激活 binding。创建响应或消息投递结果不确定时，均先用同一 UUID 重载，再以 OpenHands 正式 user `MessageEvent` ID/`parent_id` 对账，绝不按文本/顺序猜测或重发；明确发送失败调用正式 delete 并隐藏失败行。OpenHands 适配器保留所选子目录为 LocalWorkspace working_dir；旧 binding 保持根路径/版本未知而不猜测。唯一 head 为 `0067_agent_bootstrap`；无 `CURRENT`，FR-54 为下一可执行切片。 |
| 2026-08-27 | FR-52 | 0066 隔离 PostgreSQL upgrade/downgrade/upgrade；Agent Workspace 定向 pytest（25 passed，其中工作目录 11 项）；受影响 Python Ruff、平台 Pyright（0 errors）；OpenAPI 基线；Alembic head、任务状态唯一性与 `git diff --check` | PASS：默认根工作区保持 `/runtime/workspace/project` 隐式上下文且不创建默认记录；工作目录可选择一至多个普通子目录，单目录映射该目录，多目录按 OpenHands 1.42.0 单一 working directory 契约回落项目根，仅表达产品分组与导航范围。路径版本不可变，改选追加版本、改名不追加、归档保留历史；公开 CRUD 拒绝绝对路径、`..`、反斜杠、超长路径、重复、父子重叠、缺失、文件和任一层符号链接。未创建 Conversation、调用模型、修改 OpenHands 或 FlowRun。唯一 head 为 `0066_agent_work_directories`；无 `CURRENT`，FR-53 为下一可执行切片。 |
| 2026-08-27 | FR-51 | 平台 OpenHands 投影 Ruff/Pyright/pytest（62 passed）；Web lint/typecheck/production build；源码与部署后 Agent 工作台定向 Playwright（各 1 passed）；API/Web 镜像重建与强制替换；真实历史会话 API 与 Browser 复验；HTTP health、Alembic head、任务状态唯一性与 `git diff --check` | PASS：平台 REST/实时共用的安全投影补齐正式 `action_id`、`tool_call_id`、`tool_name`，真实历史事件证明 Observation 仅按正式身份关联 Action，不误用 `parent_id`。工具折叠标题明确显示运行、读取、编辑或失败动作与对象，整行带箭头且详情默认折叠；展开显示安全投影后的原命令、文件参数、结构化结果、输出和退出码，不再出现笼统“文件操作已完成”。API/Web 已重建并部署，API 健康、Web 返回 200，真实会话 25 条工具详情默认全部关闭。首次及重试的无缓存 API 构建均因 Debian 镜像源持续 502/连接失败而无法下载 `gcc-12`、`binutils-aarch64-linux-gnu`、`libdpkg-perl`；随后复用仓库已验证基础层缓存完成当前 API/Web 源码打包和强制替换，不将无缓存构建误记为成功。唯一 head/current 为 `0065_agent_model_selection`；无 `CURRENT`、`READY` 或下一切片。 |
| 2026-08-27 | FR-49 | 固定 OpenHands 1.42.0 `ActionEvent` 源码取证；平台 OpenHands 投影 Ruff/Pyright/pytest（62 passed）；Web lint/typecheck/production build；部署后生产 Web 上 Agent 工作台定向 Playwright（1 passed）；API/Web 无缓存镜像构建与强制重建；HTTP health、Alembic head、任务状态唯一性与 `git diff --check` | PASS：平台从正式事件顶层投影安全可见 `thought/summary`，隐藏 reasoning 字段继续剔除；普通 Tool Action commentary 原子接替流式 delta，工具标题显示正式 summary；同一 `FinishAction` 的 thought 位于工作过程、message 仅生成一份最终回复。长回复完成后定位到回复开头，独立悬浮的跳转控件可真正滚到底并消失。API 健康、Web 返回 200，唯一 head 为 `0065_agent_model_selection`。真实历史会话事件复验未伪造为通过：既有 Agent Runtime 的恢复任务此前因 Docker 后端不可用耗尽 20 次重试并进入 `DEAD`，当前 events/context 接口返回 503；该独立运行时恢复故障不由本切片改写数据库规避。无 `CURRENT`、`READY` 或下一切片。 |
| 2026-08-27 | FR-50 | Agent Workspace 定向 pytest（2 passed）；受影响 Python `py_compile`；`git diff --check`；Alembic head 与任务状态唯一性核对 | PASS：Agent Runtime 仅把真实 `READY` 且有后端 ID 的 generation 视为健康；残留 `RUNNING/ERROR` 资源不再阻塞 `DEAD` task 恢复。启动自愈与 Worker 周期维护均扫描最新 `DEAD` provision/recovery task，旧成功 task 不会遮蔽恢复；任务重置为 `RETRY` 后可在后端恢复时重新领取，Conversation/events/context 数据保持原生持久化。未修改数据库迁移、OpenHands 或 FlowRun 协议；无 `CURRENT`、`READY` 或下一切片。 |
| 2026-08-27 | FR-47 | Web lint/typecheck/production build；Agent 工作台定向 Playwright（1 passed）；桌面与 390px 窄视口浏览器布局检查；最终合并 Web 镜像构建部署与页面复核；`git diff --check`；Alembic head 与任务状态唯一性 | PASS：输入区模型摘要同时显示当前模型和中文思考程度，桌面与窄视口均保持思考程度可见且不与发送按钮重叠；选择后立即更新并在刷新后恢复。实际部署显示 `gpt-5.6-sol 高`，Web 返回 200、API ready，唯一 head 为 `0065_agent_model_selection`；无 `CURRENT`、`READY` 或下一切片。 |
| 2026-08-27 | FR-48 | 持久 tmux 脚本定向 pytest（1 passed）与 Python 语法检查；Agent 工作台定向 Playwright（1 passed，覆盖终端宿主/屏幕底边与 tmux SGR 滚轮报告）；Web lint/typecheck/build；`git diff --check`；Alembic head 与任务状态唯一性 | PASS：持久终端开启 tmux mouse，由 tmux 原生复制模式消费滚轮；前端不再捕获并吞掉 wheel，xterm 在 tmux 开启鼠标协议后把 SGR wheel report 通过现有终端输入通道发送，未生成 shell 上下方向键序列。抽屉标题、终端区域和 xterm 使用边框盒尺寸，终端内边距移至 FitAddon 可感知的 xterm 元素，屏幕底边和宿主底边均保持在抽屉内。唯一 head 为 `0065_agent_model_selection`；无 `CURRENT`，FR-47 为下一可执行切片。 |
| 2026-08-27 | FR-46 | Agent 工作台定向 Playwright（终端抽屉打开、有效尺寸连接、连续输出贴底、滚轮 scrollback 与 WebSocket 输入隔离）；Web lint/typecheck/build；`git diff --check`；Alembic head 与任务状态唯一性 | PASS：Agent Workspace 终端等待抽屉布局稳定并使用 FitAddon 有效尺寸后才连接 PTY，ResizeObserver 按动画帧合并尺寸更新；连接和输出在用户处于底部时保持 scrollback 贴底，用户主动上滚时不被输出拉回。终端元素捕获 wheel 事件并调用 xterm scrollLines，阻止事件进入应用鼠标报告或 shell，回归验证滚轮未产生任何 input 方向键序列。未修改 OpenHands、PTY、Runtime Provider、Workspace 持久化或 FlowRun 语义；唯一 head 为 `0065_agent_model_selection`，无 `CURRENT`、`READY` 或下一切片。 |
| 2026-08-27 | FR-45 | 0065 隔离 PostgreSQL upgrade/downgrade/upgrade；Agent Workspace 定向 pytest（13 passed）；Ruff format/check；Pyright（0 errors）；Web lint/typecheck/production build；源码 Vite 上 Agent 工作台定向 Playwright（1 passed）；Alembic head、任务状态唯一性与 `git diff --check` | PASS：Conversation binding 持久化完整的供应商、模型和推理强度；用户选择时通过独立 API 立即应用并保存，刷新从 binding 恢复。消息 body 仅保留正文与附件，每次正式 user event 前重新应用已保存配置；历史非流式 binding 可先保存选择，再由原生流式 fork 继承并应用，普通 fork 同样继承完整配置。模型浮层点击外部区域会关闭且不吞掉原控件点击。唯一 head 为 `0065_agent_model_selection`；无 `CURRENT`，FR-46 为下一可执行切片。 |
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
