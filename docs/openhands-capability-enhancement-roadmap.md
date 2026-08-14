# FlowWeave × OpenHands 能力补齐与演进路线图

> 本文是后续实施依据，描述 FlowWeave 应如何补齐 OpenHands Agent Server 已有但尚未使用、仅部分使用或重复实现的能力。当前实现事实请参见 `docs/openhands-agent-server-design.md`。
>
> 路由、请求字段和默认值以 FlowWeave 镜像实际安装的 OpenHands Agent Server / SDK / Tools `1.40.0` 为准；`/Users/zhengmengen/WorkSpace/openhands/software-agent-sdk` 当前检出点 `v1.39.1-6-g6d597ff7` 用于解释内部机制。版本不一致时，以 `1.40.0` OpenAPI 和安装包为最终依据。

## 1. 文档目标

本文解决三个问题：

1. **差异是什么**：OpenHands 已经提供了什么，FlowWeave 当前用了多少，哪些地方语义不完整或重复实现。
2. **目标怎么定**：哪些能力必须直接复用 OpenHands，哪些仍由 FlowWeave 作为控制面治理，二者如何避免形成两套事实来源。
3. **后续怎么做**：给出数据模型、RuntimePort、API、UI、迁移顺序、验收标准和旧实现退场条件。

本文不是“可选功能清单”。除明确标为暂缓的项目外，能力仓库、原生子 Agent、Profile、Plugin、Tool Policy、上下文压缩、原生确认等均属于目标能力。

## 2. 核心架构决策

### 2.1 OpenHands-first 原则

以后新增 Agent 能力时遵循以下顺序：

1. 先确认 OpenHands 目标版本是否已有原生模型、Tool、事件或 API；
2. 已有能力优先通过 OpenHands 原生配置或协议接入；
3. FlowWeave 只补控制面能力：版本冻结、权限、策略、审批、密钥引用、审计、资源隔离、业务状态和产物投影；
4. OpenHands 缺少平台可见性时，优先扩展适配器或向 OpenHands 上游补事件/API；
5. 不再用提示词约定、自由文本 JSON 或重复执行器长期替代 OpenHands 已有的正式协议。

### 2.2 控制面与执行面边界

| 领域 | FlowWeave 控制面 | OpenHands 执行面 | 事实来源 |
|---|---|---|---|
| 流程 | Flow、Snapshot、Node Run、Attempt、Gate、人工验收 | 不感知流程拓扑 | FlowWeave |
| 能力目录 | 来源、版本、digest、审批、绑定、策略、兼容性 | 加载 Skill、Plugin、MCP、Hook、Tool、Agent Definition | FlowWeave 版本记录 |
| Agent 会话 | 所有权、业务状态、队列、租约、Sandbox、审计 | Conversation、事件树、Agent state、Tool loop | 双方映射，运行状态以 OpenHands 为准 |
| Tool | 节点级允许/禁止、风险与确认策略 | Tool 注册、选择、执行、Observation | OpenHands 事件 + FlowWeave 策略快照 |
| 子 Agent | 配额、权限、可见性、成本、取消传播 | Task Tool、Agent Definition、子会话执行 | OpenHands 原生执行，FlowWeave 投影 |
| 模型 | Provider、凭据、允许模型、预算 | LLM/ACP 会话、切换、Token 统计 | FlowWeave 配置；OpenHands 运行指标 |
| 上下文 | Snapshot 输入、数据边界、Memory Policy | AgentContext、Condenser、Memory、Skill 激活 | FlowWeave 策略 + OpenHands运行状态 |
| 产物 | Artifact Version、输出契约、验收 | Workspace 文件、Git diff、Finish | FlowWeave |

### 2.3 目标架构

~~~mermaid
flowchart LR
    U["用户 / Web"] --> API["FlowWeave API"]
    API --> CAT[("Capability Catalog + Versions")]
    API --> DB[("Run / Conversation / Audit")]
    W["FlowWeave Worker"] --> DB
    W --> CTRL["Sandbox Controller"]
    CTRL --> OH["OpenHands Agent Runtime"]
    CAT -->|"冻结 manifest + digest"| W
    W -->|"Agent/Profile/Tool/Policy spec"| OH
    OH --> SK["Skills / Plugins / MCP / Hooks"]
    OH --> SA["Native Task Subagents"]
    OH --> TOOLS["Terminal / Files / Browser / Workflow"]
    OH --> CTX["Condenser / Memory / Critic"]
    OH -->|"events / stats / confirmations"| W
    W -->|"durable projection"| DB
    DB --> U
~~~

关键点是：能力在 FlowWeave 中被治理和冻结，但在 OpenHands 中原生运行。FlowWeave 不把 Skill 改造成自己的函数执行器，也不把原生 Task 子 Agent 改成提示词协议。

## 3. 当前差异总览

### 3.1 状态定义

| 状态 | 定义 |
|---|---|
| 已接入 | 使用 OpenHands 原生能力，关键协议闭环并有持久化投影 |
| 部分接入 | 已调用部分接口或消费部分事件，但缺少完整语义、治理或恢复 |
| 重复实现 | FlowWeave 用自建机制实现了 OpenHands 已有能力 |
| 未接入 | OpenHands 能力没有进入 Runtime 配置或 FlowWeave 产品 |
| 暂缓 | 有能力但当前收益低、风险高或与产品定位不匹配 |

### 3.2 能力差异矩阵

