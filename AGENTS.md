# FlowWeave 协作指南

本文件适用于整个 FlowWeave 仓库，并补充上级
`/Users/zhengmengen/WorkSpace/AGENTS.md`。若目标目录存在更近的 `AGENTS.md`，优先遵守离目标文件最近的说明。

## OpenHands-first 架构原则

- FlowWeave 的产品设计、用户流程和业务边界是需求来源；OpenHands 是 Agent 执行能力的实现依赖，
  不是产品能力清单。不得以覆盖 OpenHands 全部能力、API 或版本差异为目标扩张 FlowWeave 范围。
- FlowWeave 是控制面，只负责能力治理、不可变版本冻结、权限、策略、审批、审计、资源隔离和业务投影。
- Tool、Skill、Plugin、MCP、Hook、Agent Definition、Task 子 Agent、Condenser、Memory、Critic、Fork 和 ACP
  等执行能力应由 OpenHands 正式类型、事件、API 和生命周期实现。
- 不得用提示词、私有控制 JSON、文本约定、私有 HTTP 或平台自建执行器模拟 OpenHands 已提供的能力。
- FlowWeave 显式传入的 Runtime 能力必须可追溯到固定 version、digest、blob/hash 和
  Snapshot Runtime Manifest，明文 Secret 不得持久化进入 Runtime。OpenHands 1.44.0 原生的 HOME/项目
  ambient Plugin 发现明确允许，它不是 FlowWeave 冻结 Plugin 的替代事实源，也不得用私有
  字段或源码补丁禁用。
- 事件关联必须使用 OpenHands 正式的 `id`、`parent_id`、`action_id`、`tool_call_id`、cursor 等字段，
  不得按事件顺序、名称或文本猜测。

## OpenHands 源码与镜像基线

当前目标能力事实固定为 OpenHands 源码 commit
`9a24f6c8866f353042a57df0514ccc900e3a0691`（审计时 `v1.44.0-6-g9a24f6c88`），只修改 FlowWeave。
OpenHands 源码工作树保持只读；不得在当前 `FR-*` 主线中创建 fork、修改 OpenHands 源码或提前实施二开。

- SDK 源码：`/Users/zhengmengen/WorkSpace/openhands/software-agent-sdk`
- 历史兼容基线：`v1.42.0` / `f09e03eac772290feeb51b7d7390ffaefeca1a09`
- 固定包版本：`openhands-agent-server==1.44.0`、`openhands-sdk==1.44.0`、
  `openhands-tools==1.44.0`、`openhands-workspace==1.44.0`
- 固定运行时镜像：`flowweave-openhands-runtime:1`
- 契约探针：`infra/openhands/contract_check.py`

能力判断优先读取固定 commit，并只在当前切片确有需要时取证：

```bash
git -C /Users/zhengmengen/WorkSpace/openhands/software-agent-sdk \
  show 9a24f6c8866f353042a57df0514ccc900e3a0691:<相对路径>

git -C /Users/zhengmengen/WorkSpace/openhands/software-agent-sdk \
  grep -n '<模式>' 9a24f6c8866f353042a57df0514ccc900e3a0691 -- \
  openhands-agent-server openhands-sdk openhands-tools openhands-workspace
```

证据优先级从强到弱为：固定源码构建的实际镜像及可执行探针、固定 commit 源码和测试、FlowWeave
source lock 与适配代码、历史兼容源码和镜像、版本明确匹配的官方文档。不得凭记忆或浮动 `main` 推断契约。

## FlowRun Runtime 重构恢复方式

每次开始或恢复任务时依次执行：

1. 完整读取 `docs/flowrun-openhands-runtime-design.md` 和 `docs/flowrun-runtime-task-progress.md`。
2. 检查 `git status --short --branch`、未提交 diff 和当前 Alembic heads。
3. 检查进度文档是否最多只有一个 `CURRENT`，以及当前切片依赖是否全部 `DONE`。
4. 若工作树存在未闭环切片，先完成该切片；不得越过它开始后续切片。
5. 一次只完成一个最小可独立验收切片；实现边界过大时先拆分任务和依赖。
6. 按进度文档要求完成实现、基础检查和状态更新。
7. 切片完成后提交该切片代码和文档；确认提交成功后立即停止，不自动开始下一切片。

源码、迁移、测试和实际运行结果是当前进度的权威证据。旧的 OpenHands 重构文档和历史验收结论不得
替代当前 `FR-*` 任务重新实施与验证。

## 切片验证与提交规则

- `FR-01`–`FR-11` 只运行进度文档允许的最窄语法、解析或编译检查，以及 `git diff --check` 和任务状态
  唯一性核对；不得提前运行集中在 `FR-12` 的业务测试、迁移实跑、完整构建、Runtime、安全或 E2E 门禁。
- `FR-12` 负责进度文档列明的完整故障恢复、安全、契约、迁移和 E2E 验证。
- 提交前必须复核 staged diff，确保只包含当前切片和必要的协作文档变更，不混入无关改动、密钥、缓存
  或生成物。
