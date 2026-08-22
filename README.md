# FlowWeave

FlowWeave 是面向内部研发流程的 Agent 工作台：可复用节点资产、可视化流程编排，以及以不可变快照和显式产物版本驱动的运行系统。Agent 负责执行，人工始终负责开始确认、结果验收、修订、快照同步和取消。

## 产品能力

- 节点目录与节点资产：模型、提示词、可选的 Skill/MCP、输入输出契约；默认 Skill 不是必填。Skill ZIP 最大 25 MiB，既支持根目录直接包含单个 `SKILL.md`，也支持一个 ZIP 下按目录批量导入多个 Skill；macOS 元数据不会占用有效条目配额。MCP 通过页面内 JSON 配置创建并作为真实工具注入 Agent；命令型 MCP 所需 CLI 由终端环境安装并随环境版本发布。
- 模型服务：加密 API Key、模型发现、启用模型和默认模型。
- 流程编排：同一资产可重复放置为不同 Flow Node，边提供产物映射候选，节点拥有多条 START/END 门禁。
- 流程运行：任意节点启动、显式 Input Binding、Node Run/Attempt 分离、不可变 Artifact Version；产物来源仅用于血缘和审计。
- 人工控制：开始门禁后确认、结束门禁后验收、驳回创建新 Attempt、Snapshot 追加同步。
- 恢复与审计：PostgreSQL 任务租约/fencing、追加式事件、按 cursor 恢复的 SSE。
- 会话能力引用：在输入框键入 `$`，统一检索并引用当前节点的 Skill 或 MCP；选择结果随消息持久化、排队和引导。

## 仓库结构

```text
apps/web/          React + TypeScript 产品前端
services/platform/ Python 3.12 FastAPI API、Worker、五段核心加两段前向 Alembic 迁移
contracts/         跨进程 JSON Schema
agent-packages/    可导入 Agent Skill/Plugin 示例
infra/compose.yaml PostgreSQL、migration、Runtime Provider、API、Worker、Web
```

## 启动

要求：Node.js 22+、pnpm 10.33.4、Python 3.12+、uv 0.7.8、Docker。

```bash
cp .env.example .env
make install
make infra-up
```

访问 <http://localhost:5173>。默认由 Runtime Provider 为每个 FlowRun 启动独立的 OpenHands Agent Server generation，并在每次执行时读取节点所选模型服务、模型和加密保存的 API Key；Compose 不再运行共享 Agent Server。Mock Runtime 只用于自动化测试；如需显式指定生产适配器：

```bash
RUNTIME_ADAPTER=openhands make infra-up
```

修改基础镜像、平台服务或 Web 后，可一条命令无缓存重建全部本地镜像并重新部署完整服务。该命令保留数据库、Artifact 与节点工作区数据：

```bash
make rebuild-deploy
```

全量重建、单服务部署、运行时镜像更新、迁移顺序和部署后检查详见 [本地编译、打包与部署](docs/local-build-and-deploy.md)。

节点可写资源保存在宿主机 `var/workspaces/nodes/<node-asset-id>/`：`skills/` 存放完整 Skill 包，`files/` 可放文本或附件，`repositories/` 可放代码仓库，`sessions/` 保存各次运行的会话工作区。上传的 MCP/Hook 配置与脚本由平台物化到 `var/workspaces/.managed-assets/nodes/<node-asset-id>/`，并以只读方式挂载到 Runtime 的 `/runtime/capabilities/nodes/<node-asset-id>/`，不会暴露在节点可写挂载中。可通过绝对路径配置 `FLOWWEAVE_RUNTIME_HOST_WORKSPACE_ROOT` 改为其他宿主机目录。

OpenHands 镜像内置 `sh`、`bash`、Python、Node.js、npm/npx、`js`、uv/uvx、Git、SSH、curl、jq、unzip 与 `lark-cli`。默认会把宿主机 `~/.lark-cli` 及 `~/Library/Application Support/lark-cli` 映射到 Session 容器，并自动为 Linux 创建兼容的 `master.key` 链接，因此在宿主机完成的配置和授权可被所有 Agent 会话直接复用。macOS 首次共享前需在宿主机终端执行一次：

```bash
lark-cli config keychain-downgrade
lark-cli auth login --domain all
```

可用 `FLOWWEAVE_HOST_LARK_CLI_HOME` 和 `FLOWWEAVE_HOST_LARK_CLI_KEY_HOME` 覆盖这两个宿主机目录。它们包含授权凭据，只应挂载到可信的本地 Agent 环境。

本地分进程开发：

```bash
make api-dev
make worker-dev
make web-dev
```

验证：

```bash
make check
make migration-check
make e2e
```

公共接口统一位于 `/api/v1`，读写接口均可直接访问；命令通过 `Idempotency-Key` 保证幂等。模型密钥加密保存且不进入 API 响应、运行快照、Prompt 或事件。
