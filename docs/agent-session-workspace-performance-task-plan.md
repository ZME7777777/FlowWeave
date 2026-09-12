# Agent 会话与工作区加载性能任务过程表

> 状态：`PERF-06 DONE`
> 创建日期：2026-09-12
> 范围：Agent Workspace 与 FlowRun 节点会话的浏览器加载、工作区浏览、会话恢复和运行态读取。
> OpenHands 事实基线：`30cf5832e42c71c24daa82a1a4fd5d25eb70d1b9`（四包 `1.47.0`）。
> 前置记录：`FR-209`--`FR-220` 与 `FR-314` 已完成的资源保护和有界 EventLog 读取仍然有效；本任务只改变在不损害产品语义的前提下可延迟、可按需或可复用的读取与渲染。

## 1. 已冻结的产品与架构约束

1. OpenHands 是 Conversation、完整 EventLog、正式 `id`、`parent_id`、`action_id`、`tool_call_id`、HEAD、Fork、navigate、输入状态和运行状态的唯一事实源。FlowWeave 不建立消息、事件、HEAD 或 cursor 的第二持久化事实源。
2. 会话首次 hydration 必须由后端从 OpenHands 聚合并返回完整 active branch；不得以最近 100 条事件或浏览器局部窗口充当会话逻辑真相。完整数据必须足以构建用户消息索引、继续发送、重写和 Fork 所需的关系。
3. 浏览器缓存只用于展示和读取加速。缓存未命中、淘汰、刷新、换浏览器或切换账号之外的任何浏览器状态，都不得成为后端拒绝发送、Fork、重写、暂停或恢复的理由。
4. 若用户界面呈现一个正式 `event_id`，Fork 或重写必须以 `conversation_id + event_id` 向 OpenHands 正式 event-by-id 路径重新验证；后端不得只在首屏、最近窗口、浏览器缓存或 FlowWeave 临时投影中查找该 ID。
5. 首屏快照在当前浏览器标签页与当前登录身份中持续保留；完整历史逻辑缓存对 inactive 会话采用最近 5 个会话且 5 分钟 TTL 的 LRU 淘汰。超长 stdout/stderr、diff、图片、文件正文和终端回放不作为长期历史缓存内容。
6. 未选中会话的运行圆形动态状态、运行结束后的蓝色未读标记、当前会话 WebSocket 断流后的正式 events 回读、暂停/恢复/排队发送、上下文用量和压缩提示均为不得回归的产品能力。
7. 工作区路径、绑定工作目录、附件、用户隔离和符号链接的服务端授权校验不能因目录懒加载、缓存或批量读取被下放到浏览器或弱化。
8. 不修改 OpenHands 源码、协议或 EventLog 存储格式。所有 Runtime 调用必须使用固定版本的正式 API。

## 2. 现状与本轮边界

当前页面在会话选择、工作区详情和历史事件恢复中存在可避免的重复 I/O：工作区详情递归扫描文件树并同步发现 Git 仓库；已选会话可请求两份等价工作区详情；会话 REST 读取会重复获取 Conversation state/context；当前事件窗口和 `history_cursor` 路径为了资源保护按页补齐。

本任务不把“按页补齐”简单改为“只显示最近 100 条”。目标是将完整 active branch 的后端 hydration 与浏览器的首屏快照、历史逻辑缓存、重型内容渲染拆开：正确性完整，展示成本按需。

## 3. 任务顺序

