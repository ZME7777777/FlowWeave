---
name: flowweave-agent-schedules
description: 创建、查询、更新、暂停、恢复、手动触发或删除 FlowWeave Agent Workspace 会话定时任务，并读取运行 occurrence；FlowRun 调度转 flowweave-schedules。
---

# FlowWeave Agent 会话定时任务

**开始前先完整阅读 `../flowweave/SKILL.md`。** 本 Skill 只处理顶层 Agent Workspace 的会话定时任务，不处理 FlowRun 周期调度；后者转 `flowweave-schedules`。

## 对象与边界

会话定时任务保存一个活动的顶层 Agent Workspace conversation binding、提示词和五段 UTC Cron。来源会话只提供冻结的模型、能力和工作目录配置；每次定时或手动触发均创建一条新的独立会话并投递提示词，绝不向来源会话追加消息。

只能使用平台公开的 `agent schedules` 和 `agent schedule-*` 接口。不得用顶层 `schedule`、外部 cron、Docker、数据库、Runtime Provider 或 OpenHands 私有端点替代，以免绕过 occurrence、状态和审计。

## 操作闭环

1. 读取 Workspace、活动候选会话和已有定时任务，确认真实 ID：

   ```bash
   flowweave agent workspace <workspace-id>
   flowweave agent conversations <workspace-id>
   flowweave agent schedules <workspace-id>
   ```

2. 创建时使用 `name`、`binding_id`、`prompt` 和 `cron_expression`；Cron 必须为五段 UTC 表达式。先审阅请求：

   ```bash
   flowweave agent schedule-create <workspace-id> \
     --data-file ./.tmp/agent-schedule.json --dry-run
   ```

3. 更新、暂停或恢复前重新读取任务，以响应中的 `row_version` 防止覆盖并发修改：

   ```bash
   flowweave agent schedule-update <workspace-id> <schedule-id> \
     --data-file ./.tmp/agent-schedule.json --dry-run
   flowweave agent schedule-pause <workspace-id> <schedule-id> \
     --expected-row-version <row-version> --dry-run
   flowweave agent schedule-resume <workspace-id> <schedule-id> \
     --expected-row-version <row-version> --dry-run
   ```

4. 运行状态和新会话 binding ID 只以 occurrence 为准；仅使用其返回 ID 打开产生的会话：

   ```bash
   flowweave agent schedule-occurrences <workspace-id> <schedule-id>
   ```

## 状态变更

`agent schedule-trigger` 和 `agent schedule-delete` 会改变平台状态。执行前必须取得用户对精确 schedule ID 的明确授权，并先通过 `--dry-run` 核对 URL、范围和请求体。异步 trigger 返回后读取 occurrence，而不是重复触发。

```bash
flowweave agent schedule-trigger <workspace-id> <schedule-id> --dry-run
flowweave agent schedule-delete <workspace-id> <schedule-id> --dry-run
```