| 能力域 | OpenHands 原生能力 | FlowWeave 当前实现 | 状态 | 目标 | 优先级 |
|---|---|---|---|---|---|
| Tool Action 确认 | Confirmation Policy、Security Analyzer、当前待处理动作批次的批准/拒绝 | 只把等待状态映射成人工输入，再发送普通消息 | 部分接入且语义错误 | 原生确认闭环 + 平台审计 | P0 |
| Context Condenser | Rolling、LLM Summarizing、Pipeline、手动 condense | 未配置 | 未接入 | 节点/会话级压缩策略 | P0 |
| 长期 Memory | 用户/项目 Memory 索引 | 未启用 | 未接入 | 受控作用域 Memory | P1 |
| MCP 验证 | 连接、tools/list、只读试调用、OAuth | 仅静态 JSON/脚本校验 | 部分接入 | 目标环境真实验证 | P1 |
| 成本与统计 | Token、cost、模型、子 Agent/Condenser 指标 | 未持久化汇总 | 未接入 | Run/Attempt/Conversation 成本账本 | P1 |
| 实时事件 | Conversation WebSocket、历史补发 | 已有可见文本 delta 代理；耐久投影仍轮询 | 部分接入 | WS 唤醒 + REST cursor 补偿 | P1 |
| Tool Catalog | Browser、grep/glob、workflow/task 等 | 固定 terminal/file_editor/task_tracker | 部分接入 | 节点级 Tool Policy | P1 |
| Browser | 网页交互、截图、录制 | 未注册 | 未接入 | 受网络和确认策略治理 | P1 |
| Conversation fork | 原生事件树 fork/navigate | 序列化可见文本创建新会话 | 重复实现 | 同 Runtime 优先原生 fork | P1 |
| 原生子 Agent | Task Tool、Agent Definition、resume、指标回收 | Finish 文本中的控制 JSON，平台另建会话 | 重复实现 | OpenHands 原生 Task 执行 + 平台投影 | P1 |
| Agent Definition | 内置/项目/用户 Agent、Tool/Skill/MCP/Hook/预算 | 无正式能力类型 | 未接入 | 纳入能力仓库和 Snapshot | P1 |
| Skills 生命周期 | 安装、发现、启停、刷新、Marketplace | ZIP 导入和 Node 绑定 | 重复实现一部分 | FlowWeave 治理版本，OpenHands 原生加载 | P1 |
| Plugins | 一包贡献 Skill/MCP/Hook/Agent | 没有 Plugin 类型 | 未接入 | 一等能力包 | P1 |
| Agent Profile | Profile CRUD、激活、切换、物化 | Node executor + Model Provider | 重复实现一部分 | 导入/冻结为节点 Agent Spec | P2 |
| Critic | 评分、自动精炼 | END Gate/人工 Reject | 重复职责 | Runtime 内自修复，Gate 保持独立 | P2 |
| Goal loop | 同会话目标审计循环 | 无 | 未接入 | 受预算约束的可选模式 | P2 |
| ACP Agent | Codex/Claude/Gemini ACP、session resume | 仅原生 Agent + LLM | 未接入 | 第二种 Agent Kind | P2 |
| File/Git/Trajectory | diff、commits、archive、trajectory | 共享目录和部分附件接口 | 部分重叠 | 只读审阅与导出能力 | P2 |
| VSCode/Desktop | Runtime IDE/桌面 URL | 未接入 | 未接入 | 安全反向代理后按需开放 | P3 |
| Warm pool | deferred init、POST /api/init | 完整启动动态 Runtime | 未接入 | 规模化后的冷启动优化 | P3 |
| OpenAI Compatibility | `/v1/chat/completions` | 使用 Conversation API | 未接入 | 不接入 | 不需要 |

## 4. 统一能力仓库目标设计

### 4.1 为什么现有模型不够

当前 `NodeCapabilityRef` 只支持 `SKILL`、`MCP`、`HOOK`，配置和版本主要依赖 `CapabilityImport.normalized_config`。这能支撑导入，但不足以表达：

- Plugin 同时携带 Skill、MCP、Hook 和 Agent Definition；
- Agent Definition 的模型继承、Tool、Skill、MCP、Hook、Condenser、预算和确认策略；
- Tool Policy 与版本；
- Marketplace/Git/本地包来源、commit、digest、签名和许可证；
- 能力之间的依赖和冲突；
- OpenHands 版本兼容范围；
- 已验证环境、Tool 清单和安全评估；
- Snapshot 应冻结的完整 Runtime manifest。

### 4.2 目标能力类型

能力仓库至少支持：

| 类型 | OpenHands 对应物 | 说明 |
|---|---|---|
| SKILL | `Skill` | 指令、references、scripts、资源 |
| PLUGIN | `Plugin` | 可组合 Skill、MCP、Hook、Agent Definition |
| MCP_SERVER | `MCPServer` | 远程或 stdio Server |
| HOOK_SET | `HookConfig` | 生命周期/Tool Hook |
| AGENT_DEFINITION | `AgentDefinition` | 原生子 Agent 或节点 Agent 模板 |
| TOOL_POLICY | `Tool` 列表 + 参数/确认策略 | 控制节点可用工具 |
| AGENT_PROFILE | `Agent Profile` | 可导入、解析并冻结的 Agent 配置模板 |
| MEMORY_POLICY | `AgentContext.load_memory` 等 | 规定 Memory 来源和作用域，不直接保存 Memory 正文 |
| CONTEXT_POLICY | `Condenser` + AgentContext | 压缩、项目规则和消息后缀策略 |
| CRITIC_POLICY | `Critic` | 自评与精炼策略 |

