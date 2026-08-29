# Agent 会话唯一实现与可复用化技术方案

> 状态：`IMPLEMENTING`
> 日期：2026-08-29
> 实施状态：进行中；FR-87 已完成共享页面唯一入口，后端内核迁移尚未开始
> 当前阶段：只整理和迁移已经完成的外层 `Agent 会话`，不接入 Flow、FlowRun 或节点
> OpenHands 事实基线：`9a24f6c8866f353042a57df0514ccc900e3a0691`（四包 `1.44.0`）
> 参考设计：`docs/agent-workbench-technical-design.md`

## 1. 需求理解与结论

外层 `Agent 会话` 是为了脱离流程和节点约束，先把会话能力完整开发、调试并验证稳定。现在这套能力已经
具备完整页面、交互和后端逻辑。下一步不是先研究节点和流程如何管理会话，也不是立即重做节点入口，而是
先把这套已经调通的 Agent 会话整理成平台唯一、完整、可复用的会话实现。

本次工作的唯一目标是：

1. 以当前外层 `Agent 会话` 的实际行为为产品基线，功能一项不少地迁移为共享 Agent Session 实现；
2. 当前 `/agent` 入口继续使用这套实现，并证明页面、交互、数据和 Runtime 行为全部无回归；
3. 前端只保留一棵完整会话组件树，后端只保留一套完整会话应用逻辑；
4. 共享实现不包含 Flow、FlowRun、节点、Attempt 等业务判断；
5. 本阶段不接入节点，也不决定节点如何创建、触发、隔离或管控会话；
6. 等共享 Agent 会话完整迁移并验收后，后续节点功能只能调用这套实现，不能再复制一份。

节点启动提示词、节点独立工作空间以及流程对会话的管控，是平台已有或后续接入时需要保留的业务能力，
不是本次 Agent 会话迁移阶段需要设计的“扩展点”。本方案不修改、重解释或实现这些能力。

## 2. 本阶段范围

### 2.1 要做的事情

- 完整盘点当前外层 Agent 会话的前端、后端、数据和 Runtime 行为；
- 将当前集中在 `AgentWorkbenchPage.tsx` 的完整会话页面拆成可复用组件和控制器；
- 将当前集中在 `agent_workspaces/application/conversations.py` 的完整会话行为整理为唯一会话应用内核；
- 保留 Agent Workspace 作为当前唯一已接入的会话宿主；
- 保留当前 Agent Workspace API、稳定 URL、数据库数据和 OpenHands Conversation identity；
- 用现有顶层 Agent 工作台运行全部回归，证明重构前后行为一致；
- 增加架构守卫，禁止以后再出现第二套会话页面、消息逻辑、事件归一化、WebSocket 状态机或终端实现；
- 为未来调用留出稳定、与业务宿主无关的前后端接口，但本阶段只实现 Agent Workspace 这一种调用方。

### 2.2 明确不做的事情

- 不修改 Flow、FlowRun、Run Snapshot、Node Asset、流程节点实例或 Attempt；
- 不修改节点进入会话的按钮、导航、创建时机或管控方式；
- 不处理节点启动提示词；
- 不处理节点工作空间隔离或目录布局；
- 不处理节点会话与流程自动执行 Conversation 的关系；
- 不删除现有旧节点会话页面、旧 FlowRun 会话 API 或历史 locator；
- 不将 FlowRun Runtime 接入共享 Agent 会话；
- 不新增通用 context 表、FlowRun Node host 或节点相关数据库字段；
- 不迁移、猜测或清理任何旧 FlowRun 会话数据；
- 不改变 OpenHands、Runtime Provider、FlowRun 单容器或 generation 机制。

上述内容全部留到后续“节点接入共享 Agent 会话”方案中单独审核。本阶段不提前实现，也不为了未来假设而
修改节点和流程代码。

## 3. 必须完整迁移的 Agent 会话能力

迁移完成不等于只抽出消息列表和输入框。以下能力全部必须归入同一共享实现，并由当前 `/agent` 原样使用。
任何一项缺失，都不能称为“完整迁移”。

### 3.1 会话生命周期

- 浏览器内新会话草稿，首条消息前不创建数据库 binding 或 OpenHands Conversation；
- 首条消息原子 bootstrap；
- 稳定幂等键和网络结果不确定时的正式事件身份对账；
- 创建明确失败时清理隐藏预留；
- 会话列表、选择、稳定 URL、刷新恢复、标题生成、双击改名和删除；
- OpenHands 原生 fork、navigate、condense、interrupt 和 resume；
- 最后一条用户消息编辑并重新思考；
- 历史非流式 Conversation 的正式 switch/fork 迁移。

