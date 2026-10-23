# @flowweave-ai/cli

`@flowweave-ai/cli` 是可独立安装的 FlowWeave 平台命令行客户端，要求 Node.js 22 或更高版本。包中不包含 Python、Docker 或平台源码依赖。

```bash
npm install -g @flowweave-ai/cli
flowweave config init --base-url https://host.example/flowweave
flowweave auth login
flowweave health --ready
```

配置只保存基础 URL 到 `~/.config/flowweave/config.json`（可由 `FLOWWEAVE_CONFIG_PATH` 覆盖）。登录会话单独保存到同目录的 `auth.json`（可由 `FLOWWEAVE_AUTH_PATH` 覆盖），文件权限为 `0600`，并且只会发送给登录时的同一平台地址。使用 `flowweave auth status` 检查当前身份，使用 `flowweave auth logout` 撤销服务端会话并删除本地文件。默认登录会隐藏密码输入；非交互环境使用 `--password-stdin`，不要把密码写进命令参数、脚本或日志。

页面域命令包括 `node`、`node-directory`、`capability`、`event-trigger`、`environment`、`credential`、`flow`、`run`、`schedule`、`model` 和 `agent`。它们分别覆盖节点资产、能力仓库、运行事件触发器、终端环境、网站认证条目、流程编排、FlowRun、FlowRun 周期调度、大模型配置与 Agent 工作台的常用原子操作。`credential` 管理网站凭据，不等于 `auth` 用户登录；Agent 会话定时任务通过 `agent schedules` 与 `agent schedule-*` 管理，不能与顶层 `schedule` 混用。每个命令的 JSON 请求体与在线 OpenAPI 一致；运行 `flowweave <域> --help` 查看映射。

工作区浏览支持按需详情、增量目录、受授权的 Git 历史/提交/Diff 与创建普通文件或目录：使用 `agent workspace-details [--full-index]|workspace-directory|workspace-git-repositories|workspace-git-log|workspace-git-commit|workspace-git-diff|workspace-entry-create` 处理顶层 Agent Workspace；在 FlowRun 节点范围内使用对应的 `run workspace-* <run-id> --attempt <attempt-id>`。`--full-index` 只在用户确实需要完整索引时使用，默认优先按目录读取。Git 操作只接受由工作区 API 返回的 `repository_path`、commit 和文件路径，不能从本地或显示名称猜测。`agent hydration|head` 与 `run hydration|head` 读取会话原生状态；它们不修改 Conversation。

连续自动记录的可移植初始配置使用 `run automatic-config-export <parent-run-id> --data-file ./.tmp/export.json` 和 `run automatic-config-import <parent-run-id> --data-file ./.tmp/import.json`。导出只接受已读取的连续记录 ID；导入会创建记录，先 dry-run 审阅格式版本、起始节点与节点计划。遇到明确的原生完成动作未投影时，才可在读取当前 Attempt 后使用 `run reconcile-runtime-completion <attempt-id> --data '{"expected_state_version": 3}'`；该操作必须携带当前状态版本，不能用来猜测或强制完成运行。

常用命令示例：

`flowweave node-directory delete-many --id <directory-id> --id <directory-id> --dry-run`、`flowweave credential delete-many --id <credential-id> --id <credential-id>`、`flowweave environment publish <setup-session-id> --description '升级 Python 依赖'`、`flowweave agent file-delete <workspace-id> --path <workspace-api-returned-path>`、`flowweave run workspace-delete <run-id> --attempt <attempt-id> --path <attempt-workspace-api-returned-path>`。

节点目录批量删除、Agent Workspace 文件树删除和 FlowRun 节点工作区删除都使用 JSON 数组请求体。重复传入 `--id` 或 `--path` 即可批量选择；先读取真实资源与路径，并用 `--dry-run` 核对 DELETE URL、范围 query 和请求体。FlowRun 工作目录删除使用 `flowweave run work-directory-delete <run-id> --attempt <attempt-id> --work-directory <directory-id>`。

