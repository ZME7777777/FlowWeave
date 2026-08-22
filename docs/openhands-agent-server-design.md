# OpenHands Agent Server 集成与运行时设计

> 历史文档：本文描述已删除的共享 Agent Server/Attempt/Conversation Runtime 架构，不再代表当前生产拓扑。当前冻结设计与实施状态分别以 `docs/flowrun-openhands-runtime-design.md` 和 `docs/flowrun-runtime-task-progress.md` 为准。

> 本文原描述 FlowWeave 旧代码中的实现，基于 OpenHands Agent Server、SDK、Tools 1.40.0。重点回答：OpenHands Agent Server 提供什么能力、如何提供和实现，FlowWeave 如何及何时调用，以及 Skill、MCP、Hook、模型和业务上下文如何交给 OpenHands。
>
> 未接入能力的核对以 FlowWeave 镜像中实际安装的 1.40.0 OpenAPI 和 Python 包为准，并参考 `/Users/zhengmengen/WorkSpace/openhands/software-agent-sdk` 源码解释实现。该源码检出点为 `v1.39.1-6-g6d597ff7`，与运行版本非常接近但并非完全相同；凡涉及路由是否存在、请求字段和默认值，均以本地 1.40.0 包为最终依据。

## 1. 核心结论

FlowWeave 不直接实现 Agent，也不是逐个调用“终端工具 API”。它把 OpenHands Agent Server 当作 RuntimePort 的生产执行器：FlowWeave 负责业务编排、Run Snapshot、Attempt、模型选择、能力物化、隔离资源、持久化任务、事件投影和人工状态机；OpenHands 负责单个 Conversation 内的模型循环、工具选择与执行、MCP 连接、Hook 触发以及最终 Finish。

| 层次 | FlowWeave 的职责 | OpenHands 的职责 |
|---|---|---|
| 业务 | Flow、Node、Attempt、输入输出契约、人工开始与验收 | 不理解 FlowWeave 领域模型 |
| 编排 | 后台任务、重试、租约、状态 CAS、轮询时机 | Conversation 执行状态和事件日志 |
| Agent | 选择模型，注入上下文和能力 | 推理循环、工具调用、Finish |
| 资源 | 创建 Runtime、挂载目录、网络、密钥和回收 | 使用已提供的 Workspace 与进程环境 |
| 数据 | PostgreSQL、Artifact、Run Event、Agent Message | Conversation 内部状态 |

关键边界如下：

1. OpenHands 是执行引擎，不是流程真相来源。Attempt、消息、产物、验收和取消结果以 FlowWeave 数据库为准。
2. 能力在创建 Conversation 时注册：Skill 进入 agent_context.skills，MCP 进入 agent.mcp_config，Hook 进入顶层 hook_config，业务上下文进入 system_message_suffix。
3. 真正运行发生在用户消息带 run: true 时。自动执行在创建 Conversation 时同时发送初始消息；协作会话先创建空 Conversation，用户发消息后才运行。
4. OpenHands 不获得 FlowWeave 数据库、Artifact 根目录或平台密钥解密能力。它只能看到明确挂载的节点工作区、只读能力目录、所选模型的运行时凭据和环境专属凭据卷。

## 2. 总体架构

~~~mermaid
flowchart LR
    U["用户 / Web"] --> API["FlowWeave API"]
    API --> DB[("PostgreSQL")]
    W["FlowWeave Worker"] --> DB
    W --> C["Sandbox Controller"]
    C --> R["隔离 Agent Runtime<br/>agent-server :8000"]
    W -->|"Conversation HTTP API"| R
    R --> LLM["模型服务"]
    R --> MCP["远程或 stdio MCP"]
    R --> WS["节点工作区"]
    R --> HA["只读 Hook / MCP 脚本"]
    W -->|"事件投影"| DB
    DB --> SSE["SSE / Agent 消息"]
    SSE --> U
~~~

系统里有两种 OpenHands 服务形态：

- 常驻 openhands-agent-server：Compose 基础服务，监听容器内 8000 端口；既可作为默认 Runtime，也是动态 Runtime 获取 Workspace bind mount 信息的来源。
- 动态 AGENT_RUNTIME：节点绑定已发布终端环境时，由 Worker 经 Sandbox Controller 为 Attempt 或 Conversation 创建。每个 Runtime 有独立容器、网络、会话密钥、节点工作区挂载和生命周期，容器内同样运行 agent-server。

因此，常驻基础服务并不意味着所有 Agent 必须共享一个执行容器。绑定环境后的执行走独立 Runtime。

## 3. OpenHands Agent Server 提供的能力

### 3.1 Conversation API

FlowWeave 实际使用以下 OpenHands HTTP 接口：

| 能力 | HTTP 调用 | 用途 |
|---|---|---|
| 创建 Conversation | POST /api/conversations | 一次性注册模型、工具、Skill、MCP、Hook、Workspace 和上下文 |
| 发送消息并运行 | POST /api/conversations/{id}/events | 普通聊天、继续执行和图片消息，请求带 run: true |
| 增量读取事件 | GET /api/conversations/{id}/events/search | 获取消息、思考、工具调用、工具结果、Finish 和错误 |
| 检查状态 | GET /api/conversations/{id} | 事件不能确定终态时检查 running、finished、error、stuck 或等待确认 |
| 切换模型 | POST /api/conversations/{id}/switch_llm | 某一轮显式选择不同模型或推理强度 |
| 中断 | POST /api/conversations/{id}/interrupt | steer、resume、停止和取消 |

所有请求携带 X-Session-API-Key。常驻服务使用根密钥；动态 Runtime 的密钥由根密钥、manager scope 和资源名通过 HMAC 派生，因此不需要单独持久化每个 Runtime 的明文密钥。

### 3.2 Agent 循环和模型

FlowWeave 创建的 agent.kind 固定为 Agent。OpenHands 根据以下配置构造并运行 Agent：

- LLM：模型名、Base URL、API Key、API 模式、可选推理强度；
- Tools：终端、文件编辑、任务跟踪；
- Agent Context：Skill 内容和系统消息后缀；
- MCP Config：远程或本地工具 Server；
- Hook Config：工具及 Session 生命周期的拦截逻辑；
- Workspace：Conversation 的工作目录；
- max_iterations：节点配置，默认 100。

模型名没有供应商前缀时会转成 openai/模型名。普通 API Key 模型按兼容 OpenAI 的 Base URL 调用。CODEX_OAUTH 模型会启用 Responses API、流式输出，关闭不支持的采样参数，并把 reasoning effort 放到请求体。

### 3.3 内置工具

FlowWeave 当前显式注册三个 OpenHands 工具：

| 工具 | 能力 | 边界 |
|---|---|---|
| terminal | 执行 shell、Git、语言工具链和 Skill 脚本 | 在 Runtime 容器中执行 |
| file_editor | 读取、创建和修改文件 | 受 Workspace 挂载限制 |
| task_tracker | Agent 内部拆解任务 | 不替代 FlowWeave 后台任务 |

Runtime 镜像还预装 shell、Git/SSH、Python 3.13、Node.js/npm/npx、uv、Java/Maven、常用 Unix 工具和 lark-cli。它们是 terminal 可调用的程序，不会自动变成独立 OpenHands Tool。

