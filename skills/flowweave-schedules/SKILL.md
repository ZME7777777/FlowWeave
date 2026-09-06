---
name: flowweave-schedules
description: 创建、查询、暂停、恢复、手动触发或删除 FlowWeave 周期 FlowRun 调度；单次运行转 flowweave-runs。
---

# FlowWeave 周期调度

**开始前先完整阅读 `../flowweave/SKILL.md`。** 调度是独立管理的重复运行配置，会产生可追溯 occurrence 和 FlowRun；它不是外部 cron，也不是对现有 Run 的重启。

## 创建前检查

定时任务只能从一条已满足启动条件的**连续运行** FlowRun 记录创建。先读取平台提供的可用母版，而不是重新选择 Flow、Environment、节点、输入或 Agent 配置；这些事实会随母版冻结，后续修改源记录不会改写调度。

```bash
flowweave schedule templates
flowweave schedule create --data-file ./schedule.json --dry-run
flowweave schedule create --data-file ./schedule.json
flowweave schedule list
```

`schedule.json` 只包含 `name`、从 `schedule templates` 返回的 `source_flow_run_id`，以及标准五段 `cron_expression`。请求字段和枚举以在线 OpenAPI 为准。创建成功后从 `schedule list` 核对 ID、母版、`config_version`、`row_version`、`next_run_at` 和 occurrence；不要根据名称或页面顺序猜 ID。Cron 按 UTC 计算，旧分钟间隔配置不会被当作完整 Cron 继续运行。

## 执行历史

调度首页只读取目录摘要。需要查看实际执行时，再按需分页读取 occurrence；每条 occurrence 的生成 FlowRun 与 NodeRun 是真实执行身份，不能根据名称或触发时间猜测。

```bash
flowweave schedule occurrences <schedule-id> --page 1 --page-size 10
flowweave run get <flow-run-id>
flowweave run node <flow-run-id> --node <node-run-id>
```

## 状态、触发与删除

暂停或恢复是带 CAS 的状态变更。每次先重新读取当前 `row_version`，再执行并复读结果：

```bash
flowweave schedule pause <schedule-id> --expected-row-version <row-version> --dry-run
flowweave schedule resume <schedule-id> --expected-row-version <row-version> --dry-run
```

`schedule trigger <schedule-id>` 会新增一次 `MANUAL` occurrence 并异步物化独立的连续运行，不会复用或改写既有 occurrence。只有用户明确要求立即触发时才执行；返回后观察 occurrence 与其 FlowRun，而不是重复触发。

删除前先读取调度及 occurrence。生成的 FlowRun 可单独删除，且不会静默删除该调度；反过来，母版仍被调度引用、或调度仍有生成记录时，平台会明确拒绝删除。必须按 `flowweave-runs` 的精确删除与 Runtime 清理契约处理；不得通过数据库、Worker 或外部 cron 绕过。仅在用户明确授权精确调度 ID 后使用 `schedule delete`。