### 3.2 消息与实时过程

- OpenHands 正式事件树是唯一内容事实源；
- REST 历史回填与 WebSocket 实时流合并；
- token delta、Thought、Tool Action/Observation、Condensation 和 ERROR 安全投影；
- assistant `MessageEvent` 与 `FinishAction.message` 两种正式终态；
- 工作过程与最终回复分层；
- 工具 Action/Observation 只按正式 `action_id`、`tool_call_id` 关联；
- 运行中计时、刷新后恢复、完成后折叠、长回复定位和用户消息刻度导航；
- 本地排队发送、busy 恢复、IME 保护、复制和链接安全打开；
- 不展示供应商隐藏推理，不持久化浏览器临时 delta 或队列。

### 3.3 模型与上下文

- 新会话显式选择已连接供应商、模型和推理强度；
- 当前选择持久化到 binding；
- 每次发送前重新应用 OpenHands 正式 `switch_llm`；
- 强制流式供应商的 callback 绑定与历史迁移；
- context stats 只读取当前 LLM 正式 usage bucket；
- 缺失容量时不估算或伪造上下文比例；
- 正式失败事件可见，额度或供应商错误不会被页面吞掉。

### 3.4 附件、来源、文件与终端

- 草稿和正式会话附件上传；
- 图片使用 OpenHands 正式 `ImageContent`，普通文件使用真实工作区路径；
- 来源统一展示用户提供的文件、图片和 URL；
- 工作区概览、文件树、预览和下载；
- 多独立终端、持久 tmux、resize、滚屏、选择复制和显式关闭；
- 文件和终端始终经过服务端工作区范围校验；
- 浏览器不获得容器 endpoint、Docker socket 或宿主机路径。

### 3.5 能力与 OpenHands 原生契约

- 新会话默认 Skill、MCP、Plugin 冻结；
- 当前会话使用正式 Marketplace 与 `load_plugin` 增量加载；
- 已加载能力锁定，不能伪造卸载；
- MCP readiness 使用同一 Runtime 的 OpenHands 正式测试接口；
- confirmation、condenser、Task、Oracle 和 Tool 都使用 OpenHands 正式能力；
- FlowWeave 不自建 Agent loop、消息状态机、Tool 执行器或私有协议。

## 4. 目标代码结构

### 4.1 前端唯一会话工作台

当前 `apps/web/src/pages/AgentWorkbenchPage.tsx` 同时承担页面入口、数据请求、实时状态、会话列表、
Composer、能力管理、文件和终端等职责。本阶段将其按职责迁移为一套可复用组件树：

~~~text
AgentWorkbenchPage                    当前 /agent 的薄路由入口
  `- AgentSessionWorkbench             唯一完整会话页面
       |- useAgentSessionController     唯一查询、mutation、实时流和队列控制器
       |- AgentSessionRail              会话列表、工作目录分组和新建入口
       |- AgentSessionHeader            标题和会话级操作
       |- ConversationSurface           正式事件与实时过程的唯一渲染器
       |- AgentSessionComposer          输入、附件、模型、推理强度、发送/暂停/继续
       |- AgentSessionCapabilities      默认能力和当前会话能力管理
       `- AgentSessionWorkspaceTools    来源、概览、文件、预览、终端和 IDE 信息
~~~

关键约束：

- `AgentSessionWorkbench` 必须包含当前页面全部交互，不是简化版聊天组件；
- `AgentWorkbenchPage` 只负责读取现有路由参数并装配当前 Agent Workspace gateway；
- 页面 wrapper 不得拥有自己的消息 reducer、WebSocket、Composer 或终端状态机；
- `ConversationSurface` 继续是唯一消息和过程渲染组件；
- 共享组件不得 import Flow、FlowRun、Node、Attempt 类型或 store；
- 未来节点接入时只能装配同一个 `AgentSessionWorkbench`，不能复制目录后修改。

### 4.2 前端调用边界

共享页面通过窄接口 `AgentSessionGateway` 调用后端。该接口完全来自当前 Agent 会话实际能力，不预埋节点
字段：

~~~text
AgentSessionGateway
  loadWorkspace / loadRuntime
  listConversations / bootstrapConversation
  getConversation / renameConversation / deleteConversation
  loadEvents / openEventStream
  sendMessage / interrupt / resume / inputReadiness
  rerunMessage / fork / condense
  getContext / switchModel / migrateStreaming
  uploadAttachment
  listCapabilities / replaceDefaultCapabilities / addConversationCapability / probeMcp
  loadWorkspaceDetails / readFile / openTerminal / closeTerminal