### 3.4 Skill

Skill 给模型提供可复用说明，并可附带脚本、参考资料、图片等资源。FlowWeave 将 SKILL.md 正文注册到 agent.agent_context.skills，例如：

~~~json
{
  "agent": {
    "agent_context": {
      "skills": [{
        "name": "requirements-analysis",
        "content": "# Requirements ...",
        "description": "...",
        "source": "/workspaces/nodes/<asset>/skills/requirements-analysis/SKILL.md",
        "is_agentskills_format": false
      }]
    }
  }
}
~~~

Skill 有两条并行通道：content 让模型获得规则；source、Skill 目录和系统上下文中的路径让 Agent 能通过 terminal 读取完整资源并执行脚本。

Skill 不是 FlowWeave 每轮代为执行的函数，而是创建 Agent 时注册的候选能力。模型根据当前消息决定是否使用；用户显式引用能力时，FlowWeave 会加入“本轮必须优先使用”的指令。

### 3.5 MCP

MCP Server 通过 OpenHands 原生 mcp_config 注册，注册后其 Tools 进入 Agent 的工具集合：

~~~json
{
  "agent": {
    "mcp_config": {
      "docs": {
        "url": "https://mcp.example.com/mcp",
        "transport": "streamable-http",
        "timeout": 30
      },
      "localTools": {
        "command": "mcp-tool-server",
        "args": ["--stdio"],
        "transport": "stdio",
        "cwd": "/runtime/capabilities/nodes/<asset>/mcp/localTools"
      }
    }
  }
}
~~~

当前允许传给 OpenHands 的配置字段包括 url、transport、command、args、env、cwd、description、icon、timeout、sse_read_timeout、keep_alive、headers、auth 和 enabled。兼容字段 type 会规范化成 transport，shttp 会规范化为 streamable-http。

远程 MCP 依赖 Runtime 网络；stdio MCP 依赖命令已安装在节点环境镜像中，或能力包提供可执行脚本。FlowWeave 负责注册配置，不在平台进程中代理每次 MCP Tool Call。

### 3.6 Hook

Hook 使用 OpenHands 原生顶层 hook_config。当前支持：

- pre_tool_use
- post_tool_use
- user_prompt_submit
- session_start
- session_end
- stop

示例：

~~~json
{
  "hook_config": {
    "pre_tool_use": [{
      "matcher": "terminal",
      "hooks": [{
        "type": "command",
        "command": "python /runtime/capabilities/nodes/<asset>/hooks/policy/scripts/check.py",
        "timeout": 30
      }]
    }]
  }
}
~~~

上传配置里的 type: script 不会把用户路径原样交给 OpenHands。FlowWeave 先校验清单、文件类型、数量、大小和 SHA-256，物化到独立只读目录，再转换成绝对路径的 type: command：sh 文件用 sh，py 文件用 python，js/mjs/cjs 文件用 node。

Hook 的实际触发和 matcher 判断由 OpenHands 在 Session 或工具调用前后执行；FlowWeave 负责配置安全化和文件边界，不参与每次 Hook 的同步调度。

### 3.7 Workspace

OpenHands 使用 LocalWorkspace，working_dir 指向 Attempt Session：

~~~json
{
  "workspace": {
    "kind": "LocalWorkspace",
    "working_dir": "/workspaces/nodes/<asset>/sessions/<run>/<node-run>/<attempt>"
  }
}
~~~

目录布局如下：

~~~text
/workspaces/nodes/<node-asset-id>/
├── skills/<capability-key>/
├── files/
├── repositories/
├── sessions/<run-id>/<node-run-id>/<attempt-no>/
├── .runtime/<capability-key>/
└── .flowweave-manifest.json

/runtime/capabilities/nodes/<node-asset-id>/
├── mcp/<capability-key>/
└── hooks/<capability-key>/
~~~

第一棵目录是节点工作区；第二棵是上传的可执行 MCP/Hook 资产，只读挂载，并刻意位于 /workspaces 之外，避免工作区软链接影响能力挂载目标。

### 3.8 事件与结果

OpenHands 返回异构事件，FlowWeave 将其归一化：

| OpenHands 事件 | FlowWeave 类型 |
|---|---|
| MessageEvent | MESSAGE |
| ActionEvent 的 ThinkAction | THOUGHT |
| 其他 ActionEvent | TOOL_CALL |
| ObservationEvent | TOOL_RESULT |
| FinishAction 或 FinishObservation | COMPLETED |
| 名称包含 error | ERROR |
| 其他 | STATE |

投影前，事件详情中名称包含 api_key、authorization、password、secret、token 的字段会脱敏；嵌套深度、集合长度和字符串长度也受限制。前端只消费 FlowWeave 的稳定事件和 Agent Message，不依赖 OpenHands 内部事件结构。

自动执行的正式输出由 Agent 在 Finish message 中返回：

~~~json
{
  "outputs": {
    "design": {
      "artifact_type": "URL",
      "uri": "https://example.feishu.cn/docx/xxx"
    }
  }
}
~~~

适配器只接受输出契约声明的字段，并只接受 https 的 feishu.cn、larksuite.com 或 larkoffice.com 官方域名。缺少必需输出时，Attempt 不会被视为成功产出。

## 4. FlowWeave 如何构造一次 Agent

### 4.1 字段来源与注入点

一次运行以 Run Snapshot 和 Attempt 为边界组装，而不是实时读取可编辑 Node Asset。

| 注入项 | 来源 | OpenHands 字段 |
|---|---|---|
| 模型与凭据 | Node executor、Model Provider、本轮选择 | agent.llm |
| 内置工具 | 适配器固定配置 | agent.tools |
| Skill 正文 | Snapshot 能力引用对应的 ZIP | agent.agent_context.skills |
| 业务上下文 | 提示词、输入、输出、历史、资源路径 | system_message_suffix |
| MCP | Snapshot 中规范化配置 | agent.mcp_config |
| Hook | Snapshot 配置和物化脚本 | 顶层 hook_config |
| 工作目录 | Attempt workspace_ref | workspace.working_dir |
| 首条消息 | 启动方式、提示词、启动 Skill | initial_message 或后续 events |
| 循环上限 | executor.max_iterations | max_iterations |

核心入口 build_runtime_request 的过程为：

1. 从节点快照解析资产；
2. 校验模型选择并取得运行时凭据；
3. materialize_node_workspace 解压 Skill、生成 MCP 配置和 manifest；
4. materialize_hook_config 生成 OpenHands 原生 Hook 配置；
5. 验证 Attempt 工作区属于选定节点且路径不存在软链接逃逸；
6. 构造与具体 Runtime 解耦的 StartAttemptRequest；
7. OpenHandsRuntime 再翻译成 Agent Server JSON。

### 4.2 创建请求形态

省略密钥后，自动执行请求等价于：

