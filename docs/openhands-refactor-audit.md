# FlowWeave OpenHands-first 基线审计与目标边界

> 审计快照：2026-08-12。当前权威实现证据来自当前工作树和
> `flowweave-openhands-runtime:1` 镜像内从摘要锁定的固定提交
> `f09e03eac772290feeb51b7d7390ffaefeca1a09` 构建的 OpenHands `1.42.0` 四包；历史 v1.40.0
> 仅保留为兼容回归基线。该提交审计时为 `v1.42.0-1-gf09e03eac`；当前阶段
> 只修改 FlowWeave，不修改 OpenHands。设计意图来自
> `openhands-agent-server-design.md` 与 `openhands-capability-enhancement-roadmap.md`。
> 本文描述目标和迁移门槛，不把尚未落地的模型误写成当前能力。

## 1. 可复现证据

| 证据 | 复现方式 | 证明范围 |
|---|---|---|
| OpenHands 镜像契约 | `make openhands-contract-check` | 固定目标归档和源码 provenance；四个 OpenHands 包均从校验后的源码构建且为 1.42.0；关键 REST 路由、请求字段、默认值、Task/Fork/Condenser 事件类型存在 |
| 平台适配器测试 | `cd services/platform && uv run pytest -q tests/test_openhands.py` | FlowWeave 对 OpenHands 请求、事件、确认、压缩和游标的翻译；T9 平台全量 449 项已通过 |
| PostgreSQL 投影测试 | `cd services/platform && uv run pytest -q tests/test_conversations.py tests/integration/test_postgres_baseline.py` | 控制面投影、幂等、CAS、迁移和恢复 |
| 静态边界 | `cd services/platform && uv run pytest -q tests/architecture/test_boundaries.py` | 模块依赖、固定镜像和锁文件约束 |

镜像探针直接读取目标源码安装产物，不使用设计文档或锁文件声明代替运行事实。已验证目标
`1.42.0` 仍满足的最小契约包括：

- Conversation 默认 `NeverConfirm`；确认响应请求只包含必填 `accept` 与可选 `reason`。
- 正式路由包括 confirmation、condense、fork、navigate、MCP test、Plugin/Skill、
  sub-agent、Goal 和 Tool catalog。
- `StartConversationRequest` 原生承载 `agent`、`agent_definitions`、`plugins`、
  `hook_config`、`confirmation_policy`、`security_analyzer`、观测 metadata 和父会话。
- `TaskAction` / `TaskObservation` / `TaskToolSet`、`AgentDefinition`、
  `CondensationRequest` / `Condensation` 均来自安装包正式类型。
- 正式空参数 `Tool(name="task_tool_set")` 经公共 registry 解析时只调用
  `TaskToolSet.create(conv_state=...)`；镜像探针直接检查生成的 `TaskManager` 没有
  confirmation handler，因此当前目标源码仍不具备可由控制面关联和恢复的异步子 Agent 确认链。
- Task Tool 仍是父进程内同步阻塞执行；`TaskExecutor.interrupt` 继承 no-op，实际阻塞线程在父
  interrupt 后继续存活。目标服务没有单 Task cancel/pause/interrupt 路由或可寻址运行身份。
- 子会话文件虽写入父会话持久目录下的 `subagents`，但 `TaskManager` 的 `task_id` 索引只存在于
  当前进程；实际镜像探针以已存在子目录启动新 Manager 后仍得到 `Task not found`，因此服务重启
  后没有正式 resume 契约。
- `OpenHandsAgentProfile` schema v2、16 个正式字段、五条 Profile 路由和
  `LaunchedAgentProfile` 已由镜像安装产物验证；`agent_profile_id` 与显式 `agent` /
  `agent_settings` 互斥，生产运行不使用可变 Server Profile Store。
- LLM Summarizing Condenser 默认值为 `max_size=240`、`max_tokens=null`、
  `keep_first=2`、`minimum_progress=0.1`、硬重置重试 5 次、缩放 0.8。
