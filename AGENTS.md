# FlowWeave 协作指南

本文件适用于整个 FlowWeave 仓库，并补充上级
`/Users/zhengmengen/WorkSpace/AGENTS.md`。若目标目录存在更近的 `AGENTS.md`，优先遵守离目标文件最近的说明。

## 远程部署与私有配置

生产服务器地址、域名、账号、SSH 配置、部署根目录、网络拓扑和运行时路径都不得提交到 Git，亦不得写入公开文档、Issue、PR、聊天记录或构建日志。将此类信息仅保存在本机受权限保护且被 Git 忽略的 `.local/remote-deploy.env` 中；从 `deploy/remote-deploy.env.example` 创建该文件。

任何远程部署、发布、远端镜像、生产、SSH 或远端排障，先完整读取 `docs/deployment.md` 和 `docs/local-build-and-deploy.md`，然后运行：

```bash
scripts/verify-remote-deploy.sh --config .local/remote-deploy.env \
  --commit <已提交的完整或短 SHA> --scope <web|platform|runtime|other>
```

预检必须复述从本地配置读取的目标、部署根、主 Compose/env、构建/镜像目录、可选 stream-api Compose/env、发布范围和 commit，并通过只读 SSH 验证其 Compose 拓扑。未提供有效本地配置，或声明入口未通过预检时停止，不得猜测 SSH 别名、目标环境、Compose 入口或 stream-api 所属项目。普通部署严禁 `docker compose down -v`、删除 volume/Workspace、覆盖远端 Compose 或环境文件。`make rebuild-deploy` 和 `infra/compose.yaml` 仅用于本地；绝不可当作远端部署入口。

### 受管内部服务器的配置变更

仅当请求人在当前会话明确确认目标是其受管内部服务器，并明确授权本次变更时，发布人员可以在已通过预检的唯一目标上更新服务器侧 Compose 或环境文件。该例外仅用于让已提交版本新增或变更的服务可部署，且必须同时满足：

- 先在服务器侧为原 Compose 和环境文件创建带时间戳、权限保持不变的备份；绝不将其复制到仓库、日志或聊天中。
- Compose 改动必须是明确的服务级补丁，沿用已验证的网络、卷、镜像命名和健康检查约定；不得用本地 `infra/compose.yaml` 覆盖远端文件，或改变无关服务。
- 新增密钥只能在目标服务器通过安全随机源生成并直接写入受保护环境文件；不得回显、记录、提交或在任何 URL／镜像层中传递明文。密钥变更只限本次新增服务所需变量。
- 修改后必须先运行 `docker compose config --quiet`，再按受影响服务执行构建、迁移、健康检查和路由验证；若任一步失败，保留日志并从刚创建的备份仅恢复本次触及的文件或服务。
- 即使适用本例外，仍禁止删除卷、工作区或持久数据，禁止 `docker compose down -v`、`--remove-orphans`、`docker system prune`、`reset --hard` 或任何未经单独明确授权的清理操作。

远程 Compose、环境文件、持久数据路径和私有入口均为服务器侧资产，不得复制进仓库。更新 API 时，必须从同一已提交版本同步更新并 recreate `stream-api`；不得以 orphan 或 `--remove-orphans` 忽略它。发生故障时先保留日志和数据库错误状态，再仅回滚受影响服务；禁止使用删除 volume、清空 Workspace、`reset --hard` 或 `clean -f` 代替回滚。

## OpenHands-first 架构原则

- FlowWeave 的产品设计、用户流程和业务边界是需求来源；OpenHands 是 Agent 执行能力的实现依赖，不是产品能力清单。
- FlowWeave 是控制面，只负责能力治理、不可变版本冻结、权限、策略、审批、审计、资源隔离和业务投影。
- Tool、Skill、Plugin、MCP、Hook、Agent Definition、Task 子 Agent、Condenser、Memory、Critic、Fork 和 ACP 等执行能力应由 OpenHands 正式类型、事件、API 和生命周期实现。
- 不得用提示词、私有控制 JSON、文本约定、私有 HTTP 或平台自建执行器模拟 OpenHands 已提供的能力。
- FlowWeave 显式传入的 Runtime 能力必须可追溯到固定 version、digest、blob/hash 和 Snapshot Runtime Manifest，明文 Secret 不得持久化进入 Runtime。OpenHands 原生的 HOME/项目 ambient Plugin 发现明确允许，它不是 FlowWeave 冻结 Plugin 的替代事实源，也不得用私有字段或源码补丁禁用。
- 事件关联必须使用 OpenHands 正式的 `id`、`parent_id`、`action_id`、`tool_call_id`、cursor 等字段，不得按事件顺序、名称或文本猜测。

