---
name: flowweave-runs
description: 创建、查询、取消、完成、诊断或替换 FlowWeave FlowRun；节点层面的输入、产物和门禁转 flowweave-flowrun-workbench。
---

# FlowWeave FlowRun

**开始前先完整阅读 `../flowweave/SKILL.md`。** FlowRun 是一次冻结运行：它将已校验的 Flow 与用户显式选择的 READY Environment Version 关联到运行 Snapshot、Runtime、节点执行和事件。

## 启动前检查

不要只凭名称启动。先读取 Flow，执行校验；再读取 Environment 并从返回版本中选择 `READY` 的真实 version ID。Flow 不拥有环境版本字段，不能省略这个选择。

```bash
flowweave flow get <flow-id>
flowweave flow validate <flow-id>
flowweave environment get <environment-id>
```

## 创建与观察

```bash
flowweave run start --flow <flow-id> --environment-version <ready-version-id> --name "可选名称"
flowweave run get <run-id>
flowweave run runtime <run-id>
flowweave run events <run-id>
```

创建后立即读取 Run，确认 Flow ID、冻结的 Environment Version、Snapshot 与状态均符合预期。异步状态以 `run get`、`run runtime` 和 `run events` 为准；不要因 UI 暂未刷新而重复创建。Run 内节点、Attempt、产物、人工输入/输出、门禁、自动运行草稿和节点会话由 `flowweave-flowrun-workbench` 处理。

## 诊断和受限变更

诊断时先读 Run 详情、Runtime 与事件，记录真实 `run_id`、generation、session `row_version` 和失败原因。Runtime replacement 是受 fence 保护的动作，只有用户明确要求替换且请求体含从当前状态读取的 `expected_generation` 与 `expected_session_row_version` 时，才执行：

```bash
flowweave run replace <run-id> --data-file ./replacement.json --dry-run
flowweave run replace <run-id> --data-file ./replacement.json
```

暂停与恢复使用相同的 fencing 值；先读取 `run runtime`，再把当前 `generation` 和 session `row_version` 放入请求，且只在用户明确授权后执行：

```bash
flowweave run pause <run-id> --data '{"expected_generation": 3, "expected_session_row_version": 12}' --dry-run
flowweave run resume <run-id> --data '{"expected_generation": 3, "expected_session_row_version": 13}' --dry-run
```

复制或删除执行记录前，用 `flowweave run node <run-id> --node <node-run-id>` 确认归属和状态。`node-copy` 只创建独立的新手动记录；`node-delete` 是不可逆的状态变更，均须有用户对精确记录 ID 的明确授权。

取消、完成和删除都改变业务状态。先读取 Run，确认它就是目标且用户意图明确，再使用 `run cancel`、`run complete` 或 `run delete`；操作后重新读取或查看事件验证结果。不能用 Docker 重启或数据库写入来替代平台的取消/恢复语义。
