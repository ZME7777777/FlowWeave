# FlowWeave 平台概览

FlowWeave 是面向研发流程的 Agent 控制面。它把可复用的节点能力、模型服务、终端环境和流程定义组合成可审计的 FlowRun；OpenHands Runtime 负责实际会话、工具调用和执行。

## 它解决什么问题

- 用节点资产复用提示词、输入/输出契约、Skill、MCP 和模型选择。
- 用可视化流程组织节点、边、产物映射与开始/结束门禁。
- 用不可变 Snapshot、显式 Artifact Version 和人工确认确保每次运行可追溯。
- 用独立、可替换的 Runtime 容器运行 Agent，同时保留工作区和 OpenHands 持久状态。
- 通过 Runtime Provider 集中管理 Docker；API 和 Worker 不持有 Docker Socket。

## 领域模型

| 对象 | 作用 |
| --- | --- |
| Node Asset | 可复用的节点定义，声明模型、环境、能力和 I/O。 |
| Flow Definition | 节点实例、边、端口映射与 Gate 的可编辑流程。 |
| Environment Version | 从 Setup Session 发布的不可变运行环境，使用 image digest 锁定。 |
| FlowRun / Snapshot | 一次业务运行及其冻结配置。新 Snapshot 只追加，不改写历史。 |
| Node Run / Attempt | 节点的一次逻辑工作与其中每轮实际执行。Reject 会创建新的 Attempt。 |
| Artifact Version | 显式绑定给输入或产出的不可变内容版本。 |
| Conversation | 由 OpenHands 原生持有的会话和事件树；FlowWeave 只保存授权和定位事实。 |

边只提供产物映射候选，不会自动执行下游节点。START Gate 通过后仍需人工确认；END Gate 通过后仍需人工验收。

## 系统边界

```text
Browser → Web (React/Vite/Nginx) → API / stream-api (FastAPI)
                                      │
                         PostgreSQL + Artifact/Workspace storage
                                      │
                                  Worker
                                      │
                           Runtime Provider ── Docker Engine
                                      │
                    Per-FlowRun OpenHands Agent Server Runtime
```

- **Web** 提供流程、环境、能力和 Agent Workspace 界面。
- **API** 提供 REST、SSE 和受控会话入口；**stream-api** 专门承载长 WebSocket。
- **Worker** 使用持久化任务、租约与 fencing 处理异步工作和恢复。
- **Runtime Provider** 是唯一拥有 Docker Socket 的服务，创建、检查、替换和回收隔离 Runtime。
- **PostgreSQL** 是领域状态、任务和审计的事实源；OpenHands 自己持有 Conversation/Event 状态。

## 仓库结构

```text
apps/web/              React + TypeScript 前端
services/platform/     FastAPI API、Worker、Runtime Provider、Alembic 迁移
infra/                 Compose 与受控镜像 Dockerfile
contracts/             跨进程 JSON Schema
packages/cli/          Node.js CLI 包
skills/                FlowWeave 能力包
scripts/               检查、远端预检和运维脚本
```

## 运行时原则

1. FlowWeave 治理，OpenHands 执行。不要以私有协议或提示词模拟 OpenHands 已有的生命周期能力。
2. 用户选择并发布 Environment Version；运行时只使用已冻结的 image digest。
3. Runtime 容器可被替换；工作区、Conversation/Event 持久化和 Secret Reference 不随容器丢失。
4. 明文密钥不写入镜像、Snapshot、日志或浏览器响应。
5. 生产部署按变更范围更新；不要用删除 volume 或 Workspace 来处理发布故障。

继续阅读：[本地启动](getting-started.md)、[环境配置](environment-reference.md)、[部署与运维](deployment.md)。
