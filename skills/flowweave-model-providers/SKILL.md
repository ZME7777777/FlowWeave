---
name: flowweave-model-providers
description: 创建、测试、配置、登录或移除 FlowWeave 大模型服务时使用，包括 API Key 和 Codex OAuth。
---

# FlowWeave 大模型配置

先用 `flowweave model list` 确认目标服务。创建、修改或删除分别使用 `model create`、`model update <id>` 和 `model delete <id>`；请求体以在线 OpenAPI 为准。API Key 仅通过命令请求体或受控输入传入，绝不写入配置文件、Skill、仓库或命令示例。

API Key 服务可用 `model discover [<id>]` 发现模型，随后使用 `model test <id>` 测试连接。Codex OAuth 是模型供应商的设备授权流程，而不是 FlowWeave CLI 登录：先执行 `model oauth-start <id>`，在返回的验证页完成授权，再执行 `model oauth-poll <id>`；状态查询与撤销分别使用 `oauth-status`、`oauth-revoke`。

当前平台不需要也不提供 `flowweave auth login`。模型服务变更会影响节点可用性，删除或撤销前先读取引用状态，避免破坏仍在使用的节点。