### 4.3 建议数据模型

不要继续把所有信息塞进 `normalized_config`。建议演进为：

~~~text
capability_packages
  id, stable_key, type, name, description, owner, visibility, lifecycle_state

capability_versions
  id, package_id, version, source_kind, source_uri, source_ref
  resolved_commit, content_digest, manifest_json, compatibility_json
  validation_state, security_state, created_by, created_at

capability_blobs
  version_id, storage_key, path, sha256, size, executable, media_type

capability_dependencies
  version_id, dependency_package_key, version_constraint, resolved_version_id
  dependency_kind, optional

capability_validations
  version_id, environment_version_id, validation_kind, status
  summary_json, tool_catalog_json, started_at, completed_at

node_capability_bindings
  node_asset_id, capability_version_id, alias, enabled, config_override_json
  position, row_version

runtime_policy_versions
  id, policy_type, version, policy_json, digest

snapshot_runtime_manifests
  snapshot_id, node_key, manifest_json, digest
~~~

现有 `CapabilityImport` 可保留为短期上传/验证事务，但 commit 后应生成不可变 `capability_version`，而不是让 Import 本身承担永久版本实体。`SkillCollection` 应升级为通用 Capability Collection，或新增通用集合后迁移 Skill 集合。

### 4.4 来源与安装策略

支持以下来源，但都必须解析为固定版本：

- 上传 ZIP/JSON/YAML；
- Git URL + commit/tag；
- OpenHands Marketplace 项目；
- OpenHands 已安装 Skill/Plugin；
- 组织内部仓库；
- 受控内置能力。

Marketplace 只作为**导入来源**，不能让生产 Conversation 自动加载浮动内容。导入时解析 commit/digest，完成安全校验后生成 FlowWeave Capability Version；运行时再把固定内容交给 OpenHands 原生 Loader。

### 4.5 Runtime manifest

每个 Snapshot Node 冻结一个 Runtime manifest：

~~~json
{
  "schema_version": 1,
  "openhands_version": "1.40.0",
  "agent_kind": "openhands",
  "agent_profile": {"version_id": "...", "digest": "..."},
  "capabilities": [
    {"type": "PLUGIN", "version_id": "...", "digest": "..."},
    {"type": "MCP_SERVER", "version_id": "...", "digest": "..."}
  ],
  "tool_policy": {"version_id": "...", "digest": "..."},
  "context_policy": {"version_id": "...", "digest": "..."},
  "confirmation_policy": {"kind": "ConfirmRisky", "threshold": "HIGH"},
  "budgets": {"max_iterations": 100, "max_cost_usd": 5.0}
}
~~~

运行时不得重新解析浮动 Marketplace 或“最新版本”。manifest 中每项都应能回查来源、内容、审核和兼容性。

## 5. RuntimePort 与 OpenHands 适配器改造

### 5.1 从请求 DTO 演进为 Agent Spec

当前 `StartAttemptRequest` 平铺 Skill、MCP、Hook 等字段。目标应增加结构化 Agent Spec：

~~~text
RuntimeAgentSpec
  agent_kind: OPENHANDS | ACP
  llm / acp_config
  tools[]
  agent_context
  plugins[]
  agent_definitions[]
  mcp_config
  hook_config
  condenser
  critic
  confirmation_policy
  security_analyzer
  tool_concurrency_limit
  tags / observability metadata
  budgets
~~~

`StartAttemptRequest` 仍包含 FlowWeave 输入输出和 Workspace，但不再自行定义一套 Agent 能力语义；它引用已经编译好的 `RuntimeAgentSpec`。

### 5.2 RuntimePort 需要补充的方法

建议新增或标准化：

~~~text
respond_to_confirmation(handle, expected_pending_digest, accept, reason)
get_pending_confirmation(handle)
condense(handle)
get_stats(handle)
fork(handle, from_event_id, target_runtime?)
navigate(handle, event_id)
probe_mcp(runtime_allocation, server_config, optional_readonly_call)
list_runtime_tools(runtime_allocation)
load_plugin(handle, plugin_ref)          # 仅受控调试；生产优先创建时冻结
get_agent_final_response(handle)         # 诊断兜底
pause(handle) / run(handle)
export_trajectory(handle)
~~~

不是所有方法都必须暴露给用户，但适配器应该覆盖 OpenHands 正式协议，避免上层继续拼 HTTP。

### 5.3 事件模型扩展

现有归一化类型不足以表达新能力，建议增加：

- `CONFIRMATION_REQUIRED`、`CONFIRMATION_RESOLVED`；
- `CONDENSATION_STARTED`、`CONDENSATION_COMPLETED`；
- `SUBAGENT_STARTED`、`SUBAGENT_PROGRESS`、`SUBAGENT_COMPLETED`；
- `SKILL_ACTIVATED`、`PLUGIN_LOADED`；
- `USAGE_UPDATED`、`BUDGET_WARNING`、`BUDGET_EXCEEDED`；
- `SECURITY_RISK_EVALUATED`；
- `BRANCH_CREATED`、`HEAD_NAVIGATED`；
- `GOAL_STATE_CHANGED`、`CRITIC_RESULT`。

事件 payload 必须有版本和稳定 ID，并对 Tool 参数、Secret、Headers、OAuth state 做字段级脱敏。

## 6. P0：原生 Tool Action 确认闭环

