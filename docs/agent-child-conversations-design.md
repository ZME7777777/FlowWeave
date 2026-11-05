# 原生子会话接入设计

> 状态：`FR-702 FROZEN`，仅冻结接入契约，不表示子会话已经实现或可用。
> 日期：2026-10-26。
> 授权范围：完整合入已评估的 OpenHands upstream main；FlowWeave 只增加子会话产品能力。
> 不实施 Git Worktree 产品功能，不初始化仓库，不迁移会话工作目录，不部署或替换现有 Runtime。
> 后续任务及唯一执行状态见 [Runtime 任务进度](flowrun-runtime-task-progress.md)。

## 1. 产品边界

子会话是 OpenHands 正式 Conversation，具有独立 ID、事件历史和执行状态。FlowWeave 仅管理
其用户授权、冻结能力、父子 locator、资源归属、审计和产品投影；不得用临时侧边聊天或平台
执行器模拟子会话，不复制正式聊天事件到数据库。

- Agent Workspace 和 FlowRun node-session 两种宿主使用相同子会话语义。
- 子会话继承父会话所属用户、宿主、Runtime Session、冻结协议和允许能力边界。
- 本轮一律显式使用 `isolation="shared"`，与父会话共享其已验证的工作目录；不使用上游默认的
  `worktree` 隔离，不创建新 allocation、容器、NodeRun 或 Attempt。
- 子会话不继承父聊天历史。父 Agent 必须提供完整任务说明；历史读取仍走既有引用授权机制。
- 每个子会话允许用户继续发消息、上传附件、选择模型／推理程度、暂停／继续及原生压缩上下文。
- 子会话的模型在创建时继承父会话当前正式模型，后续独立切换，不反向修改父会话。
- 原生 Tool、Skill、Plugin、MCP、Hook 和 Task 仍由 OpenHands 加载执行；用户续聊不能扩大冻结能力。
- 首版只允许一层子会话。子会话可使用已授权 Task，但不具备创建孙会话的权限。
- 关闭任何侧边视图只关闭视图；不删除、不暂停，不存在临时聊天的一小时 TTL。

“只接入子会话”不表示只 cherry-pick 子会话提交。上游修复和其他源码改动完整进入 baseline，
但不因源码升级而增加其他 FlowWeave 产品入口或改变既有 Run 的冻结镜像／协议。

## 2. 固定源码事实与缺口

### 2.1 比较身份

| 身份 | 固定值 |
|---|---|
| FR-702 冻结时 FlowWeave source lock | `73e3cd8e4d74ff30c9ed794baf72d977800509d7`，四包 1.54.0 |
| FR-702 冻结时上游锚点 | `f0206428306db687ac9c8d833ece4fb82b015eba` |
| 本轮已拉取、评估的 upstream main | `ce5716a3c0a955a85bb0cc4dcfaa94d5fefcda50`，四包 1.58.2 |
| 两上游锚点之间 | 132 个提交；目标 HEAD 在 v1.58.2 发布提交之后还有五个提交 |
| FR-702 冻结时 SDK baseline HEAD | `2c8025ff11b62cbce8880d08f8b7e9c69e844ecd`，与 FR-702 时 source lock 文件树相同 |

FR-703 只合并上述固定目标，不以构建时浮动 main 替代。若执行前有新上游增量，另行取证和切片。
本地 baseline 比锁定源码多的浏览器修改／撤回提交应保留历史，不重新引入已撤回功能。

FR-703 已将上述固定目标完整合入 SDK baseline，merge commit 为
`ecf0aec091463fb242b0c5ab59d155c65d96ded8`；两个 parent 分别是原 baseline 和固定 upstream。
本轮定向源码／真实 TCP 合并验证已完成，详见进度记录。FR-704 将新来源归档锁定为该 merge
commit，归档 SHA-256 为 `5e4a53a2fab54bd3435b7ea29d6c6ea98fc181c3d0f2299cf85a7414a658d927`，
并同步四包 1.58.2、固定依赖和镜像构建输入。此身份属于本分支后续构建；既有 Environment、
FlowRun Snapshot、Runtime generation 和 V1／V2 协议不会被追溯修改。最终验收以 FR-704
进度记录为准。原生子会话工具尚未开放，后续扩展将再次生成不可变身份。

### 2.2 原生已具备

固定目标中的 `StartChildConversationRequest` 提供 `task`、`title`、`isolation`；
`POST /api/conversations/{conversation_id}/children` 使用正式父子身份创建独立 Conversation，
继承父配置和 resolved Agent，并立即投递首条任务、启动执行。工具 `start_child_conversation`
接受部署方指定的 `launch_url`，不属于默认工具预设。

正式响应包含 `conversation_id`、`parent_conversation_id`、status、workspace 和实际 isolation。
创建后的子 Conversation 可以继续使用正式事件、消息、run、interrupt、模型切换和 condense 接口。
Task 的执行器等待子任务完成后返回 `TaskObservation`；子会话启动工具只返回启动结果。

证据：固定目标的 `openhands-sdk/openhands/sdk/conversation/request.py`、
`openhands-agent-server/openhands/agent_server/conversation_service.py`、
`openhands-tools/openhands/tools/child_conversation/` 和 `openhands-tools/openhands/tools/task/`。

### 2.3 必须作为正式 baseline 扩展补齐

以下是待实现契约，不能标为上游现有能力：

1. 稳定创建身份、幂等查回，以及子 Conversation 创建后、首条任务运行前的治理接入点。
2. 可信的父 Tool invocation 上下文：原生 action/tool-call ID、父 Conversation 和调用权限。
3. 父 Agent 的直属子会话 list／read／send／wait／interrupt 工具及有界结果读取。
4. 首条交办和后续父 Agent 指令的正式来源关联、重复投递保护和忙碌竞争语义。
5. 原生工具权限／深度限制：只访问直属子会话、固定 shared 隔离、禁止子会话再创建子会话。

扩展进入 SDK baseline 的正式类型、Tool、事件和服务生命周期，经测试和不可变归档锁定；
FlowWeave 不自建 Agent loop、轮询执行器、私有 MCP 或让模型用 Shell 调用平台控制 API。
不通过提示词规避深度／权限／隔离限制，也不通过修改持久文件偷偷补全原生关系。

### 2.4 创建身份基础契约（FR-705）

SDK 的 `ChildConversationCreationIdentity.for_request(parent_id, creation_id)` 按固定 v1
命名规则生成 child UUID，供控制面在物化子 Conversation 前预留 binding。creation_id 是
受信调用方稳定分配的创建请求 UUID，不是模型提供的身份或权限凭证；与正式 invocation
的可信关联继续由 FR-706 补齐。不同父会话的创建 ID 命名空间互相独立。

`ChildConversationCreationStore.reserve` 在原生持久目录内按 child 身份加跨进程文件锁，
原子提交 schema 1 回执；相同身份且 task／title／isolation 摘要一致时返回原回执和原
时间，内容改变则拒绝。`get` 用于响应丢失后的只读查回，不创建目录；损坏、未知 schema
或身份错配不能作为未创建处理。回执只包含三项 UUID、请求摘要与创建时间，不保存任务
正文、Agent 配置、Secret 或执行状态；随 Runtime 持久数据保留，不能因关闭子标签清理。

FR-705 仅提供此基础原语，未接入任何创建路由，不创建 Conversation、不投递 task、
不证明配额或运行许可。FR-718 必须在 native 生命周期中串起准入 → 原子预留 → 准备
→ 治理 → 首轮投递／运行，使用生命周期 executor 执行同步存储操作；不能将拿到回执
等同于创建或授权成功，不能在响应未知时改用另一个 creation_id。

### 2.5 两阶段原生生命周期（FR-718）

baseline 追加三个正式路由，均使用 Agent Server 现有会话认证：

- `POST /api/conversations/{parent}/children/preparations/{creation_id}`：typed request
  为 `ChildConversationPreparationRequest`，只允许 shared；相同意图重试返回原身份。
- `GET` 同一路径：返回 receipt、reserved／prepared／dispatching／started 和实际工作目录。
  phase 是创建步骤，不是 Conversation execution_status；reserved 不能当作已创建。
- `POST` 同一路径的 `/release`：受信部署方完成绑定及凭据治理后，以原 task／title／isolation
  放行。创建 UUID 只负责幂等，不能替代 FR-706 的可信 invocation 和用户权限。

准备前先通过原生 RunSlot 准入，继而持久预留身份；原生不可变启动计划使用子 Conversation
对应 Cipher 整体加密，只在 Runtime 持久状态目录保存。计划在初始化前提交，恢复时保持原
配置和 child ID，即便 base_state 已写而 meta 尚未写。该原生路径不创建 Worktree，也不初始化
Git 仓库；父配置中的平台绑定级授权须由 FR-723 使用 FR-720／721 的固定原生契约，
在首轮前替换／重新激活。

prepared／dispatching 门禁阻止普通 send、run、goal、ask、condense 和 live Conversation
访问；只读事件／配置及既有凭据激活仍可用。release 在取得 RunSlot 后才写 dispatching，
通过 LocalConversation 的正式稳定 event_id 受理首条任务；恢复只按该 ID 查回，不比较文本
相似度。相同 ID 绑定不同消息或 sender 会拒绝；已运行／终止的首轮不会因确认丢失而重跑。
started 仅表示调度已受理；进程在后台任务真正启动前退出时，重试会检查原生活动任务、
execution_status 与 leaf_event_id。只有没有活动任务、仍为 IDLE 且 HEAD 恰为首条任务
event ID 时才补启动；已完成、暂停、异常或已推进到其他事件的会话不因重试再次运行。
请求取消必须等原生离线写入收口后再释放生命周期锁，避免新重试与迟到写入交叉。