- MCP OAuth state 的正式输入位于 `server.auth.state`，成功响应在顶层返回 `oauth_state`；
  `MCPTestRequest` 本身没有 `oauth_state` 字段。FlowWeave 仅在受管 Runtime 调用边界解密注入，
  并把返回 state 立即写回加密 Secret Reference，不将 token 或 client secret 投影到普通报告。
- 固定 1.42.0 还正式提供 `/api/mcp/oauth/start`、`/api/mcp/oauth/status/{job_id}` 和
  `/api/mcp/oauth/callback/{job_id}`。FlowWeave 0040 以耐久 Authorization 绑定目标 Runtime、原生 job ID
  与预期 Secret 版本；callback URL 仅单次转发，最终 state 继续复用 Secret CAS 和整体加密边界。
- 固定 1.42.0 的 `MarketplaceRegistration` / `MarketplaceRegistry` 正式解析 Marketplace manifest、
  Plugin 条目坐标和目录仓库 `resolved_ref`。FlowWeave 0041 只把该能力用于隔离的发布前来源解析：
  目录仓库和外部 Plugin 仓库都冻结为完整 commit，双重经过 HTTPS allowlist 与安全子路径校验后
  生成规范本地 ZIP；生产 Runtime 不注册浮动 Marketplace，不使用安装态、auto-load 或运行中 attach。
- 固定 1.42.0 的 AgentSkills 使用 `KeywordTrigger` 原生激活并把名称写入
  `MessageEvent.activated_skills`；可调用 Skill 会条件挂载 `InvokeSkillTool`，正式
  `InvokeSkillAction` / `InvokeSkillObservation` 返回内容并在 `ConversationState.invoked_skills` 去重。
  FlowWeave 的结构化 Skill 选择只映射到冻结触发词，不再用平台自然语言指令模拟调用。
- 固定 1.42.0 的 `AgentContext.load_memory` 只有单一布尔开关；镜像探针实际创建 User/Project
  两层 `.openhands/memory/MEMORY.md` 并证明两者会合并，不能独立选择。缺失、I/O 或 UTF-8
  解码失败只告警后继续启动，没有期望 digest 或“必须加载成功”的正式契约。FlowWeave 已闭环
  受治理内容与 owner 隔离物化；仅受管 Docker Runtime 可按冻结运行 scope 启用，正文只经正式
  用户/项目索引路径加载，不以私有协议或提示词补足。

### 1.1 当前源码适配目标

OpenHands 1.40.0 是历史兼容与回归基线，不是当前目标能力集合。`source.lock.json` 现已冻结
目标提交 `f09e03eac772290feeb51b7d7390ffaefeca1a09`、codeload 归档 SHA-256
`a33dfae9a55732cfb6ffe0b7d5cf02b557a041bc82629df5c61459400d35c832` 和四包版本 `1.42.0`。
镜像从校验后的 `/opt/openhands-source` 构建四包，保存 source/overlay provenance，并由
`direct_url.json` 证明没有回退到 PyPI 执行包或共享工作树；当前镜像 ID 为
`sha256:1453e1a01739dc67bb8db619a83eb5825e3157297726b4227cfe4f7125244139`。FlowWeave 的产品设计、
用户流程和控制面职责是能力接入的需求来源；不以 OpenHands 能力覆盖率为目标，也不做脱离产品
场景的全量版本差异盘点。后续仅在具体产品切片发现平台重复实现、协议不正确或缺少执行面能力时，
针对固定源码和实际镜像做最小契约取证，并优先接入 OpenHands 已有正式能力。
未来可以二开 OpenHands，但必须另立任务并获得明确授权，不属于当前 T1-T9 主链。

## 2. 控制面与执行面最终边界