~~~json
{
  "workspace": {"kind": "LocalWorkspace", "working_dir": "/workspaces/.../1"},
  "max_iterations": 100,
  "agent": {
    "kind": "Agent",
    "llm": {
      "model": "openai/gpt-5.6-sol",
      "base_url": "https://model.example/v1",
      "api_key": "<runtime-only>",
      "usage_id": "flowweave:<provider-id>"
    },
    "tools": [
      {"name": "terminal", "params": {}},
      {"name": "file_editor", "params": {}},
      {"name": "task_tracker", "params": {}}
    ],
    "agent_context": {
      "skills": ["<materialized skills>"],
      "system_message_suffix": "<assembled context>"
    },
    "mcp_config": {"<server>": {"<normalized config>": "..."}}
  },
  "hook_config": {"<event>": ["<matchers and hooks>"]},
  "initial_message": {
    "role": "user",
    "content": [{"type": "text", "text": "<startup prompt>"}],
    "run": true
  }
}
~~~

协作会话创建时没有 initial_message。这样会先得到具备完整能力但尚未运行的空会话，不会把节点预置说明误当成用户任务。

## 5. 上下文如何交给 OpenHands

### 5.1 稳定的系统上下文

system_message_suffix 在 Conversation 创建时生成，并作为整个 Conversation 的稳定基线。它可能包含：

- 执行模式或协作模式说明；
- 节点 context_prompt；
- 协作模式下仅作背景的 startup_prompt；
- Attempt 输入 Artifact 的字段、说明、类型、正文或 URI、metadata、模板 URL；
- 自动执行输出字段、目标目录、运行名称、标题和模板 URL；
- 节点目录、文件目录、仓库目录；
- Skill 名称、入口、脚本及依赖运行器目录；
- MCP Server 名称和目录；
- 分叉协作会话的历史消息；
- 父 Agent 是否允许发起 FlowWeave 子 Agent 委派；
- Finish 输出协议和安全提示。

自动执行与协作模式的语义刻意不同：

- 自动执行：上下文明确这是流程任务，并包含输出契约；
- 协作会话：启动提示词只是背景，Agent 必须等待当前用户消息；能力是候选项，不因默认配置自动调用；协作消息不要求产出流程 Artifact。

### 5.2 当前用户消息和附件

普通消息通过 events 接口发送：

~~~json
{
  "role": "user",
  "content": [
    {"type": "text", "text": "<message>"},
    {"type": "image", "image_urls": ["data:image/png;base64,..."]}
  ],
  "run": true
}
~~~

附件先写入当前 Attempt 的 files/chat/Conversation/客户端消息目录，消息中给出 Agent 容器内绝对路径。受支持且不超过限制的图片还会编码成 data URL 发送给 OpenHands，从而同时支持 Workspace 文件读取和多模态输入。

### 5.3 能力引用的结构化语义

聊天输入框中的 $能力名 不依赖模型从纯文本猜测。消息会持久化 capability_refs：

~~~json
[
  {"capability_type": "SKILL", "capability_key": "architecture-review"},
  {"capability_type": "MCP", "capability_key": "docs"}
]
~~~

服务端先校验引用存在于当前 Attempt 的节点 Snapshot 中，否则返回 CAPABILITY_NOT_AVAILABLE。投递前生成明确指令：

~~~text
用户显式指定本条消息必须调用以下能力：
- Skill “architecture-review”：先读取并遵循该 Skill，再完成请求。
- MCP “docs”：优先使用该 Server 暴露的合适工具完成请求。

以下是用户原始消息：
……
~~~

Skill/MCP 本身早在 Conversation 创建时已注册，这一步只是本轮的强选择，并非临时安装。启动时选择 Skill 也采用相同原则：startup_capability_key 会把首条消息加上 $能力名。

## 6. 什么时机调用 OpenHands

### 6.1 自动节点执行

~~~mermaid
sequenceDiagram
    actor U as 用户
    participant API as FlowWeave API
    participant DB as PostgreSQL
    participant W as Worker
    participant C as Sandbox Controller
    participant OH as OpenHands
    U->>API: 确认开始 Attempt
    API->>DB: 状态 CAS + START_RUNTIME
    W->>DB: claim START_RUNTIME
    W->>W: 冻结输入并物化 Skill/MCP/Hook
    opt 绑定已发布环境
        W->>C: create AGENT_RUNTIME
        C-->>W: base_url + resource_name
    end
    W->>OH: POST conversations<br/>含 initial_message, run=true
    OH-->>W: conversation_id + cursor
    W->>DB: 保存 handle，投递 POLL_RUNTIME
    loop Agent 运行中
        W->>OH: GET events/search
        alt 事件不足以判定终态
            W->>OH: GET conversation
        end
        W->>DB: 投影事件、cursor 和状态
        W->>DB: 延迟投递下一次 POLL_RUNTIME
    end
    OH-->>W: Finish / Error / waiting_for_confirmation
    W->>DB: 登记输出或进入等待人工、失败、结束门禁
~~~

调用发生在 Readiness 与 START Gates 通过、用户确认开始、Worker claim 到 START_RUNTIME 后。FlowWeave 不会因上游节点完成而自动启动下游 OpenHands。

自动执行调用 OpenHandsRuntime.start，因此创建请求自带 initial_message 和 run: true。返回后持久化 runtime_job_id、conversation_id、cursor、adapter 和可选 sandbox ID，再由 POLL_RUNTIME 拉取。

OpenHands 报 waiting_for_confirmation 时，FlowWeave 转为 HUMAN_INPUT_REQUIRED；人工补充后，RESUME_RUNTIME 中断旧活动轮并发送新消息。

### 6.2 人工协作会话

~~~mermaid
sequenceDiagram
    actor U as 用户
    participant API as FlowWeave API
    participant DB as PostgreSQL
    participant W as Worker
    participant OH as OpenHands
    U->>API: 新建 Conversation
    API->>DB: 保存 + CREATE_CONVERSATION
    W->>OH: POST conversations<br/>无 initial_message
    OH-->>W: conversation_id
    W->>DB: Conversation = IDLE
    U->>API: 发送消息、能力引用、附件
    API->>DB: 保存 + DELIVER_CONVERSATION_MESSAGE
    W->>OH: 可选 interrupt
    W->>OH: 可选 switch_llm
    W->>OH: POST events, run=true
    W->>DB: 保存 cursor + POLL_CONVERSATION
    loop 当前轮运行中
        W->>OH: GET events/search
        W->>DB: 投影进度、工具事件和状态
    end
    W->>DB: 保存 final，Conversation = IDLE
~~~

协作会话调用 create_conversation，只注册能力和上下文，不立即运行。用户消息经持久化队列投递，使页面刷新、Worker 重启或短暂故障后仍可恢复。

同一 Conversation 的消息按队列顺序发送。steer 时先 interrupt 再投递；本轮选择新模型时先 switch_llm。动态切换只替换后续 LLM 配置，不会重建 Conversation、工具、能力和历史。

### 6.3 游标和轮询

OpenHands 的 page_id 是包含锚点本身的事件定位，不是“从下一条开始”。FlowWeave 持久化最后已投影事件 ID，读取第一页时定位锚点并丢弃它及更早事件，避免上一轮 FinishAction 被重放成下一轮结果。

如果 OpenHands 找不到锚点而回退到完整历史，适配器拒绝这批历史并保留原 cursor，避免重复投影。事件批次已有终态时不再 inspect；否则检查 Conversation 状态兜底。