## OpenHands 源码与镜像基线

OpenHands `baseline` 是 FlowWeave 的持续集成基线：先合并已验证的 upstream `main`，再在同一 `baseline` 分支追加经审计的 FlowWeave Runtime 扩展。当前上游锚点为 `e21d77673b738f056676044600c4ad81c5a575c8`，四包发布版本为 `1.49.5`；实际 Runtime 身份始终以 `baseline` 的固定 HEAD commit、归档 SHA-256 和 source lock 为准，而非假定等于上游锚点。

- SDK 源码工作树可在 `baseline` 分支实施最小、可测试的 FlowWeave 扩展；不得直接修改 `main`，不得基于浮动上游提交构建，也不得在镜像构建期施加未进入 `baseline` 提交的源码补丁。
- 每次合并上游后必须重新验证所有 FlowWeave 扩展；发生冲突时保留扩展的明确契约、更新其测试，并以新的 `baseline` commit、不可变归档和镜像 provenance 锁定。
- FlowWeave 适配器只能消费这些扩展提供的正式 OpenHands 路由、类型和事件生命周期；不得用平台侧重算、完整历史或模型 usage 统计伪造当前 View 指标。

- SDK 源码：`/Users/zhengmengen/WorkSpace/openhands/software-agent-sdk-total-tokens-1.47`（`baseline`）
- 历史兼容基线：`v1.42.0` / `f09e03eac772290feeb51b7d7390ffaefeca1a09`
- 固定包版本：`openhands-agent-server==1.49.5`、`openhands-sdk==1.49.5`、`openhands-tools==1.49.5`、`openhands-workspace==1.49.5`
- 固定运行时镜像：`flowweave-openhands-runtime:1`
- 契约探针：`infra/openhands/contract_check.py`

能力判断优先读取 source lock 锁定的 `baseline` commit，并只在当前切片确有需要时取证：

```bash
git -C /Users/zhengmengen/WorkSpace/openhands/software-agent-sdk-total-tokens-1.47 \
  show <source.lock.json 的 source_commit>:<相对路径>

git -C /Users/zhengmengen/WorkSpace/openhands/software-agent-sdk-total-tokens-1.47 \
  grep -n '<模式>' <source.lock.json 的 source_commit> -- \
  openhands-agent-server openhands-sdk openhands-tools openhands-workspace
```

证据优先级从强到弱为：固定源码构建的实际镜像及可执行探针、固定 commit 源码和测试、FlowWeave source lock 与适配代码、历史兼容源码和镜像、版本明确匹配的官方文档。不得凭记忆或浮动 `main` 推断契约。

## FlowRun Runtime 重构恢复方式

每次开始或恢复任务时依次执行：

1. 完整读取 `docs/flowrun-openhands-runtime-design.md` 和 `docs/flowrun-runtime-task-progress.md`。
2. 检查 `git status --short --branch`、未提交 diff 和当前 Alembic heads。
3. 检查进度文档是否最多只有一个 `CURRENT`，以及当前切片依赖是否全部 `DONE`。
4. 若工作树存在未闭环切片，先完成该切片；不得越过它开始后续切片。
5. 一次只完成一个最小可独立验收切片；实现边界过大时先拆分任务和依赖。
6. 按进度文档要求完成实现、基础检查和状态更新。
7. 切片完成后提交该切片代码和文档；确认提交成功后立即停止，不自动开始下一项。

源码、迁移、测试和实际运行结果是当前进度的权威证据。旧的 OpenHands 重构文档和历史验收结论不得替代当前 `FR-*` 任务重新实施与验证。

## 本地临时产物

- 一次性 API 请求体、dry-run 结果、平台响应快照、临时能力包、调试导出和工具中间文件必须写入仓库根目录的 `.tmp/`。
- 禁止在仓库根目录或任何受版本控制的源码/文档目录创建临时文件。
- `.tmp/` 已由 `.gitignore` 忽略；提交前仍须检查 `git status`，确保没有临时产物被暂存。
- 仅在需要保留为正式、可复现资产时，才将内容移入相应的版本化目录并使用明确、非临时的文件名。

