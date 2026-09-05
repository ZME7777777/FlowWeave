---
name: flowweave-schedules
description: 创建、查询、暂停、恢复、手动触发或删除 FlowWeave 周期 FlowRun 调度；单次运行转 flowweave-runs。
---

# FlowWeave 周期调度

**开始前先完整阅读 `../flowweave/SKILL.md`。** 调度是独立管理的重复运行配置，会产生可追溯 occurrence 和 FlowRun；它不是外部 cron，也不是对现有 Run 的重启。

## 创建前检查

先读取并校验 Flow，再选择真实的 READY Environment Version 和起始节点。`FlowRunScheduleWrite` 还会冻结运行方式、间隔、启动提示、Agent preset 与起点 URL 输入；连续运行要求所有可达节点的输入已映射或配置。

```bash
flowweave flow get <flow-id>
flowweave flow validate <flow-id>
flowweave environment get <environment-id>
flowweave schedule create --data-file ./schedule.json --dry-run
flowweave schedule create --data-file ./schedule.json
flowweave schedule list
```

请求字段和枚举以在线 OpenAPI 为准。创建成功后从 `schedule list` 核对 ID、`config_version`、`row_version`、`next_run_at` 和 occurrence；不要根据名称或页面顺序猜 ID。

## 状态、触发与删除

暂停或恢复是带 CAS 的状态变更。每次先重新读取当前 `row_version`，再执行并复读结果：

```bash
flowweave schedule pause <schedule-id> --expected-row-version <row-version> --dry-run
flowweave schedule resume <schedule-id> --expected-row-version <row-version> --dry-run
```

`schedule trigger <schedule-id>` 会新增一次 `MANUAL` occurrence 并异步物化运行，不会复用或改写既有 occurrence。只有用户明确要求立即触发时才执行；返回后观察 occurrence 与其 FlowRun，而不是重复触发。

删除前先读取调度及全部 occurrence。已有 FlowRun 记录时平台会拒绝删除，必须按 `flowweave-runs` 的精确删除与 Runtime 清理契约处理；不得通过数据库、Worker 或外部 cron 绕过。仅在用户明确授权精确调度 ID 后使用 `schedule delete`。