删除子会话先持久标记删除身份，并清除其加密启动计划；旧 creation_id 后续不能重新创建。
新 fork／旧式 immediate child 创建不继承原准备门禁。旧 children 请求形状、默认 immediate
语义和默认 worktree 选项保持上游兼容，但 FlowWeave 产品不接入该入口或 Worktree。
未治理的旧会话元数据不新增空 gate 字段。TypeScript 发布客户端仍遵循它的已发布镜像 schema
锁；本轮新 native 路由由通用 HTTP 客户端调用，FlowWeave 的静态 V3 消费与契约门禁留在 FR-709。

### 2.6 受治理 launcher 身份与门禁（FR-706）

新增正式 `start_governed_child_conversation` Tool，与旧 `start_child_conversation` 并存。
旧工具的参数、默认 Worktree 和 immediate 语义继续兼容；FlowWeave 后续只物化受治理工具。
新工具的模型输入只有 task／title，不接受 URL、父子 ID、scope、generation 或 isolation。
shared 是类型和执行约束，不依赖提示词纠正。

Tool 配置冻结 `ChildLauncherPolicy`：父 Conversation、scope UUID、Runtime Session、generation、
Manifest digest 及最大活动子数（不超过三）。scope UUID 后续由 FR-719 固定为父 binding；
不能从模型输入或调用方任意 HTTP 参数推导用户和宿主。创建工具只能由有相同父 Conversation
的配置实例加载；子 Conversation 不继承这两个创建工具，也不继承父的 live tool executor。

原生 Agent 的单次执行边界通过可复原的 ContextVar 绑定当前 ActionEvent 和 Conversation，
覆盖同步／异步 Agent 共用的实际工具分发及并行线程。受治理 executor 只在该边界内签发
至多十分钟的 Cipher token，绑定正式 action ID、tool-call ID、冻结策略、完整目标 URL 和
任务摘要；creation UUID 由正式 invocation 确定，未知响应不能改用新 ID。签发／验证都拒绝
Cipher 使用任一 Runtime session key 的配置，避免仅持有连接 key 就能生成调用证明。
token 按 SecretStr 及既有 Secret serializer／脱敏规则处理，不进入工具 observation 或错误文本。

正式 `POST /api/conversations/{parent}/children/launcher/verify` 接受 typed task／title／shared／token，
验证返回 `ChildLauncherInvocation`，不创建会话或占用创建配额。服务端按原生 event ID 读取
ActionEvent，核对工具、tool-call、任务及当前分支祖先关系，再比对父正式配置中的全部策略
和 audience；伪造 token、跨父、旧 generation／Manifest、旧分支或子会话发起均拒绝。
过期证明只允许对已有**原生准备回执**的原 creation 做恢复；仅有平台预留不满足此条件，不允许首次准入。

prepare／release 增加可选 `launcher_token`；对配置受治理工具的父会话必须提供并复核证明，
旧无此配置的准备请求形状保持兼容。受治理父／子不得通过旧 immediate children 或通用
parent_conversation_id 创建入口绕过门禁。准备操作在父生命周期锁及跨进程非等待锁内，
按原生持久回执和正式执行状态检查最多三个创建中／活动子会话；并发超限返回 429，
不预留新身份，同一已受理 creation 的重试不重复占用。删除尚未完成时不提前释放配额。
release 复核原准备保存的策略、creation 和任务摘要，不能借另一次调用放行子会话。

该验证接口只证明原生调用来源。控制面必须另行解析 scope 对应的用户／宿主／父 binding，
核对 active generation、冻结 Manifest 和权限，再执行 prepare → 凭据治理 → release。
这部分依赖 FR-710 的关系模型，分别由 FR-719 接入调用授权，FR-720／721 补齐和冻结原生
前置契约，FR-722 接入 V4 父能力／首轮授权，FR-723 接入创建／凭据恢复；
Runtime key 或 verifier 的成功响应都不能单独授权平台操作。FR-706 不开放任何平台或浏览器入口，也不更新既有冻结 Runtime。

### 2.7 原生直属子查询工具（FR-707）

baseline 新增 `list_child_conversations`、`read_child_conversation` 和
`wait_for_child_conversation` 三个正式 Tool。每个工具冻结父 `ChildLauncherPolicy` 和可信
`access_url`，模型只能传目标子 ID、分页参数／读取预算或等待 checkpoint。工具只在原生
invocation 边界内签发独立用途 token，绑定 operation、全部参数、action／tool-call、冻结
scope／generation／Manifest 和 audience；不能用 launcher token 替代查询 token。
子会话移除这三个父工具，服务端同时要求调用者为根父会话。

正式 `POST /api/conversations/{parent}/children/queries/verify` 只校验原生查询来源，
`POST /api/conversations/{parent}/children/queries` 执行 typed list／read／wait。两者均沿用
Runtime 认证并要求原生证明；平台用户与 binding 权限仍由 FR-719 的受管入口复核。
目标必须满足正式 parent ID、确定性 child ID、创建回执、已受理策略 scope／Runtime／
Manifest 和 shared 工作目录一致；不是本父受治理直属子、跨父、删除中或证明参数变化均拒绝。
异步投影完成后再次校验调用 scope 和目标关系，不能把等待期间已撤销的权限作为有效结果返回。

list 按原生 child UUID 排序，以加密 keyset cursor 翻页，每页最多 50 条；状态和 HEAD 来自
原生持久快照，标题最多 256 字符并显式标记 `title_truncated`。read 按当前正式事件分支从新
到旧读取：events 模式返回 Action／Observation 的正式身份和工具进度元数据，以及正式
assistant MESSAGE 的文本；results 模式只读这些 assistant 文本。均不返回系统提示词、
用户消息、工具参数／正文、隐藏思考或流式 delta；这不是完整原始事件导出接口。
每页最多扫描 100 个事件、返回 50 条摘录及 12000 个 Unicode 文本字符。大正文通过同一
event ID 的 text_offset／next_text_offset 继续；页及摘录均有 complete 标记，不能将截断
结果声称为完整读取。游标绑定父策略、子 ID、mode 和读取 HEAD；HEAD 改变返回 409，
要求不带游标重读，避免跨分支混合。加密 cursor 和 checkpoint 都不能换父／换子复用。

wait 比较正式 HEAD／execution_status／准备阶段／显示标题，单次最多 30 秒；无 checkpoint
时从本次读取的快照开始等待。超时返回 timed_out，状态有变化返回 changed，不发送消息、
运行会话或中断子会话。服务端使用 asyncio 有界等待；浏览器／工具连接断开取消服务端任务，
父原生 cancellation token 触发时工具关闭 HTTP 请求并回收异步任务。无需启动已关闭子
EventService、获取 child run-slot 或建立平台轮询执行器。

原生工具执行可能持有父 run lock，因此查询及 launcher verifier 读取不可变正式事件和冻结
配置时不得再次等待该锁；子状态读取原子持久快照，不等待正在执行的子工具释放 live state。
此修复同时覆盖 FR-706 verifier／准备前的配置检查。真实 HTTP 回归验证父 Agent 可发起
原生 list 工具、完成身份验证并收到 observation 后正常结束；不以受控 LLM 代替真实模型验收。
本片仍只修改 baseline 和接入设计，不开放产品入口，版本冻结留在 FR-709。

### 2.8 原生父指令、中断与输入准入（FR-708）

新增正式 `send_child_conversation_message` 与 `interrupt_child_conversation` Tool；模型只能
指定直属 child ID 及发送正文。配置冻结父策略和可信 access_url，原生 invocation 签发独立
`child-command` 用途 token，绑定 operation、所有命令参数、action／tool-call、scope、Runtime
Session、generation、Manifest 与 audience。command UUID 由正式父调用确定，不取自模型。
正式 `/api/conversations/{parent}/children/commands/verify` 校验来源，`.../commands` 执行
typed send／interrupt；沿用 FR-707 的配置／分支和直属关系验证，不凭 Runtime key 授权。
平台用户与 active generation 的复核由 FR-719／FR-712 的受管入口接入，两工具不继承给子会话。

受治理 child 的用户续聊走既有 `POST .../events`，追加稳定 submission_id 并使用 run=true；
不带稳定身份、run=false、目标 busy 或正在等待确认时均拒绝。普通旧会话的消息形状和
受理行为保持兼容。用户与父 send 共用 EventService 输入锁，和手工 run、pause、interrupt、
condense 串行；受治理 child 不开放额外生成输入的 goal／resume-goal 路径。只有原生可写态
才准入新输入，运行中不会排入服务端队列、自动 interrupt 或走 ACP supersede。
先取得 RunSlot，再调用原生 LocalConversation.send_message 和 run：busy 返回 409，容量
不足返回 429，均不追加新消息。新输入只允许 idle、paused 或 finished；error／stuck 不隐式恢复。

用户 submission UUID 和父 command UUID 在 child 命名空间内确定正式 event ID；正文、sender
或来源改变时拒绝复用。已受理重试先读取该正式事件，返回同 event_id，不新增气泡／输入，
不重新执行已完成轮次。只有事件仍为当前 HEAD、无活动 run 且原生 idle／paused 的未启动
窗口可以补启动；不会依据正文、相似时间或完整历史猜测。取消请求仍须等待原生持久写入与
调度收口后释放输入锁；外部结果未知时只能用同一身份对账。