读取 FlowRun 中的记录使用 `flowweave run node <run-id> --node <node-run-id>`；只有用户明确要求时才能执行 `node-copy` 或 `node-delete`。暂停或恢复 Runtime 前，必须先读取 `run runtime`，将返回的 `generation` 与 session `row_version` 写入 `expected_generation`、`expected_session_row_version` 后传给 `run pause` 或 `run resume`。供应商上游余额/用量使用 `flowweave model usage <provider-id>`，它可能依赖该供应商的有效 API 凭据。

周期任务使用 `schedule list/templates/occurrences/create/pause/resume/trigger/delete`。先用 `schedule templates` 读取可作为冻结母版的已就绪连续运行记录；创建请求必须使用在线 `FlowRunScheduleWrite` schema：只提交任务名称、母版 `source_flow_run_id` 和五段 `cron_expression`，节点、环境、输入和启动提示词均来自该记录的冻结母版。`schedule occurrences <schedule-id> --page 1 --page-size 10` 按需读取该调度的执行历史及其 FlowRun/NodeRun，而不预加载所有记录。暂停或恢复前从 `schedule list` 读取当前 `row_version`，再传入 `--expected-row-version`。手动触发会新增一次 occurrence，不会改写既有运行；删除生成的 FlowRun 不会删除调度，而有生成记录的调度删除会被平台拒绝。环境发布现在会返回 `202`：发布后读取对应 Environment，等待新版本进入 `READY`，不要重复提交同一 Setup Session。

Agent 会话定时任务使用 `agent schedules <workspace-id>`、`agent schedule-create <workspace-id> --data-file ./.tmp/agent-schedule.json`、`agent schedule-update <workspace-id> <schedule-id> --data-file ./.tmp/agent-schedule.json`、`agent schedule-pause|schedule-resume <workspace-id> <schedule-id> --expected-row-version <n>`、`agent schedule-trigger|schedule-delete` 和 `agent schedule-occurrences`。创建体包含 `name`、`binding_id`、`prompt` 和五段 UTC `cron_expression`。来源必须是已读取的、活动的顶层 Agent Workspace 会话；每次触发从其冻结的模型、能力和工作目录配置新建独立会话，而不是向来源会话发送消息。更新、暂停和恢复先读取当前 `row_version`；从 occurrence 读取实际状态与新会话 binding ID。手动触发和删除会改变状态，必须取得用户对精确 ID 的明确授权，并先用 `--dry-run` 核对请求。

能力命令还支持受治理的 OpenHands Plugin Marketplace：`capability marketplace` 读取服务端固定到完整 commit 的公开目录；使用返回的 `source`、`commit`、`repo_path` 与插件名称调用 `capability marketplace-resolve --data-file ./marketplace-plugin.json`，随后读取 `capability plugin-resolution <resolution-id>`，并以当前 `state_version` 调用 `capability plugin-publish <resolution-id> --data '{"expected_state_version": 1}'`。自定义受信任 Marketplace 可先用 `capability marketplace-preview --data-file ./marketplace.json` 审阅目录。Skill 的已冻结 ZIP 使用 `capability download <capability-id> --output ./skill.zip` 下载；输出文件不能已存在，避免覆盖本地审计产物。Agent Definition 只能以 UTF-8 `.md`/`.markdown` 的 OpenHands YAML frontmatter + Markdown 正文通过 `capability validate|import --type AGENT_DEFINITION --file <file>` 导入。Hook 使用受控表单参数：`capability validate --type HOOK --file ./hook.md --hook-name <name> --hook-event <event> --hook-mode <PROMPT|SCRIPT>`；可选 `--hook-description` 与工具事件的 `--hook-matcher` 会被编译为 OpenHands 原生 `hook_config`，不能提交任意 Hook JSON。

事件触发器是用户拥有的不可变版本：先 `event-trigger list` 或 `event-trigger get <trigger-key>` 读取，再用 `event-trigger create --data-file ./trigger.json --dry-run` 创建，或用 `event-trigger version <trigger-key> --data-file ./trigger.json --dry-run` 新建版本。请求体必须使用在线 `EventTriggerWrite` schema；`actions[].config` 不得包含 token、password、secret 等明文凭据，Webhook/OAuth 等凭据应通过平台受控引用提供。

