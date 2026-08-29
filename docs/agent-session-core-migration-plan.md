# Agent 会话内核完整迁移方案（审核稿）

> 状态：`IN PROGRESS — 已批准，按 A1–A5 分切片实施`
>
> 日期：2026-08-29
>
> 本文只覆盖一级 `Agent 会话` 的完整迁移和收口。FlowRun、节点、Attempt 的会话接入均不在本次实施范围。

## 1. 要解决的问题

当前一级 `/agent` 已经是验证会话能力的产品入口，也已经具备一个完整工作台、OpenHands Runtime、会话
绑定、文件、终端、模型、能力和实时事件能力。但它还不是可以被其他宿主安全复用的**唯一会话内核**：

- Web 的 `AgentSessionWorkbench` 虽可注入 gateway/host，内部仍把查询 key、变量名和部分产品语义写成
  `workspace`；
- 后端完整会话逻辑已放入 `modules/agent_sessions/application/conversations.py`，但仍直接依赖
  `agent_workspaces` 的模型、Runtime 和工作目录服务；
- `agent_workspaces` 同时承担“默认 Agent 宿主”和“会话领域实现”两种责任，后续节点若直接接入，容易再次
  复制代码，或把 `if flow_run` 分支扩散进一级会话逻辑。

本次的唯一目标是：把 `/agent` 迁移并固化为**唯一完整的 Agent 会话产品和会话内核**。后续无论什么入口
需要打开会话，都只能提供宿主上下文并装配同一内核；它们不能复制页面、API 语义、事件处理或 OpenHands
调用。

本次不实现第二个入口，也不改变任何 FlowRun/节点行为。

## 2. 最终效果

用户仍通过 `/agent` 使用 Agent 会话，视觉、操作和已有 URL 均不变。其背后的结构变为：

```text
一级 /agent
  │
  ├─ Agent Workspace 宿主适配器
  │    └─ 默认长期 Runtime、默认工作目录、默认能力/模型策略
  │
  └─ AgentSessionWorkbench（唯一 React 页面、状态和交互）
          │
          └─ AgentSessionGateway（唯一浏览器传输合同）
                   │
                   └─ agent_sessions（唯一会话应用内核）
                            │
                            └─ OpenHands 正式 Conversation/Event/Tool 生命周期
```

具体保证：

1. `/agent` 的会话列表、创建、首发消息、流式事件、停止/恢复、确认、改名、删除、分叉、压缩、重跑、模型
   切换、附件、能力、文件和终端都由同一套会话内核负责。
2. `AgentWorkspace` 只保留默认宿主职责：长期 Runtime 的预置/恢复、默认根目录、默认模型和能力设置，以及
   对一级 `/agent` 路由的参数解析；不再拥有会话行为的实现。
3. 会话内核只接收服务端解析、不可由浏览器伪造的 `AgentSessionHostContext`。该上下文提供会话范围、当前
   Runtime、冻结工作目录、能力/模型策略和读写权限；内核不认识 FlowRun、节点或 Attempt。
4. OpenHands 仍是消息、事件树、HEAD、cursor、运行状态与 Tool 事件的唯一事实源。FlowWeave 仅保存会话
   locator、展示标题、命令幂等性、授权和审计。
5. 以后新增任何宿主时，只有“宿主解析器 + gateway 装配”两个薄适配点；不会出现第二份页面、第二份
   WebSocket/SSE 处理、第二份 Composer，或第二份会话服务。

## 3. 本地修改范围

以下是实施时允许修改的本地位置。名称表达目标职责；精确文件拆分以实施时的最小 diff 为准。

| 区域 | 主要位置 | 修改目标 |
|---|---|---|
| 共享 Web 页面 | `apps/web/src/components/agent-session/AgentSessionWorkbench.tsx` | 清除内部对 `workspace` 的宿主假设，改为中性的 host/session scope 视图模型；保留一份页面和全部交互状态。 |
| Web 宿主合同 | `apps/web/src/components/agent-session/session-host.ts` | 扩展为仅描述 URL、页面标题、恢复命名空间和宿主展示信息的合同；不得放入会话状态或业务判断。 |
| Web gateway | `apps/web/src/api/agent-session-gateway.ts` | 将仍带 `agentWorkspace` 含义的传输字段收敛为中性会话方法；默认 gateway 继续逐项映射现有 `/agent-workspaces/*` API，保证 `/agent` 无行为改变。 |
| 一级路由宿主 | `apps/web/src/pages/AgentWorkbenchPage.tsx`、`apps/web/src/App.tsx` | 保持 `/agent` 和 `/agent/conversations/:bindingId` 的兼容入口，只负责装配默认 host/gateway；不得重新实现 UI。 |
| 会话领域内核 | `services/platform/src/flowweave/modules/agent_sessions/` | 建立明确的 host context、locator、会话命令、OpenHands Runtime bridge 和文件/终端访问边界。`conversations.py` 中的逻辑按职责拆分，但所有公开会话行为都留在本模块。 |
| 会话持久化 | `services/platform/src/flowweave/modules/agent_sessions/infrastructure/`、新的 Alembic migration | 将会话 binding、命令、附件、会话能力等“会话自身事实”迁为中性 schema。保留原始 `runtime_session_id`、`openhands_conversation_id`、标题和时间；绝不复制 OpenHands 消息/事件。 |
| 默认宿主适配器 | `services/platform/src/flowweave/modules/agent_workspaces/` | 仅保留 `AgentWorkspace` 的默认 Runtime、目录、模型/能力设置和一级 API 适配；调用 `agent_sessions.public`，不再实现会话生命周期。 |
| 一级 API 路由 | `services/platform/src/flowweave/modules/agent_workspaces/presentation/router.py`、`apps/web/src/api/client.ts` | URL 和请求/响应兼容优先。路由只解析默认宿主、授权和输入，并调用共享内核；不能保留平行的消息、事件或控制服务。 |
| Worker/Runtime 接线 | `services/platform/src/flowweave/bootstrap/worker.py`、`services/platform/src/flowweave/modules/tasks/` | 只调整默认 Agent Workspace Runtime 的启动、恢复与健康接线，使它通过宿主适配器为共享内核提供 active Runtime；不改变 Runtime Provider 语义。 |
| 测试与文档 | `services/platform/tests/`、`apps/web/tests/`、本文件及相关 Agent 工作台设计 | 以 `/agent` 回归和“唯一实现”静态边界为验收；记录迁移、回滚和已知兼容边界。 |