### 6.4 取消和回收

取消先 interrupt，再短轮询状态，确认不处于 starting、running、executing 或 stopping。Conversation 404 按已停止处理，保证幂等。

动态 Runtime 的删除由 Sandbox 控制面负责，OpenHands 适配器不直接操作 Docker。即使 HTTP 端点随容器消失，控制面删除仍是权威结果。业务取消、Conversation 停止、idle/hard TTL 和对账最终都会推进回收。

## 7. 能力资产的物化

### 7.1 Skill ZIP

Skill 导入经过 validate/commit 两阶段。运行前按 Snapshot 引用的版本物化：

1. 校验 Artifact 存在；
2. 拒绝绝对路径、父目录穿越和目标逃逸；
3. 过滤 macOS 元数据；
4. 将选中 Skill 根解压到 skills/能力键；
5. 读取入口 SKILL.md；
6. 如有依赖包，验证构建状态并解压到 .runtime/能力键；
7. 生成 RuntimeSkill 和 .flowweave-manifest.json。

依赖运行器目录会作为 python 和 node 路径写入上下文。依赖不在 Agent 启动时临时联网安装，而是经过独立 Dependency Builder 和 Artifact 流程。

### 7.2 MCP 与 Hook 脚本

MCP/Hook 脚本不会进入节点可写目录，而是物化到 .managed-assets 对应宿主路径，再只读挂载为 /runtime/capabilities。校验包括：

- 文件清单与 hash 清单一致；
- 最多 20 个脚本；
- 解压总量最多 10 MiB；
- 只允许普通文件，不接受软链接或路径穿越；
- 每个文件 SHA-256 与导入记录一致；
- Hook 扩展名必须映射到受支持解释器。

每次物化前都会安全替换对应 managed 目录，防止复用遗留文件或预置软链接。

## 8. 动态 Runtime 的安全边界

节点绑定发布环境时，Agent 不运行在 API/Worker 进程中，API/Worker 也不接触 Docker Socket。Worker 请求 Sandbox Controller 创建 AGENT_RUNTIME，只有 Controller 操作 Docker。

| 维度 | 当前设计 |
|---|---|
| 身份 | 10001:10001，非 root |
| 根文件系统 | read-only |
| 临时写入 | 有界 /tmp 与 /runtime/workspace tmpfs |
| 工作区 | 只挂载选定节点子目录，可写 |
| 能力脚本 | 独立只读挂载 |
| 平台数据 | 不挂载数据库、Artifact 根、其他节点目录 |
| 网络 | 每个 Runtime 专属 bridge；固定 isolated 或 egress |
| Agent API | 每个资源独立派生 Session API Key |
| 环境凭据 | 每环境独立 Volume，Runtime 挂到 /home/flowweave |
| 生命周期 | ManagedSandbox、不可变 spec、idle/hard TTL、对账回收 |

isolated 禁止 Runtime 直接出网；外部模型、远程 MCP 或在线工具需要 egress。egress 只是允许 Docker NAT，不是域名白名单，生产仍需出口防火墙或代理。

模型 API Key 作为运行时秘密写入 OpenHands LLM 配置。平台不把 Lark OAuth token 从数据库注入 Agent；如需 lark-cli，用户在对应环境的 Setup Session 登录，凭据只存在环境专属 Volume，不进入发布镜像、Workspace 或 Agent 消息。

## 9. 适配器实现结构

RuntimePort 定义以下能力：

~~~text
create_conversation  创建空协作会话
start                创建并立即运行自动会话
read_events          增量读取并归一化事件
inspect              检查状态和终态
send_message         发送消息并运行
resume               中断后发送消息
switch_model         切换会话模型
interrupt            中断当前轮
cancel               中断并确认停止
~~~

生产实现为 OpenHandsRuntime，测试可显式使用 MockRuntime。Attempt 和 Conversation 都持久化 runtime_adapter，避免配置切换后把旧 handle 发给错误执行器。

RuntimeHandle 包含：

- conversation_id：OpenHands Conversation ID；
- cursor：最后已处理的事件 ID；
- job_id：默认等于 Conversation ID；动态环境使用 env-exec:资源名 或 env-chat:资源名，据此路由到容器和派生密钥。

## 10. 一致性与失败恢复

OpenHands HTTP 是数据库事务外副作用。FlowWeave 使用“短事务读取与冻结 → 事务外调用 → 新短事务 CAS 写回”：

1. Worker claim 带 lease_generation 的任务；
2. 事务内读取状态和输入，然后结束读事务；
3. 创建 Sandbox 或调用 OpenHands；
4. 再检查 lease；
5. 使用 state_version 和当前状态 CAS 绑定 handle 或写结果；
6. CAS 失败时取消新 Conversation 并请求删除 Sandbox；
7. 按 cursor 增量投影，Runtime Event ID 去重。

主要错误语义：

- Agent Server 不可达或拒绝请求：EXECUTOR_UNAVAILABLE；
- Conversation 404：RUNTIME_CONVERSATION_MISSING，上层可重建；
- 绑定环境但没有控制面分配：RUNTIME_SANDBOX_REQUIRED；
- Skill/MCP/Hook 不完整或摘要不符：RUNTIME_CAPABILITY_UNAVAILABLE；
- 模型未启用或凭据缺失：创建 Runtime 前拒绝；
- 轮询重试耗尽：Conversation 进入 FAILED，并追加可见错误；
- Worker 丢失 lease：迟到结果不能覆盖新 Worker 状态。

## 11. OpenHands 已提供但 FlowWeave 尚未充分使用的能力

### 11.1 如何理解“未使用”

OpenHands 1.40.0 的公开 Agent Server 不只是 Conversation 执行 API。它还暴露 Tools、实时事件、原生确认、Conversation 分支、上下文压缩、浏览器、直接 Bash、Git/File API、Skills/Plugins 市场、Agent Profile、原生子 Agent、MCP OAuth、长期记忆、ACP Agent、可观测性等能力。

这里将差集分成四类，避免把“上游存在”简单等同于“FlowWeave 缺陷”：

| 状态 | 含义 | 典型例子 |
|---|---|---|
| 未接入 | FlowWeave 没有传配置，也没有调用对应接口 | Browser Tool、MCP 测试、OpenAI 兼容 API |
| 隐式使用 | OpenHands 默认行为生效，但 FlowWeave 没有显式配置、展示或审计 | stuck detection、顺序 Tool 执行 |
| 部分接入 | FlowWeave 能识别一部分状态，但没有完成上游协议闭环 | Tool Action 人工确认 |
| 能力重叠 | FlowWeave 已用自己的领域设计实现相似功能 | 子 Agent、Conversation 分叉、能力仓库、Gate |

“能力重叠”通常不能直接替换为 OpenHands 原生实现，因为 FlowWeave 还需要 Snapshot、审计、权限、Artifact、租约和人工状态机。是否复用上游能力应先明确哪一层拥有事实来源。

### 11.2 总体差集与建议优先级