父首条任务及后续 send 继续使用正式 `user` role，在 MessageEvent 增加可选
`parent_instruction: ChildInstructionSource`，记录 start／send、父 Conversation、action ID、
tool-call ID 和冻结策略定位；sender 同时指向父 Conversation。旧事件及普通用户输入不新增
空来源字段，不从消息正文识别父指令。该字段是后续 UI 显示“来自父会话的指令”的依据。

interrupt 只对目标 child 调用原生中断。先在 Runtime 子会话私有目录持久写入命令回执，
仅含来源、目标 input／HEAD、步骤和正式中断事件 ID，不保存任务正文或维护执行队列。
实际 native InterruptEvent（同步 fallback 为 PauseEvent）取得稳定 event ID 和同一来源；
LocalConversation 以独立线程锁保护待写来源，不从 async run lock 中取证明。原生中断调用
返回后，等待 run 任务收口至多五秒，未完成则返回 pending=true，必须以同一 command
继续对账；同步 fallback 沿用原生 cooperative pause，可能先等待当前工具退出，期间保持
输入围栏，不能用迟到 pause 影响新轮次。回执已完成或正式
中断事件已存在时仅查回；回执待完成但目标已进入更新用户输入时不再中断，避免迟到重试
停止新的轮次。回执写入遵守原生 lease write guard，随 child 持久数据保留及删除。

本片的真实 HTTP 回归使用受控 LLM 驱动原生工具和正式 prepare／release：验证父 send、
用户重复提交及异步 InterruptEvent 来源，另覆盖重载、写后失败、确认丢失和取消围栏。
这不替代 FR-717 的真实模型与平台双宿主验收；FR-708 不开放 Web／平台入口或升级旧 Runtime。

### 2.9 最终扩展来源与 V3 静态冻结（FR-709）

最终受审计 SDK 来源固定为 `3a4f5f1f61082430e8a2ccf8d59e52c9f4eab5dc`，四包仍为 1.58.2，
upstream anchor 不变。完整归档进入 `infra/openhands/vendor/`，SHA-256 为
`f63ecb0d046c1a6a20e39540c1cb6b37570c105c788916b782bc76c5881b3846`；source lock、
构建输入、provenance、平台身份和可执行契约同步。旧归档保留，不修改已发布 Environment、
Snapshot、Runtime Session／generation 或 SDK 发布客户端 schema。

`runtime/child_contract.py` 定义 `oh-fw-child-v1` 能力 Manifest，摘要为
`c50549595d98880f44c2f97edd7fed834c8ebef83ff67aa6f7e4fb9eaf3320a1`。内容包含六个正式父工具、
固定来源、shared、一层深度、三个活跃子上限、分页／扫描／文本预算、30 秒等待、稳定用户
submission ID、prepare-before-release，以及 Task／子会话选择和结果完整性的父使用说明。
Manifest 不含 URL、用户、binding、generation、token 或 Secret；绑定级原生策略由受信宿主在
授权后，使用该版本编译为正式 `Tool.params.policy`、`launch_url`／`access_url`。本片验证了
实际编译结果可由归档中的六个原生工厂解析，尚不把工具安装到产品会话；FR-719 接入授权和加载。

静态 `OH_FW_V3` adapter 注册为独立契约，沿用 V2 的 timeout／Task 边界，额外要求八个
children HTTP method/path。V3 只接受上述受审计来源及版本；Environment 发布门禁还核对
归档摘要、四包身份和完整能力 Manifest。新构建／恢复发布的镜像只有实证来源吻合时才写入
该能力声明。选择 V3 的新 Snapshot 同时冻结能力 Manifest；V1／V2 的 Snapshot 不增加该字段。
绑定级工具与凭据继续在会话准备阶段冻结，不能仅凭 Snapshot 声明取得父工具权限。

默认 profile 仍为 V1，不自动选择“最高版本”。新增 `0175_add_oh_fw_v3_protocol_profile`
只扩充 Environment Version／Runtime Session／generation 三张表的允许值，不更新现有行。
回退持有三表写入锁，关闭 RLS 隐藏后逐表拒绝仍存在的 V3 记录；不把 V3 改写成 V2。
旧 V1／V2 的请求和冻结契约继续使用其原镜像身份，旧 Runtime 不探测／模拟子功能。

独立 frozen 环境通过归档安装与完整可执行契约，536 个 Python 文件与固定 SDK commit
逐字节一致；这不是新 Runtime 镜像或真实模型的业务验收。完整镜像／两宿主／真实模型门禁
仍在 FR-717，FR-709 不开放浏览器入口、不部署或替换 Runtime。

### 2.10 父子关系与普通入口隔离（FR-710）

新增 `agent_child_conversations`，由 0176 迁移建立并登记到用户隔离表清单及公共 ORM 导出。
记录 owner、host／scope、Runtime Session、父子 binding／原生 Conversation、creation／action／
tool-call、受理时 generation、Manifest digest 和创建审计时间；不保存 task 正文、事件、HEAD、
cursor、token 或执行状态。`accepted_generation` 是不可变受理审计，不是当前 generation 的权限证明。

唯一约束覆盖 child binding、Runtime 中的 child Conversation、父下 creation 与正式 invocation。
PostgreSQL 强制 RLS 的 USING／WITH CHECK 沿用平台用户策略；数据库触发器按稳定顺序锁住两端
binding，检查同用户／宿主／scope／Runtime／shared 目录、原生身份和目录版本一致，拒绝自关联、
多父和一层以上关系，以及临时侧边聊天／门禁 sidecar 混入。子 binding 不持有 node_run_id 或
node_attempt_id，FlowRun 定位使用父已授权的 conversation_scope_id；不取得节点执行身份。
关系不可更新，关联中的 binding 身份字段不可漂移或直接删除，标题和未读仍独立可更新。
迁移回退在表锁及禁止 RLS 隐藏下拒绝非空关系，不把仍存在的子会话降为普通会话。

共享 `child_relations` 服务按当前用户和已解析宿主验证直属 locator，两宿主使用同一规则；
正式 native parent／child ID、workspace 和 isolation 另与冻结关系比对；FR-711 提供正式
原生适配，FR-723 再接入两宿主创建。此关系或原生身份校验不单独授予创建／发送权限，
仍需宿主授权及 active generation 门禁。

普通列表、分页、置顶、排序、管理列表、活动与搜索扫描／历史命中投影均在 SQL 中排除 child，
保存的搜索命中在分页前排除，避免只含隐藏 child 的空分页链。
普通 binding 读取、消息／控制、fork、历史引用、文件／终端及创建幂等重放也不能复用 child。
独立打开主路由只取得 `AGENT_CHILD_REQUIRES_PARENT` 与已授权父 binding，浏览器按当前宿主
构造父路由并替换导航；不接收服务器任意 URL，不打开子标签，迟到响应不能改变新选择的路由。
跨用户／错误宿主／scope 返回 404，不能从跳转响应取得另一个宿主的父定位。

FR-713 尚未实现可恢复逐子清理前，关联中的父删除在任何原生删除调用前明确拒绝，防止孤立 child。
本片不增加子创建或续聊 API、不安装父工具、不改临时聊天 TTL；后续专用 child 入口必须使用直属
关系服务，不能绕过普通入口隔离。真实 PostgreSQL、ORM、并发、迁移和受控 Chromium 已分别验证，
实际原生两宿主／模型／Runtime 验收继续留在 FR-717。

### 2.11 原生子创建 HTTP 边界（FR-711）

原 FR-711 同时覆盖 Runtime transport、父能力装配／授权及准备／凭据／释放恢复，
按独立验收边界拆为 FR-711、FR-719、FR-720／721／722／723；FR-712 依赖 FR-723。
本片只增加 `RuntimePort` 与 OpenHands adapter 的类型化边界，不安装工具或开放产品入口。

| Runtime 操作 | 正式原生接口／返回 |
|---|---|
| `verify_child_launcher` | POST `children/launcher/verify`，返回 native invocation 证据 |
| `prepare_child_conversation` | POST `children/preparations/{creation_id}`，返回 preparation |
| `read_child_preparation` | GET 同一路径，只有真实 HTTP 404 返回不存在 |
| `release_child_conversation` | POST 同一路径 `/release`，显式放行原创建意图 |
| `child_conversation_configuration` | GET 原生 Conversation，显式 `include_skills=true` |

路径均从已定位的父／子 Conversation 构造并沿用 Runtime session key；操作要求 handle
显式为 `OH_FW_V3`，未声明 profile、V1／V2 和 Mock Runtime 在外部调用前拒绝。
写操作走既有 control 连接池，单次 HTTP 使用 30 秒阶段 timeout，并服从外层剩余读取预算；
不提供无限等待、自动重试、改用新 UUID、自动 release、平台排队或私有执行器。

launcher verifier 的 HTTP JSON **没有** SDK Python `creation_id` property；typed DTO 不凭
缺失字段或模型文本构造它。prepare／read／release 均由受信调用方显式传入 UUID，
核对返回的 parent／creation 以及正式 native UUID5 child 不变量；写响应还校验原始 task／
title／shared 的正式摘要。请求只允许 shared 与 opaque `SecretStr`，verifier 的 `token`
在 prepare／release 线路上转为正式 `launcher_token`，不持久化或写入 repr。

原生 401／403／404／409／422／429 保留状态，不被旧的 missing-conversation 转换吞掉；
仅 reconcile 的真实 404 可投影为不存在，成功 JSON 中的伪 missing marker 明确报错。
连接中断、超时、5xx 或无法校验的写响应保持结果未知，后续必须按原 UUID 对账。
错误不回显 response body、任务或 token；原有非 child HTTP 的处理保持不变。

