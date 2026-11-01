---
name: flowweave-cases
description: 创建、配置、启动和监控 FlowWeave 场景空间（Case）：路径阶段、资料、协调 Agent 会话、暂停/恢复与控制计划；单个 FlowRun 的节点处理转 flowweave-flowrun-workbench。
---

# FlowWeave 场景空间（Case）

**开始前先完整阅读 `../flowweave/SKILL.md`。** 场景是带目标、有序阶段路径、资料和协调 Agent 配置的工作空间。启动后由一个原生协调 Agent 会话判断并通过平台受控工具驱动 FlowRun；平台负责校验、授权、事件投递和审计，不用固定顺序替 Agent 做业务判断。

## 核心事实

- 名称、工作区、说明、目标创建后固定；资料、路径、会话配置可继续管理。
- 阶段的 `flow_run_id` 是**模板**，场景永不直接启动它；`current_execution_flow_run_id` 才是由模板复制的当前独立执行。逐步模板必须是子逐步记录，且起点可达的所有节点草稿完整。
- 所有写入使用 `row_version` 乐观锁（`expected_row_version`）；写前重新读取场景，写后复读。
- 单个 FlowRun 完成不等于阶段业务完成，需协调 Agent 复核后推进。

## 只读检查

```bash
flowweave case list
flowweave case get <case-id>
flowweave case stage-templates <case-id>        # 可作为阶段的运行记录
flowweave case artifact-materials <case-id> --offset 0 --limit 20
flowweave case round <case-id> <round-id>       # 协调轮次状态
```

关注 `state`、`row_version`、`stages[].state`、`stages[].current_execution_flow_run_id`、`current_control_round_id`、`control_plans`、`feedback_deliveries`、`decision_proposals`。ID 一律取自返回值，不按名称或顺序猜。

## 创建与配置

```bash
flowweave case create --data-file ./.tmp/case.json --dry-run     # name、objective、description、work_directory_id、materials、stages
flowweave case update <case-id> --data-file ./.tmp/case.json     # 带 expected_row_version
flowweave case path <case-id> --data-file ./.tmp/path.json       # {expected_row_version, cards:[{flow_run_id}|{id}]}
flowweave case session-config <case-id> --data-file ./.tmp/session.json
flowweave case material-upload <case-id> --file ./doc.pdf --expected-row-version <n>
flowweave case material-file <case-id> --value <资料引用> [--download]
```

`session.json` 含 `expected_row_version`、`model_provider_id`、`model_name`、可选 `reasoning_effort`、`capability_version_ids`、`credential_ids`；模型供应商先按 `flowweave-model-providers` 确认可用，认证只能是当前用户的。路径中的 FlowRun 必须是完整配置的逐步/连续记录。

## 启动协调 Agent

启动会创建新的原生会话并占用模型额度，**只在用户明确要求时执行**。必须带 `Idempotency-Key`，超时重试复用同一个 key，避免重复建会话。

```bash
flowweave case start <case-id> --data-file ./.tmp/session.json -H 'Idempotency-Key: <uuid>' --dry-run
```

返回后轮询 `case get`/`case round`，等 `current_control_round_id` 对应轮次进入就绪并出现 `control_conversation_binding_id`；再用 `flowweave agent conversation` / `agent hydration` 读取会话，不要重复 start。轮次失败用 `case round-retry`（复用原身份）。已有会话再次 start 会新开轮次，旧轮次保留。

## 阶段与受控动作

```bash
flowweave case stage-launch <case-id> <stage-id>        # 复制模板为独立执行副本（可带 {"name":...}）
flowweave case stage-start  <case-id> <stage-id> --data '{"reason":"..."}'   # 启动当前执行副本
flowweave case stage-bind   <case-id> <stage-id> --data '{"flow_run_id":"...","execution_mode":"STEPWISE"}'
flowweave case plan-create  <case-id> --data-file ./.tmp/plan.json   # action、idempotency_key、reason、node_run_id、node_attempt_id、input_snapshot
flowweave case plan-execute <case-id> <plan-id>
flowweave case plan-retry   <case-id> <plan-id>
flowweave case proposal-create|proposal-execute ...
```

控制计划 `action` 白名单：`CREATE_EXECUTION_COPY`、`CONFIGURE_EXECUTION`、`START_EXECUTION`、`START_STEPWISE_NODE`、`ACCEPT_STEPWISE_ATTEMPT`、`REJECT_STEPWISE_ATTEMPT`、`STOP_EXECUTION`、`RERUN_STEPWISE_NODE`、`COPY_CONTINUOUS_EXECUTION`、`ADVANCE_CASE_STAGE`、`COMPLETE_CASE`。`idempotency_key` 须匹配 `^[a-z][a-z0-9._:-]{0,139}$`。正常情况下这些动作由协调 Agent 经原生 MCP 工具发起；人工通过 CLI 调用属于接管，需用户授权，且只作用于活动 `current_execution_flow_run_id`，不可作用于模板、历史副本或其他阶段。

执行结果以持久记录为准：看计划/提案的状态、失败码，并复读对应 FlowRun；不要把"HTTP 返回了"当作"已启动"。

## 暂停与恢复

```bash
flowweave case pause  <case-id> --expected-row-version <n>
flowweave case resume <case-id> --expected-row-version <n>
```

暂停只阻止新的自主动作，不中断原生会话，也不取消已启动的 FlowRun。原生会话的中断/继续用 `flowweave agent interrupt|resume <binding-id>`；停止 FlowRun 用 `flowweave run cancel`（终态，不可靠改回场景状态恢复）。

## 删除

`case delete` 与 `case conversation-delete` 不可逆；先 `case get` 读取精确 ID，用 `--dry-run` 核对，仅在用户对该精确 ID 明确授权后执行。删除场景不得被当作清理 FlowRun/Runtime 的手段。

## 排障

- 启动被拒：读错误码，常见为路径不完整、模板未就绪、会话配置过期、供应商/认证不可用。
- 事件未回访：看 `feedback_deliveries` 状态（`AMBIGUOUS` 表示提交结果不确定，已停止该轮次自动投递，需人工接管，不要重复提交）。
- 未知的 `case` 子命令或字段，以在线 OpenAPI 为准：`flowweave openapi`。