## 4. 核心设计

### 4.1 中性宿主上下文

会话内核新增不可变的 `AgentSessionHostContext`，只由服务端解析器创建。最小字段为：

```text
host_kind                 # 当前只有 AGENT_WORKSPACE
host_id                   # 当前默认 Agent Workspace 的稳定 ID
conversation_scope_id     # 可列出、读取和写入的会话范围
runtime_session_id        # 当前逻辑 Runtime 身份；非 endpoint/container ID
working_directory         # 服务端冻结的容器内逻辑目录
runtime_manifest          # 已冻结的能力/策略引用
model_policy              # 可创建/切换模型的边界
permissions               # list/read/create/write/terminal/files 等显式权限
```

内核通过该对象完成列表、创建、读写、文件、终端和控制命令。它不得接受浏览器提交的路径、Runtime ID、
容器 endpoint 或权限判断结果。

本次只实现 `AgentWorkspaceSessionHostResolver`。不存在 FlowRun resolver、节点 resolver 或它们的路由。

### 4.2 单一会话持久化合同

会话 binding 从“默认工作区的实现细节”中抽离为 `agent_sessions` 的通用 locator。迁移采取 expand →
backfill → switch → retire 四步：

1. **expand**：添加中性表/列、约束和索引，不删除现有表；
2. **backfill**：从现有 Agent Workspace binding 精确迁入；每条记录必须保留同一个 OpenHands conversation
   ID、Runtime Session ID、工作目录版本、标题和审计时间；
3. **switch**：共享内核和默认宿主改读写新 locator；旧表只读兼容，禁止双套业务状态；
4. **retire**：在迁移验证、发布观察期和回滚窗口结束后，另行切片删除旧会话表和仅为其服务的代码。

不能证明 identity 或 Runtime 归属的历史记录保持只读归档并输出可诊断错误；不能猜测、重建空会话或替换
OpenHands conversation ID。

### 4.3 Web 的唯一表面

`AgentSessionWorkbench` 是唯一可以渲染会话内容、composer、流式事件、确认、模型、附件、文件和终端的
React 组件。它只依赖：

- `AgentSessionHost`：路径编解码、展示名称与浏览器恢复命名空间；
- `AgentSessionGateway`：与宿主 API、文件 URL、终端 URL、流订阅的传输合同；
- 标准化的 host/session DTO：不出现 `AgentWorkspace` 的私有字段。

默认 Agent Workspace gateway 是该接口的第一份、也是本阶段唯一的实现。`/agent` 的 query 参数、URL、
刷新恢复与错误展示保持兼容，避免“迁移完成但原会话无法打开”。

### 4.4 OpenHands 与 Runtime 边界

共享内核只经现有 Runtime Provider/OpenHands 正式接口创建、加载和操作 Conversation。它不：

- 将 Event、消息、HEAD 或 cursor 投影为平台聊天数据库；
- 解释或拼接 OpenHands 私有状态文件；
- 建立平台自有 agent loop、文本协议或执行器；
- 把 container ID、endpoint、API key、Secret 或宿主机路径返给浏览器。

默认 Agent Workspace 继续使用其长期、可替换的 Runtime 和外置持久目录。容器替换时必须按相同
OpenHands conversation ID reload；文件、终端和会话消息的恢复边界不变。

## 5. 实施顺序（每项独立提交）

### A1：冻结 `/agent` 回归基线

- 补全一级工作台的 API/页面清单、现有 URL 与 DTO 契约快照；
- 建立最小端到端回归：创建、首发消息、刷新恢复、流式事件、文件、终端、模型、附件、确认和 Runtime
  replacement 后原 ID reload；
- 加入静态边界检查，确保 `AgentSessionWorkbench` 是唯一完整会话页面。

