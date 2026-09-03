---
name: flowweave-runs
description: 创建、查询、取消、完成、诊断或替换 FlowWeave FlowRun 时使用；适用于手动和自动运行记录。
---

# FlowWeave 运行

先用 `flowweave run list` 或 `run get <id>` 获取真实状态。启动手动运行必须显式指定 Flow 与 READY Environment Version：

`flowweave run start --flow <flow-id> --environment-version <version-id> [--name <name>]`。

运行时状态用 `run runtime <run-id>` 查看，事件使用 `run events <run-id>`。仅在用户明确要求且给出预期 generation/row version 请求体时使用 `run replace <run-id> --data-file ...`；它会触发受 fence 保护的 Runtime replacement。取消、完成和删除都会改变业务状态，执行前先读取 Run 并核对 ID。