### 6.1 当前问题

FlowWeave 看到 `waiting_for_confirmation` 后只生成通用人工问题。用户回复时调用 `interrupt + send_message`。这无法批准或拒绝 OpenHands 内部挂起的原 Action，也没有保存 Action ID、Tool 参数、风险和决定。

OpenHands 1.40.0 的确认单位是**当前待处理动作批次**，不是由客户端指定的单个 Action。`POST /api/conversations/{id}/events/respond_to_confirmation` 的请求体只有 `accept` 和 `reason`：批准会重新运行 Conversation 并执行当前全部 unmatched actions；拒绝会为当前批次中的每个动作生成拒绝 Observation。端点不接收 confirmation ID、Action ID 或 Tool Call ID。FlowWeave 因此必须在审批前读取并冻结当前动作集合的 digest，并在提交决定前校验 Runtime 仍处于同一个待确认状态。若动作集合已漂移，必须拒绝旧审批并要求用户重新确认。

### 6.2 目标流程

~~~mermaid
sequenceDiagram
    participant OH as OpenHands
    participant W as FlowWeave Worker
    participant DB as FlowWeave DB
    actor U as User
    OH-->>W: Confirmation required(pending action batch, risks)
    W->>DB: 保存 confirmation batch + pending digest
    DB-->>U: 展示动作列表、参数摘要、风险和原因
    U->>DB: APPROVE / REJECT + reason
    W->>OH: 校验 pending digest 未漂移
    W->>OH: respond_to_confirmation(accept, reason)
    OH-->>W: Action results or UserRejectObservations
    W->>DB: 关闭 confirmation，继续事件投影
~~~

### 6.3 数据模型

新增 `runtime_confirmation_batches`：

- `id`、`attempt_id`、`conversation_id`；
- `runtime_conversation_id`、`runtime_cursor`、`pending_actions_digest`；
- `pending_actions_json`：动作 ID、Tool Call ID、Tool 名、脱敏参数、单动作 digest；
- `risk_summary_json`：每个动作的风险级别、原因和分析器信息；
- `policy_version_id`、`action_count`；
- `state=PENDING|APPROVED|REJECTED|EXPIRED|CANCELLED`；
- `decided_by`、`decision_reason`、`decided_at`；
- `runtime_response_cursor`、`state_version`。

若后续 OpenHands 增加逐 Action 决策协议，可再规范化 `runtime_confirmation_actions` 子表；在 1.40.0 上不得让 UI 表现为可以只批准批次中的某一个动作。

### 6.4 验收标准

- 审批后冻结批次中的 Tool Actions 各执行一次；
- 拒绝为批次中的每个动作产生对应 `UserRejectObservation`；
- 重复点击、Worker 重试和页面刷新不重复决定；
- UI 能展示完整动作列表、脱敏参数、逐动作风险和策略来源；
- Runtime pending digest 漂移时，旧审批不能提交；
- Gate 审批与 Tool Action 审批明确区分；
- 当前普通 `resume` 不再用于处理原生确认。

## 7. 上下文压缩与 Memory

### 7.1 Condenser 先行

节点或 Agent Profile 可选择：

- `NoOp`：短任务或强审计要求；
- `Rolling`：不额外调用模型；
- `LLMSummarizing`：用指定模型生成摘要；
- `Pipeline`：组合策略。

FlowWeave 冻结阈值、保留首尾数量、摘要模型和最大费用。Condensation Event 必须投影，UI 标识被压缩区间、摘要时间和费用。用户不应把摘要误认为完整原文；原始事件仍由 OpenHands 持久化并可按权限审计。

### 7.2 Memory 后置

Memory 至少分为：

| 作用域 | 示例 | 生命周期 |
|---|---|---|
| Conversation | 当前会话偏好 | 随会话 |
| Attempt | 本次节点执行经验 | 随 Attempt |
| Node Asset | 节点领域规则 | 显式发布版本 |
| Project/Repository | 代码库约定 | 随仓库版本或规则版本 |
| User | 个人偏好 | 用户可见、可删除 |
| Organization | 组织规范 | 管理员发布 |

禁止直接全局开启 `load_memory=true`。必须先定义写入主体、审核、过期、删除、敏感数据扫描和 Snapshot 重放规则。运行时 Memory 应通过受控目录或编译后的 AgentContext 注入。

## 8. MCP 验证与 OAuth

### 8.1 目标生命周期

~~~text
导入/编辑
  → 静态 Schema 与敏感字段校验
  → 在目标 Environment Version 中启动探测 Runtime
  → OpenHands /api/mcp/test
  → tools/list 与参数 Schema 快照
  → 用户可选只读 Tool 试调用
  → OAuth 授权与加密 Token Reference
  → 验证结果绑定 Capability Version
  → Node 绑定与 Snapshot 冻结
  → Conversation 创建时再次快速验证
~~~

### 8.2 设计要求

- stdio MCP 必须在最终环境镜像中验证 command、cwd 和依赖；
- 远程 MCP 必须在最终网络模式下验证；
- 只读试调用由用户明确选择，不根据 Tool 名猜测；
- OAuth state/token 独立存储为 Secret Reference，不写入能力 JSON、Workspace 或 Snapshot；
- Token 按环境/用户/组织作用域隔离，并支持刷新、撤销和审计；
- 保存 Tool Catalog 的 name、description、input schema、annotations 和服务端版本摘要；
- Runtime 启动时若 Tool Catalog 漂移，按策略警告或拒绝。