| 领域 | FlowWeave 唯一职责 | OpenHands 唯一职责 | 运行事实来源 |
|---|---|---|---|
| 能力 | 包、版本、digest、来源、许可、安全校验、节点绑定 | 原生 Loader、Tool 注册、Skill 激活、Plugin/MCP/Hook 执行 | 冻结 Manifest + OpenHands 事件 |
| Agent | Profile/Definition 的治理、版本冻结、允许模型与预算 | Agent/ACP、Tool loop、Task 子 Agent、Critic、Goal | 冻结 Agent Spec + OpenHands state |
| 会话 | 所有权、租约、Sandbox、业务状态、权限和审计投影 | Conversation、事件树、HEAD、fork/navigate、运行状态 | OpenHands state；FlowWeave 仅投影 |
| 安全 | Tool allow/deny、风险阈值、审批人、Secret Reference | Confirmation/Security Analyzer、原始 Action 执行 | 策略快照 + 原生 Action/Event |
| 上下文 | 输入数据边界、Memory Policy、保留/删除策略 | AgentContext、Condenser、Memory、Skill 渐进披露 | 策略快照 + 原生事件树 |
| 流程 | Flow/Snapshot/Attempt/Gate/Artifact/人工验收 | 不解释流程拓扑和业务验收 | FlowWeave |
| 资源 | Environment、网络、Workspace、容器、TTL、配额 | 在分配资源内运行 Agent | FlowWeave ledger + Runtime health |
| 观测 | Run/Attempt/Conversation 成本账本与审计关联 | token/cost/stats/trace 原始指标 | OpenHands metrics 投影 |

不允许的边界穿透：平台不得解析自由文本来模拟已有原生命令；Runtime 不得从浮动
Marketplace/最新版本决定生产能力；Secret 不得进入 Snapshot、普通 JSON、消息或日志。

## 3. 当前能力审计矩阵

状态含义：`已接入` 表示原生协议闭环且有耐久投影；`部分` 表示只完成协议的一部分；
`重复` 表示平台仍在模拟 OpenHands 已有能力；`缺失` 表示尚无产品链路。

