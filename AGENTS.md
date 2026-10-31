# FlowWeave 协作指南

本文件适用于整个 FlowWeave 仓库，并补充上级
`/Users/zhengmengen/WorkSpace/AGENTS.md`。若目标目录存在更近的 `AGENTS.md`，优先遵守离目标文件最近的说明。


## Agent 工作区浏览器界面

- 浏览器地址栏由 `apps/web/src/components/agent-session/AgentSessionWorkbench.tsx` 的 `WorkspaceBrowser` 渲染；新工作区标签页本身就是浏览器，不要在启动页重复提供“浏览器”工具卡片。
- 浏览器与启动页视觉样式集中于 `apps/web/src/pages/agent-workbench.css`；修改后至少运行 `pnpm --filter @flowweave/web typecheck`，并运行 `agent-conversation-schedules.spec.ts` 中对应的定向端到端用例。

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

普通 `web`／`platform` 发布必须按 `docs/deployment.md` 使用 `make remote-release-prepare` 的 commit-bound 最小上下文，并在健康、版本与路由验收后执行 `make remote-release-finish`，保留最近 24 小时的受管上下文、所有实际路径引用和每仓库一个回滚镜像，避免继续创建无人管理的源码副本和 tar 包。该收尾只回收受管发布上下文与精确平台／Web 历史 image tags，不得扩大为共享 BuildKit、卷、Workspace 或数据库备份清理；Runtime 发布继续遵守独立预检与活跃会话门禁。

## OpenHands-first 架构原则

- FlowWeave 的产品设计、用户流程和业务边界是需求来源；OpenHands 是 Agent 执行能力的实现依赖，不是产品能力清单。
- FlowWeave 是控制面，只负责能力治理、不可变版本冻结、权限、策略、审批、审计、资源隔离和业务投影。
- Tool、Skill、Plugin、MCP、Hook、Agent Definition、Task 子 Agent、Condenser、Memory、Critic、Fork 和 ACP 等执行能力应由 OpenHands 正式类型、事件、API 和生命周期实现。
- 不得用提示词、私有控制 JSON、文本约定、私有 HTTP 或平台自建执行器模拟 OpenHands 已提供的能力。
- FlowWeave 显式传入的 Runtime 能力必须可追溯到固定 version、digest、blob/hash 和 Snapshot Runtime Manifest，明文 Secret 不得持久化进入 Runtime。OpenHands 原生的 HOME/项目 ambient Plugin 发现明确允许，它不是 FlowWeave 冻结 Plugin 的替代事实源，也不得用私有字段或源码补丁禁用。
- 事件关联必须使用 OpenHands 正式的 `id`、`parent_id`、`action_id`、`tool_call_id`、cursor 等字段，不得按事件顺序、名称或文本猜测。

## OpenHands 源码与镜像基线

OpenHands `baseline` 是 FlowWeave 的持续集成基线：先合并已验证的 upstream `main`，再在同一 `baseline` 分支追加经审计的 FlowWeave Runtime 扩展。当前上游锚点为 `f0206428306db687ac9c8d833ece4fb82b015eba`，四包发布版本为 `1.54.0`；实际 Runtime 身份始终以 `baseline` 的固定 commit、归档 SHA-256 和 source lock 为准，而非假定等于上游锚点或浮动分支 HEAD。

- SDK 源码工作树可在 `baseline` 分支实施最小、可测试的 FlowWeave 扩展；不得直接修改 `main`，不得基于浮动上游提交构建，也不得在镜像构建期施加未进入 `baseline` 提交的源码补丁。
- 每次合并上游后必须重新验证所有 FlowWeave 扩展；发生冲突时保留扩展的明确契约、更新其测试，并以新的 `baseline` commit、不可变归档和镜像 provenance 锁定。
- 每次推进或发布受治理的 baseline，必须同步更新独立 Agent Workspace Runtime：更新 source lock／归档／构建身份，验证固定镜像，再在正式活跃会话门禁通过后通过平台 replacement 更新既有 generation。仅更新 SDK、镜像 tag、Web／API／Worker 或 Provider 不算完成。验收必须核对实际运行容器的 image digest、四包版本、`/server_info.build_git_sha`、provenance 与本次 source lock 一致，并验证本次变更所需 capability 及原会话恢复；运行中会话阻止切换时记录待完成项，不能声称已同步。流程 Environment Version 保持不可变，不因此改写既有 FlowRun 的冻结镜像。详见 `docs/deployment.md` 的独立 Agent Runtime 基线同步流程。
- FlowWeave 适配器只能消费这些扩展提供的正式 OpenHands 路由、类型和事件生命周期；不得用平台侧重算、完整历史或模型 usage 统计伪造当前 View 指标。

