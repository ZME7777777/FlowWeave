---
name: flowweave
description: 通过已配置的 `flowweave` CLI 操作 FlowWeave 平台，包括流程、运行、环境、能力和 Agent Workspace 的 REST 操作。用于 FlowWeave 平台管理或自动化；不用于直接控制 OpenHands 或 Docker。
---

# FlowWeave CLI

将 `flowweave` CLI 作为 FlowWeave REST 操作的唯一入口。当前平台不要求 CLI 登录；只需配置平台基础 URL，并保留部署前缀：

```bash
flowweave config init --base-url https://host.example/flowweave
flowweave health --ready
```

## 发现并调用契约

以在线 OpenAPI 文档作为可用 REST 路径和请求体的唯一权威：

```bash
flowweave openapi --paths
flowweave api get /flows
```

`flowweave api` 会自动为相对资源路径添加 `/api/v1`。仅对平台根接口使用 `--raw`。使用 `--data` 或 `--data-file` 传递 JSON 请求体；当写接口需要可重试的命令身份时，传入 `-H 'Idempotency-Key: …'`。

对于常用顶层资源，`flowweave resource <flows|runs|environments|capabilities|node-assets|node-directories|model-providers|memory-sources|capability-collections> <list|get|create|update|delete>` 提供简洁路径映射。嵌套路由和新增 JSON 接口使用 `api`；multipart 路由使用 `upload`；WebSocket 流使用 `ws`。

## 边界

- 操作必须限定在用户请求的 FlowWeave 资源范围内；目标身份重要时，写入前先读取在线状态。
- `--dry-run` 用于核对最终方法、URL 和 JSON 请求体，不改变平台状态。
- CLI 不保存凭据或任意请求头。不得将密钥写入配置文件、skill 指引、提交的脚本或 shell 历史。
- 不得通过 Docker、Runtime Provider 接口、直接数据库访问或 OpenHands 私有 API 绕过 FlowWeave；FlowWeave 始终是治理控制面。