| 能力 | 当前证据 | 状态 | 下一退出条件 |
|---|---|---|---|
| Confirmation | 原生批次 digest、整批决策、CAS、恢复、UI | 已接入 | 保持镜像契约与真实服务 E2E |
| Condenser | 配置冻结、1.40.0 映射、事件投影、手动命令、崩溃重放与真实 smoke | 已接入 | 保持重放测试与固定镜像 E2E |
| 耐久事件 | REST page cursor、投影幂等、通知丢失和 anchor 消失补偿；T6.13–T6.14 新增 Worker lease 独占的 Conversation/Bash WS 首帧认证唤醒，Conversation 只加速 REST poll，Bash 由正式 REST `timestamp__gte` 和稳定 `(timestamp,event_id)` 补偿；直接 Bash 仅投影脱敏 identity 并标记 `HUMAN_OR_SYSTEM`，不保存 stdout/stderr | 已接入 | T9 保持断线、丢帧、重复、多 Worker 与 Sandbox 回收无缺失无重复 |
| Runtime Agent Spec | Snapshot 冻结 Version/digest 并编译单一 `RuntimeAgentSpec`；Adapter 不补隐式能力 | 已接入 | 保持重放、漂移拒绝与固定镜像契约 |
| Tool Policy/Catalog | schema v2 不可变 Policy 冻结固定 1.42.0/source commit/catalog digest、15 项正式 Tool、create 参数边界、读写/控制分类、确认和并发契约；未知/禁用 Tool、动态 module、过时策略均 fail closed；Web 编辑器展示有效治理与 Browser policy-disabled 原因，不提供无效开关 | 已接入 | T9 安全、恢复与真实 Runtime 回归通过；MCP schema 仍等待正式接口 |
| Capability Version | Package/Version/Blob/Dependency/Validation 已上线；Import 仅作来源审计 | 已接入 | 完成剩余能力类型和 Secret Reference 治理 |
| Skill | 不可变内容以 AgentSkills 进入 `AgentContext`；结构化选择映射为正式 `KeywordTrigger`，原生 `activated_skills` 与 `InvokeSkillAction` / `InvokeSkillObservation` 已投影；系统提示不再披露正文、目录或脚本旁路 | 已接入 | 保持固定镜像 activation/invoke 契约与 T9 真服务回归 |
| Plugin | 不可变 Version/file digest、只读物化、原生 `plugins`；ambient 与动态加载旁路已关闭；固定 Marketplace 目录可浏览并选择条目，目录 commit、实际 Plugin commit 与内容 digest 分层展示和冻结；发布前不安装，生产仍只加载本地不可变对象 | 已接入 | T9 保持正式 Registry/Loader、来源冻结与供应链回归 |
| MCP | 原生执行已接；指定 Environment Version 的受管 Runtime 通过正式 `/api/mcp/test` 投影连接结果、Tool 名称和脱敏只读试调用；0038 保存验证审计；0039 以加密 Secret Reference 闭环正式 OAuth state 往返、刷新 CAS、撤销、脱敏审计、完整性/泄漏拒绝与环境引用保护；0040 以耐久 job、双版本 fencing 和 post-commit 清理接入正式异步首次浏览器授权；正式接口不提供 Tool schema，平台不伪造 | 部分 | 未来仅在 OpenHands 正式接口提供 schema 后补投影 |
| Hook | 固定 OpenHands 1.42.0/source commit 的 Hook Set schema v1、六类正式事件、脚本清单/hash、只读物化和 `runtime_mutation=FORBIDDEN` 随不可变 Version/Manifest 冻结；旧版本和治理元数据漂移 fail closed，不接运行时全局 Hooks API | 已接入 | T9 保持真实 Hook 加载、脚本漂移和权限回归 |
| 子 Agent | Agent Definition/Task Tool 原生请求与 `TaskAction`/`TaskObservation` 耐久投影；旧控制 JSON 执行器已删除；usage/cost、预算、UI、可见性恢复、取消 fail-closed、正式事件重新对账和受管 Runtime 耐久清理已闭环；目标 1.42.0 镜像已确认单 Task 取消、异步确认和重启 resume 三项正式契约仍缺失 | 部分 | 保持共享 Runtime 重新对账、受管 Runtime 整体清理和 `never_confirm` 限制；未来能力需独立授权 OpenHands 二开，不在 FlowWeave 伪造协议 |
| Fork | 同 Runtime/Snapshot 使用正式 `/fork`，冻结源 HEAD/Event、目标 identity、metrics 处置并耐久恢复；Semantic Fork 是必须显式选择并确认六类 Runtime 状态损失的可见文本副本，不会由原生 Fork 静默降级，审计保存来源身份与损失清单 | 部分 | T9 行为、恢复竞态与固定 Runtime 契约回归通过；Navigate 仍为 `SKIP` |
| Memory | Policy Version、Snapshot 固定 source_refs、0042–0044 内容/治理/保留状态机及隔离物化已闭环。启动时只解析当前 Snapshot hold 覆盖且 `ACTIVE + APPROVED + PASSED`、`version_id + digest` 完全匹配的 Version；ATTEMPT/CONVERSATION scope 独立授权，USER/PROJECT tier 进入 owner 隔离只读索引，并由受管 Docker bind/named-volume 子路径挂载到 OpenHands 正式用户/项目 Memory 路径。正文不进入普通 DTO/Manifest/事件/审计，mock、非 Docker、缺 Environment、路径/digest/UTF-8 漂移均 fail closed；Sandbox 物理删除后才清理源目录。目标 1.42.0 原生单开关仍不可分离 tier 且读取失败静默降级 | 已接入 | T9 加载、删除恢复与泄漏回归通过；上游双 tier 单开关限制继续 fail closed |
| 成本/Trace | Task 子 Agent 已有 `(conversation_id, task_id)` 累计账本、预算审计与 UI；父 Conversation/Run/Attempt 全量账本和 Trace 未实现，T6.09–T6.12 已由用户选择跳过 | 部分 | 当前主链不再实现；仅保留现有 Task 子 Agent usage 能力 |
| Browser / 直接 Bash | 原生 Tool 与 Bash 事件身份基础存在；T6.17–T6.19、T7.09 已标记为 `SKIP` | 缺失 / SKIP | 当前主链不实现 Browser 或直接 Bash 操作者通道 |
| Critic/Goal | Critic Policy Version 冻结评分阈值/最多 2 次精炼并映射 `AgentFinishedCritic`；正式 Action/Message `critic_result` 以事件 ID 幂等投影得分。Goal 正式 start/stop/resume、`ConversationStateUpdateEvent(key="goal")`、轮次/Token/金额预算、人工操作和 dispatch/recovery fence 已耐久化；活跃 Goal 期间普通消息 fail closed，END Gate 仍独立。内置 Critic 无独立 LLM 调用，固定契约没有 critic 专用 usage bucket，未伪造费用 | 已接入 | T9 行为、恢复、预算与固定契约回归通过；若未来启用 APIBasedCritic，须先获得正式 usage 归属契约 |
| ask_agent | 正式无状态 `POST .../ask_agent` 由后台调用；诊断实体冻结 actor、问题 digest/长度、超时、输出分类和 `ask-agent-llm` usage 增量，结果按 actor 读取且不写 Conversation 消息/事件树；完成/失败清除原问题，无上游幂等键时用 RUNNING fence 避免未知结果重复收费 | 已接入 | T9 权限、恢复、费用增量和消息/事件树不变回归通过 |
| ACP | 当前固定 `Agent`；Codex OAuth 只是 LLM；T7.03–T7.08 已标记为 `SKIP` | 缺失 / SKIP | 当前主链不实现 ACP Agent |
| Agent Profile | Profile schema v2 的 16 字段兼容矩阵、无 Secret 不可变 Version、追加式修订/复制/退役、固定 Policy UUID、显式 Agent 物化、运行 provenance，以及固定 Version/digest → 新 Snapshot/Attempt 的预览/切换/回滚审计已闭环；Web 可查看版本差异/绑定并显式创建新 Snapshot/Attempt，既有执行不热改 | 已接入 | T9 行为、恢复与固定 Runtime provenance 回归通过 |
| IDE/File/Git/Workspace/Trajectory | 未形成受治理产品链；T7.10–T7.15 已标记为 `SKIP` | 缺失 / SKIP | 当前主链不实现这些直接 Runtime API 与 IDE/Desktop 访问链 |

