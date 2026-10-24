# 2026-10-24 会话失败与 503 取证（FR-674）

本片完成故障机制的隔离复现与提交时间追溯，不代表生产故障已经根治。没有向用户会话发送、
暂停、继续或重放任务，没有修改服务器配置、生产数据库、Runtime 镜像或业务容器。

## 生产证据与时间线

以下时间均为北京时间。SDK 事件的时间字符串没有时区；对应 Runtime 的时区已核对为UTC，
按此换算。Docker 元数据和已保留平台日志使用带 UTC 标识的时间。

| 时间 | 已保留的事实 | 能证明的范围 |
| --- | --- | --- |
| 10月23日 23:34:29 | 节点中心会话的正式 AgentErrorEvent 包含终端 Errno 11 | 工具遇到了资源暂不可用；不能单凭错误字符串确定耗尽的是 PID、线程还是其他系统资源 |
| 10月23日 23:39:17–23:39:43 | 同一 Runtime 容器的停止、启动时间 | 容器发生过停启；没有据此确定操作者或归因某次发布 |
| 10月23日 23:41:19、23:41:28 | 两条会话分别写入原生工具未完成的重启恢复事件 | 恢复时原生执行状态仍为 running，存在未配对工具调用；不是最终 assistant 回复 |
| 10月24日 02:33:11 | API、stream-api 的数据库 DNS 解析异常 | 独立连接基础设施错误；原始日志没有证明与23:34的终端资源错误是同一原因 |
| 10月24日 02:34–02:37 | Worker QueuePool 超时，栈位于 run_once → claim | 领取任务的普通异步连接池曾不可用，不能仅归为 Runtime poll 池耗尽 |
| 10月24日 02:37:56–02:41:15 | API 日志五条 events GET 503 记录 | 确实发生读取失败；没有保留响应体中的具体错误码 |
| 10月24日 02:46:48 | Worker 环境版本锁等待语句超时 | 存在数据库等待；未保留当时的阻塞事务身份和完整锁图 |

直接解析正式事件、保留日志和容器元数据，未将业务消息正文、私有入口、部署路径或账号复制到本文件。
运行中的会话可能继续产生新事件，因此本表只描述已发生并可定位的历史事件。

两个目标 binding 所用实际 Runtime 镜像为
`sha256:712050922eaf54c2361338d1b23c89c8ddfc0c385e2c600f3d173ec496702aba`，
四包均为 `1.49.5`，source commit 为
`d3a9a1f99b144d8799df170c1e1ee9ca0bf4b19d`（9月30日）。同一容器的创建时间也在9月30日。
本片的原生故障验证使用这个实际不可变镜像，没有用仓库当前 `1.54.0` 的 source lock 代替。
这排除了“两个旧会话已经原地换成23日新 SDK 镜像”的解释，但不排除控制面改动或工作量变化
触发旧 Runtime 的资源问题。

## 原生 Runtime 复现

[incident_recovery_check.py](../infra/openhands/incident_recovery_check.py) 只在显式允许的独立
Docker 容器中执行，要求固定 source commit；测试容器无外部网络、无发布端口、无业务挂载，
PID 限额128、内存1 GiB、CPU 1。临时状态属于测试容器，结束后仅移除该容器。

1. 使用原生 TerminalExecutor 执行正常命令，随后用测试子进程真实耗尽 cgroup PID 配额。
   内核返回 EAGAIN，pids.events 的 max 计数增加；原生 libtmux → subprocess 路径抛出
   BlockingIOError／errno 11。释放测试子进程后，原生终端命令恢复成功。
2. 启动真实 Agent Server HTTP 服务，用进程内确定性 LLM 响应创建原生 terminal 工具调用，
   不调用外部模型。在正式 ActionEvent 持久化、状态 running 后实际杀死测试服务进程。
   新进程从同一测试持久目录读取原 conversation，遵守原生租约接管逻辑；结果为 error，
   保留原有五个事件，追加一个原生 AgentErrorEvent，不生成 assistant 最终回复。
   conversation ID、tool_call_id 和历史事件内容不变；重复读取不再追加恢复事件。

模型响应仅用于确定工具动作；终端、内核限额、进程死亡、HTTP、事件落盘和恢复均为实际实现。
未替换 OpenHands 生命周期、伪造恢复事件或修改安装源码。

生产现存的 pids.events、OOM 和用量均在停启之后读取，不能还原故障当时状态。当前日志中
没有足以定位原始 errno 11 调用栈的记录。因此复现证明 PID 耗尽是一条可行机制，**没有证明
生产当时一定触达512的容器 PID 上限**，也不构成直接调大配额的依据。

