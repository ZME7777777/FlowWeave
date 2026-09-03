---
name: flowweave-node-assets
description: 创建、修改、查询或删除 FlowWeave 节点资产和节点目录，包括输入输出字段与执行提示词；流程编排转 flowweave-flows。
---

# FlowWeave 节点资产

**开始前先完整阅读 `../flowweave/SKILL.md`。** 本 Skill 在其平台模型、CLI 配置、OpenAPI 发现方式和安全边界之上，处理“节点定义”而不是节点的某次执行。

## 对象与边界

节点目录用于组织节点资产。节点资产定义可复用的工作单元：名称、目录、执行提示词，以及输入和输出字段。Flow 中的节点实例引用节点资产；FlowRun 中的 NodeRun/Attempt 是这一定义的运行投影，转 `flowweave-flowrun-workbench` 处理。

字段的 `field_key` 是程序接口契约：端口映射、输入产物与输出产物都依赖它。创建后不要因展示名称变化而随意改 key。当前字段类型只能是 `URL` 或 `FILE`；在请求体前以线上 `NodeAssetWrite` schema 为准。

## 读、写、验证

1. 先查真实目录与资产，避免用名称猜 ID：

   ```bash
   flowweave node-directory list
   flowweave node list
   flowweave node get <node-asset-id>
   ```

2. 要新建分类时创建目录；要创建或修改节点时，把完整定义写到 JSON 文件。对复杂字段数组，文件比内联 JSON 更可靠。

   ```bash
   flowweave openapi --paths
   flowweave node-directory create --data-file ./directory.json --dry-run
   flowweave node create --data-file ./node-asset.json --dry-run
   flowweave node create --data-file ./node-asset.json
   flowweave node get <new-node-asset-id>
   ```

3. 验证返回的目录、提示词和每个字段的 `field_key`、方向与类型都与请求一致。准备编排时，把**返回的节点资产 ID 与字段 key**交给 `flowweave-flows`；不要手填推测值。

## 修改与删除

更新会影响之后使用此资产的流程定义。先读取目标和相关 Flow，再使用 `flowweave node update <id> --data-file ./node-asset.json`，随后重新读取并在受影响 Flow 上校验。删除前先确认用户不需要保留引用它的 Flow 或运行历史；只有精确 ID 与删除授权都明确时执行 `node delete <id>`。

若用户要“运行节点”或“上传节点输入”，不要在此处猜 Run/Attempt：先读取 FlowRun 详情，再转 FlowRun 工作台 Skill。