## 4. 目标模块与端口

### 4.1 模块所有权

~~~text
catalog/       Capability Package/Version/Blob/Dependency/Validation/Collection
policies/      Tool/Context/Memory/Critic/Budget/Security Policy Version
manifests/     编译并冻结 Snapshot Runtime Manifest 与 RuntimeAgentSpec
conversations/所有权、命令、Runtime 映射和耐久业务投影
runtime/       纯 OpenHands 协议适配器；不拥有业务状态和能力版本
sandboxes/     Runtime allocation、网络、Workspace、TTL、资源账本
orchestration/ Flow/Attempt/Gate/Artifact 与后台任务协调
observability/ usage/cost/trace/security ledger 与脱敏
~~~

跨模块只通过 `public.py`；Runtime Adapter 不读取 catalog ORM，Manifest compiler 不发 HTTP，
Conversation projector 不执行 Tool。

### 4.2 RuntimePort 目标

`StartAttemptRequest` 只保留 FlowWeave execution key、输入/输出契约、Workspace allocation，
并引用已经编译好的 `RuntimeAgentSpec`。端口覆盖 OpenHands 正式协议：create/start、
read/stream/inspect、pause/run/interrupt/cancel、confirmation、condense、stats、fork/navigate、
MCP probe、Tool catalog、受控 Plugin load、final response 和 trajectory export。