~~~

本阶段只有 `AgentWorkspaceSessionGateway`，内部继续调用现有 `/agent-workspaces/**` API。接口不会出现
`flowRunId`、`nodeId`、`attemptId` 或节点启动提示词。未来其他业务入口要使用会话时，必须先满足这套已有
契约，再提供自己的 gateway；共享页面本身不增加业务分支。

### 4.3 后端唯一会话应用内核

新增中性的 `modules/agent_sessions/`，承接当前 Agent Workspace 已验证的会话行为。建议结构：

~~~text
modules/agent_sessions/
  application/
    conversations.py       bootstrap、消息、控制、fork、rerun、condense、模型
    events.py              REST/实时安全投影和正式身份关联
    capabilities.py        冻结、Marketplace、load_plugin、MCP readiness
    workspace_tools.py     附件、来源、文件范围和终端会话
    titles.py              标题任务与手动标题 CAS
  domain/
    ports.py               Runtime、存储、模型、工作区等窄接口
  public.py                唯一跨模块 facade
~~~

当前 `agent_workspaces` 模块继续负责：

- 默认 Agent Workspace 的生命周期；
- 外置 allocation、稳定 Secret 和 Runtime Session/generation；
- 工作目录 CRUD；
- 将当前 Workspace、Runtime、工作目录和持久模型解析为 Agent Session 内核需要的依赖；
- 保持现有 `/agent-workspaces/**` 路由兼容。

当前阶段不把数据库表改名，也不把 `AgentConversationBinding.workspace_id` 泛化为 FlowRun owner。先迁移代码
所有权而保持表名、列、外键、ID 和 API JSON 不变。后续节点接入是否需要通用 owner/context 数据模型，应
在节点接入方案中根据实际管控要求另行决定。

### 4.4 后端依赖方向

重构后的依赖方向必须是：

~~~text
agent_workspaces presentation / host adapter
                   |
                   v
             agent_sessions public
                   |
                   v
      OpenHands runtime + repositories + storage ports
~~~

禁止反向依赖：

- `agent_sessions` 不 import orchestration、runs、flows、nodes 或 FlowRun Conversation 服务；
- `agent_sessions` 不根据路径、owner 字符串或 execution key 猜测调用方；
- `agent_sessions` 不包含 `if flow_run`、`if node` 或节点专用默认值；
- Agent Workspace 适配器只装配依赖，不能复制会话行为。

## 5. 本地预计修改范围

以下是审核通过后的预计范围。本轮只写设计，不实施这些改动。

### 5.1 Web 文件

主要修改：

- `apps/web/src/pages/AgentWorkbenchPage.tsx`：迁移为薄入口；
- `apps/web/src/components/ConversationSurface.tsx`：保持唯一渲染器，只做必要的纯组件边界整理；
- `apps/web/src/api/client.ts`：把现有 Agent Workspace 会话调用组织为 gateway，不改变公开请求；
- `apps/web/src/types.ts`：把纯会话类型移动到共享边界，不增加 FlowRun/Node union；
- `apps/web/src/pages/agent-workbench.css` 与 `agent-workbench-layout.css`：随组件拆分整理，视觉结果保持不变；
- `apps/web/e2e/product-flow.spec.ts`：现有 Agent 工作台场景继续作为回归基线，必要时拆出专门 spec。

预计新增：

- `apps/web/src/components/agent-session/AgentSessionWorkbench.tsx`；
- `apps/web/src/components/agent-session/useAgentSessionController.ts`；
- rail、header、composer、capabilities、workspace-tools 等纯子组件；
- `apps/web/src/api/agent-session-gateway.ts` 及当前 Agent Workspace 实现；
- 共享组件的定向测试或 Playwright page object。

本阶段不修改：

- `apps/web/src/pages/WorkbenchPage.tsx`；
- `apps/web/src/pages/AgentChatPage.tsx`；
- `apps/web/src/store/workbench.ts` 中的 FlowRun/节点导航；
- FlowRun Conversation 的 client、stream 和 terminal 方法；
- 节点与流程相关 E2E 预期。

旧节点页面即使存在问题，本阶段也保持原状。只有共享 Agent 会话完成并验收后，后续节点接入切片才允许
替换和删除旧实现。

