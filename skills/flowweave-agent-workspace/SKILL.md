---
name: flowweave-agent-workspace
description: 管理 FlowWeave Agent Workspace、会话、消息、归档、未读、工作目录、附件、能力升级与认证同步；子会话转 flowweave-agent-children，自动化转 flowweave-agent-schedules，节点会话转 flowweave-flowrun-workbench。
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

`agent readiness <workspace-id> <binding-id>` 读取当前输入状态，`agent context` 读取正式上下文指标，未知指标等待后续读取。`agent pending-question` 读取原生 `ask_user` 待回答问题，按返回的问题与服务端消息 schema 提交用户答案。`agent condense` 只在可写且 idle/paused 时发起原生压缩，返回后观察正式压缩事件。压缩不创建用户消息，完成前暂缓发送和重复压缩。

### 会话能力与认证同步

读取当前会话及 `capability configurable` 后，以 `{"capability_version_ids":["<version-id>"]}` 调用 `agent capabilities-register <workspace-id> <binding-id> --data-file ./.tmp/capabilities.json --dry-run`。单批最多 100 个版本，支持新装 Skill/MCP/Plugin 与同一独立 Skill 的更高版本，Context、Agent Definition 和 Hook 保持创建时冻结。运行中的会话须等回复完成或暂停。

HTTP 成功仍可能是部分加载成功，必须检查 `completed`、`loaded_capability_version_ids`、`already_loaded_capability_version_ids` 与 `failure`。重新读取会话，仅重试尚未加载的版本，不删除已成功项或创建新会话来替代恢复。能力继承和分叉以正式配置与平台版本记录为准，不能把父会话当前版本当作历史分支冻结版本。

`agent credentials <workspace-id> <binding-id>` 读取凭据同步状态，再以 `{"credential_ids":["<credential-id>"]}` 调用 `agent credentials-sync`。只传当前用户的受管 ID，不读取或拼接明文 Secret。原生 Task 子会话使用 `flowweave-agent-children` 的父范围入口。

### 归档与未读

`agent activity` 读取活动投影，`agent management-workspaces` 读取管理分组。`agent archive|restore <workspace-id> <binding-id>` 管理单条会话，`agent work-directory-archive|work-directory-restore <workspace-id> <directory-id>` 管理整组工作区。工作区归档会原子归档其全部正式会话，恢复工作区前须向用户确认整组恢复。恢复单条会话会同时恢复所属工作区，其他会话保持原归档状态。

`agent read|unread <workspace-id> <binding-id>` 写入用户隔离的服务端未读投影，手工未读使用普通 `MANUAL` 来源。只读查看和活动列表预览不自动标记已读。批量归档、后台删除任务及其进度通过 `/conversation-management/archive`、`/conversation-management/deletion-jobs` 的公开 API 按需操作。

## 工作目录、附件、能力与高级操作

工作目录、附件上传、能力绑定、MCP readiness、模型选择、pending confirmation、fork、condense、rerun 和 terminal 都是同一工作区域的原子 API。先读取目标会话/工作区和在线 OpenAPI，再使用 `flowweave api` 或 `upload`。能力必须是平台已治理的版本，先转 `flowweave-capabilities` 导入或定位；不能将文件复制进 Runtime 作为绑定。

### 可恢复上传、临时侧栏与 Runtime replacement

大附件采用公开的可恢复上传协议：先以 `api post /agent-workspaces/<workspace-id>/attachments/uploads --data-file ./.tmp/upload.json` 创建（工作区文件用 `/workspace/uploads`，既有 binding 附件用 `/conversations/<binding-id>/attachments/uploads`）；只用响应返回的 upload ID 和未完成分片号调用 `upload put .../parts/<part-number> --file file=./part.bin`，再 `api post .../complete`。用 `api get .../uploads/<upload-id>` 读取状态，只有用户明确放弃时才 DELETE；草稿附件清理由平台 draft UUID owner 约束，不能用别的 binding 或路径代替。

临时侧栏聊天是主 binding 的受限、一小时 TTL 子会话，不会出现在普通会话列表。先读取主 binding 和被引用的正式事件，再使用 `agent sidebar-create <workspace-id> <source-binding-id> -H 'Idempotency-Key: <uuid>' --data-file ./.tmp/sidebar.json --dry-run`。请求体中的 `conversation_id`、内容、附件及正式事件引用均必须来自公开读取结果；模型配置继承主会话，不得再传模型供应商、模型名称或推理程度字段。用 `agent sidebar` 读取、`agent sidebar-close` 显式关闭，过期后不得重试发送。

切换页面或恢复连接时，先用 `agent sidebar-current <workspace-id> <source-binding-id>` 查询当前仍有效的侧栏会话，避免重复创建。