- SDK 源码：`/Users/zhengmengen/WorkSpace/openhands/software-agent-sdk-total-tokens-1.47`（`baseline`）
- 历史兼容基线：`v1.42.0` / `f09e03eac772290feeb51b7d7390ffaefeca1a09`
- 固定包版本：`openhands-agent-server==1.54.0`、`openhands-sdk==1.54.0`、`openhands-tools==1.54.0`、`openhands-workspace==1.54.0`
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
## Web 交互编码规范

- 产品页面中的筛选、配置和表单选择器禁止使用原生 `<select>`、`<option>` 或浏览器默认下拉界面；统一复用可访问的自定义选择组件（如 `LaunchOptionMenu`）。必须支持键盘选择、Escape 关闭、点击外部关闭，并通过 `ViewportPopover` 处理浏览器边界与滚动容器裁切。此规则同样适用于修改已有选择器。

## UI 设计与前端交互准则

- 一切布局以内容为视觉重心，使用户注意力聚焦于当前主体；整体布局需保持视觉平衡。
- 不使用大标题或冗余说明；能够在一行呈现的信息不得拆分为多个区域，从而避免占用主体空间。
- 所有弹窗、浮层和菜单除原有操作或关闭按钮外，均须支持点击外部区域和按 `Escape` 关闭。
- 每个 Tab 必须保有独立页面快照；切换至其他 Tab 后再返回时，应恢复离开时的页面状态，不得重置。
- 所有使用 `Command` 或 `Option` 的快捷键，必须同时提供 Windows 对应的 `Ctrl` 或 `Alt` 快捷键支持。
- 产品页面不得直接呈现浏览器默认样式的文本域、下拉框或选择框。原生 `<textarea>` 可作为稳定的内部编辑实现（尤其是会话输入、IME、多行编辑和无障碍场景），但必须通过 FlowWeave 自定义组件或样式提供一致的视觉、焦点、尺寸、快捷键和可达性交互；不得因形式上的替换而破坏已验证的编辑体验。筛选、配置和表单选择器仍禁止使用原生 `<select>`、`<option>` 或浏览器默认下拉界面。
- 设计新的功能或页面前，先参考仓库内已有设计；既有界面经过反复打磨，默认应继承本节的全部准则。


## Web 会话状态

- 工作区归档是对其所有版本下正式会话的批量归档；先校验完整批次可归档，再原子更新工作区和会话。恢复工作区必须二次确认并恢复其中会话；恢复单条会话须同时恢复所属工作区，但保留其他会话归档状态。归档成功后立即同步分页、置顶和活动列表，不能等待刷新或由旧分页缓存重新插入。
- 会话管理使用独立工作区投影：根组显示 Agent 工作区名称，自动化目录显示 Agent 定时任务名称，Case 控制目录显示场景名称；普通工作目录仍显示自身名称。普通 FlowRun 的 `FLOW_NODE` 会话不进入全局会话管理，继续随 FlowRun 记录管理；不得为纳入列表而绕过节点 Attempt 的生命周期与删除栅栏。

- 会话内搜索属于右侧悬浮信息卡的同一浮动容器，使用其显隐、宽度、工具面板挤压及全屏规则；搜索面板与概览面板视觉独立、间隔 10px，不得放回发送框区域或另建独立定位与显隐状态。
- 悬浮信息卡及其概览禁止横向、纵向滚动。子智能体头像摘要必须在卡片可用宽度内裁切，网格轨道及其行、按钮允许收缩；数量与详情箭头保持可见，不得由头像总宽度撑开卡片或内容。
- `AgentSessionWorkbench` 中会在异步发送、重写或流订阅回调内更新的本地事件和 UI 状态，必须携带并校验 `bindingId`；不能只依赖会话切换 effect 清空共享状态，否则旧会话的迟到回调会污染新会话。
- 会话未读状态是用户隔离的服务端 `AgentConversationBinding.unread` 投影；Agent Workspace 与 FlowRun node-session 两种宿主必须共同读写该字段。浏览器 `localStorage` 仅用于置顶等设备本地展示偏好，不能作为未读事实源。后台会话从运行转为终态时，必须将普通未读标记持久化到服务端，不能只更新浏览器内存状态。前端切换会话时先乐观更新，再异步持久化；写请求未完成期间必须让本地目标值覆盖列表刷新，并用请求代次忽略同会话较旧写响应，避免旧服务端快照造成未读样式回退。活动会话列表的单击仅预览，不得更新未读状态或改变列表归属；只有双击进入正式会话路由时才可标记已读。活动列表中未读项的右键菜单必须提供显式“标记为已读”，普通列表则保留“标记为未读”。
- 活动会话列表双击进入正式路由时，若目标普通会话在收起的工作区、默认可见数量之外或后续分页中，必须以临时 binding 定位状态依次展开所属分组、加载目标所在分页、扩大可见数量，并在目标行挂载后滚动和短暂高亮；不能只切换路由或恢复此前的侧栏滚动位置。置顶会话直接定位置顶区，无需展开工作区分组。

