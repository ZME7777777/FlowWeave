---
name: flowweave-flowrun-workbench
description: 在 FlowWeave FlowRun 工作台处理节点执行、输入产物、门禁、人工输出、自动运行或节点会话；不直接操作底层 Runtime。
---

# FlowWeave FlowRun 工作台

**开始前先完整阅读 `../flowweave/SKILL.md`。** 本 Skill 操作一次已存在 FlowRun 中的执行投影：NodeRun、Attempt、Artifact、Gate、确认批次和节点会话。

## 身份与状态来源

每次动作先读取 Run。响应中的 Snapshot、节点、Attempt、Artifact 和 `row_version` 才是操作身份；不得按节点名、画布顺序、事件顺序或旧屏幕内容猜测。

```bash
flowweave run get <run-id>
flowweave run events <run-id>
flowweave run runtime <run-id>
```

针对单个 NodeRun，使用它在 Run 详情中出现的 ID：

```bash
flowweave api get /flow-runs/<run-id>/nodes/<node-run-id>
```

## 原子操作模式

CLI 对工作台的细粒度原子操作统一经 `api` / `upload` 完成。先在在线 OpenAPI 中读取请求 schema，再用真实 ID 和当前版本字段发送请求；写后立刻重新读取 NodeRun 或 FlowRun。

```bash
flowweave openapi --paths
flowweave api post /flow-runs/<run-id>/nodes/<flow-node-key>/runs --data-file ./start-node.json --dry-run
flowweave api put /node-attempts/<attempt-id>/input-bindings --data-file ./bindings.json
flowweave api post /node-attempts/<attempt-id>/confirm-start --data-file ./confirm.json
flowweave api post /node-attempts/<attempt-id>/human-input --data-file ./human-input.json
flowweave api post /node-attempts/<attempt-id>/manual-outputs --data-file ./outputs.json
```

常见工作顺序是：创建 NodeRun/Attempt → 上传或创建输入 Artifact → 绑定输入 → 使用当前 `row_version` 确认启动 → 观察事件/门禁 → 提交人工输入或输出 → 接受、拒绝、重试或取消。并非每个节点都会经历全部步骤；以它的状态和 OpenAPI schema 为准。

文件产物用平台上传接口，字段名须来自节点定义：

```bash
flowweave upload post /flow-runs/<run-id>/artifacts/upload \
  --file file=./input.pdf --form field_key=<field-key> --form display_name="输入文件"
```

节点专属输入上传使用 `/flow-runs/<run-id>/nodes/<flow-node-key>/input-artifacts/upload`。上传返回的 Artifact ID 必须再通过读取 Run/NodeRun 验证其归属与绑定。

## 门禁、会话与自动运行

接受/拒绝、retry gate、Runtime confirmation batch decision、人工输出、自动运行草稿/启动、节点会话等均是平台控制的状态机动作。每一步都先读当前 Attempt/批次/Run，并以 OpenAPI 取得真实 body；需要幂等保障时使用 `Idempotency-Key`。顶层聊天任务不要混用节点会话，转 `flowweave-agent-workspace`。

END gate 失败后，风险接受和补救 fork 都需要用户明确授权、当前 Attempt 的 `expected_state_version`，并应使用公开原子接口：`POST /node-attempts/<attempt-id>/accept-gate-risk`（还必须提供具体 `reason`）或 `POST /node-attempts/<attempt-id>/remediate-gate-failure`。先读取 gate evaluation；审查会话证据只能通过 `GET /node-attempts/<attempt-id>/gate-evaluations/<evaluation-id>/conversation/events` 读取，不得猜测评估、事件或版本 ID。

节点会话可在首条消息或后续消息的 `references` 数组中引用已验证的 conversation event；每项只包含来源 `event_id` 和给用户显示/发送的 `content`。引用前读取来源会话事件，最多传入服务端 schema 允许的数量；不得伪造、改写或把引用当作跨 Run/Workspace 的绕过授权方式。

如果状态卡住，先读取 Run、NodeRun、Runtime 和 events，保留失败上下文；不可进入 Docker 或 OpenHands 私有端点“手工完成”节点。取消或拒绝前必须有用户的明确授权。

## 节点会话工作区维护

删除节点会话工作区文件或目录前，从 Run 详情取得真实 `attempt_id`，再读取该 Attempt 的工作区范围。使用重复 `--path` 批量选择，并先 dry-run：

```bash
flowweave run workspace-delete <run-id> --attempt <attempt-id> \
  --path /runtime/workspace/project/result.txt \
  --path /runtime/workspace/project/cache \
  [--binding <binding-id>] [--work-directory <directory-id>] --dry-run
```

平台拒绝工作区根、范围外路径、隐藏路径、符号链接和特殊文件；目录会递归删除。删除 FlowRun 逻辑工作目录使用 `flowweave run work-directory-delete <run-id> --attempt <attempt-id> --work-directory <directory-id> --dry-run`。先读取目录 ID；仍被会话冻结版本引用时平台会拒绝删除。两类操作均只操作 FlowWeave 公开 API，不进入 Runtime 或宿主机直接删除。
