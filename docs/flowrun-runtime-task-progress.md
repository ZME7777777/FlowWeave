# FlowRun OpenHands Runtime 重构进度

> 创建日期：2026-08-21
> 状态：`IN_PROGRESS`
> 当前执行切片：无
> 下一可执行切片：`FR-06`
> 架构设计：`docs/flowrun-openhands-runtime-design.md`

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
- 用户必须创建并发布自定义 Environment Version，再将其绑定到 Flow/Flow Snapshot。
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

### FR-06 OpenHands 原生状态外置与原 ID reload — READY

依赖：`FR-05`。

目标：把 Workspace、Conversation/Event、Bash Event 和 `OH_PERSISTENCE_DIR` 全部挂载到 FlowRun 持久目录；
使用稳定 secret 和原 conversation ID reload；证明 Workspace 文件和 OpenHands 正式事件 identity 跨进程
重启不变。禁止平台消息、cursor、HEAD 或空会话替代恢复。

### FR-07 generation fencing、drain 与 replacement — PENDING

依赖：`FR-06`。

目标：实现 replacement lease、N+1 预热、平台写入冻结、OpenHands 正式
`POST /api/conversations/prepare-for-sandbox-pause`、默认 45 秒 Conversation lease 协调、原 ID reload、
active generation CAS、旧 generation 隔离/停止/删除和 Worker 重启幂等。任意时刻最多一个 writer。

### FR-08 删除共享 Agent Server 和旧 Runtime 分支 — PENDING

依赖：`FR-07`。

目标：删除 Compose 顶层 Agent Server、`OPENHANDS_BASE_URL` fallback、共享 Runtime 分支、
source-container mount、Attempt/Conversation 新启容器和持久状态 tmpfs；新增架构测试防止回归。

### FR-09 Conversation 模型 FlowRun 化与去角色化 — PENDING

依赖：`FR-08`。

目标：停止并删除 AUTO/HUMAN_CREATED、节点/用户会话角色、平台 `AgentMessage` 真相、Conversation 状态机、
完整事件和 cursor 投影；只保留 locator、授权、流程引用和独立审计 actor。OpenHands `user/assistant` 只在
线路适配层存在。

### FR-10 OpenHands 能力原生加载复核 — PENDING

依赖：`FR-09`。

目标：逐项复核 Skill、MCP、Plugin、Hook、Agent Definition、Profile、Policy 和 Memory 的创建、注册、
加载、执行和恢复路径；只允许固定 OpenHands 正式字段、Loader、Tool、事件和生命周期。删除仍存在的
平台注册器、Agent 执行器、提示词/文本/私有 JSON 旁路，并以固定镜像 create/smoke 证明原生加载。

### FR-11 API、UI、历史数据与运维收口 — PENDING

依赖：`FR-10`。

目标：客户端只持 FlowRun/locator，通过 FlowWeave 授权代理连接 active generation；统一“会话、提问、
回复”和 `RECONNECTING/DEGRADED` 展示；不暴露物理 endpoint。旧 Run 只读归档或显式重跑；补齐健康、
替换、删除、保留、诊断和运维入口。