所有命令必须携带业务幂等键或 expected digest/version；所有事件必须包含稳定 runtime
event ID、schema version 和脱敏后的 payload。WebSocket 只唤醒，REST event cursor 才是耐久事实。

## 5. 目标数据库 Schema

Phase 1 新增（名称为目标，不代表当前已存在）：

~~~text
capability_packages(id, stable_key, type, owner, visibility, lifecycle_state)
capability_versions(id, package_id, version, source_kind, source_ref, resolved_commit,
                    content_digest, manifest_json, compatibility_json, validation_state)
capability_blobs(version_id, path, storage_key, sha256, size, executable, media_type)
capability_dependencies(version_id, dependency_key, constraint, resolved_version_id, kind)
capability_validations(version_id, environment_version_id, kind, status,
                       summary_json, tool_catalog_json, started_at, completed_at)
node_capability_bindings(node_asset_id, capability_version_id, alias, enabled,
                         config_override_json, position, row_version)
runtime_policy_versions(id, policy_type, version, policy_json, digest)
snapshot_runtime_manifests(snapshot_id, node_key, schema_version, manifest_json, digest)
~~~

后续投影按能力增加 `runtime_branches`、通用 `runtime_usage_ledger`、
`runtime_security_events`。它们保存平台需要查询/审批的最小投影，不复制完整 OpenHands
event store。Secret 字段只能保存 Secret Reference ID。

T5 已先落地专用 `runtime_subagent_task_usage`：一条原生 Task identity 一条累计账本，
通过正式 `TaskObservation.task_id` 与生命周期 invocation 关联。OpenHands resume 复用 task_id，
因此账本指向最新 invocation 并以累计替换更新，不把同一 child 指标复制到每次调用，也不再
合入父级总数造成双计。只保存 snapshot digest、cursor、累计 cost/token 和冻结预算事实；
不保存 prompt、Secret、逐次 latency/cost history 或私有子会话。

## 6. 迁移和删除清单

采用“新表 → 回填 → 双读比较 → 新写单写 → Snapshot Manifest 切换 → 停止旧写 → 删除”。
历史 Snapshot 永不原地改变执行后端。

| 待删除/降级机制 | 删除前的强证据 |
|---|---|
| Finish delegation JSON、`_delegation_tasks`、平台子 Agent 执行器及委派提示词 | 新 Snapshot 原生 Task 成功/失败/取消/恢复/确认/预算 E2E；旧 Snapshot 只读兼容 |
| `conversation_history` 高保真声明 | 原生 fork 映射与事件树测试；保留名为 Semantic Fork 的显式降级 |
| `$Skill` 自然语言强制前缀 | 正式 Skill invoke/activation 命令和事件投影 |
| 固定三个 Tool | Tool Catalog/Policy 版本化且未知 Tool fail closed |
| `CapabilityImport` 永久引用、`NodeCapabilityRef.normalized_config` | 所有节点绑定已回填具体 Capability Version/digest |
| Skill-only Collection | 通用 Capability Collection 完成迁移 |
| 旧能力类型限制 `SKILL/MCP/HOOK` | Plugin/Agent Definition/Policy 类型进入 API、Manifest 和 UI |
| 普通 human resume 处理原生确认 | 已完成；架构测试持续禁止回归 |

旧迁移不在中途随意删除；发布前重建基线时合并 0024 以后未发布迁移，并用旧库升级和
空库安装双路径证明一致。

## 7. 分阶段验收矩阵

