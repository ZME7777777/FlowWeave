# FlowWeave 项目总览、构建与部署统一入口

> 适用对象：第一次接触 FlowWeave 的开发、测试和运维人员
> 当前基线：2026-08-24；FlowRun Runtime 的 FR-12 最终门禁已通过，后续切片状态见
> [FlowRun Runtime 进度](flowrun-runtime-task-progress.md)；Alembic head 为 `0057_flow_run_conversations`
> 本文负责建立全局认识和给出部署决策。命令细节以
> [本地编译、打包与部署](local-build-and-deploy.md) 为准，Runtime 的架构约束以
> [FlowRun 级可替换 OpenHands Runtime 设计](flowrun-openhands-runtime-design.md) 为准。

## 1. 先建立一个整体认识

FlowWeave 是面向内部研发流程的 Agent 工作台。它不是 Agent 执行器本身，而是 Agent 能力的控制面：
管理节点、模型、Skill/MCP/Plugin、流程、快照、权限、门禁、人工确认、产物、审计和 Runtime 生命周期。
真正的 Agent 对话、事件树、工具执行和状态恢复由固定版本的 OpenHands Agent Server 完成。

可以把系统理解成两层：

```text
控制面
Browser -> Web -> API -> PostgreSQL <- Worker -> Runtime Provider
                                                |
执行面                                           v
                           Docker Engine -> FlowRun OpenHands Runtime
                                         -> Gate Sandbox
                                         -> Dependency Builder
                                         -> Environment Setup
```

最重要的运行时关系是：

- 一个 `FlowRun` 对应一个稳定的 Runtime Session、一个外置持久工作空间和一个当前活跃 generation；
- 同一 FlowRun 的多个 Conversation 共用这一个 Runtime，但保留各自的 OpenHands conversation ID 和事件树；
- Runtime 容器可以替换，Workspace、Conversation/Event、Bash Event 和 persistence 位于容器外；
- FlowWeave 只保存 Conversation locator、授权和独立审计，不复制 OpenHands 的消息、HEAD、cursor 或事件树；
- Flow/Flow Snapshot 必须显式绑定已发布、digest 锁定的 Environment Version，没有默认环境或共享
  Agent Server fallback。

典型的产品使用顺序是：

1. 配置模型服务和可用模型；
2. 创建终端环境；平台用内部启动镜像建立首次 Setup Session，用户在终端中安装工具，然后发布不可变
   Environment Version；
3. 导入或创建 Skill、MCP、Plugin、Hook、Memory 等能力，组装节点资产；
4. 在画布中编排 Flow，配置输入输出映射和 START/END Gate，并绑定 Environment Version；
5. 创建 FlowRun，系统冻结 Snapshot；
6. 激活节点、显式绑定输入 Artifact、通过 START Gate 后由人工确认开始；
7. Worker 驱动 OpenHands Runtime 执行，结束后通过 END Gate，再由人工接受或驳回；
8. 驳回会创建新的 Attempt，旧 Snapshot、Attempt、Artifact 和审计历史保持不变。

## 2. 从目录层级认识仓库

### 2.1 根目录

```text
FlowWeave/
├── apps/web/                 React + TypeScript 前端
├── services/platform/        FastAPI API、Worker、Runtime Provider、迁移和测试
├── infra/                    Compose 与所有动态执行镜像
├── contracts/                OpenAPI 与跨进程 JSON Schema 基线
├── agent-packages/           可导入的 OpenHands Skill/Plugin 示例包
├── docs/                     产品、架构、开发、Runtime 与部署文档
├── var/                      本地 Artifact、Workspace 和工具状态
├── Makefile                  开发、验证、镜像构建和 Compose 入口
├── package.json              pnpm workspace 根配置
├── pnpm-lock.yaml            Web 依赖锁
├── pyproject.toml            仓库根的轻量 Python 元数据
└── .env.example              Compose 配置模板，不包含真实密钥
```

各目录的修改边界：

| 路径 | 主要职责 | 修改后通常影响 |
| --- | --- | --- |
| `apps/web/` | 页面、组件、API Client、状态、E2E、Nginx | Web 镜像 |
| `services/platform/` | 全部控制面 Python 代码、迁移、测试 | API/Worker/Runtime Provider/Migration 中的一个或多个 |
| `infra/openhands/` | 固定 OpenHands Runtime 基础镜像、源码锁和契约探针 | 后续 Environment 发布和新 generation |
| `infra/sandbox/` | Python/JavaScript 一次性 Gate Sandbox | 后续新 Gate Sandbox |
| `infra/dependency-builder/` | 能力依赖构建容器 | 后续新依赖构建任务 |
| `infra/compose.yaml` | 本地单机拓扑、网络、卷、健康检查和启动顺序 | 被修改的 Compose 服务，复杂变更建议全量部署 |
| `contracts/` | 公共 API 和跨进程结构的冻结基线 | 文件本身不单独部署，跟随实现变更范围验证/部署 |
| `agent-packages/` | 可选导入示例，不是平台内置执行代码 | 通常只重新打包或导入该 Agent 包 |
| `docs/` | 说明和设计事实 | 不需要部署应用 |
| `var/` | 本地持久数据和工作区 | 不是构建输入，不应提交或随镜像发布 |

### 2.2 平台后端

`services/platform/src/flowweave/` 的主干如下：

```text
flowweave/
├── __init__.py              Python 根包标记
├── bootstrap/
│   ├── api.py                FastAPI 入口、健康检查、Router 注册
│   ├── worker.py             后台任务 claim、lease、heartbeat 和恢复循环
│   ├── runtime_provider.py   受认证的高层 Docker 控制 API
│   ├── container.py          API/Worker 依赖装配
│   └── settings.py           平台配置
├── modules/
│   ├── __init__.py          业务模块命名空间标记
│   ├── catalog/              节点、能力、Profile、Memory、Plugin/MCP 治理
│   ├── model_providers/      模型服务、密钥、模型发现、OAuth
│   ├── environments/         终端环境、Setup Session、版本发布
│   ├── flows/                Flow、节点实例、边和 Gate 配置
│   ├── runs/                 FlowRun、Snapshot、Attempt、Artifact、事件
│   ├── orchestration/        Readiness、Gate、Runtime 与人工动作编排
│   ├── conversations/        FlowRun Conversation locator 与代理连接
│   ├── tasks/                PostgreSQL 后台任务、租约、重试和 fencing
│   ├── sandboxes/            Runtime allocation/session/generation/replacement
│   └── gates/                Prompt/Python/JavaScript Gate 执行适配
├── runtime/                  OpenHands/Mock 适配器、Manifest、路由、Workspace
└── shared/                   数据库、UoW、Artifact、加密、Sandbox/Builder Port
```

业务模块大体遵循以下分层：