产物：可作为后续重构护栏的自动化基线；不改功能。

### A2：定义并接入中性 host/gateway 合同

- 完成 `AgentSessionHost`、`AgentSessionGateway` 与标准化 DTO；
- 让默认 Agent Workspace host/gateway 适配现有 API；
- 去除 Workbench 中由宿主泄漏出的 query key、文案、恢复 key 和变量假设。

验收：`/agent` 全部 UI/交互无行为变化，且 Workbench 不直接导入 Agent Workspace API 或特有类型。

### A3：会话内核与默认宿主分层

- 将会话 lifecycle、OpenHands 调用、事件、命令、附件、模型、能力、文件和终端权限收敛到
  `agent_sessions`；
- 以 `AgentWorkspaceSessionHostResolver` 提供当前的 Runtime、目录和策略；
- `agent_workspaces` 仅保留默认宿主资源和 API 适配。

验收：`agent_sessions` 不再直接引用 `agent_workspaces` 的 ORM 模型或应用服务；反向依赖只发生在默认宿主
适配层，且不会让会话内核知道默认 Workspace 的具体实现。

### A4：中性 locator 数据迁移

- 编写可升级/降级的 additive migration 与可审计 backfill；
- 切换 `/agent` 到新 locator；
- 对迁入前后逐条核对 binding、Runtime Session、OpenHands Conversation ID、目录、标题与时间。

验收：已存在的 `/agent` 会话可直接打开；无消息或事件被复制进 FlowWeave；失败记录被保留且可诊断。

### A5：删除一级会话重复实现并发布验证

- 删除已经被共享内核取代的默认 Workspace 会话实现、临时兼容别名和死代码；
- 全量回归迁移、重启、Runtime replacement、文件/终端、流式事件与浏览器刷新；
- 完成无缓存构建、部署及部署后真实 `/agent` E2E。

本阶段完成的判定是：一级 Agent 会话是唯一完整实现，且所有已有 Agent 会话能力在真实部署上可用。它不
包含、也不暗示任何 FlowRun 或节点入口已经切换。

## 6. 明确不能动的地方

本方案审批和实施期间，以下内容均不在修改范围：

1. **FlowRun、节点、NodeRun、NodeAttempt 与流程编排代码**：不新增 node session resolver，不迁移旧
   FlowRun binding，不改节点启动门禁、提示词、工作目录隔离或 UI。
2. **当前未提交的 FR-93 节点宿主解析器改动**：保持原状、暂停处理；不继续、提交、回退或混入本方案的
   Agent 会话提交。恢复节点工作时需由负责人单独决定其取舍。
3. **OpenHands 源码、固定版本、镜像与正式事件契约**：只读，绝不通过补丁、私有 HTTP 或提示词模拟上游
   能力。
4. **Agent Workspace 的产品行为和公开兼容入口**：`/agent`、`/agent/conversations/:bindingId`、默认 Runtime
   预置、已有会话数据、工作区文件和用户可见功能不能被删除、重置或悄悄换语义。
5. **Runtime Provider 的安全边界**：外置持久目录、generation fencing、Secret、endpoint 隐藏、镜像 digest
   与容器替换协议不能为本次代码整理而削弱。
6. **OpenHands 的事实所有权**：不得新增平台消息表/事件树/cursor/HEAD 副本，不得把浏览器缓存升级成
   会话事实。
7. **历史数据安全性**：不得 destructive migration、猜测补全 locator 或删除不能迁入的记录。

## 7. 关键注意事项

- 不以“复制当前 `/agent` 页面到节点”作为后续捷径；复用的验收标准是同一个组件文件、同一个核心服务和
  同一个 OpenHands bridge，而不仅是样式相似。
- host 只能在边界解析，不能把 `host_kind` 分支散布进每一个会话函数。共享内核只处理标准
  `AgentSessionHostContext`。
- 一个会话的工作目录、Runtime Session 与 OpenHands Conversation ID 都是服务端绑定事实。任何修改都必须
  在客户端不可篡改、Runtime replacement 后仍可重建的前提下进行。
- 数据库切换必须保留可回滚的双读窗口；业务层不能长期双写，避免新的两份事实源。
- 文件和终端接口与消息接口同样必须经 host context 授权，不能因为它们看起来是辅助功能而直连默认
  Workspace 路径。
- 首先验证 `/agent` 真正的模型调用和 Tool 行为；只通过页面渲染或 mock stream 不能算迁移完成。

## 8. 审核后需要确认的决策

1. 同意本期止于 `/agent` 的内核收口，节点/流程接入另开批准切片；
2. 同意将现有 Agent Workspace 会话 binding 迁到中性 `agent_sessions` locator，而不是长期保留
   `agent_workspaces` ORM 作为共享内核的依赖；
3. 同意保留现有 `/agent` URL/API 行为作为严格兼容目标；
4. 同意历史无法精确证明 OpenHands identity 的记录只读归档，不自动造新会话；
5. 同意 A1–A5 每一步独立审查、提交和验证，任何一步失败均停止在该步修复，不提前开始节点接入。