验收采用两层门禁：T1-T8 只执行格式/语法、最窄类型或编译、API schema 可生成、迁移可加载到
唯一 head 和 `git diff --check` 等基本代码门禁；行为单元、集成、恢复/竞态、Web 生产构建、历史
迁移矩阵、真实 Runtime、OpenHands smoke 和 E2E 统一在 T9 执行。安全/fail-closed 改动只当场
证明最小拒绝路径，新增 OpenHands 契约假设只用固定源码或最小镜像探针证明存在，不在普通切片
扩张成功能验收。
仓库级 `.agents/skills/flowweave-refactor` 固化跨窗口恢复、单原子切片执行、防重跑和门禁分层
流程。详细顺序、状态和验收条件只在任务清单的“第 11 章覆盖账本”及编号化原子队列中维护；
本文只保存实现事实和退出索引，避免双写执行计划。`DONE`/`IMPLEMENTED` 只表示代码落地并通过
基本代码门禁，不表示功能已经验收；T1-T5 的既有验证证据继续保留但不要求普通切片重复。最终
`COMPLETE` 统一由 T9 按两份设计文档和当前工作树的集中功能证据决定。

| Phase | 必须证明的结果 | 权威验收 |
|---|---|---|
| 0 协议正确性 | Confirmation、Condenser、耐久 cursor、固定目标源码契约 | 镜像探针 + 真服务断线/恢复 E2E |
| 1 能力模型 | 任一 Snapshot 可重放完全相同 Agent/Tool/能力 | Manifest digest、迁移双读、重放测试 |
| 2 MCP/Plugin | 上线前目标环境验证，生产不加载浮动远程内容 | MCP/Plugin 真容器与 Secret 泄漏测试；MCP 名称目录已闭环，schema 正式缺口保持 fail closed |
| 3 子 Agent | 父 Agent 无控制 JSON，原生 Task 全程可治理 | 成功/失败/取消/恢复/确认/预算 E2E |
| 4 产品运行时基础 | Memory、原生事件树、成本对账、实时唤醒、Tool Policy、Browser 安全闭环 | T9 统一执行 fork/cursor/cost/network/Browser 功能与集成测试 |
| 5 高级产品能力 | Profile、ACP、直接 Runtime API、IDE/Desktop、Hook、Critic/Goal 与能力协商逐项闭环 | T9 统一执行配置、运行、恢复、审计、成本与安全功能验收 |
| 6 产品收口 | 第 11 章能力具备正式 API、可理解 UI 和一致文档 | T9 统一执行 OpenAPI 行为、Web build 和关键 UI E2E |

第 11 章中由用户确认为 FlowWeave 产品必做的能力，不允许用“暂不接入”关闭：Tool Action
确认、长会话上下文、MCP 验证与 OAuth、费用与可观测性、实时事件、Conversation 分支、
Browser、Agent 工具集与 Tool Policy、原生子 Agent、Skills/Plugins/Marketplace、Agent/LLM
Profile、ACP Agent，以及直接 Bash/File/Git/Workspace/Trajectory Runtime API。正式 OpenHands
能力缺失时只能在任务清单中登记 `UPSTREAM_BLOCKED`、保持 fail closed 并写明解锁条件。

## 8. 最终状态

T8.01–T8.09 已完成实现和 T9 验收并转为 `COMPLETE`：Marketplace 只浏览固定目录并冻结双层
provenance；Tool Policy、Profile、Fork、Task usage、WebSocket/REST 恢复、Critic/Goal 与
`ask_agent` 均有与正式 API 对齐的可理解 UI。Browser、ACP、IDE/Desktop、直接 Runtime API、
Navigate 和父级 Trace 不因状态卡片而视为实现，继续保持既有 `SKIP`；MCP Tool schema 与子 Agent
单 Task 控制继续保持 `UPSTREAM_BLOCKED` 和 fail closed。

T9.01 已完成生产 Web build、平台 449 项全量测试、迁移矩阵、Compose/平台镜像安全检查、
Sandbox Controller Python/JavaScript smoke、固定 1.42.0 Runtime 契约与真实
Confirmation/Condenser/Task smoke、恢复与安全矩阵以及 5 项隔离产品 E2E。T1–T9 均为
`COMPLETE`，当前无执行批次；既有 `SKIP` 和四项 `UPSTREAM_BLOCKED` 继续按原决定保留。
