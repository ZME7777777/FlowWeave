---
name: flowweave-flows
description: 编排、校验、读取、更新或删除 FlowWeave 流程定义，包括节点、控制边和端口映射；运行实例转 flowweave-runs。
---

# FlowWeave 流程编排

**开始前先完整阅读 `../flowweave/SKILL.md`。** Flow 是可编辑模板，不是运行实例，也不绑定 Environment Version。

## 构成与前置条件

一个 Flow 包含节点实例、控制边和端口映射。节点实例必须引用真实节点资产；端口映射的源/目标字段必须使用该资产稳定的 `field_key`。控制边表达执行先后，端口映射表达数据流，二者不可用展示名称替代。编排前读取候选节点资产与已有流程：

```bash
flowweave node list
flowweave node get <node-asset-id>
flowweave flow list
flowweave openapi --paths
```

## 创建和更新闭环

1. 将完整 `FlowWrite` 请求体保存为 JSON 文件。其结构、节点实例 key、边和映射以在线 OpenAPI 为准；不要使用猜测的字段名。
2. 写入前用 dry run，随后创建或更新：

   ```bash
   flowweave flow create --data-file ./flow.json --dry-run
   flowweave flow create --data-file ./flow.json
   # 或：flowweave flow update <flow-id> --data-file ./flow.json
   ```

3. 读取返回的 Flow 后校验。只有校验通过才进入运行阶段：

   ```bash
   flowweave flow get <flow-id>
   flowweave flow validate <flow-id>
   ```

4. 当用户要运行时，转 `flowweave-runs`：该步骤需要另外选择一个 READY Environment Version，不能在 Flow JSON 中偷偷绑定版本。

## 变更与删除

更新已存在流程前，读取其当前定义，并保留未被用户要求更改的节点、边与映射。对字段 key 或节点资产变更，必须重新校验。删除是状态变更：先核对精确 Flow ID、用户是否要保留已有 FlowRun 历史；只有得到删除授权才执行 `flowweave flow delete <flow-id>`。