```text
presentation -> application -> domain
                     |
                     v
               infrastructure
```

- `presentation/`：HTTP/WebSocket/SSE Router；
- `application/`：用例编排和事务边界；
- `domain/`：状态机、不变量和纯业务规则；
- `infrastructure/`：SQLAlchemy 模型、Docker/HTTP/存储实现；
- `public.py`：跨模块调用门面，其他模块不应穿透内部实现。

后端的其他重要目录：

| 路径 | 用途 |
| --- | --- |
| `services/platform/migrations/versions/` | Alembic 迁移链，当前唯一 head 是 `0057_flow_run_conversations` |
| `services/platform/tests/` | 单元、PostgreSQL 集成、契约和架构边界测试 |
| `services/platform/scripts/` | 迁移矩阵、Compose 安全、OpenHands/Sandbox Smoke |
| `services/platform/pyproject.toml` | Python 3.12 平台依赖和 Ruff/Pyright/Pytest 配置 |
| `services/platform/uv.lock` | 平台依赖锁，是平台镜像的实际 Python 锁文件 |

#### 2.2.1 `bootstrap/`：进程入口与依赖装配

```text
bootstrap/
├── __init__.py            # Python 包标记，不放业务逻辑
├── api.py                 # 创建 FastAPI；注册中间件、错误处理、健康检查和六组业务 Router
├── container.py           # 装配 Database、Runtime、Artifact、Builder、Resolver、Sandbox 等 Port
├── runtime_provider.py    # Runtime Provider HTTP API；认证后执行 Sandbox、终端、镜像和事件操作
├── settings.py            # Pydantic Settings；读取数据库、Runtime、Controller、TTL、存储等配置
└── worker.py              # 后台任务 Worker；claim、lease heartbeat、恢复、执行、重试和优雅退出
```

四个平台注册/作业容器使用同一份 Python 包，但入口不同：`api.py` 对外服务，`worker.py` 消费持久任务，
`runtime_provider.py` 是唯一 Docker 控制边界，Migration 直接运行 Alembic。

#### 2.2.2 `modules/catalog/`：节点和能力治理

```text
catalog/
├── __init__.py                         # 模块包标记
├── public.py                           # 对其他模块公开的稳定门面和导出集合
├── application/
│   ├── __init__.py                     # application 子包标记
│   ├── service.py                      # Node Directory/Asset、I/O、执行器、能力绑定和删除约束
│   ├── agent_profiles.py               # Agent Profile 读取、修订、复制、退役和引用查询
│   ├── capability_collections.py       # 能力集合的查询、保存、成员排序和删除
│   ├── capability_imports.py           # Skill/MCP/Plugin/Hook 校验、解包、提交、更新、依赖构建和批量删除
│   ├── capability_repository.py        # 不可变 Capability 包/版本/blob 发布、digest 和内置 Policy
│   ├── mcp_oauth_authorizations.py     # MCP OAuth 授权 Runtime、回调、状态和清理生命周期
│   ├── mcp_oauth_secrets.py            # MCP OAuth Secret Reference 加密状态、刷新、撤销和审计
│   ├── mcp_validations.py              # MCP Probe Runtime 的分配、完成、失败和回收
│   ├── memory_sources.py               # Memory Source/Version 的审核、扫描、激活、退役和保留
│   ├── plugin_sources.py               # Git/Marketplace Plugin 来源解析、发布、过期和失败处理
│   └── plugin_validations.py           # Plugin Probe Runtime 的校验和生命周期
├── domain/
│   └── __init__.py                     # 当前无独立领域对象，保留边界
├── infrastructure/
│   ├── __init__.py                     # infrastructure 子包标记
│   └── models.py                       # Catalog 全部 SQLAlchemy 映射
└── presentation/
    ├── __init__.py                     # presentation 子包标记
    └── router.py                       # Node、Capability、Profile、Memory、Plugin、MCP OAuth/Probe API
```

#### 2.2.3 `modules/conversations/`：FlowRun 会话定位与代理

```text
conversations/
├── __init__.py                         # 模块包标记
├── public.py                           # locator、绑定和 active Runtime 路由的公开门面
├── application/
│   ├── __init__.py                     # application 子包标记
│   ├── locator.py                      # 维护最小 locator，并解析 FlowRun 当前 active generation
│   └── service.py                      # 创建/列出/读取/重命名会话，代理消息、事件和控制请求
├── domain/
│   ├── __init__.py                     # domain 子包标记
│   └── enums.py                        # 明确声明会话 role/state 归 OpenHands，平台不定义这些枚举
├── infrastructure/
│   ├── __init__.py                     # infrastructure 子包标记
│   └── models.py                       # Conversation binding 与 Runtime confirmation 审计映射
└── presentation/
    ├── __init__.py                     # presentation 子包标记
    └── router.py                       # FlowRun Conversation REST、事件流和终端 WebSocket
```

#### 2.2.4 `modules/environments/`：终端环境和版本发布

```text
environments/
├── __init__.py                         # 模块包标记
├── public.py                           # Environment 用例的稳定公开门面
├── application/
│   ├── __init__.py                     # application 子包标记
│   └── service.py                      # 创建环境、Setup Session、发布 Version、删除和清理任务
├── infrastructure/
│   ├── __init__.py                     # infrastructure 子包标记
│   ├── docker.py                       # Setup 容器/终端/凭据卷/镜像发布和内部启动镜像 digest
│   └── models.py                       # TerminalEnvironment、Version、SetupSession 映射
└── presentation/
    ├── __init__.py                     # presentation 子包标记
    └── router.py                       # Environment CRUD、发布和 Setup Terminal WebSocket
```

#### 2.2.5 `modules/flows/`：流程定义

```text
flows/
├── __init__.py                         # 模块包标记
├── public.py                           # Flow 读写和校验公开门面
├── application/
│   ├── __init__.py                     # application 子包标记
│   └── service.py                      # Flow CRUD、节点实例、边、端口映射、Gate 和环境绑定
├── domain/
│   ├── __init__.py                     # domain 子包标记
│   └── rules.py                        # 拓扑、映射、重复实例和引用等纯规则
├── infrastructure/
│   ├── __init__.py                     # infrastructure 子包标记
│   └── models.py                       # FlowDefinition/Node/Edge/PortMapping/GatePolicy 映射
└── presentation/
    ├── __init__.py                     # presentation 子包标记
    └── router.py                       # Flow 列表、创建、详情、更新、校验和删除 API
```

#### 2.2.6 `modules/gates/`：门禁执行

```text
gates/
├── __init__.py                         # 模块包标记
├── public.py                           # Gate executor 公开门面
└── application/
    ├── __init__.py                     # application 子包标记
    ├── executor.py                     # Prompt/Python/JavaScript Gate 的统一调度和结果规范化
    └── _python_runner.py               # 进程模式下的最小 Python Gate Runner
```