## PostgreSQL 与平台执行器复现

[database_incident_check.py](../services/platform/scripts/database_incident_check.py) 在独立空
PostgreSQL 与平台测试容器中运行；二者只共享独立网络命名空间，通过 loopback 连接，不接入业务网络。
显式 opt-in、固定测试 URL 和空库检查通过后，执行完整正式 Alembic 链，到
`0170_revision_draft_owner`。使用真实 Database、生产 claim、lock_referenceable_version 以及
run_blocking／run_blocking_control；只有容器资源和测试数据库为新建隔离资源。

| 场景 | 实际结果 |
| --- | --- |
| 同步 QueuePool 两个连接被占用 | 第三个连接超时，独立 control／history 池仍可 SELECT；释放后恢复 |
| Worker 普通异步池被占用 | 真实 claim 调用产生 QueuePool TimeoutError；释放后领取空队列正常 |
| 同一环境版本父行被锁 | 实际 lock_referenceable_version 等待；pg_blocking_pids 可见阻塞，语句超时 SQLSTATE 57014；回滚后恢复 |
| 两个读取线程同时等待上述真实行锁 | 第三个 run_blocking 产生 DomainError，status=503、code=RUNTIME_READ_SATURATED；释放锁后读取、control 和普通异步查询恢复 |
| 在无外部网络的测试容器解析随机 .invalid 名称 | psycopg OperationalError，无 SQLSTATE；验证实际解析错误，未 mock DNS |

读取 503 验证调用的是实际生产执行器并检查其错误码；没有声称走过生产业务 URL 或浏览器。
连接池和锁测试缩短了超时并使用小容量，以确定性地触发边界，不能把这些时间和容量当作
生产故障当时的实测参数。所有池在结束时无 checked-out 连接，随后移除自身测试容器。

## 与提交的关系及下一修复边界

- 前端历史工具活动覆盖终态的关键逻辑来自9月29日 `b6d1f129`，10月17日 `d2d72bf1`
  又修改了暂停优先级；FR-673 已修复这条独立展示链路。
- `lock_referenceable_version` 的引用／删除互斥早于23日；现有 Runtime 观测路径调用它的
  实现也早于23日（当前相关变更可追溯到10月16日 `cef5ae83`）。不能因代码较旧排除新工作量放大竞争。
- 23日13:15的 `7716e53a` 引入历史 MCP 并使用控制 lane 做鉴权；FR-672 已将它隔离到
  history lane。这是已定位的新提交问题，不是本片七类故障都由该提交导致的证据。
- 尚无证据把五条历史 503 逐条归因到环境锁、Runtime 读容量、DNS 或历史鉴权。
  只凭相近时间或相同 HTTP 状态码不能归因。

FR-675 应先补齐读取失败的安全错误码／lane／request-id 关联证据，再针对已复现的
Runtime 观测环境行锁确定最小修复：复用已激活且冻结版本未变的 Runtime 时，避免不必要地
将引用创建锁持有至外部观察结束；新建／替换 Runtime、环境删除互斥和 generation fencing
必须保留正式保护。不能简单删除 FOR UPDATE、增加数据库连接或重启 Runtime 代替修复。
历史资源耗尽及 DNS 的具体触发者仍保留证据缺口，不标记根因已关闭。

## 复现入口与验收

将单个脚本复制到新建、无业务挂载的相应测试容器；原生容器必须使用上面实际验收的固定镜像，
并配置上述资源限额及独立临时状态。数据库容器必须是独立空实例；平台探针容器共享其隔离
网络命名空间，并使用包含正式迁移链的固定平台镜像。本机或共享业务容器禁止执行故障注入。

```bash
# 在独立原生测试容器内；--scenario terminal/restart 可单独验收。
python /tmp/incident_recovery_check.py --allow-isolated-fault-injection \
  --expected-source-commit d3a9a1f99b144d8799df170c1e1ee9ca0bf4b19d

# 在独立平台测试容器内，不能指向业务库；首次运行要求 public schema 无表。
TEST_DATABASE_URL=postgresql+psycopg://incident@127.0.0.1:5432/incident \
  python /tmp/database_incident_check.py --allow-isolated-fault-injection
```

本片七类故障／恢复场景通过，另完整空库迁移通过；原生两项在严格 errno 11 断言与显式 opt-in
加入后重新验证。脚本 Ruff／format／语法与 diff 检查通过。故障注入前后的38个既有容器镜像和
启动时间逐一一致，临时容器均移除。未部署平台或 Runtime 变更，未调整生产配额。
