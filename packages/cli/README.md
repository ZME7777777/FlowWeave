# @flowweave-ai/cli

`@flowweave-ai/cli` 是可独立安装的 FlowWeave 平台命令行客户端，要求 Node.js 22 或更高版本。包中不包含 Python、Docker 或平台源码依赖。

```bash
npm install -g @flowweave-ai/cli
flowweave config init --base-url https://host.example/flowweave
flowweave auth login
flowweave health --ready
```

配置只保存基础 URL 到 `~/.config/flowweave/config.json`（可由 `FLOWWEAVE_CONFIG_PATH` 覆盖）。登录会话单独保存到同目录的 `auth.json`（可由 `FLOWWEAVE_AUTH_PATH` 覆盖），文件权限为 `0600`，并且只会发送给登录时的同一平台地址。使用 `flowweave auth status` 检查当前身份，使用 `flowweave auth logout` 撤销服务端会话并删除本地文件。默认登录会隐藏密码输入；非交互环境使用 `--password-stdin`，不要把密码写进命令参数、脚本或日志。

页面域命令包括 `node`、`node-directory`、`capability`、`environment`、`credential`、`flow`、`run`、`schedule`、`model` 和 `agent`。它们分别覆盖节点资产、能力仓库、终端环境、网站认证条目、流程编排、FlowRun、周期调度、大模型配置与 Agent 工作台的常用原子操作。`credential` 管理网站凭据，不等于 `auth` 用户登录。每个命令的 JSON 请求体与在线 OpenAPI 一致；运行 `flowweave <域> --help` 查看映射。

常用命令示例：

`flowweave node-directory delete-many --id <directory-id> --id <directory-id> --dry-run`、`flowweave credential delete-many --id <credential-id> --id <credential-id>`、`flowweave environment publish <setup-session-id> --description '升级 Python 依赖'`、`flowweave agent file-delete <workspace-id> --path <workspace-api-returned-path>`、`flowweave run workspace-delete <run-id> --attempt <attempt-id> --path <attempt-workspace-api-returned-path>`。

节点目录批量删除、Agent Workspace 文件树删除和 FlowRun 节点工作区删除都使用 JSON 数组请求体。重复传入 `--id` 或 `--path` 即可批量选择；先读取真实资源与路径，并用 `--dry-run` 核对 DELETE URL、范围 query 和请求体。FlowRun 工作目录删除使用 `flowweave run work-directory-delete <run-id> --attempt <attempt-id> --work-directory <directory-id>`。

读取 FlowRun 中的记录使用 `flowweave run node <run-id> --node <node-run-id>`；只有用户明确要求时才能执行 `node-copy` 或 `node-delete`。暂停或恢复 Runtime 前，必须先读取 `run runtime`，将返回的 `generation` 与 session `row_version` 写入 `expected_generation`、`expected_session_row_version` 后传给 `run pause` 或 `run resume`。供应商上游余额/用量使用 `flowweave model usage <provider-id>`，它可能依赖该供应商的有效 API 凭据。

周期任务使用 `schedule list/create/pause/resume/trigger/delete`。创建请求必须使用在线 `FlowRunScheduleWrite` schema；暂停或恢复前从 `schedule list` 读取当前 `row_version`，再传入 `--expected-row-version`。手动触发会新增一次 occurrence，不会改写既有运行；删除已有执行记录的调度会被平台拒绝。

`api`、`upload`、`ws` 是完整契约入口：任意当前或未来的 REST、multipart、WebSocket 原子接口均可直接调用，不需要等待 CLI 发布。三者都会使用当前 `auth login` 会话；不得用 `--header` 手工传 Cookie。写操作支持 `--dry-run`，并可使用 `-H 'Idempotency-Key: …'` 传入一次性幂等键。对于没有快捷命令的新接口，先运行 `flowweave openapi --paths`，再通过通用命令调用。

安装页面域 skill：

```bash
npx skills add ZME7777777/FlowWeave -g -y
```

仓库尚未发布该 npm 包前，进入 `packages/cli` 后运行 `npm pack` 生成 tarball，并以 `npm install -g ./flowweave-ai-cli-*.tgz` 安装。发布到 npm registry 需要拥有 `@flowweave-ai` scope 的发布权限；本仓库不会自动发布。