#### 2.2.7 `modules/model_providers/`：模型服务

```text
model_providers/
├── __init__.py                         # 模块包标记
├── public.py                           # Provider 查询、运行凭据和模型快照公开门面
├── application/
│   ├── __init__.py                     # application 子包标记
│   └── service.py                      # Provider CRUD、模型发现/启停、测试和引用约束
├── domain/
│   └── __init__.py                     # 当前无独立领域文件，保留边界
├── infrastructure/
│   ├── __init__.py                     # infrastructure 子包标记
│   ├── client.py                       # OpenAI 兼容模型发现/测试 HTTP Client
│   ├── codex_oauth.py                  # Codex Device OAuth 和 Runtime credential 解析
│   └── models.py                       # ModelProvider、ProviderModel 映射
└── presentation/
    ├── __init__.py                     # presentation 子包标记
    └── router.py                       # Provider CRUD、发现、测试和 OAuth API
```

#### 2.2.8 `modules/orchestration/`：运行编排主用例

```text
orchestration/
├── __init__.py                         # 模块包标记
├── public.py                           # Run/Attempt/Artifact/Runtime 用例的稳定公开门面
├── application/
│   ├── __init__.py                     # application 子包标记
│   └── service.py                      # FlowRun 主编排：Snapshot、Binding、Gate、Runtime、人工动作、事件
├── domain/
│   └── __init__.py                     # 当前领域规则主要复用 runs/domain
├── infrastructure/
│   └── __init__.py                     # 当前无独立适配文件，保留层级
└── presentation/
    └── __init__.py                     # HTTP 入口统一放在 runs/presentation
```

`application/service.py` 是后端最大的用例文件；修改它通常同时影响 API 与 Worker，不适合只部署单个进程。

#### 2.2.9 `modules/runs/`：运行领域、持久化和 API

```text
runs/
├── __init__.py                         # 模块包标记
├── public.py                           # Readiness 领域类型和求值函数公开门面
├── application/
│   └── __init__.py                     # 应用用例由 orchestration 承担，保留包边界
├── domain/
│   ├── __init__.py                     # domain 子包标记
│   ├── readiness.py                    # InputField/Binding/Artifact 完备性与类型检查
│   ├── state.py                        # FlowRun/NodeRun 等基础状态迁移
│   └── state_machine.py                # Attempt 事件驱动状态机
├── infrastructure/
│   ├── __init__.py                     # infrastructure 子包标记
│   ├── event_listener.py               # PostgreSQL LISTEN/NOTIFY + cursor SSE 补偿读取
│   └── models.py                       # FlowRun、Snapshot、NodeRun、Attempt、Artifact、Gate、Event 映射
└── presentation/
    ├── __init__.py                     # presentation 子包标记
    └── router.py                       # Run、NodeRun、Attempt、Artifact、人工动作、Runtime 运维和 SSE API
```

#### 2.2.10 `modules/sandboxes/`：Runtime 与动态资源控制面

```text
sandboxes/
├── __init__.py                         # 模块包标记
├── public.py                           # Sandbox/Runtime allocation/session/replacement 的公开门面
├── application/
│   ├── __init__.py                     # application 子包标记
│   ├── runtime_allocation.py           # FlowRun 外置目录、只读 capability、Secret Reference 和删除保护
│   ├── runtime_operations.py           # Runtime 脱敏概览、保留策略和人工 replacement 请求
│   ├── runtime_replacement.py          # N→N+1 drain、lease takeover、identity probe 和激活流程
│   ├── runtime_sessions.py             # Session/generation/CAS fence/replacement lease 数据操作
│   └── service.py                      # Managed Sandbox ensure/inspect/delete/reconcile 与 owner 生命周期
├── infrastructure/
│   ├── __init__.py                     # infrastructure 子包标记
│   ├── docker.py                       # Docker/Remote Controller Sandbox Provider 实现
│   └── models.py                       # ManagedSandbox、allocation、session、generation 映射
```

#### 2.2.11 `modules/tasks/`：PostgreSQL 后台任务

```text
tasks/
├── __init__.py                         # 模块包标记
├── public.py                           # enqueue、lease 和任务状态公开门面
├── application/
│   ├── __init__.py                     # application 子包标记
│   ├── handlers.py                     # task_type 到具体业务 Handler 的分发表
│   └── service.py                      # enqueue、claim、heartbeat、succeed/fail、重试和过期恢复
├── infrastructure/
│   └── models.py                       # BackgroundTask SQLAlchemy 映射
└── presentation/
    └── __init__.py                     # 当前无公共任务管理 API，保留边界
```

`modules/credentials/` 当前没有受版本控制的源码文件，只是本地残留的空目录/缓存路径，不是有效业务包；
凭据加密和模型/MCP Secret 分别位于 `shared/credentials_crypto.py`、`model_providers/` 和 `catalog/`。

#### 2.2.12 `runtime/`：OpenHands 适配层

```text
runtime/
├── __init__.py          # Runtime 包标记
├── auth.py              # 临时 Runtime API Key、认证头和 Secret 注入辅助
├── base.py              # RuntimePort、Handle、Request、Result、Agent Spec 等正式内部类型
├── contract.py          # Runtime result/event/identity 契约规范化和严格校验
├── dependencies.py      # RuntimePort 的 ContextVar 绑定、获取和释放
├── manifest.py          # Snapshot Runtime Manifest 编译、digest 和能力版本冻结
├── mock.py              # 仅测试使用的内存 Mock Runtime
├── openhands.py         # OpenHands REST/stream/pause/conversation/terminal 生产适配器
├── request.py           # 从 Snapshot、模型、Policy、能力和 Workspace 构造正式创建请求
├── routing.py           # 根据持久 adapter/handle 选择 OpenHands 或 Mock
└── workspace.py         # 节点/Attempt 工作区及 Skill/MCP/Hook/Memory 的安全物化
```

#### 2.2.13 `shared/`：跨模块领域、Port 与基础设施