- 侧栏红色异常标识仅在 `unread_origin === 'SYSTEM'` 且服务端活动投影为 `possibly_stuck` 或 `failed` 时显示；后台会话进入这两种投影应自动写入系统未读。`possibly_stuck` 在 native running 状态显示心跳动画，`error`/`stuck` 终态显示静态标识；用户手动未读必须覆盖为普通蓝点。
- Composer 草稿的文本、附件、引用和注释必须作为带 `scope` 的同一快照读写；会话切换先持久化 outgoing scope，再恢复 incoming scope。子组件卸载 cleanup 不得从共享 ref 读取内容后写入捕获的旧 scope。未创建草稿切换后必须保留可发现的恢复入口；用户显式新建会话仍须创建全新的空 scope，仅页面自动进入或用户点击草稿入口时才允许恢复同工作区的未创建草稿。
- 会话运行中的视觉状态不能只依赖可能短暂抖动的 Runtime readiness；只要正式事件树仍存在未完成用户轮次且未超过终态同步期限，就必须保持会话活动和底部任务计划的 DOM、动画与布局稳定。
- 未创建会话的附件以草稿 UUID 为 owner 写入当前宿主的 `uploads/`；用户显式放弃草稿或移除附件时必须调用宿主级安全清理，且上传晚于放弃时由上传完成回调补偿。服务端只能删除 UUID owner 前缀、规范路径内的普通文件，并在同 UUID 已存在正式 `AgentConversationBinding` 时拒绝删除；每个草稿上传还必须登记延迟兜底回收，以覆盖页面关闭、断网和客户端清理失败。