### FR-12 最终故障恢复、安全与 E2E 门禁 — PENDING

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
| 2026-08-21 | FR-00 | 固定 OpenHands 源码取证；`git status`；`alembic heads`；任务状态、引用和 whitespace 检查 | PASS：新架构与独立任务主线已冻结；分支 `feat/refactor`；唯一 head `0051_physical_delete`；无 `CURRENT`，仅 FR-01 为 `READY` |
| 2026-08-21 | 全阶段验证策略 | 文档规则一致性与 `git diff --check` | PASS：FR-01–FR-11 仅做受影响代码语法/解析/编译检查；所有业务、迁移、协议、安全、恢复、容器和 E2E 验证统一推迟到 FR-12 |
| 2026-08-21 | FR-01 | 受影响 Python `py_compile`；受影响 Web 文件定向 TypeScript 编译；Compose YAML 解析；`alembic heads`；任务状态唯一性；`git diff --check` | PASS：Flow 强制绑定用户创建的 READY Environment Version，Run/Snapshot 冻结同一版本；历史空绑定运行与会话入口 fail closed；用户 base image 与最终 Runtime Image 均按 digest 冻结，发布调用固定 OpenHands `docker.build` 并保存 provenance。静态 head 为 `0052_flow_environment`；未运行数据库、镜像构建、业务测试或服务/容器验证，统一留待 FR-12 |
| 2026-08-21 | FR-02 | 受影响 Python `py_compile`；Compose YAML 解析；`alembic heads`；任务状态唯一性；`git diff --check` | PASS：每个新 FlowRun 建立 scope 隔离、服务端推导且受权限/符号链接校验的 `workspace/project`、Conversation、Bash Event、Persistence 与只读 capability 外置目录；稳定 `OH_SECRET_KEY` 仅以加密 Secret Reference 持久化并在 Runtime 创建边界解密注入；FlowRun Runtime 不再把持久 Workspace 放入 tmpfs；创建失败回滚与受引用删除保护已实现。静态 head 为 `0053_runtime_allocation`；未运行数据库迁移、业务测试、Runtime、容器、安全或 E2E 验证，统一留待 FR-12 |
| 2026-08-21 | FR-03 | 受影响 Python `py_compile`；Compose YAML 解析；`alembic heads`；任务状态唯一性；`git diff --check` | PASS：主执行 Runtime owner 收敛为 `FLOW_RUN`，同一 Run 通过 owner 锁复用唯一 active 物理容器；Attempt/Conversation 不再创建、续租或删除自己的容器，旧 owner 只进入回收；验证与 OAuth 使用显式临时 Runtime owner；FlowRun Runtime 生命周期不受单次执行 TTL/取消控制，显式删除 Run 时才清理物理 Runtime 和受保护外置 allocation；特权 Compose 服务及入口已从 Sandbox Controller 更名为 Runtime Provider。静态 head 仍为 `0053_runtime_allocation`；未运行业务测试、服务、容器、安全、恢复或 E2E 验证，统一留待 FR-12 |
| 2026-08-22 | FR-04 | 受影响 Python 与迁移 `py_compile`；ORM metadata 导入；`alembic heads`；任务状态唯一性；`git diff --check` | PASS：每个新 FlowRun 建立 stable Runtime Session，冻结 Environment Version、Runtime Image digest 和外置 allocation；generation 按 Session 单调分配并保留物理实例审计，Session 复合外键是唯一 active-generation 真相；激活、失败和命令校验携带 generation、fence token 与双 row version 并以 CAS fail closed；Attempt/Conversation 清理不能删除 Run 级 Runtime，显式删除 Run 按物理 generation、Session、allocation 顺序解除引用。静态 head 为 `0054_runtime_sessions`；未运行数据库迁移、业务测试、Runtime、容器、安全、恢复或 E2E 验证，统一留待 FR-12 |
| 2026-08-22 | FR-05 | 受影响 Python 与迁移 `py_compile`；ORM metadata 导入；`alembic heads`；任务状态唯一性；`git diff --check` | PASS：新增 `flow_run_conversation_bindings` 最小 locator，以复合外键保证 Conversation 所属 FlowRun 与 Runtime Session 一致；自动、人工和原生 fork 的 OpenHands conversation ID 均绑定到同一 FlowRun Session；创建、消息、轮询、控制、流和终端连接每次从 locator 解析唯一 active Agent Server generation，不再把首次容器名作为路由真相；Attempt 仅保留无 FK 的 OpenHands conversation ID 执行引用。同 Run 多 Conversation 继续复用 FR-04 唯一 active generation。静态 head 为 `0055_conversation_bindings`；未运行数据库迁移、业务测试、Runtime、容器、安全、恢复或 E2E 验证，统一留待 FR-12 |
