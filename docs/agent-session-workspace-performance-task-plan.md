# Agent 会话与工作区加载性能任务过程表

> 状态：`IMPLEMENTING`
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
| `PERF-01` | `PERF-00` | `READY` | 为工作区扫描、Git 探测、会话 hydration、OpenHands state/events/event-by-id 和前端加载阶段建立低基数指标与结构化耗时日志。 | 定向单测/静态检查证明指标无会话/用户/正文标签；不改变功能路径。 |
| `PERF-02` | `PERF-01` | `BLOCKED_BY_DEPENDENCY` | 新增受服务端范围校验的目录列表 API：根目录/展开目录按 `parent_path + cursor + limit` 返回直接子项；保留现有文件下载与写入授权。 | API/授权/分页测试，证明没有递归全树扫描作为首屏读取。 |
| `PERF-03` | `PERF-02` | `BLOCKED_BY_DEPENDENCY` | Web 文件树按目录懒加载、局部缓存失效和虚拟化；已选会话复用唯一工作区详情，阅读会话时不预加载文件树。 | 定向浏览器回归：展开、上传、删除、Agent 写入后的局部刷新，以及现有工作目录/附件范围保护。 |
| `PERF-04` | `PERF-02` | `BLOCKED_BY_DEPENDENCY` | 将 Git 仓库发现、branch/HEAD/remote、log、commit 和 diff 从工作区首屏拆为 Git 面板按需读取，并设置受控短 TTL。 | 定向 API/Web 回归：未打开 Git 不运行 Git 扫描；打开面板后仍只读且范围正确。 |
| `PERF-05` | `PERF-01` | `BLOCKED_BY_DEPENDENCY` | 实现完整会话 hydration：服务端聚合完整 active branch、正式 state、readiness、context/usage，并以一次浏览器响应提供完整逻辑事实。 | 长会话、工具密集会话、Fork/重写/继续发送的服务端回归；不得把 100 条窗口作为逻辑上限。 |
| `PERF-06` | `PERF-05` | `BLOCKED_BY_DEPENDENCY` | 前端首屏快照持久缓存、完整历史逻辑缓存（5 个 inactive 会话 + 5 分钟 LRU/TTL）、HEAD 轻量校验和内存上限；重型内容延迟读取。 | 定向浏览器回归：来回切换秒开、TTL/LRU 淘汰、刷新后的完整恢复和旧缓存覆盖。 |
| `PERF-07` | `PERF-05` | `BLOCKED_BY_DEPENDENCY` | 固化 Fork、旧用户消息重写和普通发送的 event-by-id / 正式状态验证路径，补足目标不在首屏或浏览器历史缓存时的回归。 | API/Runtime 回归：缓存淘汰、刷新、另一浏览器和局部窗口无目标事件时仍可操作；OpenHands 真缺失才明确失败。 |
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
