---
name: flowweave-flowrun-workbench
description: 在 FlowWeave FlowRun 工作台处理节点执行、输入产物、门禁、人工输出、自动运行或节点会话时使用。
---

# FlowWeave FlowRun 工作台

先用 `flowweave run get <run-id>` 和 `flowweave run events <run-id>` 读取当前 Run、Snapshot、节点执行与 Attempt 身份。FlowRun 中所有细粒度原子操作——创建节点执行、绑定输入产物、确认启动、人工输入、提交人工输出、验收/拒绝、重试或取消——都以在线 OpenAPI 为权威，使用 `flowweave api` 调用对应路径；文件产物使用 `flowweave upload`。

例如，先发现接口再以真实 Attempt ID 操作：

```bash
flowweave openapi --paths
flowweave api post /node-attempts/<attempt-id>/confirm-start --data '{"row_version": 3}' --dry-run
```

不要按节点名称、执行顺序或页面显示顺序猜测 `run-id`、`node-run-id`、`attempt-id`、Snapshot 或版本号。节点会话、确认批次和 Runtime 均必须经 FlowWeave API 授权；不得直接连接 Docker、Runtime Provider 或 OpenHands 私有端点。