- 每个完成切片使用独立、可审计的 Git commit；提交信息应包含切片编号和结果，例如
  `feat(runtime): complete FR-01 environment binding`。
- 提交成功是切片收尾的一部分。提交后只报告提交哈希、验证结果和下一可执行切片，不继续实现下一项。

## 远程服务器部署基线（192.168.91.154）

以下信息是 2026-09-02 已验收的服务器实况。新会话需要更新远程镜像或排障时，先读取本节和
`docs/local-build-and-deploy.md`，然后直接检查、构建、传输、部署和验证；除非涉及数据删除、SSH/防火墙、
生产凭据或其他高风险变更，不要只停留在描述计划。

### 入口、主机和目录

- 业务地址：`https://hq-ai.hszq8.com/flowweave/`；Agent 深层路由为
  `https://hq-ai.hszq8.com/flowweave/agent`。
- HTTPS 边缘入口是 `192.168.90.254` 的 Tengine；当前账号不能登录该机器。它把域名请求转发到
  `192.168.91.154:3000`，不要误以为域名直接落到 `.154:8001`。
- `.154` 的真实域名分流配置是 `/etc/nginx/conf.d/apidoc-3000.conf`：`/flowweave/` 代理到
  `127.0.0.1:15173`，根路径继续代理 FastGPT `127.0.0.1:3010`。修改前必须备份，执行
  `nginx -t` 成功后才可 reload，并验证 FastGPT 根页面未受影响。
- FlowWeave 部署根：`/opt/flowweave`；Compose：`/opt/flowweave/deploy/compose.yaml`；机密环境文件：
  `/opt/flowweave/.env`；镜像包：`/opt/flowweave/images`；构建日志：`/opt/flowweave/build-logs`。
- 管理 SSH：`root@192.168.91.154`，现有管理密钥由本机 SSH Agent/`~/.ssh/id_ed25519` 提供。不得把
  私钥、`.env` 内容或任何 API Key 写入仓库、聊天记录、构建日志或飞书文档。
- 本机部署凭据保存在仅本机文件 `.local/secrets/flowweave-server-154.env`，该路径通过
  `.git/info/exclude` 排除且文件权限必须为 `0600`。部署脚本需要 root 登录信息时读取该文件中的
  `FLOWWEAVE_SERVER_HOST`、`FLOWWEAVE_SERVER_USER` 和 `FLOWWEAVE_SERVER_PASSWORD`；禁止在命令行、日志或
  受 Git 跟踪文件中回显其值。优先使用 SSH 密钥；只有密钥不可用且确有必要时才使用密码。
- 服务器是 `linux/amd64`，本地 Mac 构建必须显式使用 `--platform linux/amd64`；部署前用
  `docker image inspect --format '{{.Os}}/{{.Architecture}}' <image>` 逐个确认。

### 当前镜像和持久数据契约

远端 Compose 使用以下标签；更新时保持标签不变，除非同时有受审计的 Compose 变更：

```text
flowweave-platform:remote-amd64
flowweave-web:remote-amd64
flowweave-openhands-runtime:1
flowweave-dependency-builder:1
flowweave-sandbox-python:1
flowweave-sandbox-javascript:1
flowweave-postgres:16.9-amd64
flowweave-alpine:3.22-amd64
```

- PostgreSQL 与 Artifact 使用 named volume；Workspace 是宿主机 bind mount
  `/opt/flowweave/data/workspaces`。普通部署禁止 `docker compose down -v`，也不得删除或覆盖该目录。
- 服务器专用 Compose 包含镜像标签、网络、bind mount、`IDE_SSH_*` 和入口端口等本地仓库默认配置之外的
  调整。更新镜像时不要用本地 `infra/compose.yaml` 覆盖 `/opt/flowweave/deploy/compose.yaml`。
- `/opt/flowweave/.env` 已有生产随机密钥。`deploy/install.sh` 只会在文件不存在时生成它；任何更新都必须保留
  现有 `.env`，只核对变量名，不输出值。
- 当前平台服务 `migration/api/worker/runtime-provider` 共用
  `flowweave-platform:remote-amd64`；修改共享平台源码必须一起更新，不能让这些进程运行不同代码。

### 新窗口更新镜像的标准流程

1. 在本地仓库检查 `git status --short --branch`、目标 commit 和未提交 diff。不得覆盖或打包与本次发布无关的
   用户改动。按变更范围运行测试；至少执行受影响包的单测/类型检查和 `git diff --check`。
2. 用 Buildx 构建需要更新的 amd64 镜像。平台和 Web 的基准命令如下；其它镜像使用上面的固定标签和各自
   Dockerfile：

   ```bash
   docker buildx build --platform linux/amd64 --load \
     -f services/platform/Dockerfile -t flowweave-platform:remote-amd64 .
   docker buildx build --platform linux/amd64 --load \
     -f apps/web/Dockerfile -t flowweave-web:remote-amd64 .
   ```

   OpenHands、Sandbox 和 Dependency Builder 还必须运行仓库对应契约/Smoke；不要解除固定源码、版本或 digest。
