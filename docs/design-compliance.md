# FlowWeave 设计符合性审计

权威来源：

- 飞书《FlowWeave MVP 产品原型》revision 9
- 飞书《FlowWeave MVP 后台详细设计（Python 统一工程·绿地重构实施基线）》revision 22

状态含义：`DONE` 已有直接、完整证据；`PARTIAL` 仅覆盖部分要求；`TODO` 尚未实现或现状与设计冲突。测试通过本身不等于设计符合。

## 产品原型

| 要求 | 状态 | 当前证据 / 缺口 |
|---|---|---|
| 节点目录树、当前目录搜索、卡片与只读详情 | DONE | `parent_id` 递归目录树、子树节点计数、当前目录搜索/数量、节点卡片和只读详情抽屉已实现；Playwright 覆盖卡片详情。 |
| 四步节点编辑器 | DONE | 四步表单覆盖完整标准类型；Skill 与默认 Skill 均可选，设置默认值时必须指向已导入 Skill；I/O 按输入、输出分区并说明字段契约。 |
| Skill ZIP、MCP/Hooks JSON/YAML 导入 | DONE | 两阶段导入、来源回查、ZIP/JSON/YAML 契约测试。 |
| 模型服务、测试、发现、默认模型、引用数 | DONE | CRUD/test/discover、密钥不回显、启用/默认模型、派生可用状态和引用节点数已实现；服务端禁止节点引用禁用模型，也禁止禁用仍被节点引用的模型；API 与 E2E 覆盖。 |
| 三栏拖拽画布、端口连接、自动布局、重复提示 | DONE | 三栏布局、目录分组/搜索、HTML5 拖入、端口连线、自动布局、重复资产提示均已实现；节点显示默认 Skill 与 START/END 数量；Playwright 覆盖。 |
| 多条 START/END 门禁 | DONE | 编辑、顺序执行、审计和 Python/JavaScript/Prompt 测试已有。 |
| 运行首页两级分组、搜索和状态筛选 | DONE | 流程→运行两级结构、流程/运行/当前节点搜索、状态筛选、分组折叠和真实快照/当前节点/Attempt/进度/时间字段已实现；API 与 E2E 覆盖。 |
| 只读运行图与节点详情弹窗 | DONE | 运行图基于不可变 Snapshot；已激活与未激活节点均可打开详情弹窗，展示执行配置、默认 Skill、I/O、START/END 门禁、运行次数、最新状态及 Node Run/Attempt 历史，并可跳转执行详情；Playwright 覆盖。 |
| Snapshot、任意节点启动、显式 Artifact Binding | DONE | API、状态机和 E2E 覆盖。 |
| Attempt 修订与产物版本不覆盖 | DONE | API、数据库模型和 E2E 覆盖。 |

## 后台详细设计