`api`、`upload`、`ws` 是完整契约入口：任意当前或未来的 REST、multipart、WebSocket 原子接口均可直接调用，不需要等待 CLI 发布。三者都会使用当前 `auth login` 会话；不得用 `--header` 手工传 Cookie。写操作支持 `--dry-run`，并可使用 `-H 'Idempotency-Key: …'` 传入一次性幂等键。对于没有快捷命令的新接口，先运行 `flowweave openapi --paths`，再通过通用命令调用。

安装页面域 skill：

```bash
npx skills add ZME7777777/FlowWeave -g -y
```

仓库尚未发布该 npm 包前，进入 `packages/cli` 后运行 `npm pack` 生成 tarball，并以 `npm install -g ./flowweave-ai-cli-*.tgz` 安装。发布到 npm registry 需要拥有 `@flowweave-ai` scope 的发布权限；本仓库不会自动发布。

## 0.8.0：Agent 会话定时任务

`agent schedules` 和 `agent schedule-create|schedule-update|schedule-pause|schedule-resume|schedule-trigger|schedule-delete|schedule-occurrences` 已同步 Agent Workspace 的公开定时会话接口。定时任务使用五段 UTC Cron，来源会话只提供冻结配置；每次运行都创建新的独立会话。更新和状态变更必须携带当前 `row_version`，以避免覆盖并发修改。

## 0.7.0：会话管理、置顶与 Skill 下载

`agent conversation-management` 按工作目录读取可管理会话，`agent conversation-management-delete <workspace-id> --id <binding-id>` 只删除用户明确选定的会话，`agent pinned-conversations` 和 `agent pin|unpin` 管理 Agent Workspace 会话置顶。FlowRun 节点会话使用 `run pin|unpin <run-id> --attempt <attempt-id> --binding <binding-id>`。临时侧栏会话的模型配置继承主会话，创建请求不再接受模型供应商、模型名称或推理程度字段；跨会话引用使用来源 binding、可选正式 event 与可选选区，均须来自公开读取结果。

## 0.6.0：Runtime、临时侧栏与逐步草稿

`agent runtime-replace <workspace-id>`、`agent sidebar-create|sidebar|sidebar-close` 与 `run stepwise-draft|stepwise-start` 已同步当前公开 API。Runtime replacement、侧栏首条消息和逐步草稿启动均会改变受控状态，CLI 要求 `-H 'Idempotency-Key: <uuid>'`；保存逐步草稿不创建 Attempt，只有显式 `stepwise-start` 才会原子预留并启动。可恢复分片上传继续通过通用 `api`/`upload`：先读取创建上传响应，再用响应的 upload ID 与分片编号上传和完成，不能猜测 upload ID、分片或 owner。

## 只读会话历史

已登录的客户端可读取当前用户有权访问的 Agent Workspace 会话正文：

```bash
flowweave agent history <workspace-id> <binding-id>
flowweave agent history <workspace-id> <binding-id> -q history_cursor=<返回的游标>
flowweave agent history <workspace-id> <binding-id> -q query=关键词 -q max_content_chars=4000
```

每次读取一个 OpenHands 正式事件窗口；使用 `history_cursor` 向更早历史分页，直到它为空。
`next_cursor` 是当前正式事件锚点，不是旧历史分页游标。关键词仅过滤当前页；某页没有匹配
且仍有 `history_cursor` 时，可继续翻页，不能据此断言整个会话没有匹配。单条正文默认最多
2,000 字符，可通过 `max_content_chars` 调整为 1–10,000；`truncated` 和 `content_chars`
明确说明是否截断及原始长度。只返回用户／助手可见正文，不返回工具输出或隐藏引用背景。

此命令沿用 `flowweave auth login` 的用户认证与权限；它不自动为 Runtime 授权，也不替代
后续面向 Agent 的受限 MCP 接入。不要将平台登录凭据放进消息或复制给 Agent。