| 能力域 | OpenHands 1.40.0 能力 | FlowWeave 当前状态 | 主要缺口或取舍 | 建议 |
|---|---|---|---|---|
| Tool Action 确认 | 风险分析、确认策略、批准/拒绝挂起 Action | 部分接入 | 只识别等待状态，没有原生批准/拒绝闭环 | P0 |
| 长会话上下文 | Condenser、手动 condense、持久 Memory | 未接入 | 长对话可能逼近上下文窗口；无摘要可见性 | P0/P1 |
| MCP 上线前验证 | 连接、列工具、只读试调用、OAuth | 未接入 | 保存后到 Agent 创建时才暴露错误；OAuth 无产品闭环 | P1 |
| 费用与可观测性 | Token/cost stats、trace metadata/tags | 未接入 | 不能按 Run/Attempt/Conversation 做成本治理 | P1 |
| 实时事件 | Conversation/Bash WebSocket | 未接入 | 目前按秒轮询，延迟和请求量较高 | P1 |
| Conversation 分支 | 原生 fork、navigate、event tree | 能力重叠 | FlowWeave 分叉只注入文本历史，不复制 OpenHands 工具状态 | P1/P2 |
| Browser | Browser Tool、截图和录制 | 未接入 | 无网页交互 Agent；网络与凭据风险未设计 | P1/P2 |
| Agent 工具集 | grep/glob、planning editor、workflow、task/subagent 等 | 未接入 | 固定只开放 3 个 Tool，缺少节点级 Tool Policy | P1 |
| 原生子 Agent | Task Tool、Agent Definition、内置 Agent | 能力重叠 | FlowWeave 使用控制 JSON 和独立 Conversation 编排 | P2 |
| Skills/Plugins | 自动发现、安装、同步、Marketplace、热加载 | 能力重叠/未接入 | 与冻结 Snapshot 和安全导入模型冲突 | P2，谨慎 |
| Agent/LLM Profile | Profile CRUD、激活、切换 | 能力重叠 | FlowWeave 自己管理模型和 Node executor | 暂不接入 |
| Agent 类型 | OpenHands Agent 与 ACP Agent | 未接入 ACP | 无 Codex/Claude/Gemini ACP 运行时契约 | P2 |
| 直接运行时 API | Bash、File、Git、Workspace 静态服务 | 未接入 | 平台没有统一暴露 Runtime 诊断/产物 API | 按需 |
| IDE/桌面 | VSCode URL、Desktop URL | 未接入 | 隔离 Runtime 没有安全的反向代理和授权模型 | 按需 |
| 自评与精炼 | Critic、自动迭代精炼、Goal loop | 未接入 | 与 FlowWeave END Gate/人工验收职责可能重叠 | 实验性 |
| Warm pool | deferred init、运行时 POST /api/init | 未接入 | 每个动态 Runtime 直接完整启动，冷启动优化有限 | 规模化后 |
| OpenAI 兼容层 | /v1/models、/v1/chat/completions | 未接入 | FlowWeave 已直接使用 Conversation API | 无需接入 |

P0 表示当前语义存在明显缺口；P1 表示能显著提升可靠性、治理或体验；P2 表示需要产品与架构决策后再做。

### 11.3 P0：原生 Tool Action 确认协议没有闭环

OpenHands 原生支持：

- 创建 Conversation 时传 `confirmation_policy`，可选始终确认、从不确认或只确认达到风险阈值的 Action；
- 传 `security_analyzer` 对 Action 做风险分级；
- Conversation 进入 `waiting_for_confirmation`；
- 客户端调用 `POST /api/conversations/{id}/events/respond_to_confirmation`，对具体挂起 Action 批准或拒绝；
- OpenHands 按原始 Action ID 继续执行或生成 `UserRejectObservation`。

FlowWeave 当前只在 `inspect` 中把 `waiting_for_confirmation` 映射为 `HUMAN_INPUT_REQUIRED`，并显示通用问题“Agent 请求人工确认后继续执行”。人工回复后走的是 `resume`：先 `interrupt`，再发送一条普通用户消息。它没有调用 `respond_to_confirmation`，也没有持久化待确认 Action ID、Tool、参数、风险级别和批准/拒绝决定。

这两种语义不等价：普通消息不能保证释放 OpenHands 内部等待的原始 Action，也不能形成“某人批准了某个具体 Tool Call”的审计记录。建议优先补齐：

1. 扩展 Runtime Event/Result，携带待确认 Action 的 ID、Tool 名、参数摘要、风险和解释；
2. 增加 `RuntimePort.respond_to_confirmation(handle, action_id, approved, reason)`；
3. 将人工操作持久化为明确的 APPROVE/REJECT，而不是自由文本 resume；
4. 定义与 FlowWeave START/END Gate 的边界：Gate 审批流程阶段，OpenHands Confirmation 审批单次 Tool Action；
5. 对终端、浏览器、MCP 写操作分别制定默认策略；
6. 增加批准人、时间、Action digest、结果和重放保护。

在闭环完成前，不应把 `HUMAN_INPUT_REQUIRED` 描述成已经实现了 OpenHands 的原生安全确认。

### 11.4 P0/P1：上下文压缩与长期记忆未设计

OpenHands Agent 可以配置 `condenser`，1.40.0 提供 No-op、Rolling、LLM Summarizing 和 Pipeline 等实现，并提供 `POST /api/conversations/{id}/condense` 强制压缩。Agent Context 还支持：

- `load_memory`：读取用户或项目下的 `MEMORY.md` 索引；
- `load_project_skills`：从 Workspace 自动发现项目规则和 Skill；
- `user_message_suffix`：为每条用户消息追加稳定上下文；
- `current_datetime`：显式时间上下文。

FlowWeave 直接构造原始 `Agent`，没有传 condenser；它也没有启用 Memory 或项目自动发现。因此：

- 长协作会话依赖模型窗口和 OpenHands 的基础事件视图，没有产品层面的压缩策略；
- 无法向用户展示“哪些历史被摘要、摘要何时生成、消耗了哪个模型和费用”；
- Node Workspace 中即使存在 `.openhands/memory`、`AGENTS.md` 或项目 Skill，也不会自动成为上下文；
- FlowWeave 的固定 `system_message_suffix` 会保留，但旧轮次细节可能在长会话中失去有效可见性。

建议先接 Condenser，再决定是否接 Memory。Condenser 可按节点或会话配置阈值、专用模型、保留首尾轮数，并把 Condensation Event 投影到 FlowWeave。Memory 涉及跨 Attempt/跨用户信息边界，必须先定义所有权、可见范围、失效、删除、敏感信息和 Snapshot 可重放性，不能直接打开 `load_memory=true`。

### 11.5 P1：MCP 测试、Tool 探测和 OAuth 未接入

OpenHands 提供 `POST /api/mcp/test`，可以在不修改服务端状态的情况下：

1. 建立候选 MCP 连接；
2. 执行 `tools/list`；
3. 可选调用一个由用户指定的只读 Tool，以验证只在调用阶段生效的凭据；
4. 断开连接；
5. 对 OAuth Server 返回可持久化的 OAuth state。

还提供 `/api/mcp/oauth/start`、`/status/{job_id}` 和 `/callback/{job_id}` 完成 OAuth 流程。FlowWeave 当前只做 JSON/文件级校验和 Runtime 创建时注册，尚未做真实连接测试、工具清单预览、参数 Schema 展示、OAuth 或运行环境中的命令存在性探测。