配置核对比较正式父子身份、shared 工作目录和同一原生公开序列化边界的 Agent 文档，
全量 skills 请求避免默认裁剪导致误判。公开配置中的 Secret 已被 native 脱敏，因此相等
不证明凭据授权相等，不能替代 FR-723 的绑定级凭据重签、失效／恢复实证。
既有 additive `/secrets` 适配继续可用，但本片不自动更新子会话或宣称已解决 reload 回灌。

可选 TCP 回归通过 `FLOWWEAVE_NATIVE_CHILD_PYTHON` 指定独立安装固定四包的 Python 环境；
夹具先检查 source-lock 归档 SHA-256、四包 1.58.2 及全部 536 个安装 Python 源码逐字节一致，
再启动真实 Uvicorn。只有 LLM completion 为受控响应；实际原生 Tool／ActionEvent 签发证明、
HTTP verifier、prepare／重复查回、配置读取、未释放拒绝、release／重复放行和正式事件均为真实实现。
证明由 native fixture 提供原创建 UUID，不把夹具内的 Python property 冒充 HTTP 字段；
测试 launcher URL 指向原生 verifier，仅为取得调用证据，故工具 observation 的结果未知不等于
平台创建成功。此探针没有平台用户授权或产品子 binding，也不替代真实模型／镜像／两宿主验收。

### 2.12 父能力冻结与 launcher 调用授权（FR-719）

`AgentConversationBinding.child_parent_manifest_json` 由 0177 迁移追加为可空字段，复用 binding
既有用户隔离／RLS。历史记录保持 NULL，不能由当前部署设置推断成已启用。新父 binding
在 PROVISIONING 阶段冻结：V3 source commit／归档摘要、能力版本／digest、Runtime image digest、
用户／宿主／scope／父 binding 与 native Conversation、Runtime Session、受理 generation、
Runtime Manifest 与摘要、三个可信目标 URL。只保存治理配置，不保存 task、token 或事件。
数据库 trigger 禁止既有非 PROVISIONING 记录启用、冻结值或其绑定身份漂移；标题、未读和正常
lifecycle 更新保留。非空冻结记录存在时 downgrade 拒绝，不能丢弃权限边界。

Agent Workspace 用 `AGENT_WORKSPACE_RUNTIME_PROTOCOL_PROFILE=OH_FW_V3` 显式选择新父会话；
默认 OH_FW_V1，已有会话不自动升级。实际创建仍须通过完整原生来源／版本／接口契约握手，
不会仅凭配置声明就接受旧镜像。FlowRun 只使用其冻结 Environment Version 的 V3 profile，
并核对 Snapshot Manifest 原摘要。手动新建、首轮 bootstrap 和自动执行 Worker 使用同一装配器；
Worker 在释放只读事务之前验证任务 lease 并提交不可变父配置，避免 native 首轮调用时配置丢失。
临时 sidebar 显式不启用父能力，仍沿用原 TTL；gate sidecar／child 都不能装配父能力。

`AGENT_CHILD_GATEWAY_BASE_URL` 必须显式配置为 Runtime 可达的可信网关基址，可含反向代理
路径前缀；不能含凭据、query 或 fragment，不能从请求 Host、模型参数或默认 public URL 猜测。
装配器生成 `.../api/v1/agent-child-launchers/{owner}/{parent_binding}/start` 以及对应 queries／commands
目标，将六个固定工具及 Task／子会话选择说明加入创建 spec；正式 `Tool.params.policy` 的 scope
固定为父 binding，Manifest 摘要来自该父冻结的 Runtime Manifest。OpenHands 正式
`tool_module_qualnames` 负责加载三个原生工厂模块；基础 preset 的 usable_tools 不需要提前注册
这些带父策略的工厂，V3 来源与八个正式路由的完整握手仍须通过。

只为精确 UUID 形式的 `/start` 路径豁免浏览器 cookie 登录；入口自身要求当前 generation 的
`X-Session-API-Key`，使用服务器 locator 推导并常量时间核对，root key 和其他 Runtime key 无效。
owner／binding 是显式定位符，不是权限证明。平台重新取得仍启用且有 Agent 权限的用户，
以其租户范围读取 ACTIVE 普通父 binding；Workspace 核对当前 ACTIVE generation、RUNNING
Workspace／资源及镜像，FlowRun 另复核 Attempt 未取消、Run 未终结、原 Snapshot 与 V3 connection。

入口执行三个阶段：短数据库授权 → 无 DB Session 的正式 native verifier → 新数据库授权复核。
最后比对全部 native policy 字段和精确 audience，拒绝期间发生的用户停用、权限撤销、binding
删除、generation／镜像／Snapshot 漂移。只有 native verifier 读取正式 ActionEvent、当前分支与
Cipher proof 后返回的 invocation 才能进入此边界；Runtime key 本身、cookie 或调用方自填 claim
均不能替代证明。请求体上限 256 KiB，格式错误不使用会回显 token／task 的通用 validation body。

FR-719 的授权成功**仍返回** `AGENT_CHILD_CREATION_NOT_ENABLED`，不返回伪造 child receipt，
也不调用 prepare／release。实际子预留、凭据和首轮恢复留在 FR-723；queries／commands 平台路由
尚未开放，等待 FR-712，不把冻结工具描述等同于产品已可用。原生工具将本阶段创建门禁视为结果
未知，不能新起另一份任务。用户侧浮卡／子标签也尚未开放。

原生 fork 会复制 resolved Agent 中绑定原父 UUID 的 policy，当前正式 fork 请求不提供替换
Agent policy 的字段。因此仅对本片新启用的 V3 父能力会话拒绝 fork 及需要换 native ID 的旧恢复
旁路，避免返回携带旧父授权的伪新父；普通旧会话行为不变。以后开放必须另行建立正式重绑定／
继承契约。replacement 后旧 generation 的父调用同样拒绝，不通过改写冻结信息自动恢复授权。

实际验收使用固定四包独立原生进程、真实 Uvicorn／TCP 与隔离 PostgreSQL，验证六个工厂从
平台创建 spec 加载，原生父 ActionEvent → 平台 `/start` → native verifier → 平台复核与明确门禁。
只有 completion 为受控 LLM；容器挂载与 FlowRun 物理连接由夹具提供，不声称真实镜像或真实模型
全链路成功。Worker 定向回归另核对冻结值已持久化且 start 前无持有事务。最终产品／实际镜像／
真实模型与完整两宿主故障验收仍由 FR-717 承担，本片不部署或替换 Runtime。

### 2.13 原生身份投影与准备时凭据冻结（FR-720）

原先两宿主创建方案需要先解决两个原生缺口：旧 verifier 的 JSON 不包含 Python
`creation_id` property；既有 `/secrets` 是 additive 更新，不能清除恢复时 StoredConversation／
AgentContext 的父凭据种子。因此拆为原生契约 FR-720、不可变归档／新静态能力契约 FR-721、
V4 父能力／首轮授权 FR-722，两宿主预留／重签／释放恢复 FR-723，FR-712 依赖 FR-723。本片尚不开放产品创建。

SDK `baseline` 提交 `b9f976b2ca05b947ee28eb34ebe590ff0409307f` 增加两个正式路由：

| 路由（相对父 Conversation） | 契约 |
|---|---|
| POST `children/launcher/resolve` | 复用原 verifier，返回 `ChildLauncherResolution`：已验证 invocation 与正式 parent／creation／child identity；原生类型核对二者一致 |
| POST `children/preparations/{creation_id}/governed` | 接收 shared 请求及必填 `ChildConversationPreparationOverrides`，在原生初始化前冻结覆盖；返回可选 governance UUID |

旧 verifier、prepare、read 和 release 路由保留；旧 gate／preparation／plan 未配置治理时省略新增
字段，旧序列化形状保持。新 route 使用已有 Runtime 认证和 native launcher proof；已配置受管父
仍需正式调用证明。未配置父沿用受信部署调用语义。identity 和 governance UUID 都不是用户授权；
用户权限、资源、generation、binding 预留与精确可信 audience 继续由平台校验。

覆盖只允许 governance UUID、移除 secret 名、替换的原生 `SecretSource` 和移除 MCP server 名。
移除／替换名称从 AgentContext 的 secrets 种子中剔除；StoredConversation 的 secrets 同时删除或
替换，选定 MCP 从 Agent 配置中移除，再初始化原生 registry。普通未选中凭据和 LLM 凭据保留。
覆盖不解析 SecretSource，不扫描任意插件／提示词内嵌秘密；平台在 FR-723 必须明确列出绑定级
授权来源和 MCP 名称，不得把此能力描述成自动识别全部父权限。子配置移除六个受管父工具及旧
launcher，并重新验证 Agent，丢弃继承的父实时 executor。

完整覆盖与最终 Agent／StoredConversation 写入原生加密不可变 preparation plan，再创建任何
子原生状态。相同 creation 只能以相同覆盖重放；改值、改 UUID、遗漏覆盖或移除列表变化均为 409。
base_state 已写但 metadata 未写的重试沿用原计划，不重新复制当前父凭据；完整服务 reload 也不会
从父 context 重新填回。受管 plan 丢失／不匹配时 prepare 和 release 拒绝，不能在治理未知时运行。
准备阶段不添加首条用户消息；原 release 的稳定 event ID／运行受理及恢复逻辑保持原生所有。

