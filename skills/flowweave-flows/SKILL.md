---
name: flowweave-flows
description: 编排、校验、读取、更新或删除 FlowWeave 流程定义时使用，包括节点、控制边和端口映射。
---

# FlowWeave 流程编排

流程是冻结前的模板，不绑定 Environment Version。先读取节点资产并准备完整 JSON，然后用 `flowweave flow create --data-file ./flow.json` 创建或 `flow update <id> --data-file ./flow.json` 更新。

写入前确保节点 key、控制边和端口映射都来自明确的节点资产字段；不要根据名称或显示顺序猜测字段。保存后用 `flowweave flow validate <id>` 校验，读取用 `flow get <id>`。只有在用户明确要求删除且确认 ID 后执行 `flow delete <id>`。