### 5.2 后端文件

主要修改：

- `modules/agent_workspaces/application/conversations.py`：把会话行为迁移至 `agent_sessions`；
- `modules/agent_workspaces/application/workspace.py`：把会话使用的附件、文件和终端通用行为迁移至共享服务，
  保留 Workspace allocation/目录解析；
- `modules/agent_workspaces/application/titles.py`：迁移标题逻辑或通过共享 facade 调用；
- `modules/agent_workspaces/presentation/router.py`：路径和响应保持不变，改为调用共享 facade；
- `modules/agent_workspaces/public.py`：继续暴露 Workspace 管理能力，不成为第二套会话 facade；
- `bootstrap/container.py` 或等价装配位置：注入共享内核依赖；
- 架构边界测试：强制跨模块只走 public facade。

预计新增：

- `services/platform/src/flowweave/modules/agent_sessions/` 及其 application/domain/public 文件；
- 当前 Agent Workspace repository/runtime/workspace adapter；
- 共享会话契约测试。

本阶段不修改：

- `modules/conversations/**` 的 FlowRun locator 与旧交互代码；
- `modules/orchestration/**`；
- `modules/runs/**`；
- FlowRun Runtime allocation、replacement 和 worker；
- Runtime Provider owner 类型或容器启动逻辑；
- `runtime/request.py` 中的 FlowRun request 编译；
- 节点 Workspace 路径；
- OpenHands 源码。

`runtime/openhands.py` 只有在纯粹消除 Agent Workspace 会话内核对字符串前缀的耦合、且不改变任何 FlowRun
行为时才允许做最小整理；若会影响执行路径，则推迟到后续接入阶段。

### 5.3 数据库与 API

本阶段原则上不需要数据库迁移：

- 不新增通用 context 表；
- 不改 `agent_conversation_bindings` 的列和外键；
- 不重建现有 binding；
- 不改变 OpenHands conversation ID；
- 不迁移附件、能力、标题、命令或工作目录版本；
- 不改现有 `/api/v1/agent-workspaces/**` 路径和 JSON 契约。

若实际抽取发现必须做 schema 变化，必须停止当前切片，单独补充迁移理由和无损方案供审核，不能以“代码
整理”为由顺带改库。

## 6. 迁移实施方法

为避免在重构过程中破坏已经稳定的 Agent 会话，实施按以下顺序进行。

### 阶段 A：冻结行为契约

- 为第 3 节每一类能力建立测试映射；
- 记录当前 API、稳定 URL、数据库 identity、OpenHands identity 和页面关键行为；
- 补足缺失的定向回归，但不修改生产行为；
- 明确哪些浏览器状态只存在内存、哪些事实属于 PostgreSQL、哪些属于 OpenHands。

退出条件：现有 Agent 会话行为有可执行的回归清单，后续重构不能靠人工印象判断一致。

### 阶段 B：前端组件化但保持原入口

- 从 `AgentWorkbenchPage.tsx` 逐职责移动到共享组件和 controller；
- 每移动一块，`/agent` 立即改用新组件，不保留新旧双运行分支；
- gateway 继续调用完全相同的 API；
- CSS 只随 DOM 边界迁移，不做视觉改版；
- 完成后页面 wrapper 不含消息、事件流、Composer、能力或终端业务逻辑。

退出条件：`/agent` 页面行为、截图、键盘交互、刷新恢复和真实 Runtime 行为无回归；仓库只有一棵完整
Agent 会话组件树。

### 阶段 C：后端会话内核迁移但保持原 API

- 把会话行为移动到 `agent_sessions`；
- Agent Workspace router 改为通过共享 facade 调用；
- 保持事务边界、幂等键、错误码和响应结构；
- 保持数据库行和 OpenHands Conversation 原 identity；
- 每次只迁移一个职责，旧实现随迁移立即删除，不保留 feature flag 双写或双读。

退出条件：Agent Workspace API 全部通过；`agent_workspaces` 中不再存在第二份 Conversation 行为；共享
内核不依赖 Flow、Node 或 Attempt。

### 阶段 D：完整回归与架构守卫

- 平台定向与全量测试、Ruff、Pyright、OpenAPI；
- Web ESLint、typecheck、production build；
- 当前 Agent 工作台完整 Playwright；
- 固定 OpenHands contract/smoke；
- 真实模型、流式事件、工具、附件、能力、文件和多终端；
- 浏览器刷新、API/Web 重启、Agent Runtime replacement 和原 ID reload；
- 增加静态守卫，防止共享层 import Flow/Node/Attempt 或再次出现第二套实现。