- Token/事件上下文指标允许 Runtime 暂时返回未知；同一 binding 已有可信指标时应保留最近可信值，首次未知仍明确显示待更新，且不得跨 binding 复用。
- 会话交互态与视觉态必须分离：暂停、继续、发送等操作服从 Runtime readiness；运行中展示按正式事件单调推进，不能因中间轮询回退。首次打开会话且 Runtime 状态未知时只显示明确加载态，不得将历史事件缺口呈现为“正在思考”。自动贴底仅由新事件、历史锚点恢复或用户操作触发，禁止用整个会话树的 ResizeObserver 响应状态文案和动画尺寸变化；已到目标位置时不得重复写 `scrollTop`。
- 会话运行中的全局 Esc 暂停必须复用既有 interrupt mutation，并采用有界的两次按键确认；首次按键仅在暂停按钮显示 `Esc`，确认超时后复原。弹层的捕获阶段 Esc 关闭逻辑优先，工作台快捷键不得越过已 `preventDefault` 的事件。
- 会话首屏 hydration 只读取 OpenHands 最新事件窗口及同批 context/readiness；更早历史由浏览器低优先级分页恢复，不能为了首屏串行读取完整分支。
- API 侧 hydration 缓存必须按用户、宿主、binding、Runtime Session/generation 和 OpenHands Conversation 隔离。不可变正式事件按 event id、分支快照按 leaf 缓存；继续执行只先失效 current/terminal 指针并递增 epoch，保留旧 branch/event。旧 epoch 的迟到请求不得恢复 current；运行中 hydration 只允许 single-flight，不得成为长期 current 缓存。
- 首次进入、刷新后或可信租约过期的既有会话必须重新取得该 binding 的正式 hydration，并在结果返回前只显示“正在加载会话”；React Query、浏览器持久快照或不完整事件不得抢先渲染，也不得参与“正在思考”判断。当前标签页内刚完成 hydration 且未失效的终态会话可短期直接复用完整快照；运行中或最新状态失效的会话切回时保留已确认历史缓存，以 OpenHands 正式 cursor 增量 hydration 取得同批事件、readiness 和 context，全部合并完成后一次性渲染，再恢复 WebSocket；不得先展示旧历史后跳变追加最新事件。游标不属于当前分支时回退完整正式 hydration，并移除旧分支后代；首次打开或刷新后的浏览器持久快照不得用于增量事实。快速切换时必须合并短时间内的选择，并将 hydration 限制为单飞、完成后只追赶最后选择的 binding；宿主卸载时用 AbortSignal 取消浏览器请求，不能让每次点击都并发占用交互读取池。
- 后台会话仅在活动投影确认“刚从运行转为终态、普通 `MANUAL` 未读且有正式 assistant 最终回复”后，才可静默预热其正式 hydration。预热必须低优先级、单并发、可在宿主卸载时通过 `AbortSignal` 取消；其完整 events/readiness/context 可进入当前标签页可信终态快照，以便用户点击时复用。不得预热 `SYSTEM`/手工未读、运行中、暂停或异常会话，且前台 hydration 始终优先。
- 普通后台完成未读可持久化该 completion event id 对应的正式 assistant `MESSAGE` 最小预览；点击时仅可按相同 event id 立即渲染该预览，且输入/控制必须保持 hydration 同步门控。正式 hydration 返回后必须以其事件树为准并移除预览；用户标记已读/未读、继续会话或出现非普通未读时不得保留或展示该预览。
- 浏览器从后台恢复可见或窗口重新获得焦点时，活动会话必须立即从无 cursor 的最新 OpenHands 事件窗口对账，并刷新会话与 readiness 投影；不能仅等待受后台节流的定时轮询或 WebSocket 重连。`visibilitychange` 与 `focus` 可能连续触发，应合并同一轮恢复。
- 运行中 REST 事件恢复必须由单一协调器串行调度：有 `next_cursor` 时优先增量追赶，定期或在 `message_complete`、断流、WebSocket 重连、前台恢复时读取无 cursor 最新窗口；不得让 React Query 定时器与自建定时器并行轮询同一会话。强制最新窗口信号发生在增量请求期间时必须排队补读，不能被 in-flight 去重吞掉。
- 历史分页完成后必须记住已耗尽的入口 `history_cursor`，避免最新窗口刷新重新激活同一分页链；若服务端返回新的入口游标，仍必须允许读取新增历史。
- 最终回复正文只从 OpenHands 正式 `MESSAGE` 事件一次性渲染；浏览器不得展示 StreamContext 文本 delta 或模拟打字光标。`message_complete` 与断流仍触发正式事件补读；但当前页面已订阅的 binding 收到 `message_complete` 时，必须立即结束该 binding 的本地视觉运行态（发送框暂停按钮、侧栏当前行圆环），直到下一次发送或切换会话。该覆盖不得持久化或影响后台会话，后台会话仍以 Runtime `input-readiness`／活动投影判断。Tool、Thought、Task 等正式过程事件仍可实时追加展示。
- 当前页面发送的用户消息使用浏览器稳定 `renderKey` 和正式 OpenHands `event.id` 双身份：正式事件只认领并补全已有本地气泡，不能创建第二个用户气泡；刷新后直接按正式历史渲染。认领必须依赖提交 ID 与正式事件 ID，不得按正文或时间相似度猜测。
- Runtime readiness 一旦确认终态，输入框、按钮和侧栏运行样式必须立即恢复；正式终态事件的补读只能在后台进行，不能呈现“正在对账”或继续占用运行态。为避免上一轮排队消息误发，可设置短时且不可见的队列门控，但必须有界并保留用户确认权。
- 会话配置仅管理能力与认证；新会话和既有会话的模型、供应商及推理程度都在发送框中选择。
- 已创建且可写、处于 idle 或 paused 的会话必须在 `/` 菜单提供“压缩上下文”；该操作只调用 OpenHands 原生 condense 控制接口，不得发送用户消息或创建乐观消息气泡。点击后的本地 pending 与持久任务投影仅用于立即反馈和刷新恢复，不能伪造普通 `execution_status=RUNNING`；最终历史展示与完成收口只认正式 `CONDENSATION_REQUESTED` / `CONDENSATION_COMPLETED` 事件。压缩期间输入保持可编辑，所有发送入口统一排入浏览器队列，禁止直接发送、重复压缩、展示暂停按钮或调用 interrupt。
- 会话中的附件、工作区文件链接、候选输出文件和生成图片统一先在页面中央预览；工作区资源从预览弹窗显式跳转文件栏，不应在首次点击时直接展开侧栏。
- 逐步节点启动的 `confirm-start` 会先原子预留 FlowNode `AgentConversationBinding`；Attempt 详情必须投影该 `binding_id`，前端收到成功响应后应以记录自身的 FlowRun ID、NodeRun ID、Attempt ID 和此 binding 打开节点会话，并同步失效逐步记录列表。不得通过猜测或等待 Runtime 的原生 conversation ID 来构造页面路由。
- 连续运行草稿保存使用乐观锁 `expected_row_version`。前端遇到 `VERSION_CONFLICT` 时可仅对同一未启动草稿读取最新详情后，以用户当前编辑的严格写入载荷重试一次；不得回显详情中的冻结审计字段，也不得吞掉其他错误或无限重试。
- 普通消息 POST 成功与 OpenHands 正式用户事件进入浏览器事件窗口之间存在短暂竞态；在当前 binding 收到服务端返回的 `cursor` 对应正式事件前，`AgentSessionWorkbench` 必须保留“正在提交消息”状态并抑制陈旧 `monitoring.possibly_stuck`，确认后立即恢复真实监控显示。该确认门控必须有界，并在失败、切换 binding 或超时后清除。
- 临时侧边聊天必须由服务端 `AgentSidebarConversation` 以主会话 binding 关联，并在一小时 TTL 到期后由后台任务删除 OpenHands 原生会话、绑定私有记录和附件；浏览器已取得的事件可继续阅读，但后续发送必须以 `AGENT_SIDEBAR_CONVERSATION_EXPIRED` 拒绝。临时 binding 不得出现在普通会话列表、活动列表或搜索投影中；侧栏首条消息可携带主会话的正式事件引用，主会话元数据只作为受限系统上下文。
- 认证管理、大模型管理，以及 FlowRun 的配置、运行记录和运行时资源按 FlowWeave 用户隔离；流程定义、节点能力和目录保持共享。历史认证、模型提供商和 FlowRun 记录迁移给 `FLOWWEAVE_USER_ID`；流程执行仅可解析当前用户可见的认证和模型提供商，禁止共享流程复用其他用户凭据。
- 会话列表的整条会话内容区是统一的原生拖拽源：拖入发送框时添加会话引用，拖到同一工作区的其他会话位置时调整顺序；不得使用独立排序把手或拆分拖拽命中区。
- 账户菜单打开的设置页面由 `App.tsx` 的会话级 `openSettingsViews` 管理；每个设置视图至多一个临时标签，关闭当前标签时优先选择右侧、否则左侧相邻标签，关闭最后一个则回到节点资产。仅当 `.context-tabs` 存在时显示固定导航与临时标签间的分隔线；其布局需保留标题收缩、图标模式与横向滚动能力。
- OpenHands interruption has a distinct formal `STATE` / `InterruptEvent` emitted after the native conversation transitions to `PAUSED`; synthetic `AgentErrorEvent` records only repair unmatched tool calls. In `AgentSessionWorkbench`, an `InterruptEvent` descended from the active user turn must take precedence over residual tool activity when resolving a native `paused` readiness state. Do not treat arbitrary `AgentErrorEvent` values as terminal because they are recoverable.
- 定时任务行的详情选择与运行会话展开相互独立：点击任务内容只选择并显示详情，只有箭头按钮可切换展开／收起。收起不能清空选中项或隐藏详情，箭头不能切换详情／路由；保留统一行样式，不增加视觉分区。任务行不提供“…”菜单，暂停、立即运行和编辑统一通过详情页操作。直接进入运行会话可首次展开定位所属任务，但后续轮询不能覆盖用户的手动收起。
- 在嵌入式会话定时任务页，打开运行会话后点击任务块必须显式返回 `/agent/schedules` 并保留面板选择状态；会话路由存在时主工作台会隐藏任务详情，不能只更新任务面板内部的 `selectedScheduleId`。
- 嵌入式定时会话路由也必须保留与主页会话列表一致的底部“会话配置”入口，并将当前 hydration 后的正式 capabilities 传入；每个定时任务下的运行会话默认只展示前三项，其余由该任务自身的展开控件显示。
- Git 提交 Diff 与工作区文件 Diff 共用 `DiffChangeNavigation` 的 `scrollIntoView({ block: 'center' })` 导航机制。每种统一/并排渲染模式都必须保留 `.agent-diff-scroll-end-spacer`（高度为可视区的 50%，且至少 120px），否则最后一个变更没有足够滚动范围进入视觉中心。Git Diff 有意只渲染补丁 hunk 中的新增、删除和有限上下文；文件末尾未改动的行应通过“查看源文件”查看，而不是补进 Diff。