建议把 MCP 生命周期设计成：

`静态校验 → 在目标环境中连通性测试 → 展示 Tool 清单 → 可选只读试调用 → 冻结已验证摘要 → 绑定节点 → Runtime 再验证`。

需要特别处理：

- 测试必须发生在与最终 Runtime 等价的网络和环境镜像中；
- Tool 试调用必须由用户选择并明确标注“只读”，平台不能猜测副作用；
- OAuth token 的加密、刷新、撤销、环境隔离和审计归属需要独立设计；
- 当前 FlowWeave 禁止能力配置保存 token/secret，不能简单把 OpenHands OAuth state 塞回现有 MCP JSON；
- stdio MCP 应验证目标环境内 `command`、工作目录和依赖，而不在 API 容器中测试。

### 11.6 P1：使用量、成本和可观测性没有进入 FlowWeave

OpenHands Conversation State 保存 `stats`，LLM 记录 Token、成本和模型指标；创建请求还支持 `user_id`、`observability_metadata`、`observability_tags` 和 `observability_span_name`，用于把业务关联信息挂到 Trace。FlowWeave 当前只给 LLM 设置 `usage_id=flowweave:<provider-id>`，没有读取 Conversation stats，也没有注入 Run/Attempt/Conversation 级 trace metadata。

因此当前缺少：

- 每个 Run、Node、Attempt、Conversation、子 Agent 的 Token 和成本；
- 主模型、Condenser、Critic、子 Agent 的费用拆分；
- 超预算前停止或降级模型；
- 从 FlowWeave Run Event 跳转到 OpenHands/OTel Trace；
- 模型切换前后成本对比；
- 运行时失败与具体 LLM/Tool latency 的关联。

建议扩展 Runtime Result/inspect 或增加 stats 读取接口，使用稳定低基数 metadata 关联 `flow_run_id`、`node_run_id`、`attempt_id`、`conversation_id`、`provider_id`，并定义预算是硬限制还是仅告警。API Key、消息正文和 Artifact 内容不得进入 observability tags。

### 11.7 P1：实时事件流未接入

OpenHands 提供：

- `/api/sockets/events/{conversation_id}`：双向 Conversation Event WebSocket；
- `/api/sockets/bash-events`：Bash Event WebSocket；
- 首帧 API Key 认证，避免 Key 出现在 URL 和代理日志；
- `resend_mode=all|since` 和 `after_timestamp` 补发。

FlowWeave 当前由持久化 `POLL_RUNTIME`/`POLL_CONVERSATION` 每隔固定秒数请求 `events/search`。轮询的恢复和 fencing 语义比较清晰，但会增加端到端延迟、HTTP 请求量和空轮询。

建议采用“WebSocket 作为低延迟唤醒，REST cursor 作为事实补偿”，而不是让 WebSocket 直接成为唯一真相来源：Worker 收到推送后仍按 event ID 拉取/投影，断线则回到轮询；这样可以保留当前幂等、租约和恢复模型。动态 Runtime 的容器销毁、Worker 多副本连接所有权、最大订阅数和背压都需要纳入设计。

### 11.8 P1/P2：原生 Conversation 分支能力未使用

OpenHands Conversation Event 是一棵可移动 HEAD 的树，并提供：

- `POST /api/conversations/{id}/fork`：复制完整或截至某个 Event 的历史与 Agent 状态，生成新 Conversation；
- `POST /api/conversations/{id}/navigate`：不创建新 Conversation，直接把 HEAD 移到某个 Event；
- `from_event_id`、`leaf_event_id`、指标是否重置等控制；
- Fork 后保留 Tool Observation、Skill 激活、Agent state 等结构化历史。

FlowWeave 的“从消息分叉”目前创建新的平台 Conversation，把分叉点之前的消息序列化进 `system_message_suffix`，再创建一个全新的 OpenHands Conversation。它保留的是可见对话文本，不是 OpenHands 原生事件树，因此不会继承：

- Tool Call/Observation 的结构化配对；
- OpenHands `agent_state`、已激活 Skill/Path Rule；
- Condensation 结果；
- 原始 Token/cost stats；
- 精确 Event HEAD。

这不一定错误：FlowWeave 当前方案更容易执行 Snapshot 隔离、换 Runtime、换模型和独立审计。但文档和 UI 应明确它是“语义分叉”，不是“运行时状态克隆”。如果需要高保真续写，可为同一 Sandbox/Workspace 内的分叉增加原生 fork 路径，并保存 source runtime conversation/event ID；跨环境、跨 Snapshot 或已回收 Runtime 时仍回退到文本基线。`navigate` 会修改同一 Conversation 的活动分支，审计和并发风险更高，不建议直接暴露给普通用户。

### 11.9 P1/P2：Browser、直接 Bash、IDE 与桌面能力未使用

OpenHands 已安装并注册 `browser_tool_set`，可进行网页导航、交互、截图和录制；Agent Server 还暴露直接 Bash API、Bash Event、VSCode URL 和 Desktop URL。FlowWeave 仅给 Agent 注册 terminal、file_editor、task_tracker，没有注册 Browser，也没有把 Runtime 的 VSCode/Desktop 安全代理到用户。

接入 Browser 前需要设计：

- 节点级是否允许浏览器及允许访问的目标；
- isolated/egress 网络下的可用性；
- Cookie、登录态、下载文件和截图的存储边界；
- 防 SSRF、内网探测和跨 Runtime 访问；
- Browser Tool Action 的确认策略；
- 录屏/截图是否进入 Artifact 与审计日志；
- 无头浏览器资源限额和容器镜像依赖。

直接 Bash API 与 Agent terminal 是两条不同链路：前者适合用户终端或诊断，不经过 LLM Tool Call；后者由 Agent 决策并进入 Conversation Event。若 FlowWeave 要提供 Conversation 旁路终端，必须清楚标识操作者、命令来源和结果，避免把人工命令误记为 Agent 行为。

VSCode/Desktop 还需要一次性访问凭据、反向代理、SameSite/CSP、Runtime 生命周期、端口暴露和多租户隔离。当前不应仅把上游 URL 原样返回浏览器。

### 11.10 P1：可选 Tool 集与 Tool Policy 尚未建模

1.40.0 实际注册的工具名包括：

~~~text
browser_tool_set
edit / file_editor / planning_file_editor
read_file / write_file / list_directory
grep / glob
terminal
task / task_tool_set / task_tracker
workflow / workflow_tool_set
~~~

FlowWeave 固定只传 `terminal`、`file_editor`、`task_tracker`。另外，SDK 内建的 `finish`、`think`、`invoke_skill`、`switch_llm`、视觉检查等由 Agent 内部按能力装配，不等同于上表的外部 Tool 注册项。

缺少的不是“把所有 Tool 全打开”，而是节点级 Tool Policy：

- 允许哪些 Tool；
- Tool 参数限制和并发度；
- 哪些 Tool 需要人工确认；
- 只读节点是否允许 file_editor/terminal 写入；
- Browser、workflow、task 是否可用；
- Tool 版本和模块来源如何进入 Snapshot；
- 未知 Tool 是否 fail closed。