验收：366 项定向原生回归通过，另有 2 项真实 TCP／受控 LLM 回归通过，覆盖新旧接口、正式
身份、原生首轮释放与后续消息／interrupt。14 项治理测试覆盖初始化前种子隔离、加密、reload、
部分初始化、并发同身份重放和丢失计划拒绝。受影响 Python 的 Ruff／Pyright／依赖及注册检查通过。
独立进程的 CLI F35 验证、缺少真实模型密钥的限制及最终证据记录在进度文档；受控 LLM 结果不作为
真实模型成功。实际 Runtime 镜像与平台两宿主故障链仍待 FR-717。

FlowWeave source lock 保持 `3a4f5f1f61082430e8a2ccf8d59e52c9f4eab5dc`，原 `OH_FW_V3`／
`oh-fw-child-v1` 的固定身份和 Manifest 不变；新源码尚未进入产品 Runtime。FR-721 必须用新增能力
版本／静态契约冻结本次扩展，不能重解释 FR-709 已冻结的身份。FR-722 另需处理首轮父 binding
仍处于 PROVISIONING 的授权时序，不能让父 native Tool 等待其自身首轮事务提交而死锁。

### 2.14 治理扩展归档与 V4 静态契约（FR-721）

构建基线改为 SDK `baseline` 固定提交 `b9f976b2ca05b947ee28eb34ebe590ff0409307f`；
完整 commit 归档 SHA-256 为
`4f53a19445c7c5cfabe022414dd33942fc3c086b0be5b60fd87e9fa0ccc5899f`。
四包继续为 1.58.2，上游锚点不变。source lock、provenance、Docker 输入、当前构建身份与
可执行 contract 探针同步更新；旧归档保留。537 个包内 Python 文件与固定 commit 逐字节一致，
独立 frozen 依赖环境从该归档安装四包并运行完整探针通过；不是实际 Runtime 镜像验收。

新增 `OH_FW_V4` 静态 adapter，Runtime contract schema 为 7，显式要求旧八条 child 路由及
`launcher/resolve`、`preparations/{creation_id}/governed` 两条正式路由。V4 仅接受上述固定
commit／ref 和四包版本，Environment 还核对归档摘要、完整 capability 与原 LLM timeout policy。
新 `oh-fw-child-v2` 能力摘要为
`d7c28b472a8667139698ddcc10211d325514d12f50bc39c21ce3caa3154ad365`，冻结正式 identity
投影、overrides 四个字段、初始化前覆盖、加密不可变计划与相同配置重放；沿用六个父工具、
shared／一层／三个活动子、查询预算及使用说明。构建／恢复发布只按实际 probe 来源声明 v1 或 v2，
不依据控制面“最新”版本猜测。选择 V4 的新 Snapshot 通过原编译入口冻结 v2 capability。

FR-709 的 V3 source／archive／contract schema 6／`oh-fw-child-v1` 与其摘要完全保持。
V3、V4 互不接受对方的来源和能力文档。Workspace 的 V3 创建／恢复改用 V3 固定身份作为期望值，
避免构建基线升级后误取全局 current；实际 HTTP 仍需对旧镜像握手，不将新镜像冒充 V3。
默认 profile 仍 V1，不更新现有 Environment Version、FlowRun、Snapshot、binding 或 generation。
0178 迁移仅为 Environment Version、Runtime Session、Generation 三表追加 V4 允许值；downgrade
持锁并关闭 RLS 隐藏，任一表存在 V4 都拒绝，不清空或改写冻结记录。

RuntimePort 新增独立 V4 方法 `resolve_child_launcher`、`prepare_governed_child_conversation`。
DTO 读取服务返回的正式 identity，校验 parent、creation／child 的原生不变量及精确任务摘要；
不把 SDK property 或本地计算值冒充服务返回字段。准备请求携带 typed overrides，FlowWeave
当前仅需要原生 StaticSecret 替换来源；秘密只在请求边界展开，DTO repr 和普通 JSON 保持脱敏。
V4 响应保留治理 UUID，prepare 必须匹配原请求，read／release 使用 V4 响应类型；请求不自动
release、run、重试或更换 creation ID。V1／V2／V3 在调用两个新方法前拒绝，Mock 同样拒绝。
旧 verifier／prepare 方法的 V3 请求与返回形状不变；V4 可以读取原有公共配置和恢复回执。

FR-721 只冻结可消费契约。FR-722 接入 V4 父工具装配、不可变父能力及首轮调用授权；
FR-723 再接两宿主预留 binding、binding 级凭据重签和首轮恢复，不能用 DTO 或 Runtime key
代替权限。真实模型、实际镜像与双宿主故障验收仍在
FR-717；本片不构建／发布镜像、部署或替换 Runtime。

### 2.15 两宿主 V4 父能力与首轮授权（FR-722）

原 FR-722 包含父能力升级、首轮时序、子预留及凭据恢复，先收敛为可独立验收的父授权切片；
子预留／凭据治理／恢复转 FR-723，FR-712 改依赖 FR-723。当前 `/start` 的授权通过后仍返回
明确 `AGENT_CHILD_CREATION_NOT_ENABLED`，不伪造创建回执，也不调用原生 prepare／release。

新增独立 `GovernedParentCapability` schema 2，profile 为 `OH_FW_V4`，固定 FR-721 source／
archive 与 `oh-fw-child-v2`。原 `ParentCapability` schema 1／V3 继续读取和使用，不由设置或当前
构建身份升级。JSON 仍保存在既有 binding 不可变字段，沿用 0177 trigger、用户隔离和 RLS；无
新增表或迁移。绑定已有父能力时按其冻结 profile 恢复，新 Workspace 可显式选择 V4，默认仍 V1。
临时 sidebar 的 `allow_child_capability=False` 保持关闭；gate／child 不可装配父能力，fork／
replacement 的既有防漂移门禁继续保留。两版本冻结记录之间不能切换 source、profile 或 capability。

Workspace 手动创建／bootstrap、Flow Node 手动创建／bootstrap 和自动 Worker 都装配冻结六工具。
V4 与 V3 的六个 Tool 参数结构相同，复用原编译器；先严格校验对应版本的完整 capability，再由
OpenHands 正式 `tool_module_qualnames` 解析。OpenHands adapter 按契约中的固定 source 选择 V3／
V4，并分别核对八条／十条路由要求，不能只用包版本猜能力。实际 Runtime handle 保留冻结 profile。

V4 在 native create 前提交父 binding 与冻结能力；Workspace 首轮 send 前也结束后续历史授权读取
事务。Flow Node 两个创建入口同样在 create 前提交；Worker 和 inline V4 启动均在冻结后提交，
上传准备后再次核对 lease 并结束事务，再进入 native start。普通 V1／V2／V3 不扩展此提交边界。
这些提交保证首次 Tool 回调能独立读取父身份，不等待父首条消息完成后才变 ACTIVE 的事务。

网关第一阶段仅把 ACTIVE 或冻结 V4 的 PROVISIONING 父作为候选，旧 V3 PROVISIONING、FAILED／
DELETE_PENDING 均拒绝。候选仍须核对用户权限、宿主、镜像、Snapshot 和 active generation，并通过
当前资源派生 key 校验；key 不是授权结果。数据库会话释放后，V4 调正式 `launcher/resolve`，
取得含正式 creation identity 的 `NativeChildResolution`；V3 保持原 verifier。第二个短数据库阶段
重新授权，并严格比对 policy／audience；V4 不能以旧 invocation DTO 通过。撤权、删除、取消、
镜像或 generation 变化均拒绝。PROVISIONING 到 ACTIVE 的正常推进不改写冻结能力。

验证以真实 PostgreSQL、独立原生进程和 HTTP 为证据：四项 TCP 覆盖两宿主 × V3／V4；V4
特意保留 PROVISIONING，断言父原生 Tool 首次回调就完成实际平台复核，关联正式 ActionEvent，
随后只到达明确创建门禁。原生服务来自各自固定归档，复核四包和 536／537 个 Python 文件。
只有 completion 为受控 LLM，物理 Runtime 连接由夹具提供；不是模型、镜像或子创建全链路验收。
V4 的 Workspace、Flow Node 两个手动入口及 leased／inline 启动另验证 native I/O 前无持有事务、
其他数据库会话可读取已冻结 PROVISIONING 父。最终计数和静态检查见进度文档；本片不部署。

### 2.16 两宿主子创建、绑定授权与首轮恢复（FR-723）

平台 gateway 仅对 `OH_FW_V4` 开放真实创建；V3 继续完成原 verifier／用户授权后返回
`AGENT_CHILD_CREATION_NOT_ENABLED`，不追溯改变旧冻结契约。V4 通过正式 `launcher/resolve`
取得原生创建身份，另读父公共配置（允许根父的 parent ID 为 null），再执行三个短事务阶段：

1. 重新校验用户、宿主、generation、不可变父能力和调用证明投影；锁定父 binding，按
   parent＋creation 查询或原子预留 child binding／关系。并发同 invocation 取得同一 binding。
2. 关闭事务后调用 `/preparations/{creation}/governed`，使用 child binding UUID 为
   `governance_id`；读取正式子配置，重新授权并核对身份、shared 目录、原生持久目录和
   准备配置摘要，才将 locator 标为 ACTIVE。ACTIVE 只表示绑定治理完成，不表示原生正在运行。
3. 再次授权，关闭事务后调用正式 `/release`；核对 started 回执，再短事务写入受理时间，
   返回原生 parent／child ID、workspace／shared 与 child binding ID。消息和运行完全由原生创建。