| 切片 | 依赖 | 状态 | 目标与交付 | 最小验收与提交边界 |
|---|---|---|---|---|
| `PERF-00` | 无 | `DONE` | 冻结本过程表、不可退让约束、切片依赖和最终验收。 | 文档审阅、任务状态唯一性、`git diff --check`；仅提交本计划。 |
| `PERF-01` | `PERF-00` | `DONE` | 为工作区扫描、Git 探测、会话 hydration、OpenHands state/events/event-by-id 和前端加载阶段建立低基数指标与结构化耗时日志。 | 已记录固定 operation/outcome 的耗时直方图与条目计数；无会话/用户/正文标签，不改变功能路径。 |
| `PERF-02` | `PERF-01` | `DONE` | 新增受服务端范围校验的目录列表 API：根目录/展开目录按 `parent_path + cursor + limit` 返回直接子项；保留现有文件下载与写入授权。 | Agent Workspace 与 FlowRun 节点入口均已提供直接子项分页读取；路径、范围、符号链接与工作目录校验仍在服务端。 |
| `PERF-03` | `PERF-02` | `DONE` | Web 文件树按目录懒加载、局部缓存失效和视口绘制优化；已选会话复用唯一轻量工作区详情，阅读会话时不预加载文件树。目录首次和展开时均只取一页，用户明确触发“加载更多”才继续读取。 | 平台目录授权与轻量详情测试、Web typecheck/lint/build 已通过；浏览器定向回归留待 `PERF-10`。 |
| `PERF-04` | `PERF-02` | `DONE` | Git 仓库发现、branch/HEAD/remote 已从工作区首屏和完整文件索引拆出；仅用户打开 Git 历史时按范围授权发现，随后才读取 log、commit、diff。仓库列表采用 15 秒受控短 TTL。 | 平台定向测试、Web typecheck/lint/build 已通过；浏览器定向回归留待 `PERF-10`。 |
| `PERF-05` | `PERF-01` | `DONE` | 会话首次 hydration 改由服务端沿 OpenHands 正式 `history_cursor` 聚合完整 active branch，并在一次响应中返回 events、context/usage 与 readiness。浏览器不再循环历史页；运行中的 WebSocket 断流仍使用有界 cursor 增量对账。 | 平台定向聚合测试、Web typecheck/lint/build 已通过；浏览器定向回归留待 `PERF-10`。 |
| `PERF-06` | `PERF-05` | `DONE` | 前端首屏快照持久缓存、完整历史逻辑缓存（5 个 inactive 会话 + 5 分钟 LRU/TTL）、HEAD 轻量校验和内存上限；重型内容延迟读取。 | 定向浏览器回归：来回切换秒开、TTL/LRU 淘汰、刷新后的完整恢复和旧缓存覆盖。 |
| `PERF-07` | `PERF-05` | `READY` | 固化 Fork、旧用户消息重写和普通发送的 event-by-id / 正式状态验证路径，补足目标不在首屏或浏览器历史缓存时的回归。 | API/Runtime 回归：缓存淘汰、刷新、另一浏览器和局部窗口无目标事件时仍可操作；OpenHands 真缺失才明确失败。 |
| `PERF-08` | `PERF-05` | `BLOCKED_BY_DEPENDENCY` | 消除安全可证明的重复 Runtime 调用：hydration 共用一次正式 state；保留独立的运行中 readiness、断流 events reconcile 和列表 summary 读取。 | 调用计数和行为回归：圆形运行态、蓝色未读、WebSocket 断流恢复、暂停/恢复/队列均保持。 |
| `PERF-09` | `PERF-03`, `PERF-06` | `BLOCKED_BY_DEPENDENCY` | 对会话消息、工具详情、Markdown/高亮、stdout/stderr、diff、预览采用视口虚拟化、默认折叠和按需解析/读取。 | 大会话浏览器回归：完整逻辑索引不丢失，首屏/滚动/展开不长任务阻塞。 |
| `PERF-10` | `PERF-01`--`PERF-09` | `BLOCKED_BY_DEPENDENCY` | 完整性能、功能、契约、迁移、Web E2E 和真实 Runtime 验收；从已提交 commit 做全量远端构建、更新平台/Web/stream-api 并验证。 | 见第 5 节；部署前必须运行仓库远端预检，且只从提交 archive 构建。 |

状态规则：同时只能有一个活跃切片；完成切片改为 `DONE` 并解锁紧随其后的依赖切片。每个切片必须单独 Git commit，提交成功后停止，不混入下一切片。

## 4. 调用与缓存模型

~~~text
首次进入或刷新会话
  Browser ── complete hydration ──> FlowWeave ── official OpenHands reads ──> active branch/state
  Browser <── complete logical conversation ── FlowWeave

再次切换到同一会话
  Browser ── immediate shell snapshot ──> render
  Browser ── HEAD/state validation ──> FlowWeave ──> OpenHands
      unchanged: reuse 5-minute/LRU logical cache
      changed:    replace with formal increment or complete hydration

Fork / rewrite old event
  Browser ── conversation locator + event_id ──> FlowWeave
  FlowWeave ── GET event-by-id ──> OpenHands
  FlowWeave ── official fork/navigate/message ──> OpenHands
~~~

首屏快照永远不能被用作后端授权或事件存在性证据。完整 hydration 或正式增量的响应到达后，必须以 OpenHands 返回的正式 HEAD 与事件关系覆盖旧快照。

## 5. PERF-10 最终验证与远端发布

本地最终门禁至少覆盖：

- 平台定向及完整 pytest、Ruff、Pyright、迁移 head/upgrade/downgrade、OpenAPI/架构边界与 `git diff --check`；
- Web ESLint、TypeScript typecheck、production build 与覆盖第 3 节所列缓存、Fork/重写、运行态、工作区和 Git 场景的 Playwright；
- 固定 OpenHands `1.47.0` contract/event-by-id/长 EventLog/Runtime replacement smoke；
- 性能基线对比：首屏、目录展开、会话 hydration、切换命中、Git 首次打开和断流恢复的耗时、请求数与响应体积不得相对 `PERF-01` 基线无解释回退。

全部切片完成且代码提交后，远端发布只能按仓库 `AGENTS.md` 的 `192.168.91.154` 基线执行：

