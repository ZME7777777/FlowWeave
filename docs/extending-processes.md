# Extending FlowWeave

1. 通过 `/api/v1/node-assets` 创建节点资产并声明全部输入/输出字段。
2. 通过两阶段 `/api/v1/capability-imports/validate` 与 `/api/v1/capability-imports` 导入 Skill ZIP 或保存 MCP JSON。
3. 通过 `/api/v1/flows` 保存完整 Flow Node、Edge Mapping 和 Gate Policy 聚合。
4. 使用 `/api/v1/flows/{flow_id}/runs` 从任意 Flow Node 启动，并将人工输入登记为 Artifact Version。
5. Runtime 适配器实现 `start`、`inspect`、`resume`、`cancel`；它只返回规范化结果，不能直接推进人工状态。
6. 新任务处理器必须使用 background task lease owner + generation fencing，外部副作用使用稳定 execution key。
