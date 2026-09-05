---
name: flowweave
description: FlowWeave 平台基准技能。用于建立统一的用户登录、资源模型、CLI 配置与安全操作方式，再创建节点、能力、环境、流程、周期调度、FlowRun 或 Agent 工作区。
---

# FlowWeave 平台基准

这是所有 `flowweave-*` 页面 Skill 的共同前置知识。无论任务只涉及一个页面，先阅读并遵守本 Skill，再加载对应页面 Skill。目标是让 Agent 从零开始理解平台并通过 `flowweave` CLI 完成用户授权的操作，而不是直接操作底层执行器。

## 平台是什么

FlowWeave 是 Agent 执行的**治理控制面**。它管理业务资源、版本、策略、审计与运行投影；OpenHands 只是受平台调度的执行依赖。不能为了完成任务绕过平台去调用 Docker、数据库、Runtime Provider 或 OpenHands 私有 API。

核心对象及关系如下：

```text
能力（Skill / MCP / Plugin / Context）──可追溯版本、digest──┐
节点资产（输入/输出字段、提示词、目录）──被流程节点引用──┼─> 流程 Flow（可编辑模板）
终端环境 Environment ──发布──> READY Environment Version ───┘        │
                                                                        ▼
                         FlowRun = Flow + 显式选择的 READY 环境版本 + 运行快照
                                                                        │
                   NodeRun / Attempt / Artifact / Gate / 节点会话 ──────┘
```

Agent Workspace 是另一条“交互式 Agent 工作台”路径：它有自己的默认工作区、会话绑定、消息、附件、能力绑定和 Runtime，不等同于某个 FlowRun 的节点会话。

重要边界：

- **Flow 是模板，不绑定环境版本。** 每次创建 FlowRun 都要显式选取一个已发布且状态为 `READY` 的 Environment Version。
- **Environment Version 是不可变快照。** 修改环境应新开 Setup Session 并发布新版本，不能把运行中的版本当作可编辑配置。
- **能力必须经平台导入和版本治理。** 不直接把文件复制进 Runtime；版本、digest、blob/hash 与 Runtime Manifest 是审计依据。
- **运行事实来自平台记录。** `run` 详情中的 Snapshot、NodeRun、Attempt、Artifact、事件和 Runtime 是目标身份来源；不要按页面顺序、名称或时间猜 ID。

## 从新环境开始

先安装 CLI 和 Skills、写入平台地址，再登录平台用户。模型供应商 OAuth 仅用于模型授权，不等于 CLI 用户登录。

```bash
npm install -g @flowweave-ai/cli
npx skills add ZME7777777/FlowWeave -g -y
flowweave config init --base-url https://host.example/flowweave
flowweave auth login
flowweave health --ready
flowweave config show
```

`--base-url` 必须包含实际部署前缀，例如 `/flowweave`。基础配置只保存 URL；登录会话单独保存在权限为 `0600`、且与该 URL 精确绑定的本地认证文件中。默认交互式登录会隐藏密码；非交互环境只用 `--password-stdin`，不得把密码、会话 Cookie 或 token 写进命令参数、Skill、请求文件或日志。用 `flowweave auth status` 核对当前身份，用 `flowweave auth logout` 撤销会话。若 CLI 尚未可从 npm 获取，先确认包的发布可见性；不要改用未审计的本地脚本替代发行包。

## 统一调用方式

页面高频操作有快捷命令：`node`、`node-directory`、`capability`、`environment`、`credential`、`flow`、`run`、`schedule`、`model`、`agent`。`credential` 是网站凭据资源，不是 `auth` 用户登录。它们之外的原子操作并未缺失：按以下优先级使用通用入口。

```bash
# 先查看当前服务真正暴露的路径和 schema
flowweave openapi --paths

# 相对路径会自动拼接 /api/v1；用 --data-file 保存复杂 JSON
flowweave api get /flows
flowweave api post /some-path --data-file ./request.json --dry-run

# multipart 上传与 WebSocket
flowweave upload post /some-upload --file file=./input.pdf --form field_key=source
flowweave ws /some-stream --max-messages 20
```

在线 OpenAPI 与服务端返回是路径、字段、枚举、状态和响应结构的唯一权威。不要根据文档示例臆造字段；先以 `--dry-run` 审核最终 URL/方法/JSON，再执行写操作。对会被网络重试的写命令，用唯一的 `-H 'Idempotency-Key: <uuid>'`；同一逻辑操作重试时复用同一个 key。

