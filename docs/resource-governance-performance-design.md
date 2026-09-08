# 资源治理与会话性能优化设计

> 状态：`FR-209 DONE`
> 日期：2026-09-08
> 范围：Agent Workspace、FlowRun 节点会话、stream-api、Runtime Provider、Worker、PostgreSQL 与控制面 Compose。

## 1. 目标与不变边界

本设计处理会话容器偶发无响应、会话/内容列表 3--5 秒加载、连接与进程泄漏风险。目标是让所有长期资源都具备
**所有者、上限、空闲回收、取消传播、指标和告警**。

- 保留一条 FlowRun 对应一个隔离 Runtime 容器；不得以共享容器池削弱 Run 隔离，也不修改 OpenHands 源码。
- OpenHands 仍是 Conversation、事件、cursor、HEAD 与运行状态唯一事实源；FlowWeave 不建立事件或消息副本。
- 所有容量数值均为首轮生产保护阈值，须经 FR-219 指标基线和 FR-220 压测复核后再调大，不能因压力直接取消上限。

## 2. 设计决策

| 问题 | 最终方案 | 资源边界 |
| --- | --- | --- |
| 会话 WebSocket | 仅在一轮消息实际发送后为该会话建立流；历史列表和仅浏览会话不建流。回合结束后，对仍有订阅者的会话保留 5 分钟空闲宽限，到期关闭。 | 每会话/通道一个共享上游，订阅者、队列和空闲 Hub 均有上限；浏览器断开立即减少引用。 |
| 1.5 秒全列表轮询 | 删除每个侧栏会话的轮询。仅当前会话在运行时做低频 readiness 兜底；列表状态来自列表快照、当前流和显式刷新。 | 单页面最多一个 readiness 兜底定时器，采用退避与页面不可见暂停。 |
| 会话列表慢与 N+1 | 服务端 cursor 分页、批量加载目录/能力/最近活动摘要；客户端每工作区首屏 5 条，“展开”每次追加 5 条，折叠后重置为 5。 | API 固定 `limit <= 5`，稳定 cursor、索引和批量查询；禁止逐行附加查询。 |
| OpenHands 事件读取 | 首屏只读当前活动分支的最近窗口；更早历史由用户显式翻页，实时更新只从客户端持有的官方 cursor 增量读取。 | 固定 OpenHands 1.44 已提供正式 `TIMESTAMP_DESC`、`page_id` 和 `next_page_id`，但没有 HEAD 分支反向迭代器；适配层以 HEAD/父事件身份在单页中恢复窗口，并把缺失父事件原样返回为只读 `history_cursor`。禁止每次请求从 cursor 0 扫到末尾，绝不新增 FlowWeave 事件库。 |
| Runtime Relay | 保持一 FlowRun 一容器。Provider 新建按 `{runtime_session,generation,conversation,channel}` 键控的 Relay Hub，多浏览器订阅共享一个 `docker exec` 上游。 | 上游、订阅者和队列均为有界；5 分钟空闲 grace 后终止；generation 变化、所有者失效和取消均强制清理。 |
| FlowRun SSE 与 PostgreSQL | 不再一 SSE 客户端一 `LISTEN` 连接。每个 stream-api 进程仅保留一个受监控 LISTEN 连接，进程内向有界订阅队列扇出；跨进程扩展前先由专用 stream-api 角色/分片保证订阅覆盖。 | 慢消费者丢弃并要求用 cursor 重连，不能反压 PostgreSQL；LISTEN 连接计入数据库预算。 |
| tmux | 记录 terminal session 的最后活动时间；默认空闲 30 分钟清理，绝对最长 8 小时；仍有受权附着连接时不清理。 | 关闭 tmux、PTY、WebSocket 和关联临时文件；不可跨 owner 清理。 |
| Worker | `worker_concurrency` 驱动有界执行 lanes，而不是只读取配置。同步阻塞任务放入专用 executor；按任务类别设置更低并发。 | 全局并发、每类并发、队列等待和取消都可观测；任务不可无限创建线程。 |
| HTTP | 每个进程拥有显式生命周期的共享 sync/async HTTP transport，配置连接上限、keepalive、pool timeout 和关闭钩子。 | 禁止每请求创建 client；外部 OpenHands/Docker 控制调用保留独立小池，避免耗尽普通 API 池。 |

## 3. 初始容量与数据库预算

下列是单个控制面部署的保守起点，不是硬件无关的永久值。部署前必须以 PostgreSQL `max_connections` 和实际 CPU/
内存复算；总应用连接数（含 stream listener）必须不超过可用连接预算的 70%。

| 服务 | CPU | 内存 | PID | 数据库/并发起点 |
| --- | ---: | ---: | ---: | --- |
| api | 2 | 2 GiB | 256 | 每进程 DB pool 8、overflow 0；4 worker 时最多 32 |
| stream-api | 2 | 1.5 GiB | 256 | DB pool 4、overflow 0；另有 1 条 LISTEN/进程 |
| worker | 2 | 2 GiB | 256 | DB pool 8、overflow 0；默认任务并发 4 |
| runtime-provider | 1 | 1 GiB | 256 | 不持有业务 DB pool；Relay Hub 有独立上限 |
| web | 0.5 | 256 MiB | 64 | 无数据库连接 |
| migration | 1 | 1 GiB | 128 | 单次短生命周期连接 |

以 PostgreSQL `max_connections=120` 为例：预留 24 给超级用户、迁移与故障处置；常驻应用（含所有 API/stream-api/
worker pool 及 LISTEN）预算最多 72；其余仅在通过 PgBouncer transaction pooling 的短事务使用。`LISTEN/NOTIFY`
连接必须保持会话语义，不可放入 transaction pooling；它们单独计数和告警。

Runtime 默认下调为 **1 CPU / 1.5 GiB / 256 PID**；已发布 Environment 可声明经审计的更高资源档。所有控制面
容器和 Runtime 均启用 Docker init、健康检查和 OOM/PID 指标。

## 4. 指标、限流与验收

Prometheus 指标至少包括：HTTP 路由耗时/状态、数据库 pool 使用与等待、慢查询、HTTP transport 连接、SSE/
WebSocket 客户数、Relay Hub/远程进程数、队列丢弃、tmux 数量与 TTL 回收、Worker lane 排队/执行/失败、容器
CPU/内存/PID/OOM、OpenHands 事件读取页数与 cursor 延迟。日志保留 request/runtime/conversation 的脱敏关联 ID，
不记录消息、查询值或凭据。

入口限流采用按用户与会话的分布式令牌桶/并发租约（需要可用 Redis/Valkey 才能跨进程正确执行）；在该依赖未就绪
前，只启用进程内保护并明确不把它当作全局限流。SSE、WebSocket、Relay、终端和消息发送分别设并发配额与
429/503 的可恢复错误语义。

FR-220 的验收必须验证：会话列表和活动会话读取不再出现 N+1 或全历史扫描；断开、重连、部署和 generation
replacement 后资源回到基线；慢订阅者不会阻塞其他用户；达到上限时系统优雅拒绝而非容器失响应。性能目标以
同一环境压测基线的 p95/p99、资源水位和无泄漏 soak 结果冻结，不能脱离实际机器承诺固定毫秒数。
