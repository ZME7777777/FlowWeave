# FlowWeave npm CLI 与页面 Skill

## 安装

`@flowweave-ai/cli` 是不依赖 Python、Docker 或 FlowWeave 源码的 Node.js 22+ 命令行客户端：

```bash
npm install -g @flowweave-ai/cli
flowweave config init --base-url https://host.example/flowweave
flowweave health --ready
```

配置只保存基础 URL 到 `~/.config/flowweave/config.json`；可以用 `FLOWWEAVE_CONFIG_PATH` 覆盖。当前平台不提供 CLI 登录，不能也不应添加 `auth login`。

> 包发布需要拥有 npm `@flowweave-ai` scope 的权限。本仓库只提供可发布包与本地安装验证，不会自动执行 `npm publish`。

## 设计方案

CLI 分为两层：第一层以页面/业务域提供中文快捷命令，覆盖用户最常用的原子操作；第二层以 `api`、`upload`、`ws` 直接映射在线 OpenAPI 的 REST、multipart 与 WebSocket 契约。这样后端新增原子接口时，用户无需等待 CLI 再次发布。

配置只有 `base_url`，默认保存到 `~/.config/flowweave/config.json`。客户端会保留部署前缀，并自动补全 `/api/v1`。平台当前不要求 CLI 登录，因此不实现 `auth login`；模型供应商的 Codex OAuth 只属于 `model oauth-*` 业务操作。

Skill 以产品页面拆分，分别指导节点资产、能力、环境、流程、FlowRun、模型服务和 Agent 工作台。所有页面 Skill 都以 `flowweave` 平台基准 Skill 为前置知识：新会话会先获得平台资源关系、安装配置、端到端生命周期、OpenAPI 发现方式和安全边界，再执行页面操作。所有写请求先通过 FlowWeave 控制面；CLI 与 Skill 均不直接连接 Docker、Runtime Provider、数据库或 OpenHands 私有接口。

## 页面原子命令

| 页面/业务域 | 命令 | 常用原子操作 |
| --- | --- | --- |
| 节点资产 | `node`、`node-directory` | list/get/create/update/delete、目录创建 |
| 能力仓库 | `capability` | list/validate/commit/import |
| 终端环境 | `environment` | create/update/delete、setup/publish/stop/version-delete |
| 流程编排 | `flow` | list/get/create/update/validate/delete |
| FlowRun | `run`、`api`、`upload` | start/list/get/runtime/events/replace/cancel/complete/delete；节点执行、门禁与产物 |
| 大模型配置 | `model` | list/create/update/delete、发现模型、连接测试、Codex OAuth 设备授权 |
| Agent 工作台 | `agent` | 默认工作区、会话创建/查询、消息发送、中断/恢复 |

所有快捷命令都只是已存在 REST 原子接口的路径映射。JSON 请求体始终应以在线 OpenAPI 为准：

```bash
flowweave openapi --paths
flowweave node create --data-file ./node.json --dry-run
flowweave capability import --type SKILL --file ./my-skill.zip
flowweave environment setup <environment-id>
flowweave flow validate <flow-id>
flowweave run start --flow <flow-id> --environment-version <ready-version-id>
```

`api`、`upload` 与 `ws` 是完整逃生舱，覆盖当前与后续的所有 JSON REST、multipart 和 WebSocket 原子接口；新接口不必等待 CLI 发布。具有副作用的命令应先加 `--dry-run`，目标 ID 不清楚时先读取资源。

## 安装全部页面 Skill

本仓库按 UI/业务页面提供独立 skill：

- `flowweave`：平台基准，所有页面 Skill 的共同前置知识；
- `flowweave-node-assets`：节点资产与目录；
- `flowweave-capabilities`：Skill、MCP、Plugin、Context 等能力；
- `flowweave-environments`：终端环境和不可变版本；
- `flowweave-flows`：流程定义、控制边和端口映射；
- `flowweave-runs`：手动/自动 FlowRun 与 Runtime；
- `flowweave-agent-workspace`：Agent Workspace、会话和工作区。
- `flowweave-model-providers`：大模型服务、连接测试与 Codex OAuth。
- `flowweave-flowrun-workbench`：节点执行、产物、门禁与自动运行。

从公开仓库安装全部页面 skill：

```bash
npx skills add ZME7777777/FlowWeave -g -y --full-depth
```

如果只需要一项，可在上述命令追加 `--skill <skill-name>`。安装后的 skill 使用已配置的 `flowweave` CLI；它们不直接操作 Docker、Runtime Provider、数据库或 OpenHands 私有 API。