Runtime replacement 会替换该 Workspace 的受管计算载体。先读取 `agent runtime <workspace-id>` 并取得用户对精确 Workspace 的明确确认，才执行 `agent runtime-replace <workspace-id> -H 'Idempotency-Key: <uuid>' --dry-run`；收到 `202` 后轮询 Runtime，绝不通过 Docker、Runtime Provider 或私有 OpenHands 路由替代该操作。

工作区目录与 Git 历史只能通过公开只读接口浏览，路径、仓库和 commit 都来自此前响应；不要把本机文件系统当作 Agent Workspace 的事实源：

```bash
flowweave agent workspace-directory <workspace-id> --parent-path <returned-path> --limit 100
flowweave agent workspace-git-repositories <workspace-id>
flowweave agent workspace-git-log <workspace-id> --repository-path <returned-repository-path>
flowweave agent workspace-git-commit <workspace-id> --repository-path <returned-repository-path> --commit <returned-commit>
flowweave agent workspace-git-diff <workspace-id> --repository-path <returned-repository-path> --commit <returned-commit> --path <returned-file-path>
```

默认优先使用增量目录读取；只有用户确实需要完整索引时才调用 `flowweave agent workspace-details <workspace-id> --full-index`，避免不必要的大响应。

分支、工作区改动和远端状态使用同范围 `/workspace/git/branches`、`/changes`、`/working-diff` 读取。切换分支与同步远端分别调用 `/workspace/git/switch`、`/sync`，先读取服务端返回的仓库和分支，按 OpenAPI 审阅请求并取得对应操作授权。Git 身份使用 `/settings/git` 读取，有缺失时只允许从当前用户资料同步，不手工设置作者或邮箱。

创建普通文件或目录使用 `agent workspace-entry-create <workspace-id> --data-file ./.tmp/entry.json --dry-run`；请求体仅使用在线 schema 的 `parent_path`、`name` 和 `kind`，写后重新读取目录。会话的 `agent hydration <workspace-id> <binding-id>` 和 `agent head <workspace-id> <binding-id>` 是原生状态读取，不会创建、续接或改变会话。

对确认、附件、删除、停止或 fork 等改变状态的操作，先核对真实 workspace/binding ID 与用户意图；不得直接使用 Docker、Runtime Provider 或 OpenHands 私有接口。

需要引用既有对话时，先读取来源 binding 与正式事件，再在创建或发送消息的 `references` 数组中传 `{"source_binding_id": "…", "source_event_id": "…", "selection": {"start": 0, "end": 10}}`；`source_event_id` 和 `selection` 均可省略。引用是用户选择的上下文，不会授予其他 Workspace 的访问权限；不可按页面文本、历史 URL 或猜测 binding/event ID 构造引用。

置顶列表使用 `agent pinned-conversations <workspace-id>`；对明确读取到的会话使用 `agent pin <workspace-id> <binding-id>` 或 `agent unpin <workspace-id> <binding-id>`。批量会话管理先用 `agent conversation-management <workspace-id> [--work-directory <directory-id>]` 读取候选；永久删除只能在用户确认每个 binding 后使用 `agent conversation-management-delete <workspace-id> --id <binding-id> [--id <binding-id> ...] --dry-run`，运行中的会话会由平台安全跳过。

删除逻辑工作目录前，先用 `flowweave agent work-directories <workspace-id>` 读取真实 ID，再执行 `flowweave agent work-directory-delete <workspace-id> <work-directory-id> --dry-run`。删除会物理级联清理该分组所有历史版本关联的会话、私有附件与目录版本，属于不可恢复操作；必须先得到用户对精确目录 ID 的明确授权。不得把“会话仍引用”当作绕过删除的理由，也不得改用 Runtime 或数据库直接删除。

用户要求保留磁盘文件时，加 `-q delete_files=false` 选择移除模式。该模式仍删除平台记录和 OpenHands 原生会话，磁盘文件和附件文件保留，执行前向用户说明会话删除范围。

删除文件或目录树使用 `flowweave agent file-delete <workspace-id> --path <runtime-path> [--path <runtime-path> ...] [--binding <binding-id>] [--work-directory <directory-id>] --dry-run`。路径必须来自当前登录用户的 Workspace 详情；不要硬编码旧共享项目根或从其他用户、历史 binding 猜路径。平台只允许当前范围内的普通文件或目录，并会递归删除目录。不得尝试删除工作区根、隐藏路径、符号链接或会话私有附件。确认 dry-run 中的 `paths` 数组和范围 query 后再执行；同时选择父目录与其后代时，平台只返回实际删除的最高层路径。