## 通用工作闭环

1. **定位与读取**：先列出或读取资源，确认真实 ID、状态、版本与引用关系。
2. **检查前置条件**：例如 FlowRun 需要已校验的 Flow 与 READY 环境版本；执行节点需要来自 Run 详情的 Attempt 和 `row_version`。
3. **最小写入**：只修改用户指明的资源，复杂请求存为 JSON 文件；不知道请求体时先看 OpenAPI。
4. **写后验证**：重新读取目标资源，必要时读取事件、Runtime、产物或校验结果。异步请求返回 `202` 时，轮询对应的读取接口，不要重复发起创建。
5. **失败诊断**：保留 HTTP 错误、资源 ID 和事件上下文；先读状态再决定重试、取消或替换。未经用户明确授权，不删除、取消、完成、拒绝、撤销或替换已有资源。

## 维护性删除与版本说明

删除前先读取精确资源 ID，并先用 `--dry-run` 核对请求。节点目录删除会保留节点资产和子目录并提升到父级，若会造成同名目录冲突则被平台拒绝；不要把“删除目录”理解成删除其中全部节点。认证可用 `flowweave credential delete-many --id <id> --id <id>` 批量删除，Secret 一经删除无法恢复。

Agent Workspace 的工作目录删除由已绑定会话保护；文件删除只接受当前授权范围内的普通文件或空目录，不能删除工作区根、隐藏路径、符号链接或会话私有附件。环境发布可以用 `flowweave environment publish <setup-session-id> --description '说明'` 记录可选版本说明；该说明随发布版本冻结，后续不能修改。

## 生命周期推荐顺序

当用户从空白开始创建可运行流程，按依赖顺序协作：

1. 配置 CLI、登录并核对身份，再检查平台健康；需要模型时先配置、发现并测试模型供应商。
2. 创建或检查节点目录和节点资产，确定稳定输入/输出 `field_key`。
3. 导入并验证能力，保留平台返回的能力版本与 digest。
4. 创建终端环境，进入 Setup Session 配置，再发布为 READY Environment Version。
5. 使用节点资产创建 Flow，校验控制边与端口映射。
6. 使用该 Flow 和显式选择的 READY Environment Version 创建 FlowRun。
7. 在 FlowRun 工作台依据 Run 详情处理节点、输入产物、门禁、人工输出和运行诊断。

需要重复执行时，在 Flow、READY 环境版本、起点、启动配置与输入都已明确后创建周期调度；调度是独立配置，不替代手动创建 FlowRun，也不允许绕过运行前置条件。

用户只要求其中一项时，不强行创建全部依赖；应先说明缺失的必要条件并读取现有资源。

## 页面 Skill 路由

- 节点目录、节点、字段或执行提示词：`flowweave-node-assets`
- Skill、MCP、Plugin、Context 的导入/版本/检测：`flowweave-capabilities`
- 终端环境、Setup Session、不可变版本：`flowweave-environments`
- Flow 节点、控制边、端口映射与校验：`flowweave-flows`
- FlowRun 创建、状态、取消、完成、Runtime：`flowweave-runs`
- 周期 FlowRun 调度、暂停/恢复、手动触发和 occurrence：`flowweave-schedules`
- 节点执行、Attempt、Artifact、人工门禁与节点会话：`flowweave-flowrun-workbench`
- 大模型供应商、测试和 Codex OAuth：`flowweave-model-providers`
- 顶层 Agent Workspace、会话、消息、工作目录与附件：`flowweave-agent-workspace`
- CLI 用户登录、身份检查或退出：`flowweave-auth`
- 认证条目与批量删除：使用本 Skill 的 `credential` 快捷命令；不把 Secret 写入 Skill、命令历史或请求快照。

## 不可跨越的边界

- 不得通过 Docker、Runtime Provider、数据库或 OpenHands 私有端点绕过 FlowWeave。
- 不把密码、会话 Cookie、API Key、OAuth 令牌、恢复码或其他明文 Secret 写入 Skill、仓库、示例、日志或 shell 历史；CLI 认证文件只由 CLI 管理且必须保持 `0600`。
- 不以节点名、显示顺序、事件顺序推测 `run_id`、`node_run_id`、`attempt_id`、版本、cursor 或 `row_version`。
- 删除、取消、完成、拒绝、撤销、替换 Runtime 与覆盖更新均会改变平台状态；先读取目标，确认精确 ID 与用户意图。