## 9. Tool Policy 与 Browser

### 9.1 Tool Policy 模型

每个节点冻结：

~~~json
{
  "allowed_tools": ["terminal", "file_editor", "grep", "glob"],
  "denied_tools": ["browser_tool_set"],
  "unknown_tool": "DENY",
  "tool_concurrency_limit": 1,
  "rules": {
    "terminal": {"write": true, "confirmation": "RISKY"},
    "file_editor": {"paths": ["/workspaces/nodes/<id>/**"]}
  }
}
~~~

OpenHands 负责注册和执行 Tool；FlowWeave 负责编译允许列表、风险规则和 Snapshot。Tool 名无法识别时必须 fail closed。

### 9.2 Browser 接入前置条件

- Runtime 具有浏览器依赖和资源限额；
- 网络策略支持目标 allow/deny 与内网阻断，不能只依赖 Docker egress；
- Cookie/登录态使用环境专属凭据，不进 Workspace；
- 下载、截图、录像登记为 Artifact 或受控临时文件；
- Browser 写操作和外部提交动作进入确认策略；
- 审计记录 URL、动作类型和脱敏参数；
- SSRF、DNS rebinding、localhost/metadata 地址有防护。

### 9.3 直接 Bash 与 Agent terminal

直接 Bash API 属于人工/系统旁路操作，Agent terminal 属于模型 Tool Action。FlowWeave 必须分别记录 `actor=HUMAN|SYSTEM|AGENT`，不得在 UI 或审计中混为同一来源。

## 10. 原生子 Agent 改造

### 10.1 当前差异

当前父 Agent 通过 Finish message 输出：

~~~json
{
  "flowweave": {
    "action": "delegate",
    "tasks": [{"title": "...", "instruction": "..."}]
  }
}
~~~

FlowWeave 解析后创建独立平台 Conversation。该方案可审计，但不是 OpenHands Task Tool；模型必须结束当前轮才能委派，也无法使用 `TaskAction`、`AgentDefinition`、resume、原生 Tool Observation 和子 Agent 指标合并。

### 10.2 目标形态

1. 节点绑定经过冻结的 `AGENT_DEFINITION` 能力；
2. 创建父 Conversation 时传 `agent_definitions`；
3. Tool Policy 允许 `task_tool_set`；
4. 父 Agent 原生调用 Task Tool；
5. OpenHands 创建和运行子 Agent；
6. FlowWeave 从 OpenHands 事件投影子任务、子会话、状态、成本和结果；
7. 用户可查看、停止和审计子 Agent，但不接管其内部执行器。

### 10.3 OpenHands 1.40.0 的集成限制

当前 Task Tool 是阻塞式：`TaskManager` 在父进程内创建 `LocalConversation`，运行完成后返回 `TaskObservation`；子任务持久化位于父 Conversation 的 subagents 目录。Agent Server 的 `/api/sub-agents` 只是发现 Agent Definition，不等同于列出正在运行的 Task 子会话。

因此不能仅把 `task_tool_set` 加入 tools 就宣称完成平台集成。需要优先推动以下上游或适配器能力：

- 稳定的 `TaskStarted/Progress/Completed/Failed` 事件，包含 task ID 和 child conversation ID；
- 查询父 Conversation 下 Task 实例及其状态；
- 取消单个 Task 和父取消传播；
- Task 级 usage/cost；
- 子 Agent Confirmation 的异步回调，而不是阻塞进程等待本地 handler；
- 可配置最大深度、并发和总预算；
- 子 Agent Workspace 写集合或冲突声明。

### 10.4 迁移策略

| 阶段 | 行为 |
|---|---|
| 兼容期 | 保留现有平台 Delegation，增加 `delegation_backend=FLOWWEAVE|OPENHANDS` |
| 试运行 | 只对内置只读 `code-explorer` 开启原生 Task Tool |
| 可观测期 | 完成 Task 事件、成本、取消和 UI 投影 |
| 默认切换 | 新节点默认 OPENHANDS，旧 Snapshot 继续 FLOWWEAVE |
| 退场 | 删除 Finish 控制 JSON 指令和 `_delegation_tasks` 解析 |

不能原地改变旧 Snapshot 的委派后端。迁移必须通过新 Node/Flow Snapshot 生效。

## 11. Plugin、Skill 和 Agent Profile 的原生化

### 11.1 Plugin

Plugin 是能力仓库的一等包。FlowWeave 导入时读取 Plugin manifest，拆出其贡献的 Skill、MCP、Hook、Agent Definition 和文件清单，完成审批和 digest 冻结；创建 Conversation 时通过 OpenHands `plugins` 原生加载。

生产会话默认禁止运行中 `load_plugin`，因为它会改变 Tool 与上下文而绕过 Snapshot。调试会话可允许，但每次加载必须形成 Runtime Mutation Event 并标记会话不可复现。

### 11.2 Skill

FlowWeave 继续管理来源、版本和绑定；OpenHands 负责 Skill 语义、渐进式披露、触发和 `invoke_skill`。应停止依赖仅在系统提示词中列路径的弱集成，改为完整保留 OpenHands Skill metadata、resource directories、trigger 和 AgentSkills 格式。

`$能力名` 可继续作为 FlowWeave UI 的显式选择，但投递时应映射成 OpenHands 原生 Skill invocation/激活语义；若上游暂无直接外部激活 API，则应增加正式事件/命令，而不是长期靠自然语言“请先读取 Skill”。