```text
shared/
├── __init__.py                    # shared 包标记
├── artifact_store.py              # ArtifactStorePort 的请求/Worker ContextVar 绑定
├── credentials_crypto.py          # 平台模型/MCP Secret 的对称加密与解密
├── database.py                    # SQLAlchemy Base、ID/时间函数和同步兼容 Session Factory
├── dependency_builder.py          # DependencyBuilderPort 的上下文绑定
├── errors.py                      # DomainError 及 not_found/conflict 等错误工厂
├── http.py                        # FastAPI Database/Idempotency 等依赖类型和响应辅助
├── models.py                      # 模块自有 ORM 映射的兼容集中导出，不应承载新模型
├── plugin_resolver.py             # PluginResolverPort 的上下文绑定
├── sandbox.py                     # SandboxPort 的上下文绑定
├── schemas.py                     # 跨模块 Pydantic API Command/Response Schema
├── settings.py                    # Settings 的请求/Worker ContextVar 兼容绑定
├── application/
│   ├── __init__.py                # application 子包标记
│   ├── artifact_store.py          # ArtifactStorePort 协议和对象引用类型
│   ├── dependency_builder.py      # DependencyBuilderPort 与 build request/result 类型
│   ├── plugin_resolver.py         # PluginResolverPort 和 Marketplace/Git 解析类型
│   ├── sandbox.py                 # SandboxPort、资源和终端协议
│   ├── transactions.py            # commit/rollback action 与 UoW 所有权辅助
│   └── uow.py                     # Async/Sync Unit of Work 抽象
├── domain/
│   ├── __init__.py                # domain 子包标记
│   ├── agent_definition.py        # Agent Definition 文档规范化与约束
│   ├── capability_digest.py       # 能力配置的 canonical form 和 digest
│   ├── enums.py                   # Run/Attempt/NodeRun/Task 等共享业务枚举
│   ├── errors.py                  # 领域层错误类型导出
│   ├── runtime_policy.py          # Agent/Context/Memory/Critic/Condenser Policy 规范化
│   └── tool_policy.py             # Tool catalog、权限、并发和参数 Policy 规范化
└── infrastructure/
    ├── __init__.py                # infrastructure 子包标记
    ├── artifact_store.py          # Local/S3 ArtifactStore 实现和工厂
    ├── database.py                # Async Database、Session/UoW、ping 和连接池
    ├── dependency_builder.py      # Disabled/Docker/Remote Dependency Builder 实现
    ├── docker_control.py          # Docker 资源所有权、label、inspect 和底层安全校验
    ├── docker_controller.py       # Runtime Provider HTTP Client、认证和终端转发
    ├── plugin_resolver.py         # Docker/Remote Plugin Resolver 实现
    └── sandbox.py                 # Process/Docker/Remote Sandbox 实现和工厂
```

#### 2.2.14 平台根文件、迁移、脚本和测试

```text
services/platform/
├── .python-version             # 本地 uv/pyenv 使用的 Python 3.12 版本提示
├── Dockerfile                  # Python 3.12 多阶段平台镜像，复制迁移与全部 src
├── alembic.ini                 # Alembic 配置和迁移脚本位置
├── pyproject.toml              # 平台依赖、Hatch、Pytest、Pyright、Ruff 配置
├── uv.lock                     # 平台可复现依赖锁
├── migrations/
│   ├── __init__.py             # migration 包标记
│   ├── env.py                  # 导入全部模块 ORM metadata，建立在线/离线迁移上下文
│   ├── script.py.mako          # Alembic 新 revision 的代码模板
│   └── versions/__init__.py    # versions 包标记
├── typings/
│   ├── boto3/__init__.pyi      # 项目所用 boto3 子集的本地类型 Stub
│   ├── quickjs.pyi             # quickjs 原生模块类型 Stub
│   └── yaml/__init__.pyi       # PyYAML 本地类型 Stub
└── scripts/
    ├── compose_security_check.py   # 检查 Socket、网络、身份、权限和 Runtime 安全配置
    ├── migration_check.py          # PostgreSQL 空库/历史基线 upgrade-downgrade 矩阵
    ├── openhands_fake_llm.py       # OpenHands Smoke 使用的本地假模型服务
    ├── openhands_smoke_check.py    # 固定 Runtime 的 create/tool/confirmation/condenser/task Smoke
    └── sandbox_smoke_check.py      # 通过生产 Provider 路径验证 Python/JavaScript Sandbox
```

迁移文件按时间追加，文件名就是其主要职责：

```text
0001_catalog.py                         # 节点目录、节点资产和基础能力目录
0002_flows.py                           # Flow、节点实例、边和 Gate
0003_runs.py                            # FlowRun、Snapshot、NodeRun、Attempt 和事件
0004_artifacts.py                       # Artifact Version 与输入绑定
0005_execution.py                       # 运行执行、Gate 和后台任务基线
0006_capability_imports.py              # 能力校验/提交两阶段导入
0007_run_event_notify.py                # Run Event PostgreSQL NOTIFY
0008_agent_conversations.py             # 历史平台 Conversation 模型
0009_runtime_cancellation.py            # Runtime 取消阶段与任务
0010_independent_port_mappings.py        # 端口映射从可视边中独立
0011_oauth_credentials.py               # 历史 OAuth credential 模型
0012_lark_document_contracts.py         # Lark 文档输入输出契约
0013_lazy_lark_run_resources.py          # Lark 运行资源延迟物化
0014_terminal_environments.py            # Terminal Environment 与 Setup Session
0015_run_terminal_environment.py         # Run 与环境版本引用
0016_environment_version_deletion.py     # Environment Version 删除生命周期
0017_managed_sandboxes.py                # Managed Sandbox 资源台账
0018_remove_platform_credentials.py      # 删除平台持久 Lark credential
0019_hard_delete_legacy_flows.py         # 旧 Flow 物理删除
0020_agent_subagents.py                  # 历史平台子 Agent 模型
0021_codex_oauth_model_providers.py      # Codex OAuth 模型服务
0022_attempt_model_reasoning.py          # Attempt 模型与 reasoning 选择
0023_skill_collections.py                # Skill 集合
0024_runtime_confirmation_batches.py     # Runtime Tool 确认批次
0025_confirmation_policy.py              # Confirmation Policy
0026_condenser_policy.py                 # Condenser Policy
0027_runtime_condensations.py            # Runtime condensation 记录
0028_runtime_condensation_commands.py    # Condensation 命令
0029_capability_repository.py            # 不可变能力仓库
0030_snapshot_runtime_manifest.py        # Snapshot Runtime Manifest
0031_tool_policy_runtime_spec.py         # Tool Policy 进入 Runtime Spec
0032_context_policy_runtime_spec.py      # Context Policy 进入 Runtime Spec
0033_plugin_source_resolutions.py        # Plugin 来源解析
0034_memory_critic_runtime_policies.py   # Memory/Critic Runtime Policy
0035_capability_collections.py           # 通用能力集合
0036_native_subagent_tasks.py            # OpenHands 原生 Task 子 Agent
0037_subagent_task_usage.py              # Task 子 Agent 用量
0038_mcp_target_validations.py           # MCP 目标 Probe/验证
0039_mcp_oauth_secret_references.py      # MCP OAuth Secret Reference
0040_mcp_oauth_authorizations.py         # MCP OAuth 授权生命周期
0041_plugin_marketplace_sources.py       # Plugin Marketplace 来源
0042_memory_sources.py                   # Memory Source/Version
0043_memory_source_governance.py         # Memory 审核/扫描/激活治理
0044_memory_source_retention.py          # Memory 保留与删除
0045_runtime_conversation_forks.py       # OpenHands Conversation fork 审计
0046_governed_tool_policy_catalog.py     # 受治理 Tool Policy Catalog
0047_runtime_agent_governance.py         # Agent Profile/Goal/Critic 等 Runtime 治理
0048_node_asset_active_name_uniqueness.py # 活跃节点名称唯一性
0049_repair_context_policy_identity.py   # 修复 Context Policy identity
0050_remove_node_environment_binding.py  # 删除 Node 级环境绑定
0051_physical_delete_business_resources.py # 业务资源物理删除约束
0052_flow_environment_binding.py         # Flow/Flow Snapshot 强制环境版本绑定
0053_flow_run_runtime_allocation.py      # FlowRun 外置 Runtime 目录和 Secret Reference
0054_flow_run_runtime_sessions.py        # Runtime Session/generation/fence
0055_flow_run_conversation_bindings.py   # 最小 Conversation locator
0056_runtime_replacement.py              # replacement lease 和 generation 状态
0057_flow_run_conversation_model.py      # 去平台消息/cursor、FlowRun 会话模型收口
```

