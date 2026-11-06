---
name: flowweave-agent-children
description: 查询和操作 FlowWeave 原生 Task 子会话，包括事件、消息、控制、模型、能力升级、认证同步与未读；适用于 Agent Workspace 和 FlowRun 节点宿主，临时侧栏聊天转 flowweave-agent-workspace。
---

# FlowWeave 原生子会话

**开始前先完整阅读 `../flowweave/SKILL.md`。** 子会话由 OpenHands 原生 Task 生命周期创建，平台保存受治理的父子绑定和授权。先通过对应宿主读取父会话，再从父范围列表取得子 binding ID。

## 定位与读取

```bash
flowweave child list <parent-binding-id>
flowweave child get <parent-binding-id> <child-binding-id>
flowweave child hydration <parent-binding-id> <child-binding-id>
flowweave child events <parent-binding-id> <child-binding-id> -q history_cursor=<returned-cursor>
flowweave child readiness <parent-binding-id> <child-binding-id>
flowweave child context <parent-binding-id> <child-binding-id>
```

两类宿主统一使用 `/agent-session-children/<parent-binding-id>/<child-binding-id>`。父子关系与宿主身份来自平台返回值，binding ID 和 OpenHands 原生 conversation ID 分别使用各自字段。事件分页沿用正式 `cursor` 与 `history_cursor`，上下文指标允许未知。流订阅使用 `flowweave ws` 调用同范围的 `/stream`，重连位置使用正式 `after_seq`。

## 消息与控制

发送前读取子会话详情和 readiness，再按在线 `MessageWrite` schema 准备 `submission_id`、`content` 及可选附件和引用。每个逻辑提交使用稳定 UUID，重试复用原提交身份。跨会话引用需要真实 `source_binding_id` 和正式 `source_event_id`。

```bash
flowweave child send <parent-binding-id> <child-binding-id> --data-file ./.tmp/child-message.json --dry-run
flowweave child interrupt <parent-binding-id> <child-binding-id> --dry-run
flowweave child resume <parent-binding-id> <child-binding-id> --dry-run
flowweave child condense <parent-binding-id> <child-binding-id> --dry-run
```

写入须符合用户意图及宿主写权限，已取消 Attempt 下的子会话保持只读。压缩只在可写且能接受输入时执行，返回后读取正式压缩事件。模型变更使用 `child model --data-file`，提交当前用户可用的 `model_provider_id`、`model_name` 和可选 `reasoning_effort`，该操作会调用原生继续入口，执行前确认用户意图。

人工确认先 `child pending-confirmation` 读取待确认摘要，再用 `child confirm --data-file` 提交 `expected_pending_digest`、`accept` 与具体 `reason`。成功受理后读取正式事件，不因暂未见结果重复提交。

## 能力与认证

先读取子会话当前配置和 `capability configurable`。`child capabilities-register --data-file` 接受 `{"capability_version_ids":["<version-id>"]}`，单批最多 100 项。支持动态追加 Skill/MCP/Plugin，并将同一独立 Skill 升级到更高版本。Context、Agent Definition 与 Hook 在创建时冻结。读取 `completed`、已加载版本、已存在版本和 `failure` 后，只重试未成功项。

`child credentials` 读取同步状态，再以 `{"credential_ids":["<credential-id>"]}` 调用 `child credentials-sync`。继承来源与实际版本以服务端投影为准，无版本记录时保留未知。MCP 探测通过同范围 `/capabilities/<version-id>/mcp-readiness` 调用公开 API。附件上传使用 `/attachments`，文件、产物引用和删除附件均按在线契约操作，路径只使用公开读取结果。

`child read|unread` 持久化当前用户的普通未读状态。父子会话的运行、未读聚合与删除恢复由平台处理。用户登录会话只授权这些公开入口，原生创建网关需要 Runtime 证明，只能由正式 Task 生命周期使用。