### 11.3 Agent Profile

OpenHands Profile 不直接成为第二套在线配置源。目标是：

1. 用户从 OpenHands Agent Profile 或 Marketplace 导入；
2. FlowWeave 解析成 Agent Spec，解析所有引用；
3. 绑定具体能力版本、Tool Policy、Model Policy；
4. 发布不可变版本；
5. Snapshot 冻结；
6. Runtime 可选择用 `agent_profile_id` 或显式 Agent JSON 创建，但效果必须与冻结 Spec 一致。

若使用 Server 端 `agent_profile_id`，Runtime 镜像内 Profile Store 也必须是版本化只读挂载，不能依赖每个容器的可变 `$HOME`。

## 12. Conversation 分支原生化

### 12.1 两类分叉

| 类型 | 语义 | 实现 |
|---|---|---|
| 高保真 Runtime Fork | 继承完整事件、Tool Observation、Agent state、Skill 激活、Condenser | OpenHands `/fork` |
| 可移植 Semantic Fork | 跨 Runtime/环境/Snapshot，只携带允许的历史与输入 | FlowWeave 新建会话并注入基线 |

当前只有第二种，UI 必须明确。目标是在同 Runtime、同 Snapshot、同 Workspace 安全条件满足时默认原生 fork；否则回退 Semantic Fork。

### 12.2 数据补充

`AgentConversation` 增加：

- `fork_kind=RUNTIME|SEMANTIC`；
- `source_conversation_id`；
- `source_runtime_conversation_id`；
- `source_runtime_event_id`；
- `runtime_branch_metadata_json`；
- `metrics_reset`。

`navigate` 会修改同一运行时会话的 HEAD，默认只向高级调试模式开放，并记录不可变 Branch Audit Event。

## 13. 成本、预算与可观测性

### 13.1 成本账本

新增按增量写入的 `runtime_usage_ledger`：

- Run/Node Run/Attempt/Conversation/Subagent 关联；
- provider、model、usage component（agent/condenser/critic/subagent/title）；
- input/output/cache/reasoning token；
- amount、currency、pricing version；
- OpenHands event/cursor、幂等 digest；
- observed_at。

总计由账本计算，不能只覆盖保存最后一次 Conversation stats。

### 13.2 预算策略

支持：

- `max_iterations`；
- `max_tokens`；
- `max_cost_usd`；
- `max_subagents`、`max_subagent_depth`；
- `max_wall_time`；
- 80% 告警和硬停止；
- 超预算后的降级模型策略（可选，必须显式）。

### 13.3 Trace

创建 Conversation 时注入稳定、低基数字段：`flow_run_id`、`node_run_id`、`attempt_id`、`conversation_id`、`snapshot_id`、`provider_id`。消息正文、Artifact 内容、Token 和 Secret 不进入 tag。FlowWeave Run Event 应保存 Trace ID 链接。

## 14. 实时事件目标

当前工作树已实现 OpenHands WebSocket 到 Web 的安全文本 delta 代理，这是部分接入，不等于耐久事件已经改为流式。目标采用双通道：

- WebSocket：低延迟 delta、状态变化和 Worker 唤醒；
- REST `events/search`：按 cursor 补偿、去重和最终持久化；
- PostgreSQL：FlowWeave 对外的耐久事件事实；
- 断线时自动退回轮询；
- 多 Worker 以 lease 确定唯一耐久消费者。

不得持久化私有模型 reasoning；只投影用户可见文本、Tool 事件、状态、确认、成本和安全事件。

## 15. Critic、Goal 与 Gate 的组合

| 能力 | 职责 |
|---|---|
| Critic | Runtime 内有限次数自评和修复 |
| Goal loop | 同一会话内持续追踪明确目标 |
| END Gate | FlowWeave 独立、可审计的外部质量判定 |
| 人工验收 | 最终业务决定 |

Critic 不能替代 END Gate，Goal 完成不能直接完成 Attempt。Critic/Goal 的每次额外模型调用都进入预算。建议先提供实验开关，默认最大精炼 2 次；达到上限后按普通 Finish 进入 END Gate。

## 16. ACP Agent

ACP 是新的 Agent Kind，不是新的模型 Provider。目标新增：

- `agent_kind=OPENHANDS|ACP`；
- 固定白名单的 ACP command/args；
- 环境凭据文件映射和独立数据目录；
- ACP session ID 与 FlowWeave Conversation 的持久映射；
- ACP 模型发现、推理强度和 live switch；
- MCP 兼容性检查；
- ACP Event 到 RuntimeEvent 的归一化；
- 退出、超时、取消、恢复和费用处理。

禁止用户直接输入任意 ACP 启动命令。命令必须来自已发布 Environment Version 或平台内置 Agent Profile。

## 17. FlowWeave API 与 UI 改造

### 17.1 建议 API

~~~text
GET/POST       /api/v1/capability-packages
GET            /api/v1/capability-packages/{id}/versions
POST           /api/v1/capability-versions/{id}/validate
POST           /api/v1/capability-versions/{id}/publish
GET            /api/v1/capability-versions/{id}/manifest
GET            /api/v1/capability-versions/{id}/tool-catalog

POST           /api/v1/mcp-probes
GET            /api/v1/mcp-probes/{id}
POST           /api/v1/mcp-probes/{id}/readonly-call
POST           /api/v1/mcp-oauth-sessions