## 切片验证与提交规则

- `FR-01`–`FR-11` 只运行进度文档允许的最窄语法、解析或编译检查，以及 `git diff --check` 和任务状态唯一性核对；不得提前运行集中在 `FR-12` 的业务测试、迁移实跑、完整构建、Runtime、安全或 E2E 门禁。
- `FR-12` 负责进度文档列明的完整故障恢复、安全、契约、迁移和 E2E 验证。
- 提交前必须复核 staged diff，确保只包含当前切片和必要的协作文档变更，不混入无关改动、密钥、缓存或生成物。
- 每个完成切片使用独立、可审计的 Git commit；提交信息应包含切片编号和结果，例如 `feat(runtime): complete FR-01 environment binding`。
- 提交成功是切片收尾的一部分。提交后只报告提交哈希、验证结果和下一可执行切片，不继续实现下一项。
## Web 会话状态

- `AgentSessionWorkbench` 中会在异步发送、重写或流订阅回调内更新的本地事件和 UI 状态，必须携带并校验 `bindingId`；不能只依赖会话切换 effect 清空共享状态，否则旧会话的迟到回调会污染新会话。
- 会话未读状态是用户隔离的服务端 `AgentConversationBinding.unread` 投影；Agent Workspace 与 FlowRun node-session 两种宿主必须共同读写该字段。浏览器 `localStorage` 仅用于置顶等设备本地展示偏好，不能作为未读事实源。前端切换会话时先乐观更新，再异步持久化；写请求未完成期间必须让本地目标值覆盖列表刷新，并用请求代次忽略同会话较旧写响应，避免旧服务端快照造成未读样式回退。
- Composer 草稿的文本、附件、引用和注释必须作为带 `scope` 的同一快照读写；会话切换先持久化 outgoing scope，再恢复 incoming scope。子组件卸载 cleanup 不得从共享 ref 读取内容后写入捕获的旧 scope。未创建草稿切换后必须保留可发现的恢复入口；用户显式新建会话仍须创建全新的空 scope，仅页面自动进入或用户点击草稿入口时才允许恢复同工作区的未创建草稿。
- 会话运行中的视觉状态不能只依赖可能短暂抖动的 Runtime readiness；只要正式事件树仍存在未完成用户轮次且未超过终态同步期限，就必须保持会话活动和底部任务计划的 DOM、动画与布局稳定。
- 未创建会话的附件以草稿 UUID 为 owner 写入当前宿主的 `uploads/`；用户显式放弃草稿或移除附件时必须调用宿主级安全清理，且上传晚于放弃时由上传完成回调补偿。服务端只能删除 UUID owner 前缀、规范路径内的普通文件，并在同 UUID 已存在正式 `AgentConversationBinding` 时拒绝删除；每个草稿上传还必须登记延迟兜底回收，以覆盖页面关闭、断网和客户端清理失败。

