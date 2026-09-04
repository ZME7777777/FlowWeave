# @flowweave-ai/cli

`@flowweave-ai/cli` 是可独立安装的 FlowWeave 平台命令行客户端，要求 Node.js 22 或更高版本。包中不包含 Python、Docker 或平台源码依赖。

```bash
npm install -g @flowweave-ai/cli
flowweave config init --base-url https://host.example/flowweave
flowweave health --ready
```

配置只保存基础 URL 到 `~/.config/flowweave/config.json`（可由 `FLOWWEAVE_CONFIG_PATH` 覆盖）。当前平台不需要登录。

页面域命令包括 `node`、`node-directory`、`capability`、`environment`、`credential`、`flow`、`run`、`model` 和 `agent`。它们分别覆盖节点资产、能力仓库、终端环境、认证管理、流程编排、FlowRun、大模型配置与 Agent 工作台的常用原子操作。每个命令的 JSON 请求体与在线 OpenAPI 一致；运行 `flowweave <域> --help` 查看映射。

常用命令示例：

`flowweave node-directory delete-many --id <directory-id> --id <directory-id> --dry-run`、`flowweave credential delete-many --id <credential-id> --id <credential-id>`、`flowweave environment publish <setup-session-id> --description '升级 Python 依赖'`、`flowweave agent file-delete <workspace-id> --path /runtime/workspace/project/report.md --path /runtime/workspace/project/output`、`flowweave run workspace-delete <run-id> --attempt <attempt-id> --path /runtime/workspace/project/result.txt`。

节点目录批量删除、Agent Workspace 文件树删除和 FlowRun 节点工作区删除都使用 JSON 数组请求体。重复传入 `--id` 或 `--path` 即可批量选择；先读取真实资源与路径，并用 `--dry-run` 核对 DELETE URL、范围 query 和请求体。FlowRun 工作目录删除使用 `flowweave run work-directory-delete <run-id> --attempt <attempt-id> --work-directory <directory-id>`。

`api`、`upload`、`ws` 是完整契约入口：任意当前或未来的 REST、multipart、WebSocket 原子接口均可直接调用，不需要等待 CLI 发布。`ws` 使用 Node 原生 WebSocket；当前公开 FlowWeave WebSocket 接口无需自定义请求头。写操作支持 `--dry-run`，并可使用 `-H 'Idempotency-Key: …'` 传入一次性幂等键。对于没有快捷命令的新接口，先运行 `flowweave openapi --paths`，再通过通用命令调用。

安装页面域 skill：

```bash
npx skills add ZME7777777/FlowWeave -g -y
```

仓库尚未发布该 npm 包前，进入 `packages/cli` 后运行 `npm pack` 生成 tarball，并以 `npm install -g ./flowweave-ai-cli-*.tgz` 安装。发布到 npm registry 需要拥有 `@flowweave-ai` scope 的发布权限；本仓库不会自动发布。
