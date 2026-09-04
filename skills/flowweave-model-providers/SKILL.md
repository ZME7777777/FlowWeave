---
name: flowweave-model-providers
description: 创建、测试、配置、登录或移除 FlowWeave 大模型服务，包括 API Key 与 Codex OAuth；不处理 FlowWeave CLI 登录。
---

# FlowWeave 大模型服务

**开始前先完整阅读 `../flowweave/SKILL.md`。** 模型供应商是平台受控配置；它与 CLI 基础 URL 配置相互独立，Codex OAuth 也不等于 `flowweave auth login`（该登录命令当前不存在）。

## 安全规则

API Key 只能在用户授权的受控请求输入中传递。绝不把它写入 JSON 示例、Skill、仓库、配置文件、命令历史或输出；回复中也不复述密钥。先列出现有供应商，避免误更新/误删：

```bash
flowweave model list
flowweave openapi --paths
```

## 配置、发现、测试

创建和修改的字段以线上 schema 为准。用 JSON 文件时，文件本身不得提交进仓库；完成后按团队安全流程处理。

```bash
flowweave model create --data-file ./provider-local.json --dry-run
flowweave model create --data-file ./provider-local.json
flowweave model discover <provider-id>
flowweave model test <provider-id>
```

写入后读取列表/详情，确认返回 ID 和状态；发现模型后再测试实际连接。连接失败时保留服务端错误码与 provider ID，先检查该供应商配置和网络许可，不在 CLI 配置中伪造认证信息。

对于支持的 API-key 供应商，可用 `flowweave model usage <provider-id>` 读取上游用量或余额。它只读取平台托管凭据的结果；不要把 API Key 作为命令参数、查询参数或请求体传入，也不要把返回中可能敏感的账户数据复制到日志。

## Codex OAuth

仅对平台支持的供应商使用设备授权：先调用 `flowweave model oauth-start <provider-id>`，让用户在返回的验证地址完成授权，再用 `oauth-poll` 查看结果。用 `oauth-status` 查询，明确授权撤销后才 `oauth-revoke`。OAuth 设备码与令牌是敏感数据，不复制到日志或文件。

删除供应商前，先读取其引用状态并提示它可能影响节点/运行；只有用户指定精确 ID 并确认后执行 `flowweave model delete <provider-id>`。