`0179_agent_child_creation_audit` 在已有用户隔离关系表增加创建策略版本、请求摘要、预期公共
Agent 配置摘要和持久目录；旧行四字段全空保留，新行必须完整合法，关系更新仍被既有不可变
trigger 拒绝。已有治理审计时拒绝降级丢弃字段。平台不保存 task、proof、Secret、Agent 文档或
第二套执行状态机。配置摘要只忽略 `AgentContext.current_datetime`：原生 finalize 会刷新此时间，
其余公共配置仍参与核对。绑定治理完成后的重放允许用户后续切换模型等原生配置变化，不重新
要求初始 Agent 摘要相等；正式父子身份、目录、持久目录、请求摘要与治理 UUID 始终校验。

创建策略 `binding-isolated-v1` 在原生 prepare 时明确移除 `FLOWWEAVE_HISTORY_ACCESS` Secret
及 `flowweave-reference-history` MCP，同时排除其平台能力引用。它们不在子首轮运行时借用父身份。
其余冻结能力引用保留 version／digest／原位置；子不取得六个父查询／控制／创建工具或旧创建工具，
不创建新 Attempt／NodeRun／Runtime。当前仅支持原生 `Agent`；ACP 的会话级 activation 不能当作
普通 Secret 安全继承，故在平台预留前以 `AGENT_CHILD_AGENT_UNSUPPORTED` 明确拒绝。

普通认证沿用原生继承语义：创建时 StoredConversation／AgentContext 中的普通 SecretSource
可继承；父后续通过 additive `/secrets` 动态加入的 registry 内容不保证进入子准备计划。平台不会
复制父 credential-sync 记录或伪报子已同步。子后续认证同步必须经 FR-712 的子 binding 专属入口，
不通过扩大本片权限或保存明文弥补。准备计划及恢复时的绑定 Secret 移除由固定 V4 SDK 加密计划保障。

恢复通过原请求／原证明重新进入 gateway：平台查回同一预留，原生 prepare／release 幂等对账，
首条任务按原生稳定 event ID 去重。容量不足返回 429 并保留平台预留；未知写入响应／读取预算
超时返回结果未知，不另造身份。release 之后权限复核失败也返回未知，不能宣称尚未受理。
用户／generation／父生命周期变化始终重新拒绝，不因存在预留自动取得权限。证明过期时，只有
**原生准备回执已存在**且原生证明、父配置和当前分支仍有效才允许原创建恢复；仅有平台预留不足以
绕过过期门禁。平台不持久化证明或 task，未提供无人值守重放循环或无限期恢复承诺。

验证包含真实 PostgreSQL、两宿主 V3／V4 原生 Tool→平台→原生 TCP、服务进程完整重启后原证明
重放、同 invocation 并发预留、准备／配置／释放边界丢失响应、配置不符与撤权拒绝。TCP 中模型
completion 与物理挂载／连接由测试夹具控制，原生路由、正式事件、加密持久化和生命周期真实；
不表示实际 Runtime 镜像、真实模型、前端或远端发布已验收。详细结果见本片进度记录。

### 2.17 两宿主子会话读写、父工具与结果引用（FR-712）

新增 cookie 认证的专用资源 `/api/v1/agent-session-children/{parent_id}/{child_id}`，两宿主
共用服务端 locator 解析。它按当前用户重新取得父 binding、V4 不可变能力、父宿主／scope、
Runtime Session／generation／image，再以直属关系核对子身份；父和子必须已进入 ACTIVE，
关系必须有 `binding-isolated-v1` 创建审计。写操作继续经过原 FlowRun／Attempt 终态围栏；
已取消节点的子会话可读但不能恢复、发送、换模型或同步凭据。浏览器不能指定 native ID、
工作目录、Runtime endpoint 或 owner；普通主会话入口仍拒绝 child，不提供独立页面路由。

| 专用后缀 | 正式行为 |
|---|---|
| GET 根资源、`events`、`hydration`、`input-readiness`、`context` | 读取子实际配置／started 回执，核对原生父子 ID、治理 UUID、shared 工作目录和持久目录；事件只读取最新正式窗口，支持原生 cursor／history_cursor。hydration 返回同批事件／context／readiness，不复制数据库历史或跨 binding 缓存 |
| POST `messages` | 必填用户稳定 UUID `submission_id`，传正式 `/events` 的 user／run=true；原生返回 event_id 为唯一受理事实。与父 send 使用同一个原生输入锁和容量门禁；409／429 不建平台消息队列，未知响应重放原 submission，不枚举历史猜 ID |
| POST `control` | interrupt／resume 只作用于目标子原生会话；condense 复用既有单次持久压缩任务，Worker 重新授权子 locator 后调用原生 condense，HTTP 不等待压缩 LLM。任务状态单独投影，不能伪造 native RUNNING；`Idempotency-Key` 在 child scope 内使用 |
| POST `model` | 复用当前用户可见、测试通过的供应商解析；原生空闲受理后只更新子 binding 的模型／推理配置，不回写父 |
| GET `pending-confirmation`、POST `pending-confirmation/decision` | 复用原生待确认 action／tool-call／digest 和确认接口；拒绝空理由，不发送用户消息代替确认 |
| POST／DELETE `attachments`、GET `file` | 使用子 binding UUID 的 upload owner；通过精确待发送／正式附件记录授权，拒绝借用父或兄弟的 uploads、非规范路径和越界目录。上传以 25 MiB 为限，移除复用宿主的普通文件安全清理；文件响应保持 sandbox CSP 与 nosniff |
| GET `credentials`、POST `credentials/sync` | 普通用户凭据按当前用户解析，单独发送至子原生 `/secrets`，成功及重新授权后才写子 sync 版本。失败不复制父状态、不扩大创建时禁用的历史 MCP／token，也不保存 Secret 明文 |
| GET `result-reference/{event_id}` | 服务端只读回该直属子的正式 assistant MESSAGE，最多一万字，返回来源 binding／event ID 与可见内容；仅预览引用，不修改父草稿或自动发送。用户显式发送时，两宿主父消息入口重新授权并读回正式事件，拒绝浏览器伪造正文或向兄弟／其他父会话引用 |

原生调用使用短事务 prepare → Runtime I/O → 重新授权／投影，传递不可变 locator，不将
SQL Session 带入新的子运行操作。消息附件投影按 child row lock 和正式 event_id 去重；原生
已受理后 locator／权限变化时返回结果未知，不能宣布尚未送达。根资源仅表示可访问性，输入
可用性仍服从正式 readiness。普通父消息的既有引用装配仍复用原路径：Workspace 引用在其
原有装配阶段只读子事件，Flow Node 的新子引用 locator 在 DB 阶段冻结、Runtime 阶段读回；
不因这次接入新增私有历史查询协议或继承父历史 token。

父工具正式回调 URL 的 `queries`／`commands` 现已接通；只对精确 owner／binding UUID 路径
免 cookie，仍必须先通过资源派生 Runtime key 和原生证明，再逐阶段复核用户、V4 policy、
audience、generation 和平台直属关系。命令不信任模型提供的父身份。已冻结 V4 的 PROVISIONING
父仅在原生证明验证后可查询／指令其已治理子，浏览器无此例外。V3 保持创建产品门禁，不因新
入口自动升级。父 list 仅展示已有已启动平台 locator 的原生结果；分页 cursor、完整性标记、
逐段文本 offset 和 checkpoint 原样保留。read 最多 50 项／12000 字；wait 最多 30 秒，单独
使用有界 history lane 和 35 秒整体预算，不占交互读 lane。调用取消不暂停子会话；已开始的
同步原生等待可占用其有界 slot 至返回／超时，不承诺可抢占该线程。

新增 `NativeQuery*`／`NativeCommand*` DTO 直接消费固定 V4 的正式路由和类型；不重签私有
执行证明、重算原生用量或构建平台 Agent loop。原生事件投影保留经类型校验的
`parent_instruction`（start／send／interrupt、parent、action／tool-call、scope、Runtime Session、
generation、manifest）和原生 sender；普通用户输入不添加父来源，不按文本或顺序猜关联。

本片不增加前端标签、浮卡列表、未读聚合、删除／replacement 恢复或部署。子 UI 仍由
FR-714～716 接入，活动／父删除继续由 FR-713 完成；固定 SDK、source lock、V3／V4 Manifest
和已发布镜像不变。相关真实数据库／原生 TCP 和兼容回归结果见本片进度记录。

## 3. 父 Agent 的操作契约

以下工具名是目标命名，最终导出必须在对应 baseline 切片中建立正式类型和 contract：

| 操作 | 结果及边界 |
|---|---|
| `start_governed_child_conversation` | 传完整 task／title；shared 由受管策略冻结；返回稳定正式身份，不等待工作完成；保留旧同类工具默认契约 |
| `list_child_conversations` | 只列当前父会话直属子 Conversation，状态来自原生投影，列表分页 |
| `read_child_conversation` | 读取正式事件进展／assistant 结果，返回 event ID、分页／续读信息及是否完整 |
| `send_child_conversation_message` | 追加正式输入并按原生规则运行，携带稳定原生 invocation 身份，不自动打断 |
| `wait_for_child_conversation` | 等待新进展或状态变化，单次最多 30 秒；超时是正常结果，可取消 |
| `interrupt_child_conversation` | 只调用目标原生 interrupt，不暂停父会话或其他子会话 |

父会话的正式 Tool 事件是执行审计和关联依据。目标 ID 不在当前授权直属关系内时拒绝；
不能仅因处于同一个 Agent Server、工作区或用户下就允许访问其他 Conversation。
工具结果以原生 observation 返回，不伪造成 assistant 最终回复或额外用户消息。