测试文件同样按职责拆分：

```text
tests/
├── conftest.py                          # PostgreSQL Fixture、App/Container 和测试工厂
├── architecture/test_boundaries.py      # 模块 public facade、ORM 归属和依赖边界
├── contract/
│   ├── test_artifact_store.py           # ArtifactStore Port 契约
│   ├── test_contracts.py                # OpenAPI 与 JSON Schema 基线
│   └── test_sandbox.py                  # Sandbox Port 契约
├── integration/
│   ├── test_object_store_uow.py         # 对象存储与 UoW 提交/回滚
│   └── test_postgres_baseline.py        # PostgreSQL 基础行为和迁移基线
├── test_api.py                          # Flow/Run/Artifact/人工动作主 API
├── test_capability_imports.py           # Skill/MCP/Plugin/Hook 导入安全和提交
├── test_compose_security_check.py       # Compose 安全检查脚本
├── test_conversations.py                # FlowRun locator、事件代理、stream 和 terminal
├── test_dependency_builder.py           # 依赖构建协议和安全约束
├── test_domain.py                       # Readiness、状态机和领域规则
├── test_environments.py                 # Environment/Setup/发布/删除/内部启动镜像
├── test_gates.py                        # Prompt/Python/JavaScript Gate
├── test_hook_scripts.py                 # Hook 脚本路径、大小、digest 和物化
├── test_mcp_validations.py              # MCP 配置与 Probe
├── test_memory_sources.py               # Memory Source 治理和保留
├── test_openhands.py                    # OpenHands adapter、request、event 和 recovery
├── test_openhands_source_supply.py      # 固定源码版本、archive digest 和 overlay 边界
├── test_plugin_resolver.py              # Plugin resolver 容器协议
├── test_plugin_sources.py               # Plugin 来源解析和发布
├── test_plugin_validations.py           # Plugin Probe
├── test_runtime_contract.py             # Runtime schema/identity/manifest 契约
├── test_runtime_replacement.py          # generation replacement、lease 和 fencing
├── test_sandbox_controller.py           # Runtime Provider 高层 Controller API
├── test_sandboxes.py                    # Managed Sandbox/Runtime allocation/reconcile
├── test_skill_collections.py            # 能力集合和引用
└── test_tasks.py                        # 后台任务 claim/heartbeat/retry/fencing
```

IDE 中看到的 `.venv/`、`.pytest_cache/`、`.ruff_cache/`、`__pycache__/`、`e2e-artifacts/` 和
`services/platform/var/` 都是本地依赖、缓存或测试运行产物，不是源码包，也不进入 Git 交付。

### 2.3 Web 前端

```text
apps/web/
├── src/
│   ├── pages/                节点、能力、环境、流程、运行、模型、Agent 会话页面
│   ├── components/           编辑器、治理面板、终端和通用组件
│   ├── api/client.ts         `/api/v1` 客户端
│   ├── store/workbench.ts    当前视图和工作台选择状态
│   ├── types.ts              前端领域类型
│   └── App.tsx               顶层导航和页面切换
├── e2e/                      Playwright 产品闭环
├── nginx.conf                静态资源、SPA fallback、`/api` 反向代理
├── Dockerfile                Node 构建 + Nginx 运行的多阶段镜像
└── package.json              Vite/TypeScript/ESLint/Playwright 脚本
```

前端负责交互和展示，不负责领域状态迁移、权限、幂等和并发判定；这些约束必须留在服务端。

#### 2.3.1 `src/`：应用入口、API、状态和公共类型

```text
src/
├── main.tsx                 # 浏览器入口；挂载 React Root、QueryClient、Dialog 和 ErrorBoundary
├── App.tsx                  # 顶层导航；切换节点/能力/环境/流程/运行/模型/工作台/Agent 会话
├── types.ts                # 前端全部领域 DTO：Flow、Run、Attempt、Artifact、Capability、Runtime 等
├── vite-env.d.ts           # Vite `import.meta.env` TypeScript 类型声明
├── styles.css              # 全站、列表、工作台、流程画布和响应式样式
├── agent-chat.css          # Agent 对话页、会话轨道和事件时间线样式
├── api/
│   └── client.ts           # `/api/v1` fetch 封装、ApiError、全部 API、SSE 和 WebSocket URL
├── store/
│   └── workbench.ts        # Zustand 工作台状态、localStorage 持久化和旧状态清洗
└── utils/                  # 当前没有受版本控制的文件；预留纯前端工具函数目录
```

`types.ts` 目前是手工维护的前端协议视图，不是由 OpenAPI 自动生成；修改 API Schema 时必须同步核对
`api/client.ts`、`types.ts` 和 `contracts/openapi-v1.json`。

#### 2.3.2 `src/pages/`：页面级容器

```text
pages/
├── NodesPage.tsx                 # 节点目录树、节点卡片、详情、编辑和引用阻塞删除
├── CapabilitiesPage.tsx          # Skill/MCP/Plugin/Hook/Profile/Collection 导入、编辑、版本和批量删除
├── TerminalEnvironmentsPage.tsx  # Environment CRUD、Setup Session、xterm 终端、发布和版本删除
├── FlowsPage.tsx                 # XYFlow 画布、节点实例、边、端口映射、Gate、环境绑定和校验
├── RunsPage.tsx                  # FlowRun 分组列表、筛选、状态摘要和进入工作台
├── WorkbenchPage.tsx             # Run/Snapshot/NodeRun/Attempt/Artifact/Gate/人工动作主工作台
├── AgentChatPage.tsx             # FlowRun 会话列表、提问、OpenHands 事件时间线和 Runtime 侧栏
└── ModelsPage.tsx                # 模型服务 CRUD、模型发现/测试、Codex Device OAuth
```