| 要求 | 状态 | 当前证据 / 缺口 |
|---|---|---|
| `src/flowweave` 正式包、旧后台物理删除 | DONE | `services/api` 删除；生产包已改名为 `flowweave`。 |
| Python 3.12、uv 锁定 | DONE | Docker 3.12.11、`uv.lock`。 |
| app factory、Container 注入、API/Worker 独立入口 | DONE | app factory、进程级 Container、API/Worker 独立入口与显式资源关闭均已实现；API 路由通过容器 AsyncSession UoW 注入，Worker 使用独立短事务会话。 |
| SQLAlchemy 2 async、psycopg、显式 UoW | DONE | API 与 Worker 均由容器持有 AsyncEngine/AsyncSession；claim、恢复、heartbeat、handler 与最终任务状态使用显式短事务 UoW。同步应用服务仅通过 `AsyncSession.run_sync` 在同一 UoW 内执行，生产 Container 不再创建同步 Engine/Session factory；架构门禁禁止生产代码回退 `sync_sessions`。 |
| 模块 domain/application/infrastructure/presentation/public.py | DONE | 所有模块均具备分层目录与 `public.py` facade，跨模块调用全部经过公开 facade；ORM 映射已按 catalog、model_providers、flows、runs、tasks 下沉到各模块 `infrastructure/models.py`，`shared.models` 仅作兼容重导出。AST 门禁同时禁止跨模块内部引用和 ORM 声明回流 shared。 |
| PostgreSQL 16 唯一数据库、Testcontainers 集成测试 | DONE | 生产与全部 80 项后端测试仅使用 PostgreSQL 16；未显式提供 `TEST_DATABASE_URL` 时，pytest fixture 与迁移往返自动使用固定镜像 `postgres:16.9-alpine3.21`，CI 默认走 Testcontainers；仍支持显式外部 PostgreSQL URL 以便本地诊断。 |
| Pydantic DTO `extra=forbid` | DONE | 公共 `ApiModel` 统一使用 `ConfigDict(from_attributes=True, extra="forbid")`。 |
| Pyright strict、Architecture/Contract/安全门禁 | DONE | Python 3.12 无豁免 Pyright strict 为 0 errors/0 warnings；Ruff、AST architecture、PostgreSQL integration、能力导入安全、OpenAPI 基线及 Run Event/Runtime/Gate 正反例 contract tests 均进入 `make api-check` 与 CI。 |
| 短事务 + 外部副作用全部任务化 | DONE | Runtime 与 Gate 由 Worker 任务化，在事务外执行 Runtime/Sandbox/Prompt I/O，返回后以 lease + Attempt CAS 短写事务提交；模型发现/连接测试采用短读事务、事务外异步 HTTP、短写事务。能力导入与大 Artifact 使用“事务外临时/稳定对象预写或读取 → 短事务登记/CAS → 提交或幂等补偿”协议；探针测试直接证明 API、Worker 与能力导入对象存储 I/O 时无活动数据库事务，DB/lease/CAS 失败会回收预写对象。 |
| 领域状态机 + `state_version` CAS | DONE | 人工 Attempt 命令、START/POLL/RESUME/CANCEL Runtime 回调与 Gate 结果写回均使用 `UPDATE ... WHERE state/runtime_phase/state_version` 原子 CAS；双 Session 与真实 I/O 并发窗口测试证明副作用唯一，迟到 Runtime/Gate 结果不会写状态、评估、事件、产物或后续任务。 |
| PostgreSQL SKIP LOCKED、lease heartbeat/fencing | DONE | SKIP LOCKED claim、generation fencing、独立短事务周期 heartbeat、过期租约不可复活与启动恢复均有 PostgreSQL 测试；Handler 只 flush，业务结果与任务 `SUCCEEDED` 由 Worker 同一事务提交，fencing 失败时整体回滚。 |
| 一次性容器 Script Sandbox | DONE | `SandboxPort`、非 root Python/JavaScript 镜像与 Docker 一次性容器适配器已实现；本机授权后的真实 Compose Worker 仍以 UID 10001 运行，仅附加 GID 0 访问 Docker Socket。真实 Worker Gate 任务证明 Python/JavaScript 容器均 `create → start → die(exit 0) → destroy`，运行时 inspect 证明禁网、只读根、cap-drop ALL、no-new-privileges、PID 64、内存 128 MiB、CPU 1、受限 tmpfs、零挂载、用户 65534:65534 和执行后零残留。Docker Socket 的宿主级风险与回退方式已明确记录；该本机配置不得直接复制到生产。 |
| OpenHands 事件归一化与未知结果 inspect | DONE | `read_events` 将异构事件归一化为固定类型/游标；终态事件批次跳过 inspect，未知状态回退 inspect；规范化事件、游标与产物均有 Worker 回归。 |
| ArtifactStorePort、临时对象/finalize/回收、S3 实现 | DONE | Local/S3 适配器统一实现临时对象、原子/幂等 finalize、读取、存在检查和幂等删除；能力导入与大产物均经 Port，Local/Fake-S3 共用安全契约覆盖路径逃逸与回收。 |
| LISTEN/NOTIFY SSE、cursor 补偿、慢客户端策略 | DONE | `0007` 触发器提交后 NOTIFY；SSE 使用独立异步 LISTEN 连接，Last-Event-ID/cursor 补偿、有界批次、生成器背压和空闲心跳均有 PostgreSQL 集成测试；快慢双消费者压力测试证明慢客户端不阻塞快客户端且可无缺口补偿。 |
| 两阶段能力导入与安全规则 | DONE | 两阶段持久化协议、来源回查、ZIP 总量/文件数/层级/单文件/扩展白名单/嵌套压缩/路径/符号链接限制、YAML alias/深度/节点数/循环限制、敏感字段拒绝及持久化到期清理任务均有自动化测试。 |
| 五段迁移基线 | DONE | `0001–0005` 严格对应 catalog、flows、runs、artifacts、execution 五段核心基线；`0006_capability_imports` 与 `0007_run_event_notify` 是为两阶段安全导入和提交后 LISTEN/NOTIFY 追加的前向迁移，保留核心基线且迁移往返已自动验证。 |
| DoD 自动化证明 | DONE | 80 项 PostgreSQL 后端测试、Pyright strict、0001→0007 迁移往返、前端 lint/typecheck/build、Compose 配置和多阶段平台镜像构建均进入门禁；ARM64 实机证明 `quickjs` 可导入、固定 Docker CLI 29.6.1 可用且最终镜像不含 gcc。最新标准 Compose API/Worker/Web/PostgreSQL 健康，2/2 产品 E2E 覆盖模型、节点、流程、运行、Snapshot、Attempt 与 Artifact 主链路；真实 Worker 业务任务和可重复 `make sandbox-smoke` 均证明 Python/JavaScript 一次性 Docker Sandbox 正常执行、受限且无残留。Docker Socket 宿主级风险及回退方式已记录，本机授权配置不得直接复制到生产。 |

## 执行顺序

1. 建立正式 Bootstrap/Container、async PostgreSQL UoW、模块 public facade 与架构测试。
2. 迁移编辑态模块（node assets/model providers/flows/imports），删除同步 service。
3. 迁移 runs/artifacts/tasks/events/orchestration，使用 CAS 和单 UoW。
4. 实现容器 Sandbox、ArtifactStore、OpenHands 事件协议、LISTEN/NOTIFY SSE。
5. 按产品原型重做四个一级页面与浏览器 E2E。
6. 使用 PostgreSQL/Testcontainers 完成 integration、contract、architecture、restart recovery 审计。