GET            /api/v1/runtime-confirmations/{id}
POST           /api/v1/runtime-confirmations/{id}/approve
POST           /api/v1/runtime-confirmations/{id}/reject

GET            /api/v1/agent-conversations/{id}/usage
POST           /api/v1/agent-conversations/{id}/condense
POST           /api/v1/agent-messages/{id}/fork?kind=runtime|semantic
GET            /api/v1/agent-conversations/{id}/runtime-subagents
POST           /api/v1/agent-conversations/{id}/runtime-subagents/{task_id}/cancel

GET/POST       /api/v1/runtime-policies
GET            /api/v1/runtime-tools
~~~

所有命令支持 `Idempotency-Key`，并遵循短事务 + 后台任务 + lease fencing。

### 17.2 UI 页面

1. **能力仓库**：包、版本、来源、digest、依赖、兼容性、安全状态；
2. **能力详情**：Skill/Plugin/Agent Definition/MCP/Hook/Tool Policy 的原生 manifest 视图；
3. **MCP 验证**：目标环境、连接结果、Tool Schema、只读试调用、OAuth；
4. **节点 Agent 配置**：Agent Kind、Profile、Tool Policy、Context、Confirmation、Budget；
5. **运行确认卡片**：Tool、参数摘要、风险、批准/拒绝；
6. **子 Agent 树**：原生 task ID、Agent 类型、状态、费用、结果和取消；
7. **上下文状态**：压缩次数、摘要区间、Memory 来源、已激活 Skill；
8. **使用量**：Run/Attempt/Conversation/子 Agent 成本；
9. **运行时诊断**：OpenHands 版本、Tool Catalog、Plugin、MCP、Trace 和事件 cursor。

## 18. 分阶段实施计划

### Phase 0：协议正确性基线（P0）

- 原生 Tool Confirmation 完整闭环；
- RuntimeEvent 增加稳定确认事件；
- Condenser 基础配置与事件投影；
- 明确当前实时文本流为非耐久通道；
- 建立 OpenHands 版本/能力兼容测试。

**退出条件**：不再用普通 resume 处理挂起 Action；长会话能按策略压缩且可审计。

### Phase 1：统一能力模型与 Tool Policy

- Capability Package/Version/Dependency/Validation；
- Runtime manifest；
- Plugin、Agent Definition、Tool Policy 类型；
- 节点绑定具体版本；
- 现有 SKILL/MCP/HOOK 数据前向迁移；
- OpenHands Tool Catalog 与 fail-closed policy。

**退出条件**：任一运行都能从 Snapshot 回放完全相同的能力和 Tool 配置。

### Phase 2：MCP、Plugin 与能力市场

- 目标环境 MCP probe；
- Tool Schema 与只读试调用；
- OAuth Secret Reference；
- Marketplace/Git 固定 commit 导入；
- OpenHands 原生 Plugin 加载；
- Skill 原生触发/invoke 语义。

**退出条件**：能力上线前可验证，运行时不加载未冻结的远程内容。

### Phase 3：原生子 Agent

- Agent Definition 管理；
- Task Tool 试点；
- 上游 Task 生命周期事件/API；
- 子 Agent 树、成本、取消、确认和预算；
- 兼容后端双跑；
- 新 Snapshot 默认切换 OpenHands。

**退出条件**：父 Agent 无需输出 FlowWeave 控制 JSON；原生子 Agent 全程可见、可取消、可计费。

### Phase 4：上下文、分支和观测

- Memory Policy；
- 原生 Runtime Fork；
- 成本账本与预算；
- WS 唤醒 + REST 补偿；
- Trace 关联；
- Browser 受控开放。

**退出条件**：长会话、分叉、浏览器和成本治理达到生产可用。

### Phase 5：高级 Agent 能力

- Agent Profile 导入/物化；
- Critic/Goal 实验；
- ACP Agent；
- File/Git/Trajectory 审阅；
- VSCode/Desktop；
- Warm pool/deferred init。

**退出条件**：按各能力独立验收，不阻塞核心路线。

## 19. 迁移与兼容策略

### 19.1 不修改历史 Snapshot

旧 Snapshot 保留原始：

- 能力物化方式；
- Tool 列表；
- Delegation backend；
- Semantic Fork；
- Runtime adapter/version。

新行为只随新 Node Asset/Flow Snapshot 生效。

### 19.2 双读、单写、再切换

能力模型迁移按以下顺序：

1. 新表上线；
2. 旧数据回填为 Package/Version；
3. API 双读并比较结果；
4. 新写只写新模型，必要时生成兼容投影；
5. Snapshot 改用 Runtime manifest；
6. 停止旧写；
7. 所有引用迁移完成后删除旧字段。

### 19.3 需要退场的临时机制

| 当前机制 | 退场条件 |
|---|---|
| Finish message 中的 delegation JSON | 原生 Task 全程可观测、可取消、可预算 |
| `_delegation_tasks` 文本解析 | 所有新 Snapshot 使用 OpenHands backend |
| 文本历史模拟高保真 fork | Runtime Fork 支持和映射落地；Semantic Fork 仍保留 |
| `$Skill` 自然语言强制指令 | 有正式 Skill invoke/activation API 或 Tool |
| 固定三个 Tool | Tool Policy 全量上线 |
| `CapabilityImport` 作为永久版本引用 | Capability Version 完成迁移 |
| 人工文本 resume 原生确认 | Confirmation API 闭环 |

## 20. 测试与验收体系

### 20.1 契约测试