页面文件负责组合查询、命令和组件。可复用弹窗、终端、治理卡片应放入 `components/`，不应继续堆入页面。

#### 2.3.3 `src/components/`：复用交互组件

```text
components/
├── AgentProfileHistoryDialog.tsx       # Agent Profile 版本历史、内容和引用查看
├── AgentProfileSwitchPanel.tsx         # Run 内 Profile 切换预览、差异确认和执行
├── AgentRuntimeSidebar.tsx              # Runtime Terminal、独立终端页和会话右侧栏
├── AgentRuntimeSidebar.css              # Runtime 侧栏、xterm 和终端窗口样式
├── AppErrorBoundary.tsx                 # React 顶层异常边界和降级 UI
├── CapabilityCollectionEditorDialog.tsx # 能力集合名称、说明、成员和顺序编辑
├── HookEditorDialog.tsx                 # Hook JSON、脚本上传、引用检查、大小和扩展名限制
├── MarketplaceCatalogDialog.tsx         # Marketplace Git/commit/path 预览、条目选择和发布
├── NodeEditor.tsx                       # Node Asset、I/O、模型、Prompt、能力和 Policy 编辑
├── ProductDialog.tsx                    # 全局 confirm/prompt 对话框 Provider 和渲染实现
├── ProductDialogContext.ts              # Dialog API 类型、Context 和 useProductDialog Hook
├── RuntimeConfirmationPanel.tsx         # OpenHands Tool Action 人工批准/拒绝面板
├── RuntimeGovernancePanel.tsx           # Runtime 健康、generation、stream 状态和 replacement 操作
├── StartRunDialog.tsx                   # FlowRun 创建参数和 Environment Version 确认
├── ToolPolicyEditorDialog.tsx           # Tool allowlist、访问等级、并发、参数和帮助编辑
└── useEscapeClose.ts                     # 全局 Escape 关闭栈，保证嵌套弹窗只关闭最上层
```

#### 2.3.4 Web 构建、代理和 E2E 文件

```text
apps/web/
├── Dockerfile                         # Node 22 构建 Vite，再复制 dist 到 Nginx
├── package.json                       # dev/build/lint/typecheck/e2e 脚本和前端依赖
├── index.html                         # Vite HTML 模板和 `#root` 挂载点
├── nginx.conf                         # SPA fallback、静态缓存、40 MiB 请求和 `/api` 代理
├── vite.config.ts                     # Vite React 插件、开发代理和构建配置
├── eslint.config.js                   # ESLint/TypeScript/React Hooks 规则
├── tsconfig.json                      # TypeScript 工程引用入口
├── tsconfig.app.json                  # Browser/React 源码 TypeScript 配置
├── tsconfig.node.json                 # Vite/Node 配置文件 TypeScript 配置
├── playwright.config.ts               # E2E 目录、Chrome、单 Worker、超时、Trace 和截图配置
└── e2e/
    ├── product-flow.spec.ts           # 模型→环境→节点→Flow→Run→Gate→会话产品闭环
    ├── capability-modules.spec.ts     # Capability/Profile/Memory/Plugin/Collection 等模块 UI
    ├── tool-policy-editor.spec.ts     # Tool Policy 编辑器交互和持久化
    └── fixtures/ui-product-skill.zip  # E2E 导入 Skill 的固定测试包