3. 记录新旧镜像 ID。给远端当前镜像增加带时间戳的本地回滚标签，再传输新镜像。推荐只打包本次变化的标签：

   ```bash
   docker save <changed-image-tags...> | gzip > /tmp/flowweave-update-linux-amd64.tar.gz
   shasum -a 256 /tmp/flowweave-update-linux-amd64.tar.gz
   scp /tmp/flowweave-update-linux-amd64.tar.gz root@192.168.91.154:/opt/flowweave/images/
   ```

   在服务器复算 SHA-256 后执行 `docker load`；不得从不可信位置直接拉取同名浮动镜像。
4. 部署前在服务器执行：

   ```bash
   cd /opt/flowweave
   docker compose --env-file .env -f deploy/compose.yaml config --quiet
   docker compose --env-file .env -f deploy/compose.yaml ps -a
   ```

5. 若更新平台镜像，先单独运行 Migration，并确认退出码为 0，再更新三个常驻平台进程：

   ```bash
   docker compose --env-file .env -f deploy/compose.yaml \
     up --no-deps --force-recreate migration
   docker compose --env-file .env -f deploy/compose.yaml ps -a migration
   docker compose --env-file .env -f deploy/compose.yaml \
     up -d --no-deps --force-recreate runtime-provider api worker
   ```

   仅更新 Web 时只 recreate `web`。更新动态 Runtime 基础镜像只影响之后创建/发布的容器；已运行容器不会
   原地替换，Environment 业务要使用新 Runtime 时还需成功发布新的 Environment Version。
6. 部署后必须验证，而不是只看容器为 `Up`：

   ```bash
   docker compose --env-file .env -f deploy/compose.yaml ps -a
   docker compose --env-file .env -f deploy/compose.yaml logs --tail=100 \
     migration runtime-provider api worker web
   curl -fsS http://127.0.0.1:15173/flowweave/api/v1/flows >/dev/null
   curl -fsS https://hq-ai.hszq8.com/flowweave/api/v1/flows >/dev/null
   curl -fsS https://hq-ai.hszq8.com/flowweave/ >/dev/null
   curl -fsS https://hq-ai.hszq8.com/login >/dev/null
   ```

   还要检查浏览器深层路由、静态 JS、Agent Workspace API，以及实际受影响的发布/终端/运行闭环。
7. 验证失败时先保留日志和数据库错误状态，再把 Compose 标签恢复到部署前记录的镜像 ID/回滚标签并
   force-recreate 受影响服务。不要用 `reset --hard`、`clean -f`、删除 volume 或清空 Workspace 代替回滚。

### 已知部署陷阱

- 外网域名实际进入 `.154:3000`。只改 `/etc/nginx/conf.d/openwebui.conf` 的 `8001` server，直连测试可能
  成功，但公网 `/flowweave` 仍会被 FastGPT 捕获。
- OpenHands Environment 发布会在 Runtime 内执行嵌套 Docker 构建。外层镜像替换 Debian 源不等于嵌套
  Dockerfile 已替换；若日志出现 APT `NOSPLIT`、`repository is not signed`，读取
  `docker buildx history logs <build-ref>`，检查嵌套 `acp-providers` 阶段是否仍访问
  `http://deb.debian.org`。不得把网络错误误报成 npm 或持续发布。
- 页面长期显示“正在发布”时，以数据库任务/版本状态和 Buildx 记录为准；确认无后台任务后，失败版本应为
  `FAILED`，浏览器旧弹窗可通过刷新消除。
- IDEA/Gateway 展示依赖 API 容器内 `IDE_SSH_HOST=192.168.91.154`、`IDE_SSH_PORT=22`、
  `IDE_SSH_USER=flowweave` 和绝对的 `RUNTIME_HOST_WORKSPACE_ROOT=/opt/flowweave/data/workspaces`。
- 宿主机 `flowweave` SSH 用户为锁定密码的公钥账号，UID 必须保持 `10001`，以匹配 Runtime 文件属主。
  `.agent-workspaces/platform-default`、marker、`workspace/state/capabilities` 必须保持代码要求的精确
  `0700/0400`；不要用 ACL 或递归 chmod/chown 授权，否则 Workspace API 会报
  `AGENT_WORKSPACE_ALLOCATION_CONFLICT`。该共享 UID 不具备会话级隔离，仅适合可信环境。
- JetBrains 使用宿主机路径
  `/opt/flowweave/data/workspaces/.agent-workspaces/platform-default/workspace/project`，不是容器路径
  `/runtime/workspace/project`。本机专用私钥是 `~/.ssh/flowweave_idea_ed25519`，只留在本机。

### 最终验收基线

远程部署完成后至少满足：`api`、`runtime-provider` 为 healthy，`worker`、`web` 为 Up，`migration` 为
`Exited (0)`；公网 FlowWeave 根页面、API、静态资源和深层 Agent 路由返回成功；FastGPT 根登录页仍返回
200；SSH 使用专用密钥可登录并读写项目目录；Workspace API 返回 200 且
`ide.gateway.supported=true`。
