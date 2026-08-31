# Agent 会话与 FlowRun 节点宿主整合设计

> 状态：`FR-90 FROZEN`
> 日期：2026-08-29
> 前置：`agent-session` 共享页面、`agent_sessions` 后端 facade、`AgentSessionGateway`
> 关联设计：`docs/agent-workbench-technical-design.md`、`docs/flowrun-openhands-runtime-design.md`

## 1. 目标

将节点中“进入会话”迁移为同一个 Agent 会话产品，而不是重做一个类似聊天页。最终用户无论从一级
`/agent` 还是从某个 FlowRun 节点进入，看到、操作和演进的都是同一套：

- `AgentSessionWorkbench` 页面组件、Conversation Surface、Composer、附件、文件、终端、能力、模型、
  原生控制、恢复与错误状态；
- `agent_sessions` 领域内核的会话生命周期、OpenHands 事件投影、消息与控制、模型和能力逻辑；
- OpenHands 原生 Conversation/Event/HEAD/runtime 行为。

FlowRun/节点不是第二种会话产品。它们仅提供会话宿主上下文：已运行的 FlowRun Runtime、当前节点
Attempt 的授权与启动门禁、被冻结的节点工作目录，以及可选的节点启动提示词。

## 2. 不可变约束

1. 不复制 `AgentSessionWorkbench`、Composer、WebSocket、文件抽屉、终端或 React 状态机；旧
   `AgentChatPage` 必须删除，不能保留为平行功能页。
2. 不复制 `agent_sessions` 会话业务。FlowRun 模块只能实现宿主适配和流程授权，不能重新实现消息、
   事件、模型切换、分叉、压缩、附件、能力或标题逻辑。
3. 不改变一级 Agent Workspace 的默认容器、路由、数据或产品行为。它仍使用自身长期 Runtime。
4. 一个 FlowRun 只使用一个已预置且可替换的 OpenHands Runtime/container；创建、打开或切换任意节点
   会话都不得启动第二个容器。
5. 同一节点可以有 N 个独立 OpenHands Conversation；不同节点的会话集合独立。每个会话保留自己的
   OpenHands Conversation ID 与事件树。
6. 节点逻辑工作区必须隔离，而 FlowRun 的项目根继续共享并挂载为
   `/runtime/workspace/project`。每个节点/Attempt 只能列出、创建和选择自己拥有的逻辑工作区；它们的
   目录范围在会话创建时冻结。客户端不能传宿主机路径、绝对路径或 `..`。无法证明创建 Attempt 的历史
   FlowRun 级工作区保持归档，不显示给任一节点。
7. 节点启动提示词是唯一的节点特有输入：在显式“启动节点会话”命令中以正式第一条 user event 写入；
   不通过系统提示词、私有 JSON 或平台消息副本模拟。
8. 不修改 OpenHands 源码；不持久化消息、事件、HEAD、cursor 或浏览器临时状态；不暴露容器 endpoint、
   Docker 信息或 Secret。

## 3. 目标结构

```text
一级 /agent                              FlowRun 节点“进入会话”
Agent Workspace host                     FlowRun node-session host
  |                                        |
  +---------- AgentSessionHost -----------+
                       |
                       v
            AgentSessionWorkbench（唯一页面/状态/交互）
                       |
                       v
           AgentSessionGateway（唯一 UI 传输协议）
                       |
                       v
      agent_sessions（唯一会话应用内核 + OpenHands 适配）
             ^                         ^
             |                         |
 Agent Workspace host adapter   FlowRun/node host adapter
  默认 Runtime/工作目录          Run Runtime/节点 Attempt/固定目录/首发提示
```

`AgentSessionHost` 是共享 UI 的路由与产品上下文；`AgentSessionGateway` 是已存在 API/WS/文件 URL
的传输注入。二者都不能承载会话状态机。共享内核只接受一个经验证的宿主上下文，不以
`if flow_run` 或 `if node` 散布到会话业务函数中。

## 4. 宿主上下文合同

FlowRun/node 适配器必须在每次读写前解析如下不可伪造的上下文：

| 字段 | 来源 | 用途 |
|---|---|---|
| `host_kind` | 服务端常量 | `AGENT_WORKSPACE` 或 `FLOW_NODE`，只用于适配分派 |
| `host_id` | Workspace ID 或 FlowRun ID | 查询、权限、运行时定位 |
| `runtime_handle` | 当前 active generation | 使用同一 Runtime，不向浏览器返回物理地址 |
| `conversation_scope_id` | Workspace 或 Node/Attempt scope | 只列出本宿主合法会话和逻辑工作区 |
| `working_directory` | 服务端推导的共享根或冻结逻辑目录 | OpenHands `LocalWorkspace` 工作目录 |
| `runtime_manifest` | Workspace 默认/Run Snapshot | 物化受治理能力与策略 |
| `model_policy` | Workspace 设置/节点冻结配置 | 创建与切换时可用模型边界 |
| `start_permission` | Attempt 状态与输入门禁 | 只允许显式节点启动创建首会话 |
| `startup_prompt` | 节点启动命令 | 仅首发正式 user event，可为空 |