```

IDE 中看到的 `node_modules/` 是 pnpm 安装结果，`dist/` 是 Vite 生产构建，`test-results/` 和
`playwright-report/` 是 E2E 产物，`*.tsbuildinfo` 是 TypeScript 增量缓存；它们都不是应手工修改的源码。

### 2.4 基础设施和持久数据

`infra/compose.yaml` 包含两类对象。

常驻或一次性 Compose 服务：

| 服务 | 类型 | 作用 |
| --- | --- | --- |
| `workspace-init` | 一次性作业 | 初始化 Artifact 与宿主机 Workspace 权限 |
| `postgres` | 常驻 | 业务数据库、后台任务队列和事件事实 |
| `migration` | 一次性作业 | 执行 `alembic upgrade head`，成功后 API/Worker 才启动 |
| `runtime-provider` | 常驻 | 唯一持有 Docker Socket 的控制边界 |
| `api` | 常驻 | `/api/v1`、健康检查、SSE、WebSocket |
| `worker` | 常驻 | 处理可恢复后台任务和 Runtime 编排 |
| `web` | 常驻 | Nginx + React 静态资源及 `/api` 反向代理 |

由 Runtime Provider 动态创建、不是普通常驻服务的容器：

| 类型 | 使用镜像 | 生效方式 |
| --- | --- | --- |
| FlowRun Agent Runtime | 已发布的 Environment Runtime Image digest | 新 FlowRun/generation 按冻结 digest 启动 |
| Environment Setup | 平台内部 Setup 启动镜像或已有 Environment Version | 用户交互配置后发布新 Environment Version；内部启动镜像不能被 Flow 绑定 |
| Gate Sandbox | `flowweave-sandbox-python:1` / `javascript:1` | 新 Gate 执行时创建，用后销毁 |
| Dependency Builder | `flowweave-dependency-builder:1` | 新能力依赖构建时创建，用后销毁 |

主要持久数据：

- PostgreSQL：Compose named volume `postgres-data`；
- Artifact：Compose named volume `artifacts`；
- Workspace/Runtime state：`${FLOWWEAVE_RUNTIME_HOST_WORKSPACE_ROOT}`，默认
  `${PWD}/var/workspaces`；
- Environment 凭据：Runtime Provider 管理的、按 Environment 隔离的 Docker volume，不进入镜像层。

## 3. “编译、打包、部署”在本项目中分别是什么

本仓库没有一个单独的二进制发布物，交付单元是 Docker 镜像和数据库迁移。

| 交付单元 | 构建入口 | 包含内容 | 部署/使用方式 |
| --- | --- | --- | --- |
| Web 镜像 | `apps/web/Dockerfile` | Vite 生产产物 + Nginx | 重建并重启 `web` |
| Platform 镜像 | `services/platform/Dockerfile` | Python venv、平台源码、迁移、Docker CLI | Compose 分别标记为 `migration/api/worker/runtime-provider` |
| OpenHands Runtime 基础镜像 | `infra/openhands/Dockerfile` | 固定 OpenHands 1.42.0、工具链、源码 provenance、契约探针 | 用于环境构建；发布后的 Environment Runtime 按 digest 运行 |
| Python/JS Sandbox 镜像 | `infra/sandbox/*/Dockerfile` | 受限的一次性代码 Runner | Runtime Provider 后续按需创建 |
| Dependency Builder 镜像 | `infra/dependency-builder/Dockerfile` | Python/Node 依赖构建 Runner | Runtime Provider 后续按需创建 |
| Schema | Alembic migration | PostgreSQL 结构变化 | `migration` 成功后再更新平台进程 |

“编译”包含 TypeScript、Vite、Python 静态检查/测试和镜像构建；“打包”主要指生成上述镜像；“部署”是
迁移数据库并重新创建对应 Compose 服务。动态 Runtime 镜像只影响之后创建的容器，不会原地改写已经
运行的容器。

## 4. 变更范围与部署选择

### 4.1 决策表

| 变更范围 | 最小合理部署 | 何时扩大范围 |
| --- | --- | --- |
| 仅 `docs/**`、注释或不进入镜像的说明 | 不部署 | 无 |
| 仅 `apps/web/**`、根 `pnpm-lock.yaml` | `web` | API 契约也变了时同时部署后端 |
| 仅 API Router/序列化，确认 Worker/Provider 不引用 | `api` | 无法证明隔离时部署全部平台进程 |
| 仅 Worker 循环或任务 Handler | `worker` | 任务 payload/共享模型变更时部署全部平台进程 |
| 仅 Runtime Provider 高层 Docker API | `runtime-provider` | 客户端请求/响应契约也变了时同时部署 API/Worker |
| `services/platform/src` 中共享模块、模型、设置、Runtime 适配器 | `migration runtime-provider api worker` | 同时改 Web/基础镜像时使用全量部署 |
| `services/platform/pyproject.toml`、`uv.lock`、平台 Dockerfile | 全部四个平台服务/作业镜像 | 一般不要做单服务部署 |
| 新增或修改 Alembic migration | 先 `migration`，成功后部署全部平台进程 | 有前端配套时再部署 Web |
| `contracts/**` | 按配套实现决定；契约文件自身不单独部署 | OpenAPI/前端类型变化通常是 API + Web |
| `infra/openhands/**` | 重建 OpenHands 基础镜像 | 要让业务使用新版本，还需发布新的 Environment Version 并重新绑定 Flow |
| `infra/sandbox/python/**` 或 `javascript/**` | 重建对应 Sandbox 镜像 | Gate 协议也变时同时部署 Worker/Provider |
| `infra/dependency-builder/**` | 重建 Dependency Builder | Builder 协议也变时同时部署 Worker/Provider |
| `infra/compose.yaml`、`.env` 契约、多个子系统 | 受影响服务全部 recreate；推荐全量部署 | 网络、卷、依赖或安全边界变化必须全量核对 |
| `agent-packages/**` | 重新打包/导入对应 Agent 包 | 不需要重建平台，除非平台导入协议也变了 |

### 4.2 什么时候只部署单个服务

只有在下面条件同时满足时才做最小部署：

1. 变更入口明确，只被一个进程加载；
2. 没有数据库迁移、共享 ORM/Schema、配置项或跨进程 payload 变化；
3. 没有修改平台 Dockerfile、Python 依赖或 Runtime/Provider 契约；
4. 已执行该服务的定向验证；
5. 能接受该服务短暂重启的影响。

通用命令：

```bash
docker compose --env-file .env -f infra/compose.yaml build --no-cache <service>
docker compose --env-file .env -f infra/compose.yaml \
  up -d --no-deps --force-recreate <service>
```

`--no-deps` 只适合依赖已经健康的情况；首次启动或依赖本身也变化时应去掉。

### 4.3 什么时候部署全部平台进程

以下情况应一起部署 `migration runtime-provider api worker`：

- 修改共享 Python 模块、SQLAlchemy 模型、设置或 Runtime 适配器；
- 修改平台依赖锁或 `services/platform/Dockerfile`；
- 不能确定一处变更是否被多个 Bootstrap 入口导入；
- 要保证 API、Worker、Provider 运行相同 commit；
- 新增迁移，需要先升级 Schema 再切换代码。

```bash
docker compose --env-file .env -f infra/compose.yaml \
  build --no-cache migration runtime-provider api worker
docker compose --env-file .env -f infra/compose.yaml \
  up --no-deps --force-recreate migration
docker compose --env-file .env -f infra/compose.yaml \
  up -d --no-deps --force-recreate runtime-provider api worker
```

Migration 必须以退出码 `0` 结束；失败时不要部署依赖新 Schema 的服务。

### 4.4 什么时候可以全量打包部署

以下场景推荐使用全量：

- 跨 Web、平台、Compose 或动态执行镜像的功能发布；
- 基础镜像、依赖、环境变量、网络、卷或安全边界变化；
- 新机器首次部署、长期未同步后的环境校准；
- 无法可靠计算影响范围；
- 需要做一次完整本地发布验收。

执行前应确认：

- 没有重要 FlowRun、Environment Setup 或终端交互正在进行；
- 数据库迁移已经评审，代码与 Schema 的前后兼容策略清楚；
- Docker、磁盘空间和外部软件源可用；
- `.env` 中的生产/共享环境密钥已经替换；
- 可以接受 API、Worker、Runtime Provider 和 Web 的短暂停机或断连。

```bash
make rebuild-deploy
```

该命令会无缓存重建 Sandbox、Dependency Builder、OpenHands、四个平台服务镜像和 Web，然后重新创建完整
Compose 栈。它保留 `postgres-data`、`artifacts` 和宿主机 Workspace，但会重启控制面。已经运行的
FlowRun generation 不是 Compose 共享服务，不会因为此命令被原地换镜像；连接仍可能因控制面重启而中断。

不要为普通部署执行 `docker compose down -v`。`-v` 会删除 PostgreSQL 和 Artifact named volume；
FlowRun 外置状态还需要按其独立保留/删除规则处理。

## 5. 标准开发、验证和部署流程

### 5.1 首次准备

要求：Docker、Node.js 22+、pnpm 10.33.4、Python 3.12、uv 0.7.8。

```bash
cp .env.example .env
openssl rand -hex 32
openssl rand -hex 32
make install
```

把两次生成的不同随机值分别写入 `DOCKER_CONTROLLER_API_KEY` 和
`DOCKER_CONTROLLER_WORKER_API_KEY`。生产或共享环境还必须替换数据库密码、
`CREDENTIALS_MASTER_KEY` 和 `OPENHANDS_SESSION_API_KEY`，并明确选择：

```dotenv
SANDBOX_RUNTIME_NETWORK_MODE=isolated
# 或在确实需要访问外部模型/MCP 时使用 egress
```

先检查 Compose 能否正确展开：

```bash
docker compose --env-file .env -f infra/compose.yaml config --services
```

### 5.2 本地分进程开发

分别在三个终端运行：

```bash
make api-dev
make worker-dev
make web-dev
```

常规完整本地栈：

```bash
make infra-up
```

访问 `http://localhost:5173`，API 健康检查为 `http://localhost:8080/health`。

### 5.3 验证门禁

| 变更 | 至少执行 |
| --- | --- |
| Web | `make web-check` |
| 平台 Python | `make api-check` |
| Migration | `make migration-check` |
| Compose/环境变量/权限 | `make compose-check` |
| 平台 Dockerfile/原生依赖 | `make platform-image-check` |
| OpenHands Runtime | `make openhands-contract-check` 和 `make openhands-smoke` |
| Sandbox | 完整栈启动后执行 `make sandbox-smoke` |
| 跨前后端产品闭环 | `make e2e` |
| 常规前后端提交 | `make check`，再按上述范围追加专项检查 |

`make check` 只包含 API 检查、Web 检查和 Compose 检查，不包含 migration、镜像 Smoke 或 E2E。

### 5.4 常用定向部署

Web：

```bash
docker compose --env-file .env -f infra/compose.yaml build --no-cache web
docker compose --env-file .env -f infra/compose.yaml \
  up -d --no-deps --force-recreate web
curl -I http://127.0.0.1:5173/
```

API：

```bash
docker compose --env-file .env -f infra/compose.yaml build --no-cache api
docker compose --env-file .env -f infra/compose.yaml \
  up -d --no-deps --force-recreate api
curl -fsS http://127.0.0.1:8080/health
```

Worker：

```bash
docker compose --env-file .env -f infra/compose.yaml build --no-cache worker
docker compose --env-file .env -f infra/compose.yaml \
  up -d --no-deps --force-recreate worker
docker compose --env-file .env -f infra/compose.yaml logs --tail=100 worker
```

Runtime Provider：

```bash
docker compose --env-file .env -f infra/compose.yaml build --no-cache runtime-provider
docker compose --env-file .env -f infra/compose.yaml \
  up -d --no-deps --force-recreate runtime-provider
docker compose --env-file .env -f infra/compose.yaml ps runtime-provider
```

### 5.5 独立动态执行镜像

OpenHands Runtime 基础镜像：

```bash
docker build --no-cache -f infra/openhands/Dockerfile \
  -t flowweave-openhands-runtime:1 .
make openhands-contract-check
make openhands-smoke
```

重建基础镜像不会改变已经发布的 Environment Runtime digest，也不会替换正在运行的 generation。业务要
使用新 OpenHands 基线时，应创建/发布新的 Environment Version，通过构建和探针冻结新的 Runtime Image
digest，再把 Flow 更新到该版本；历史 Snapshot/Run 继续引用旧版本。

Sandbox：

```bash
docker build --no-cache -f infra/sandbox/python/Dockerfile \
  -t flowweave-sandbox-python:1 .
docker build --no-cache -f infra/sandbox/javascript/Dockerfile \
  -t flowweave-sandbox-javascript:1 .
make sandbox-smoke
```

Dependency Builder：

```bash
docker build --no-cache -f infra/dependency-builder/Dockerfile \
  -t flowweave-dependency-builder:1 .
```

这些 tag 只被之后创建的动态容器使用，已有容器不会自动更新。

## 6. 部署后检查

```bash
docker compose --env-file .env -f infra/compose.yaml ps -a
curl -fsS http://127.0.0.1:8080/health
curl -fsS http://127.0.0.1:8080/health/ready
curl -I http://127.0.0.1:5173/
docker compose --env-file .env -f infra/compose.yaml \
  logs --tail=100 api worker web runtime-provider
docker compose --env-file .env -f infra/compose.yaml exec -T api alembic current
```

期望：

- `postgres`、`runtime-provider`、`api` 为 healthy；
- `worker`、`web` 为 Up；
- `workspace-init`、`migration` 为 `Exited (0)`；
- API 的 liveness/readiness 均成功；
- 数据库 revision 等于仓库唯一 Alembic head。

`dependency-builder-image` 是镜像准备/动态任务辅助项，不是需要长期保持 Up 的业务服务；实际依赖构建由
Runtime Provider 按需创建独立容器。

## 7. 停止、失败处理和回滚边界

保留数据停止：

```bash
make infra-down
```

重新启动：

```bash
make infra-up
```

构建遇到 Debian、npm、PyPI 或固定 OpenHands 源码归档的临时 `502`/超时时，优先原样重试，不要为绕过
下载失败而解除版本或 digest 锁定。

当前仓库提供的是本地单 Docker Host Compose 交付方式，没有完整的生产镜像仓库推送、版本化 Release、
自动部署和一键回滚流水线。回滚前必须同时判断代码、数据库 Schema 和已发布 Environment Runtime digest
是否兼容；Alembic 已升级后，不应直接回到不兼容的旧 API/Worker 镜像。生产化前至少还应补齐：

- 镜像 registry、不可变 release tag/digest 和 SBOM/扫描；
- 备份恢复、迁移前检查和明确的回滚 Runbook；
- API 认证、组织/RBAC、生产密钥管理；
- Runtime Provider 独立 Docker 主机/VM、mTLS、出口防火墙或代理；
- 结构化日志、指标、追踪和告警；
- `.dockerignore`，避免把本地 `.env`、虚拟环境、缓存和 Workspace 发送到 Docker build context。

## 8. 文档导航与事实优先级

从本文开始，根据问题进入下列文档：

| 想了解的内容 | 文档 |
| --- | --- |
| 最短启动方式和产品能力 | [根 README](../README.md) |
| 更完整的本地构建/单服务命令 | [本地编译、打包与部署](local-build-and-deploy.md) |
| 本地开发、Workspace、MCP/Skill 和安全说明 | [Development](development.md) |
| 当前 FlowRun/OpenHands Runtime 唯一目标架构 | [FlowRun Runtime 设计](flowrun-openhands-runtime-design.md) |
| Runtime 重构切片和最终验收证据 | [FlowRun Runtime 进度](flowrun-runtime-task-progress.md) |
| 公共和跨进程契约 | [Contracts](../contracts/README.md) |
| 产品/架构背景材料 | [系统设计](system-design.md) 与其他 `docs/` 设计文档 |

出现冲突时按以下顺序判断当前事实：

1. 当前源码、迁移、Compose、Dockerfile 和实际运行探针；
2. `flowrun-openhands-runtime-design.md` 与 `flowrun-runtime-task-progress.md`；
3. 本文和 `local-build-and-deploy.md`；
4. README、`development.md`、`system-design.md` 和较早设计稿。

部分早期文档仍包含“共享 OpenHands Agent Server”“平台保存 Conversation 消息/cursor”“节点绑定环境”
等旧描述。当前实现已经切换到 FlowRun 级 Runtime、OpenHands 原生 Conversation 事实源和 Flow 级
Environment Version 绑定；阅读旧文档时应以上述事实优先级为准。