1. 完整读取 `docs/local-build-and-deploy.md`，确认目标 `root@192.168.91.154`、根目录 `/opt/flowweave`、发布范围和已提交 commit；
2. 先运行 `scripts/verify-remote-deploy-154.sh --commit <SHA> --scope platform` 及 Web 所需的预检，不以本地未提交工作树打包；
3. 从最终 commit 生成不可变 `git archive`，在远端按该 archive 全量构建 linux/amd64 平台、Web 和实际 stream-api 部署入口所需镜像；
4. 保留远端 `.env`、`deploy/compose.yaml`、named volumes 和 `/opt/flowweave/data/workspaces`；不得 `down -v`、`--remove-orphans` 或覆盖服务器 Compose；
5. 平台更新先执行 migration，再从同一 commit recreate `runtime-provider`、`api`、`worker` 和 `stream-api`；Web 单独 recreate；
6. 验证容器健康、迁移退出、前缀 API/静态资源、Agent 深层路由、工作区 API、真实浏览器 Network 前缀、FastGPT 根登录页，以及受影响的完整会话/工作区闭环。

## 6. 过程记录

| 日期 | 切片 | 验证 | 结果 |
|---|---|---|---|
| 2026-09-12 | `PERF-00` | 文档状态唯一性、`services/platform` 工作目录下 `.venv/bin/alembic heads`、`git diff --check` | PASS：冻结完整 hydration、首屏/历史缓存、event-by-id、懒加载和最终全量远端发布的切片边界；仅 `PERF-01` 解锁。 |
| 2026-09-12 | `PERF-01` | 平台 Ruff/py_compile、无 `conftest` 的指标单测、Web typecheck/lint、`git diff --check` | PASS：工作区树/Git、会话 events/context、OpenHands state/events/event-by-id 及浏览器阶段均有低基数耗时观测；`PERF-02` 解锁。完整 pytest 因本机 Docker daemon 不可用而无法启动 Testcontainers；Pyright 当前环境错误解析全局 Python 3.14 标准库，未作为本切片代码失败。 |
| 2026-09-12 | `PERF-02` | 平台 Ruff/py_compile、无 `conftest` 的目录授权与 cursor 分页单测、`git diff --check` | PASS：两类会话入口均可按目录直接子项分页读取，隐藏文件、符号链接和范围外路径不暴露；旧 `/workspace` 全量响应暂仅为兼容保留，待 `PERF-03` 前端切换后移出首屏路径。 |
| 2026-09-12 | `PERF-03` | 平台 Ruff/py_compile、无 `conftest` 的目录授权和轻量详情测试（4 passed）、Web typecheck/lint/production build、`git diff --check` | PASS：默认工作区详情不再递归扫描文件树或发现 Git 仓库；文件页签打开后才读取根目录，目录展开只读取直接子项的一页，继续分页须显式点击“加载更多”。创建/删除会清空局部目录缓存并重新授权读取；已加载行保留并采用 `content-visibility`，不牺牲多选、展开或粘性目录路径。Vite 提示主 bundle gzip 480.85KB，未阻断构建，留给 `PERF-09` 的渲染/分包审计。 |
| 2026-09-12 | `PERF-04` | 平台 Ruff/py_compile、无 `conftest` 的 Git 拆分与目录授权测试（6 passed）、Web typecheck/lint/production build、`git diff --check` | PASS：普通工作区详情和 `full_index=true` 的文件引用路径均不会运行 Git 仓库扫描或 branch/HEAD/remote 子进程。两类会话入口仅在用户明确打开 Git 历史后调用受授权的 repositories API；选定仓库后才读取 log、commit 与 diff，列表 TTL 为 15 秒。Git 根仓库与多个授权仓库均可选择。Vite 提示主 bundle gzip 481.36KB，未阻断构建，继续留给 `PERF-09`。 |
| 2026-09-12 | `PERF-05` | 平台 Ruff/py_compile、无 `conftest` 的完整 active-branch 聚合测试（2 passed）、Web typecheck/lint/production build、`git diff --check` | PASS：两类会话入口新增 hydration 路由；服务端以正式 `history_cursor` 与 event identity 读取所有 active-branch 页面，保持最新 HEAD/usage，若 HEAD 漂移、事件冲突或 cursor 循环则 fail closed。不存在“最近 100 条”或 FlowWeave 自定事件数量上限。浏览器首开只调用 hydration 并填充 events/context/readiness 缓存；历史分页循环已移除。运行中仍保留 readiness 读与 WebSocket 断流 cursor 对账，压缩轮询合并最新页而不覆盖完整历史。Vite 主 bundle gzip 481.25KB 警告未阻断构建，留给 `PERF-09`。 |
| 2026-09-12 | `PERF-06` | 平台 Ruff/py_compile、无 `conftest` 的 hydration/HEAD 测试（4 passed）、Web typecheck/lint/production build、缓存 Playwright（1 passed）、`git diff --check` | PASS：当前标签页且当前登录身份下，首屏仅保留最多 16 条安全展示事件的 sessionStorage shell；工具结果、stdout/stderr、diff、文件正文与图片等重内容不持久化。完整 active branch 仅在 React Query 内存中保留，非当前会话最多 5 个、TTL 5 分钟并以 LRU 淘汰。首次或刷新后的会话仍走完整 hydration；切回仍在 LRU 的会话只读取 OpenHands 正式 HEAD，未变则复用、变化则完整替换。登录身份变化/登出清除 shell；浏览器缓存从不作为发送、Fork 或重写的授权或 event id 依据。Vite 主 bundle gzip 482.78KB 警告未阻断构建，留给 `PERF-09`。 |