- Token/事件上下文指标允许 Runtime 暂时返回未知；同一 binding 已有可信指标时应保留最近可信值，首次未知仍明确显示待更新，且不得跨 binding 复用。
- 会话交互态与视觉态必须分离：暂停、继续、发送等操作服从 Runtime readiness；运行中展示按正式事件单调推进，不能因中间轮询回退。首次打开会话且 Runtime 状态未知时只显示明确加载态，不得将历史事件缺口呈现为“正在思考”。自动贴底仅由新事件、历史锚点恢复或用户操作触发，禁止用整个会话树的 ResizeObserver 响应状态文案和动画尺寸变化；已到目标位置时不得重复写 `scrollTop`。
- 会话首屏 hydration 只读取 OpenHands 最新事件窗口及同批 context/readiness；更早历史由浏览器低优先级分页恢复，不能为了首屏串行读取完整分支。
- API 侧 hydration 缓存必须按用户、宿主、binding、Runtime Session/generation 和 OpenHands Conversation 隔离。不可变正式事件按 event id、分支快照按 leaf 缓存；继续执行只先失效 current/terminal 指针并递增 epoch，保留旧 branch/event。旧 epoch 的迟到请求不得恢复 current；运行中 hydration 只允许 single-flight，不得成为长期 current 缓存。
- 首次进入、刷新后或可信租约过期的既有会话必须重新取得该 binding 的正式 hydration，并在结果返回前只显示“正在加载会话”；React Query、浏览器持久快照或不完整事件不得抢先渲染，也不得参与“正在思考”判断。当前标签页内刚完成 hydration 的终态会话可短期直接复用完整快照；运行中会话短暂切换回来也应保留快照、立即恢复 WebSocket，并在后台补读最新窗口与 readiness。快速切换时必须合并短时间内的选择，并将 hydration 限制为单飞、完成后只追赶最后选择的 binding；宿主卸载时用 AbortSignal 取消浏览器请求，不能让每次点击都并发占用交互读取池。
- 浏览器从后台恢复可见或窗口重新获得焦点时，活动会话必须立即从无 cursor 的最新 OpenHands 事件窗口对账，并刷新会话与 readiness 投影；不能仅等待受后台节流的定时轮询或 WebSocket 重连。`visibilitychange` 与 `focus` 可能连续触发，应合并同一轮恢复。
- 运行中 REST 事件恢复必须由单一协调器串行调度：有 `next_cursor` 时优先增量追赶，定期或在 `message_complete`、断流、WebSocket 重连、前台恢复时读取无 cursor 最新窗口；不得让 React Query 定时器与自建定时器并行轮询同一会话。强制最新窗口信号发生在增量请求期间时必须排队补读，不能被 in-flight 去重吞掉。
- 历史分页完成后必须记住已耗尽的入口 `history_cursor`，避免最新窗口刷新重新激活同一分页链；若服务端返回新的入口游标，仍必须允许读取新增历史。
- 最终回复正文只从 OpenHands 正式 `MESSAGE` 事件一次性渲染；浏览器不得展示 StreamContext 文本 delta 或模拟打字光标。`message_complete` 与断流只触发正式事件补读，不单独决定轮次结束；Tool、Thought、Task 等正式过程事件仍可实时追加展示。
- 当前页面发送的用户消息使用浏览器稳定 `renderKey` 和正式 OpenHands `event.id` 双身份：正式事件只认领并补全已有本地气泡，不能创建第二个用户气泡；刷新后直接按正式历史渲染。认领必须依赖提交 ID 与正式事件 ID，不得按正文或时间相似度猜测。
- Runtime readiness 一旦确认终态，输入框、按钮和侧栏运行样式必须立即恢复；正式终态事件的补读只能在后台进行，不能呈现“正在对账”或继续占用运行态。为避免上一轮排队消息误发，可设置短时且不可见的队列门控，但必须有界并保留用户确认权。
- 会话配置仅管理能力与认证；新会话和既有会话的模型、供应商及推理程度都在发送框中选择。
- 已创建且可写、处于 idle 或 paused 的会话必须在 `/` 菜单提供“压缩上下文”；该操作只调用 OpenHands 原生 condense 控制接口，不得发送用户消息或创建乐观消息气泡。点击后的本地 pending 与持久任务投影仅用于立即反馈和刷新恢复，不能伪造普通 `execution_status=RUNNING`；最终历史展示与完成收口只认正式 `CONDENSATION_REQUESTED` / `CONDENSATION_COMPLETED` 事件。压缩期间输入保持可编辑，所有发送入口统一排入浏览器队列，禁止直接发送、重复压缩、展示暂停按钮或调用 interrupt。
- 会话中的附件、工作区文件链接、候选输出文件和生成图片统一先在页面中央预览；工作区资源从预览弹窗显式跳转文件栏，不应在首次点击时直接展开侧栏。
- 逐步节点启动的 `confirm-start` 会先原子预留 FlowNode `AgentConversationBinding`；Attempt 详情必须投影该 `binding_id`，前端收到成功响应后应以记录自身的 FlowRun ID、NodeRun ID、Attempt ID 和此 binding 打开节点会话，并同步失效逐步记录列表。不得通过猜测或等待 Runtime 的原生 conversation ID 来构造页面路由。
- 连续运行草稿保存使用乐观锁 `expected_row_version`。前端遇到 `VERSION_CONFLICT` 时可仅对同一未启动草稿读取最新详情后，以用户当前编辑的严格写入载荷重试一次；不得回显详情中的冻结审计字段，也不得吞掉其他错误或无限重试。