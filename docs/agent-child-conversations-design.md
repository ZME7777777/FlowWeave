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

## 3. 父 Agent 的操作契约

以下工具名是目标命名，最终导出必须在对应 baseline 切片中建立正式类型和 contract：

| 操作 | 结果及边界 |
|---|---|
| `start_child_conversation` | 传完整 task／title；shared 由受管策略冻结；返回稳定正式身份，不等待工作完成 |
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
7. 为依赖 binding 的短期凭据重新签发／激活，完成必要检查后才投递首条任务并启动。
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

父 Agent 和用户发送共用原生并发门禁。readiness 先读再写不能作为并发保证，必须由原生发送
生命周期原子受理。用户使用稳定 submission ID；父工具使用正式 invocation ID。一个请求已
受理后响应丢失，只对账同一原生事件，不能重新追加。目标忙时父工具返回明确 busy，不自动
interrupt，也不在平台数据库另外建立待执行消息队列。

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

打开子标签按现有预算协调最新事件窗口／历史分页，避免每个标签各建重复定时器。未激活标签
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
普通完成蓝点、系统异常标识按现有服务端 unread／origin 规则聚合，子会话不能作为独立普通
活动行出现。点击／激活具体子标签只确认该 child 已读，不清除父或其他 child 的未读。

父本轮结束或被暂停不会自动中断子。界面需在存在运行子会话时明确显示“子会话仍在运行”。
可提供明确“暂停全部子会话”操作，逐个调用原生 interrupt，并显示部分成功／失败；不能
因父状态推断所有子状态。连接未知保持待同步，不把断流等同终态。

子消息上的“引用到父会话”将选定正式 assistant event ID 添加到父草稿，保留原草稿，
不自动发送／run。引用授权复用已有历史读取租约，并为父／子两端显式校验用户及宿主。
父工具读取的结果通过原生 observation 返回。结果／附件先中央预览，所有路径按 child
binding 解析；shared 只表示工作目录相同，不表示附件 owner／授权可以互换。

## 8. 保留、删除及恢复

- 子生命周期跟随所属父／宿主的正式保留策略，不采用一小时 TTL。
- 隐藏悬浮卡、关闭右侧标签、关闭工具区、切换父会话均不得调用删除／暂停接口。
- 父删除前围栏新增子创建，显式列出关联子会话，使用独立可恢复删除步骤逐个清理。
- 原生删除父会话会留下 child，平台不得假定原生级联。失败时保留父删除中记录和子定位，
  继续可诊断／恢复，不能让隐藏的运行子会话失去唯一入口。
- 删除只处理明确授权的会话状态、独有附件与关系记录，不删除共享工作目录或持久 Workspace。
- Runtime replacement 使用原 Session、冻结镜像和原父／子 Conversation ID，核对正式事件身份。
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
| FR-711 | 两宿主创建治理、凭据继承和崩溃恢复 | FR-710 |
| FR-712 | 两宿主续聊／控制／附件／结果引用接口 | FR-711 |
| FR-713 | 子活动／未读聚合、父删除及恢复保护 | FR-712 |
| FR-714 | 通用侧边会话交互边界，保留临时聊天语义 | FR-712 |
| FR-715 | 悬浮卡子列表与按标题多标签 | FR-713、FR-714 |
| FR-716 | 子标签独立输入、模型、草稿、快捷键与引用 | FR-715 |
| FR-717 | 固定 Runtime／数据库／故障／两宿主端到端验收 | FR-713、FR-716 |

FR-703 的 SDK merge commit 和后续各扩展独立提交；FlowWeave 来源锁更新只认已提交且已验证
的 SDK 身份。实际部署和既有 Runtime 升级不属于本轮，不能以 FR-717 完成自动触发。

最终门禁至少包含：两宿主真实 shared 创建；同 invocation 并发／超时不重复；绑定级凭据
不串用；用户和父工具并发发送；父结束子继续；多个子标签迟到响应／草稿／附件隔离；
断流／前台恢复／历史分页；原生最终结果引用；关闭不删除；父删除失败不孤立；原 ID
replacement；非直属／跨用户／跨宿主拒绝；无 Worktree 创建；旧 V1／V2 不变。
