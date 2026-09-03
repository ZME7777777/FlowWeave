---
name: flowweave-agent-workspace
description: 管理 FlowWeave Agent Workspace、会话、消息、运行状态、工作目录或能力绑定时使用。
---

# FlowWeave Agent 工作台

先执行 `flowweave agent default` 获取默认工作区，再用 `agent workspace <workspace-id>`、`agent runtime <workspace-id>` 和 `agent conversations <workspace-id>` 读取实时状态。

新会话使用 `agent create <workspace-id> --data-file ...`，发送消息使用 `agent send <workspace-id> <binding-id> --data-file ...`，中断/恢复使用 `agent interrupt`、`agent resume`。会话事件、附件、工作目录、终端和新能力绑定等所有未列为快捷命令的原子操作，先从在线 OpenAPI 发现路径，再使用 `flowweave api`、`upload` 或 `ws`。

不要通过 Docker、Runtime Provider、OpenHands 私有 API 或数据库绕过工作台；会话和 Runtime 的授权、可替换性与审计必须保持由 FlowWeave 控制。
