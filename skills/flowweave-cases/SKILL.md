---
name: flowweave-cases
description: 创建、复制、导入导出、配置和监控 FlowWeave 场景空间（Case），处理协调会话、阶段、控制计划与提前结论确认；节点执行转 flowweave-flowrun-workbench。
---

# FlowWeave 场景空间（Case）

**开始前先完整阅读 `../flowweave/SKILL.md`。** 场景是带目标、有序阶段路径、资料和协调 Agent 配置的工作空间。启动后由一个原生协调 Agent 会话判断并通过平台受控工具驱动 FlowRun；平台负责校验、授权、事件投递和审计，不用固定顺序替 Agent 做业务判断。

## 核心事实

- 名称、工作区、说明、目标创建后固定；资料、路径、会话配置可继续管理。
- 阶段的 `flow_run_id` 是**模板**，场景永不直接启动它；`current_execution_flow_run_id` 才是由模板复制的当前独立执行。逐步模板必须是子逐步记录且流程结构有效；节点草稿不必预先配齐，由协调 Agent 在执行副本上按场景资料补配。
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

## 平台内置能力

协调会话依赖 `flowweave-case-context`、`flowweave-case-flows` 和 `flowweave-case-materials` 三个内置 MCP，由平台按部署配置的 `PLATFORM_MCP_BASE_URL` 自动治理安装，并由 Case 生命周期装配。它们在能力仓库显示为只读 `is_builtin` 项，不进入普通会话或用户可配置能力候选。

出现 `CASE_TOOL_CAPABILITY_UNAVAILABLE` 时读取轮次失败码并交由受管部署流程诊断配置和安装状态。手工受管发布限超级管理员按治理契约操作，普通 Case 操作继续通过业务入口完成。

## 启动场景（唯一入口）

平台不直接启动任何流程。`case start` 只向协调 Agent 发出一次**场景启动事件**，由 Agent 读取目标与资料、判断是否满足启动条件，再通过受控动作复制模板、配置执行副本并启动。它可能创建新的原生会话并占用模型额度，**只在用户明确要求时执行**。必须带 `Idempotency-Key`，超时重试复用同一个 key。

```bash
# session=CURRENT 在现有协调会话里投递启动事件（无会话时自动新建）；session=NEW 新建会话，历史会话保留
flowweave case start <case-id> --data '{"session":"CURRENT","expected_row_version":<row_version>}' -H 'Idempotency-Key: <uuid>' --dry-run
```

返回后轮询 `case get`/`case round`，等出现 `control_conversation_binding_id`；再用 `flowweave agent conversation` / `agent hydration` 读取会话。轮次失败用 `case round-retry`（复用原身份）。

## 阶段与受控动作

```bash
flowweave case stage-rerun  <case-id> <stage-id>        # 重置已结束阶段的执行指针；之后再 case start 由 Agent 重新复制并启动
flowweave case stage-bind   <case-id> <stage-id> --data '{"flow_run_id":"...","execution_mode":"STEPWISE"}'
flowweave case plan-create  <case-id> --data-file ./.tmp/plan.json   # action、idempotency_key、reason、node_run_id、node_attempt_id、input_snapshot
flowweave case plan-execute <case-id> <plan-id>
flowweave case plan-retry   <case-id> <plan-id>
flowweave case proposal-create|proposal-execute ...
```

控制计划 `action` 以在线 `CaseControlPlanWrite` 为准，包括执行副本创建与配置、逐步节点启动／接受／拒绝／取消／重跑、停止执行、连续或逐步记录复制、阶段推进、完成及 `CONCLUDE_CASE`。请求还必须包含当前 `stage_id`，`idempotency_key` 须匹配 `^[a-z][a-z0-9._:-]{0,139}$`。正常情况下动作由协调 Agent 经原生 MCP 工具发起，人工接管须符合用户授权，执行目标只能是活动 `current_execution_flow_run_id`。

执行结果以持久记录为准：看计划/提案的状态、失败码，并复读对应 FlowRun；不要把"HTTP 返回了"当作"已启动"。

## 暂停与恢复

```bash
flowweave case pause  <case-id> --expected-row-version <n>
flowweave case resume <case-id> --expected-row-version <n>
```

暂停只阻止新的自主动作，不中断原生会话，也不取消已启动的 FlowRun。原生会话的中断/继续用 `flowweave agent interrupt|resume <workspace-id> <binding-id>`，两个 ID 均从当前 Case 和会话公开读取结果取得。停止 FlowRun 用 `flowweave run cancel`，取消后保留终态记录。

## 提前结论与待回答问题

`CONCLUDE_CASE` 会提出提前结束结论并暂停场景，等待用户确认。先 `case get` 审阅结论、暂停原因和当前 `row_version`，再用 `case conclusion-confirm <case-id> --data-file ./.tmp/conclusion.json --dry-run` 确认，或用 `case conclusion-reject` 拒绝。请求使用当前 `expected_row_version` 与可选 `note`，仅在用户明确选择后执行。

协调 Agent 的人类提问来自原生 `ask_user`。通过其主 binding 的 `agent pending-question <workspace-id> <binding-id>` 读取问题，再按会话消息契约回答，场景判断以正式会话与平台结论状态为准。

## 复制与导入导出

`case copy <case-id>` 通过服务器复制场景配置。`case export --data '{"case_ids":["<case-id>"]}'` 返回 `flowweave.case-bundle` 版本 1 文档，`case import --data-file ./.tmp/cases.json --dry-run` 审阅后创建新场景。先读取精确 Case ID，复用平台导出的结构，检查资料缺失与模板引用可见性。复制和导入会创建配置副本，完成后重新读取路径、资料及会话配置，再决定是否启动协调会话。

## 删除

`case delete` 与 `case conversation-delete` 不可逆；先 `case get` 读取精确 ID，用 `--dry-run` 核对，仅在用户对该精确 ID 明确授权后执行。删除场景不得被当作清理 FlowRun/Runtime 的手段。

## 排障

- 启动被拒：读错误码，常见为路径不完整、模板未就绪、会话配置过期、供应商/认证不可用。
- 事件未回访：看 `feedback_deliveries` 状态（`AMBIGUOUS` 表示提交结果不确定，已停止该轮次自动投递，需人工接管，不要重复提交）。
- 未知的 `case` 子命令或字段，以在线 OpenAPI 为准：`flowweave openapi`。