退出条件：当前外层 Agent 会话完全使用共享实现，所有现有功能通过，且没有触碰节点和流程行为。

完成阶段 D 后立即停止。节点接入必须另起方案或切片，经用户再次审核后才能开始。

## 7. 明确不能动的地方

### 7.1 不能改变 Agent 会话产品行为

- 不删除或降级第 3 节任何能力；
- 不改变 `/agent` 和 `/agent/conversations/:bindingId`；
- 不改变现有会话列表、工作目录分组、标题、模型、能力、来源、文件或终端数据；
- 不重新创建 binding 或 OpenHands Conversation；
- 不改变首条消息 bootstrap、ambiguous delivery、正式事件身份和模型 rebind 语义；
- 不借组件拆分进行视觉重做、交互删减或产品命名调整；
- 不引入新旧双写、双读或两套 feature flag 实现。

### 7.2 不能动节点和流程

- 不修改节点进入会话的现有代码，即使它目前有 bug；
- 不修改节点启动提示词；
- 不修改节点工作空间和目录；
- 不修改 NodeRun、Attempt、门禁、产物、验收或流程状态机；
- 不修改 FlowRun Conversation locator、旧公开 API 或旧页面；
- 不修改 FlowRun Runtime 的预置、单容器、generation、fencing、lease 和 replacement；
- 不尝试让节点在本阶段使用新共享组件。

### 7.3 不能污染共享会话内核

- 不出现 Flow、FlowRun、Node、Attempt 专用字段或条件；
- 不把未来节点需求猜测成当前接口；
- 不用 owner 字符串、URL 前缀或 execution key 猜测业务来源；
- 不允许宿主 adapter 覆盖消息、事件、模型、能力、附件、文件或终端行为；
- 不复制 controller、router、service、event projector、Composer 或 Terminal 实现。

### 7.4 不能改变事实边界

- OpenHands 继续是消息、事件树、HEAD、执行状态和 Tool 生命周期的唯一事实源；
- FlowWeave 不保存消息或事件副本，不展示隐藏推理；
- Action/Observation 仍只按正式 `action_id`、`tool_call_id` 关联；
- 容器仍是可替换载体，外置 Workspace、Conversation state、Secret 和 locator 保持不变；
- 不修改 OpenHands 源码，不用私有协议模拟其正式能力。

## 8. 验收标准

只有同时满足以下条件，才算“Agent 会话完整迁移为共享实现”：

1. 当前 `/agent` 只渲染一个 `AgentSessionWorkbench` 完整组件树；
2. `AgentWorkbenchPage` 只是薄入口，不包含会话业务状态机；
3. 前端只有一个事件合并器、一个 WebSocket controller、一个 Composer、一个能力管理器和一个终端实现；
4. 后端 Agent Workspace 会话路由只调用 `agent_sessions` 共享 facade；
5. bootstrap、消息、事件、模型、fork、rerun、condense、能力、附件、文件、终端和标题只有一套应用逻辑；
6. 第 3 节列出的全部功能通过原有和新增回归；
7. 现有 binding ID、OpenHands conversation ID、事件 identity、工作区文件和终端数据不变；
8. `/agent` 稳定 URL、API 路径、JSON、错误码和用户可见交互无回归；
9. Runtime kill/replacement 后原 Conversation 和文件正常恢复；
10. 共享前后端代码不 import Flow、FlowRun、Node 或 Attempt；
11. 本次 diff 不包含 WorkbenchPage、orchestration、runs、FlowRun conversation、节点目录或 Runtime Provider
    行为改动；
12. OpenHands 源码未修改，FlowWeave 未新增消息、事件、HEAD、cursor 或推理副本。

## 9. 后续阶段边界

本方案通过并实施完成后，平台将拥有一套经过当前 Agent Workspace 实际验证的共享 Agent 会话能力。之后
才能另行设计“节点如何调用这套能力”。后续方案可以讨论节点已有的启动提示词、独立工作空间和流程管控，
但必须遵守：

- 节点只能调用共享 Agent Session 页面和后端内核；
- 节点只提供其已有业务上下文和管控参数；
- 不允许恢复或复制旧节点会话实现；
- 对共享会话功能的任何后续修改，顶层和节点入口必须同时生效。

这些是后续接入约束，不是本次实施内容。审核通过前，本方案不修改任何生产代码、数据库、API、Worker、
Runtime Provider、Flow、FlowRun 或节点行为。