## Case 控制面

- 新建 Case 协调会话前，必须具有完整可复用的阶段路径和已保存的 Agent 配置；前端提前禁用启动，后端在创建会话／写入运行时前重新校验模板、模型、能力与认证，拒绝过期配置。已有协调会话仍可直接打开。操作记录包含平台自动推进、手动动作和会话控制请求，仅作为次要排查入口，不得统称为 Agent 会话消息。

- 场景名称、工作区、说明和目标创建后固定，浏览器只读与服务端写入校验共同约束；创建表单不提供资料上传，创建成功后仍可管理资料。

- `cases`、`case_stages` 和 `case_agent_tasks` 是用户隔离的控制面元数据；迁移必须同步加入 PostgreSQL RLS 策略及 `shared/database.py` 的 `_USER_ISOLATED_TABLES`。
- Case 控制会话参照定时 Agent 会话：复用用户级 Agent Workspace Runtime/容器，但为每个 Case 在当前用户项目根创建 `.flowweave/cases/<case_id>` 工作目录，并将 OpenHands 原生会话 Binding 与该目录版本冻结关联。不得使用平台私有对话执行器。
- `case_flow_runs`、`case_decision_proposals` 和 `case_control_plans` 同样属于用户隔离控制面，必须维护 RLS 与 `_USER_ISOLATED_TABLES`。Case 决策按当前产品要求自主执行，但只可走白名单动作与既有编排服务，且只能引用活动 `CaseStage.current_execution_flow_run_id`，绝不可作用于模板、历史副本、其他阶段或仅因关联而可见的 FlowRun：逐步运行可继续、停止、复制节点重跑或为未启动部分应用 Revision；连续运行只能复制为独立自动运行草稿，绝不能修改已启动自动运行。所有决策必须持久记录输入、执行结果和失败码；不得把 Case Agent 的文本输出当作可执行私有控制协议。
- Case 阶段的 `CaseStage.flow_run_id` 是已完整配置的运行模板，Case 永不直接启动它；`current_execution_flow_run_id` 指向由模板复制的当前独立执行记录。0163 之前的直接阶段绑定迁移时必须移到 `current_execution_flow_run_id` 并清空模板字段，绝不可把历史执行误作可复用模板。逐步模板必须是子逐步记录，且从起点可达的所有节点都已有完整草稿；复制保留全图草稿，但下游实际启动仍须等待所有直接前驱接受。逐步节点重跑必须创建新的 NodeRun/Attempt 和新的原生会话 Binding，但重用同一 FlowRun 节点工作目录；旧 Binding、会话事件、工作区内容和执行记录必须保留，绝不可覆盖或删除。
- 逐步运行的前驱节点重跑窗口只在其所有直接后继仍是未物化的待启动草稿时开放；后继一旦已有 NodeRun 即不可回退。工作台通过双击符合条件的已接受前驱节点重新进入编辑，保存草稿后左侧记录按钮必须显示“重跑”；实际重跑继续创建新的 NodeRun/Attempt/Binding，保留原会话和工作区历史。

## FlowWeave 会话网关重连

- OpenHands 对同一会话的原生继续入口是 `POST /api/conversations/{conversation_id}/run`；`OpenHandsRuntime.run()` 必须只允许 `idle`、`paused` 和经正式可恢复错误分类的 `error` 状态，绝不能重启 `finished` 会话。
- FlowWeave 仅对正式 `ConversationErrorEvent` 中 `classification.kind === "transient"` 且 `retryable === true` 的网关/网络错误自动重连：包括模型网关 `502/503/504`、连接中断、超时和空响应；`quota`（额度耗尽）、`rate_limit`、认证、配置、上下文和内容策略错误一律不自动重连。总尝试次数是 3，包含收到的首次失败，因此最多调用原生 `/run` 2 次；每次继续后必须等待新的非终态正式事件，不能仅以 HTTP 接受响应判定成功。
- OpenHands 单次模型请求的 `num_retries` 同样是总尝试次数，当前固定为 3，包含第一次请求；流式模型重试进度上限也必须保持为 `3/3`。
- 工作台重连时维持原会话与事件树，暂时抑制该可恢复错误的终态渲染，并在思考区和发送框显示“正在尝试重连（n/3）”；收到正式进展事件后恢复常规渲染，耗尽后才显示原始终态错误。