- 在 CI 中从锁定的 OpenHands 镜像导出 OpenAPI；
- 校验 FlowWeave 使用的路径、请求字段、事件 Kind 和 Tool 名存在；
- OpenHands 版本升级时生成能力差异报告；
- 禁止未评审的 API/Tool 消失或默认值变化。

### 20.2 集成测试

- 真 OpenHands 容器创建、事件、确认、压缩、fork；
- stdio/HTTP/SSE MCP probe；
- Plugin 贡献 Skill/MCP/Hook/Agent；
- Task 子 Agent 成功、失败、取消、恢复、超预算；
- WebSocket 断线后 REST cursor 无缺失无重复；
- Browser 网络和下载边界；
- ACP 进程失败与恢复。

### 20.3 安全测试

- Capability archive 路径穿越、软链接、digest 篡改；
- Plugin/Agent Definition 请求未允许 Tool；
- MCP OAuth token 不进入日志、DB JSON、Workspace 和消息；
- Confirmation 参数脱敏和 digest 防重放；
- Browser SSRF/metadata/localhost；
- File/Git API 绝对路径不能由浏览器透传；
- Client Tool 和动态 module import 默认关闭；
- 子 Agent 不能突破父节点 Tool/网络/预算上限。

### 20.4 可恢复性测试

- OpenHands 调用成功但 FlowWeave CAS 失败；
- Worker lease 丢失；
- Runtime 重启和 Conversation reload；
- Confirmation 决定提交后网络中断；
- Task 子 Agent 完成后父进程崩溃；
- MCP OAuth 回调重复；
- Condense 中途失败；
- Runtime Fork 创建成功但平台写入失败。

## 21. 完成定义

一项 OpenHands 能力只有同时满足以下条件才算“FlowWeave 已接入”：

1. 使用 OpenHands 原生配置、Tool、事件或 API，而不是提示词模拟；
2. 配置进入不可变 Snapshot/Runtime manifest；
3. 权限、Secret、网络、Workspace 和资源边界明确；
4. 关键状态与事件进入 FlowWeave 耐久投影；
5. 支持幂等、重试、取消和故障恢复；
6. 有成本和审计归属；
7. UI 能准确展示其状态和限制；
8. 有真 OpenHands 集成测试；
9. 版本升级有兼容性检查；
10. 旧的重复实现有明确退场条件。

仅把字段传给 OpenHands、仅在 UI 增加开关、或仅能在 happy path 演示，都不算完成。

## 22. 关键代码与上游索引

| 主题 | FlowWeave / OpenHands 路径 |
|---|---|
| 当前集成设计 | `docs/openhands-agent-server-design.md` |
| RuntimePort | `services/platform/src/flowweave/runtime/base.py` |
| OpenHands Adapter | `services/platform/src/flowweave/runtime/openhands.py` |
| 能力物化 | `services/platform/src/flowweave/runtime/workspace.py` |
| 能力导入 | `services/platform/src/flowweave/modules/catalog/application/capability_imports.py` |
| Conversation 编排 | `services/platform/src/flowweave/modules/conversations/application/service.py` |
| 当前子 Agent | `services/platform/migrations/versions/0020_agent_subagents.py` |
| 当前 Skill Collection | `services/platform/migrations/versions/0023_skill_collections.py` |
| OpenHands Agent Server 路由 | `/Users/zhengmengen/WorkSpace/openhands/software-agent-sdk/openhands-agent-server/openhands/agent_server` |
| OpenHands Task Tool | `/Users/zhengmengen/WorkSpace/openhands/software-agent-sdk/openhands-tools/openhands/tools/task` |
| OpenHands Agent Context | `/Users/zhengmengen/WorkSpace/openhands/software-agent-sdk/openhands-sdk/openhands/sdk/context` |
| OpenHands Skill/Plugin | `/Users/zhengmengen/WorkSpace/openhands/software-agent-sdk/openhands-sdk/openhands/sdk/skills`、`plugin` |
| OpenHands Security | `/Users/zhengmengen/WorkSpace/openhands/software-agent-sdk/openhands-sdk/openhands/sdk/security` |

## 23. 最终目标

完成本路线图后，FlowWeave 应成为 OpenHands 的企业级控制面，而不是另一个 Agent Runtime：用户在 FlowWeave 中选择并治理能力，FlowWeave 把冻结的 Agent/Profile/Plugin/Skill/MCP/Hook/Tool/Policy 交给 OpenHands 原生运行；OpenHands 产生 Tool、子 Agent、确认、压缩、成本和结果事件，FlowWeave 将其可靠投影到流程、Artifact、审批、审计和 UI。

任何为了短期接通而新增的提示词协议，都必须有对应的正式协议迁移计划和删除条件。

### T8 产品收口状态

T8 已完成实现门禁：固定 Marketplace 目录浏览与双层 provenance、Tool Policy 拒绝原因、Profile
版本差异/绑定/新 Snapshot 激活、Fork/Runtime HEAD、Task usage、WebSocket/REST 恢复、Critic/Goal
预算与只读 `ask_agent` 已进入产品 UI 和 98-path OpenAPI/前端契约。Browser、ACP、IDE/Desktop、
直接 Runtime API、Navigate 与父级 Trace 继续保持既有 `SKIP`；MCP Tool schema 和子 Agent 单 Task
控制继续为 `UPSTREAM_BLOCKED`。T9 尚未启动，生产 build、真实 Runtime、恢复、安全和 E2E 证据
完成前不将这些实现标为 `COMPLETE`。
