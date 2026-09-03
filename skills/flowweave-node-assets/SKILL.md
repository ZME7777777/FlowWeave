---
name: flowweave-node-assets
description: 创建、修改、查询或删除 FlowWeave 节点资产与节点目录时使用，包括输入输出字段和执行提示词配置。
---

# FlowWeave 节点资产

先读取目标目录或节点，避免按名称猜测 ID：

`flowweave node-directory list`、`flowweave node list`、`flowweave node get <id>`。

创建/更新节点使用 `node create` 或 `node update <id>`，请求体遵循在线 OpenAPI 的 `NodeAssetWrite`。输入和输出字段必须使用稳定的 `field_key`，类型只能是 `URL` 或 `FILE`。复杂定义保存到 JSON 文件后传 `--data-file`。创建前可加 `--dry-run` 核对 URL 与请求体。

节点目录使用 `node-directory create`。删除节点前先确认没有仍需保留的流程定义或运行记录；目标不明确时先读取节点详情。