父 Agent 的使用说明随受管 Agent 配置冻结：简单查找自己处理；需要取得子任务结果后继续推理时
优先 Task；需要独立历史、并行开展工作或用户后续介入时使用子会话。不重复委派同一任务。
说明负责模型选择，正式权限负责执行边界。

子会话完成只更新产品投影和未读，不自动为已结束父会话注入消息或启动新一轮 run。
正在执行的父 Agent 可以主动 list／read／wait；读取较长结果必须保留正式分页，不能将截断结果
当作完整阅读。父会话被中断时取消 wait，但不会因此中断已启动子会话。

初始并发策略为每个父 binding 最多三个创建中／运行中的子会话，实际运行还受现有 Runtime
run-slot 上限约束；该策略随 Manifest 冻结。不为等待子会话无限占用线程，不在容量不足时等待
父 run-slot 自己释放而形成死锁。明确未受理的 429 可重试；未知创建结果只能按原身份对账。

## 4. 创建、身份与授权

### 4.1 创建流程

1. 由正式 Tool 调用经固定配置选择的受管 launcher；浏览器不获得 Runtime session key。
2. 校验可信调用凭据及其 active generation、父 Conversation、原生 invocation 身份。
3. 按父 binding 解析用户、宿主、Snapshot／Manifest、工作目录及子会话深度，取得创建配额。
4. 原子预留子 binding、子 Conversation 身份、父子关联及创建审计，提交短数据库事务。
5. 调用正式幂等子会话创建能力，继承允许配置并固定 shared；此时不能先运行首条 task。
6. 核对原生 parent ID、实际工作目录、Runtime 和正式 Agent 配置，建立子 binding 的授权。
7. 为依赖 binding 的凭据重新签发／激活或在原生准备计划中明确禁用；本版按 §2.16 禁用父历史授权，完成必要检查后才投递首条任务并启动。
8. 返回稳定子身份；未知结果使用同一创建身份恢复，绝不另建一个 Conversation 代替。

会话级初始化／身份检查在调用 OpenHands 的同步阶段不得长时间持有数据库事务。继续使用
prepare → native operation → revalidate/project 的短事务边界，并服从既有 generation fence。

launcher 使用上游正式 `launch_url` 接入点；Runtime key 只能证明对应 Runtime 身份，不能单独
授权任意用户／父 binding。可信 invocation 上下文与父关系由 FR-706 定义，不能信任模型提交的
owner、parent binding、宿主或 URL，不能把任意 HTTP 请求当成正式 Tool 调用。

### 4.2 凭据与能力

- 只继承父会话当前允许且可追溯的能力配置，不重新解析浮动“最新版本”。
- 随父配置复制的普通密钥仍遵循现有 Secret Reference／原生加密持久化规则；新增平台记录不存明文。
- 历史查询租约、绑定级 token 等不能复制成另一个 binding 的身份；由原授权机制重新签发，
  无法安全继承时明确禁用该授权而不是借用父 token。首轮运行前完成替换。
- 原生工具保留自身加载生命周期及 HOME／项目 ambient Plugin 规则；不能用禁用 ambient 扫描
  替代显式冻结能力的治理。
- 子会话对 FlowRun 只继承目录和授权定位，不取得节点接受、重跑、Revision 或 Case 决策权限。
- 现有 V1／V2 会话不添加新工具或新字段，不因控制面升级改变其冻结契约。

### 4.3 平台元数据

新增用户隔离的 `agent_child_conversations` 关系记录，至少包含 owner、host kind/id、parent/child
binding ID、原生 parent/child Conversation ID、受理的创建 invocation 身份、生命周期审计时间。
使用现有 `AgentConversationBinding` 承载子会话 locator、显示标题、未读和宿主定位。
关系记录不保存聊天正文、HEAD、cursor 或一套独立执行状态机；运行状态从原生读取和现有投影取得。

唯一约束覆盖 child binding、受理 invocation；拒绝自关联、多父、跨用户、跨宿主、跨 Runtime
和深度超过一层。新表同步加入强制 PostgreSQL RLS 与 `_USER_ISOLATED_TABLES`。
绑定生命周期可用于表示预留／删除步骤，不冒充原生 Conversation execution_status。

普通列表、置顶、活动、搜索和普通创建／fork／引用路由都要识别 child 关系；不得仅过滤一个
列表就宣称子会话隐藏或已授权。访问 child 的普通主会话路由时只回到有权访问的父会话，
不自动打开右侧子标签；API 的读取和写入仍分别校验授权，不依赖路由重定向实现安全。

## 5. 用户续聊与并发

每个子标签使用自己的 binding，用户消息沿用正式消息投递、renderKey 认领、队列、附件、
引用、模型切换、压缩上下文和 readiness 契约。浏览器队列只属于该子 binding：运行中可以编辑，
点击发送显示可取消的待发送项，正式可写后再提交；切换标签不能误发到新选中的会话。
未激活标签保留完整队列，回到该标签并完成正式 hydration 后继续自动发送。发送结果不明的条目
保留原 submission ID，刷新后也须用户显式确认再重试；不自动重发。

父 Agent 和用户发送共用原生并发门禁。readiness 先读再写不能作为并发保证，必须由原生发送
生命周期原子受理。用户使用稳定 submission ID；父工具使用正式 invocation ID。一个请求已
受理后响应丢失，只对账同一原生事件，不能重新追加。目标忙时父工具返回明确 busy，不自动
interrupt，也不在平台数据库另外建立待执行消息队列。

注释先按当前 child／直属 parent 的正式 event ID 复核可见来源，再沿用普通会话的用户选区语义；
DOM compact offset 只用来导航，不冒充原生 Markdown 源文位置。工作区引用沿用规范路径校验，
子私有 uploads 另外核对 owner 和已登记路径。

子会话界面将可信父指令显示为“来自父会话的指令”，但线协议仍遵守 OpenHands 正式 role。
来源由原生正式关联字段证明，不能按正文前缀或相似时间猜测。

## 6. 前台入口与多标签

### 6.1 悬浮卡

在当前“子智能体”一栏之后增加独立“子会话”摘要。没有子会话时隐藏；有记录时显示图标摘要、
运行中／总数、未读／需关注状态及详情箭头。数量和箭头固定可见，摘要允许收缩和裁切，
悬浮卡及其概览禁止横向、纵向滚动。

点击摘要打开子会话选择浮层，列出标题、原生状态和未读；点击具体行才打开／激活对应右侧标签。
长列表在独立浮层中分页，不拉高悬浮卡正文。只选中用户点击的目标，轮询和新创建记录不抢焦点。

### 6.2 一个子会话一个右侧标签

- 标签使用子会话标题，图标／圆点显示它自身的运行、异常和未读状态。
- 同一 parent＋child binding 只挂载一个标签；多个 child 可以同时保留标签，标签栏可横向滚动。
- 子标签采用侧边聊天布局，内容是该 child 的正式 ConversationSurface，而不是父会话摘录。
- 每个标签独立保存消息滚动锚点、可信 hydration、草稿快照、附件、引用和待发送项。
- 关闭选中标签优先选择右侧、否则左侧相邻标签；无其他标签时收起工具区，不删除会话。
- 不提供主会话标签、独立窗口、拖出、弹出或 child 独立会话路由。入口仍仅在父悬浮卡。
- 子标签不提供双击进入主工作台／独立全屏的捷径；工具区通用操作不能绕过此约束。
- 切换父会话按父 scope 保存已打开标签；切回可恢复此前由悬浮卡打开的视图。刷新只恢复父会话，
  不通过 URL／持久 child 参数自动打开子标签。
- 侧栏窄屏沿用工具面板抽屉布局；移动布局仍留在父会话，不跳转 child 主路由。

### 6.3 复用边界

复用 ConversationSurface、文件中央预览、AgentComposerInput、模型菜单和已有授权网关能力。
抽取最小必要的通用交互层，覆盖两宿主及临时侧边聊天；不大规模重构主工作台。

现有 `SidebarConversationPane` 及 `AgentSidebarConversation` 仍承担临时聊天，保留其关闭删除和
一小时 TTL。子会话使用独立组件／宿主适配和关系模型；只复用视觉／已验证通用逻辑，不能直接
替换名称后继承临时聊天的 lifecycle、bootstrap、API 或轮询方式。

所有异步回调、Query key、WebSocket、上传 owner、草稿和乐观状态必须包含 host／parent／child
身份。迟到响应只更新原 scope。首次正式 hydration 之前显示“正在加载会话”，不拿父事件、
本地过期快照或 unknown readiness 显示“正在思考”。

打开子标签按现有预算协调最新事件窗口／历史分页，避免每个标签各建重复定时器。
子绑定使用父作用域正式 WebSocket，按原生 after_seq 续接；每帧及空闲时复核原定位，
删除／generation 变化即关闭。游标脱离当前分支时同预算回退正式最新窗口并移除旧后代。未激活标签
只维护轻量状态，激活标签恢复正式流及串行 REST 协调。父首屏读取优先于后台子会话预热。

### 6.4 输入与快捷键

保留子发送框的附件、模型／推理程度、暂停／继续和原生 condense。父草稿和各子草稿分开。
子会话运行中输入保持可编辑，发送入口统一进入该 scope 浏览器队列。压缩期间继续遵循正式
CONDENSATION 生命周期和队列门禁，不显示普通运行中的暂停按钮。

暂停只作用于焦点所在子会话；全局两次 Esc 使用同一正式 interrupt mutation，并遵守弹层
捕获阶段 preventDefault。焦点在子面板时不能暂停父；焦点不明确时不执行隐式跨会话暂停。

## 7. 跟踪、未读和结果使用