Agent 还支持 `tool_concurrency_limit > 1` 并行执行同一步内多个 Tool Call。FlowWeave 没有设置该字段，当前使用默认值 1。开启并行会让文件系统、工作目录和 Conversation state 发生竞争，只有在 Tool 被标注为只读或具备资源锁时才应启用。

### 11.11 P2：OpenHands 原生子 Agent 与 FlowWeave 子 Agent 重叠

OpenHands 提供 Task/Delegate Tool、`agent_definitions`、`parent_conversation_id`、`POST /api/sub-agents`，并内置 `general-purpose`、`code-explorer`、`bash-runner`、`web-researcher` 等 Agent Definition。定义可包含独立 system prompt、模型、Tool、Skill、MCP、Hook、Condenser、迭代和预算。

FlowWeave 当前让父 Agent 输出受控 JSON，由平台创建最多四个独立 Conversation，等待它们结束后把结构化结果送回父 Conversation。这样做的优势是：

- 子 Agent 也是 FlowWeave 可见、可停止、可审计的实体；
- 复用平台租约、Sandbox、消息和失败恢复；
- 可以禁止子 Agent 继续委派；
- 不依赖 OpenHands 进程内注册表。

代价是没有使用 OpenHands Task Tool 的原生调用体验、共享缓存/上下文压缩和 Agent Definition 生态。短期建议保留平台编排作为事实来源；如需接原生 Task Tool，应先让其创建/回调映射到 FlowWeave Conversation，而不是形成平台不可见的嵌套执行。必须统一最大深度、并发、预算、取消传播和 Workspace 写冲突。

### 11.12 P2：Skills、Plugins 与 Marketplace 的受治理接入

Agent Server 自带：

- Skills：发现 public/user/project/org Skill，安装、启停、刷新、卸载、同步 Marketplace；
- Plugins：安装、启停、刷新、卸载、Marketplace 浏览；
- Plugin 可同时贡献 Skill、MCP、Hook 和 Agent Definition；
- Conversation 创建时传 `plugins`，或运行中调用 `load_plugin`；
- Agent Context 可配置 `load_user_skills`、`load_public_skills`、`load_project_skills`、`registered_marketplaces` 和 `disabled_skills`。

FlowWeave 不使用这套状态，而是把 Skill/MCP/Hook 作为平台 Node Capability，经 validate/commit、Artifact、Snapshot 和安全物化后显式注入。这是为了可审计与可重放，不是单纯漏接。

直接开启 OpenHands 自动发现会产生明显冲突：

- 相同 Node Snapshot 在不同时间可能从 public Marketplace 得到不同内容；
- 用户目录或项目目录中的 ambient Skill/Plugin 绕过平台绑定与审批；
- Plugin 能附带 MCP/Hook/Agent，权限面大于单一 Skill；
- 运行中 `load_plugin` 会改变 Tool 与上下文，但 FlowWeave Snapshot 不知情；
- 动态下载需要 egress 和供应链校验。

FlowWeave 现已按该边界接入 Marketplace：目录预览必须固定完整 commit，选择条目后分别冻结目录来源、实际 Plugin 来源 commit 与规范内容 digest，经显式发布生成 Capability Version。浏览不安装内容，生产 Conversation 不注册浮动 Marketplace，也不调用运行中 `load_plugin`。

### 11.13 Agent Profile、LLM Profile 与 Settings API 的治理边界

OpenHands 支持 Profile/Agent Profile 的 CRUD、激活、重命名、物化和运行时切换，也提供 Settings Schema、MCP Settings 和 Secret Store。FlowWeave 已有 Model Provider、Node executor、Environment Version、Capability 和加密凭据边界，所以没有调用这些 API。

FlowWeave 已将 Agent Profile 收敛为无 Secret 的不可变 Capability Version，并提供版本差异、Node 绑定、固定 digest 切换预览，以及显式创建新 Snapshot/Attempt 的激活链路。既有 Attempt 不热改，OpenHands Server 可变 Profile/LLM Store 与 Settings Secret Store 不成为生产真相。

### 11.14 ACP Agent 未接入

OpenHands 除原生 Agent 外还支持 ACP Agent，通过子进程运行支持 Agent Client Protocol 的 Codex、Claude、Gemini 等 Agent，支持 ACP Session 恢复、模型切换、文件凭据、MCP 转换和专属数据目录。FlowWeave 创建请求固定 `agent.kind=Agent`，CODEX_OAUTH 只是让原生 OpenHands Agent 使用 Codex Responses 模型，并不是 ACP。

接入 ACP 需要新增一类 Runtime Provider/Agent Kind，而不是复用现有 model 字段：

- 允许的 ACP command/args 必须来自发布环境白名单，不能由用户任意拼接；
- 认证文件如何从环境凭据卷安全映射；
- ACP session ID、工作目录与 FlowWeave Conversation 生命周期如何对应；
- ACP Tool/Event 如何归一化；
- Skill、MCP、Hook 中哪些字段兼容 ACP；
- 模型发现、推理强度和 live switch 如何映射；
- 进程退出、超时、费用和取消如何恢复。

它适合作为明确的第二种 Agent Runtime 产品能力，不宜隐藏在现有 OpenHands Provider 开关后。

### 11.15 Critic、Goal loop 与自动精炼的受治理接入

OpenHands 支持 Critic 对 Finish/Message 或每个 Action 评分，并在低于阈值时自动追加反馈、继续精炼；还提供 `/goal`、`/goal/stop`、`/goal/resume` 在同一 Conversation 中执行多轮目标审计。`ask_agent` 可在不修改 Conversation state 的情况下询问 Agent。

FlowWeave 已有 Prompt Gate、Script Gate、END Gate、Reject 新 Attempt 和人工验收。这与 Critic/Goal 有职责重叠：OpenHands 自评发生在 Runtime 内，FlowWeave Gate 是平台可审计的独立判断。

可能的合理组合是：

- Critic 只做低成本、有限次数的单轮自修复；
- END Gate 仍做独立质量判定；
- Critic 结果和精炼次数投影为 Run Event；
- 总迭代、Token 和金额同时受 FlowWeave 预算控制；
- 禁止无限 Goal loop 与后台任务重试相乘。

当前实现保持该职责分离：Critic 结果耐久投影，Goal 的 start/stop/resume 受迭代、Token 与金额预算约束，`ask_agent` 作为不写入消息树的只读诊断。END Gate 与人工验收仍独立；T9 已完成行为、恢复、预算与固定 Runtime 契约回归。

### 11.16 直接 File/Git/Workspace 与轨迹导出 API 未使用

Agent Server 还提供：

- File upload/download、目录搜索、Workspace archive；
- Conversation trajectory 下载，导出时对已知密钥字段做脱敏；
- Git changes、diff、commits、单 Commit changes；
- Conversation Workspace 静态文件服务；
- Workspace 收藏/父目录管理。

FlowWeave 当前通过宿主共享 Workspace、自己的附件接口和消息级图片代理完成部分需求，没有调用这些 API。潜在价值包括：代码 diff 审阅、Attempt 工作区归档、可复现实验轨迹和无需 Agent Tool 的只读 Git 面板。

