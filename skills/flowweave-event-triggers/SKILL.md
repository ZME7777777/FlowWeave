---
name: flowweave-event-triggers
description: 查询、创建或版本化 FlowWeave 运行事件触发器；只通过平台治理的动作策略投递，不直接调用外部执行器。
---

# FlowWeave 运行事件触发器

**开始前先完整阅读 `../flowweave/SKILL.md`。** 事件触发器是用户拥有的、追加式版本化规则：它监听已持久化的运行事件，并由平台 Outbox 投递受治理动作。它不是外部轮询、不是 Runtime Hook，也不能直接调用 Docker、Worker、数据库或 OpenHands 私有接口。

## 读取与创建

先读取目录或精确 `trigger_key`，确认真实身份与最新版本。新建或修改都产生不可变的新版本；修改既有规则时不能覆盖旧版本，使用 `version` 并在请求体保留相同 `trigger_key`。

```bash
flowweave event-trigger list
flowweave event-trigger get <trigger-key>
flowweave event-trigger create --data-file ./.tmp/trigger.json --dry-run
flowweave event-trigger create --data-file ./.tmp/trigger.json
flowweave event-trigger version <trigger-key> --data-file ./.tmp/trigger.json --dry-run
```

请求体以在线 `EventTriggerWrite` schema 为准，至少包含小写且稳定的 `trigger_key`、名称和一个或多个动作。事件类型必须使用服务端定义的大写名称；source 只能是 `OPENHANDS`、`RUNTIME` 或 `ORCHESTRATION`，失败分类同样只能采用 OpenAPI 返回的枚举。可按 FlowRun 或 NodeRun ID 限定范围，但必须从已读取的资源获得 ID，不能按显示名或事件顺序猜测。

动作当前只允许 `WEBHOOK`、`NOTIFY` 和 `CREATE_TASK`。`actions[].config` 是公开且可审计的配置，严禁写入 API Key、token、password、secret、Authorization 或其他明文凭据；需要认证时应先通过平台的受控 Secret/凭据引用能力配置。创建后重新读取触发器，核对返回的 `version_no`、filters、actions 与 `enabled`。异步投递失败应读取平台事件/交付状态后再处理，不能通过重复创建规则或绕过 Outbox 重投。

## 示例请求

将一次性请求文件放在仓库 `.tmp/`（不得提交），先用 `--dry-run` 审核：

```json
{
  "trigger_key": "notify.runtime-failure",
  "name": "Runtime failure notification",
  "enabled": true,
  "event_types": ["ERROR"],
  "sources": ["RUNTIME"],
  "failure_classes": ["TRANSIENT_UNAVAILABLE"],
  "actions": [
    {
      "action_type": "NOTIFY",
      "description": "Notify the on-call owner",
      "config": {"channel": "runtime-alerts"}
    }
  ]
}
```
