---
name: flowweave-agent-workspace
description: 管理 FlowWeave Agent Workspace、会话、消息、运行状态、工作目录、附件或能力绑定；FlowRun 节点会话转 flowweave-flowrun-workbench。
---

# FlowWeave Agent 工作台

**开始前先完整阅读 `../flowweave/SKILL.md`。** 本 Skill 是顶层交互式 Agent Workspace 的操作手册。它不等于 FlowRun 中某个节点的会话；若用户正在处理流程节点执行，转 FlowRun 工作台。

## 对象关系

默认 Agent Workspace 是当前登录用户的平台受控工作区。其下有 Conversation binding、消息/事件、附件、工作目录、能力绑定与 Runtime 概览；不同用户的 Agent 项目与会话相互隔离。所有身份和路径都必须由平台读取结果给出，不从聊天标题、前端 URL 或旧固定工作区根推断。

```bash
flowweave agent default
flowweave agent workspace <workspace-id>
flowweave agent runtime <workspace-id>
flowweave agent conversations <workspace-id>
```

## 会话闭环

1. 从 `agent default` 或用户提供的 ID 取得 workspace，读取其状态与现有会话。
2. 创建会话时按 OpenAPI schema 准备 JSON，写后保存返回的 binding ID：

   ```bash
   flowweave agent create <workspace-id> --data-file ./conversation.json
   flowweave agent conversation <workspace-id> <binding-id>
   ```

3. 发送消息后读取会话与事件。请求返回 `202` 说明异步受理，应观察状态/事件而不是重复发送：

   ```bash
   flowweave agent send <workspace-id> <binding-id> --data-file ./message.json
   flowweave api get /agent-workspaces/<workspace-id>/conversations/<binding-id>/events
   ```

4. 只有用户明确要求时才中断/恢复：`agent interrupt`、`agent resume`；操作后重新读取会话确认状态。

## 工作目录、附件、能力与高级操作

工作目录、附件上传、能力绑定、MCP readiness、模型选择、pending confirmation、fork、condense、rerun 和 terminal 都是同一工作区域的原子 API。先读取目标会话/工作区和在线 OpenAPI，再使用 `flowweave api` 或 `upload`。能力必须是平台已治理的版本，先转 `flowweave-capabilities` 导入或定位；不能将文件复制进 Runtime 作为绑定。

对确认、附件、删除、停止或 fork 等改变状态的操作，先核对真实 workspace/binding ID 与用户意图；不得直接使用 Docker、Runtime Provider 或 OpenHands 私有接口。

需要引用既有对话时，先读取该 binding 的正式事件，再在创建或发送消息的 `references` 数组中传 `{"event_id": "…", "content": "…"}`。引用是用户选择的上下文，不会授予其他 Workspace 的访问权限；不可按页面文本、历史 URL 或猜测 event ID 构造引用。

删除逻辑工作目录前，先用 `flowweave agent work-directories <workspace-id>` 读取真实 ID，再执行 `flowweave agent work-directory-delete <workspace-id> <work-directory-id> --dry-run`。被 Conversation 冻结引用的工作目录会被平台拒绝删除。

删除文件或目录树使用 `flowweave agent file-delete <workspace-id> --path <runtime-path> [--path <runtime-path> ...] [--binding <binding-id>] [--work-directory <directory-id>] --dry-run`。路径必须来自当前登录用户的 Workspace 详情；不要硬编码 `/runtime/workspace/project` 或从其他用户、历史 binding 猜路径。平台只允许当前范围内的普通文件或目录，并会递归删除目录。不得尝试删除工作区根、隐藏路径、符号链接或会话私有附件。确认 dry-run 中的 `paths` 数组和范围 query 后再执行；同时选择父目录与其后代时，平台只返回实际删除的最高层路径。