但上游部分 File/Git API接受绝对路径，接入时必须由 FlowWeave 服务端从 Attempt 推导路径，绝不能把浏览器传入的任意路径透传；下载轨迹的脱敏规则也不能替代 Artifact 级数据分类与导出审批。Workspace 静态服务虽有目录逃逸检查，仍会暴露整个 Conversation 工作目录，当前消息级、文件类型受限的图片代理安全面更小。

### 11.17 其他未使用或不建议直接接入的接口

| 能力 | 当前不使用的原因或建议 |
|---|---|
| Conversation search/count/title/tags/autotitle | 平台已持久化自己的 Conversation 目录；可把 OpenHands title/tags 用作诊断元数据，但不应成为 UI 真相 |
| `pause` 与独立 `run` | 当前消息 `run=true` 和 `interrupt` 已覆盖主要路径；若增加“暂停后原地继续”，需与 steer/cancel 区分 |
| `agent_final_response` | 当前从事件与 inspect 解析终态；可作为诊断兜底，但不能绕过 cursor 去重和输出契约 |
| Event count/get/batch | 可用于审计修复和缺口诊断，正常投影暂无必要 |
| Worktree | FlowWeave 已规划节点 Workspace/仓库生命周期；上游 `/tmp` Worktree 不在平台持久化与清理台账中 |
| Client Tools | Tool 在客户端执行，需双向 WebSocket、Action 回执、权限和幂等协议；当前 RuntimePort 不支持 |
| `tool_module_qualnames` 动态导入 | 允许服务端导入调用方指定模块，生产安全面过大；应使用镜像内固定 Tool allowlist |
| Secrets API | 平台刻意不把通用业务凭据注入 Agent；若未来开放，应使用受控 Secret Reference，而非任意明文字典 |
| LLM provider/model discovery 与 OpenAI subscription | FlowWeave 自己管理 Model Provider 和 Codex OAuth，不应双写 OpenHands Settings |
| `/v1/chat/completions` | 是 OpenAI 兼容入口，不提供 FlowWeave 所需的完整 Conversation/Tool 生命周期 |
| Hooks API | FlowWeave 已在创建请求中注入冻结 Hook；运行时全局修改会破坏 Snapshot |
| Warm-pool `/api/init` | 适合大规模预热 Pod；当前 Docker Controller 每 Runtime 启动完整服务，待冷启动成为瓶颈后再设计 |
| Workspace session Cookie | 主要服务 OpenHands 自带浏览器 UI；FlowWeave Worker 使用 Header API Key |
| Server health/details | 当前只用 Compose healthcheck；可补版本与能力探测，启动时拒绝不兼容版本 |

### 11.18 建议的实施路线图

建议按“先补协议正确性，再补治理，最后扩能力面”推进：

1. **原生确认闭环**：Confirmation Event、批准/拒绝 API、审计和 UI；
2. **版本与能力协商**：启动时读取 Server version/OpenAPI capability，拒绝静默漂移；
3. **成本与 Trace**：Conversation stats、Run/Attempt metadata、预算和告警；
4. **长会话治理**：Condenser、摘要事件、上下文/费用可见性；
5. **MCP 上线验证**：在目标环境测试、列 Tool、只读试调用；OAuth 独立评审；
6. **实时事件**：WebSocket 唤醒 + REST cursor 补偿；
7. **节点 Tool Policy**：按节点选择 Browser/grep/glob 等工具，配套确认策略；
8. **高保真分叉**：同 Runtime 内原生 fork，跨 Runtime 保留当前语义分叉；
9. **Browser/IDE/ACP**：作为独立产品能力分别设计，不用单个布尔开关草率开放；
10. **Marketplace/Plugin**：只做固定版本导入源，继续服从 FlowWeave Capability Snapshot。

完成 1～6 后，FlowWeave 才算较完整地使用了 OpenHands 作为可治理的生产 Agent Runtime；后续项目更多是扩展 Agent 能力面，而不是修复基础协议。

## 12. 当前限制与注意事项

1. 能力集合在 Conversation 创建时冻结。Node Asset 后续编辑不会热更新已有 Conversation。
2. Skill 是提示与资源，不是强类型 Tool。未显式引用时是否使用由模型判断；Skill 输出不会自动满足 Artifact 契约。
3. MCP 工具发现和调用由 OpenHands 管理。FlowWeave 记录投影的工具事件，但不逐次代理 MCP 协议。
4. stdio MCP 命令必须真实存在。保存配置不等于安装 CLI，应在目标环境安装发布或提供经校验脚本。
5. Hook 脚本在 Runtime 内具有执行能力。即使目录只读、容器非 root，仍应视为受信任节点能力并治理导入与绑定权限。
6. switch_llm 不重建上下文，已有工具、Skill、MCP、Hook 和历史保持不变。
7. 协作消息与流程输出是两种契约。协作 Conversation 的 output contract 为空；自动执行才登记 URL Artifact。
8. OpenHands Finish 不是最终业务完成。之后还可能有输出校验、END Gate 和人工验收；Flow Run 也不会自动完成。
9. Compose 默认会话密钥只适合本地开发。生产必须替换 OPENHANDS_SESSION_API_KEY 和 Controller 密钥，并隔离 Controller 故障域。

## 13. 关键代码索引

| 主题 | 文件 |
|---|---|
| Runtime 抽象和请求、事件、结果模型 | services/platform/src/flowweave/runtime/base.py |
| OpenHands HTTP、上下文、事件、输出和取消 | services/platform/src/flowweave/runtime/openhands.py |
| 模型解析与 Runtime 请求组装 | services/platform/src/flowweave/runtime/request.py |
| Skill/MCP/Hook 物化与路径校验 | services/platform/src/flowweave/runtime/workspace.py |
| 自动 Attempt 启动、轮询、恢复和取消 | services/platform/src/flowweave/modules/orchestration/application/service.py |
| Conversation、能力引用、附件和事件投影 | services/platform/src/flowweave/modules/conversations/application/service.py |
| Sandbox 台账与分配 | services/platform/src/flowweave/modules/sandboxes/application/service.py |
| Docker Runtime 隔离、挂载和密钥 | services/platform/src/flowweave/modules/sandboxes/infrastructure/docker.py |
| OpenHands 镜像与固定依赖 | infra/openhands/Dockerfile、infra/openhands/pyproject.toml |
| Compose 服务和网络 | infra/compose.yaml |
| 行为回归测试 | services/platform/tests/test_openhands.py、services/platform/tests/test_hook_scripts.py |
| OpenHands 参考源码 | /Users/zhengmengen/WorkSpace/openhands/software-agent-sdk |

## 14. 整条链路的一句话总结

FlowWeave 在用户真正启动 Attempt 或创建协作 Conversation 时，从冻结快照取出模型、输入和能力，把 Skill 解压到节点 Workspace，把 MCP/Hook 脚本物化到只读目录，必要时创建隔离 Runtime，再通过 POST /api/conversations 一次性把 LLM、工具、Skill、MCP、Hook、Workspace 和系统上下文注册给 OpenHands；之后用持久化消息触发 run: true，用事件游标持续读取过程，并把 Finish 转换成 FlowWeave 消息、Artifact 和人工状态，而不是让 OpenHands 直接控制流程状态。