读取既有会话不要求 Attempt 再次处于可启动状态；新建、首发、写消息、终端与控制操作必须分别按照
FlowRun 的终态和当前 Runtime write fence 重新校验。FlowRun 完成/取消后历史会话可读，所有新写入
fail closed。

## 5. 数据与迁移策略

最终会话 binding 使用共享会话 schema，不以 `AgentConversationBinding` 或
`FlowRunConversationBinding` 两套独立业务实现为长期状态。共享 binding 至少保存：

```text
id / host_kind / host_id / scope_id
runtime_session_id / openhands_conversation_id
working_directory / frozen capability/model references
display title projection / command idempotency / audit references
created_at / updated_at / last_connected_at
```

FlowRun adapter 额外保存 `flow_run_id`、`node_run_id`、`node_attempt_id` 与节点工作目录冻结引用，
它们是授权、lineage 和审计事实，不是第二个会话状态机。历史
`flow_run_conversation_bindings` 在迁移中只读保留，并用可证明的 locator 显式迁入；无法证明 Run、
Runtime、节点目录或 OpenHands identity 的记录保持归档，不猜测补全。

## 6. API 与 Web 路由

新增 FlowRun 节点宿主路由的响应形状必须与共享 gateway 所需的会话 API 相同，但 URL 只表达宿主定位：

```text
/flow-runs/{runId}/node-attempts/{attemptId}/agent-sessions
/flow-runs/{runId}/node-attempts/{attemptId}/agent-sessions/{bindingId}
.../bootstrap, .../events, .../stream, .../messages, .../files, .../terminal
```

路由只做参数校验、鉴权、host adapter 解析和调用 `agent_sessions.public`。不会回调旧
`modules.conversations.application.service`。

Web 使用稳定路由：

```text
/flow-runs/{runId}/nodes/{nodeRunId}/attempts/{attemptId}/agent-sessions
/flow-runs/{runId}/nodes/{nodeRunId}/attempts/{attemptId}/agent-sessions/{bindingId}
```

`AgentSessionWorkbench` 接收 host 的路径编解码和返回运行详情动作；其内部不得硬编码 `/agent`。
浏览器刷新仅依据 URL 重新解析此宿主，不依赖 Zustand 或 localStorage 中的 Run/Attempt ID。

## 7. 实施顺序

1. 提取共享会话 binding/host protocol，并让 Agent Workspace adapter 使用它，现有 API 行为不变。
2. 将旧 FlowRun locator 和运行时操作改为 FlowRun host adapter，完成共享 binding 的迁移和最窄后端回归。
3. 为 FlowRun/node host 提供完整 shared-gateway API 和 websocket/file/terminal 代理；删除旧 Conversation
   API 服务逻辑，不删除仍被归档迁移读取的模型前先完成数据回填。
4. 让共享 Workbench 接收 host route contract；`/agent` 无行为变化。
5. 用 FlowRun gateway 装配同一个 Workbench，替换并删除 `AgentChatPage`、其 CSS、store view 与旧入口。
6. 补齐节点启动提示词和节点目录隔离的首发验证；一个节点 N 会话、多个节点隔离、一个 Run 一个容器。
7. 执行迁移、重启/替换、真实 E2E、无缓存部署和生产回归。

每项均为独立切片，完成后单独提交。第 5 步前不得宣称节点页面已经复用；第 7 步前不得宣称改造完成。

## 8. 必须删除或不得改动的部分

完成后删除：

- `apps/web/src/pages/AgentChatPage.tsx` 与仅服务旧 FlowRun 聊天页的 CSS、路由和 Zustand `agent-chat` 状态；
- 旧 FlowRun 会话的独立 Timeline、Rail、composer、stream 与 stop 实现；
- `modules/conversations` 内与共享会话重复的业务逻辑。

本整合不得改动：

- OpenHands 源码、固定版本、正式事件身份与持久化文件；
- Agent Workspace 的默认 Runtime 拓扑、已有 `/agent` URL 或完整产品功能；
- FlowRun 一个 Runtime、外置持久目录、generation fencing、Snapshot/Environment 冻结；
- 节点/流程编排的非会话执行语义，除“启动节点会话”的必要调用替换。

## 9. 最终验收

1. Agent Workspace 全部既有会话回归通过，且页面与后端只有一份实现。
2. 从一个 FlowRun 的两个节点各进入会话，页面 DOM、交互、Composer、附件、能力、文件与终端均由同一
   `AgentSessionWorkbench` 提供；旧 `AgentChatPage` 不存在。
3. 一个节点可创建/打开多个会话和逻辑工作区；另一节点不可在列表、URL、文件或终端中看到或访问它们。
4. 同一 FlowRun 无论创建多少节点会话都只有一个 active Runtime container；所有会话以不同
   OpenHands Conversation ID 恢复。
5. 节点 A/B 的 Agent 创建文件只出现在各自冻结目录；终端初始目录与会话 working directory 一致。
6. 有启动提示词时，它恰好是首条正式 user event；无提示词时不写虚构消息。
7. 浏览器刷新、Runtime replacement 和 Run 终态均保持正确读写边界。
8. 迁移、Ruff/Pyright/pytest、Web lint/typecheck/build、Playwright、真实 Compose smoke、无缓存部署
   与部署后真实产品 E2E 通过。