父自身运行状态与子运行状态分开。父已结束、子仍运行时父行可显示“子会话：1 个运行中”，
活动投影提供这个父入口，但不能点亮父自己的运行圆环或伪造父 readiness。
首轮 started 与正式发送受理只登记终态补读；原生活动目录的更新时间变化也触发补读，
覆盖快速完成及回执丢失。`child_completion_checked_activity_at` 仅节流读取，未知／失败
读取不推进该水位；完成去重和预览始终使用正式 assistant event ID，不以时间关联消息。
普通完成蓝点、系统异常标识按现有服务端 unread／origin 规则聚合，子会话不能作为独立普通
活动行出现。点击／激活具体子标签只确认该 child 已读，不清除父或其他 child 的未读。

父本轮结束或被暂停不会自动中断子。界面需在存在运行子会话时明确显示“子会话仍在运行”。
可提供明确“暂停全部子会话”操作，逐个调用原生 interrupt，并显示部分成功／失败；不能
因父状态推断所有子状态。连接未知保持待同步，不把断流等同终态。

子消息上的“引用到父会话”将选定正式 assistant event ID 添加到父草稿，保留原草稿，
不自动发送／run。引用授权复用已有历史读取租约，并为父／子两端显式校验用户及宿主。
父工具读取的结果通过原生 observation 返回。结果／附件先中央预览，所有路径按 child
binding 解析；shared 只表示工作目录相同，不表示附件 owner／授权可以互换。
父草稿及历史里的子结果引用仅在中央预览正文，不打开 child 主路由；子标签仍由悬浮卡选择。
私有 child uploads 只能通过子授权预览和下载，共享目录文件可显式转到父文件工具。

## 8. 保留、删除及恢复

- 子生命周期跟随所属父／宿主的正式保留策略，不采用一小时 TTL。
- 隐藏悬浮卡、关闭右侧标签、关闭工具区、切换父会话均不得调用删除／暂停接口。
- 父删除前围栏新增子创建，显式列出关联子会话，使用独立可恢复删除步骤逐个清理。
- 原生删除父会话会留下 child，平台不得假定原生级联。失败时保留父删除中记录和子定位，
  继续可诊断／恢复，不能让隐藏的运行子会话失去唯一入口。
- 删除只处理明确授权的会话状态、独有附件与关系记录，不删除共享工作目录或持久 Workspace。
- 两宿主含子会话的删除返回 `202` 与原任务标识、子 binding 清单和清理进度。父和子进入
  `DELETE_PENDING` 后关闭交互／新增创建，普通列表与活动入口继续显示父的清理状态；失败时
  从同一父记录重试同一任务。无子关联的既有同步删除继续返回 `204`。
- 浏览器等待正式 DELETE 响应再改变列表归属，避免新增子会话与删除之间的竞态将 family 202 当作完成。
  受理后按用户／宿主／工作区保留父删除投影，轮询正式元数据；只有确认缺失才移除入口。
  清理阶段、原子清单和失败码由原任务投影，刷新及切换继续可见，重试沿用同一任务。
- 已审计子关联先逐子执行原生删除，再删除父作为在途 prepare／release 的原生生命周期围栏，
  随后复核并逐子删除一次，确认后才移除关系与私有记录。原生明确缺失可幂等重试；租约或
  active generation 变化时保留定位，不以未知结果推进清理。记录级删除复用此顺序，保留外层
  NodeRun／Attempt 事务及重试语义。
- 私有附件立即按 binding UUID 前缀安全清理，并保留十分钟后的独立回收任务，覆盖删除前
  已受理的 Runtime 文件上传晚到或响应丢失。回收凭据仅在受保护任务载荷保存服务端已授权
  的物理根，不出现在浏览器投影；回收仅删除该 UUID 的普通附件文件，已有正式 binding 时拒绝清理。
- Runtime replacement 使用原 Session、冻结镜像和原父／子 Conversation ID，核对正式事件身份。
  FlowRun 共享和历史 Attempt 独占两种 Session 均走同一持久替换任务；独占入口在恢复期间只登记
  幂等任务并返回待恢复，不能从普通 provisioning 路径直接激活。Provider 必须确认旧 writer 停止。
- 替换前后按原父创建 action ID／tool_call_id、直属 parent ID、创建 receipt／governance ID、
  请求摘要、共享目录和 persistence 核对；已 started 的子必须保有正式事件。原生尚未物化的
  预留或 prepared 状态原样保留，不在读取探针中创建空会话或重新投递首轮。
- Agent Workspace 存在 V4 冻结父能力时沿用原 image digest，恢复不重新选当前平台镜像；
  FlowRun 继续使用原有冻结镜像。新 generation 在全部身份校验通过并激活后恢复用户侧读取、
  续聊和用量对账。旧 locator 的迟到回调失败，旧 generation 的父工具策略继续拒绝，不自动
  重签冻结父工具；需要新父工具授权时由新父会话建立新 generation 的原生能力。
- Worker／API 在创建或删除步骤间崩溃后按既有审计及幂等身份恢复，不生成空会话冒充成功。

## 9. 冻结协议与验证门禁

新增 `OH_FW_V3` 静态 adapter 和追加式允许值迁移，不修改 V1／V2 请求形状或已发布记录。
子 ToolSet、shared 策略、深度／并发、父工具使用说明及版本摘要进入 Manifest。旧 Runtime
不支持时明确报告能力不可用，不通过探测“最高版本”或平台模拟回退。

baseline 合并需保留当前 View Token／事件指标、活动目录、执行器／驱逐隔离、读容量、
中断工具收口、过时 tool-call 清理、晚到凭据动态上下文等全部现有扩展。源码冲突集中在
conversation_service、event_service、models、agent 及真实传输测试；自动合并路径也需复核。
目标 Profile schema 4／Agent Settings schema 9 与现有 contract 的差异须独立验收。
FlowWeave 自有 Dockerfile、依赖锁及镜像组成必须核对上游安全修复，不以源码合入代替同步。

每个实现切片完成其定向测试及基础检查；最后以真实镜像／TCP、隔离 PostgreSQL 和两宿主
Chromium 验收。受控 API／LLM 测试与真实模型执行结果分别记载，不相互替代。
FR-702 仅做文档契约、依赖／状态、固定源码事实和迁移 head 检查，不声称功能测试通过。

## 10. 切片依赖

| 切片 | 内容 | 依赖 |
|---|---|---|
| FR-702 | 冻结本设计与子会话专用实施边界 | FR-701 |
| FR-703 | 固定 upstream main 完整合入 SDK baseline 并保留扩展 | FR-702 |
| FR-704 | 固定 baseline 回归与 Runtime 来源／依赖身份 | FR-703 |
| FR-705 | 正式子创建身份、请求摘要与持久幂等预留原语 | FR-704 |
| FR-718 | 原生 child 准备／查回、运行前治理及首条任务原子受理／恢复 | FR-705 |
| FR-706 | 原生 launcher 调用身份、直属权限、shared／深度策略 | FR-718 |
| FR-707 | 原生 list／read／有界 wait 工具 | FR-706 |
| FR-708 | 原生 send／interrupt、消息来源及并发受理 | FR-707 |
| FR-709 | 最终扩展归档、V3／Manifest 与兼容契约 | FR-708 |
| FR-710 | 子关联元数据迁移、RLS 与普通入口隔离 | FR-709 |
| FR-711 | 原生子创建 HTTP 适配与配置核对 | FR-710 |
| FR-719 | 两宿主父能力冻结与 launcher 调用授权 | FR-711 |
| FR-720 | 原生创建身份投影与准备凭据冻结 | FR-719 |
| FR-721 | 新治理扩展归档与静态能力契约冻结 | FR-720 |
| FR-722 | 两宿主 V4 父能力装配与首轮授权 | FR-721 |
| FR-723 | 两宿主子预留、凭据重签、首轮释放及恢复 | FR-722 |
| FR-712 | 两宿主续聊／控制／附件／结果引用接口 | FR-723 |
| FR-713 | 子活动／独立未读聚合与直属子列表 | FR-712 |
| FR-724 | 父删除围栏与逐子恢复清理 | FR-713 |
| FR-725 | 原父子身份 Runtime 替换恢复 | FR-724 |
| FR-726 | 快速完成与恢复首轮的子未读对账 | FR-725 |
| FR-714 | 通用侧边会话交互边界，保留临时聊天语义 | FR-712 |
| FR-727 | 子会话正式流代理与游标失效恢复 | FR-726、FR-714 |
| FR-715 | 悬浮卡子列表与按标题多标签 | FR-727、FR-714 |
| FR-716 | 子标签独立输入、模型、草稿、快捷键与引用 | FR-715 |
| FR-728 | 父子删除任务与恢复状态的前端投影 | FR-716、FR-724 |
| FR-717 | 固定 Runtime／数据库／故障／两宿主端到端验收 | FR-725、FR-728 |

FR-703 的 SDK merge commit 和后续各扩展独立提交；FlowWeave 来源锁更新只认已提交且已验证
的 SDK 身份。实际部署和既有 Runtime 升级不属于本轮，不能以 FR-717 完成自动触发。

最终门禁至少包含：两宿主真实 shared 创建；同 invocation 并发／超时不重复；绑定级凭据
不串用；用户和父工具并发发送；父结束子继续；多个子标签迟到响应／草稿／附件隔离；
断流／前台恢复／历史分页；原生最终结果引用；关闭不删除；父删除失败不孤立；原 ID
replacement；非直属／跨用户／跨宿主拒绝；无 Worktree 创建；旧 V1／V2 不变。
